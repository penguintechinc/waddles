//! Host capability implementations serviced on the stage side of the
//! host-API connection (spec §7.4): the executor issues a `host-call`
//! frame naming a `capability`/`op`, and this module answers it, replying
//! `host-result`.
//!
//! **Capability scope is resolved per invoke, never per connection**
//! (post-M3 security review finding). One host-API connection multiplexes
//! many `invoke`s, potentially for different `(tenant, community, app_id)`
//! activations sharing the same executor replica; a capability handler
//! that was scoped once at connection-construction time would answer every
//! host-call on that connection under the FIRST invoke's tenant, a latent
//! cross-tenant bug. [`InvokeScope`] is threaded per call instead:
//! `crate::host_api::Connection::invoke` records the scope for the `invoke`
//! frame's own id before sending it, and the connection's read loop looks
//! it up again by the `host-call` frame's `call_id` (which spec §6.6
//! defines as "the originating `invoke` id") before ever calling
//! [`CapabilityHandler::handle`]. A `call_id` with no matching in-flight
//! invoke -- forged, stale, or from a different connection -- is refused
//! outright (`unknown_invoke`), never answered against a guessed or
//! leftover scope.
//!
//! `relay` (Twitch outbound via Valkey `LPUSH`, drained by svc-ingest's own
//! persistent IRC connection; Discord outbound via a direct, stateless bot
//! REST send -- see [`StageCapabilities::handle_discord_relay`]'s doc for
//! why Discord takes a different path than Twitch), `clock`, `context`,
//! `log`, and `kv` are fully wired. `http` is wired to
//! `crate::egress::EgressGuard` (spec §8's full SSRF guard). `kv` is wired
//! to `bundle_host_kv::KvHost` (the crate shared with `core/svc_process` --
//! see that crate's own module doc for the key-derivation/isolation/quota
//! design) over the same direct Valkey connection this stage already opens
//! for `relay`/usage metering (see [`Self::with_kv`]'s doc for why that
//! connection is reused rather than a second one opened). `db` remains a
//! documented seam -- see [`StageCapabilities::handle`]'s match arm for
//! what a real wiring needs and where that design is tracked.
//!
//! A bundle never holds a platform credential (spec §4.3): every
//! capability here resolves any credential itself, from this process's own
//! configuration/environment, never from the `args` the guest supplied.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

use bundle_capability_gate::{
    AppScopedResource, CapabilityGate, Denied, HostInvokeScopeBuilder, PermissionId, ResourceRef,
    TenantTier,
};
use bundle_host_kv::{KvBackend, KvError, KvHost, KvScope};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};

use crate::egress::EgressGuard;
use crate::usage::UsageBatcher;

/// The `(tenant, community, app_id)` an in-flight `invoke` belongs to,
/// resolved from the delivered envelope (spec §5.11: "Tenant and community
/// come from the key, never from payload") and carried alongside that
/// invoke's frame id for the lifetime of the call -- see the module doc.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InvokeScope {
    pub tenant: String,
    pub community: Option<String>,
    pub app_id: String,
    /// The delivered envelope's own `event.source.channel_id` (spec
    /// `penguin_spine::envelope::Source`) -- the platform channel/guild/room
    /// the *inbound* event that triggered this invoke actually came from,
    /// when the platform has one. Threaded through by
    /// `crate::dispatch::invoke_dispatch` so the `relay` capability's
    /// Discord path can reply into the right channel without ever trusting
    /// a bundle-supplied channel argument (`handle_discord_relay`'s doc).
    /// `None` for Twitch relay sends (which still take `channel` from the
    /// bundle's own `message_json`, unchanged) and for any invoke with no
    /// channel-bearing origin event.
    pub origin_channel_id: Option<String>,
    /// Numeric `tenants.id`/`communities.id` (spec
    /// `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
    /// SS5.1/SS4 `GrantScopeKey`) -- resolved by `crate::dispatch::
    /// handle_delivered` via `TenantResolver` before this scope is built
    /// (never guest-suppliable). `community_id` is `0` for a tenant-wide
    /// activation, matching `bundle_active_set`'s existing sentinel
    /// convention.
    pub tenant_id: i32,
    pub community_id: i32,
    /// The approved `(app_id, version)` row id this invoke's grants are
    /// pinned to (spec SS4). **Interim placeholder** until the real
    /// `app_versions.id` is threaded through `crate::dispatch`'s bundle-load
    /// path (tracked as follow-on work, same "interim substitute" posture
    /// this crate already documents for `ProcessDeps::version`): every
    /// invocation today resolves to `0` here, which only ever matches a
    /// grant row also written under version `0` -- never a real approved
    /// version, so this fails closed (denies every non-platform permission)
    /// rather than silently matching the wrong version's grants.
    pub app_version: i64,
}

impl InvokeScope {
    /// Builds the gate's host-only [`bundle_capability_gate::InvokeScope`]
    /// from this already-trusted scope (spec SS5.1: `HostInvokeScopeBuilder`
    /// is the sole constructor). `tenant_tier` is not yet resolved per
    /// invocation (no capability wired in this stage today reads it) and
    /// defaults to [`TenantTier::Free`] -- documented here rather than
    /// silently guessed, so the follow-on task that wires `storage.objects`
    /// (spec SS6) knows exactly what to replace.
    fn gate_scope(&self) -> bundle_capability_gate::InvokeScope {
        HostInvokeScopeBuilder::new()
            .tenant_id(self.tenant_id)
            .community_id(self.community_id)
            .app_id(self.app_id.clone())
            .app_version(self.app_version)
            .tenant_tier(TenantTier::Free)
            .build()
            // `app_id` is always non-empty here: it is always
            // `deps.app_id`, itself sourced from a live `app_catalog` row
            // (never guest input, never constructed empty) -- see
            // `crate::dispatch::DispatchDeps::app_id`'s doc.
            .expect("InvokeScope::app_id is never empty for a live invocation")
    }
}

/// Maps a gate [`Denied`] onto the `{code, message}` shape every `host-call`
/// error reply carries (spec SS5.4's stable `reason` vocabulary).
fn denied_from_gate(err: Denied) -> HostResultError {
    denied(err.reason_str(), err.to_string())
}

/// Best-effort host extraction for `net.http:<host>` (spec SS1) from the
/// `http.send` host-call's own `{"url": ...}` arg -- no `url` crate
/// dependency in this crate, so this is a small manual scheme/port strip
/// rather than a full URL parse. `crate::egress::EgressGuard::send` (called
/// only once this permission is authorized) performs the real, security-
/// relevant URL validation/SSRF guarding on this same `url` string; this
/// function only needs a good-enough host to select which per-host
/// permission grant to check.
fn extract_http_host(args: &serde_json::Value) -> Option<String> {
    let url = args.get("url").and_then(|v| v.as_str())?;
    let after_scheme = url.split("://").nth(1).unwrap_or(url);
    let host_port = after_scheme.split('/').next().unwrap_or(after_scheme);
    let host = host_port.split(':').next().unwrap_or(host_port);
    if host.is_empty() {
        None
    } else {
        Some(host.to_ascii_lowercase())
    }
}

/// Classifies an extracted `http.send` host into the specific
/// [`PermissionId`] family `bundle_capability_gate`'s catalog actually
/// grants against (`NetHttpFqdn`/`NetHttpPublicIp`/`NetHttpPrivateIp`) --
/// a literal IP address is classified directly (no DNS lookup, since only
/// literal-IP syntax can be classified without one); anything else is a
/// hostname, gated as `NetHttpFqdn`. This is a permission-family choice,
/// NOT the SSRF wall itself -- `crate::egress::EgressGuard::send`'s own
/// resolved-IP `is_forbidden_address` check remains the authoritative
/// defense against a hostname that DNS-resolves to a private address; a
/// bundle merely holding `net.http.fqdn` never bypasses that downstream
/// check.
fn classify_net_http_permission(host: &str) -> PermissionId {
    match host.parse::<std::net::IpAddr>() {
        Ok(std::net::IpAddr::V4(v4)) => {
            if v4.is_private() || v4.is_loopback() || v4.is_link_local() {
                PermissionId::NetHttpPrivateIp(host.to_string())
            } else {
                PermissionId::NetHttpPublicIp(host.to_string())
            }
        }
        Ok(std::net::IpAddr::V6(v6)) => {
            let is_unique_local = (v6.segments()[0] & 0xfe00) == 0xfc00;
            if v6.is_loopback() || v6.is_unicast_link_local() || is_unique_local {
                PermissionId::NetHttpPrivateIp(host.to_string())
            } else {
                PermissionId::NetHttpPublicIp(host.to_string())
            }
        }
        Err(_) => PermissionId::NetHttpFqdn(host.to_string()),
    }
}

/// Answers one `host-call` for a given `capability`/`op`, scoped to the
/// invoke it happened during. Object-safe (a manually-boxed future rather
/// than `async fn` in a trait) so the host-API connection can hold
/// `Arc<dyn CapabilityHandler>` without an `async_trait` dependency.
pub trait CapabilityHandler: Send + Sync {
    /// Services one host call under `scope` (resolved by the caller from
    /// the `call_id`'s own in-flight invoke -- never guessed, never a
    /// connection-wide default), returning the capability-specific JSON
    /// result or a `{code, message}` error the executor forwards to the
    /// guest as `error-code::access-denied`/`internal-error` per the WIT
    /// world's `host-error` variant (spec §6.5).
    fn handle<'a>(
        &'a self,
        scope: &'a InvokeScope,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>>;
}

pub(crate) fn denied(code: &str, message: impl Into<String>) -> HostResultError {
    HostResultError {
        code: code.to_string(),
        message: message.into(),
    }
}

/// The Valkey list key an outbound relay send `LPUSH`es onto for
/// `provider` -- a byte-exact Rust port of `waddle_transports.transports.
/// irc_relay.outbound_queue_key` (`libs/waddle_transports` in this repo):
/// `waddles:transport:irc:{provider}:outbound`. One key per provider (not
/// per tenant/community), matching the inbound side's single-socket model.
pub fn outbound_relay_queue_key(provider: &str) -> String {
    format!("waddles:transport:irc:{provider}:outbound")
}

/// Compiled-in providers the `relay` capability accepts. Spec §7.4 (as
/// published) still reads "the compiled-in provider list (`twitch`
/// today)" -- `discord` extends that list in this landing; the two
/// providers take genuinely different code paths in
/// [`StageCapabilities::handle_relay`] (Valkey `LPUSH` vs. a direct bot
/// REST send), documented on that function and
/// [`StageCapabilities::handle_discord_relay`]. Action-stage bundles only.
const RELAY_PROVIDERS: &[&str] = &["twitch", "discord"];

/// Strips CR/LF and every other control character before an outbound relay
/// write -- a byte-exact port of `waddle_transports.transports.irc.
/// sanitize_irc_component`'s CRLF-injection defense, applied here (not just
/// by the eventual IRC-writing process) so a malformed payload is rejected
/// before it is ever queued.
fn sanitize_irc_component(value: &str) -> String {
    value.chars().filter(|c| !c.is_control()).collect()
}

/// Discord snowflake IDs are unsigned 64-bit integers rendered as decimal
/// ASCII digits. Validated purely as defense in depth before `channel_id`
/// reaches a URL path segment -- `scope.origin_channel_id` is sourced from
/// the binding-MAC-verified envelope, never bundle input, but a malformed
/// value should still fail closed rather than build a bogus request URL.
fn is_discord_snowflake(id: &str) -> bool {
    !id.is_empty() && id.chars().all(|c| c.is_ascii_digit())
}

/// Pins DNS resolution for `discord.com` to a single address that passes
/// [`crate::egress::is_forbidden_address`] (loopback/private/link-local/
/// multicast/cloud-metadata all rejected, `allow_private` hardcoded
/// `false` here -- this built-in has no per-tenant escape hatch,
/// unlike bundle-declared egress's spec §8.5 `allowPrivateHosts`).
async fn resolve_discord_address() -> Result<std::net::SocketAddr, HostResultError> {
    let mut addrs = tokio::net::lookup_host(("discord.com", 443))
        .await
        .map_err(|e| {
            denied(
                "relay_unavailable",
                format!("discord.com dns resolution failed: {e}"),
            )
        })?;
    addrs
        .find(|addr| crate::egress::is_forbidden_address(addr.ip(), false).is_none())
        .ok_or_else(|| denied("relay_unavailable", "no permitted address for discord.com"))
}

/// This crate's own sanity bound on a bundle's `log` host-call message
/// length -- the spec (§5.12/§7.4) does not set one; capped so a
/// buggy/malicious bundle cannot force unbounded memory/log-volume via a
/// single `host-call` (the "never trust guest input" posture
/// [`sanitize_irc_component`] already applies to the `relay` capability).
const MAX_BUNDLE_LOG_MESSAGE_LEN: usize = 4096;

/// Sanitizes a bundle-supplied `log` host-call `message` before it ever
/// reaches `tracing` (spec §7.4: "`fields-json` is sanitized with the
/// `penguin-logging` `SENSITIVE_KEYS` rule before anything is emitted").
/// Three defenses, in order: (1) `penguin_logging::sanitize::sanitize_object`
/// redacts an email-shaped value (the same shared sanitizer every other
/// pipeline log line and OTel record passes through); (2)
/// [`sanitize_irc_component`] strips CR/LF and every other control
/// character, the same CRLF-injection defense `handle_relay` already
/// applies, so a bundle can't forge extra log lines or terminal escape
/// sequences; (3) truncation to [`MAX_BUNDLE_LOG_MESSAGE_LEN`] `char`s
/// (not bytes, so multi-byte UTF-8 is never split mid-codepoint). Free
/// function (not a method) so it's unit-testable without a `tracing`
/// subscriber capturing the eventual log line.
fn sanitize_bundle_log_message(raw_message: &str) -> String {
    let mut wrapped = serde_json::Map::with_capacity(1);
    wrapped.insert(
        "message".to_string(),
        serde_json::Value::String(raw_message.to_string()),
    );
    let sanitized = penguin_logging::sanitize::sanitize_object(&wrapped);
    let sanitized_message = sanitized
        .get("message")
        .and_then(|v| v.as_str())
        .unwrap_or("<empty>");

    let mut message = sanitize_irc_component(sanitized_message);
    if message.chars().count() > MAX_BUNDLE_LOG_MESSAGE_LEN {
        message = message.chars().take(MAX_BUNDLE_LOG_MESSAGE_LEN).collect();
    }
    message
}

/// The one Valkey operation the `relay` capability needs -- narrow and easy
/// to fake in tests (mirrors `libs/waddle_transports`'s own
/// `RelayRedisLike` protocol on the Python side).
pub trait RelayQueue: Send + Sync {
    fn lpush<'a>(
        &'a self,
        key: &'a str,
        value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>>;
}

impl RelayQueue for redis::aio::MultiplexedConnection {
    fn lpush<'a>(
        &'a self,
        key: &'a str,
        value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        let mut conn = self.clone();
        Box::pin(async move {
            redis::AsyncCommands::lpush::<_, _, ()>(&mut conn, key, value)
                .await
                .map_err(|e| e.to_string())
        })
    }
}

/// Fixed operational limits for the built-in Discord relay send. Not
/// `crate::egress::EgressLimits` (that struct governs *bundle*-declared
/// `http.send` policy, one bucket per `app_id`, spec §7.3/§8.2) -- this is
/// a stage built-in with exactly one compiled-in destination and no
/// per-bundle manifest to read limits from, so it gets its own small,
/// fixed defaults rather than threading CLI config through for a single
/// hardcoded host.
const DISCORD_RELAY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);
const DISCORD_RELAY_MAX_RESPONSE_BYTES: usize = 65_536;

/// Discord's REST API version this send targets (spec: relay providers).
const DISCORD_API_BASE: &str = "https://discord.com/api/v10";

/// The Discord relay send's own dependencies, present only once
/// [`StageCapabilities::with_discord`] configures them -- absent (`None`)
/// means `DISCORD_BOT_TOKEN` was not set at startup, and every Discord
/// relay send is refused `relay_unavailable` rather than the process
/// failing to start or a bundle-runtime credential ever being consulted
/// (module doc: "resolves any credential itself, from this process's own
/// configuration/environment").
struct DiscordRelay {
    transport: Arc<dyn crate::egress::HttpTransport>,
    bot_token: crate::config::Secret,
}

/// The real capability implementations this stage wires today. Generic
/// over [`RelayQueue`] so `handle_relay` is fully unit-testable against a
/// fake queue without a live Valkey server -- production callers
/// instantiate `StageCapabilities<redis::aio::MultiplexedConnection>`.
/// Also generic over [`KvBackend`] (defaulted to the same production
/// connection type) for the identical reason: `handle_kv`'s argument-
/// parsing/error-mapping is unit-testable against a fake implementing the
/// public `bundle_host_kv::KvBackend` trait, with no live Valkey server --
/// see this module's `tests::FakeKvBackend`. Holds no per-connection
/// tenant/community/app_id (see the module doc -- that scope now arrives
/// per call via [`InvokeScope`]).
pub struct StageCapabilities<Q: RelayQueue, K: KvBackend = redis::aio::MultiplexedConnection> {
    relay_queue: Q,
    egress: Arc<EgressGuard>,
    /// Shared with the dispatch loop's own `DispatchDeps::usage` so a
    /// `relay`/`http` host call's outbound byte count is metered alongside
    /// the same activation's `actions_delivered` (spec §5.12/D31: "host
    /// calls by kind"). Recorded under an empty `workstream_id`: unlike
    /// `dispatch::handle_delivered`, which knows the delivered envelope's
    /// real `workstream_id`, a host call's `InvokeScope` doesn't carry one
    /// (spec §5.11: "No bundle host call accepts a tenant or community
    /// argument at all" -- the same constraint extends to workstream_id).
    usage: Arc<Mutex<UsageBatcher>>,
    /// See [`DiscordRelay`]'s doc; `None` until [`Self::with_discord`] is
    /// called.
    discord: Option<DiscordRelay>,
    /// See [`Self::with_kv`]'s doc; `None` until it is called (mirrors
    /// [`Self::discord`]'s graceful-degradation shape: a bundle sees
    /// `not_implemented` rather than this process failing to start if a
    /// live Valkey connection for `kv` was never configured).
    kv: Option<KvHost<K>>,
    /// The standard enforcement gate (spec SS5) every arm of
    /// [`CapabilityHandler::handle`] calls first. Mandatory, never
    /// bypassable (spec SS5.2 "no kill-switch") -- see [`Self::new`]'s doc.
    gate: Arc<CapabilityGate>,
}

impl<Q: RelayQueue, K: KvBackend> StageCapabilities<Q, K> {
    /// Builds the capability set this connection's read loop answers every
    /// `host-call` against, for as long as the connection lives. No
    /// tenant/community/app_id here -- every capability method below takes
    /// its [`InvokeScope`] as a parameter instead (module doc). The Discord
    /// relay provider and the `kv` backend both start unconfigured
    /// (`relay_unavailable`/`not_implemented` until [`Self::with_discord`]/
    /// [`Self::with_kv`] are chained on) so every existing caller of this
    /// constructor -- production and test alike -- is unaffected by this
    /// landing. `gate` is mandatory: every arm calls `gate.authorize()`
    /// first (spec SS5), typically backed by
    /// `crate::grant_gate::AlwaysGrantedLoader` so `context`/`clock`/`log`
    /// keep working even before the sibling grants migration lands, while
    /// every other permission fails closed with no grant data.
    pub fn new(
        relay_queue: Q,
        egress: Arc<EgressGuard>,
        usage: Arc<Mutex<UsageBatcher>>,
        gate: Arc<CapabilityGate>,
    ) -> Self {
        Self {
            relay_queue,
            egress,
            usage,
            discord: None,
            kv: None,
            gate,
        }
    }

    /// Enables the `kv` capability over `backend` (production:
    /// `redis::aio::MultiplexedConnection` -- the exact same direct Valkey
    /// connection `lib.rs`'s `build_stage_capabilities` already opens for
    /// `relay`/usage metering, cloned rather than opening a second
    /// connection, since `MultiplexedConnection::clone` is a cheap handle
    /// clone over one shared TCP connection, not a new socket). Builder-
    /// style so a deployment where the Valkey connection failed to open can
    /// still construct every other capability and simply skip this call,
    /// the same pattern [`Self::with_discord`] already established.
    pub fn with_kv(mut self, backend: K) -> Self {
        self.kv = Some(KvHost::new(backend));
        self
    }

    /// Enables the Discord relay provider, given the transport to send
    /// through (production: `crate::egress::ReqwestTransport`; tests: a
    /// fake -- see this module's `tests::FakeDiscordTransport`) and the
    /// bot token to authenticate with (`DISCORD_BOT_TOKEN`, the exact same
    /// secret the Helm chart already provisions for svc-ingest's Discord
    /// Gateway connection -- `crate::config::Config::discord_bot_token`'s
    /// doc). Builder-style so `lib.rs`'s real construction site can skip
    /// this call entirely when the token is unset, rather than needing an
    /// `Option`-wrapped transport threaded through [`Self::new`] itself.
    pub fn with_discord(
        mut self,
        transport: Arc<dyn crate::egress::HttpTransport>,
        bot_token: crate::config::Secret,
    ) -> Self {
        self.discord = Some(DiscordRelay {
            transport,
            bot_token,
        });
        self
    }

    async fn handle_relay(
        &self,
        scope: &InvokeScope,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostResultError> {
        let provider = args
            .get("provider")
            .and_then(|v| v.as_str())
            .ok_or_else(|| denied("invalid_args", "relay.send requires a 'provider' string"))?;
        if !RELAY_PROVIDERS.contains(&provider) {
            return Err(denied(
                "unknown_provider",
                format!("relay provider {provider:?} is not in the compiled-in allowlist"),
            ));
        }
        // Gate call FIRST (spec SS5), now that `provider` is known well
        // enough to name the specific `chat.send:<platform>` permission id
        // this call maps to.
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::ChatSend(provider.to_string()),
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        // WIT `relay.push(provider: string, message-json: string)`
        // (`wit/waddle-bundle/stage.wit`) carries the message as an
        // opaque, provider-shaped JSON *string* -- `channel`/`text` are
        // fields INSIDE `message_json`, never top-level host-call args.
        // `core/bundle_executor/src/host/imports.rs`'s `relay::Host::push`
        // proxies exactly `{"provider", "message_json"}` over the host-API
        // wire; a flat `{"provider","channel","text"}` args object (this
        // function's prior shape) is never what a real invoke sends --
        // every real relay call hit `invalid_args: requires a non-empty
        // 'channel'` regardless of the bundle's actual message, caught by
        // the hermetic relay e2e proof (`fix/svc-action-bundle-executor-
        // alpha-wiring`).
        let message_json = args
            .get("message_json")
            .and_then(|v| v.as_str())
            .ok_or_else(|| {
                denied(
                    "invalid_args",
                    "relay.send requires a 'message_json' string",
                )
            })?;
        let message: serde_json::Value = serde_json::from_str(message_json).map_err(|e| {
            denied(
                "invalid_args",
                format!("relay.send message_json is not valid JSON: {e}"),
            )
        })?;
        // `text` is common to every provider; `channel` resolution below is
        // NOT -- Discord branches off before ever looking at
        // `message.channel` (see `handle_discord_relay`'s doc for why).
        let text = message
            .get("text")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                denied(
                    "invalid_args",
                    "relay.send message_json requires non-empty 'text'",
                )
            })?;

        if provider == "discord" {
            return self.handle_discord_relay(scope, text).await;
        }

        // Twitch, the only other compiled-in provider: `channel` comes from
        // the bundle's own `message_json`, unchanged from this capability's
        // original (Twitch-only) landing.
        let channel = message
            .get("channel")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                denied(
                    "invalid_args",
                    "relay.send message_json requires a non-empty 'channel'",
                )
            })?;

        let channel = sanitize_irc_component(channel);
        let text = sanitize_irc_component(text);
        if channel.is_empty() || text.is_empty() {
            return Err(denied(
                "invalid_args",
                "relay.send requires a non-empty 'channel' and 'text' after sanitization",
            ));
        }

        let key = outbound_relay_queue_key(provider);
        let payload = serde_json::json!({"channel": channel, "text": text}).to_string();
        let outbound_bytes = payload.len() as u64;
        self.relay_queue
            .lpush(&key, payload)
            .await
            .map_err(|e| denied("relay_unavailable", e))?;
        // spec §5.12/D31: "host calls by kind" -- see the `usage` field's
        // doc for why `workstream_id` is an empty placeholder here.
        self.usage
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .record_relay_call(
                &scope.tenant,
                scope.community.as_deref(),
                "",
                &scope.app_id,
                outbound_bytes,
            );
        Ok(serde_json::json!({"queued": true, "provider": provider}))
    }

    /// Discord relay send: a stateless bot REST `POST
    /// /channels/{channel_id}/messages` with `Authorization: Bot <token>`
    /// -- no persistent Gateway (websocket) connection is needed to *send*,
    /// only to *receive*. Deliberately **not** the Twitch pattern (`LPUSH`
    /// onto a Valkey list drained by svc-ingest's own persistent
    /// connection): svc-ingest owns the inbound Discord Gateway connection
    /// because receiving genuinely needs one, but sending needs no such
    /// thing, so routing a reply through svc-ingest would only add a
    /// dependency this send has no actual use for -- and svc-ingest's
    /// connection is flaky, exactly the failure mode this path avoids.
    ///
    /// **The channel is never the bundle's to name.** Unlike Twitch (whose
    /// `channel` comes straight from the bundle's own `message_json`,
    /// unchanged above), Discord's target channel is `scope.
    /// origin_channel_id` -- threaded from the delivered envelope's own
    /// `event.source.channel_id` by `crate::dispatch::invoke_dispatch`,
    /// never a bundle-runtime literal. A bundle-chosen Discord channel id
    /// would let any bundle holding this capability post into *any*
    /// channel the shared bot account is a member of, cluster-wide --
    /// crossing every tenant/community boundary this capability set is
    /// otherwise built to hold (module doc, `crate::hop`'s D30 tenant
    /// wall). Replying only into the channel the triggering event actually
    /// came from closes that off by construction, the same way `crate::
    /// senders::discord_webhook_url_from_config`'s doc reasons about a
    /// bundle-chosen webhook URL.
    async fn handle_discord_relay(
        &self,
        scope: &InvokeScope,
        text: &str,
    ) -> Result<serde_json::Value, HostResultError> {
        let Some(discord) = &self.discord else {
            return Err(denied(
                "relay_unavailable",
                "discord relay is not configured on this stage (DISCORD_BOT_TOKEN unset)",
            ));
        };
        let channel_id = scope.origin_channel_id.as_deref().ok_or_else(|| {
            denied(
                "invalid_args",
                "discord relay requires an origin channel id on the delivered envelope",
            )
        })?;
        if !is_discord_snowflake(channel_id) {
            return Err(denied(
                "invalid_args",
                "discord relay origin channel id is not a valid snowflake",
            ));
        }

        // Spec §8.2 steps 6-7's SSRF-pinning discipline, reused here even
        // though `discord.com` is a compiled-in host rather than a
        // bundle-declared one (`crate::egress::is_forbidden_address`'s doc):
        // defense in depth against DNS-rebinding this process's own
        // trusted egress path onto an internal address.
        let pinned_addr = resolve_discord_address().await?;
        let url = format!("{DISCORD_API_BASE}/channels/{channel_id}/messages");
        let body = serde_json::json!({ "content": text })
            .to_string()
            .into_bytes();
        let outbound_bytes = body.len() as u64;
        let request = crate::egress::TransportRequest {
            method: "POST".to_string(),
            url,
            pinned_addr,
            headers: vec![
                (
                    "Authorization".to_string(),
                    format!("Bot {}", discord.bot_token.expose()),
                ),
                ("Content-Type".to_string(), "application/json".to_string()),
            ],
            body: Some(body),
        };
        let response = discord
            .transport
            .send(
                request,
                DISCORD_RELAY_TIMEOUT,
                DISCORD_RELAY_MAX_RESPONSE_BYTES,
            )
            .await?;
        if !(200..300).contains(&response.status) {
            return Err(denied(
                "relay_unavailable",
                format!("discord API returned status {}", response.status),
            ));
        }

        // spec §5.12/D31: "host calls by kind" -- see the `usage` field's
        // doc for why `workstream_id` is an empty placeholder here.
        self.usage
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .record_relay_call(
                &scope.tenant,
                scope.community.as_deref(),
                "",
                &scope.app_id,
                outbound_bytes,
            );
        Ok(serde_json::json!({"sent": true, "provider": "discord"}))
    }

    fn handle_clock(
        &self,
        scope: &InvokeScope,
        op: &str,
    ) -> Result<serde_json::Value, HostResultError> {
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::PlatformClock,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        match op {
            "now-millis" => {
                let millis = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis() as u64)
                    .unwrap_or(0);
                Ok(serde_json::json!(millis))
            }
            other => Err(denied(
                "unknown_op",
                format!("clock op {other:?} not supported"),
            )),
        }
    }

    fn handle_context(&self, scope: &InvokeScope) -> Result<serde_json::Value, HostResultError> {
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::PlatformContext,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        // Spec §7.4: "Tenant and community come from the key, never from
        // payload." Never includes a credential or secret.
        Ok(serde_json::json!({
            "tenant": scope.tenant,
            "community": scope.community,
            "app_id": scope.app_id,
        }))
    }

    fn handle_log(
        &self,
        scope: &InvokeScope,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostResultError> {
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::PlatformLog,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        let level = args.get("level").and_then(|v| v.as_str()).unwrap_or("info");
        let raw_message = args
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("<empty>");
        let message = sanitize_bundle_log_message(raw_message);
        let message = message.as_str();

        match level {
            "error" => {
                tracing::error!(app_id = %scope.app_id, tenant = %scope.tenant, bundle_log = %message, "bundle log")
            }
            "warn" => {
                tracing::warn!(app_id = %scope.app_id, tenant = %scope.tenant, bundle_log = %message, "bundle log")
            }
            "debug" => {
                tracing::debug!(app_id = %scope.app_id, tenant = %scope.tenant, bundle_log = %message, "bundle log")
            }
            _ => {
                tracing::info!(app_id = %scope.app_id, tenant = %scope.tenant, bundle_log = %message, "bundle log")
            }
        }
        Ok(serde_json::json!({}))
    }

    /// `kv.get`/`kv.set`/`kv.delete`/`kv.increment` (`wit/waddle-bundle/
    /// stage.wit` `interface kv`). Every argument shape here matches
    /// exactly what `core/bundle_executor::host::imports`'s `kv::Host`
    /// impl sends (`{"key"}`, `{"key","value","ttl_seconds"}`,
    /// `{"key","delta","ttl_seconds"}`) and expects back
    /// (`{"value": ...}`) -- see that module's doc for the wire contract
    /// this must not drift from. Tenant/community/app_id come from `scope`
    /// (never `call.app_id`, which is executor-set metadata, not a trust
    /// boundary this capability re-derives its own scope from -- module
    /// doc: "every capability here resolves its own scope from `self`").
    async fn handle_kv(
        &self,
        scope: &InvokeScope,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        // Gate call FIRST (spec SS5) -- `storage.kv`, `AppScoped`. This
        // supersedes `bundle_host_kv::authorize::authorize_kv`'s interim
        // always-grant stand-in as the real security boundary; that inner
        // seam remains harmlessly redundant until it is retired in a
        // follow-on cleanup.
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::StorageKv,
                ResourceRef::AppScoped(AppScopedResource::KvState),
            )
            .map_err(denied_from_gate)?;
        let Some(kv) = &self.kv else {
            return Err(denied(
                "not_implemented",
                "kv capability is not configured on this stage (no Valkey connection)",
            ));
        };
        let kv_scope = KvScope::new(
            scope.tenant.clone(),
            scope.community.clone(),
            scope.app_id.clone(),
        );

        match call.op.as_str() {
            "get" => {
                let args: KvKeyArgs = parse_kv_args(&call.args)?;
                let value = kv
                    .get(&kv_scope, call.call_id, &args.key)
                    .await
                    .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({ "value": value }))
            }
            "set" => {
                let args: KvSetArgs = parse_kv_args(&call.args)?;
                kv.set(
                    &kv_scope,
                    call.call_id,
                    &args.key,
                    &args.value,
                    args.ttl_seconds,
                )
                .await
                .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({}))
            }
            "delete" => {
                let args: KvKeyArgs = parse_kv_args(&call.args)?;
                kv.delete(&kv_scope, call.call_id, &args.key)
                    .await
                    .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({}))
            }
            "increment" => {
                let args: KvIncrementArgs = parse_kv_args(&call.args)?;
                let value = kv
                    .increment(
                        &kv_scope,
                        call.call_id,
                        &args.key,
                        args.delta,
                        args.ttl_seconds,
                    )
                    .await
                    .map_err(kv_err_to_host)?;
                Ok(serde_json::json!({ "value": value }))
            }
            other => Err(denied(
                "unknown_op",
                format!("kv op {other:?} not supported"),
            )),
        }
    }
}

/// `{"key": String}` -- `kv.get`/`kv.delete`'s args.
#[derive(serde::Deserialize)]
struct KvKeyArgs {
    key: String,
}

/// `{"key": String, "value": Vec<u8>, "ttl_seconds": u32}` -- `kv.set`'s args.
#[derive(serde::Deserialize)]
struct KvSetArgs {
    key: String,
    value: Vec<u8>,
    ttl_seconds: u32,
}

/// `{"key": String, "delta": i64, "ttl_seconds": u32}` -- `kv.increment`'s args.
#[derive(serde::Deserialize)]
struct KvIncrementArgs {
    key: String,
    delta: i64,
    ttl_seconds: u32,
}

fn parse_kv_args<T: serde::de::DeserializeOwned>(
    args: &serde_json::Value,
) -> Result<T, HostResultError> {
    serde_json::from_value(args.clone())
        .map_err(|e| denied("invalid_args", format!("malformed kv host-call args: {e}")))
}

/// Maps [`KvError`] onto the `{code, message}` shape every `host-call`
/// error reply carries -- [`KvError::wire_code`]/[`KvError::wire_message`]
/// already collapse to exactly the two `kv.error` variants a bundle SDK
/// knows how to render (`too_large`/everything else), so this is a direct
/// pass-through, not a second mapping layer.
fn kv_err_to_host(err: KvError) -> HostResultError {
    denied(err.wire_code(), err.wire_message())
}

impl<Q: RelayQueue, K: KvBackend> CapabilityHandler for StageCapabilities<Q, K> {
    fn handle<'a>(
        &'a self,
        scope: &'a InvokeScope,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            match call.capability {
                CapabilityKind::Relay => self.handle_relay(scope, &call.args).await,
                CapabilityKind::Clock => self.handle_clock(scope, &call.op),
                CapabilityKind::Context => self.handle_context(scope),
                CapabilityKind::Log => self.handle_log(scope, &call.args),
                CapabilityKind::Http => {
                    let host = extract_http_host(&call.args).ok_or_else(|| {
                        denied("invalid_args", "http.send requires a 'url' string")
                    })?;
                    self.gate
                        .authorize(
                            &scope.gate_scope(),
                            classify_net_http_permission(&host),
                            ResourceRef::AppScoped(AppScopedResource::None),
                        )
                        .map_err(denied_from_gate)?;
                    self.egress.send(&scope.app_id, &call.args).await
                }
                CapabilityKind::Kv => self.handle_kv(scope, &call).await,
                // `db` needs the manifest's `data.tables` allowlist plus
                // per-bundle-role Postgres RLS (`SET LOCAL waddles.tenant`/
                // `waddles.community`, spec §7.4/§11.10), and is being
                // extended further (single-statement -> transactional
                // `db.execute-batch`, a manifest `capabilities` allowlist)
                // by a separate, in-progress design -- see
                // `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md`
                // §3/§4. Gate call FIRST (spec SS5): a real grant now
                // reports `not_implemented` (the wiring seam), but an
                // ungranted call reports `not_granted` -- never silently
                // succeeding either way.
                CapabilityKind::Db => {
                    self.gate
                        .authorize(
                            &scope.gate_scope(),
                            PermissionId::StorageTables,
                            ResourceRef::AppScoped(AppScopedResource::Table),
                        )
                        .map_err(denied_from_gate)?;
                    Err(denied(
                        "not_implemented",
                        "db capability is not wired in this build",
                    ))
                }
                CapabilityKind::Flags => {
                    self.gate
                        .authorize(
                            &scope.gate_scope(),
                            PermissionId::FlagsRead,
                            ResourceRef::AppScoped(AppScopedResource::None),
                        )
                        .map_err(denied_from_gate)?;
                    Err(denied(
                        "not_implemented",
                        "flags capability is not wired in this build -- TODO(M3+)",
                    ))
                }
            }
        })
    }
}

/// A [`CapabilityHandler`] that denies every call -- used where no
/// capability set is configured (e.g. a health-check-only invocation path,
/// or the executor's Valkey/manifest dependencies are unavailable at
/// startup) or in tests exercising the host-API connection layer in
/// isolation from capability semantics.
pub struct DenyAllCapabilities;

impl CapabilityHandler for DenyAllCapabilities {
    fn handle<'a>(
        &'a self,
        _scope: &'a InvokeScope,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            Err(denied(
                "not_implemented",
                format!("{:?} capability has no handler configured", call.capability),
            ))
        })
    }
}

/// Type-erased convenience so callers can hold either implementation
/// behind one `Arc<dyn CapabilityHandler>`.
pub fn boxed(handler: impl CapabilityHandler + 'static) -> Arc<dyn CapabilityHandler> {
    Arc::new(handler)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::distribution::BundleCatalog;
    use std::sync::Mutex;

    #[derive(Default)]
    struct FakeRelayQueue {
        pushed: Mutex<Vec<(String, String)>>,
        fail: bool,
    }

    impl RelayQueue for FakeRelayQueue {
        fn lpush<'a>(
            &'a self,
            key: &'a str,
            value: String,
        ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
            Box::pin(async move {
                if self.fail {
                    return Err("simulated relay outage".to_string());
                }
                self.pushed.lock().unwrap().push((key.to_string(), value));
                Ok(())
            })
        }
    }

    /// A minimal in-memory [`KvBackend`] fake, mirroring
    /// `bundle_host_kv::backend::fake::FakeBackend`'s semantics (that one
    /// is crate-private to `bundle_host_kv`, so `handle_kv`'s own
    /// argument-parsing/error-mapping is exercised here against a fresh,
    /// independent implementation of the public `KvBackend` trait -- no
    /// live Valkey server needed for this module's own tests).
    #[derive(Default)]
    struct FakeKvBackend {
        data: Mutex<std::collections::HashMap<String, Vec<u8>>>,
        counts: Mutex<std::collections::HashMap<String, u64>>,
    }

    impl KvBackend for FakeKvBackend {
        fn get<'a>(
            &'a self,
            data_key: &'a str,
        ) -> bundle_host_kv::BoxFuture<'a, Result<Option<Vec<u8>>, String>> {
            let value = self.data.lock().unwrap().get(data_key).cloned();
            Box::pin(async move { Ok(value) })
        }

        fn set_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
            value: &'a [u8],
            _ttl_seconds: u32,
            max_keys: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bundle_host_kv::QuotaOutcome<()>, String>>
        {
            let mut data = self.data.lock().unwrap();
            let existed = data.contains_key(data_key);
            if !existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                if count >= max_keys {
                    return Box::pin(
                        async move { Ok(bundle_host_kv::QuotaOutcome::QuotaExceeded) },
                    );
                }
                counts.insert(count_key.to_string(), count + 1);
            }
            data.insert(data_key.to_string(), value.to_vec());
            Box::pin(async move { Ok(bundle_host_kv::QuotaOutcome::Admitted(())) })
        }

        fn delete<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bool, String>> {
            let existed = self.data.lock().unwrap().remove(data_key).is_some();
            if existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                counts.insert(count_key.to_string(), count.saturating_sub(1));
            }
            Box::pin(async move { Ok(existed) })
        }

        fn increment_with_quota<'a>(
            &'a self,
            data_key: &'a str,
            count_key: &'a str,
            delta: i64,
            _ttl_seconds: u32,
            max_keys: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bundle_host_kv::QuotaOutcome<i64>, String>>
        {
            let mut data = self.data.lock().unwrap();
            let existed = data.contains_key(data_key);
            if !existed {
                let mut counts = self.counts.lock().unwrap();
                let count = *counts.get(count_key).unwrap_or(&0);
                if count >= max_keys {
                    return Box::pin(
                        async move { Ok(bundle_host_kv::QuotaOutcome::QuotaExceeded) },
                    );
                }
                counts.insert(count_key.to_string(), count + 1);
            }
            let current = data
                .get(data_key)
                .and_then(|v| std::str::from_utf8(v).ok())
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(0);
            let new_value = current + delta;
            data.insert(data_key.to_string(), new_value.to_string().into_bytes());
            Box::pin(async move { Ok(bundle_host_kv::QuotaOutcome::Admitted(new_value)) })
        }

        fn increment_rate<'a>(
            &'a self,
            _rate_key: &'a str,
            _window_seconds: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<u64, String>> {
            // Unbounded in this fake -- `handle_kv`'s own rate limit is
            // `bundle_host_kv::KvHost`'s responsibility, already covered by
            // that crate's own tests; this module only needs to prove its
            // argument parsing/error mapping, not re-prove the limiter.
            Box::pin(async move { Ok(1) })
        }
    }

    fn test_egress() -> Arc<EgressGuard> {
        Arc::new(EgressGuard::new(
            Arc::new(crate::egress::ReqwestTransport),
            crate::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
            },
            Arc::new(BundleCatalog::new()),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_egress_denied_total", "test"),
                &["app_id", "reason"],
            )
            .unwrap(),
            crate::flags::boxed(crate::flags::StaticFlag(true)),
        ))
    }

    /// Grants every permission this file's existing (pre-gate) tests
    /// already exercised, for both app ids this suite uses
    /// (`kv_two_apps_in_the_same_tenant_are_isolated_through_the_handler`'s
    /// `waddles.bot.other` included) under `scope()`'s `(7, 3, 1)` -- so
    /// every happy-path test keeps proving its own capability logic, not
    /// this landing's gate wiring (which the dedicated `gate_*` tests below
    /// exercise directly, including the deny-without-grant cases).
    fn permissive_gate() -> Arc<CapabilityGate> {
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        for app_id in ["waddles.bot.commands.default", "waddles.bot.other"] {
            let mut grants = std::collections::HashMap::new();
            for id in [
                "platform.context",
                "platform.clock",
                "platform.log",
                "storage.kv",
                "chat.send:twitch",
                "chat.send:discord",
                "net.http.fqdn:example.com",
                "storage.tables",
                "flags.read",
            ] {
                grants.insert(
                    id.to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: id.to_string(),
                        params: serde_json::json!({}),
                    },
                );
            }
            snapshot.set(
                bundle_capability_gate::GrantScopeKey {
                    tenant_id: 7,
                    community_id: 3,
                    app_id: app_id.to_string(),
                    app_version: 1,
                },
                bundle_capability_gate::GrantSet {
                    permission_snapshot_hash: "test".to_string(),
                    grants,
                },
            );
        }
        Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ))
    }

    /// A gate with an empty snapshot -- every permission fails closed
    /// `not_granted`, for tests proving the gate is actually consulted.
    fn deny_all_gate() -> Arc<CapabilityGate> {
        Arc::new(CapabilityGate::new(
            Arc::new(bundle_capability_gate::InMemoryGrantSnapshot::new()),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ))
    }

    fn caps(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
        caps_with_usage(queue, Arc::new(Mutex::new(UsageBatcher::new())))
    }

    fn caps_with_usage(
        queue: FakeRelayQueue,
        usage: Arc<Mutex<UsageBatcher>>,
    ) -> StageCapabilities<FakeRelayQueue> {
        StageCapabilities::new(queue, test_egress(), usage, permissive_gate())
    }

    fn caps_with_kv(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue, FakeKvBackend> {
        StageCapabilities::new(
            queue,
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            permissive_gate(),
        )
        .with_kv(FakeKvBackend::default())
    }

    fn scope() -> InvokeScope {
        InvokeScope {
            tenant: "acme".to_string(),
            community: Some("main".to_string()),
            app_id: "waddles.bot.commands.default".to_string(),
            origin_channel_id: None,
            tenant_id: 7,
            community_id: 3,
            app_version: 1,
        }
    }

    /// Same as [`scope`] but carrying an origin channel id, as
    /// `crate::dispatch::invoke_dispatch` would populate it from a real
    /// inbound Discord event's `event.source.channel_id`.
    fn discord_scope() -> InvokeScope {
        InvokeScope {
            origin_channel_id: Some("123456789012345678".to_string()),
            ..scope()
        }
    }

    /// Mirrors `crate::egress`'s own `FakeTransport` test pattern (module
    /// doc's own `HttpTransport` split) so the Discord relay send is
    /// unit-testable with no live network access: records every request it
    /// is asked to send, then replies with a queued response (defaulting to
    /// a bare `200 {}` when none is queued).
    #[derive(Default)]
    struct FakeDiscordTransport {
        requests: Mutex<Vec<crate::egress::TransportRequest>>,
        responses: Mutex<Vec<Result<crate::egress::TransportResponse, HostResultError>>>,
    }

    impl FakeDiscordTransport {
        fn queue(&self, resp: Result<crate::egress::TransportResponse, HostResultError>) {
            self.responses.lock().unwrap().push(resp);
        }
    }

    impl crate::egress::HttpTransport for FakeDiscordTransport {
        fn send<'a>(
            &'a self,
            req: crate::egress::TransportRequest,
            _timeout: std::time::Duration,
            _max_response_bytes: usize,
        ) -> Pin<
            Box<
                dyn Future<Output = Result<crate::egress::TransportResponse, HostResultError>>
                    + Send
                    + 'a,
            >,
        > {
            self.requests.lock().unwrap().push(req);
            let next = self.responses.lock().unwrap().pop().unwrap_or_else(|| {
                Ok(crate::egress::TransportResponse {
                    status: 200,
                    headers: vec![],
                    body: b"{}".to_vec(),
                    truncated: false,
                })
            });
            Box::pin(async move { next })
        }
    }

    fn call(capability: CapabilityKind, op: &str, args: serde_json::Value) -> HostCallBody {
        HostCallBody {
            app_id: "waddles.bot.commands.default".to_string(),
            capability,
            op: op.to_string(),
            args,
            call_id: 1,
        }
    }

    #[tokio::test]
    async fn kv_is_not_implemented_when_no_backend_was_configured() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    #[tokio::test]
    async fn kv_set_then_get_round_trips_through_the_handler() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "counter", "value": [1, 2, 3], "ttl_seconds": 0}),
            ),
        )
        .await
        .expect("set succeeds");

        let result = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Kv,
                    "get",
                    serde_json::json!({"key": "counter"}),
                ),
            )
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::json!([1, 2, 3]));
    }

    #[tokio::test]
    async fn kv_get_of_an_absent_key_returns_a_null_value_not_an_error() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let result = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Kv,
                    "get",
                    serde_json::json!({"key": "absent"}),
                ),
            )
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn kv_delete_then_get_returns_null() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "k", "value": [9], "ttl_seconds": 0}),
            ),
        )
        .await
        .unwrap();
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Kv,
                "delete",
                serde_json::json!({"key": "k"}),
            ),
        )
        .await
        .expect("delete succeeds");
        let result = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn kv_increment_accumulates_across_calls() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let first = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Kv,
                    "increment",
                    serde_json::json!({"key": "hits", "delta": 5, "ttl_seconds": 0}),
                ),
            )
            .await
            .expect("increment succeeds");
        assert_eq!(first["value"], serde_json::json!(5));

        let second = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Kv,
                    "increment",
                    serde_json::json!({"key": "hits", "delta": 3, "ttl_seconds": 0}),
                ),
            )
            .await
            .expect("increment succeeds");
        assert_eq!(second["value"], serde_json::json!(8));
    }

    #[tokio::test]
    async fn kv_set_with_malformed_args_is_rejected_as_invalid_args() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "set", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn kv_unknown_op_is_rejected() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "bogus", serde_json::json!({})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn kv_rejects_a_guest_key_that_attempts_a_namespace_escape() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Kv,
                    "set",
                    serde_json::json!({"key": "other:app:data:secret", "value": [1], "ttl_seconds": 0}),
                ),
            )
            .await
            .unwrap_err();
        // `KvError::wire_code` collapses every non-`too_large` reason to
        // `"backend"` at the host-call boundary (module doc) -- the
        // finer-grained `invalid_key` reason is what `bundle_host_kv`'s own
        // tests assert against `KvError::code()` directly.
        assert_eq!(err.code, "backend");
    }

    #[tokio::test]
    async fn kv_two_apps_in_the_same_tenant_are_isolated_through_the_handler() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "secret", "value": [42], "ttl_seconds": 0}),
            ),
        )
        .await
        .unwrap();

        let other_app = InvokeScope {
            app_id: "waddles.bot.other".to_string(),
            ..scope()
        };
        let result = caps
            .handle(
                &other_app,
                call(
                    CapabilityKind::Kv,
                    "get",
                    serde_json::json!({"key": "secret"}),
                ),
            )
            .await
            .expect("get succeeds");
        assert_eq!(
            result["value"],
            serde_json::Value::Null,
            "a different app_id must never see this app's value"
        );
    }

    #[test]
    fn outbound_relay_queue_key_matches_python_irc_relay_format() {
        assert_eq!(
            outbound_relay_queue_key("twitch"),
            "waddles:transport:irc:twitch:outbound"
        );
    }

    #[test]
    fn sanitize_irc_component_strips_control_characters() {
        assert_eq!(sanitize_irc_component("hi\r\nthere"), "hithere");
        assert_eq!(sanitize_irc_component("clean"), "clean");
    }

    // Regression coverage for the MED finding: `handle_log` previously
    // logged a guest-supplied `message` verbatim, with neither
    // `penguin-logging` `SENSITIVE_KEYS` sanitization nor CRLF/control-char
    // stripping (unlike `handle_relay` in this same file).

    #[test]
    fn sanitize_bundle_log_message_strips_crlf_and_control_characters() {
        assert_eq!(
            sanitize_bundle_log_message("line1\r\nline2\tafter-tab"),
            "line1line2after-tab"
        );
    }

    #[test]
    fn sanitize_bundle_log_message_redacts_an_email_shaped_value() {
        let sanitized = sanitize_bundle_log_message("user@example.com logged in");
        assert!(!sanitized.contains("user@example.com"));
        assert!(sanitized.starts_with("[email]@example.com"));
    }

    #[test]
    fn sanitize_bundle_log_message_truncates_to_the_length_cap() {
        let huge = "a".repeat(MAX_BUNDLE_LOG_MESSAGE_LEN + 500);
        let sanitized = sanitize_bundle_log_message(&huge);
        assert_eq!(sanitized.chars().count(), MAX_BUNDLE_LOG_MESSAGE_LEN);
    }

    #[test]
    fn sanitize_bundle_log_message_passes_clean_short_messages_through() {
        assert_eq!(sanitize_bundle_log_message("all good"), "all good");
    }

    #[tokio::test]
    async fn relay_send_pushes_the_expected_key_and_payload() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#somechannel\",\"text\":\"hi\"}"}),
                ),
            )
            .await
            .expect("relay send succeeds");
        assert_eq!(result["queued"], serde_json::json!(true));
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        assert_eq!(pushed.len(), 1);
        assert_eq!(pushed[0].0, "waddles:transport:irc:twitch:outbound");
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["channel"], "#somechannel");
        assert_eq!(parsed["text"], "hi");
    }

    /// Regression coverage for the CRITICAL usage-metering finding:
    /// `UsageBatcher::record_relay_call` was never called anywhere in this
    /// crate. A successful relay send must record one host-call-by-kind
    /// delta (spec §5.12/D31).
    #[tokio::test]
    async fn relay_send_records_a_relay_call_against_the_usage_batcher() {
        let usage = Arc::new(Mutex::new(UsageBatcher::new()));
        let caps = caps_with_usage(FakeRelayQueue::default(), Arc::clone(&usage));
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#somechannel\",\"text\":\"hi\"}"}),
            ),
        )
        .await
        .expect("relay send succeeds");

        assert_eq!(usage.lock().unwrap().pending_len(), 1);
    }

    #[tokio::test]
    async fn relay_send_sanitizes_crlf_before_queuing() {
        let caps = caps(FakeRelayQueue::default());
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#c\",\"text\":\"line1\\r\\nline2\"}"}),
            ),
        )
        .await
        .expect("relay send succeeds");
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["text"], "line1line2");
    }

    #[tokio::test]
    async fn relay_send_rejects_unknown_provider() {
        // "discord" used to be this test's rejected example before it
        // joined `RELAY_PROVIDERS` in this landing -- "slack" is the next
        // platform `crate::senders::Platform` names but that has no relay
        // path wired (`crate::senders::sender_status` still reports it a
        // `PendingSeam`), so it stays a genuinely-unknown relay provider.
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "slack", "message_json": r#"{"channel":"c","text":"hi"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_provider");
        assert!(caps.relay_queue.pushed.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn relay_send_rejects_empty_channel() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": r#"{"channel":"","text":"hi"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_rejects_empty_text() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": r#"{"channel":"c","text":""}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_missing_provider_is_invalid_args() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"message_json": r#"{"channel":"c","text":"hi"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_missing_message_json_is_invalid_args() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_propagates_queue_failure() {
        let caps = caps(FakeRelayQueue {
            fail: true,
            ..Default::default()
        });
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": r#"{"channel":"c","text":"hi"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "relay_unavailable");
    }

    /// **Proves the design decision this landing makes**: a Discord relay
    /// send never touches `self.relay_queue` (no Valkey `LPUSH`, no
    /// dependency on svc-ingest's outbound drain) -- it hits Discord's REST
    /// API directly, through the same `crate::egress::HttpTransport`
    /// abstraction (`http.send`'s own guard) uses, mocked exactly the way
    /// `crate::egress`'s own tests mock it.
    #[tokio::test]
    async fn relay_send_discord_posts_to_the_discord_rest_api() {
        let transport = Arc::new(FakeDiscordTransport::default());
        let caps = caps(FakeRelayQueue::default()).with_discord(
            transport.clone(),
            crate::config::Secret::new("test-bot-token"),
        );

        let result = caps
            .handle(
                &discord_scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "discord", "message_json": r#"{"text":"pong"}"#}),
                ),
            )
            .await
            .expect("discord relay send succeeds");
        assert_eq!(result["sent"], serde_json::json!(true));
        assert_eq!(result["provider"], serde_json::json!("discord"));
        // Never LPUSHed -- the whole point of this design.
        assert!(caps.relay_queue.pushed.lock().unwrap().is_empty());

        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        let req = &requests[0];
        assert_eq!(req.method, "POST");
        assert_eq!(
            req.url,
            "https://discord.com/api/v10/channels/123456789012345678/messages"
        );
        assert!(req
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == "Bot test-bot-token"));
        assert!(req
            .headers
            .iter()
            .any(|(k, v)| k == "Content-Type" && v == "application/json"));
        let body = req.body.as_ref().expect("body present");
        let parsed: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(parsed["content"], "pong");
    }

    /// A bundle-supplied `channel` in `message_json` is silently ignored for
    /// Discord -- `handle_discord_relay`'s whole reason for existing is that
    /// the channel comes from the envelope, never bundle args.
    #[tokio::test]
    async fn relay_send_discord_ignores_a_bundle_supplied_channel() {
        let transport = Arc::new(FakeDiscordTransport::default());
        let caps = caps(FakeRelayQueue::default()).with_discord(
            transport.clone(),
            crate::config::Secret::new("test-bot-token"),
        );

        caps.handle(
            &discord_scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "discord", "message_json": r#"{"channel":"999999999999999999","text":"pong"}"#}),
            ),
        )
        .await
        .expect("discord relay send succeeds");

        let requests = transport.requests.lock().unwrap();
        // The scope's origin channel (123...678), never the bundle's
        // "999...999".
        assert!(requests[0].url.contains("123456789012345678"));
        assert!(!requests[0].url.contains("999999999999999999"));
    }

    /// Multi-line Discord text is sent verbatim -- unlike Twitch, no
    /// IRC-line CRLF sanitization applies (Discord is a JSON-bodied REST
    /// call, not a line-oriented wire protocol; stripping newlines would
    /// mangle a legitimate multi-line message).
    #[tokio::test]
    async fn relay_send_discord_does_not_strip_newlines_from_text() {
        let transport = Arc::new(FakeDiscordTransport::default());
        let caps = caps(FakeRelayQueue::default()).with_discord(
            transport.clone(),
            crate::config::Secret::new("test-bot-token"),
        );

        caps.handle(
            &discord_scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "discord", "message_json": r#"{"text":"line1\nline2"}"#}),
            ),
        )
        .await
        .expect("discord relay send succeeds");

        let requests = transport.requests.lock().unwrap();
        let body = requests[0].body.as_ref().unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(parsed["content"], "line1\nline2");
    }

    #[tokio::test]
    async fn relay_send_discord_records_a_relay_call_against_the_usage_batcher() {
        let usage = Arc::new(Mutex::new(UsageBatcher::new()));
        let transport = Arc::new(FakeDiscordTransport::default());
        let caps = caps_with_usage(FakeRelayQueue::default(), Arc::clone(&usage))
            .with_discord(transport, crate::config::Secret::new("test-bot-token"));

        caps.handle(
            &discord_scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "discord", "message_json": r#"{"text":"pong"}"#}),
            ),
        )
        .await
        .expect("discord relay send succeeds");

        assert_eq!(usage.lock().unwrap().pending_len(), 1);
    }

    #[tokio::test]
    async fn relay_send_discord_is_unavailable_when_not_configured() {
        // No `.with_discord(...)` -- the graceful-degradation default.
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &discord_scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "discord", "message_json": r#"{"text":"pong"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "relay_unavailable");
    }

    #[tokio::test]
    async fn relay_send_discord_rejects_a_missing_origin_channel() {
        let transport = Arc::new(FakeDiscordTransport::default());
        let caps = caps(FakeRelayQueue::default())
            .with_discord(transport, crate::config::Secret::new("test-bot-token"));
        // `scope()` (not `discord_scope()`) carries `origin_channel_id: None`.
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "discord", "message_json": r#"{"text":"pong"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_discord_rejects_a_malformed_origin_channel() {
        let transport = Arc::new(FakeDiscordTransport::default());
        let caps = caps(FakeRelayQueue::default())
            .with_discord(transport, crate::config::Secret::new("test-bot-token"));
        let bad_scope = InvokeScope {
            origin_channel_id: Some("not-a-snowflake".to_string()),
            ..scope()
        };
        let err = caps
            .handle(
                &bad_scope,
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "discord", "message_json": r#"{"text":"pong"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_discord_propagates_a_non_2xx_discord_response() {
        let transport = Arc::new(FakeDiscordTransport::default());
        transport.queue(Ok(crate::egress::TransportResponse {
            status: 401,
            headers: vec![],
            body: b"{\"message\":\"401: Unauthorized\"}".to_vec(),
            truncated: false,
        }));
        let caps = caps(FakeRelayQueue::default())
            .with_discord(transport, crate::config::Secret::new("bad-token"));
        let err = caps
            .handle(
                &discord_scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "discord", "message_json": r#"{"text":"pong"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "relay_unavailable");
    }

    #[tokio::test]
    async fn clock_now_millis_returns_a_positive_integer() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(
                &scope(),
                call(CapabilityKind::Clock, "now-millis", serde_json::json!({})),
            )
            .await
            .expect("clock succeeds");
        assert!(result.as_u64().unwrap() > 0);
    }

    #[tokio::test]
    async fn clock_unknown_op_is_denied() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Clock, "bogus", serde_json::json!({})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn context_reports_scope_without_secrets() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(
                &scope(),
                call(CapabilityKind::Context, "get", serde_json::json!({})),
            )
            .await
            .expect("context succeeds");
        assert_eq!(result["tenant"], "acme");
        assert_eq!(result["community"], "main");
        assert_eq!(result["app_id"], "waddles.bot.commands.default");
    }

    #[tokio::test]
    async fn log_write_at_every_level_succeeds() {
        let caps = caps(FakeRelayQueue::default());
        for level in ["error", "warn", "debug", "info", "unrecognized"] {
            let result = caps
                .handle(
                    &scope(),
                    call(
                        CapabilityKind::Log,
                        "write",
                        serde_json::json!({"level": level, "message": "hello"}),
                    ),
                )
                .await
                .expect("log write succeeds");
            assert_eq!(result, serde_json::json!({}));
        }
    }

    /// `http` now delegates to `crate::egress::EgressGuard`; with an empty
    /// `BundleCatalog` (no manifest registered for this `app_id`), every
    /// call is denied `host_not_declared` -- proving the wiring reaches the
    /// guard rather than a stub, without needing a live manifest here (see
    /// `crate::egress`'s own tests for the guard's full behavior).
    #[tokio::test]
    async fn http_capability_is_wired_to_the_egress_guard() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Http,
                    "send",
                    serde_json::json!({"method": "GET", "url": "https://example.com/"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    #[tokio::test]
    async fn db_and_flags_capabilities_are_documented_seams() {
        // `kv` is no longer an unconditional seam -- see
        // `kv_is_not_implemented_when_no_backend_was_configured` for its
        // own (backend-unconfigured) not_implemented case, and the
        // `kv_*` tests above for the fully-wired behavior.
        let caps = caps(FakeRelayQueue::default());
        for capability in [CapabilityKind::Db, CapabilityKind::Flags] {
            let err = caps
                .handle(
                    &scope(),
                    call(capability, "anything", serde_json::json!({})),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_implemented");
        }
    }

    /// A [`StageCapabilities`] wired to `deny_all_gate()` -- every arm below
    /// exercises `gate.authorize()` actually being consulted first, not
    /// bypassed.
    fn caps_denied(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue, FakeKvBackend> {
        StageCapabilities::new(
            queue,
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            deny_all_gate(),
        )
        .with_kv(FakeKvBackend::default())
    }

    /// Regression (gh-433): the dispatch-time `InvokeScope.app_version` this
    /// module's own `gate_scope()` builds must be the REAL resolved
    /// `app_versions.id` (never the `0` interim placeholder
    /// `crate::dispatch::invoke_dispatch` used to hardcode) for a seeded
    /// grant to ever match -- `permissive_gate()`'s grants are seeded under
    /// `app_version: 1` (`scope()`'s own value, the same value a real
    /// dispatch loop now threads through via `crate::dispatch::
    /// resolve_action_app_version`), so an invoke carrying that resolved
    /// version is ALLOWED.
    #[tokio::test]
    async fn dispatch_invocation_with_the_resolved_app_version_is_allowed_by_a_seeded_grant() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let result = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .expect("a grant seeded under the resolved app_version allows the call");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    /// Regression (gh-433): an invoke whose `app_version` cannot be
    /// resolved to the version a grant was actually issued for -- modeled
    /// here as a scope carrying a version no grant was ever seeded under --
    /// is DENIED, never silently authorized under the wrong version's
    /// permissions (which is exactly what the old `app_version: 0`
    /// placeholder would have risked had any grant ever been seeded under
    /// `0`).
    #[tokio::test]
    async fn dispatch_invocation_with_an_unresolved_app_version_is_denied() {
        let caps = caps_with_kv(FakeRelayQueue::default());
        let unresolved_scope = InvokeScope {
            app_version: 999,
            ..scope()
        };
        let err = caps
            .handle(
                &unresolved_scope,
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_kv_without_a_grant() {
        let caps = caps_denied(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_relay_without_a_grant() {
        let caps = caps_denied(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": r#"{"channel":"c","text":"hi"}"#}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_http_without_a_grant() {
        let caps = caps_denied(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Http,
                    "send",
                    serde_json::json!({"method": "GET", "url": "https://example.com/"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_db_without_a_grant() {
        let caps = caps_denied(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Db, "execute", serde_json::json!({})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn gate_denies_flags_without_a_grant() {
        let caps = caps_denied(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Flags, "get", serde_json::json!({})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    /// The "Always granted, zero-config" platform capabilities (spec SS1)
    /// still route through `gate.authorize()` (module doc: "so every call is
    /// still audited uniformly") -- with no grant data at all, they deny
    /// exactly like any other permission; `crate::grant_gate::
    /// AlwaysGrantedLoader` (not exercised by this gate double) is what
    /// makes them unconditionally present in production.
    #[tokio::test]
    async fn gate_denies_context_clock_log_without_a_grant() {
        let caps = caps_denied(FakeRelayQueue::default());
        for capability in [
            CapabilityKind::Context,
            CapabilityKind::Clock,
            CapabilityKind::Log,
        ] {
            let err = caps
                .handle(
                    &scope(),
                    call(capability, "now-millis", serde_json::json!({})),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_granted");
        }
    }

    /// An undeclared permission (granted set has entries, but not the one
    /// this call needs) denies exactly like an empty grant set -- proving
    /// `authorize()` checks the specific permission id, not just "some
    /// grant exists for this app."
    #[tokio::test]
    async fn gate_denies_a_permission_the_app_was_never_granted() {
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        snapshot.set(
            bundle_capability_gate::GrantScopeKey {
                tenant_id: 7,
                community_id: 3,
                app_id: "waddles.bot.commands.default".to_string(),
                app_version: 1,
            },
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants: std::collections::HashMap::from([(
                    "flags.read".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "flags.read".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let gate = Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ));
        let caps = StageCapabilities::new(
            FakeRelayQueue::default(),
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            gate,
        )
        .with_kv(FakeKvBackend::default());

        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    /// Revocation mid-stream (spec SS4/SS5.3, task instruction): a grant
    /// present at the start of the connection, then invalidated (simulating
    /// a `bundle:grants:invalidate` push notification landing between two
    /// calls), denies the very next call -- proving `StageCapabilities`
    /// reads through a live [`bundle_capability_gate::GrantCache`], not a
    /// one-shot snapshot copied at construction time.
    #[tokio::test]
    async fn gate_revocation_mid_stream_denies_the_next_call() {
        let key = bundle_capability_gate::GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: "waddles.bot.commands.default".to_string(),
            app_version: 1,
        };
        let loader = Arc::new(bundle_capability_gate::InMemoryGrantLoader::new());
        loader.set(
            key.clone(),
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "v1".to_string(),
                grants: std::collections::HashMap::from([(
                    "storage.kv".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "storage.kv".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let cache = Arc::new(bundle_capability_gate::GrantCache::new(loader));
        cache.refresh(&key).await.unwrap();
        let gate = Arc::new(CapabilityGate::new(
            cache.clone(),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ));
        let caps = StageCapabilities::new(
            FakeRelayQueue::default(),
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            gate,
        )
        .with_kv(FakeKvBackend::default());

        caps.handle(
            &scope(),
            call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
        )
        .await
        .expect("granted before revocation");

        cache.invalidate(&key);

        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
    }

    #[tokio::test]
    async fn deny_all_denies_every_capability() {
        let handler = DenyAllCapabilities;
        let result = handler
            .handle(
                &scope(),
                call(CapabilityKind::Clock, "now-millis", serde_json::json!({})),
            )
            .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn boxed_wraps_a_handler_as_an_arc_dyn() {
        let handler: Arc<dyn CapabilityHandler> = boxed(DenyAllCapabilities);
        let result = handler
            .handle(
                &scope(),
                call(CapabilityKind::Log, "write", serde_json::json!({})),
            )
            .await;
        assert!(result.is_err());
    }
}

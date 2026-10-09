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
//! connection is reused rather than a second one opened). `db` is wired to
//! `bundle_host_db::DbHost` (mirrors `core/svc_process::capabilities::
//! StageCapabilities::handle_db` byte-for-byte, rescoped per-call to this
//! connection's [`InvokeScope`] instead of svc_process's fixed
//! per-invocation `StageCapabilities`). `flags` remains a documented
//! `TODO(M3+)` seam -- see [`StageCapabilities::handle`]'s match arm.
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
use bundle_host_db::{
    CapabilitySnapshot as DbCapabilitySnapshot, DbError, DbHost, DbScope, DbValue, PostgresBackend,
    SchemaCache,
};
use bundle_host_kv::{KvBackend, KvError, KvHost, KvScope};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};

use crate::egress::EgressGuard;
use crate::flags::FeatureFlag;
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

/// Version stamped on every outbound action envelope this host queues
/// (`"v"`). The consumer (`svc_ingest::outbound_ops`) treats a missing `v`
/// as the legacy text-only shape and rejects any version above the one it
/// knows -- bump only with a consumer that understands it.
const OUTBOUND_SCHEMA_VERSION: u32 = 1;

/// The verb of an outbound platform action (provider-framework Step 0,
/// issue #719). Mirrors `svc_ingest::outbound_ops::OutboundAction`; the
/// wire string is the same `op` field the queue envelope carries.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RelayOp {
    ChatSend,
    ChatDelete,
    DmSend,
}

impl RelayOp {
    /// Parses the wire `op` string; `None` for an unknown verb (the caller
    /// fails loud rather than defaulting to `chat.send`).
    fn parse(raw: &str) -> Option<Self> {
        match raw {
            "chat.send" => Some(Self::ChatSend),
            "chat.delete" => Some(Self::ChatDelete),
            "dm.send" => Some(Self::DmSend),
            _ => None,
        }
    }

    /// The wire/permission-family spelling of this op.
    fn as_str(self) -> &'static str {
        match self {
            Self::ChatSend => "chat.send",
            Self::ChatDelete => "chat.delete",
            Self::DmSend => "dm.send",
        }
    }

    /// The catalog permission id this op requires for `provider`.
    fn permission(self, provider: &str) -> PermissionId {
        match self {
            Self::ChatSend => PermissionId::ChatSend(provider.to_string()),
            Self::ChatDelete => PermissionId::ChatDelete(provider.to_string()),
            Self::DmSend => PermissionId::DmSend(provider.to_string()),
        }
    }
}

/// Whether `(provider, op)` has a live sender behind it today. Only
/// `chat.send` does; `chat.delete`/`dm.send` are authorized by the gate but
/// refused loudly (`unsupported_op`) until the per-platform follow-up PRs
/// (Discord REST sender, Twitch Helix client) implement them and flip the
/// matching row here -- never queued into a consumer that cannot act on
/// them, so the bundle sees the failure instead of a silent black hole.
fn relay_op_supported(_provider: &str, op: RelayOp) -> bool {
    matches!(op, RelayOp::ChatSend)
}

/// Peeks the `op` a bundle's `message_json` asks for. Absent, unparsable or
/// non-string `op` is the legacy `chat.send` shape -- the later field-level
/// validation in `handle_relay` still produces the same `invalid_args`
/// errors it always did for malformed input. `Err` carries an unknown op
/// string.
fn peek_relay_op(args: &serde_json::Value) -> Result<RelayOp, String> {
    let op = args
        .get("message_json")
        .and_then(|v| v.as_str())
        .and_then(|raw| serde_json::from_str::<serde_json::Value>(raw).ok())
        .and_then(|m| m.get("op").and_then(|o| o.as_str()).map(str::to_string));
    match op {
        None => Ok(RelayOp::ChatSend),
        Some(raw) => RelayOp::parse(&raw).ok_or(raw),
    }
}

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
///
/// Everything `handle_db` needs, shared across every invocation this
/// process serves -- constructed once at startup and cloned cheaply into
/// each connection's [`StageCapabilities`] via `Arc`. Byte-for-byte the
/// same shape as `core/svc_process::capabilities::DbWiring`, just built
/// over this crate's own [`FeatureFlag`] trait instead of svc_process's
/// `FeatureGate` -- the two stages' license-gating traits differ (module
/// doc's "kept as a separate trait per crate" rationale extends to this
/// struct too). `schemas`/`capabilities` are refreshed by this service's
/// own `bundle_loader` poll (not wired in this landing -- see
/// `bundle_host_db`'s crate doc "remaining work"); an `app_id` this
/// process has never heard of fails closed by construction ([`DbHost`]'s
/// own resolve-then-authorize order).
#[derive(Clone)]
pub struct DbWiring {
    pub host: Arc<DbHost<PostgresBackend>>,
    pub schemas: Arc<SchemaCache>,
    pub capabilities: Arc<DbCapabilitySnapshot>,
    /// `crate::flags::BUNDLE_DB_CAPABILITY_FLAG` gate -- OFF denies every
    /// `db` call `feature_disabled` before any schema/authorize lookup.
    pub flag: Arc<dyn FeatureFlag>,
}

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
    /// `None` until `crate::lib`'s startup wiring provisions a live
    /// Postgres pool + flag client -- every `db` call denies
    /// `feature_disabled` in that state, never panics (mirrors every
    /// other unimplemented-seam capability's fail-closed default in
    /// [`Self::handle`]).
    db: Option<DbWiring>,
    /// `None` until [`Self::with_detokenize`] is called -- every relay send
    /// then falls back to showing the raw `{user:<token>}` placeholder it
    /// received from the bundle (the degraded, but never-PII-leaking,
    /// fallback -- see that method's doc).
    detokenize: Option<DetokenizeWiring>,
}

/// The outbound PII-detokenization dependency: a live resolver over
/// `waddles.hub.internal.v1.IdentityService.ResolveDisplayNames`
/// (`core/egress_detokenizer::HubClientResolver`), plus the opt-out
/// kill-switch gate (`crate::flags::DISABLE_PII_DETOKENIZATION_FLAG`).
#[derive(Clone)]
struct DetokenizeWiring {
    resolver: Arc<dyn egress_detokenizer::DisplayNameResolver>,
    gate: Arc<dyn crate::flags::FeatureFlag>,
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
            db: None,
            detokenize: None,
        }
    }

    /// Enables the outbound PII-detokenization pass for every `relay` host
    /// call this connection answers -- builder-style so existing
    /// `StageCapabilities::new` call sites (including every current test)
    /// are unaffected and so a deployment whose `hub_client::HubClient`
    /// connection failed to open at startup can still construct every
    /// other capability (same graceful-degradation pattern as
    /// [`Self::with_discord`]/[`Self::with_kv`]).
    pub fn with_detokenize(
        mut self,
        resolver: Arc<dyn egress_detokenizer::DisplayNameResolver>,
        gate: Arc<dyn crate::flags::FeatureFlag>,
    ) -> Self {
        self.detokenize = Some(DetokenizeWiring { resolver, gate });
        self
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
    ///
    /// `capabilities` is the manifest-declared-capability snapshot
    /// `bundle_host_kv::authorize::authorize_kv` checks -- the same
    /// `Arc<CapabilitySnapshot>` `crate::bundle_loader`'s DB-driven poll
    /// loop updates every tick, shared (not copied) so a capability
    /// dropped from a new manifest version is visible to the very next
    /// `kv` host-call, not just the next connection.
    pub fn with_kv(
        mut self,
        backend: K,
        capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    ) -> Self {
        self.kv = Some(KvHost::new(backend, capabilities));
        self
    }

    /// Attaches the `db` capability's live wiring -- called once at
    /// startup (`crate::lib`) with the shared, process-wide [`DbWiring`],
    /// never per-connection. Builder-style so existing `StageCapabilities::new`
    /// call sites (including every current test) are unaffected. Mirrors
    /// `core/svc_process::capabilities::StageCapabilities::with_db`.
    pub fn with_db(mut self, db: DbWiring) -> Self {
        self.db = Some(db);
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

    /// Substitutes every `{user:<token>}` placeholder in `text` with its
    /// resolved, `sink`-escaped display name (`egress_detokenizer::
    /// detokenize_resolving`) -- the PII boundary's outbound half. Returns
    /// `text` unchanged (tokens still visible, never raw PII -- see
    /// [`Self::with_detokenize`]'s doc) when no resolver is configured yet,
    /// or when the opt-out kill-switch is ON.
    async fn detokenize_text(
        &self,
        tenant: &str,
        text: &str,
        sink: egress_detokenizer::Sink,
    ) -> String {
        let Some(wiring) = &self.detokenize else {
            return text.to_string();
        };
        if !wiring.gate.enabled().await {
            return text.to_string();
        }
        egress_detokenizer::detokenize_resolving(text, tenant, wiring.resolver.as_ref(), sink)
            .await
            .text
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
        // The op defaults to `chat.send` (legacy shape); an unknown op is
        // refused loudly, never defaulted.
        let op = peek_relay_op(args).map_err(|raw| {
            denied(
                "unknown_op",
                format!("relay op {raw:?} is not one of chat.send/chat.delete/dm.send"),
            )
        })?;
        // Gate call FIRST (spec SS5), now that `provider` and `op` are known
        // well enough to name the specific `<op>:<platform>` permission id
        // this call maps to (`chat.send:`/`chat.delete:`/`dm.send:`).
        self.gate
            .authorize(
                &scope.gate_scope(),
                op.permission(provider),
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        if !relay_op_supported(provider, op) {
            tracing::error!(
                provider,
                op = op.as_str(),
                app_id = %scope.app_id,
                "relay op authorized but no sender implements it yet"
            );
            return Err(denied(
                "unsupported_op",
                format!(
                    "relay op {:?} is not implemented for provider {provider:?}",
                    op.as_str()
                ),
            ));
        }
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

        let text = self
            .detokenize_text(&scope.tenant, text, egress_detokenizer::Sink::Twitch)
            .await;
        let channel = sanitize_irc_component(channel);
        let text = sanitize_irc_component(&text);
        if channel.is_empty() || text.is_empty() {
            return Err(denied(
                "invalid_args",
                "relay.send requires a non-empty 'channel' and 'text' after sanitization",
            ));
        }

        let key = outbound_relay_queue_key(provider);
        // Versioned envelope. `channel`/`text` stay top-level so a not-yet-
        // upgraded consumer (which ignores unknown fields) still delivers it
        // during a rolling upgrade; `v`/`op`/`platform` are what the new
        // consumer dispatches on.
        let payload = serde_json::json!({
            "v": OUTBOUND_SCHEMA_VERSION,
            "op": op.as_str(),
            "platform": provider,
            "channel": channel,
            "text": text,
        })
        .to_string();
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

        let text = self
            .detokenize_text(&scope.tenant, text, egress_detokenizer::Sink::Discord)
            .await;
        let text = text.as_str();

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

    /// Structured insert/get/update/delete/query against this app's own
    /// `app_core`/`app_community` table -- byte-for-byte mirror of
    /// `core/svc_process::capabilities::StageCapabilities::handle_db`
    /// (same design doc, same `bundle_host_db::DbHost`), rescoped per-call
    /// to this connection's [`InvokeScope`] instead of a per-connection
    /// fixed tenant (module doc's "capability scope is resolved per
    /// invoke, never per connection").
    ///
    /// Op/args shape identical to svc_process's `handle_db` doc:
    /// - `insert`: `args.column_values` = `{col: value, ...}` -> `{row_id, version, columns}`
    /// - `get`: `args.row_id` (string) -> `{row_id, version, columns}`
    /// - `update`: `args.row_id`, `args.expected_version` (u64), `args.column_values` -> `{row_id, version, columns}`
    /// - `delete`: `args.row_id`, `args.expected_version` (u64) -> `{}`
    /// - `query`: `args.limit`/`args.offset` (both optional u32, clamped to
    ///   `MAX_QUERY_LIMIT`) -> `{rows: [{row_id, version, columns}, ...]}`
    ///   -- host-side op ready for the proposed WIT shape, not yet
    ///   guest-reachable in this landing (see `bundle_host_db`'s PR description)
    async fn handle_db(
        &self,
        scope: &InvokeScope,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        // Gate call FIRST (spec SS5), same pattern as `handle_kv` above --
        // checked before the feature-flag/wiring state below so an
        // ungranted call always reports `not_granted`, never
        // `not_implemented`/`feature_disabled` (which would leak whether
        // the capability is wired at all to a caller that was never
        // entitled to ask).
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::StorageTables,
                ResourceRef::AppScoped(AppScopedResource::Table),
            )
            .map_err(denied_from_gate)?;
        let Some(db) = &self.db else {
            return Err(denied(
                "not_implemented",
                "db capability is not wired in this build -- TODO(M4+)",
            ));
        };

        if !db.flag.enabled().await {
            return Err(denied(
                "feature_disabled",
                "db capability is disabled (waddles.bundle-db-capability is OFF)",
            ));
        }

        let db_scope = DbScope::new(
            scope.tenant.clone(),
            scope.community.clone(),
            scope.app_id.clone(),
        );

        let column_values =
            |args: &serde_json::Value| -> Result<Vec<(String, DbValue)>, HostResultError> {
                let obj = args
                    .get("column_values")
                    .and_then(|v| v.as_object())
                    .ok_or_else(|| denied("invalid_args", "column_values must be a JSON object"))?;
                obj.iter()
                    .map(|(k, v)| Ok((k.clone(), json_to_db_value(v)?)))
                    .collect()
            };

        let row_id = |args: &serde_json::Value| -> Result<String, HostResultError> {
            args.get("row_id")
                .and_then(|v| v.as_str())
                .map(str::to_string)
                .ok_or_else(|| denied("invalid_args", "row_id must be a string"))
        };

        let expected_version = |args: &serde_json::Value| -> Result<u64, HostResultError> {
            args.get("expected_version")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| denied("invalid_args", "expected_version must be a u64"))
        };

        let result = match call.op.as_str() {
            "insert" => {
                let values = column_values(&call.args)?;
                db.host
                    .insert(&db_scope, &db.schemas, &db.capabilities, values)
                    .await
            }
            "get" => {
                let id = row_id(&call.args)?;
                db.host
                    .get(&db_scope, &db.schemas, &db.capabilities, &id)
                    .await
            }
            "update" => {
                let id = row_id(&call.args)?;
                let version = expected_version(&call.args)?;
                let values = column_values(&call.args)?;
                db.host
                    .update(
                        &db_scope,
                        &db.schemas,
                        &db.capabilities,
                        &id,
                        version,
                        values,
                    )
                    .await
            }
            "delete" => {
                let id = row_id(&call.args)?;
                let version = expected_version(&call.args)?;
                return db
                    .host
                    .delete(&db_scope, &db.schemas, &db.capabilities, &id, version)
                    .await
                    .map(|()| serde_json::json!({}))
                    .map_err(db_error_to_host_error);
            }
            "query" => {
                let limit = call
                    .args
                    .get("limit")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .unwrap_or(bundle_host_db::MAX_QUERY_LIMIT);
                let offset = call
                    .args
                    .get("offset")
                    .and_then(|v| v.as_u64())
                    .and_then(|v| u32::try_from(v).ok())
                    .unwrap_or(0);
                let order_by = match parse_order_by(&call.args) {
                    Ok(o) => o,
                    Err(e) => return Err(e),
                };
                return db
                    .host
                    .query(
                        &db_scope,
                        &db.schemas,
                        &db.capabilities,
                        limit,
                        offset,
                        order_by,
                    )
                    .await
                    .map(|rows| {
                        serde_json::json!({
                            "rows": rows.into_iter().map(row_to_json).collect::<Vec<_>>(),
                        })
                    })
                    .map_err(db_error_to_host_error);
            }
            other => {
                return Err(denied(
                    "unknown_op",
                    format!("db op {other:?} not supported"),
                ))
            }
        };

        result.map(row_to_json).map_err(db_error_to_host_error)
    }

    /// `enabled` op: `{"key": String, "default_value": bool}` ->
    /// `{"enabled": bool}` -- un-stubs the `flags` WIT capability via
    /// `crate::flags::resolve_flag`'s real `penguin_licensing::
    /// LicenseClient`-backed fallback chain (live -> cached -> this
    /// call's own `default_value`; see that function's doc). Never
    /// returns an error for a well-formed call -- see `core/svc_process::
    /// capabilities::StageCapabilities::handle_flags`'s identical doc for
    /// why. `tier` is not wired in this landing.
    async fn handle_flags(
        &self,
        scope: &InvokeScope,
        call: &HostCallBody,
    ) -> Result<serde_json::Value, HostResultError> {
        // Gate call FIRST (spec SS5), same pattern as `handle_kv`/`handle_db`.
        self.gate
            .authorize(
                &scope.gate_scope(),
                PermissionId::FlagsRead,
                ResourceRef::AppScoped(AppScopedResource::None),
            )
            .map_err(denied_from_gate)?;
        match call.op.as_str() {
            "enabled" => {
                let args: FlagsEnabledArgs =
                    serde_json::from_value(call.args.clone()).map_err(|e| {
                        denied(
                            "invalid_args",
                            format!("malformed flags.enabled host-call args: {e}"),
                        )
                    })?;
                let value = crate::flags::resolve_flag(&args.key, args.default_value).await;
                Ok(serde_json::json!({ "enabled": value }))
            }
            other => Err(denied(
                "not_implemented",
                format!("flags op {other:?} is not wired in this build -- TODO(M3+)"),
            )),
        }
    }
}

/// `{"key": String, "default_value": bool}` -- `flags.enabled`'s args.
#[derive(serde::Deserialize)]
struct FlagsEnabledArgs {
    key: String,
    default_value: bool,
}

fn json_to_db_value(v: &serde_json::Value) -> Result<DbValue, HostResultError> {
    Ok(match v {
        serde_json::Value::Null => DbValue::Null,
        serde_json::Value::Bool(b) => DbValue::Bool(*b),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                DbValue::Int(i)
            } else if let Some(f) = n.as_f64() {
                DbValue::Float(f)
            } else {
                return Err(denied("invalid_args", "unsupported numeric value"));
            }
        }
        serde_json::Value::String(s) => DbValue::Text(s.clone()),
        _ => {
            return Err(denied(
                "invalid_args",
                "unsupported value shape (array/object)",
            ))
        }
    })
}

/// Parses `query`'s optional `order_by` arg: `{"random": true}` or
/// `{"column": "<name>", "descending": <bool>}` -- `None`/absent means the
/// backend's own default (`row_id ASC`). Column-name validation itself
/// happens host-side in `bundle_host_db::backend::order_by_sql` (never
/// trusted from this JSON alone) -- this function only shapes the
/// wire-level args into [`bundle_host_db::OrderBy`]. Mirrors
/// `svc_process::capabilities::parse_order_by` (not shared/exported from
/// `bundle_host_db`, so duplicated here).
fn parse_order_by(
    args: &serde_json::Value,
) -> Result<Option<bundle_host_db::OrderBy>, HostResultError> {
    let Some(order_by) = args.get("order_by") else {
        return Ok(None);
    };
    if order_by.is_null() {
        return Ok(None);
    }
    if order_by.get("random").and_then(|v| v.as_bool()) == Some(true) {
        return Ok(Some(bundle_host_db::OrderBy::Random));
    }
    let name = order_by
        .get("column")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            denied(
                "invalid_args",
                "order_by must be {\"random\": true} or {\"column\": string, \"descending\": bool}",
            )
        })?;
    let descending = order_by
        .get("descending")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    Ok(Some(bundle_host_db::OrderBy::Column {
        name: name.to_string(),
        descending,
    }))
}

fn db_value_to_json(v: &DbValue) -> serde_json::Value {
    match v {
        DbValue::Null => serde_json::Value::Null,
        DbValue::Bool(b) => serde_json::json!(b),
        DbValue::Int(i) => serde_json::json!(i),
        DbValue::Float(f) => serde_json::json!(f),
        DbValue::Text(s) => serde_json::json!(s),
        DbValue::Bytes(b) => serde_json::json!(b),
    }
}

fn row_to_json(row: bundle_host_db::Row) -> serde_json::Value {
    let columns: serde_json::Map<String, serde_json::Value> = row
        .columns
        .into_iter()
        .map(|(k, v)| (k, db_value_to_json(&v)))
        .collect();
    serde_json::json!({
        "row_id": row.row_id,
        "version": row.version,
        "columns": columns,
    })
}

/// Maps [`DbError`] to the `{code, message}` shape the executor forwards to
/// the guest -- byte-for-byte the same as
/// `core/svc_process::capabilities::db_error_to_host_error`.
fn db_error_to_host_error(err: DbError) -> HostResultError {
    denied(err.code(), err.to_string())
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
                // `db` is wired below (mirrors svc_process); `handle_db`
                // itself gate-checks FIRST (spec SS5) before consulting its
                // own feature-flag/wiring state.
                CapabilityKind::Db => self.handle_db(scope, &call).await,
                // `enabled` is wired to a real `penguin_licensing::
                // LicenseClient` (`crate::flags::resolve_flag`) -- see
                // `handle_flags`'s doc for the fallback semantics.
                // `handle_flags` itself gate-checks FIRST (spec SS5).
                CapabilityKind::Flags => self.handle_flags(scope, &call).await,
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

        fn reconcile_count_if_missing<'a>(
            &'a self,
            _count_key: &'a str,
            _data_scan_pattern: &'a str,
            _lock_key: &'a str,
            _lock_ttl_ms: u64,
            _scan_limit: u64,
        ) -> bundle_host_kv::BoxFuture<'a, Result<bundle_host_kv::ReconcileOutcome, String>>
        {
            // Always "already present" -- the eviction self-heal path is
            // `bundle_host_kv`'s own responsibility, covered by that
            // crate's tests; this module only needs argument parsing/error
            // mapping.
            Box::pin(async move { Ok(bundle_host_kv::ReconcileOutcome::AlreadyPresent) })
        }
    }

    fn test_egress() -> Arc<EgressGuard> {
        Arc::new(EgressGuard::new(
            Arc::new(crate::egress::ReqwestTransport::new()),
            crate::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
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
                "chat.delete:twitch",
                "dm.send:twitch",
                "dm.send:discord",
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

    /// Grants `storage.kv` to `"waddles.bot.commands.default"` -- the
    /// `app_id` every `scope()`/`call()` helper in this module uses -- so
    /// every existing `kv_*` test below (which is testing `handle_kv`'s
    /// argument parsing/error mapping, not the gate itself) is unaffected
    /// by the "undeclared means denied" default.
    /// `kv_call_is_denied_when_storage_kv_is_undeclared` below is the one
    /// test exercising an ungranted app.
    fn caps_with_kv(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue, FakeKvBackend> {
        caps_with_kv_and_capabilities(queue, &["waddles.bot.commands.default"])
    }

    fn caps_with_kv_and_capabilities(
        queue: FakeRelayQueue,
        granted_app_ids: &[&str],
    ) -> StageCapabilities<FakeRelayQueue, FakeKvBackend> {
        let snapshot = bundle_host_kv::CapabilitySnapshot::new();
        for app_id in granted_app_ids {
            snapshot.update(
                *app_id,
                [bundle_host_kv::authorize::KV_PERMISSION_ID.to_string()],
            );
        }
        StageCapabilities::new(
            queue,
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            permissive_gate(),
        )
        .with_kv(FakeKvBackend::default(), Arc::new(snapshot))
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
    async fn kv_call_is_denied_when_storage_kv_is_undeclared() {
        // A configured `kv` backend, but the app's `CapabilitySnapshot`
        // grants nothing -- "undeclared means denied", distinct from
        // `kv_is_not_implemented_when_no_backend_was_configured`'s
        // "backend never configured at all" case.
        let caps = caps_with_kv_and_capabilities(FakeRelayQueue::default(), &[]);
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Kv, "get", serde_json::json!({"key": "k"})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "backend"); // KvError::wire_code() collapse
    }

    #[tokio::test]
    async fn kv_call_succeeds_when_storage_kv_is_declared() {
        let caps = caps_with_kv_and_capabilities(
            FakeRelayQueue::default(),
            &["waddles.bot.commands.default"],
        );
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "k", "value": [1], "ttl_seconds": 0}),
            ),
        )
        .await
        .expect("set succeeds when storage.kv is declared");
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

    /// Mirrors `core/svc_process::capabilities::tests::mock_db_wiring` --
    /// same `MockDatabase`-backed `PostgresBackend`, over this crate's own
    /// `FeatureFlag`/`StaticFlag` instead of svc_process's `FeatureGate`.
    fn mock_db_wiring(flag_on: bool) -> DbWiring {
        let conn = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres).into_connection();
        DbWiring {
            host: Arc::new(DbHost::new(bundle_host_db::PostgresBackend::new(conn))),
            schemas: Arc::new(SchemaCache::new()),
            capabilities: Arc::new(DbCapabilitySnapshot::new()),
            flag: crate::flags::boxed(crate::flags::StaticFlag(flag_on)),
        }
    }

    #[tokio::test]
    async fn db_capability_denies_not_implemented_when_never_wired() {
        let capabilities = caps(FakeRelayQueue::default());
        let err = capabilities
            .handle_db(
                &scope(),
                &call(
                    CapabilityKind::Db,
                    "get",
                    serde_json::json!({"row_id": "x"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    #[tokio::test]
    async fn db_capability_denies_feature_disabled_when_flag_is_off() {
        let capabilities = caps(FakeRelayQueue::default()).with_db(mock_db_wiring(false));
        let err = capabilities
            .handle_db(
                &scope(),
                &call(
                    CapabilityKind::Db,
                    "get",
                    serde_json::json!({"row_id": "x"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "feature_disabled");
    }

    #[tokio::test]
    async fn db_capability_denies_no_table_when_flag_on_but_unprovisioned() {
        let capabilities = caps(FakeRelayQueue::default()).with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(
                &scope(),
                &call(
                    CapabilityKind::Db,
                    "get",
                    serde_json::json!({"row_id": "00000000-0000-0000-0000-000000000000"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[tokio::test]
    async fn db_capability_query_op_denies_no_table_when_unprovisioned() {
        let capabilities = caps(FakeRelayQueue::default()).with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(
                &scope(),
                &call(
                    CapabilityKind::Db,
                    "query",
                    serde_json::json!({"limit": 10, "offset": 0}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[tokio::test]
    async fn db_capability_rejects_an_unknown_op() {
        let capabilities = caps(FakeRelayQueue::default()).with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(
                &scope(),
                &call(CapabilityKind::Db, "truncate", serde_json::json!({})),
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
        let caps = caps_with_kv_and_capabilities(
            FakeRelayQueue::default(),
            &["waddles.bot.commands.default", "waddles.bot.other"],
        );
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

    #[tokio::test]
    async fn db_capability_dispatches_through_the_handle_match_arm() {
        // Proves `CapabilityKind::Db` in `StageCapabilities::handle` itself
        // reaches `handle_db` (not just the direct-call unit tests above).
        let capabilities = caps(FakeRelayQueue::default()).with_db(mock_db_wiring(false));
        let err = CapabilityHandler::handle(
            &capabilities,
            &scope(),
            call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "x"}),
            ),
        )
        .await
        .unwrap_err();
        assert_eq!(err.code, "feature_disabled");
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
        // Versioned op envelope (provider-framework Step 0) alongside the
        // legacy top-level fields.
        assert_eq!(parsed["v"], 1);
        assert_eq!(parsed["op"], "chat.send");
        assert_eq!(parsed["platform"], "twitch");
    }

    #[test]
    fn relay_op_parse_round_trips_and_rejects_unknown() {
        for op in [RelayOp::ChatSend, RelayOp::ChatDelete, RelayOp::DmSend] {
            assert_eq!(RelayOp::parse(op.as_str()), Some(op));
        }
        assert_eq!(RelayOp::parse("chat.nuke"), None);
        assert!(relay_op_supported("twitch", RelayOp::ChatSend));
        assert!(!relay_op_supported("twitch", RelayOp::ChatDelete));
        assert!(!relay_op_supported("discord", RelayOp::DmSend));
    }

    #[test]
    fn peek_relay_op_defaults_to_chat_send_and_flags_unknown() {
        let legacy = serde_json::json!({"message_json": r#"{"channel":"c","text":"hi"}"#});
        assert_eq!(peek_relay_op(&legacy), Ok(RelayOp::ChatSend));
        assert_eq!(
            peek_relay_op(&serde_json::json!({"provider": "twitch"})),
            Ok(RelayOp::ChatSend)
        );
        let del = serde_json::json!({"message_json": r#"{"op":"chat.delete"}"#});
        assert_eq!(peek_relay_op(&del), Ok(RelayOp::ChatDelete));
        let bad = serde_json::json!({"message_json": r#"{"op":"bogus"}"#});
        assert_eq!(peek_relay_op(&bad), Err("bogus".to_string()));
    }

    #[tokio::test]
    async fn relay_unknown_op_fails_loud_and_queues_nothing() {
        let caps = caps(FakeRelayQueue::default());
        let message_json = serde_json::json!({"op": "bogus", "channel": "#c", "text": "hi"});
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": message_json.to_string()}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
        assert!(caps.relay_queue.pushed.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn relay_new_ops_fail_loud_unsupported_and_queue_nothing() {
        for (provider, message) in [
            (
                "twitch",
                serde_json::json!({"op": "chat.delete", "channel": "#c", "message_id": "m1"}),
            ),
            (
                "twitch",
                serde_json::json!({"op": "dm.send", "user_id": "u1", "text": "hi"}),
            ),
            (
                "discord",
                serde_json::json!({"op": "dm.send", "user_id": "u1", "text": "hi"}),
            ),
        ] {
            let caps = caps(FakeRelayQueue::default());
            let err = caps
                .handle(
                    &scope(),
                    call(
                        CapabilityKind::Relay,
                        "send",
                        serde_json::json!({"provider": provider, "message_json": message.to_string()}),
                    ),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "unsupported_op", "{provider} {message}");
            assert!(caps.relay_queue.pushed.lock().unwrap().is_empty());
        }
    }

    #[tokio::test]
    async fn relay_new_ops_require_their_own_grant_not_chat_send() {
        // `caps_denied` has an empty grant snapshot; a `chat.send` grant must
        // never stand in for `chat.delete:`/`dm.send:`.
        let caps = caps_denied(FakeRelayQueue::default());
        let message = serde_json::json!({"op": "dm.send", "user_id": "u1", "text": "hi"});
        let err = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": message.to_string()}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
        assert!(caps.relay_queue.pushed.lock().unwrap().is_empty());
    }

    /// Minimal [`egress_detokenizer::DisplayNameResolver`] test fixture:
    /// resolves every token in `self.0` to its mapped name, nothing else.
    struct FixtureResolver(std::collections::HashMap<String, String>);

    impl egress_detokenizer::DisplayNameResolver for FixtureResolver {
        fn resolve_many<'a>(
            &'a self,
            _tenant_id: &'a str,
            tokens: Vec<String>,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<
                        Output = Result<
                            std::collections::HashMap<String, String>,
                            egress_detokenizer::DetokenizeError,
                        >,
                    > + Send
                    + 'a,
            >,
        > {
            let map = self.0.clone();
            Box::pin(async move {
                Ok(tokens
                    .into_iter()
                    .filter_map(|t| map.get(&t).cloned().map(|n| (t, n)))
                    .collect())
            })
        }
    }

    /// PII boundary hard invariant: a Twitch relay send with detokenization
    /// wired and enabled substitutes the token with the resolved display
    /// name before the message is queued -- a user never sees a raw
    /// `{user:<token>}` placeholder in chat.
    #[tokio::test]
    async fn relay_send_detokenizes_the_token_before_queuing_when_wired_and_enabled() {
        let resolver: Arc<dyn egress_detokenizer::DisplayNameResolver> = Arc::new(FixtureResolver(
            [("abc-1".to_string(), "CoolStreamer".to_string())]
                .into_iter()
                .collect(),
        ));
        let gate: Arc<dyn FeatureFlag> = Arc::new(crate::flags::StaticFlag(true));
        let caps = caps(FakeRelayQueue::default()).with_detokenize(resolver, gate);
        let result = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Relay,
                    "send",
                    serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#somechannel\",\"text\":\"hi {user:abc-1}\"}"}),
                ),
            )
            .await
            .expect("relay send succeeds");
        assert_eq!(result["queued"], serde_json::json!(true));
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["text"], "hi CoolStreamer");
        assert!(!parsed["text"].as_str().unwrap().contains("abc-1"));
    }

    /// Fail-safe-empty regression: an unresolved token (resolver has no
    /// entry for it) substitutes the neutral label, never the raw token.
    #[tokio::test]
    async fn relay_send_detokenizes_an_unresolved_token_to_the_neutral_label() {
        let resolver: Arc<dyn egress_detokenizer::DisplayNameResolver> =
            Arc::new(FixtureResolver(std::collections::HashMap::new()));
        let gate: Arc<dyn FeatureFlag> = Arc::new(crate::flags::StaticFlag(true));
        let caps = caps(FakeRelayQueue::default()).with_detokenize(resolver, gate);
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#somechannel\",\"text\":\"hi {user:missing}\"}"}),
            ),
        )
        .await
        .expect("relay send succeeds");
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(
            parsed["text"],
            format!("hi {}", egress_detokenizer::NEUTRAL_LABEL)
        );
    }

    /// Kill-switch + not-configured-yet regression: with no resolver wired
    /// at all, a relay send falls back to showing the raw token -- the
    /// degraded, but never-PII-leaking, fallback (`detokenize_text`'s doc).
    #[tokio::test]
    async fn relay_send_passes_the_raw_token_through_when_detokenize_is_not_configured() {
        let caps = caps(FakeRelayQueue::default());
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#somechannel\",\"text\":\"hi {user:abc-1}\"}"}),
            ),
        )
        .await
        .expect("relay send succeeds");
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["text"], "hi {user:abc-1}");
    }

    /// Kill-switch regression: wired but disabled (gate OFF) behaves
    /// identically to not-configured-at-all -- raw token passes through.
    #[tokio::test]
    async fn relay_send_passes_the_raw_token_through_when_the_kill_switch_is_on() {
        let resolver: Arc<dyn egress_detokenizer::DisplayNameResolver> = Arc::new(FixtureResolver(
            [("abc-1".to_string(), "CoolStreamer".to_string())]
                .into_iter()
                .collect(),
        ));
        let gate: Arc<dyn FeatureFlag> = Arc::new(crate::flags::StaticFlag(false));
        let caps = caps(FakeRelayQueue::default()).with_detokenize(resolver, gate);
        caps.handle(
            &scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "message_json": "{\"channel\":\"#somechannel\",\"text\":\"hi {user:abc-1}\"}"}),
            ),
        )
        .await
        .expect("relay send succeeds");
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["text"], "hi {user:abc-1}");
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

    /// PII boundary hard invariant, Discord sink: a resolved display name
    /// containing Discord markdown control characters is escaped so it
    /// can't smuggle a mention/formatting out of its position in the
    /// rendered content.
    #[tokio::test]
    async fn relay_send_discord_detokenizes_and_escapes_the_resolved_name() {
        let transport = Arc::new(FakeDiscordTransport::default());
        let resolver: Arc<dyn egress_detokenizer::DisplayNameResolver> = Arc::new(FixtureResolver(
            [("abc-1".to_string(), "@everyone".to_string())]
                .into_iter()
                .collect(),
        ));
        let gate: Arc<dyn FeatureFlag> = Arc::new(crate::flags::StaticFlag(true));
        let caps = caps(FakeRelayQueue::default())
            .with_discord(
                transport.clone(),
                crate::config::Secret::new("test-bot-token"),
            )
            .with_detokenize(resolver, gate);

        caps.handle(
            &discord_scope(),
            call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "discord", "message_json": r#"{"text":"hi {user:abc-1}"}"#}),
            ),
        )
        .await
        .expect("discord relay send succeeds");

        let requests = transport.requests.lock().unwrap();
        let body = requests[0].body.as_ref().expect("body present");
        let parsed: serde_json::Value = serde_json::from_slice(body).unwrap();
        let content = parsed["content"].as_str().unwrap();
        assert_eq!(content, "hi \\@everyone");
        assert!(!content.contains("abc-1"));
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
        .with_kv(
            FakeKvBackend::default(),
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
        )
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
        .with_kv(
            FakeKvBackend::default(),
            Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
        );

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
        let kv_capabilities = bundle_host_kv::CapabilitySnapshot::new();
        kv_capabilities.update(
            "waddles.bot.commands.default",
            [bundle_host_kv::authorize::KV_PERMISSION_ID.to_string()],
        );
        let caps = StageCapabilities::new(
            FakeRelayQueue::default(),
            test_egress(),
            Arc::new(Mutex::new(UsageBatcher::new())),
            gate,
        )
        .with_kv(FakeKvBackend::default(), Arc::new(kv_capabilities));

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

    // ---- handle_flags: authorize() allow/deny + op dispatch ------------

    /// `permissive_gate()` grants `flags.read` to this scope's app id --
    /// the gate's allow branch must fall through to the real
    /// `crate::flags::resolve_flag` resolution, not short-circuit deny.
    /// Asserts `true` regardless of `default_value` (rather than asserting
    /// a specific value derived from it) because this test process has no
    /// license env vars configured, so `resolve_flag`'s hardcoded-domain
    /// bypass (`crate::flags`'s own `bypass_active_resolves_true_
    /// regardless_of_default` test) is what actually answers the call --
    /// the point here is that `handle_flags` reached that resolution at
    /// all, not which fallback tier inside it fired.
    #[tokio::test]
    async fn flags_enabled_resolves_through_the_gate_when_granted() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(
                &scope(),
                call(
                    CapabilityKind::Flags,
                    "enabled",
                    serde_json::json!({"key": "waddles.some-capability", "default_value": false}),
                ),
            )
            .await
            .expect("flags.read is granted by permissive_gate()");
        assert_eq!(result["enabled"], serde_json::json!(true));
    }

    #[tokio::test]
    async fn flags_enabled_malformed_args_is_invalid_args() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Flags, "enabled", serde_json::json!({})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn flags_unknown_op_is_not_implemented() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(
                &scope(),
                call(CapabilityKind::Flags, "bogus-op", serde_json::json!({})),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    // ---- db/JSON value mapping helpers (handle_db's building blocks) ---

    #[test]
    fn json_to_db_value_maps_every_supported_json_shape() {
        assert_eq!(
            json_to_db_value(&serde_json::json!(null)).unwrap(),
            DbValue::Null
        );
        assert_eq!(
            json_to_db_value(&serde_json::json!(true)).unwrap(),
            DbValue::Bool(true)
        );
        assert_eq!(
            json_to_db_value(&serde_json::json!(42)).unwrap(),
            DbValue::Int(42)
        );
        assert_eq!(
            json_to_db_value(&serde_json::json!(1.5)).unwrap(),
            DbValue::Float(1.5)
        );
        assert_eq!(
            json_to_db_value(&serde_json::json!("hi")).unwrap(),
            DbValue::Text("hi".to_string())
        );
    }

    #[test]
    fn json_to_db_value_rejects_arrays_and_objects() {
        assert_eq!(
            json_to_db_value(&serde_json::json!([1, 2]))
                .unwrap_err()
                .code,
            "invalid_args"
        );
        assert_eq!(
            json_to_db_value(&serde_json::json!({"a": 1}))
                .unwrap_err()
                .code,
            "invalid_args"
        );
    }

    #[test]
    fn parse_order_by_defaults_to_none_when_absent_or_null() {
        assert_eq!(parse_order_by(&serde_json::json!({})).unwrap(), None);
        assert_eq!(
            parse_order_by(&serde_json::json!({"order_by": null})).unwrap(),
            None
        );
    }

    #[test]
    fn parse_order_by_parses_random() {
        assert_eq!(
            parse_order_by(&serde_json::json!({"order_by": {"random": true}})).unwrap(),
            Some(bundle_host_db::OrderBy::Random)
        );
    }

    #[test]
    fn parse_order_by_parses_a_column_with_explicit_descending() {
        assert_eq!(
            parse_order_by(&serde_json::json!({
                "order_by": {"column": "created_at", "descending": true}
            }))
            .unwrap(),
            Some(bundle_host_db::OrderBy::Column {
                name: "created_at".to_string(),
                descending: true,
            })
        );
    }

    #[test]
    fn parse_order_by_defaults_descending_to_false() {
        assert_eq!(
            parse_order_by(&serde_json::json!({"order_by": {"column": "row_id"}})).unwrap(),
            Some(bundle_host_db::OrderBy::Column {
                name: "row_id".to_string(),
                descending: false,
            })
        );
    }

    #[test]
    fn parse_order_by_rejects_a_malformed_order_by_object() {
        let err = parse_order_by(&serde_json::json!({"order_by": {}})).unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[test]
    fn db_value_to_json_maps_every_variant() {
        assert_eq!(db_value_to_json(&DbValue::Null), serde_json::Value::Null);
        assert_eq!(
            db_value_to_json(&DbValue::Bool(true)),
            serde_json::json!(true)
        );
        assert_eq!(db_value_to_json(&DbValue::Int(7)), serde_json::json!(7));
        assert_eq!(
            db_value_to_json(&DbValue::Float(2.5)),
            serde_json::json!(2.5)
        );
        assert_eq!(
            db_value_to_json(&DbValue::Text("x".to_string())),
            serde_json::json!("x")
        );
        assert_eq!(
            db_value_to_json(&DbValue::Bytes(vec![1, 2, 3])),
            serde_json::json!([1, 2, 3])
        );
    }

    #[test]
    fn row_to_json_shapes_row_id_version_and_columns() {
        let row = bundle_host_db::Row {
            row_id: "r1".to_string(),
            version: 3,
            columns: vec![("name".to_string(), DbValue::Text("quote".to_string()))],
        };
        let json = row_to_json(row);
        assert_eq!(json["row_id"], serde_json::json!("r1"));
        assert_eq!(json["version"], serde_json::json!(3));
        assert_eq!(json["columns"]["name"], serde_json::json!("quote"));
    }

    #[test]
    fn db_error_to_host_error_carries_the_stable_reason_code() {
        let err = db_error_to_host_error(DbError::NoTable);
        assert_eq!(err.code, "no_table");
        let err = db_error_to_host_error(DbError::Conflict);
        assert_eq!(err.code, "conflict");
    }
}

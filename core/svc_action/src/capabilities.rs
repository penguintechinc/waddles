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
//! why Discord takes a different path than Twitch), `clock`, `context` and
//! `log` are fully wired. `http` is wired to `crate::egress::EgressGuard`
//! (spec §8's full SSRF guard). `db`/`kv`/`flags` remain documented
//! `TODO(M3+)` seams -- see [`StageCapabilities::handle`]'s match arms.
//!
//! A bundle never holds a platform credential (spec §4.3): every
//! capability here resolves any credential itself, from this process's own
//! configuration/environment, never from the `args` the guest supplied.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};

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
/// Holds no per-connection tenant/community/app_id (see the module doc --
/// that scope now arrives per call via [`InvokeScope`]).
pub struct StageCapabilities<Q: RelayQueue> {
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
}

impl<Q: RelayQueue> StageCapabilities<Q> {
    /// Builds the capability set this connection's read loop answers every
    /// `host-call` against, for as long as the connection lives. No
    /// tenant/community/app_id here -- every capability method below takes
    /// its [`InvokeScope`] as a parameter instead (module doc). The Discord
    /// relay provider starts unconfigured (`relay_unavailable` until
    /// [`Self::with_discord`] is chained on) so every existing caller of
    /// this constructor -- production and test alike -- is unaffected by
    /// this landing.
    pub fn new(relay_queue: Q, egress: Arc<EgressGuard>, usage: Arc<Mutex<UsageBatcher>>) -> Self {
        Self {
            relay_queue,
            egress,
            usage,
            discord: None,
        }
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

    fn handle_clock(&self, op: &str) -> Result<serde_json::Value, HostResultError> {
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
}

impl<Q: RelayQueue> CapabilityHandler for StageCapabilities<Q> {
    fn handle<'a>(
        &'a self,
        scope: &'a InvokeScope,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            match call.capability {
                CapabilityKind::Relay => self.handle_relay(scope, &call.args).await,
                CapabilityKind::Clock => self.handle_clock(&call.op),
                CapabilityKind::Context => self.handle_context(scope),
                CapabilityKind::Log => self.handle_log(scope, &call.args),
                CapabilityKind::Http => self.egress.send(&scope.app_id, &call.args).await,
                // TODO(M3+): `db`/`kv`/`flags` (spec §7.4's SQL-parser-gated
                // statement execution, the per-bundle KV hash, and the
                // `penguin-licensing` two-gate flag check) are not wired in
                // this landing. Denying (never silently succeeding) is the
                // correct behavior for an unimplemented capability: a
                // bundle calling it sees `access-denied`, not a fabricated
                // success.
                CapabilityKind::Db => Err(denied(
                    "not_implemented",
                    "db capability is not wired in this build -- TODO(M3+)",
                )),
                CapabilityKind::Kv => Err(denied(
                    "not_implemented",
                    "kv capability is not wired in this build -- TODO(M3+)",
                )),
                CapabilityKind::Flags => Err(denied(
                    "not_implemented",
                    "flags capability is not wired in this build -- TODO(M3+)",
                )),
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

    fn caps(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
        caps_with_usage(queue, Arc::new(Mutex::new(UsageBatcher::new())))
    }

    fn caps_with_usage(
        queue: FakeRelayQueue,
        usage: Arc<Mutex<UsageBatcher>>,
    ) -> StageCapabilities<FakeRelayQueue> {
        StageCapabilities::new(queue, test_egress(), usage)
    }

    fn scope() -> InvokeScope {
        InvokeScope {
            tenant: "acme".to_string(),
            community: Some("main".to_string()),
            app_id: "waddles.bot.commands.default".to_string(),
            origin_channel_id: None,
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
    async fn db_kv_flags_capabilities_are_documented_seams() {
        let caps = caps(FakeRelayQueue::default());
        for capability in [
            CapabilityKind::Db,
            CapabilityKind::Kv,
            CapabilityKind::Flags,
        ] {
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

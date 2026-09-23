//! Host capability implementations serviced on the stage side of the
//! host-API connection (spec §7.4): the executor issues a `host-call`
//! frame naming a `capability`/`op`, and this module answers it, replying
//! `host-result`. This M3 landing wires the two capabilities the Twitch
//! relay sender path needs end to end (`relay`, `clock`) plus `context`
//! (needed for every `invoke` regardless of sender) and `log` (sanitized
//! passthrough); `http`/`kv`/`db` are documented seams -- see
//! [`StageCapabilities::handle`]'s match arms -- since the REST platform
//! senders that would exercise them (Discord/Slack/YouTube/Kick) are
//! themselves `TODO(M3)` seams in `crate::senders` (spec §16 M3 row's
//! "REALISTIC SCOPE": "leave remaining senders as honest TODO seams").
//!
//! A bundle never holds a platform credential (spec §4.3): every
//! capability here resolves any credential itself, from this process's own
//! configuration/environment, never from the `args` the guest supplied.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};

/// Answers one `host-call` for a given `capability`/`op`. Object-safe (a
/// manually-boxed future rather than `async fn` in a trait) so the host-API
/// connection can hold `Arc<dyn CapabilityHandler>` without an `async_trait`
/// dependency.
pub trait CapabilityHandler: Send + Sync {
    /// Services one host call, returning the capability-specific JSON
    /// result or a `{code, message}` error the executor forwards to the
    /// guest as `error-code::access-denied`/`internal-error` per the WIT
    /// world's `host-error` variant (spec §6.5).
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>>;
}

fn denied(code: &str, message: impl Into<String>) -> HostResultError {
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

/// Compiled-in providers the `relay` capability accepts (spec §7.4:
/// "Validates `provider` against the compiled-in provider list (`twitch`
/// today)"). Action-stage bundles only.
const RELAY_PROVIDERS: &[&str] = &["twitch"];

/// Strips CR/LF and every other control character before an outbound relay
/// write -- a byte-exact port of `waddle_transports.transports.irc.
/// sanitize_irc_component`'s CRLF-injection defense, applied here (not just
/// by the eventual IRC-writing process) so a malformed payload is rejected
/// before it is ever queued.
fn sanitize_irc_component(value: &str) -> String {
    value.chars().filter(|c| !c.is_control()).collect()
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

/// The real capability implementations this stage wires today. Generic
/// over [`RelayQueue`] so `handle_relay` is fully unit-testable against a
/// fake queue without a live Valkey server -- production callers
/// instantiate `StageCapabilities<redis::aio::MultiplexedConnection>`.
pub struct StageCapabilities<Q: RelayQueue> {
    relay_queue: Q,
    tenant: String,
    community: Option<String>,
    app_id: String,
}

impl<Q: RelayQueue> StageCapabilities<Q> {
    /// Builds the capability set for one dispatch loop's lifetime, scoped
    /// to the (tenant, community, app_id) it is currently servicing --
    /// `context` and every other capability are scope-implicit (spec
    /// §5.11: "No bundle host call accepts a tenant or community argument
    /// at all").
    pub fn new(relay_queue: Q, tenant: String, community: Option<String>, app_id: String) -> Self {
        Self {
            relay_queue,
            tenant,
            community,
            app_id,
        }
    }

    async fn handle_relay(
        &self,
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
        let channel = args
            .get("channel")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| denied("invalid_args", "relay.send requires a non-empty 'channel'"))?;
        let text = args
            .get("text")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| denied("invalid_args", "relay.send requires non-empty 'text'"))?;

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
        self.relay_queue
            .lpush(&key, payload)
            .await
            .map_err(|e| denied("relay_unavailable", e))?;
        Ok(serde_json::json!({"queued": true, "provider": provider}))
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

    fn handle_context(&self) -> Result<serde_json::Value, HostResultError> {
        // Spec §7.4: "Tenant and community come from the key, never from
        // payload." Never includes a credential or secret.
        Ok(serde_json::json!({
            "tenant": self.tenant,
            "community": self.community,
            "app_id": self.app_id,
        }))
    }

    fn handle_log(&self, args: &serde_json::Value) -> Result<serde_json::Value, HostResultError> {
        let level = args.get("level").and_then(|v| v.as_str()).unwrap_or("info");
        let message = args
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("<empty>");
        // `fields-json` sanitization (spec §7.4) belongs to
        // `penguin-logging`'s `SENSITIVE_KEYS` rule once this call is wired
        // to structured field emission; this landing logs via `tracing`
        // directly with the bundle's own attribution.
        match level {
            "error" => {
                tracing::error!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
            "warn" => {
                tracing::warn!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
            "debug" => {
                tracing::debug!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
            _ => {
                tracing::info!(app_id = %self.app_id, tenant = %self.tenant, bundle_log = %message, "bundle log")
            }
        }
        Ok(serde_json::json!({}))
    }
}

impl<Q: RelayQueue> CapabilityHandler for StageCapabilities<Q> {
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            match call.capability {
                CapabilityKind::Relay => self.handle_relay(&call.args).await,
                CapabilityKind::Clock => self.handle_clock(&call.op),
                CapabilityKind::Context => self.handle_context(),
                CapabilityKind::Log => self.handle_log(&call.args),
                // TODO(M3+): `http` (guarded egress + per-platform
                // credential injection for the REST senders --
                // Discord/Slack/YouTube/Kick, spec §7.4/§8) and `db`/`kv`
                // (spec §7.4's SQL-parser-gated statement execution and the
                // per-bundle KV hash) are not wired in this landing --
                // `crate::senders` documents the same gap per platform.
                // Denying (never silently succeeding) is the correct
                // behavior for an unimplemented capability: a bundle
                // calling it sees `access-denied`, not a fabricated
                // success.
                CapabilityKind::Http => Err(denied(
                    "not_implemented",
                    "http capability is not wired in this build -- TODO(M3+), see crate::senders",
                )),
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
/// capability set is configured (e.g. a health-check-only invocation path)
/// or in tests exercising the host-API connection layer in isolation from
/// capability semantics.
pub struct DenyAllCapabilities;

impl CapabilityHandler for DenyAllCapabilities {
    fn handle<'a>(
        &'a self,
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

    fn caps(queue: FakeRelayQueue) -> StageCapabilities<FakeRelayQueue> {
        StageCapabilities::new(
            queue,
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
        )
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

    #[tokio::test]
    async fn relay_send_pushes_the_expected_key_and_payload() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "channel": "#somechannel", "text": "hi"}),
            ))
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

    #[tokio::test]
    async fn relay_send_sanitizes_crlf_before_queuing() {
        let caps = caps(FakeRelayQueue::default());
        caps.handle(call(
            CapabilityKind::Relay,
            "send",
            serde_json::json!({"provider": "twitch", "channel": "#c", "text": "line1\r\nline2"}),
        ))
        .await
        .expect("relay send succeeds");
        let pushed = caps.relay_queue.pushed.lock().unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["text"], "line1line2");
    }

    #[tokio::test]
    async fn relay_send_rejects_unknown_provider() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "discord", "channel": "c", "text": "hi"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_provider");
        assert!(caps.relay_queue.pushed.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn relay_send_rejects_empty_channel() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "channel": "", "text": "hi"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_rejects_empty_text() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "channel": "c", "text": ""}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn relay_send_missing_provider_is_invalid_args() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"channel": "c", "text": "hi"}),
            ))
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
            .handle(call(
                CapabilityKind::Relay,
                "send",
                serde_json::json!({"provider": "twitch", "channel": "c", "text": "hi"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "relay_unavailable");
    }

    #[tokio::test]
    async fn clock_now_millis_returns_a_positive_integer() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(call(
                CapabilityKind::Clock,
                "now-millis",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds");
        assert!(result.as_u64().unwrap() > 0);
    }

    #[tokio::test]
    async fn clock_unknown_op_is_denied() {
        let caps = caps(FakeRelayQueue::default());
        let err = caps
            .handle(call(CapabilityKind::Clock, "bogus", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn context_reports_scope_without_secrets() {
        let caps = caps(FakeRelayQueue::default());
        let result = caps
            .handle(call(CapabilityKind::Context, "get", serde_json::json!({})))
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
                .handle(call(
                    CapabilityKind::Log,
                    "write",
                    serde_json::json!({"level": level, "message": "hello"}),
                ))
                .await
                .expect("log write succeeds");
            assert_eq!(result, serde_json::json!({}));
        }
    }

    #[tokio::test]
    async fn http_kv_db_flags_capabilities_are_documented_seams() {
        let caps = caps(FakeRelayQueue::default());
        for capability in [
            CapabilityKind::Http,
            CapabilityKind::Db,
            CapabilityKind::Kv,
            CapabilityKind::Flags,
        ] {
            let err = caps
                .handle(call(capability, "anything", serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_implemented");
        }
    }

    #[tokio::test]
    async fn deny_all_denies_every_capability() {
        let handler = DenyAllCapabilities;
        let result = handler
            .handle(call(
                CapabilityKind::Clock,
                "now-millis",
                serde_json::json!({}),
            ))
            .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn boxed_wraps_a_handler_as_an_arc_dyn() {
        let handler: Arc<dyn CapabilityHandler> = boxed(DenyAllCapabilities);
        let result = handler
            .handle(call(CapabilityKind::Log, "write", serde_json::json!({})))
            .await;
        assert!(result.is_err());
    }
}

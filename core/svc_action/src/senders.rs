//! Built-in platform senders (spec §4.3/§16 M3 row): "Platform senders
//! become Rust built-ins the bundles reach through the host API (`relay`
//! for Twitch, `http` with guarded egress for REST platforms) -- a bundle
//! never holds a platform credential."
//!
//! This M3 landing implements the Valkey `LPUSH` half of the Twitch relay
//! path in `crate::capabilities::StageCapabilities::handle_relay` (the
//! platform explicitly called out as the relay-based one, and the simplest
//! to land completely: no SSRF-guarded `http` capability, no per-platform
//! credential injection).
//!
//! **Discord is a bot-token built-in, never a bundle-held credential and
//! never the bundle `http` capability / `crate::egress::EgressGuard`.**
//! Three ops, two paths: `chat.send` is an inline bot-REST `POST` from
//! `StageCapabilities::handle_discord_relay`; `chat.delete` and `dm.send`
//! are queued to svc-ingest's bot-token REST sender
//! (`svc_ingest::outbound::run_discord`) -- accepted only while that drain
//! advertises readiness, and reported successful only once it confirms the
//! op (`StageCapabilities::handle_discord_queued_op`). Slack/YouTube/Kick
//! remain a documented `TODO(M3+)` seam -- rather than reimplement connector logic
//! here, this module leaves an explicit seam per remaining platform naming
//! the `penguin-connectors` crate that owns it, per the M3 task's own
//! instruction ("USE penguin-connectors' senders where they exist ... do
//! NOT reimplement connector logic in svc_action").
//!
//! **Wired end to end (post-M3 review fix).** `crate::lib::try_start_host_api`
//! now installs a real `crate::capabilities::StageCapabilities` -- backed
//! by a live Valkey connection for `relay` and `crate::egress::EgressGuard`
//! for `http` -- as the live connection's handler, scoped per invoke (not
//! per connection, see `crate::capabilities`'s module doc for why that
//! distinction matters). A bundle's `relay`/`http` host-call is answered
//! for real; `Platform`/`sender_status`/`is_retryable`/`twitch_relay_args`
//! below remain reference/documentation helpers for the *bundle-authoring*
//! side of this contract (the relay/http call itself is issued by the
//! bundle via the wire protocol, per the doc below, never constructed by
//! this stage) and by `crate::dispatch::interpret_dispatch_payload`'s
//! `target_type_hint`, which stays `"irc_relay"` (Python-parity transport
//! name, `libs/waddle_transports/transports/irc_relay.py`'s `name` field --
//! not `Platform::Twitch.as_str()`, a different, bundle-facing string).

use penguin_bundle_host::wire::HostResultError;

/// The platform a dispatch outcome targeted -- becomes
/// `action_dispatch_log.target_type` (spec: mirrors the Python runner's
/// `result.transport`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Twitch,
    Discord,
    Slack,
    Youtube,
    Kick,
}

impl Platform {
    pub fn as_str(&self) -> &'static str {
        match self {
            Platform::Twitch => "twitch",
            Platform::Discord => "discord",
            Platform::Slack => "slack",
            Platform::Youtube => "youtube",
            Platform::Kick => "kick",
        }
    }
}

/// Whether a sender path is implemented in this build, or is a documented
/// `TODO(M3+)` seam awaiting a `penguin-connectors` REST sender.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SenderStatus {
    /// Fully wired end to end through the `relay` host capability (Twitch
    /// via the IRC drain; Discord via bot-token REST -- see the module doc).
    Implemented,
    /// `TODO(M3+): {platform} sender -- pending penguin-connectors
    /// {platform}` -- names exactly which upstream crate/module will back
    /// it once the `http` capability (`crate::capabilities`) lands.
    PendingSeam { reason: &'static str },
}

/// Reports which of the five built-in senders spec §16 M3 lists are
/// implemented in this build vs. an honest seam -- used by `crate::dispatch`
/// to fail a dispatch attempt with a clear, non-retryable error rather than
/// silently no-op for a platform this build doesn't yet send to.
pub fn sender_status(platform: Platform) -> SenderStatus {
    match platform {
        Platform::Twitch => SenderStatus::Implemented,
        // Bot-token REST (module doc): chat.send inline; chat.delete/dm.send
        // queued to svc-ingest's Discord REST sender behind the drain-ready
        // + confirmation handshake (relay capability).
        Platform::Discord => SenderStatus::Implemented,
        Platform::Slack => SenderStatus::PendingSeam {
            reason: "TODO(M3+): slack sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connectors' Slack scaffold",
        },
        Platform::Youtube => SenderStatus::PendingSeam {
            reason: "TODO(M3+): youtube sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connectors' YouTube scaffold",
        },
        Platform::Kick => SenderStatus::PendingSeam {
            reason: "TODO(M3+): kick sender -- pending the http host capability \
                     (crate::capabilities) and penguin-connectors' Kick scaffold",
        },
    }
}

/// Builds the `relay` host-call args for a Twitch chat send -- the shape
/// `crate::capabilities::StageCapabilities::handle_relay` expects, which is
/// in turn the shape `core/bundle_executor/src/host/imports.rs`'s
/// `relay::Host::push` actually proxies over the wire for the WIT
/// `relay.push(provider: string, message-json: string)` import:
/// `{"provider", "message_json"}`, with `channel`/`text` nested INSIDE the
/// `message_json` string, never top-level args (fixed alongside
/// `handle_relay` itself, `fix/svc-action-bundle-executor-alpha-wiring` --
/// this function's prior flat `{"provider","channel","text"}` shape was
/// self-consistent with `handle_relay`'s own prior bug, so this exact
/// mismatch with the real executor was never caught until the hermetic
/// relay e2e proof). `crate::dispatch` issues this over the live host-API
/// `Connection` as a `host-call` frame the executor's `dispatch` export
/// would normally trigger; a bundle never constructs this JSON itself
/// (spec §4.3: "a bundle never holds a platform credential" -- there is no
/// credential here at all, which is exactly why Twitch is relay-based).
pub fn twitch_relay_args(channel: &str, text: &str) -> serde_json::Value {
    let message_json = serde_json::json!({"channel": channel, "text": text}).to_string();
    serde_json::json!({
        "provider": "twitch",
        "message_json": message_json,
    })
}

/// Classifies a `relay` capability's `HostResultError` into whether the
/// dispatch attempt should retry (spec §4.3: `transport-error.retryable`).
/// `invalid_args`/`unknown_provider` are caller bugs -- never retryable;
/// `relay_unavailable` is a transient Valkey outage -- retryable.
pub fn is_retryable(err: &HostResultError) -> bool {
    matches!(err.code.as_str(), "relay_unavailable")
}

/// Extracts this bundle's own Discord webhook URL from its activation
/// config (`crate::distribution::BundleRow::config_json`, hub-api/admin-
/// controlled -- never bundle-runtime-supplied). Hardening fix (own
/// observation from the `secret_refs` finding, same principle applied
/// here): the webhook URL embeds a bearer-token-equivalent secret in its
/// path, so it must be resolved the same way as any other outbound
/// credential/target -- from this bundle's own trusted config, never a
/// literal a caller/bundle chooses at call time (spec §7.5: "the outbound
/// credential and target are resolved from the envelope's (tenant,
/// community) -- never from anything a bundle's outbound args carry").
/// [`discord_webhook_args`] takes an already-resolved URL for exactly this
/// reason: obtain it through this function, never by embedding a literal.
/// Returns `None` when the config has no `discord_webhook_url` string
/// (not configured for this bundle, or malformed config JSON) --
/// `crate::dispatch`'s caller treats that as a non-retryable configuration
/// error, never a reason to fall back to a bundle-supplied URL.
pub fn discord_webhook_url_from_config(config_json: &str) -> Option<String> {
    let config: serde_json::Value = serde_json::from_str(config_json).ok()?;
    config
        .get("discord_webhook_url")
        .and_then(|v| v.as_str())
        .map(str::to_string)
}

/// Builds the `http` host-call args for a Discord webhook send -- the
/// shape `crate::egress::EgressGuard::send` expects (spec §6.5's WIT
/// `http::request`, this crate's JSON-wire convention -- see
/// `crate::egress`'s module doc). `webhook_url` **must** come from
/// [`discord_webhook_url_from_config`] (this bundle's own trusted
/// activation config), never a caller-invented or bundle-runtime-supplied
/// literal -- see that function's doc for why. It must also already be on
/// the bundle's manifest `egress` allowlist (e.g. `{host: "discord.com",
/// methods: ["POST"]}`); the bundle constructs and issues this call itself
/// via the wire protocol -- this function documents the expected shape,
/// mirroring `twitch_relay_args` above for `relay`.
///
/// **The returned `url` is a live bearer credential** (the webhook token is
/// a *path* segment, `/api/webhooks/{id}/{token}`): never log it or put it
/// in an error/span. Render it for diagnostics only through
/// [`bundle_host_http::redact::redact_url_for_log`] (host + default-deny
/// redacted path), the same redaction `bundle_host_http`'s transport
/// applies to every send error, so a failed webhook send's
/// `HostResultError` never echoes the token back to the bundle or the
/// DLQ/log pipeline.
pub fn discord_webhook_args(webhook_url: &str, content: &str) -> serde_json::Value {
    use base64::Engine;
    let body = serde_json::json!({"content": content}).to_string();
    let body_base64 = base64::engine::general_purpose::STANDARD.encode(body.as_bytes());
    serde_json::json!({
        "method": "POST",
        "url": webhook_url,
        "headers": [{"name": "Content-Type", "value": "application/json"}],
        "body_base64": body_base64,
    })
}

/// Classifies an `http` capability's `HostResultError` into whether the
/// dispatch attempt should retry (spec §4.3). `rate_limited`/`timeout`/
/// `transport` are transient (a bundle should back off and retry); every
/// SSRF/allowlist/malformed-request denial reason is a caller-side
/// configuration problem -- never retryable, since retrying an undeclared
/// host or a bad URL will fail identically every time.
pub fn discord_is_retryable(err: &HostResultError) -> bool {
    matches!(err.code.as_str(), "rate_limited" | "timeout" | "transport")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twitch_and_discord_are_implemented_every_other_platform_is_a_seam() {
        assert_eq!(sender_status(Platform::Twitch), SenderStatus::Implemented);
        assert_eq!(sender_status(Platform::Discord), SenderStatus::Implemented);
        for platform in [Platform::Slack, Platform::Youtube, Platform::Kick] {
            assert!(matches!(
                sender_status(platform),
                SenderStatus::PendingSeam { .. }
            ));
        }
    }

    #[test]
    fn platform_as_str_matches_dispatch_log_target_type_convention() {
        assert_eq!(Platform::Twitch.as_str(), "twitch");
        assert_eq!(Platform::Discord.as_str(), "discord");
        assert_eq!(Platform::Slack.as_str(), "slack");
        assert_eq!(Platform::Youtube.as_str(), "youtube");
        assert_eq!(Platform::Kick.as_str(), "kick");
    }

    #[test]
    fn twitch_relay_args_shape_matches_the_relay_capability() {
        let args = twitch_relay_args("#somechannel", "hello");
        assert_eq!(args["provider"], "twitch");
        assert!(
            args.get("channel").is_none(),
            "channel must not be top-level"
        );
        assert!(args.get("text").is_none(), "text must not be top-level");
        let message_json = args["message_json"]
            .as_str()
            .expect("message_json is a string");
        let message: serde_json::Value = serde_json::from_str(message_json).unwrap();
        assert_eq!(message["channel"], "#somechannel");
        assert_eq!(message["text"], "hello");
    }

    #[test]
    fn discord_webhook_url_from_config_extracts_the_configured_url() {
        let config = r#"{"discord_webhook_url": "https://discord.com/api/webhooks/1/abc"}"#;
        assert_eq!(
            discord_webhook_url_from_config(config),
            Some("https://discord.com/api/webhooks/1/abc".to_string())
        );
    }

    #[test]
    fn discord_webhook_url_from_config_is_none_when_not_configured() {
        assert_eq!(discord_webhook_url_from_config("{}"), None);
    }

    #[test]
    fn discord_webhook_url_from_config_is_none_on_malformed_json() {
        assert_eq!(discord_webhook_url_from_config("not json"), None);
    }

    #[test]
    fn discord_webhook_args_shape_matches_the_http_capability() {
        let args = discord_webhook_args("https://discord.com/api/webhooks/1/abc", "hello");
        assert_eq!(args["method"], "POST");
        assert_eq!(args["url"], "https://discord.com/api/webhooks/1/abc");
        assert_eq!(args["headers"][0]["name"], "Content-Type");
        assert_eq!(args["headers"][0]["value"], "application/json");
        let body_b64 = args["body_base64"].as_str().unwrap();
        let decoded =
            base64::Engine::decode(&base64::engine::general_purpose::STANDARD, body_b64).unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&decoded).unwrap();
        assert_eq!(parsed["content"], "hello");
    }

    /// regression: a Discord webhook's bearer token is a URL *path* segment,
    /// and the old query/fragment-only scrub left it in the error text of a
    /// failed send (-> `HostResultError.message` -> bundle log / DLQ detail).
    /// Drives the webhook URL from config through the real
    /// `ReqwestTransport` against a refused local port -- no mocked
    /// transport -- and asserts neither the token nor the id appears.
    #[tokio::test]
    async fn failed_webhook_send_error_never_carries_the_webhook_token() {
        use crate::egress::{HttpTransport, ReqwestTransport, TransportRequest};

        const ID: &str = "1234567890123456789";
        const TOKEN: &str = "Zk3Xw9_tOkEn-s3cr3t-DoNotLeak-Zk3Xw9";
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);

        let config = serde_json::json!({
            "discord_webhook_url":
                format!("http://localhost:{}/api/webhooks/{ID}/{TOKEN}?wait=true", addr.port())
        })
        .to_string();
        let webhook_url = discord_webhook_url_from_config(&config).unwrap();
        let args = discord_webhook_args(&webhook_url, "hello");

        let err = ReqwestTransport::new()
            .send(
                TransportRequest {
                    method: args["method"].as_str().unwrap().to_string(),
                    url: args["url"].as_str().unwrap().to_string(),
                    pinned_addr: addr,
                    headers: vec![],
                    body: None,
                },
                std::time::Duration::from_secs(5),
                1024,
            )
            .await
            .expect_err("nothing is listening, so the send must fail");

        assert_eq!(err.code, "transport");
        assert!(discord_is_retryable(&err));
        for needle in [TOKEN, ID, "wait=true"] {
            assert!(
                !err.message.contains(needle),
                "{needle} leaked in {:?}",
                err.message
            );
        }
        // The helper the doc points callers at renders the same URL safely.
        let rendered = bundle_host_http::redact::redact_url_for_log(&webhook_url);
        assert!(!rendered.contains(TOKEN) && !rendered.contains(ID));
        assert!(rendered.ends_with("/api/webhooks/***/***"), "{rendered}");
    }

    #[test]
    fn discord_transient_codes_are_retryable_denial_reasons_are_not() {
        for code in ["rate_limited", "timeout", "transport"] {
            assert!(discord_is_retryable(&HostResultError {
                code: code.to_string(),
                message: "x".to_string()
            }));
        }
        for code in [
            "scheme_not_https",
            "host_not_declared",
            "method_not_declared",
            "malformed_url",
            "secret_unresolved",
            "ssrf_blocked_address",
        ] {
            assert!(!discord_is_retryable(&HostResultError {
                code: code.to_string(),
                message: "x".to_string()
            }));
        }
    }

    #[test]
    fn relay_unavailable_is_retryable_other_codes_are_not() {
        assert!(is_retryable(&HostResultError {
            code: "relay_unavailable".to_string(),
            message: "x".to_string()
        }));
        assert!(!is_retryable(&HostResultError {
            code: "invalid_args".to_string(),
            message: "x".to_string()
        }));
        assert!(!is_retryable(&HostResultError {
            code: "unknown_provider".to_string(),
            message: "x".to_string()
        }));
    }
}

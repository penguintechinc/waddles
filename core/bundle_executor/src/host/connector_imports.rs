//! `Host` trait implementations for the `waddle:connector@1.0.0` world's
//! imports (`wit/waddle-connector/connector.wit`, spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S1).
//!
//! `http`/`log`/`clock`/`%flags` are the same reused `waddle:bundle@1.0.0`
//! interfaces `crate::host::imports` already services for the `stage`
//! world -- these impls route through the identical [`imports::call`]
//! round trip, just against the distinct Rust types
//! `crate::engine::connector_world::bindgen!` generated for this world
//! (bindgen produces independent types per invocation even for an
//! identical WIT interface, so `crate::engine::engine::waddle::bundle::
//! http::Request` and `connector_world::waddle::bundle::http::Request` are
//! not the same Rust type despite matching field-for-field).
//!
//! `identity` is connector-only (never present in `stage`/`stage-v1_1`,
//! spec S3.3). Its real implementation -- a direct, RO-replica Postgres
//! read through the `waddles_connector_pii_reader` role, with the caching/
//! rate-limiting/audit trail spec S3.4 specifies -- is out of this task's
//! scope (phase 1 task A is the WIT world + per-component `Linker` gate,
//! not the identity backend). `identity::Host::lookup` below is a
//! deliberate, honest stub: it always returns `Error::Backend`, never
//! panics, and is reachable ONLY when `VerifiedManifest::may_link_identity`
//! already passed (`crate::engine::build_linker_for`) -- linking, not this
//! stub's behavior, is what this task tests.

use tracing::{debug, warn};

use crate::engine::connector_world::waddle::bundle::{clock, flags, http, log};
use crate::engine::connector_world::waddle::connector::identity;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;
use penguin_bundle_host::wire::CapabilityKind;

impl http::Host for ExecState {
    async fn send(&mut self, req: http::Request) -> Result<http::Response, http::Error> {
        let args = serde_json::json!({
            "method": req.method,
            "url": req.url,
            "headers": req.headers.iter().map(|h| serde_json::json!({"name": h.name, "value": h.value})).collect::<Vec<_>>(),
            "body": req.body,
            "secret_refs": req.secret_refs,
        });
        match call(self, CapabilityKind::Http, "send", args).await {
            Ok(value) => serde_json::from_value::<ConnectorHttpResponseWire>(value)
                .map(Into::into)
                .map_err(|e| http::Error::Transport(format!("malformed host-result: {e}"))),
            Err(e) => Err(connector_http_error_from(e)),
        }
    }
}

#[derive(Debug, serde::Deserialize)]
struct ConnectorHttpResponseWire {
    status: u16,
    #[serde(default)]
    headers: Vec<(String, String)>,
    #[serde(default)]
    body: Vec<u8>,
    #[serde(default)]
    truncated: bool,
}

impl From<ConnectorHttpResponseWire> for http::Response {
    fn from(w: ConnectorHttpResponseWire) -> Self {
        http::Response {
            status: w.status,
            headers: w
                .headers
                .into_iter()
                .map(|(name, value)| http::Header { name, value })
                .collect(),
            body: w.body,
            truncated: w.truncated,
        }
    }
}

fn connector_http_error_from(err: ExecutorError) -> http::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "denied" => http::Error::Denied(message.clone()),
            "timeout" => http::Error::Timeout,
            "too_large" => http::Error::TooLarge(message.parse().unwrap_or(0)),
            "rate_limited" => http::Error::RateLimited(message.parse().unwrap_or(0)),
            _ => http::Error::Transport(message.clone()),
        },
        other => http::Error::Transport(other.to_string()),
    }
}

impl log::Host for ExecState {
    /// Same posture as the `stage` world's `log::Host::write`
    /// (`crate::host::imports`): sanitized/emitted stage-side (Gemini
    /// condition 6), no WIT error channel, a failed host-call is logged
    /// locally at DEBUG and otherwise swallowed.
    async fn write(&mut self, lvl: log::Level, message: String, fields_json: String) {
        let level_str = match lvl {
            log::Level::Error => "error",
            log::Level::Warn => "warn",
            log::Level::Info => "info",
            log::Level::Debug => "debug",
        };
        let args = serde_json::json!({ "level": level_str, "message": message, "fields_json": fields_json });
        if let Err(e) = call(self, CapabilityKind::Log, "write", args).await {
            debug!(error = %e, "connector log.write host-call failed, dropping this guest log line");
        }
    }
}

impl clock::Host for ExecState {
    async fn now_millis(&mut self) -> u64 {
        match call(
            self,
            CapabilityKind::Clock,
            "now-millis",
            serde_json::json!({}),
        )
        .await
        {
            Ok(value) => value.as_u64().unwrap_or(0),
            Err(e) => {
                warn!(error = %e, "connector clock.now-millis host-call failed, returning 0");
                0
            }
        }
    }

    async fn now_rfc3339(&mut self) -> String {
        match call(
            self,
            CapabilityKind::Clock,
            "now-rfc3339",
            serde_json::json!({}),
        )
        .await
        {
            Ok(value) => value.as_str().map(str::to_string).unwrap_or_default(),
            Err(e) => {
                warn!(error = %e, "connector clock.now-rfc3339 host-call failed, returning empty");
                String::new()
            }
        }
    }

    async fn monotonic_nanos(&mut self) -> u64 {
        match call(
            self,
            CapabilityKind::Clock,
            "monotonic-nanos",
            serde_json::json!({}),
        )
        .await
        {
            Ok(value) => value.as_u64().unwrap_or(0),
            Err(e) => {
                warn!(error = %e, "connector clock.monotonic-nanos host-call failed, returning 0");
                0
            }
        }
    }
}

impl flags::Host for ExecState {
    async fn enabled(&mut self, key: String, default_value: bool) -> bool {
        let args = serde_json::json!({ "key": key, "default_value": default_value });
        match call(self, CapabilityKind::Flags, "enabled", args).await {
            Ok(value) => value
                .get("enabled")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(default_value),
            Err(e) => {
                warn!(error = %e, key, "connector flags.enabled host-call failed, failing open to default");
                default_value
            }
        }
    }

    async fn tier(&mut self) -> String {
        match call(self, CapabilityKind::Flags, "tier", serde_json::json!({})).await {
            Ok(value) => value
                .get("tier")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("free")
                .to_string(),
            Err(e) => {
                warn!(error = %e, "connector flags.tier host-call failed, defaulting to \"free\"");
                "free".to_string()
            }
        }
    }
}

impl identity::Host for ExecState {
    /// Deliberate stub -- see this module's doc comment. Reachable only
    /// when `crate::manifest::VerifiedManifest::may_link_identity` already
    /// passed at `Linker`-build time (spec S3.2.1 gate 2); the RO-replica
    /// read, cache, rate limit, and audit trail (spec S3.3/S3.4) are a
    /// later task in this design's phased rollout, not this one.
    async fn lookup(
        &mut self,
        _key: identity::IdentityKey,
    ) -> Result<identity::IdentityRecord, identity::Error> {
        warn!(
            app_id = %self.app_id,
            "identity.lookup called but the RO-replica backend is not yet wired (spec S3.3/S3.4, deferred to a later phase-1 task)"
        );
        Err(identity::Error::Backend(
            "identity.lookup backend not yet implemented".to_string(),
        ))
    }
}

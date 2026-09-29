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
//! spec S3.3). `identity::Host::lookup` below delegates over the same
//! host-API wire bridge (`crate::host::imports::call`) every other import
//! in this module uses, reusing `CapabilityKind::Db` (spec S3.3's
//! RO-replica-backed lookup is fundamentally a database read) -- there is
//! no `Identity` variant in `penguin-bundle-host`'s `CapabilityKind` enum
//! (external `penguin-libs` dependency), and adding one is a `penguin-libs`
//! change out of this repo's scope. The actual RO-replica query --
//! tenant-scoped, cached (TTL + erasure/rename invalidation), rate-limited
//! per connector digest, audited counts-only against
//! `waddles_connector_pii_reader` (migration
//! `0037_connector_pii_reader_role`) -- is implemented by whichever
//! process's capability handler answers `CapabilityKind::Db`/`"identity.lookup"`
//! (today a `not_implemented` seam for every `Db` call, e.g.
//! `core/svc_process/src/capabilities.rs`); wiring that handler in
//! `svc_ingest`/`svc_action` is the next phase-1 task, not this one. This
//! function's own job -- and what it's tested for -- is delegating
//! faithfully and mapping every wire outcome to the exact WIT
//! `identity::Error` variant spec S1 defines, reachable ONLY when
//! `VerifiedManifest::may_link_identity` already passed
//! (`crate::engine::build_linker_for`).

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
    /// See this module's doc comment: delegates over the standard
    /// `CapabilityKind::Db`/`"identity.lookup"` wire round trip. Reachable
    /// only when `crate::manifest::VerifiedManifest::may_link_identity`
    /// already passed at `Linker`-build time (spec S3.2.1 gate 2) -- a
    /// `stage`/`stage-v1_1` component has no path to this function at all
    /// (the `identity` interface isn't in that world's Linker), and a
    /// `connector`-world component without `connector.pii.read` never gets
    /// it linked either, so every call reaching here already passed both
    /// gates.
    async fn lookup(
        &mut self,
        key: identity::IdentityKey,
    ) -> Result<identity::IdentityRecord, identity::Error> {
        let args = match &key {
            identity::IdentityKey::Uuid(uuid) => {
                serde_json::json!({ "kind": "uuid", "uuid": uuid })
            }
            identity::IdentityKey::PlatformIdentity((platform, platform_user_id)) => {
                serde_json::json!({
                    "kind": "platform-identity",
                    "platform": platform,
                    "platform_user_id": platform_user_id,
                })
            }
        };
        match call(self, CapabilityKind::Db, "identity.lookup", args).await {
            Ok(value) => serde_json::from_value::<IdentityRecordWire>(value)
                .map(Into::into)
                .map_err(|e| identity::Error::Backend(format!("malformed host-result: {e}"))),
            Err(e) => {
                warn!(app_id = %self.app_id, error = %e, "identity.lookup host-call failed");
                Err(connector_identity_error_from(e))
            }
        }
    }
}

#[derive(Debug, serde::Deserialize)]
struct IdentityRecordWire {
    uuid: String,
    linked: bool,
    #[serde(default)]
    handle: Option<String>,
    #[serde(default)]
    display_name: Option<String>,
}

impl From<IdentityRecordWire> for identity::IdentityRecord {
    fn from(w: IdentityRecordWire) -> Self {
        identity::IdentityRecord {
            uuid: w.uuid,
            linked: w.linked,
            handle: w.handle,
            display_name: w.display_name,
        }
    }
}

/// Maps a wire-level [`ExecutorError`] to the exact WIT `identity::Error`
/// variant spec S1 defines -- `not-found` (erased or never existed) and
/// `rate-limited` (per-connector-digest token bucket exhausted, spec S3.4)
/// are distinct, meaningful outcomes a connector bundle must branch on, not
/// collapsed into a generic `backend` error.
fn connector_identity_error_from(err: ExecutorError) -> identity::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "denied" => identity::Error::Denied(message.clone()),
            "not_found" => identity::Error::NotFound,
            "rate_limited" => identity::Error::RateLimited(message.parse().unwrap_or(0)),
            _ => identity::Error::Backend(message.clone()),
        },
        other => identity::Error::Backend(other.to_string()),
    }
}

#[cfg(test)]
mod identity_tests {
    use super::*;

    #[test]
    fn denied_code_maps_to_denied_variant() {
        let err = ExecutorError::HostCallDenied {
            code: "denied".to_string(),
            message: "no grant".to_string(),
            capability: "db",
            op: "identity.lookup",
        };
        assert!(matches!(
            connector_identity_error_from(err),
            identity::Error::Denied(m) if m == "no grant"
        ));
    }

    #[test]
    fn not_found_code_maps_to_not_found_variant() {
        let err = ExecutorError::HostCallDenied {
            code: "not_found".to_string(),
            message: "erased".to_string(),
            capability: "db",
            op: "identity.lookup",
        };
        assert!(matches!(
            connector_identity_error_from(err),
            identity::Error::NotFound
        ));
    }

    #[test]
    fn rate_limited_code_maps_to_rate_limited_variant_with_parsed_retry_after() {
        let err = ExecutorError::HostCallDenied {
            code: "rate_limited".to_string(),
            message: "5".to_string(),
            capability: "db",
            op: "identity.lookup",
        };
        assert!(matches!(
            connector_identity_error_from(err),
            identity::Error::RateLimited(5)
        ));
    }

    #[test]
    fn rate_limited_with_unparseable_message_defaults_to_zero_retry_after() {
        let err = ExecutorError::HostCallDenied {
            code: "rate_limited".to_string(),
            message: "not-a-number".to_string(),
            capability: "db",
            op: "identity.lookup",
        };
        assert!(matches!(
            connector_identity_error_from(err),
            identity::Error::RateLimited(0)
        ));
    }

    #[test]
    fn unknown_code_maps_to_backend_variant() {
        let err = ExecutorError::HostCallDenied {
            code: "something_else".to_string(),
            message: "oops".to_string(),
            capability: "db",
            op: "identity.lookup",
        };
        assert!(matches!(
            connector_identity_error_from(err),
            identity::Error::Backend(m) if m == "oops"
        ));
    }
}

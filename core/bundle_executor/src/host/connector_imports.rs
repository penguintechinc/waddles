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
//! change out of this repo's scope. The actual RO-replica query -- cached
//! (TTL + erasure/rename invalidation), rate-limited per connector digest,
//! audited counts-only against `waddles_connector_pii_reader` (migrations
//! `0044_connector_pii_reader_role` + `0046_connector_pii_tenant_scope`) --
//! is implemented by whichever process's capability handler answers
//! `CapabilityKind::Db`/`"identity.lookup"` (today a `not_implemented` seam
//! for every `Db` call, e.g. `core/svc_process/src/capabilities.rs`); wiring
//! that handler in `svc_ingest`/`svc_action` is the next phase-1 task.
//!
//! **Tenant scoping (SECURITY: PII / tenant isolation) -- three independent,
//! fail-closed layers, because `identity.lookup` is the one capability that
//! returns raw PII (handle, display name) to a guest:**
//!
//! 1. **This module (the executor).** The `identity-key` the guest passes
//!    carries no tenant, and the guest can never supply one. Before any PII
//!    read, [`establish_tenant`] fetches the invoking tenant from the
//!    stage's own host-derived `context`/`get-context` (spec S7.4: "tenant
//!    and community come from the key, never from payload"). If it cannot be
//!    established -- the call fails, or the tenant is missing/empty/absurd --
//!    the lookup is `identity::Error::Denied` and **no `identity.lookup`
//!    host-call is sent at all**. The established tenant is sent as
//!    `args.tenant`, and the reply MUST echo it as `tenant`
//!    ([`verify_tenant_echo`]); a missing or different echo is `Denied` and
//!    the record (and the PII in it) is discarded, never returned. So a
//!    stage handler that answers without tenant scoping cannot leak: its
//!    reply is rejected here.
//! 2. **The stage handler (contract for the next phase-1 task).** It sets
//!    `waddles.tenant_id` from its own `InvokeScope` -- NOT from `args.tenant`
//!    -- treats `args.tenant` only as a cross-check (mismatch -> `denied`),
//!    reads only the tenant-scoped views, and echoes `tenant` in the reply.
//! 3. **The database.** `waddles_connector_pii_reader` has no access to the
//!    raw identity tables; it reads `connector_pii_identities` /
//!    `connector_pii_members`, security-barrier views that return only the
//!    session tenant's rows and raise (SQLSTATE 42501) when
//!    `waddles.tenant_id` is unset or malformed (migration
//!    `0046_connector_pii_tenant_scope`).
//!
//! Logs from this path are PII-free: never the key, handle, display name or
//! platform user id -- only `app_id`, `call_id`, the error, and booleans.
//! This function's job is therefore delegating faithfully under that scope
//! and mapping every wire outcome to the exact WIT `identity::Error` variant
//! spec S1 defines, reachable ONLY when `VerifiedManifest::may_link_identity`
//! already passed (`crate::engine::build_linker_for`).

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

/// Longest tenant string accepted from the stage's `get-context` reply. Real
/// tenant ids are short decimal strings; anything past this is treated as a
/// malformed/hostile reply and denies the lookup rather than being forwarded.
const MAX_TENANT_LEN: usize = 64;

/// Denial text when the invoking tenant cannot be established. Fixed (no
/// interpolation) so nothing guest- or stage-controlled reaches the guest.
const TENANT_NOT_ESTABLISHED: &str = "identity.lookup denied: tenant scope not established";

/// Denial text when a reply is not provably scoped to the invoking tenant.
const REPLY_NOT_TENANT_SCOPED: &str =
    "identity.lookup denied: reply was not scoped to the invoking tenant";

impl identity::Host for ExecState {
    /// See this module's doc comment: delegates over the standard
    /// `CapabilityKind::Db`/`"identity.lookup"` wire round trip, **fail-closed
    /// on tenant scope** -- the invoking tenant is established first
    /// ([`establish_tenant`]; no host-call is sent without it), sent as
    /// `args.tenant`, and the reply must echo it ([`verify_tenant_echo`]).
    /// Reachable only when `crate::manifest::VerifiedManifest::may_link_identity`
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
        let tenant = establish_tenant(self).await?;
        let args = match &key {
            identity::IdentityKey::Uuid(uuid) => {
                serde_json::json!({ "kind": "uuid", "uuid": uuid, "tenant": tenant })
            }
            identity::IdentityKey::PlatformIdentity((platform, platform_user_id)) => {
                serde_json::json!({
                    "kind": "platform-identity",
                    "platform": platform,
                    "platform_user_id": platform_user_id,
                    "tenant": tenant,
                })
            }
        };
        match call(self, CapabilityKind::Db, "identity.lookup", args).await {
            Ok(value) => {
                let wire = serde_json::from_value::<IdentityRecordWire>(value)
                    .map_err(|e| identity::Error::Backend(format!("malformed host-result: {e}")))?;
                // Checked BEFORE the wire record becomes an `IdentityRecord`:
                // an unscoped reply is dropped here, PII and all.
                if let Err(denied) = verify_tenant_echo(&tenant, wire.tenant.as_deref()) {
                    warn!(
                        app_id = %self.app_id,
                        call_id = self.call_id,
                        reply_had_tenant = wire.tenant.is_some(),
                        "identity.lookup reply rejected: not scoped to the invoking tenant"
                    );
                    return Err(denied);
                }
                Ok(wire.into())
            }
            Err(e) => {
                warn!(app_id = %self.app_id, error = %e, "identity.lookup host-call failed");
                Err(connector_identity_error_from(e))
            }
        }
    }
}

/// Establishes the tenant this invocation runs under, or denies.
///
/// The tenant comes from the stage's `context`/`get-context` -- derived by
/// the stage from the invoke key, never from anything the guest supplies --
/// and is cached on [`ExecState::identity_tenant`] for the rest of the
/// invocation. Any failure (host-call error, missing/empty/non-string/over-
/// long tenant) is `identity::Error::Denied`: deny-by-default, so a lookup
/// can never proceed with an unknown tenant.
async fn establish_tenant(state: &mut ExecState) -> Result<String, identity::Error> {
    if let Some(tenant) = &state.identity_tenant {
        return Ok(tenant.clone());
    }
    let value = match call(
        state,
        CapabilityKind::Context,
        "get-context",
        serde_json::json!({}),
    )
    .await
    {
        Ok(value) => value,
        Err(e) => {
            warn!(
                app_id = %state.app_id,
                call_id = state.call_id,
                error = %e,
                "identity.lookup denied: tenant context host-call failed"
            );
            return Err(identity::Error::Denied(TENANT_NOT_ESTABLISHED.to_string()));
        }
    };
    match parse_tenant(&value) {
        Some(tenant) => {
            state.identity_tenant = Some(tenant.clone());
            Ok(tenant)
        }
        None => {
            warn!(
                app_id = %state.app_id,
                call_id = state.call_id,
                "identity.lookup denied: context reply carried no usable tenant"
            );
            Err(identity::Error::Denied(TENANT_NOT_ESTABLISHED.to_string()))
        }
    }
}

/// Extracts a usable tenant from a `get-context` reply: a non-empty (after
/// trimming), bounded JSON string. Anything else -- absent, `null`, a
/// number, an empty/blank or over-long string -- is `None`.
fn parse_tenant(context: &serde_json::Value) -> Option<String> {
    let tenant = context.get("tenant")?.as_str()?.trim();
    if tenant.is_empty() || tenant.len() > MAX_TENANT_LEN {
        return None;
    }
    Some(tenant.to_string())
}

/// Requires the stage's reply to echo exactly the tenant this lookup was
/// issued under. A missing echo is as fatal as a different one: it means the
/// handler did not scope the read, and the reply is rejected.
fn verify_tenant_echo(expected: &str, reply: Option<&str>) -> Result<(), identity::Error> {
    match reply {
        Some(echoed) if echoed == expected => Ok(()),
        _ => Err(identity::Error::Denied(REPLY_NOT_TENANT_SCOPED.to_string())),
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
    /// The tenant the stage scoped this read to; must equal the invoking
    /// tenant ([`verify_tenant_echo`]). Never converted into the guest-visible
    /// record.
    #[serde(default)]
    tenant: Option<String>,
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

/// SECURITY REVIEW (PII / tenant isolation): `identity.lookup` must be
/// fail-closed on tenant scope. Every test here drives the real `lookup` over
/// a real wire round trip to a scripted fake stage (never a fabricated return
/// value, `rules/general.md`: "never fake a host call") and asserts both the
/// outcome and exactly which host-calls were -- or were NOT -- sent.
#[cfg(test)]
mod identity_scope_tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::host::HostBridge;
    use penguin_bundle_host::wire::{
        read_frame, write_frame, Frame, HostCallBody, HostResultBody, HostResultError, Message,
    };
    use std::sync::{Arc, Mutex};
    use tokio::sync::mpsc;

    type Outcome = Result<serde_json::Value, HostResultError>;
    type Responder = Box<dyn Fn(&HostCallBody) -> Outcome + Send + Sync>;
    type Recorded = Arc<Mutex<Vec<HostCallBody>>>;

    /// Builds a real [`HostBridge`] wired to a fake stage that answers EVERY
    /// `host-call` with `responder(body)` and records each body it saw, in
    /// order -- so a test can assert that a call was never even sent.
    fn scripted_bridge(responder: Responder) -> (Arc<HostBridge>, Recorded) {
        let (exec_io, stage_io) = tokio::io::duplex(64 * 1024);
        let (mut exec_reader, mut exec_writer) = tokio::io::split(exec_io);
        let (writer_tx, mut writer_rx) = mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(writer_tx);

        tokio::spawn(async move {
            while let Some(frame) = writer_rx.recv().await {
                if write_frame(&mut exec_writer, &frame).await.is_err() {
                    break;
                }
            }
        });

        let connection_for_reader = Arc::clone(&connection);
        tokio::spawn(async move {
            while let Ok(frame) = read_frame(&mut exec_reader).await {
                let _ = connection_for_reader.deliver(frame);
            }
        });

        let recorded: Recorded = Arc::new(Mutex::new(Vec::new()));
        let recorded_for_stage = Arc::clone(&recorded);
        tokio::spawn(async move {
            let mut io = stage_io;
            while let Ok(frame) = read_frame(&mut io).await {
                let Message::HostCall(body) = frame.message else {
                    continue;
                };
                recorded_for_stage.lock().unwrap().push(body.clone());
                let reply = match responder(&body) {
                    Ok(v) => HostResultBody {
                        result: Some(v),
                        error: None,
                    },
                    Err(e) => HostResultBody {
                        result: None,
                        error: Some(e),
                    },
                };
                if write_frame(&mut io, &Frame::new(frame.id, Message::HostResult(reply)))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        });

        (HostBridge::new(connection), recorded)
    }

    fn denied(code: &str, message: &str) -> HostResultError {
        HostResultError {
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    /// A stage whose `get-context` reports `context_tenant` and whose
    /// `identity.lookup` replies with a record echoing `reply_tenant`
    /// (`None` = the reply carries no `tenant` field at all).
    fn stage_for(context: Outcome, reply_tenant: Option<&'static str>) -> Responder {
        Box::new(move |body| match body.capability {
            CapabilityKind::Context => context.clone(),
            CapabilityKind::Db => {
                let mut record = serde_json::json!({
                    "uuid": "11111111-1111-1111-1111-111111111111",
                    "linked": true,
                    "handle": "secret-handle#0001",
                    "display_name": "Secret Display Name",
                });
                if let Some(t) = reply_tenant {
                    record["tenant"] = serde_json::json!(t);
                }
                Ok(record)
            }
            _ => Err(denied("unknown_op", "unexpected capability")),
        })
    }

    fn state_with(bridge: Arc<HostBridge>) -> ExecState {
        ExecState::new(Some(bridge), "waddles.core.connector.test".to_string(), 7)
    }

    fn uuid_key() -> identity::IdentityKey {
        identity::IdentityKey::Uuid("11111111-1111-1111-1111-111111111111".to_string())
    }

    fn tenant_ctx(tenant: &str) -> Outcome {
        Ok(serde_json::json!({ "tenant": tenant, "community": null, "app_id": "x" }))
    }

    fn db_calls(recorded: &Recorded) -> usize {
        recorded
            .lock()
            .unwrap()
            .iter()
            .filter(|b| b.capability == CapabilityKind::Db)
            .count()
    }

    fn context_calls(recorded: &Recorded) -> usize {
        recorded
            .lock()
            .unwrap()
            .iter()
            .filter(|b| b.capability == CapabilityKind::Context)
            .count()
    }

    #[tokio::test]
    async fn lookup_with_established_tenant_sends_it_and_accepts_the_matching_echo() {
        let (bridge, recorded) = scripted_bridge(stage_for(tenant_ctx("7"), Some("7")));
        let mut state = state_with(bridge);

        let record = identity::Host::lookup(&mut state, uuid_key())
            .await
            .expect("matching tenant echo is accepted");

        assert_eq!(record.handle.as_deref(), Some("secret-handle#0001"));
        assert_eq!(record.display_name.as_deref(), Some("Secret Display Name"));
        let calls = recorded.lock().unwrap();
        assert_eq!(calls.len(), 2, "exactly: get-context, then identity.lookup");
        assert_eq!(calls[0].capability, CapabilityKind::Context);
        assert_eq!(calls[0].op, "get-context");
        assert_eq!(calls[1].capability, CapabilityKind::Db);
        assert_eq!(calls[1].op, "identity.lookup");
        assert_eq!(calls[1].args["tenant"], "7");
        assert_eq!(calls[1].args["kind"], "uuid");
    }

    #[tokio::test]
    async fn platform_identity_lookup_also_carries_the_tenant() {
        let (bridge, recorded) = scripted_bridge(stage_for(tenant_ctx("12"), Some("12")));
        let mut state = state_with(bridge);

        identity::Host::lookup(
            &mut state,
            identity::IdentityKey::PlatformIdentity(("discord".into(), "d-123".into())),
        )
        .await
        .expect("scoped platform-identity lookup");

        let calls = recorded.lock().unwrap();
        assert_eq!(calls[1].args["kind"], "platform-identity");
        assert_eq!(calls[1].args["platform"], "discord");
        assert_eq!(calls[1].args["tenant"], "12");
    }

    /// Cross-tenant denial: a handler that read ANOTHER tenant's row (a stage
    /// bug, a mis-set `waddles.tenant_id`) is rejected here and none of the
    /// PII in that reply reaches the guest.
    #[tokio::test]
    async fn reply_scoped_to_a_different_tenant_is_denied_and_leaks_nothing() {
        let (bridge, recorded) = scripted_bridge(stage_for(tenant_ctx("7"), Some("8")));
        let mut state = state_with(bridge);

        let err = identity::Host::lookup(&mut state, uuid_key())
            .await
            .expect_err("a cross-tenant reply must be denied");

        match err {
            identity::Error::Denied(message) => {
                assert_eq!(message, REPLY_NOT_TENANT_SCOPED);
                assert!(!message.contains("secret-handle"));
                assert!(!message.contains("Secret Display Name"));
            }
            other => panic!("expected Denied, got {other:?}"),
        }
        assert_eq!(
            db_calls(&recorded),
            1,
            "the call was sent; the reply was refused"
        );
    }

    #[tokio::test]
    async fn reply_without_a_tenant_echo_is_denied() {
        let (bridge, _recorded) = scripted_bridge(stage_for(tenant_ctx("7"), None));
        let mut state = state_with(bridge);

        let err = identity::Host::lookup(&mut state, uuid_key())
            .await
            .expect_err("an unscoped reply must be denied");

        assert!(matches!(err, identity::Error::Denied(m) if m == REPLY_NOT_TENANT_SCOPED));
    }

    /// Fail-closed when the tenant cannot be established: the lookup is
    /// denied AND no `identity.lookup` host-call is sent at all.
    #[tokio::test]
    async fn lookup_is_denied_without_sending_a_db_call_when_context_host_call_fails() {
        let (bridge, recorded) = scripted_bridge(stage_for(
            Err(denied("not_granted", "platform.context not granted")),
            Some("7"),
        ));
        let mut state = state_with(bridge);

        let err = identity::Host::lookup(&mut state, uuid_key())
            .await
            .expect_err("no tenant, no lookup");

        assert!(matches!(err, identity::Error::Denied(m) if m == TENANT_NOT_ESTABLISHED));
        assert_eq!(db_calls(&recorded), 0, "identity.lookup must never be sent");
        assert_eq!(context_calls(&recorded), 1);
    }

    #[tokio::test]
    async fn lookup_is_denied_without_a_db_call_for_every_unusable_tenant() {
        let too_long = "9".repeat(MAX_TENANT_LEN + 1);
        let unusable: Vec<serde_json::Value> = vec![
            serde_json::json!({}),
            serde_json::json!({ "tenant": null }),
            serde_json::json!({ "tenant": "" }),
            serde_json::json!({ "tenant": "   " }),
            serde_json::json!({ "tenant": 7 }),
            serde_json::json!({ "tenant": ["7"] }),
            serde_json::json!({ "tenant": too_long }),
            serde_json::json!("not-an-object"),
        ];
        for context in unusable {
            let (bridge, recorded) = scripted_bridge(stage_for(Ok(context.clone()), Some("7")));
            let mut state = state_with(bridge);

            let err = identity::Host::lookup(&mut state, uuid_key())
                .await
                .expect_err("unusable tenant must deny");

            assert!(
                matches!(&err, identity::Error::Denied(m) if m == TENANT_NOT_ESTABLISHED),
                "context {context}: got {err:?}"
            );
            assert_eq!(db_calls(&recorded), 0, "context {context}: no db call");
        }
    }

    #[tokio::test]
    async fn tenant_is_established_once_per_invocation_not_per_lookup() {
        let (bridge, recorded) = scripted_bridge(stage_for(tenant_ctx("7"), Some("7")));
        let mut state = state_with(bridge);

        for _ in 0..3 {
            identity::Host::lookup(&mut state, uuid_key())
                .await
                .expect("scoped lookup");
        }

        assert_eq!(context_calls(&recorded), 1);
        assert_eq!(db_calls(&recorded), 3);
    }

    #[tokio::test]
    async fn a_failed_tenant_lookup_is_not_cached_as_established() {
        let (bridge, recorded) = scripted_bridge(stage_for(Ok(serde_json::json!({})), Some("7")));
        let mut state = state_with(bridge);

        for _ in 0..2 {
            assert!(identity::Host::lookup(&mut state, uuid_key())
                .await
                .is_err());
        }

        assert_eq!(
            context_calls(&recorded),
            2,
            "denial must be re-evaluated, not cached"
        );
        assert_eq!(db_calls(&recorded), 0);
        assert!(state.identity_tenant.is_none());
    }

    /// The tenant sent on the wire is the stage-established one, never
    /// anything derived from the guest-controlled key.
    #[tokio::test]
    async fn tenant_never_comes_from_the_guest_supplied_key() {
        let (bridge, recorded) = scripted_bridge(stage_for(tenant_ctx("7"), Some("7")));
        let mut state = state_with(bridge);

        identity::Host::lookup(
            &mut state,
            identity::IdentityKey::PlatformIdentity((
                "discord".into(),
                r#"x","tenant":"999"#.into(),
            )),
        )
        .await
        .expect("scoped lookup");

        let calls = recorded.lock().unwrap();
        assert_eq!(calls[1].args["tenant"], "7");
        assert_eq!(calls[1].args["platform_user_id"], r#"x","tenant":"999"#);
    }

    #[tokio::test]
    async fn stage_errors_still_map_to_their_wit_variants_under_a_scoped_tenant() {
        let responder: Responder = Box::new(|body| match body.capability {
            CapabilityKind::Context => tenant_ctx("7"),
            _ => Err(denied("not_found", "erased")),
        });
        let (bridge, _recorded) = scripted_bridge(responder);
        let mut state = state_with(bridge);

        let err = identity::Host::lookup(&mut state, uuid_key())
            .await
            .expect_err("stage said not_found");

        assert!(matches!(err, identity::Error::NotFound));
    }

    #[tokio::test]
    async fn lookup_without_a_bridge_fails_closed() {
        let mut state = ExecState::new(None, "waddles.core.connector.test".to_string(), 7);

        let err = identity::Host::lookup(&mut state, uuid_key())
            .await
            .expect_err("no connection, no tenant, no lookup");

        assert!(matches!(err, identity::Error::Denied(m) if m == TENANT_NOT_ESTABLISHED));
    }

    #[test]
    fn parse_tenant_trims_and_bounds() {
        assert_eq!(
            parse_tenant(&serde_json::json!({ "tenant": "  42 " })).as_deref(),
            Some("42")
        );
        assert!(
            parse_tenant(&serde_json::json!({ "tenant": "9".repeat(MAX_TENANT_LEN) })).is_some()
        );
        assert!(
            parse_tenant(&serde_json::json!({ "tenant": "9".repeat(MAX_TENANT_LEN + 1) }))
                .is_none()
        );
    }

    #[test]
    fn verify_tenant_echo_requires_an_exact_match() {
        assert!(verify_tenant_echo("7", Some("7")).is_ok());
        for bad in [None, Some("8"), Some(""), Some("7 "), Some("07")] {
            assert!(
                matches!(
                    verify_tenant_echo("7", bad),
                    Err(identity::Error::Denied(_))
                ),
                "echo {bad:?} must be rejected"
            );
        }
    }
}

//! Host capability implementations serviced on the stage side of the
//! host-API connection (spec §7.4): the executor issues a `host-call`
//! frame naming a `capability`/`op`, and this module answers it, replying
//! `host-result`.
//!
//! **Per-invoke scoping (the corrected design -- see `crate::host_api`'s
//! module doc for the full rationale).** `core/svc_action`'s landed M3
//! `StageCapabilities` is constructed once per host-API *connection* with
//! a fixed `(tenant, community, app_id)`, which is wrong the moment a
//! single connection ever serves more than one activation (the
//! then-latent bug the security review flagged). This module's
//! [`StageCapabilities`] is instead constructed **fresh for every
//! `invoke`**, scoped to the specific envelope `crate::execute` is
//! currently servicing, and handed to `crate::host_api::Connection::invoke`
//! so only host-calls carrying *that* invoke's `call_id` are ever answered
//! by it (spec §5.11: "No bundle host call accepts a tenant or community
//! argument at all" -- every one is scope-implicit, taken from the
//! invocation in flight, never from a connection-lifetime default).
//!
//! This landing wires `context`/`clock`/`log` fully (the three
//! capabilities every bundle needs regardless of manifest declarations,
//! spec §6.5's capability table: "Always granted"), plus `http` -- see
//! [`StageCapabilities::egress`]/[`HttpEgressCatalog`]'s docs for the
//! shared `bundle_host_http::egress::EgressGuard` pipeline (extracted from
//! `core/svc_action`, PR #459 follow-up) and the interim
//! capability-gate seam this stage's own catalog stands in for ahead of
//! PR #433's standard `core/bundle_capability_gate::authorize` landing.
//! `db`/`kv`/`flags` remain documented seams -- see
//! [`StageCapabilities::handle`]'s match arms -- since a real `db` wiring
//! needs the manifest's `data.tables` allowlist plus per-bundle-role RLS
//! (`SET LOCAL waddles.tenant`/`waddles.community`, spec §7.4/§11.10) and
//! `kv` needs a live Valkey connection keyed by `Scope::state_key`,
//! neither of which this landing's scope covers. `relay` is never granted
//! to a process-stage bundle at all (spec §6.5: "Capability: granted only
//! to action-stage bundles") and is denied unconditionally, not merely
//! unimplemented.
//!
//! A bundle never holds a platform credential or a tenant/community
//! argument (spec §4.3, §5.11): every capability here resolves its own
//! scope from `self`, never from the `args`/`op` the guest supplied.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, RwLock};

use bundle_host_db::{
    CapabilitySnapshot as DbCapabilitySnapshot, DbError, DbHost, DbScope, DbValue, PostgresBackend,
    SchemaCache,
};
use bundle_host_http::egress::{EgressGuard, EgressRuleRow, EgressRuleSource};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};

use crate::license::FeatureGate;

/// Answers one `host-call` for a given `capability`/`op`. Object-safe (a
/// manually-boxed future rather than `async fn` in a trait) so the host-API
/// connection can hold `Arc<dyn CapabilityHandler>` without an `async_trait`
/// dependency. Identical shape to `core/svc_action::capabilities::
/// CapabilityHandler` -- kept as a separate trait per crate rather than a
/// shared one in `penguin-bundle-host`, since the two stages' capability
/// sets differ (spec §6.5's grant table).
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

/// This crate's own sanity bound on a bundle's `log` host-call message
/// length -- same rationale and value as `core/svc_action::capabilities::
/// MAX_BUNDLE_LOG_MESSAGE_LEN`: the spec (§5.12/§7.4) does not set one, so
/// this caps unbounded memory/log-volume from a single `host-call`.
const MAX_BUNDLE_LOG_MESSAGE_LEN: usize = 4096;

/// Strips CR/LF and every other control character -- the same
/// CRLF-injection defense `core/svc_action::capabilities::
/// sanitize_irc_component` applies, reused here for a bundle-supplied log
/// message (process-stage bundles have no `relay` capability to sanitize
/// for, but a log message is still guest-controlled text reaching this
/// process's own log stream).
fn strip_control_chars(value: &str) -> String {
    value.chars().filter(|c| !c.is_control()).collect()
}

/// Sanitizes a bundle-supplied `log` host-call `message` before it ever
/// reaches `tracing` (spec §7.4: "`fields-json` is sanitized with the
/// `penguin-logging` `SENSITIVE_KEYS` rule before anything is emitted").
/// Byte-for-byte the same three-defense pipeline as
/// `core/svc_action::capabilities::sanitize_bundle_log_message`: PII
/// redaction, control-character stripping, then length truncation.
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

    let mut message = strip_control_chars(sanitized_message);
    if message.chars().count() > MAX_BUNDLE_LOG_MESSAGE_LEN {
        message = message.chars().take(MAX_BUNDLE_LOG_MESSAGE_LEN).collect();
    }
    message
}

/// Everything `handle_db` needs, shared across every invocation this
/// process serves -- constructed once at startup (never per-invoke, unlike
/// [`StageCapabilities`] itself) and cloned cheaply into each one via
/// `Arc` (`crate::spine`'s call site). `schemas`/`capabilities` are
/// refreshed by this service's own `bundle_loader` poll (not wired in this
/// landing -- see `bundle_host_db`'s crate doc "remaining work"); an
/// `app_id` neither has ever heard of fails closed by construction
/// ([`DbHost`]'s own resolve-then-authorize order).
#[derive(Clone)]
pub struct DbWiring {
    pub host: Arc<DbHost<PostgresBackend>>,
    pub schemas: Arc<SchemaCache>,
    pub capabilities: Arc<DbCapabilitySnapshot>,
    /// `crate::license::BUNDLE_DB_CAPABILITY_FLAG` gate -- OFF denies every
    /// `db` call `feature_disabled` before any schema/authorize lookup.
    pub flag: Arc<dyn FeatureGate>,
}

/// Per-`app_id` egress-allowlist source for this stage's `http` capability
/// -- the interim capability-gate seam (`docs/superpowers/specs/
/// 2026-09-28-bundle-permissions-and-capability-gate.md`, PR #419/#433).
/// `core/bundle_capability_gate::authorize(scope, permission, resource)`
/// (PR #433) is the standard, install-time permission gate this will be
/// replaced by once it lands; until then, this snapshot is this stage's
/// own copy of the same interim pattern PR #425 (`core/bundle_host_kv`)
/// used ahead of the standard gate: **undeclared means denied**. An
/// `app_id` this snapshot has never been told about (every app, today --
/// no writer populates it yet, same honest gap `crate::capabilities`'s own
/// module doc already documents for `db`/`kv`/`flags`) resolves to `None`,
/// which [`EgressGuard`] treats identically to a declared-but-empty
/// `egress` list: every `http.send` call is refused `host_not_declared`.
/// [`HttpEgressCatalog::update`] is the write side a future DB-driven
/// active-set loader (mirroring `svc_action::bundle_loader`) will call
/// once this stage grows one -- not wired to any real data source in this
/// landing.
#[derive(Default)]
pub struct HttpEgressCatalog(RwLock<HashMap<String, EgressRuleRow>>);

impl HttpEgressCatalog {
    pub fn new() -> Arc<Self> {
        Arc::new(Self::default())
    }

    /// Replaces `app_id`'s declared `net.http:<host>` egress allowlist
    /// wholesale (never merges) -- see the type doc for the future writer
    /// this is built for.
    pub fn update(&self, app_id: impl Into<String>, row: EgressRuleRow) {
        self.0
            .write()
            .unwrap_or_else(|e| e.into_inner())
            .insert(app_id.into(), row);
    }
}

impl EgressRuleSource for HttpEgressCatalog {
    fn resolve(&self, app_id: &str) -> Option<EgressRuleRow> {
        self.0
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .get(app_id)
            .cloned()
    }
}

/// The real capability implementation this stage wires today, scoped to
/// exactly one invocation's `(tenant, community, app_id)` -- see the
/// module doc for why this is constructed per-invoke, never per-connection.
/// [`egress`] is the one exception to "everything scope-implicit, nothing
/// shared" -- it is a per-*process* singleton (owns rate-limit token
/// buckets keyed by `app_id` across every invoke, spec §8.2 step 8), built
/// once by this stage's own startup wiring and cloned (cheap, `Arc`) into
/// every per-invoke `StageCapabilities`.
pub struct StageCapabilities {
    tenant: String,
    community: Option<String>,
    app_id: String,
    /// `None` until `crate::lib`'s startup wiring provisions a live
    /// Postgres pool + flag client -- every `db` call denies
    /// `feature_disabled` in that state, never panics (mirrors every other
    /// unimplemented-seam capability's fail-closed default in
    /// [`Self::handle`]).
    db: Option<DbWiring>,
    egress: Arc<EgressGuard>,
}

impl StageCapabilities {
    /// Builds the capability set for exactly one `invoke` -- `context` and
    /// every other capability are scope-implicit (spec §5.11: "No bundle
    /// host call accepts a tenant or community argument at all"). `egress`
    /// is the shared, per-process [`EgressGuard`] -- see the struct doc.
    pub fn new(
        tenant: String,
        community: Option<String>,
        app_id: String,
        egress: Arc<EgressGuard>,
    ) -> Self {
        Self {
            tenant,
            community,
            app_id,
            db: None,
            egress,
        }
    }

    /// Attaches the `db` capability's live wiring -- called once at
    /// startup (`crate::lib`) with the shared, process-wide [`DbWiring`],
    /// never per-invoke. Builder-style so existing `StageCapabilities::new`
    /// call sites (including every current test) are unaffected.
    pub fn with_db(mut self, db: DbWiring) -> Self {
        self.db = Some(db);
        self
    }

    async fn handle_http(
        &self,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostResultError> {
        self.egress.send(&self.app_id, args).await
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
            "now-rfc3339" => Ok(serde_json::json!(
                chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
            )),
            "monotonic-nanos" => {
                // No process-wide monotonic epoch exists to subtract
                // against; a bundle only ever diffs two calls of its own,
                // for which an arbitrary fixed origin is sufficient and
                // matches `std::time::Instant`'s own "opaque, comparable
                // only to itself" contract.
                static ORIGIN: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();
                let origin = *ORIGIN.get_or_init(std::time::Instant::now);
                Ok(serde_json::json!(origin.elapsed().as_nanos() as u64))
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
        let raw_message = args
            .get("message")
            .and_then(|v| v.as_str())
            .unwrap_or("<empty>");
        let message = sanitize_bundle_log_message(raw_message);
        let message = message.as_str();

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

    /// Structured insert/get/update/delete against this app's own
    /// `app_core`/`app_community` table (design doc
    /// `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md`).
    /// `query` is not implemented in this landing -- returns `not_implemented`
    /// (see `bundle_host_db`'s crate doc "remaining work").
    ///
    /// Op/args shape (host-API `{capability, op, args}` dispatch, ahead of
    /// a corresponding `stage.wit`/`bundle_executor` change -- see this
    /// PR's description for the proposed WIT shape):
    /// - `insert`: `args.column_values` = `{col: value, ...}` -> `{row_id, version, columns}`
    /// - `get`: `args.row_id` (string) -> `{row_id, version, columns}`
    /// - `update`: `args.row_id`, `args.expected_version` (u64), `args.column_values` -> `{row_id, version, columns}`
    /// - `delete`: `args.row_id`, `args.expected_version` (u64) -> `{}`
    /// - `query`: `args.limit`/`args.offset` (both optional u32, clamped to
    ///   `MAX_QUERY_LIMIT`) -> `{rows: [{row_id, version, columns}, ...]}`
    ///   -- host-side op ready for the proposed WIT shape, not yet
    ///   guest-reachable in this landing (see this PR's description)
    ///
    /// Every value is a JSON scalar (`null`/bool/number/string); `bytes`
    /// values are not supported over this JSON args shape in this landing.
    async fn handle_db(&self, call: &HostCallBody) -> Result<serde_json::Value, HostResultError> {
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

        let scope = DbScope::new(
            self.tenant.clone(),
            self.community.clone(),
            self.app_id.clone(),
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
                    .insert(&scope, &db.schemas, &db.capabilities, values)
                    .await
            }
            "get" => {
                let id = row_id(&call.args)?;
                db.host
                    .get(&scope, &db.schemas, &db.capabilities, &id)
                    .await
            }
            "update" => {
                let id = row_id(&call.args)?;
                let version = expected_version(&call.args)?;
                let values = column_values(&call.args)?;
                db.host
                    .update(&scope, &db.schemas, &db.capabilities, &id, version, values)
                    .await
            }
            "delete" => {
                let id = row_id(&call.args)?;
                let version = expected_version(&call.args)?;
                return db
                    .host
                    .delete(&scope, &db.schemas, &db.capabilities, &id, version)
                    .await
                    .map(|()| serde_json::json!({}))
                    .map_err(db_error_to_host_error);
            }
            "query" => {
                // Host-side op ready for the proposed `query` WIT shape
                // (this crate's PR description) -- not yet reachable from
                // a guest bundle's `stage.wit` bindings in this landing
                // (no WIT/`bundle_executor` change here), but already
                // dispatchable at this untyped `{capability, op, args}`
                // layer the same way insert/get/update/delete are.
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
                return db
                    .host
                    .query(&scope, &db.schemas, &db.capabilities, limit, offset)
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

/// Maps [`DbError`] to the `{code, message}` shape the executor forwards
/// to the guest -- never leaks a raw SQL error string beyond `DbError`'s
/// own already-sanitized `Backend` variant (no row data or PII in any
/// `DbError` variant to begin with, `bundle_host_db`'s own doc).
fn db_error_to_host_error(err: DbError) -> HostResultError {
    denied(err.code(), err.to_string())
}

impl CapabilityHandler for StageCapabilities {
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            match call.capability {
                CapabilityKind::Clock => self.handle_clock(&call.op),
                CapabilityKind::Context => self.handle_context(),
                CapabilityKind::Log => self.handle_log(&call.args),
                // `http` is wired to the shared `bundle_host_http::egress::
                // EgressGuard` (`self.egress`, see the struct doc) --
                // `db` (SS7.4's SQL-parser-gated statement execution
                // against the manifest's `data.tables` allowlist,
                // RLS-scoped via `SET LOCAL waddles.tenant`/
                // `waddles.community`) and `kv` (the bundle's own
                // `…:state` hash, `penguin_spine::Scope::state_key`) remain
                // documented seams -- denying (never silently succeeding)
                // is the correct behavior for an unimplemented capability:
                // a bundle calling it sees `access-denied`, not a
                // fabricated success.
                CapabilityKind::Http => self.handle_http(&call.args).await,
                CapabilityKind::Db => self.handle_db(&call).await,
                CapabilityKind::Kv => Err(denied(
                    "not_implemented",
                    "kv capability is not wired in this build -- TODO(M4+)",
                )),
                CapabilityKind::Flags => Err(denied(
                    "not_implemented",
                    "flags capability is not wired in this build -- TODO(M4+)",
                )),
                // Spec §6.5: "Capability: granted only to action-stage
                // bundles" -- never granted to a process-stage bundle at
                // all, so this is a permanent denial, not a seam.
                CapabilityKind::Relay => Err(denied(
                    "not_granted",
                    "relay is an action-stage-only capability, never granted to a process-stage bundle",
                )),
            }
        })
    }
}

/// A [`CapabilityHandler`] that denies every call -- the fallback answer
/// for a `host-call` whose `call_id` matches no in-flight invoke (see
/// `crate::host_api::Connection`'s per-invoke scope registry), and for
/// tests exercising the host-API connection layer in isolation from
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
                format!(
                    "{:?} capability has no in-flight invoke scope for this call_id",
                    call.capability
                ),
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
    use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
    use std::time::Duration;

    fn test_egress_metrics() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_svc_process_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap()
    }

    fn test_egress_limits() -> bundle_host_http::egress::EgressLimits {
        bundle_host_http::egress::EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        }
    }

    /// The default fixture: an empty [`HttpEgressCatalog`] -- every
    /// `app_id` is undeclared, so `http` denies every call
    /// `host_not_declared` (this stage's deny-by-default posture, see
    /// [`HttpEgressCatalog`]'s doc). Tests exercising a granted call build
    /// their own guard via [`egress_guard_with`] instead.
    fn caps() -> StageCapabilities {
        let egress = Arc::new(EgressGuard::new(
            Arc::new(ReqwestTransport::new()),
            test_egress_limits(),
            HttpEgressCatalog::new(),
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            egress,
        )
    }

    /// A [`StageCapabilities`] whose `egress` catalog declares exactly one
    /// `(host, methods)` entry for `waddles.bot.commands.default` -- the
    /// interim capability-gate "declared" case ([`HttpEgressCatalog`]'s
    /// doc).
    fn caps_with_egress_rule(host: &str, methods: &[&str]) -> StageCapabilities {
        let catalog = HttpEgressCatalog::new();
        catalog.update(
            "waddles.bot.commands.default",
            EgressRuleRow::from_legacy_patterns(
                vec![(
                    host.to_string(),
                    methods.iter().map(|m| m.to_string()).collect(),
                )],
                None,
                HashMap::new(),
            ),
        );
        let egress = Arc::new(EgressGuard::new(
            Arc::new(ReqwestTransport::new()),
            test_egress_limits(),
            catalog,
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            egress,
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

    fn mock_db_wiring(flag_on: bool) -> DbWiring {
        let conn = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres).into_connection();
        DbWiring {
            host: Arc::new(DbHost::new(bundle_host_db::PostgresBackend::new(conn))),
            schemas: Arc::new(SchemaCache::new()),
            capabilities: Arc::new(DbCapabilitySnapshot::new()),
            flag: Arc::new(crate::license::test_support::FixedGate(flag_on)),
        }
    }

    #[tokio::test]
    async fn db_capability_denies_not_implemented_when_never_wired() {
        let capabilities = caps();
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "x"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    #[tokio::test]
    async fn db_capability_denies_feature_disabled_when_flag_is_off() {
        let capabilities = caps().with_db(mock_db_wiring(false));
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "x"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "feature_disabled");
    }

    #[tokio::test]
    async fn db_capability_denies_no_table_when_flag_on_but_unprovisioned() {
        let capabilities = caps().with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "get",
                serde_json::json!({"row_id": "00000000-0000-0000-0000-000000000000"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[tokio::test]
    async fn db_capability_rejects_an_unknown_op() {
        let capabilities = caps().with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(&call(CapabilityKind::Db, "truncate", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn db_capability_rejects_missing_row_id_for_get() {
        let capabilities = caps().with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(&call(CapabilityKind::Db, "get", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn db_capability_query_op_denies_no_table_when_unprovisioned() {
        // `query` is dispatched the same as insert/get/update/delete at
        // this untyped op layer -- proves the new arm actually reaches
        // `DbHost::query` (resolve-then-authorize denies `no_table` here,
        // same as every other op against an unprovisioned schema) rather
        // than falling through to `unknown_op`.
        let capabilities = caps().with_db(mock_db_wiring(true));
        capabilities.db.as_ref().unwrap().capabilities.update(
            "waddles.bot.commands.default",
            ["storage.tables".to_string()],
        );
        let err = capabilities
            .handle_db(&call(
                CapabilityKind::Db,
                "query",
                serde_json::json!({"limit": 10, "offset": 0}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "no_table");
    }

    #[tokio::test]
    async fn db_capability_query_op_defaults_limit_and_offset_when_omitted() {
        // No `limit`/`offset` in args must not be `invalid_args` -- both
        // are optional, defaulting to MAX_QUERY_LIMIT/0 respectively.
        let capabilities = caps().with_db(mock_db_wiring(true));
        let err = capabilities
            .handle_db(&call(CapabilityKind::Db, "query", serde_json::json!({})))
            .await
            .unwrap_err();
        // Not `invalid_args` -- both args are optional and were accepted;
        // this app simply never declared `storage.tables`
        // (`DbHost::resolve_and_authorize` maps a denied authorize() to
        // `DbError::InvalidColumn`, whose `code()` is `"invalid_column"`).
        assert_eq!(err.code, "invalid_column");
    }

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

    #[tokio::test]
    async fn clock_now_millis_returns_a_positive_integer() {
        let result = caps()
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
    async fn clock_now_rfc3339_returns_a_z_suffixed_string() {
        let result = caps()
            .handle(call(
                CapabilityKind::Clock,
                "now-rfc3339",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds");
        assert!(result.as_str().unwrap().ends_with('Z'));
    }

    #[tokio::test]
    async fn clock_monotonic_nanos_is_non_decreasing_across_two_calls() {
        let c = caps();
        let first = c
            .handle(call(
                CapabilityKind::Clock,
                "monotonic-nanos",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds")
            .as_u64()
            .unwrap();
        let second = c
            .handle(call(
                CapabilityKind::Clock,
                "monotonic-nanos",
                serde_json::json!({}),
            ))
            .await
            .expect("clock succeeds")
            .as_u64()
            .unwrap();
        assert!(second >= first);
    }

    #[tokio::test]
    async fn clock_unknown_op_is_denied() {
        let err = caps()
            .handle(call(CapabilityKind::Clock, "bogus", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn context_reports_scope_without_secrets() {
        let result = caps()
            .handle(call(CapabilityKind::Context, "get", serde_json::json!({})))
            .await
            .expect("context succeeds");
        assert_eq!(result["tenant"], "acme");
        assert_eq!(result["community"], "main");
        assert_eq!(result["app_id"], "waddles.bot.commands.default");
    }

    #[tokio::test]
    async fn log_write_at_every_level_succeeds() {
        let c = caps();
        for level in ["error", "warn", "debug", "info", "unrecognized"] {
            let result = c
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
    async fn kv_db_flags_capabilities_are_documented_seams() {
        let c = caps();
        for capability in [
            CapabilityKind::Db,
            CapabilityKind::Kv,
            CapabilityKind::Flags,
        ] {
            let err = c
                .handle(call(capability, "anything", serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_implemented");
        }
    }

    // -- `http` capability (interim capability-gate: undeclared means
    // denied, `HttpEgressCatalog`'s doc) --

    /// The default fixture's catalog is empty -- every `app_id` is
    /// undeclared, so `http` denies exactly like an explicit
    /// manifest-egress miss in `svc_action`.
    #[tokio::test]
    async fn http_denies_an_undeclared_app_as_host_not_declared() {
        let err = caps()
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://api.example.com/"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A host the app's manifest never declared is denied even when the
    /// same app *does* have other declared hosts -- declaring one host
    /// buys access to nothing else.
    #[tokio::test]
    async fn http_denies_a_non_allowlisted_host_for_a_declared_app() {
        let err = caps_with_egress_rule("api.example.com", &["GET"])
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://evil.example.com/"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A declared host resolving to a private IP is still blocked by the
    /// guard's always-enforced SSRF address check -- declaring a host
    /// never bypasses step 6 (`bundle_host_http::egress::
    /// is_forbidden_address`'s doc).
    #[tokio::test]
    async fn http_denies_a_declared_host_that_is_actually_a_private_ip() {
        let err = caps_with_egress_rule("10.0.0.5", &["GET"])
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    /// The allow path: a declared public host, reached through the real
    /// `EgressGuard` pipeline via a live local server standing in for
    /// `api.example.com` (the guard's own DNS-pinning connects to whatever
    /// `Resolver` returns -- `ReqwestTransport` here resolves the
    /// loopback listener through the OS resolver override below).
    #[tokio::test]
    async fn http_send_reaches_the_transport_for_a_declared_allowlisted_host() {
        // `EgressGuard`'s production `Resolver` is `tokio::net::
        // lookup_host`, not swappable outside this crate -- so the
        // end-to-end allow path is proven the same way
        // `bundle_host_http::egress`'s own moved test suite proves it
        // (`FakeTransport`/`TestCatalog`-equivalent), via a fresh
        // `EgressGuard` built directly rather than through `caps_with_
        // egress_rule` (which pins `ReqwestTransport`, a *real* TLS
        // client this test must not depend on network access).
        struct FakeTransport(
            Arc<std::sync::Mutex<Vec<bundle_host_http::egress::TransportRequest>>>,
        );
        impl bundle_host_http::egress::HttpTransport for FakeTransport {
            fn send<'a>(
                &'a self,
                req: bundle_host_http::egress::TransportRequest,
                _timeout: Duration,
                _max_response_bytes: usize,
            ) -> Pin<
                Box<
                    dyn Future<
                            Output = Result<
                                bundle_host_http::egress::TransportResponse,
                                HostResultError,
                            >,
                        > + Send
                        + 'a,
                >,
            > {
                self.0.lock().unwrap().push(req);
                Box::pin(async move {
                    Ok(bundle_host_http::egress::TransportResponse {
                        status: 200,
                        headers: vec![],
                        body: b"{}".to_vec(),
                        truncated: false,
                    })
                })
            }
        }

        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let catalog = HttpEgressCatalog::new();
        catalog.update(
            "waddles.bot.commands.default",
            EgressRuleRow::from_legacy_patterns(
                vec![("93.184.216.34".to_string(), vec!["GET".to_string()])],
                None,
                HashMap::new(),
            ),
        );
        let egress = Arc::new(EgressGuard::new(
            Arc::new(FakeTransport(Arc::clone(&seen))),
            test_egress_limits(),
            catalog,
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        let caps = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            egress,
        );

        let result = caps
            .handle(call(
                CapabilityKind::Http,
                "send",
                serde_json::json!({"method": "GET", "url": "https://93.184.216.34/"}),
            ))
            .await
            .expect("declared public IP is permitted");
        assert_eq!(result["status"], 200);
        assert_eq!(seen.lock().unwrap().len(), 1);
    }

    /// Secret-handle substitution (connector spec condition 8) works
    /// identically through this stage's `http` capability -- the guest's
    /// own `secret_refs` name is resolved against this bundle's granted
    /// map, never handed a raw env-var name.
    #[tokio::test]
    async fn http_send_substitutes_a_granted_secret_ref_as_a_header() {
        struct FakeTransport(
            Arc<std::sync::Mutex<Vec<bundle_host_http::egress::TransportRequest>>>,
        );
        impl bundle_host_http::egress::HttpTransport for FakeTransport {
            fn send<'a>(
                &'a self,
                req: bundle_host_http::egress::TransportRequest,
                _timeout: Duration,
                _max_response_bytes: usize,
            ) -> Pin<
                Box<
                    dyn Future<
                            Output = Result<
                                bundle_host_http::egress::TransportResponse,
                                HostResultError,
                            >,
                        > + Send
                        + 'a,
                >,
            > {
                self.0.lock().unwrap().push(req);
                Box::pin(async move {
                    Ok(bundle_host_http::egress::TransportResponse {
                        status: 200,
                        headers: vec![],
                        body: b"{}".to_vec(),
                        truncated: false,
                    })
                })
            }
        }

        // SAFETY: test-process-local env var, unique name avoids
        // cross-test collisions under parallel `cargo test` execution.
        unsafe { std::env::set_var("SVC_PROCESS_EGRESS_TEST_TOKEN", "s3cr3t") };
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let catalog = HttpEgressCatalog::new();
        catalog.update(
            "waddles.bot.commands.default",
            EgressRuleRow::from_legacy_patterns(
                vec![("93.184.216.34".to_string(), vec!["POST".to_string()])],
                None,
                HashMap::from([(
                    "TOKEN_REF".to_string(),
                    "SVC_PROCESS_EGRESS_TEST_TOKEN".to_string(),
                )]),
            ),
        );
        let egress = Arc::new(EgressGuard::new(
            Arc::new(FakeTransport(Arc::clone(&seen))),
            test_egress_limits(),
            catalog,
            test_egress_metrics(),
            bundle_host_http::egress::boxed(StaticFlag(true)),
        ));
        let caps = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
            egress,
        );

        caps.handle(call(
            CapabilityKind::Http,
            "send",
            serde_json::json!({
                "method": "POST",
                "url": "https://93.184.216.34/",
                "secret_refs": {"Authorization": "TOKEN_REF"}
            }),
        ))
        .await
        .expect("send succeeds");
        unsafe { std::env::remove_var("SVC_PROCESS_EGRESS_TEST_TOKEN") };

        let requests = seen.lock().unwrap();
        assert!(requests[0]
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == "s3cr3t"));
    }

    #[tokio::test]
    async fn relay_is_permanently_denied_never_granted_to_process() {
        let err = caps()
            .handle(call(CapabilityKind::Relay, "push", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_granted");
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

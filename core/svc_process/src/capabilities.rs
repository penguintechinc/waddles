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
//! This M4 landing wires `context`/`clock`/`log` fully (the three
//! capabilities every bundle needs regardless of manifest declarations,
//! spec §6.5's capability table: "Always granted") and `kv`, backed by
//! `bundle_host_kv::KvHost` (the crate shared with `core/svc_action` --
//! see that crate's own module doc for the key-derivation/isolation/quota
//! design) over a direct Valkey connection opened once at startup
//! (`crate::lib::connect_kv`) and cloned into every per-invoke
//! [`StageCapabilities`] this loop constructs (`crate::spine::
//! ProcessDeps::kv_conn`'s doc). `http`/`db`/`flags` remain documented
//! seams -- see [`StageCapabilities::handle`]'s match arms -- since a real
//! `db` wiring needs the manifest's `data.tables` allowlist plus
//! per-bundle-role RLS (`SET LOCAL waddles.tenant`/`waddles.community`,
//! spec §7.4/§11.10), tracked as a separate design in progress. `relay` is
//! never granted to a process-stage bundle at all (spec §6.5: "Capability:
//! granted only to action-stage bundles") and is denied unconditionally,
//! not merely unimplemented.
//!
//! A bundle never holds a platform credential or a tenant/community
//! argument (spec §4.3, §5.11): every capability here resolves its own
//! scope from `self`, never from the `args`/`op` the guest supplied.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use bundle_host_kv::{KvBackend, KvError, KvHost, KvScope};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};

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

/// The real capability implementation this stage wires today, scoped to
/// exactly one invocation's `(tenant, community, app_id)` -- see the
/// module doc for why this is constructed per-invoke, never per-connection.
/// Generic over [`KvBackend`] (defaulted to the production connection
/// type) for the same reason `core/svc_action::capabilities::
/// StageCapabilities` is: `handle_kv`'s argument-parsing/error-mapping is
/// unit-testable against a fake implementing the public
/// `bundle_host_kv::KvBackend` trait, with no live Valkey server -- see
/// this module's `tests::FakeKvBackend`.
pub struct StageCapabilities<K: KvBackend = redis::aio::MultiplexedConnection> {
    tenant: String,
    community: Option<String>,
    app_id: String,
    /// See [`Self::with_kv`]'s doc; `None` until it is called (a bundle
    /// sees `not_implemented` rather than this loop failing to start if
    /// the Valkey connection for `kv` was never configured).
    kv: Option<KvHost<K>>,
}

impl<K: KvBackend> StageCapabilities<K> {
    /// Builds the capability set for exactly one `invoke` -- `context` and
    /// every other capability are scope-implicit (spec §5.11: "No bundle
    /// host call accepts a tenant or community argument at all"). `kv`
    /// starts unconfigured; see [`Self::with_kv`].
    pub fn new(tenant: String, community: Option<String>, app_id: String) -> Self {
        Self {
            tenant,
            community,
            app_id,
            kv: None,
        }
    }

    /// Enables the `kv` capability over `backend` (production:
    /// `redis::aio::MultiplexedConnection`, cloned from
    /// `crate::spine::ProcessDeps::kv_conn` on every invoke -- a cheap
    /// handle clone over one shared connection, not a new socket).
    /// Builder-style so a deployment where the Valkey connection failed to
    /// open at startup can still construct every other capability and
    /// simply skip this call.
    pub fn with_kv(mut self, backend: K) -> Self {
        self.kv = Some(KvHost::new(backend));
        self
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

    /// `kv.get`/`kv.set`/`kv.delete`/`kv.increment` (`wit/waddle-bundle/
    /// stage.wit` `interface kv`). Argument shapes match exactly what
    /// `core/bundle_executor::host::imports`'s `kv::Host` impl sends/
    /// expects -- see `core/svc_action::capabilities::StageCapabilities::
    /// handle_kv`'s identical doc for the wire contract this must not
    /// drift from (byte-for-byte the same parsing/mapping, duplicated
    /// rather than shared only because the two stages' `handle` methods
    /// take `scope` differently -- `self` here vs. a separate parameter
    /// there -- module doc). Tenant/community/app_id come from `self`
    /// (never `call.app_id`).
    async fn handle_kv(&self, call: &HostCallBody) -> Result<serde_json::Value, HostResultError> {
        let Some(kv) = &self.kv else {
            return Err(denied(
                "not_implemented",
                "kv capability is not configured on this stage (no Valkey connection)",
            ));
        };
        let kv_scope = KvScope::new(
            self.tenant.clone(),
            self.community.clone(),
            self.app_id.clone(),
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
/// error reply carries -- see `core/svc_action::capabilities::
/// kv_err_to_host`'s identical doc.
fn kv_err_to_host(err: KvError) -> HostResultError {
    denied(err.wire_code(), err.wire_message())
}

impl<K: KvBackend> CapabilityHandler for StageCapabilities<K> {
    fn handle<'a>(
        &'a self,
        call: HostCallBody,
    ) -> Pin<Box<dyn Future<Output = Result<serde_json::Value, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            match call.capability {
                CapabilityKind::Clock => self.handle_clock(&call.op),
                CapabilityKind::Context => self.handle_context(),
                CapabilityKind::Log => self.handle_log(&call.args),
                CapabilityKind::Kv => self.handle_kv(&call).await,
                // TODO(M4+): `http` (guarded egress per manifest `egress`
                // allowlist) is not wired in this landing -- denying
                // (never silently succeeding) is the correct behavior for
                // an unimplemented capability: a bundle calling it sees
                // `access-denied`, not a fabricated success.
                CapabilityKind::Http => Err(denied(
                    "not_implemented",
                    "http capability is not wired in this build -- TODO(M4+)",
                )),
                // `db` needs the manifest's `data.tables` allowlist plus
                // per-bundle-role Postgres RLS (`SET LOCAL waddles.tenant`/
                // `waddles.community`, spec §7.4/§11.10), and is being
                // extended further (single-statement -> transactional
                // `db.execute-batch`, a manifest `capabilities` allowlist)
                // by a separate, in-progress design -- see
                // `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md`
                // §3/§4. Denying (never silently succeeding) is the correct
                // behavior for an unimplemented capability until that design
                // lands and is wired here.
                CapabilityKind::Db => Err(denied(
                    "not_implemented",
                    "db capability is not wired in this build",
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

    fn caps() -> StageCapabilities {
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
        )
    }

    fn caps_with_kv() -> StageCapabilities<FakeKvBackend> {
        StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.bot.commands.default".to_string(),
        )
        .with_kv(FakeKvBackend::default())
    }

    /// A minimal in-memory [`KvBackend`] fake, mirroring
    /// `bundle_host_kv::backend::fake::FakeBackend`'s semantics (that one
    /// is crate-private to `bundle_host_kv`, so `handle_kv`'s own
    /// argument-parsing/error-mapping is exercised here against a fresh,
    /// independent implementation of the public `KvBackend` trait -- no
    /// live Valkey server needed for this module's own tests). Identical
    /// to `core/svc_action::capabilities::tests::FakeKvBackend`.
    #[derive(Default)]
    struct FakeKvBackend {
        data: std::sync::Mutex<std::collections::HashMap<String, Vec<u8>>>,
        counts: std::sync::Mutex<std::collections::HashMap<String, u64>>,
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
    async fn http_db_flags_capabilities_are_documented_seams() {
        // `kv` is no longer an unconditional seam -- see
        // `kv_is_not_implemented_when_no_backend_was_configured` for its
        // own (backend-unconfigured) not_implemented case, and the `kv_*`
        // tests below for the fully-wired behavior.
        let c = caps();
        for capability in [
            CapabilityKind::Http,
            CapabilityKind::Db,
            CapabilityKind::Flags,
        ] {
            let err = c
                .handle(call(capability, "anything", serde_json::json!({})))
                .await
                .unwrap_err();
            assert_eq!(err.code, "not_implemented");
        }
    }

    #[tokio::test]
    async fn kv_is_not_implemented_when_no_backend_was_configured() {
        let err = caps()
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "not_implemented");
    }

    #[tokio::test]
    async fn kv_set_then_get_round_trips_through_the_handler() {
        let c = caps_with_kv();
        c.handle(call(
            CapabilityKind::Kv,
            "set",
            serde_json::json!({"key": "counter", "value": [1, 2, 3], "ttl_seconds": 0}),
        ))
        .await
        .expect("set succeeds");

        let result = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "counter"}),
            ))
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::json!([1, 2, 3]));
    }

    #[tokio::test]
    async fn kv_get_of_an_absent_key_returns_a_null_value_not_an_error() {
        let c = caps_with_kv();
        let result = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "absent"}),
            ))
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn kv_delete_then_get_returns_null() {
        let c = caps_with_kv();
        c.handle(call(
            CapabilityKind::Kv,
            "set",
            serde_json::json!({"key": "k", "value": [9], "ttl_seconds": 0}),
        ))
        .await
        .unwrap();
        c.handle(call(
            CapabilityKind::Kv,
            "delete",
            serde_json::json!({"key": "k"}),
        ))
        .await
        .expect("delete succeeds");
        let result = c
            .handle(call(
                CapabilityKind::Kv,
                "get",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .expect("get succeeds");
        assert_eq!(result["value"], serde_json::Value::Null);
    }

    #[tokio::test]
    async fn kv_increment_accumulates_across_calls() {
        let c = caps_with_kv();
        let first = c
            .handle(call(
                CapabilityKind::Kv,
                "increment",
                serde_json::json!({"key": "hits", "delta": 5, "ttl_seconds": 0}),
            ))
            .await
            .expect("increment succeeds");
        assert_eq!(first["value"], serde_json::json!(5));

        let second = c
            .handle(call(
                CapabilityKind::Kv,
                "increment",
                serde_json::json!({"key": "hits", "delta": 3, "ttl_seconds": 0}),
            ))
            .await
            .expect("increment succeeds");
        assert_eq!(second["value"], serde_json::json!(8));
    }

    #[tokio::test]
    async fn kv_set_with_malformed_args_is_rejected_as_invalid_args() {
        let c = caps_with_kv();
        let err = c
            .handle(call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "k"}),
            ))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    #[tokio::test]
    async fn kv_unknown_op_is_rejected() {
        let c = caps_with_kv();
        let err = c
            .handle(call(CapabilityKind::Kv, "bogus", serde_json::json!({})))
            .await
            .unwrap_err();
        assert_eq!(err.code, "unknown_op");
    }

    #[tokio::test]
    async fn kv_rejects_a_guest_key_that_attempts_a_namespace_escape() {
        let c = caps_with_kv();
        let err = c
            .handle(call(
                CapabilityKind::Kv,
                "set",
                serde_json::json!({"key": "other:app:data:secret", "value": [1], "ttl_seconds": 0}),
            ))
            .await
            .unwrap_err();
        // `KvError::wire_code` collapses every non-`too_large` reason to
        // `"backend"` at the host-call boundary (module doc) -- the
        // finer-grained `invalid_key` reason is what `bundle_host_kv`'s own
        // tests assert against `KvError::code()` directly.
        assert_eq!(err.code, "backend");
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

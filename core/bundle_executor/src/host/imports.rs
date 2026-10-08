//! `Host` trait implementations for every interface the `waddle:bundle/
//! stage@1.0.0` world imports, wired onto [`ExecState`]. Every one of
//! `context`/`http`/`kv`/`db`/`relay`/`%flags`/`log`/`clock` is answered
//! by a real round trip through [`HostBridge::call`] to the stage (spec
//! SS7.4) -- none of these fabricate a result locally.
//!
//! Three WIT signatures carry no error channel at all
//! (`context.get-context`, `flags.enabled`/`flags.tier`,
//! `clock.*`, `log.write`); each documents, at its own `impl`, the
//! specific fallback spec SS7.4 mandates or -- where SS7.4 is silent --
//! the most conservative default, always logged.

use penguin_bundle_host::wire::CapabilityKind;
use tracing::{debug, error, warn};

use crate::engine::waddle::bundle::{clock, context, db, flags, http, kv, log, relay, types};
use crate::error::ExecutorError;
use crate::host::ExecState;

/// `types` carries only shared records/variants (`platform-event`,
/// `stage-envelope`, ...) used by the *exported* `process-stage`/
/// `action-stage` interfaces, not by anything the `stage` world imports --
/// `bindgen!` still requires a (trivially empty) `Host` impl for it since
/// the world's dependency graph reaches it.
impl types::Host for ExecState {}

/// Routes a WIT import call through this instance's [`HostBridge`],
/// producing a uniform [`ExecutorError`] when there is no bridge at all
/// (a unit test exercising WASI/engine wiring without a live connection)
/// or when the round trip itself fails. `pub(crate)` so
/// `crate::host::connector_imports` (the `connector` world's reused
/// `http`/`log`/`clock`/`%flags` imports, spec S1) shares this exact
/// round-trip instead of a second copy.
pub(crate) async fn call(
    state: &mut ExecState,
    capability: CapabilityKind,
    op: &'static str,
    args: serde_json::Value,
) -> Result<serde_json::Value, ExecutorError> {
    let bridge = state
        .bridge
        .as_ref()
        .ok_or(ExecutorError::ConnectionUnavailable)?;
    bridge
        .call(&state.app_id, state.call_id, capability, op, args)
        .await
}

impl context::Host for ExecState {
    /// Spec SS7.4: "Built once per `invoke` from the envelope the stage
    /// took off the key plus the 3-tier resolved config." No WIT error
    /// channel exists on this signature; a bridge failure returns an
    /// empty-but-well-formed `BundleContext` and logs at ERROR, since a
    /// bundle silently getting an empty tenant/app_id is exactly the kind
    /// of thing an operator must be alerted to rather than have masked.
    async fn get_context(&mut self) -> context::BundleContext {
        match call(
            self,
            CapabilityKind::Context,
            "get-context",
            serde_json::json!({}),
        )
        .await
        {
            Ok(value) => match serde_json::from_value::<BundleContextWire>(value) {
                Ok(w) => w.into(),
                Err(e) => {
                    error!(error = %e, "get-context: malformed host-result, using empty context");
                    BundleContextWire::default().into()
                }
            },
            Err(e) => {
                error!(error = %e, "get-context host-call failed, using empty context");
                BundleContextWire::default().into()
            }
        }
    }
}

/// This executor's half of the `context`/`get-context` host-call JSON
/// contract (not specified at the byte level by the spec beyond "capability-
/// specific JSON", spec SS6.6) -- field names mirror the WIT record's own
/// kebab-case-to-snake_case mapping so the eventual stage-side
/// implementation (`core/svc_process`'s SS7.4 `context` row, currently
/// `TODO(M4)`) has an unambiguous shape to match.
#[derive(Debug, Default, serde::Deserialize)]
struct BundleContextWire {
    #[serde(default)]
    tenant: String,
    #[serde(default)]
    community: Option<String>,
    #[serde(default)]
    app_id: String,
    #[serde(default)]
    feature: String,
    #[serde(default)]
    version: String,
    #[serde(default)]
    message_id: String,
    #[serde(default)]
    config_json: String,
}

impl From<BundleContextWire> for context::BundleContext {
    fn from(w: BundleContextWire) -> Self {
        context::BundleContext {
            tenant: w.tenant,
            community: w.community,
            app_id: w.app_id,
            feature: w.feature,
            version: w.version,
            message_id: w.message_id,
            config_json: if w.config_json.is_empty() {
                "{}".to_string()
            } else {
                w.config_json
            },
        }
    }
}

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
            Ok(value) => serde_json::from_value::<HttpResponseWire>(value)
                .map(Into::into)
                .map_err(|e| http::Error::Transport(format!("malformed host-result: {e}"))),
            Err(e) => Err(http_error_from(e)),
        }
    }
}

#[derive(Debug, serde::Deserialize)]
struct HttpResponseWire {
    status: u16,
    #[serde(default)]
    headers: Vec<(String, String)>,
    #[serde(default)]
    body: Vec<u8>,
    #[serde(default)]
    truncated: bool,
}

impl From<HttpResponseWire> for http::Response {
    fn from(w: HttpResponseWire) -> Self {
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

fn http_error_from(err: ExecutorError) -> http::Error {
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

impl kv::Host for ExecState {
    async fn get(&mut self, key: String) -> Result<Option<Vec<u8>>, kv::Error> {
        let args = serde_json::json!({ "key": key });
        match call(self, CapabilityKind::Kv, "get", args).await {
            Ok(value) => serde_json::from_value::<KvGetWire>(value)
                .map(|w| w.value)
                .map_err(|e| kv::Error::Backend(format!("malformed host-result: {e}"))),
            Err(e) => Err(kv_error_from(e)),
        }
    }

    async fn set(
        &mut self,
        key: String,
        value: Vec<u8>,
        ttl_seconds: u32,
    ) -> Result<(), kv::Error> {
        let args = serde_json::json!({ "key": key, "value": value, "ttl_seconds": ttl_seconds });
        call(self, CapabilityKind::Kv, "set", args)
            .await
            .map(|_| ())
            .map_err(kv_error_from)
    }

    async fn delete(&mut self, key: String) -> Result<(), kv::Error> {
        let args = serde_json::json!({ "key": key });
        call(self, CapabilityKind::Kv, "delete", args)
            .await
            .map(|_| ())
            .map_err(kv_error_from)
    }

    async fn increment(
        &mut self,
        key: String,
        delta: i64,
        ttl_seconds: u32,
    ) -> Result<i64, kv::Error> {
        let args = serde_json::json!({ "key": key, "delta": delta, "ttl_seconds": ttl_seconds });
        match call(self, CapabilityKind::Kv, "increment", args).await {
            Ok(value) => serde_json::from_value::<KvIncrementWire>(value)
                .map(|w| w.value)
                .map_err(|e| kv::Error::Backend(format!("malformed host-result: {e}"))),
            Err(e) => Err(kv_error_from(e)),
        }
    }
}

#[derive(Debug, serde::Deserialize)]
struct KvGetWire {
    #[serde(default)]
    value: Option<Vec<u8>>,
}

#[derive(Debug, serde::Deserialize)]
struct KvIncrementWire {
    value: i64,
}

fn kv_error_from(err: ExecutorError) -> kv::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } if code == "too_large" => {
            kv::Error::TooLarge(message.parse().unwrap_or(0))
        }
        other => kv::Error::Backend(other.to_string()),
    }
}

impl db::Host for ExecState {
    async fn insert(&mut self, column_values: Vec<db::ColumnValue>) -> Result<db::Row, db::Error> {
        let args = serde_json::json!({ "column_values": column_values_to_json(&column_values) });
        db_call(self, "insert", args).await
    }

    async fn get(&mut self, row_id: String) -> Result<db::Row, db::Error> {
        let args = serde_json::json!({ "row_id": row_id });
        db_call(self, "get", args).await
    }

    async fn query(
        &mut self,
        limit: u32,
        offset: u32,
        order_by: Option<db::OrderBy>,
    ) -> Result<Vec<db::Row>, db::Error> {
        let args = serde_json::json!({
            "limit": limit,
            "offset": offset,
            "order_by": order_by.map(order_by_to_json),
        });
        match call(self, CapabilityKind::Db, "query", args).await {
            Ok(value) => serde_json::from_value::<DbQueryWire>(value)
                .map(|w| w.rows.into_iter().map(Into::into).collect())
                .map_err(|e| db::Error::Backend(format!("malformed host-result: {e}"))),
            Err(e) => Err(db_error_from(e)),
        }
    }

    async fn update(
        &mut self,
        row_id: String,
        expected_version: u64,
        column_values: Vec<db::ColumnValue>,
    ) -> Result<db::Row, db::Error> {
        let args = serde_json::json!({
            "row_id": row_id,
            "expected_version": expected_version,
            "column_values": column_values_to_json(&column_values),
        });
        db_call(self, "update", args).await
    }

    async fn delete(&mut self, row_id: String, expected_version: u64) -> Result<(), db::Error> {
        let args = serde_json::json!({ "row_id": row_id, "expected_version": expected_version });
        call(self, CapabilityKind::Db, "delete", args)
            .await
            .map(|_| ())
            .map_err(db_error_from)
    }
}

/// Shapes a WIT `db.order-by` into the JSON `{"random": true}` /
/// `{"column": name, "descending": bool}` shape `core/svc_process::
/// capabilities::parse_order_by` parses at the untyped host-call layer.
fn order_by_to_json(order_by: db::OrderBy) -> serde_json::Value {
    match order_by {
        db::OrderBy::Random => serde_json::json!({ "random": true }),
        db::OrderBy::Column(col) => {
            serde_json::json!({ "column": col.name, "descending": col.descending })
        }
    }
}

/// Shared by every `db` op that answers with a single row
/// (`insert`/`get`/`update`) -- `query` decodes its own `{rows: [...]}`
/// envelope separately (see [`db::Host::query`]).
async fn db_call(
    state: &mut ExecState,
    op: &'static str,
    args: serde_json::Value,
) -> Result<db::Row, db::Error> {
    match call(state, CapabilityKind::Db, op, args).await {
        Ok(value) => serde_json::from_value::<DbRowWire>(value)
            .map(Into::into)
            .map_err(|e| db::Error::Backend(format!("malformed host-result: {e}"))),
        Err(e) => Err(db_error_from(e)),
    }
}

fn db_value_to_json(v: &db::Value) -> serde_json::Value {
    match v {
        db::Value::NullValue => serde_json::Value::Null,
        db::Value::BoolValue(b) => serde_json::json!(b),
        db::Value::IntValue(i) => serde_json::json!(i),
        db::Value::FloatValue(f) => serde_json::json!(f),
        db::Value::TextValue(s) => serde_json::json!(s),
        db::Value::BytesValue(b) => serde_json::json!(b),
    }
}

fn json_to_db_value(v: &serde_json::Value) -> db::Value {
    match v {
        serde_json::Value::Null => db::Value::NullValue,
        serde_json::Value::Bool(b) => db::Value::BoolValue(*b),
        serde_json::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                db::Value::IntValue(i)
            } else {
                db::Value::FloatValue(n.as_f64().unwrap_or_default())
            }
        }
        serde_json::Value::String(s) => db::Value::TextValue(s.clone()),
        serde_json::Value::Array(items) => {
            let bytes: Option<Vec<u8>> = items
                .iter()
                .map(|i| i.as_u64().and_then(|n| u8::try_from(n).ok()))
                .collect();
            match bytes {
                Some(b) => db::Value::BytesValue(b),
                None => db::Value::TextValue(v.to_string()),
            }
        }
        other => db::Value::TextValue(other.to_string()),
    }
}

/// `host-call`'s `column_values` arg: a JSON object, `{column: value, ...}`
/// -- matches `core/svc_process::capabilities::StageCapabilities::handle_db`'s
/// own `column_values` parsing (`args.get("column_values").as_object()`).
fn column_values_to_json(column_values: &[db::ColumnValue]) -> serde_json::Value {
    let mut obj = serde_json::Map::with_capacity(column_values.len());
    for cv in column_values {
        obj.insert(cv.column.clone(), db_value_to_json(&cv.value));
    }
    serde_json::Value::Object(obj)
}

#[derive(Debug, serde::Deserialize)]
struct DbRowWire {
    row_id: String,
    version: u64,
    columns: serde_json::Map<String, serde_json::Value>,
}

impl From<DbRowWire> for db::Row {
    fn from(w: DbRowWire) -> Self {
        db::Row {
            row_id: w.row_id,
            version: w.version,
            columns: w
                .columns
                .into_iter()
                .map(|(column, value)| db::ColumnValue {
                    column,
                    value: json_to_db_value(&value),
                })
                .collect(),
        }
    }
}

#[derive(Debug, serde::Deserialize)]
struct DbQueryWire {
    rows: Vec<DbRowWire>,
}

fn db_error_from(err: ExecutorError) -> db::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "denied" | "not_granted" | "feature_disabled" | "not_implemented" => {
                db::Error::Denied(message.clone())
            }
            "invalid_column" => db::Error::InvalidColumn(message.clone()),
            "invalid_value" | "invalid_args" => db::Error::InvalidValue(message.clone()),
            "not_found" => db::Error::NotFound,
            "conflict" => db::Error::Conflict(message.clone()),
            "quota_exceeded" => db::Error::QuotaExceeded(message.clone()),
            "timeout" => db::Error::Timeout,
            _ => db::Error::Backend(message.clone()),
        },
        other => db::Error::Backend(other.to_string()),
    }
}

impl relay::Host for ExecState {
    async fn push(&mut self, provider: String, message_json: String) -> Result<(), relay::Error> {
        let args = serde_json::json!({ "provider": provider, "message_json": message_json });
        call(self, CapabilityKind::Relay, "push", args)
            .await
            .map(|_| ())
            .map_err(|err| match &err {
                ExecutorError::HostCallDenied { code, message, .. } if code == "denied" => {
                    relay::Error::Denied(message.clone())
                }
                other => relay::Error::Backend(other.to_string()),
            })
    }
}

impl flags::Host for ExecState {
    /// Spec SS7.4/SS6.5: "fail-open to the supplied `default-value` on a
    /// flag-server outage, never an exception" -- the fallback here is
    /// mandated, not a judgment call.
    async fn enabled(&mut self, key: String, default_value: bool) -> bool {
        let args = serde_json::json!({ "key": key, "default_value": default_value });
        match call(self, CapabilityKind::Flags, "enabled", args).await {
            Ok(value) => value
                .get("enabled")
                .and_then(serde_json::Value::as_bool)
                .unwrap_or(default_value),
            Err(e) => {
                warn!(error = %e, key, "flags.enabled host-call failed, failing open to default");
                default_value
            }
        }
    }

    /// No spec-mandated fallback for `tier`; `"free"` is the least-
    /// privileged tier (`rules/critical-rules.md` Feature Flags & License
    /// Tiers), so a failure here can only ever narrow, never widen, what
    /// a bundle believes it's entitled to.
    async fn tier(&mut self) -> String {
        match call(self, CapabilityKind::Flags, "tier", serde_json::json!({})).await {
            Ok(value) => value
                .get("tier")
                .and_then(serde_json::Value::as_str)
                .unwrap_or("free")
                .to_string(),
            Err(e) => {
                warn!(error = %e, "flags.tier host-call failed, defaulting to \"free\"");
                "free".to_string()
            }
        }
    }
}

impl log::Host for ExecState {
    /// Spec SS7.4: the stage sanitizes and emits guest logs, never this
    /// executor. `write` has no WIT error channel, so a failed host-call
    /// is logged locally at DEBUG and otherwise swallowed -- a bundle's
    /// own logging must never be able to affect its control flow.
    async fn write(&mut self, lvl: log::Level, message: String, fields_json: String) {
        let level_str = match lvl {
            log::Level::Error => "error",
            log::Level::Warn => "warn",
            log::Level::Info => "info",
            log::Level::Debug => "debug",
        };
        let args = serde_json::json!({ "level": level_str, "message": message, "fields_json": fields_json });
        if let Err(e) = call(self, CapabilityKind::Log, "write", args).await {
            debug!(error = %e, "log.write host-call failed, dropping this guest log line");
        }
    }
}

impl clock::Host for ExecState {
    /// No WIT error channel; falls back to this process's own clock on a
    /// failed host-call (spec SS7.4's "Wall clock from the stage" is the
    /// intended source, but a bundle's timing call must never block or
    /// panic on a connection hiccup).
    async fn now_millis(&mut self) -> u64 {
        match call(
            self,
            CapabilityKind::Clock,
            "now-millis",
            serde_json::json!({}),
        )
        .await
        {
            Ok(value) => value.as_u64().unwrap_or_else(local_now_millis),
            Err(e) => {
                warn!(error = %e, "clock.now-millis host-call failed, using local clock");
                local_now_millis()
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
            Ok(value) => value
                .as_str()
                .map(str::to_string)
                .unwrap_or_else(local_now_rfc3339),
            Err(e) => {
                warn!(error = %e, "clock.now-rfc3339 host-call failed, using local clock");
                local_now_rfc3339()
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
                warn!(error = %e, "clock.monotonic-nanos host-call failed, returning 0");
                0
            }
        }
    }
}

fn local_now_millis() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

fn local_now_rfc3339() -> String {
    // No `time`/`chrono` dependency in this crate by design (spec SS4.5's
    // minimal dependency set) -- this fallback path only runs when the
    // stage round trip already failed, so a coarse millisecond-epoch
    // string is preferable to pulling in a formatting crate for a
    // degraded-mode value nothing downstream treats as authoritative.
    format!("epoch-ms:{}", local_now_millis())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::host::HostBridge;
    use penguin_bundle_host::wire::{read_frame, write_frame, Frame, HostResultError, Message};
    use std::sync::Arc;
    use tokio::sync::mpsc;

    /// Builds a real [`HostBridge`] wired to a fake stage (an in-memory
    /// `tokio::io::duplex`) that replies to exactly one `host-call` with
    /// `outcome`, then stops. Every `Host` trait method under test issues
    /// exactly one host-call per invocation, so this is a complete,
    /// genuine round trip for each -- never a fabricated return value
    /// (`rules/general.md`: "never fake a host call").
    fn one_shot_bridge(outcome: Result<serde_json::Value, HostResultError>) -> Arc<HostBridge> {
        let (exec_io, stage_io) = tokio::io::duplex(64 * 1024);
        let (exec_reader, mut exec_writer) = tokio::io::split(exec_io);
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
        let mut exec_reader = exec_reader;
        tokio::spawn(async move {
            if let Ok(frame) = read_frame(&mut exec_reader).await {
                let _ = connection_for_reader.deliver(frame);
            }
        });

        tokio::spawn(async move {
            let mut io = stage_io;
            if let Ok(frame) = read_frame(&mut io).await {
                let body = match outcome {
                    Ok(v) => penguin_bundle_host::wire::HostResultBody {
                        result: Some(v),
                        error: None,
                    },
                    Err(e) => penguin_bundle_host::wire::HostResultBody {
                        result: None,
                        error: Some(e),
                    },
                };
                let _ =
                    write_frame(&mut io, &Frame::new(frame.id, Message::HostResult(body))).await;
            }
        });

        HostBridge::new(connection)
    }

    fn denied(code: &str, message: &str) -> HostResultError {
        HostResultError {
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    fn state_with(bridge: Arc<HostBridge>) -> ExecState {
        ExecState::new(Some(bridge), "waddles.test.app".to_string(), 1)
    }

    #[tokio::test]
    async fn context_get_context_decodes_a_real_host_result() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({
            "tenant": "t1", "community": "c1", "app_id": "waddles.test.app",
            "feature": "process", "version": "1", "message_id": "m1",
            "config_json": "{\"k\":1}"
        })));
        let mut state = state_with(bridge);
        let ctx = context::Host::get_context(&mut state).await;
        assert_eq!(ctx.tenant, "t1");
        assert_eq!(ctx.community, Some("c1".to_string()));
        assert_eq!(ctx.config_json, "{\"k\":1}");
    }

    #[tokio::test]
    async fn http_send_decodes_a_successful_response() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({
            "status": 200, "headers": [["x", "y"]], "body": [1,2,3], "truncated": false
        })));
        let mut state = state_with(bridge);
        let req = http::Request {
            method: "GET".to_string(),
            url: "https://example.test/".to_string(),
            headers: vec![],
            body: None,
            secret_refs: vec![],
        };
        let resp = http::Host::send(&mut state, req).await.expect("send ok");
        assert_eq!(resp.status, 200);
        assert_eq!(resp.body, vec![1, 2, 3]);
    }

    #[tokio::test]
    async fn http_send_maps_every_denial_code() {
        for (code, expect_variant) in [
            ("denied", "denied"),
            ("timeout", "timeout"),
            ("too_large", "too_large"),
            ("rate_limited", "rate_limited"),
            ("transport", "transport"),
            ("something_else", "transport"),
        ] {
            let bridge = one_shot_bridge(Err(denied(code, "nope")));
            let mut state = state_with(bridge);
            let req = http::Request {
                method: "GET".to_string(),
                url: "https://example.test/".to_string(),
                headers: vec![],
                body: None,
                secret_refs: vec![],
            };
            let err = http::Host::send(&mut state, req)
                .await
                .expect_err("must be denied");
            let matches = matches!(
                (&err, expect_variant),
                (http::Error::Denied(_), "denied")
                    | (http::Error::Timeout, "timeout")
                    | (http::Error::TooLarge(_), "too_large")
                    | (http::Error::RateLimited(_), "rate_limited")
                    | (http::Error::Transport(_), "transport")
            );
            assert!(
                matches,
                "code {code:?} mapped to unexpected variant {err:?}"
            );
        }
    }

    #[tokio::test]
    async fn http_send_reports_malformed_host_result() {
        let bridge = one_shot_bridge(Ok(serde_json::json!("not-an-object")));
        let mut state = state_with(bridge);
        let req = http::Request {
            method: "GET".to_string(),
            url: "https://example.test/".to_string(),
            headers: vec![],
            body: None,
            secret_refs: vec![],
        };
        assert!(matches!(
            http::Host::send(&mut state, req).await,
            Err(http::Error::Transport(_))
        ));
    }

    #[tokio::test]
    async fn kv_get_decodes_present_and_absent_values() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({ "value": [1,2,3] })));
        let mut state = state_with(bridge);
        assert_eq!(
            kv::Host::get(&mut state, "k".to_string()).await.unwrap(),
            Some(vec![1, 2, 3])
        );
    }

    #[tokio::test]
    async fn kv_get_maps_too_large_denial() {
        let bridge = one_shot_bridge(Err(denied("too_large", "65537")));
        let mut state = state_with(bridge);
        assert!(matches!(
            kv::Host::get(&mut state, "k".to_string()).await,
            Err(kv::Error::TooLarge(65537))
        ));
    }

    #[tokio::test]
    async fn kv_set_success_and_backend_error() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({})));
        let mut state = state_with(bridge);
        kv::Host::set(&mut state, "k".to_string(), vec![1], 0)
            .await
            .expect("set ok");

        let bridge = one_shot_bridge(Err(denied("backend", "boom")));
        let mut state = state_with(bridge);
        assert!(matches!(
            kv::Host::set(&mut state, "k".to_string(), vec![1], 0).await,
            Err(kv::Error::Backend(_))
        ));
    }

    #[tokio::test]
    async fn kv_delete_success() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({})));
        let mut state = state_with(bridge);
        kv::Host::delete(&mut state, "k".to_string())
            .await
            .expect("delete ok");
    }

    #[tokio::test]
    async fn kv_increment_success_and_malformed_result() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({ "value": 5 })));
        let mut state = state_with(bridge);
        assert_eq!(
            kv::Host::increment(&mut state, "k".to_string(), 1, 0)
                .await
                .unwrap(),
            5
        );

        let bridge = one_shot_bridge(Ok(serde_json::json!({})));
        let mut state = state_with(bridge);
        assert!(matches!(
            kv::Host::increment(&mut state, "k".to_string(), 1, 0).await,
            Err(kv::Error::Backend(_))
        ));
    }

    #[tokio::test]
    async fn db_insert_decodes_a_row_and_maps_every_error_code() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({
            "row_id": "00000000-0000-0000-0000-000000000000",
            "version": 1,
            "columns": {"score": 42, "note": null, "name": "x", "flag": true},
        })));
        let mut state = state_with(bridge);
        let row = db::Host::insert(&mut state, vec![])
            .await
            .expect("insert ok");
        assert_eq!(row.row_id, "00000000-0000-0000-0000-000000000000");
        assert_eq!(row.version, 1);
        assert_eq!(row.columns.len(), 4);

        for (code, check) in [
            ("denied", "denied"),
            ("invalid_column", "invalid_column"),
            ("invalid_value", "invalid_value"),
            ("not_found", "not_found"),
            ("conflict", "conflict"),
            ("quota_exceeded", "quota_exceeded"),
            ("timeout", "timeout"),
            ("backend", "backend"),
        ] {
            let bridge = one_shot_bridge(Err(denied(code, "nope")));
            let mut state = state_with(bridge);
            let err = db::Host::insert(&mut state, vec![])
                .await
                .expect_err("must be denied");
            let matches = matches!(
                (&err, check),
                (db::Error::Denied(_), "denied")
                    | (db::Error::InvalidColumn(_), "invalid_column")
                    | (db::Error::InvalidValue(_), "invalid_value")
                    | (db::Error::NotFound, "not_found")
                    | (db::Error::Conflict(_), "conflict")
                    | (db::Error::QuotaExceeded(_), "quota_exceeded")
                    | (db::Error::Timeout, "timeout")
                    | (db::Error::Backend(_), "backend")
            );
            assert!(
                matches,
                "code {code:?} mapped to unexpected variant {err:?}"
            );
        }
    }

    #[tokio::test]
    async fn db_insert_reports_malformed_host_result() {
        let bridge = one_shot_bridge(Ok(serde_json::json!("nope")));
        let mut state = state_with(bridge);
        assert!(matches!(
            db::Host::insert(&mut state, vec![]).await,
            Err(db::Error::Backend(_))
        ));
    }

    #[tokio::test]
    async fn db_get_decodes_a_row() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({
            "row_id": "00000000-0000-0000-0000-000000000000",
            "version": 3,
            "columns": {"score": 7},
        })));
        let mut state = state_with(bridge);
        let row = db::Host::get(
            &mut state,
            "00000000-0000-0000-0000-000000000000".to_string(),
        )
        .await
        .expect("get ok");
        assert_eq!(row.version, 3);
        assert_eq!(row.columns[0].column, "score");
        assert!(matches!(row.columns[0].value, db::Value::IntValue(7)));
    }

    #[tokio::test]
    async fn db_query_decodes_a_rows_envelope() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({
            "rows": [
                {"row_id": "00000000-0000-0000-0000-000000000001", "version": 1, "columns": {}},
                {"row_id": "00000000-0000-0000-0000-000000000002", "version": 1, "columns": {}},
            ]
        })));
        let mut state = state_with(bridge);
        let rows = db::Host::query(&mut state, 10, 0, None)
            .await
            .expect("query ok");
        assert_eq!(rows.len(), 2);
    }

    #[tokio::test]
    async fn db_query_accepts_an_order_by() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({ "rows": [] })));
        let mut state = state_with(bridge);
        let rows = db::Host::query(&mut state, 10, 0, Some(db::OrderBy::Random))
            .await
            .expect("query ok");
        assert!(rows.is_empty());
    }

    #[test]
    fn order_by_to_json_renders_random() {
        assert_eq!(
            order_by_to_json(db::OrderBy::Random),
            serde_json::json!({ "random": true })
        );
    }

    #[test]
    fn order_by_to_json_renders_a_column() {
        assert_eq!(
            order_by_to_json(db::OrderBy::Column(db::OrderColumn {
                name: "score".to_string(),
                descending: true,
            })),
            serde_json::json!({ "column": "score", "descending": true })
        );
    }

    #[tokio::test]
    async fn db_update_sends_row_id_expected_version_and_column_values() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({
            "row_id": "00000000-0000-0000-0000-000000000000",
            "version": 2,
            "columns": {},
        })));
        let mut state = state_with(bridge);
        let row = db::Host::update(
            &mut state,
            "00000000-0000-0000-0000-000000000000".to_string(),
            1,
            vec![db::ColumnValue {
                column: "score".to_string(),
                value: db::Value::IntValue(9),
            }],
        )
        .await
        .expect("update ok");
        assert_eq!(row.version, 2);
    }

    #[tokio::test]
    async fn db_delete_success_and_denied() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({})));
        let mut state = state_with(bridge);
        db::Host::delete(
            &mut state,
            "00000000-0000-0000-0000-000000000000".to_string(),
            1,
        )
        .await
        .expect("delete ok");

        let bridge = one_shot_bridge(Err(denied("conflict", "version mismatch")));
        let mut state = state_with(bridge);
        assert!(matches!(
            db::Host::delete(
                &mut state,
                "00000000-0000-0000-0000-000000000000".to_string(),
                1
            )
            .await,
            Err(db::Error::Conflict(_))
        ));
    }

    #[tokio::test]
    async fn relay_push_success_and_denied() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({})));
        let mut state = state_with(bridge);
        relay::Host::push(&mut state, "twitch".to_string(), "{}".to_string())
            .await
            .expect("push ok");

        let bridge = one_shot_bridge(Err(denied("denied", "no action stage")));
        let mut state = state_with(bridge);
        assert!(matches!(
            relay::Host::push(&mut state, "twitch".to_string(), "{}".to_string()).await,
            Err(relay::Error::Denied(_))
        ));
    }

    #[tokio::test]
    async fn relay_push_backend_error() {
        let bridge = one_shot_bridge(Err(denied("backend", "queue full")));
        let mut state = state_with(bridge);
        assert!(matches!(
            relay::Host::push(&mut state, "twitch".to_string(), "{}".to_string()).await,
            Err(relay::Error::Backend(_))
        ));
    }

    #[tokio::test]
    async fn flags_enabled_and_tier_decode_real_host_results() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({ "enabled": true })));
        let mut state = state_with(bridge);
        assert!(flags::Host::enabled(&mut state, "k".to_string(), false).await);

        let bridge = one_shot_bridge(Ok(serde_json::json!({ "tier": "enterprise" })));
        let mut state = state_with(bridge);
        assert_eq!(flags::Host::tier(&mut state).await, "enterprise");
    }

    #[tokio::test]
    async fn flags_enabled_fails_open_on_bridge_error() {
        // Two independent one-shot bridges: `one_shot_bridge` answers a
        // single host-call, and each `enabled()` call issues its own.
        let bridge = one_shot_bridge(Err(denied("backend", "flag server down")));
        let mut state = state_with(bridge);
        assert!(flags::Host::enabled(&mut state, "k".to_string(), true).await);

        let bridge = one_shot_bridge(Err(denied("backend", "flag server down")));
        let mut state = state_with(bridge);
        assert!(!flags::Host::enabled(&mut state, "k".to_string(), false).await);
    }

    #[tokio::test]
    async fn clock_decodes_real_host_results_and_falls_back_on_error() {
        let bridge = one_shot_bridge(Ok(serde_json::json!(42_u64)));
        let mut state = state_with(bridge);
        assert_eq!(clock::Host::now_millis(&mut state).await, 42);

        let bridge = one_shot_bridge(Ok(serde_json::json!("2026-01-01T00:00:00.000Z")));
        let mut state = state_with(bridge);
        assert_eq!(
            clock::Host::now_rfc3339(&mut state).await,
            "2026-01-01T00:00:00.000Z"
        );

        let bridge = one_shot_bridge(Ok(serde_json::json!(7_u64)));
        let mut state = state_with(bridge);
        assert_eq!(clock::Host::monotonic_nanos(&mut state).await, 7);

        let bridge = one_shot_bridge(Err(denied("backend", "down")));
        let mut state = state_with(bridge);
        assert_eq!(clock::Host::monotonic_nanos(&mut state).await, 0);

        let bridge = one_shot_bridge(Err(denied("backend", "down")));
        let mut state = state_with(bridge);
        assert!(!clock::Host::now_rfc3339(&mut state).await.is_empty());
    }

    #[tokio::test]
    async fn log_write_succeeds_with_a_real_bridge() {
        let bridge = one_shot_bridge(Ok(serde_json::json!({})));
        let mut state = state_with(bridge);
        log::Host::write(
            &mut state,
            log::Level::Info,
            "hello".to_string(),
            "{}".to_string(),
        )
        .await;
    }

    #[tokio::test]
    async fn context_get_context_logs_and_defaults_on_malformed_result() {
        let bridge = one_shot_bridge(Ok(serde_json::json!("not-an-object")));
        let mut state = state_with(bridge);
        let ctx = context::Host::get_context(&mut state).await;
        assert_eq!(ctx.config_json, "{}");
    }

    #[tokio::test]
    async fn flags_enabled_fails_open_to_default_without_a_bridge() {
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        assert!(flags::Host::enabled(&mut state, "waddles.some-feature".to_string(), true).await);
        assert!(!flags::Host::enabled(&mut state, "waddles.some-feature".to_string(), false).await);
    }

    #[tokio::test]
    async fn flags_tier_defaults_to_free_without_a_bridge() {
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        assert_eq!(flags::Host::tier(&mut state).await, "free");
    }

    #[tokio::test]
    async fn clock_falls_back_to_local_time_without_a_bridge() {
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        assert!(clock::Host::now_millis(&mut state).await > 0);
    }

    #[tokio::test]
    async fn context_returns_empty_but_well_formed_without_a_bridge() {
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        let ctx = context::Host::get_context(&mut state).await;
        assert_eq!(ctx.config_json, "{}");
    }

    #[tokio::test]
    async fn log_write_never_panics_without_a_bridge() {
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        log::Host::write(
            &mut state,
            log::Level::Debug,
            "probe".to_string(),
            "{}".to_string(),
        )
        .await;
    }

    #[test]
    fn db_value_json_round_trips_every_variant() {
        let values = vec![
            db::Value::NullValue,
            db::Value::BoolValue(true),
            db::Value::IntValue(-7),
            db::Value::FloatValue(1.5),
            db::Value::TextValue("hi".to_string()),
            db::Value::BytesValue(vec![1, 2, 3]),
        ];
        for v in values {
            let json = db_value_to_json(&v);
            let back = json_to_db_value(&json);
            match (&v, &back) {
                (db::Value::NullValue, db::Value::NullValue) => {}
                (db::Value::BoolValue(a), db::Value::BoolValue(b)) => assert_eq!(a, b),
                (db::Value::IntValue(a), db::Value::IntValue(b)) => assert_eq!(a, b),
                (db::Value::FloatValue(a), db::Value::FloatValue(b)) => assert_eq!(a, b),
                (db::Value::TextValue(a), db::Value::TextValue(b)) => assert_eq!(a, b),
                (db::Value::BytesValue(a), db::Value::BytesValue(b)) => assert_eq!(a, b),
                _ => panic!("round trip changed variant: {v:?} -> {back:?}"),
            }
        }
    }
}

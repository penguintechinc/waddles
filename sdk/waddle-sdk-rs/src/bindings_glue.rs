//! `wasm32`-only glue: the single `wit_bindgen::generate!` invocation for
//! the normative `waddle:bundle/stage@1.0.0` world -- imported by relative
//! path from `wit/waddle-bundle/stage.wit`, never forked, per that file's
//! own header comment ("Every consumer ... references this exact file by
//! path ... and never inlines a copy") -- plus the mechanical conversions
//! between this crate's idiomatic types (`crate::types`, `crate::http`,
//! `crate::kv`, `crate::db`, `crate::relay`, `crate::flags`, `crate::log`,
//! `crate::context`) and the WIT-generated records/variants.
//!
//! This module, and the thin host-call wrapper functions in each
//! capability module that delegate to it, only compile under
//! `#[cfg(target_arch = "wasm32")]`. There is no wasmtime-hosted test
//! harness for this crate, so none of this file's actual host-call sites
//! run under `cargo test` on a host target -- it is entirely absent from
//! that compilation, the same way `core/svc_process`'s `main.rs` is
//! excluded from its coverage gate as a "thin, mechanically-verified-by-
//! a-downstream-build" seam. The field-mapping conversions here are
//! exercised for real by `bundles/rust/example`'s `cargo component
//! build` + `wasm-tools component wit` check.

#![cfg(target_arch = "wasm32")]

wit_bindgen::generate!({
    world: "stage",
    path: "../../wit/waddle-bundle",
    // The generated `export!` macro must be usable from a downstream
    // bundle crate (via `waddle_sdk::export_stage!`, `crate::stage`),
    // not just from this crate -- default `pub(crate)` visibility would
    // make it inaccessible outside `waddle-sdk-rs`.
    pub_export_macro: true,
});

use self::waddle::bundle::clock as wit_clock;
use self::waddle::bundle::context as wit_context;
use self::waddle::bundle::db as wit_db;
use self::waddle::bundle::flags as wit_flags;
use self::waddle::bundle::http as wit_http;
use self::waddle::bundle::kv as wit_kv;
use self::waddle::bundle::log as wit_log;
use self::waddle::bundle::relay as wit_relay;
use self::waddle::bundle::types as wit_types;

use crate::context::BundleContext;
use crate::db::{Rows, Value as DbValue};
use crate::error::{DbError, HttpError, KvError, RelayError, SdkError};
use crate::http::{Header, Request, Response};
use crate::log::Level;
use crate::types::{
    PlatformEvent, StageEnvelope, TransportError, TransportResult, UnsupportedStage,
};

// -- context --------------------------------------------------------------

pub(crate) fn get_context() -> BundleContext {
    let raw = wit_context::get_context();
    BundleContext {
        tenant: raw.tenant,
        community: raw.community,
        app_id: raw.app_id,
        feature: raw.feature,
        version: raw.version,
        message_id: raw.message_id,
        config_json: raw.config_json,
    }
}

// -- http -------------------------------------------------------------------

pub(crate) fn http_send(req: Request) -> Result<Response, SdkError> {
    let wit_req = wit_http::Request {
        method: req.method,
        url: req.url,
        headers: req
            .headers
            .into_iter()
            .map(|h| wit_http::Header {
                name: h.name,
                value: h.value,
            })
            .collect(),
        body: req.body,
        secret_refs: req.secret_refs,
    };
    wit_http::send(&wit_req)
        .map(|resp| Response {
            status: resp.status,
            headers: resp
                .headers
                .into_iter()
                .map(|h| Header {
                    name: h.name,
                    value: h.value,
                })
                .collect(),
            body: resp.body,
            truncated: resp.truncated,
        })
        .map_err(|err| SdkError::Http(convert_http_error(err)))
}

fn convert_http_error(err: wit_http::Error) -> HttpError {
    match err {
        wit_http::Error::Denied(s) => HttpError::Denied(s),
        wit_http::Error::Timeout => HttpError::Timeout,
        wit_http::Error::TooLarge(n) => HttpError::TooLarge(n),
        wit_http::Error::RateLimited(n) => HttpError::RateLimited(n),
        wit_http::Error::Transport(s) => HttpError::Transport(s),
    }
}

// -- kv -----------------------------------------------------------------

pub(crate) fn kv_get(key: &str) -> Result<Option<Vec<u8>>, SdkError> {
    wit_kv::get(key).map_err(|e| SdkError::Kv(convert_kv_error(e)))
}

pub(crate) fn kv_set(key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), SdkError> {
    wit_kv::set(key, value, ttl_seconds).map_err(|e| SdkError::Kv(convert_kv_error(e)))
}

pub(crate) fn kv_delete(key: &str) -> Result<(), SdkError> {
    wit_kv::delete(key).map_err(|e| SdkError::Kv(convert_kv_error(e)))
}

pub(crate) fn kv_increment(key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, SdkError> {
    wit_kv::increment(key, delta, ttl_seconds).map_err(|e| SdkError::Kv(convert_kv_error(e)))
}

fn convert_kv_error(err: wit_kv::Error) -> KvError {
    match err {
        wit_kv::Error::TooLarge(n) => KvError::TooLarge(n),
        wit_kv::Error::Backend(s) => KvError::Backend(s),
    }
}

// -- db -------------------------------------------------------------------

pub(crate) fn db_execute(statement: &str, params: &[DbValue]) -> Result<Rows, SdkError> {
    let wit_params: Vec<wit_db::Value> = params.iter().map(value_to_wit).collect();
    wit_db::execute(statement, &wit_params)
        .map(|rows| Rows {
            columns: rows.columns,
            rows: rows
                .rows
                .into_iter()
                .map(|row| row.into_iter().map(value_from_wit).collect())
                .collect(),
            rows_affected: rows.rows_affected,
        })
        .map_err(|err| SdkError::Db(convert_db_error(err)))
}

fn value_to_wit(v: &DbValue) -> wit_db::Value {
    match v {
        DbValue::Null => wit_db::Value::NullValue,
        DbValue::Bool(b) => wit_db::Value::BoolValue(*b),
        DbValue::Int(i) => wit_db::Value::IntValue(*i),
        DbValue::Float(f) => wit_db::Value::FloatValue(*f),
        DbValue::Text(s) => wit_db::Value::TextValue(s.clone()),
        DbValue::Bytes(b) => wit_db::Value::BytesValue(b.clone()),
    }
}

fn value_from_wit(v: wit_db::Value) -> DbValue {
    match v {
        wit_db::Value::NullValue => DbValue::Null,
        wit_db::Value::BoolValue(b) => DbValue::Bool(b),
        wit_db::Value::IntValue(i) => DbValue::Int(i),
        wit_db::Value::FloatValue(f) => DbValue::Float(f),
        wit_db::Value::TextValue(s) => DbValue::Text(s),
        wit_db::Value::BytesValue(b) => DbValue::Bytes(b),
    }
}

fn convert_db_error(err: wit_db::Error) -> DbError {
    match err {
        wit_db::Error::Denied(s) => DbError::Denied(s),
        wit_db::Error::Syntax(s) => DbError::Syntax(s),
        wit_db::Error::Conflict(s) => DbError::Conflict(s),
        wit_db::Error::Timeout => DbError::Timeout,
        wit_db::Error::Backend(s) => DbError::Backend(s),
    }
}

// -- relay ------------------------------------------------------------------

pub(crate) fn relay_push(provider: &str, message_json: &str) -> Result<(), SdkError> {
    wit_relay::push(provider, message_json).map_err(|e| SdkError::Relay(convert_relay_error(e)))
}

fn convert_relay_error(err: wit_relay::Error) -> RelayError {
    match err {
        wit_relay::Error::Denied(s) => RelayError::Denied(s),
        wit_relay::Error::Backend(s) => RelayError::Backend(s),
    }
}

// -- flags ------------------------------------------------------------------

pub(crate) fn flags_enabled(key: &str, default_value: bool) -> bool {
    wit_flags::enabled(key, default_value)
}

pub(crate) fn flags_tier() -> String {
    wit_flags::tier()
}

// -- log --------------------------------------------------------------------

pub(crate) fn log_write(level: Level, message: &str, fields_json: &str) {
    let wit_level = match level {
        Level::Error => wit_log::Level::Error,
        Level::Warn => wit_log::Level::Warn,
        Level::Info => wit_log::Level::Info,
        Level::Debug => wit_log::Level::Debug,
    };
    wit_log::write(wit_level, message, fields_json);
}

// -- clock ------------------------------------------------------------------

pub(crate) fn clock_now_millis() -> u64 {
    wit_clock::now_millis()
}

pub(crate) fn clock_now_rfc3339() -> String {
    wit_clock::now_rfc3339()
}

pub(crate) fn clock_monotonic_nanos() -> u64 {
    wit_clock::monotonic_nanos()
}

// -- stage export type conversions ------------------------------------------
//
// Used by `crate::stage`'s `run_transform`/`run_dispatch` (invoked from the
// `export_stage!` macro's `Guest` impls) to translate between the
// WIT-generated export types and this crate's idiomatic `crate::types`.

pub(crate) fn platform_event_from_wit(e: wit_types::PlatformEvent) -> PlatformEvent {
    PlatformEvent {
        platform: e.platform,
        event_type: e.event_type,
        actor: e.actor,
        payload_json: e.payload_json,
        occurred_at: e.occurred_at,
    }
}

pub(crate) fn platform_event_to_wit(e: PlatformEvent) -> wit_types::PlatformEvent {
    wit_types::PlatformEvent {
        platform: e.platform,
        event_type: e.event_type,
        actor: e.actor,
        payload_json: e.payload_json,
        occurred_at: e.occurred_at,
    }
}

pub(crate) fn unsupported_stage_to_wit(u: UnsupportedStage) -> wit_types::UnsupportedStage {
    wit_types::UnsupportedStage { stage: u.stage }
}

pub(crate) fn stage_envelope_from_wit(e: wit_types::StageEnvelope) -> StageEnvelope {
    StageEnvelope {
        tenant: e.tenant,
        community: e.community,
        app_id: e.app_id,
        stage: e.stage,
        event: platform_event_from_wit(e.event),
        ts: e.ts,
        target_app_id: e.target_app_id,
        trace_context: e.trace_context,
    }
}

pub(crate) fn transport_result_to_wit(r: TransportResult) -> wit_types::TransportResult {
    wit_types::TransportResult {
        ok: r.ok,
        status: r.status,
        detail: r.detail,
        provider_message_id: r.provider_message_id,
    }
}

pub(crate) fn transport_error_to_wit(e: TransportError) -> wit_types::TransportError {
    wit_types::TransportError {
        retryable: e.retryable,
        code: e.code,
        message: e.message,
        retry_after_ms: e.retry_after_ms,
    }
}

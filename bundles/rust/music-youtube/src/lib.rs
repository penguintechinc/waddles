//! Music YouTube SERVICE bundle.
//!
//! An always-on `kind: service` bundle on `world stage-tools` exporting
//! `tool-provider` with the common music tool contract
//! (`music.youtube.{search,track,playlist.get,resolve}`). All logic lives in
//! [`logic`] / [`parse`] (host-testable behind three seams); this file only supplies
//! the real wasm implementations of those seams -- the generated `http`/`kv`/`clock`
//! imports -- and routes `tool-provider.call` through the SDK registry. Unknown
//! tools fail LOUD with a typed `invalid-args`. Credentials are secret REFS the host
//! resolves; they never enter this component.
#[allow(warnings)]
mod bindings;
mod logic;
mod parse;

use std::sync::{Arc, OnceLock};

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::ToolError as WitError;
use bindings::waddle::bundle::{clock, http, kv, log};
use logic::{Clock, HttpFail, HttpHost, HttpResp, KvHost, Service};
use waddle_sdk::tool::{ToolError, ToolRegistry};

struct Component;

/// Real guarded host HTTP.
struct WasmHttp;

impl HttpHost for WasmHttp {
    /// GET through the host egress guard; secret refs are injected host-side.
    fn get(&self, url: &str, secret_refs: &[(String, String)]) -> Result<HttpResp, HttpFail> {
        let req = http::Request {
            method: "GET".to_string(),
            url: url.to_string(),
            headers: vec![http::Header {
                name: "accept".to_string(),
                value: "application/json".to_string(),
            }],
            body: None,
            secret_refs: secret_refs.to_vec(),
        };
        match http::send(&req) {
            Ok(r) => Ok(HttpResp {
                status: r.status,
                body: r.body,
                truncated: r.truncated,
            }),
            Err(http::Error::Denied(c)) => Err(HttpFail::Denied(c)),
            Err(http::Error::Timeout) => Err(HttpFail::Timeout),
            Err(http::Error::TooLarge(_)) => Err(HttpFail::TooLarge),
            Err(http::Error::RateLimited(s)) => Err(HttpFail::RateLimited(s)),
            Err(http::Error::Transport(_)) => Err(HttpFail::Transport),
        }
    }
}

/// The bundle's own kv (host-scoped to this community + app).
struct WasmKv;

impl KvHost for WasmKv {
    /// Raw kv read; the error never carries host detail.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        kv::get(key).map_err(|_| "kv unavailable".to_string())
    }
    /// Raw kv write with TTL.
    fn set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), String> {
        kv::set(key, value, ttl_seconds).map_err(|_| "kv unavailable".to_string())
    }
    /// Atomic kv add.
    fn increment(&self, key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, String> {
        kv::increment(key, delta, ttl_seconds).map_err(|_| "kv unavailable".to_string())
    }
}

/// Host wall clock.
struct WasmClock;

impl Clock for WasmClock {
    /// Milliseconds since the Unix epoch as the stage sees it.
    fn now_millis(&self) -> u64 {
        clock::now_millis()
    }
}

/// Maps the SDK tool error onto the WIT one.
fn to_wit(e: ToolError) -> WitError {
    match e {
        ToolError::NotGranted => WitError::NotGranted,
        ToolError::Forbidden => WitError::Forbidden,
        ToolError::Timeout => WitError::Timeout,
        ToolError::DepthExceeded => WitError::DepthExceeded,
        ToolError::PoolExhausted => WitError::PoolExhausted,
        ToolError::TargetUnavailable => WitError::TargetUnavailable,
        ToolError::InvalidArgs(m) => WitError::InvalidArgs(m),
        ToolError::NoTransport => WitError::InvalidArgs("no tool transport installed".to_string()),
    }
}

/// Emits one PII-free line: a known tool label and outcome code only -- never args
/// (queries/urls can carry user text), never a caller-supplied unknown name.
fn log_outcome(tool: &str, outcome: &str) {
    let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
    log::write(log::Level::Debug, "music-youtube tool call", &fields);
}

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

fn registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| {
        logic::build_registry(Arc::new(Service::new(
            Arc::new(WasmHttp),
            Arc::new(WasmKv),
            Arc::new(WasmClock),
        )))
    })
}

impl Guest for Component {
    /// Routes `name` through the tool table; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        let result = registry().call(&name, &args);
        log_outcome(
            logic::tool_log_label(&name),
            match &result {
                Ok(_) => "ok",
                Err(ToolError::InvalidArgs(m)) => logic::outcome_code(m),
                Err(ToolError::NotGranted) => "not_granted",
                Err(ToolError::Forbidden) => "forbidden",
                Err(ToolError::TargetUnavailable) => "unavailable",
                Err(ToolError::Timeout) => "timeout",
                Err(_) => "error",
            },
        );
        result.map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

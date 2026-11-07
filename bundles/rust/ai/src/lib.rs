//! AI SERVICE bundle (catalog `ai`, P0b).
//!
//! An always-on `kind: service` bundle on `world stage-tools`: it exports
//! `tool-provider` and wraps the host `ai.generate` capability (model choice,
//! BYOK/tier gating and PII rejection are host-side; no key, model name or URL
//! lives here). All tool logic lives in [`logic`] (host-testable behind three
//! seams); this file only supplies the real wasm implementations of those seams
//! and routes `tool-provider.call` through the SDK registry. Unknown tools fail
//! LOUD with a typed `invalid-args`.
#[allow(warnings)]
mod bindings;
mod logic;

use std::sync::{Arc, OnceLock};

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::ToolError as WitError;
use bindings::waddle::bundle::{ai, clock, kv, log};
use logic::{AiError, AiHost, AiService, Clock, GenOpts, Store};
use waddle_sdk::tool::{ToolError, ToolRegistry};

struct Component;

/// The real `ai` host capability.
struct WasmAi;

impl AiHost for WasmAi {
    /// Calls `ai.generate`, mapping the WIT error onto the logic error.
    fn generate(&self, prompt: &str, opts: &GenOpts) -> Result<String, AiError> {
        let o = ai::Options {
            max_tokens: opts.max_tokens,
            temperature: opts.temperature,
            json: opts.json,
        };
        ai::generate(prompt, o).map_err(|e| match e {
            ai::Error::Denied(c) => AiError::Denied(c),
            ai::Error::TooManyTokens(n) => AiError::TooManyTokens(n),
            ai::Error::ContentFiltered => AiError::ContentFiltered,
            ai::Error::Invalid(m) => AiError::Invalid(m),
            ai::Error::Unavailable(m) => AiError::Unavailable(m),
            ai::Error::Backend(m) => AiError::Backend(m),
        })
    }
}

/// The bundle's own kv (host-scoped to this community + app).
struct WasmKv;

impl Store for WasmKv {
    /// Raw kv read; the error never carries host detail.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        kv::get(key).map_err(|_| "kv unavailable".to_string())
    }

    /// Atomic kv increment (quota counters).
    fn increment(&self, key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, String> {
        kv::increment(key, delta, ttl_seconds).map_err(|_| "kv unavailable".to_string())
    }
}

/// The host clock import.
struct WasmClock;

impl Clock for WasmClock {
    /// Seconds since the Unix epoch.
    fn now_secs(&self) -> u64 {
        clock::now_millis() / 1000
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

/// Emits one PII-free line: a known tool label and outcome code only -- never
/// args, prompts, completions, or a caller-supplied unknown name.
fn log_outcome(tool: &str, outcome: &str, out_bytes: usize) {
    let fields =
        serde_json::json!({ "tool": tool, "outcome": outcome, "out_bytes": out_bytes }).to_string();
    log::write(log::Level::Debug, "ai tool call", &fields);
}

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

fn registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| {
        let svc = AiService::new(Arc::new(WasmAi), Arc::new(WasmKv), Arc::new(WasmClock));
        logic::build_registry(Arc::new(svc))
    })
}

impl Guest for Component {
    /// Routes `name` through the tool table; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        let result = registry().call(&name, &args);
        let (outcome, n) = match &result {
            Ok(b) => ("ok", b.len()),
            Err(ToolError::InvalidArgs(m)) => (logic::outcome_code(m), 0),
            Err(ToolError::NotGranted) => ("not_granted", 0),
            Err(ToolError::Forbidden) => ("forbidden", 0),
            Err(_) => ("error", 0),
        };
        log_outcome(logic::tool_log_label(&name), outcome, n);
        result.map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

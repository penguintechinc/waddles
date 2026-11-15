//! Game-server SERVICE bundle (`world stage-tools`).
//!
//! Exports `tool-provider` and routes `call` through the tool table in
//! [`logic`]. This file only supplies the real wasm implementations of the
//! [`logic::Env`] seam: the generated `kv`/`log`/`clock` imports and the
//! `gameserver-net` capability -- which the host does not provide yet, so the
//! network seam is [`net::UnavailableNet`]: every network tool fails LOUD
//! (`target-unavailable`) until the host implements the contract in README.md
//! ("Needs from S0"). There is deliberately no fallback and no raw socket.
#[allow(warnings)]
mod bindings;
mod logic;
mod net;
mod proto;

use std::sync::{Arc, OnceLock};

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::ToolError as WitError;
use bindings::waddle::bundle::{clock, kv, log};
use logic::{Env, Level};
use net::{GameNet, UnavailableNet};
use serde_json::Value;
use waddle_sdk::tool::{ToolError, ToolRegistry};

struct Component;

/// The real host environment.
struct WasmEnv {
    net: UnavailableNet,
}

impl Env for WasmEnv {
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
        kv::get(key).map_err(|_| "kv unavailable".to_string())
    }
    fn kv_set(&self, key: &str, value: &[u8]) -> Result<(), String> {
        kv::set(key, value, 0).map_err(|_| "kv unavailable".to_string())
    }
    fn kv_delete(&self, key: &str) -> Result<(), String> {
        kv::delete(key).map_err(|_| "kv unavailable".to_string())
    }
    fn kv_incr(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String> {
        kv::increment(key, delta, ttl_s).map_err(|_| "kv unavailable".to_string())
    }
    fn log(&self, level: Level, message: &str, fields: &Value) {
        let lvl = match level {
            Level::Info => log::Level::Info,
            Level::Warn => log::Level::Warn,
        };
        log::write(lvl, message, &fields.to_string());
    }
    fn now_millis(&self) -> u64 {
        clock::now_millis()
    }
    fn net(&self) -> &dyn GameNet {
        &self.net
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

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

fn registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| {
        logic::build_registry(Arc::new(WasmEnv {
            net: UnavailableNet,
        }))
    })
}

impl Guest for Component {
    /// Routes `name` through the tool table; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        let result = registry().call(&name, &args);
        let outcome = match &result {
            Ok(_) => "ok",
            Err(ToolError::InvalidArgs(_)) => "invalid_args",
            Err(ToolError::NotGranted) => "not_granted",
            Err(ToolError::Forbidden) => "forbidden",
            Err(_) => "error",
        };
        let fields = serde_json::json!({"tool": logic::tool_log_label(&name), "outcome": outcome});
        log::write(
            log::Level::Debug,
            "gameserver tool call",
            &fields.to_string(),
        );
        result.map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

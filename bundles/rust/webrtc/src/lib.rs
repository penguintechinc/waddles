//! WebRTC SERVICE bundle.
//!
//! An always-on `kind: service` bundle on `world stage-tools-webrtc`: it exports
//! `tool-provider` (the surface app bundles reach through `tool.invoke`) and wraps
//! the `webrtc` host capability served by svc-streaming. All tool logic lives in
//! [`logic`] (host-testable behind one seam); this file only supplies the real wasm
//! implementation of that seam and routes `tool-provider.call` through the SDK
//! registry. The media path, TURN/ICE config and the token-signing secret never
//! enter the guest. Unknown tools fail LOUD with a typed `invalid-args`.
#[allow(warnings)]
mod bindings;
mod logic;

use std::sync::{Arc, OnceLock};

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::ToolError as WitError;
use bindings::waddle::bundle::{log, webrtc};
use logic::{HostError, Media};
use waddle_sdk::tool::{ToolError, ToolRegistry};

struct Component;

/// The real `webrtc` host capability.
struct WasmMedia;

fn host_err(e: webrtc::Error) -> HostError {
    match e {
        webrtc::Error::Denied(c) => HostError::Denied(c),
        webrtc::Error::NotFound => HostError::NotFound,
        webrtc::Error::Invalid(m) => HostError::Invalid(m),
        webrtc::Error::QuotaExceeded => HostError::QuotaExceeded,
        webrtc::Error::NotImplemented(m) => HostError::NotImplemented(m),
        webrtc::Error::Unavailable(m) => HostError::Unavailable(m),
        webrtc::Error::Backend(m) => HostError::Backend(m),
    }
}

impl Media for WasmMedia {
    /// One control op over the host capability (args/result are JSON text).
    fn call(&self, op: &str, args: &serde_json::Value) -> Result<serde_json::Value, HostError> {
        let out = webrtc::call(op, &args.to_string()).map_err(host_err)?;
        serde_json::from_str(&out)
            .map_err(|_| HostError::Backend("malformed host result".to_string()))
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

/// One PII-free line: a known tool label and outcome only -- never args or tokens.
fn log_outcome(tool: &str, outcome: &str) {
    let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
    log::write(log::Level::Debug, "webrtc tool call", &fields);
}

static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

fn registry() -> &'static ToolRegistry {
    REGISTRY.get_or_init(|| logic::build_registry(Arc::new(WasmMedia)))
}

impl Guest for Component {
    /// Routes `name` through the tool table; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        let result = registry().call(&name, &args);
        log_outcome(
            logic::tool_log_label(&name),
            match &result {
                Ok(_) => "ok",
                Err(ToolError::InvalidArgs(_)) => "invalid_args",
                Err(ToolError::NotGranted) => "not_granted",
                Err(ToolError::Forbidden) => "forbidden",
                Err(_) => "error",
            },
        );
        result.map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

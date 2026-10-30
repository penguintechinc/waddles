//! P3b real-path CALLER fixture: an app bundle on `world stage-tools` that
//! IMPORTS `tool` and invokes other bundles' tools through the REAL
//! `waddle-sdk-rs` client (`invoke`). Its single exported tool, `go`, takes
//! `{target, tool, args, key?}` and forwards it, passing the host's typed
//! `tool-error` straight back out so a test sees the exact refusal. (`stage-tools`
//! is the only world that carries the `tool` import, so the app also exports
//! `tool-provider`; the stage never routes tool calls to an app -- it is only
//! the harness's entry point for starting a call chain.)
#[allow(warnings)]
mod bindings;

use std::sync::Once;

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::{self, ToolError as WitError};
use serde_json::Value;
use waddle_sdk::tool::{
    ToolContext, ToolError, ToolTransport, dispatch, install_transport, invoke, register_tool,
};

struct Component;

/// Adapts the generated `tool.invoke` import to the SDK's transport seam.
struct HostTransport;

impl ToolTransport for HostTransport {
    fn invoke(
        &self,
        target: &str,
        tool_name: &str,
        args: &[u8],
        idempotency_key: Option<&str>,
    ) -> Result<Vec<u8>, ToolError> {
        tool::invoke(target, tool_name, args, idempotency_key).map_err(from_wit)
    }
}

fn from_wit(e: WitError) -> ToolError {
    match e {
        WitError::NotGranted => ToolError::NotGranted,
        WitError::Forbidden => ToolError::Forbidden,
        WitError::Timeout => ToolError::Timeout,
        WitError::DepthExceeded => ToolError::DepthExceeded,
        WitError::PoolExhausted => ToolError::PoolExhausted,
        WitError::TargetUnavailable => ToolError::TargetUnavailable,
        WitError::InvalidArgs(m) => ToolError::InvalidArgs(m),
    }
}

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

fn str_arg(v: &Value, key: &str) -> Result<String, ToolError> {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| ToolError::InvalidArgs(format!("missing string field {key}")))
}

/// One-time wiring: the real transport + the `go` tool.
fn init() {
    static INIT: Once = Once::new();
    INIT.call_once(|| {
        install_transport(Box::new(HostTransport));
        register_tool("go", |_ctx: &ToolContext, args: Value| {
            let target = str_arg(&args, "target")?;
            let tool_name = str_arg(&args, "tool")?;
            let inner = args.get("args").cloned().unwrap_or(Value::Null);
            let key = args.get("key").and_then(Value::as_str).map(str::to_string);
            let bytes = invoke(&target, &tool_name, &inner, key)?;
            serde_json::from_slice(&bytes).map_err(|e| ToolError::InvalidArgs(e.to_string()))
        });
    });
}

impl Guest for Component {
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        init();
        dispatch(&name, &args).map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

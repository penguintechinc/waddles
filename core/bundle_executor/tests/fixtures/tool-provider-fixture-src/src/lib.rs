//! P3b real-path PROVIDER fixture: a service bundle on `world stage-tools`
//! that exports `tool-provider` and serves its tools through the REAL
//! `waddle-sdk-rs` `ToolRegistry`. The SDK's own `bindings_glue` only
//! generates the frozen `stage` world, so this crate adapts ITS generated
//! `stage-tools` bindings to the SDK's `ToolTransport` seam (the pattern
//! `sdk/waddle-sdk-rs/src/tool.rs` documents).
//!
//! Tools: `echo` (read, returns its args), `guarded` (WRITE: needs the
//! host-forwarded idempotency key and echoes it back), `relay` (nested
//! `tool.invoke` to another bundle -- cycle/depth/self-call probes) and
//! `slow` (spins forever; only the host/epoch deadline ends it).
#[allow(warnings)]
mod bindings;

use std::sync::Once;

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::{self, ToolError as WitError};
use serde_json::{Value, json};
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

/// One-time wiring: the real transport + the tool table.
fn init() {
    static INIT: Once = Once::new();
    INIT.call_once(|| {
        install_transport(Box::new(HostTransport));
        register_tool("echo", |_ctx: &ToolContext, args: Value| Ok(args));
        register_tool("guarded", |ctx: &ToolContext, args: Value| {
            let key = ctx
                .idempotency_key
                .clone()
                .ok_or_else(|| ToolError::InvalidArgs("idempotency key required".to_string()))?;
            Ok(json!({"guarded": true, "idempotency_key": key, "args": args}))
        });
        // Echoes the HOST-VERIFIED actor/scopes the SDK handed it (the args it
        // sees must NOT contain the reserved `__ctx`).
        register_tool("whoami", |ctx: &ToolContext, args: Value| {
            Ok(json!({"actor": ctx.actor, "scopes": ctx.actor_scopes, "args": args}))
        });
        register_tool("relay", |_ctx: &ToolContext, args: Value| {
            let target = str_arg(&args, "target")?;
            let tool_name = str_arg(&args, "tool")?;
            let inner = args.get("args").cloned().unwrap_or(Value::Null);
            let bytes = invoke(&target, &tool_name, &inner, None)?;
            let value: Value = serde_json::from_slice(&bytes)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
            Ok(json!({"relayed": value}))
        });
        register_tool(
            "slow",
            |_ctx: &ToolContext, _args: Value| -> Result<Value, ToolError> {
                loop {
                    std::hint::black_box(());
                }
            },
        );
    });
}

impl Guest for Component {
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        init();
        dispatch(&name, &args).map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

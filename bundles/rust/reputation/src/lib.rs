//! Reputation SERVICE bundle skeleton (bundle-MCP-tools P5 Task 2).
//!
//! An always-on `kind: service` bundle on `world stage-tools`: it exports
//! `tool-provider` (the surface other bundles reach through `tool.invoke`) and
//! imports the `reputation` host capability it will wrap. The tool table is
//! empty until Task 3; an unknown tool fails LOUD with a typed
//! `invalid-args`, never a default answer. Adapts this crate's generated
//! `stage-tools` bindings to the SDK's `ToolTransport` seam, the pattern
//! `sdk/waddle-sdk-rs/src/tool.rs` documents.
#[allow(warnings)]
mod bindings;

use std::sync::Once;

use bindings::exports::waddle::bundle::tool_provider::Guest;
use bindings::waddle::bundle::tool::{self, ToolError as WitError};
use waddle_sdk::tool::{ToolError, ToolTransport, dispatch, install_transport};

struct Component;

/// Adapts the generated `tool.invoke` import to the SDK's transport seam.
struct HostTransport;

impl ToolTransport for HostTransport {
    /// Forwards a nested tool call to the host (the sole authority).
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

/// Maps the WIT tool error onto the SDK's.
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

/// One-time wiring: installs the real transport. Reputation tools register
/// here in Task 3.
fn init() {
    static INIT: Once = Once::new();
    INIT.call_once(|| {
        install_transport(Box::new(HostTransport));
    });
}

impl Guest for Component {
    /// Dispatches `name` through the SDK registry; unknown tools are a typed error.
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        init();
        dispatch(&name, &args).map_err(to_wit)
    }
}

bindings::export!(Component with_types_in bindings);

//! P6a T4 real-path fixture: a connector overlay on `world connector-tools`.
//! `receiver.on-frame` still normalizes frames (the connector contract is
//! unchanged), while `tool-provider.call` serves platform tools through the
//! REAL `waddle-sdk-rs` `ToolRegistry`, and the `tool` import is adapted to the
//! SDK's `ToolTransport` seam (same pattern as `tool-provider-fixture-src`).
//!
//! Tools: `get_chatters` (read; returns a fixed chatter-id list keyed by the
//! opaque `source_id` arg), `echo` (returns its args), `denied` (always
//! `not-granted`), and `relay` (nested `tool.invoke` to another bundle -- proves
//! the `tool` import is linked and answers a typed error, never a link failure).
#[allow(warnings)]
mod bindings;

use std::sync::Once;

use bindings::exports::waddle::bundle::tool_provider::Guest as ToolGuest;
use bindings::exports::waddle::connector::receiver::Guest as ReceiverGuest;
use bindings::exports::waddle::connector::sender::{Action, Guest as SenderGuest, Header, HttpRequestTpl};
use bindings::waddle::bundle::tool::{self, ToolError as WitError};
use bindings::waddle::connector::types::{
    ConnectionCtx, FrameError, HandshakePayload, NormalizedEvent, RawFrame,
};
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
        register_tool("get_chatters", |_ctx: &ToolContext, args: Value| {
            let source_id = str_arg(&args, "source_id")?;
            Ok(json!({"source_id": source_id, "chatters": ["chatter-a", "chatter-b"]}))
        });
        register_tool(
            "denied",
            |_ctx: &ToolContext, _args: Value| -> Result<Value, ToolError> {
                Err(ToolError::NotGranted)
            },
        );
        // Spins forever; only the host fuel/epoch deadline ends it (saturation +
        // deadline-clamp regression probes).
        register_tool(
            "slow",
            |_ctx: &ToolContext, _args: Value| -> Result<Value, ToolError> {
                loop {
                    std::hint::black_box(());
                }
            },
        );
        register_tool("relay", |_ctx: &ToolContext, args: Value| {
            let target = str_arg(&args, "target")?;
            let tool_name = str_arg(&args, "tool")?;
            let inner = args.get("args").cloned().unwrap_or(Value::Null);
            let bytes = invoke(&target, &tool_name, &inner, None)?;
            let value: Value = serde_json::from_slice(&bytes)
                .map_err(|e| ToolError::InvalidArgs(e.to_string()))?;
            Ok(json!({"relayed": value}))
        });
    });
}

impl ToolGuest for Component {
    fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
        init();
        dispatch(&name, &args).map_err(to_wit)
    }
}

impl ReceiverGuest for Component {
    fn on_connect(_ctx: ConnectionCtx) -> Result<HandshakePayload, FrameError> {
        Ok(HandshakePayload {
            bytes: b"tools-hello".to_vec(),
            secret_refs: Vec::new(),
        })
    }

    fn on_frame(frame: RawFrame) -> Result<Vec<NormalizedEvent>, FrameError> {
        let v: Value = serde_json::from_slice(&frame.bytes)
            .map_err(|e| FrameError::Malformed(format!("not JSON: {e}")))?;
        let text = v
            .get("text")
            .and_then(Value::as_str)
            .filter(|t| !t.is_empty())
            .ok_or_else(|| FrameError::Malformed("missing required 'text'".to_string()))?;
        Ok(vec![NormalizedEvent {
            platform: frame.ctx.platform,
            event_type: "message".to_string(),
            actor: None,
            payload_json: json!({ "text": text }).to_string(),
            occurred_at: frame.received_at,
        }])
    }

    fn on_heartbeat_due(_ctx: ConnectionCtx) -> Result<Option<Vec<u8>>, FrameError> {
        Ok(None)
    }

    fn on_disconnect(_ctx: ConnectionCtx, _reason: String) {}
}

/// Deterministic `sender.build-request` (P6a T6d real-path fixture). Behavior
/// is keyed on `action.kind`, so a test selects each wire outcome by input:
/// `chat.send` -> a template (target id in the URL, payload as body, one opaque
/// secret handle); `chat.bad` / `chat.backend` -> `malformed` / `backend`
/// frame-errors; `chat.huge` -> a ~950 KB body (over the host's result cap);
/// `chat.spin` -> an unbounded loop (fuel/deadline containment); anything
/// else -> `unsupported`.
impl SenderGuest for Component {
    fn build_request(action: Action) -> Result<HttpRequestTpl, FrameError> {
        match action.kind.as_str() {
            "chat.send" => Ok(HttpRequestTpl {
                method: "POST".to_string(),
                url: format!(
                    "https://api.example.test/v1/{}/messages",
                    action.target_source_id
                ),
                headers: vec![Header {
                    name: "Content-Type".to_string(),
                    value: "application/json".to_string(),
                }],
                body: Some(action.payload_json.into_bytes()),
                secret_refs: vec![("token".to_string(), "handle-1".to_string())],
            }),
            "chat.bad" => Err(FrameError::Malformed("bad action".to_string())),
            "chat.backend" => Err(FrameError::Backend("platform down".to_string())),
            "chat.huge" => Ok(HttpRequestTpl {
                method: "POST".to_string(),
                url: "https://api.example.test/v1/huge".to_string(),
                headers: Vec::new(),
                body: Some(vec![b'x'; 950_000]),
                secret_refs: Vec::new(),
            }),
            "chat.spin" => loop {
                std::hint::black_box(0u64);
            },
            _ => Err(FrameError::Unsupported(
                "connector-tools fixture sender: unsupported kind".to_string(),
            )),
        }
    }
}

bindings::export!(Component with_types_in bindings);

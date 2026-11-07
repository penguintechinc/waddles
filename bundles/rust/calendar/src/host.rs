//! Host seams the calendar logic is written against. The wasm component implements them over
//! the generated host imports; tests implement them in memory. Nothing here is mocked at the
//! boundary the bundle implements -- these are the boundaries the HOST owns.

use serde_json::Value;
use thiserror::Error;
use waddle_sdk::tool::ToolError;

/// Calendar-level errors, mapped to chat replies / admin error codes at the edge.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum CalError {
    /// Bad user input (message is safe to echo).
    #[error("{0}")]
    Validation(String),
    /// No such event (or not visible to the caller).
    #[error("no such event")]
    NotFound,
    /// The caller lacks the required authority.
    #[error("not allowed")]
    Forbidden,
    /// State forbids the operation (e.g. cancelling a rejected event).
    #[error("{0}")]
    Conflict(String),
    /// Storage backend failure or corrupt stored state; never silently defaulted.
    #[error("storage failure: {0}")]
    Storage(String),
}

/// Community-scoped key/value storage (host-scoped to community + app).
pub trait Kv {
    /// Reads a key.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, CalError>;
    /// Writes a key; `ttl_seconds == 0` means no expiry (the host clamps to its max TTL).
    fn set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), CalError>;
    /// Deletes a key.
    fn delete(&self, key: &str) -> Result<(), CalError>;
    /// Atomic increment; returns the new value.
    fn increment(&self, key: &str, delta: i64) -> Result<i64, CalError>;
}

/// Calls a service-bundle tool through the host (`tool.invoke`): gated, granted, host-mediated.
pub trait ToolInvoker {
    /// Invokes `tool` on `target` with JSON `args`; writes carry an idempotency key.
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &Value,
        idempotency_key: &str,
    ) -> Result<Value, ToolError>;
}

/// Wall clock (host-provided).
pub trait Clock {
    /// Unix seconds now.
    fn now_secs(&self) -> i64;
}

/// PII-free structured logging (ids / op / counts only; never titles, descriptions or handles).
pub trait Logger {
    /// Emits one line at `level` ("debug" | "info" | "warn" | "error") with JSON `fields`.
    fn log(&self, level: &str, msg: &str, fields: Value);
}

/// Everything the operations need, bundled per invocation.
pub struct Ctx<'a> {
    pub kv: &'a dyn Kv,
    pub tools: &'a dyn ToolInvoker,
    pub clock: &'a dyn Clock,
    pub log: &'a dyn Logger,
    pub tenant: &'a str,
    pub community: &'a str,
}

/// The verified identity behind an operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Caller {
    /// Community user UUID (canonical lower-case), if the platform identity is linked.
    pub actor: Option<String>,
    /// Mod/broadcaster badge or hub-gateway authorization: may approve/edit anything.
    pub approver: bool,
}

impl Caller {
    /// The actor UUID or a loud validation error (never falls back to a raw handle).
    pub fn require_actor(&self) -> Result<&str, CalError> {
        self.actor.as_deref().ok_or_else(|| {
            CalError::Validation(
                "this needs a linked Waddles identity; yours isn't linked yet".into(),
            )
        })
    }
}

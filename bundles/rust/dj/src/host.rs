//! The seam between the DJ engine and the host (kv, clock, tool invoke, logs).
//!
//! The engine is generic over [`Host`] so the exact same code runs natively
//! under test against an in-memory host and inside the component against the
//! real WIT imports (see `component` in `lib.rs`).

use serde_json::Value;

/// Mirrors the WIT `tool.tool-error` variant.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ToolErr {
    #[error("not granted")]
    NotGranted,
    #[error("forbidden")]
    Forbidden,
    #[error("timeout")]
    Timeout,
    #[error("depth exceeded")]
    DepthExceeded,
    #[error("pool exhausted")]
    PoolExhausted,
    #[error("target unavailable")]
    TargetUnavailable,
    #[error("invalid args: {0}")]
    InvalidArgs(String),
}

/// Log levels the host accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Error,
    Warn,
    Info,
    Debug,
}

/// Host capabilities the engine uses. All state is community-scoped by the host.
pub trait Host {
    /// Raw kv read.
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Raw kv write; `ttl_s == 0` means no expiry.
    fn kv_set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), String>;
    /// Raw kv delete.
    fn kv_delete(&self, key: &str) -> Result<(), String>;
    /// Milliseconds since the Unix epoch.
    fn now_ms(&self) -> u64;
    /// Gated bundle-to-bundle tool call (JSON in, JSON out).
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &Value,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ToolErr>;
    /// PII-free structured log line.
    fn log(&self, level: Level, message: &str, fields: Value);
}

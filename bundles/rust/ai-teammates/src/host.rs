//! The seam between teammate logic and the host. Production wires it to the WIT imports
//! (`lib.rs`); tests drive the REAL logic through a stateful in-memory host.

use waddle_sdk::tool::ToolError;

/// A host capability failure (kv/identity); the text never carries user content.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostError {
    /// The identity is not linked / not a member / not resolvable: tell the user, never guess.
    Identity(String),
    /// kv or another capability is unavailable.
    Backend(String),
}

/// Log severity.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Debug,
    Info,
    Warn,
    Error,
}

/// Everything teammate logic may ask of the host. Calls here are process-stage-only
/// capabilities (identity, tool, kv); the relay happens later in the action stage.
pub trait Host {
    /// Milliseconds since the Unix epoch.
    fn now_millis(&self) -> u64;
    /// Community-scoped kv read.
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, HostError>;
    /// Community-scoped kv write (`ttl_seconds` 0 = no expiry).
    fn kv_set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), HostError>;
    /// Atomic kv counter.
    fn kv_incr(&self, key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, HostError>;
    /// Community `user_uuid` of the triggering actor (host-derived, never caller-supplied).
    fn resolve_actor(&self) -> Result<String, HostError>;
    /// Invokes a service tool with canonical-JSON args.
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &[u8],
        idem: Option<&str>,
    ) -> Result<Vec<u8>, ToolError>;
    /// PII-free structured log line (`fields_json` is a JSON object of ids/op/counts only).
    fn log(&self, level: Level, msg: &str, fields_json: &str);
}

/// Per-invocation context from the host.
#[derive(Debug, Clone, Default)]
pub struct Ctx {
    /// Community the invocation belongs to (None for tenant-wide activations).
    pub community: Option<String>,
    /// Stable per-delivery id (idempotency / de-dupe key).
    pub message_id: String,
    /// Resolved 3-tier config JSON.
    pub config_json: String,
}

/// Encodes an arbitrary id into the kv key charset (`[A-Za-z0-9_.-]`, never `:`): other bytes
/// become `~xx` hex, so distinct ids never collide.
pub fn key_part(id: &str) -> String {
    let mut out = String::with_capacity(id.len());
    for b in id.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.') {
            out.push(b as char);
        } else {
            out.push_str(&format!("~{b:02x}"));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_parts_are_injective_and_colon_free() {
        assert_eq!(key_part("abc-1.2_x"), "abc-1.2_x");
        assert_eq!(key_part("a:b"), "a~3ab");
        assert_ne!(key_part("a:b"), key_part("a_b"));
        assert_ne!(key_part("a~3ab"), key_part("a:b"));
        assert!(!key_part("x:y/z é").contains(':'));
    }
}

//! Idiomatic wrapper over the WIT `log` interface (sanitized, levelled
//! logging into the stage's OTel pipeline; always granted;
//! `wit/waddle-bundle/stage.wit` `interface log`).
//!
//! The host sanitizes `fields-json` with the penguin logging
//! `SENSITIVE_KEYS` rule before emission (spec `interface log`'s doc
//! comment) -- this module never attempts its own redaction, so a bundle
//! author still should not deliberately pass secrets in `fields`.

use serde::Serialize;
use serde_json::{Map, Value};

use crate::error::SdkError;
use crate::types::to_canonical_json_object;

/// Mirrors WIT `enum level`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Error,
    Warn,
    Info,
    Debug,
}

/// A structured field map for one log line, built incrementally and
/// serialized to canonical JSON object text before crossing the WIT
/// boundary.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Fields(Map<String, Value>);

impl Fields {
    pub fn new() -> Self {
        Self::default()
    }

    /// Adds `key`/`value` (any `Serialize` type) to the field map.
    pub fn with(mut self, key: impl Into<String>, value: impl Serialize) -> Result<Self, SdkError> {
        let value = serde_json::to_value(value).map_err(SdkError::from)?;
        self.0.insert(key.into(), value);
        Ok(self)
    }

    #[cfg_attr(not(any(test, target_arch = "wasm32")), allow(dead_code))]
    pub(crate) fn to_json(&self) -> Result<String, SdkError> {
        to_canonical_json_object(&self.0)
    }
}

/// Emits one log line at `level` with `message` and structured `fields`.
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn write(level: Level, message: &str, fields: &Fields) -> Result<(), SdkError> {
    let fields_json = fields.to_json()?;
    crate::bindings_glue::log_write(level, message, &fields_json);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fields_builds_a_json_object() {
        let fields = Fields::new()
            .with("alias", "sr")
            .expect("serializes")
            .with("count", 3u32)
            .expect("serializes");
        let json = fields.to_json().expect("valid object");
        assert!(json.contains("\"alias\":\"sr\""));
        assert!(json.contains("\"count\":3"));
    }

    #[test]
    fn empty_fields_serializes_to_empty_object() {
        let json = Fields::new().to_json().expect("valid object");
        assert_eq!(json, "{}");
    }
}

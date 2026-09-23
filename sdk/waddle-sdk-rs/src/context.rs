//! Idiomatic wrapper over the WIT `context` interface (always-granted
//! capability; `wit/waddle-bundle/stage.wit` `interface context`).

use serde::de::DeserializeOwned;

use crate::error::SdkError;

/// The immutable, per-call scope every stage invocation carries. Mirrors
/// `record bundle-context`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BundleContext {
    pub tenant: String,
    pub community: Option<String>,
    pub app_id: String,
    pub feature: String,
    pub version: String,
    /// The Valkey stream entry id of the event being processed -- the
    /// de-duplication key a bundle records to stay idempotent under
    /// at-least-once redelivery (spec SS5.4).
    pub message_id: String,
    /// Resolved 3-tier config (activation > tenant availability > bundle
    /// default), as canonical JSON object text.
    pub config_json: String,
}

impl BundleContext {
    /// Deserializes [`Self::config_json`] into `T`. `T` is typically a
    /// `#[derive(Deserialize)]` struct describing the bundle's own
    /// `bundle.yaml` config schema.
    pub fn config<T: DeserializeOwned>(&self) -> Result<T, SdkError> {
        serde_json::from_str(&self.config_json).map_err(SdkError::from)
    }
}

/// Fetches the current call's [`BundleContext`] from the host.
///
/// Only callable from inside a running stage export -- the host has
/// nothing to answer with outside a `transform`/`dispatch` invocation, so
/// this function only compiles for `wasm32` targets (see
/// `crate::bindings_glue`'s module doc comment for the resulting host
/// coverage carve-out).
#[cfg(target_arch = "wasm32")]
pub fn get_context() -> BundleContext {
    crate::bindings_glue::get_context()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Deserialize, PartialEq)]
    struct BundleConfig {
        greeting: String,
    }

    fn sample() -> BundleContext {
        BundleContext {
            tenant: "acme".to_string(),
            community: Some("main".to_string()),
            app_id: "app-1".to_string(),
            feature: "welcome".to_string(),
            version: "1.0.0".to_string(),
            message_id: "1732300000000-0".to_string(),
            config_json: r#"{"greeting":"hi"}"#.to_string(),
        }
    }

    #[test]
    fn config_deserializes_typed_struct() {
        let ctx = sample();
        let cfg: BundleConfig = ctx.config().expect("valid config JSON");
        assert_eq!(
            cfg,
            BundleConfig {
                greeting: "hi".to_string()
            }
        );
    }

    #[test]
    fn config_reports_malformed_json() {
        let mut ctx = sample();
        ctx.config_json = "not json".to_string();
        let result: Result<BundleConfig, _> = ctx.config();
        assert!(result.is_err());
    }

    #[test]
    fn message_id_is_the_dedup_key() {
        let ctx = sample();
        assert_eq!(ctx.message_id, "1732300000000-0");
    }
}

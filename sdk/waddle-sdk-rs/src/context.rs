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

    /// The community's PUBLIC UUID when the host supplied it (see
    /// [`community_public_id_from_config`]); the only community identifier allowed in a
    /// user-facing URL. `None` means unknown: omit the link, never use a numeric id.
    pub fn community_public_id(&self) -> Option<String> {
        community_public_id_from_config(&self.config_json)
    }
}

/// The host-reserved `config-json` key carrying the community's PUBLIC UUID.
pub const COMMUNITY_PUBLIC_ID_KEY: &str = "community_public_id";

/// Extracts the community PUBLIC UUID the host folded into a context `config-json`.
///
/// The frozen WIT context record has no field for it, so the host passes it additively as the
/// reserved [`COMMUNITY_PUBLIC_ID_KEY`] key. Returns the canonical lowercase UUID, or `None` when
/// the host could not supply one (a miss, a database failure, an older host) or the value is not
/// a UUID -- a numeric community id is NEVER accepted or substituted. A bundle building a
/// user-facing link must then omit the link rather than fall back to the numeric id.
pub fn community_public_id_from_config(config_json: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(config_json).ok()?;
    let raw = v.get(COMMUNITY_PUBLIC_ID_KEY)?.as_str()?.trim();
    let b = raw.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        });
    ok.then(|| raw.to_ascii_lowercase())
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

    const PUB: &str = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f01";

    #[test]
    fn community_public_id_is_read_from_the_reserved_config_key() {
        let mut ctx = sample();
        assert_eq!(ctx.community_public_id(), None, "absent by default");
        ctx.config_json = format!(r#"{{"greeting":"hi","community_public_id":"{PUB}"}}"#);
        assert_eq!(ctx.community_public_id().as_deref(), Some(PUB));
        // The typed config still deserialises with the extra reserved key present.
        let cfg: BundleConfig = ctx.config().expect("extra key ignored");
        assert_eq!(cfg.greeting, "hi");
    }

    #[test]
    fn community_public_id_rejects_anything_but_a_uuid() {
        let upper = format!(r#"{{"community_public_id":"{}"}}"#, PUB.to_uppercase());
        assert_eq!(
            community_public_id_from_config(&upper).as_deref(),
            Some(PUB)
        );
        for bad in [
            "",
            "not json",
            "[1]",
            "{}",
            r#"{"community_public_id":7}"#,
            r#"{"community_public_id":"7"}"#,
            r#"{"community_public_id":null}"#,
            r#"{"community_public_id":"0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f0"}"#,
            r#"{"community_public_id":"0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9fzz"}"#,
            r#"{"community_public_id":"0b6b0a525f0e4f6a9a391b6a3d6d9f01xxxx"}"#,
        ] {
            assert_eq!(community_public_id_from_config(bad), None, "{bad:?}");
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

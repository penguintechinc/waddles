//! Idiomatic wrapper over the WIT `relay` interface (outbound relay queue,
//! granted only to action-stage bundles; `wit/waddle-bundle/stage.wit`
//! `interface relay`).

#[cfg(target_arch = "wasm32")]
use serde::Serialize;

#[cfg(any(test, target_arch = "wasm32"))]
use crate::error::SdkError;
#[cfg(any(test, target_arch = "wasm32"))]
use crate::types::to_canonical_json_object;

/// Pushes `message` (serialized to canonical JSON object text) onto the
/// `provider`-scoped outbound relay queue owned by `svc-ingest`.
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn push<T: Serialize>(provider: &str, message: &T) -> Result<(), SdkError> {
    let message_json = to_canonical_json_object(message)?;
    crate::bindings_glue::relay_push(provider, &message_json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Serialize;

    #[derive(Serialize)]
    struct Message {
        text: String,
    }

    #[test]
    fn message_serializes_to_json_object_text() {
        let json = to_canonical_json_object(&Message {
            text: "hi".to_string(),
        })
        .expect("object serializes");
        assert!(json.starts_with('{'));
        assert!(json.contains("\"text\":\"hi\""));
    }

    #[test]
    fn non_object_message_is_rejected_before_it_would_reach_the_host() {
        let err = to_canonical_json_object(&"just a string").unwrap_err();
        assert!(matches!(err, SdkError::NonObjectJson));
    }
}

//! Idiomatic, host-independent equivalents of the WIT world's `types`
//! interface records (`wit/waddle-bundle/stage.wit`, `interface types`).
//!
//! These structs never reference the `wit_bindgen`-generated types
//! directly -- that keeps every field, JSON-payload helper and constructor
//! on this page unit-testable on the host target. The mechanical
//! `From`/`TryFrom` glue to the actual WIT records lives in
//! `crate::bindings_glue`, is `#[cfg(target_arch = "wasm32")]`, and is
//! exercised only by an on-target (wasm32) build -- see that module's doc
//! comment for why it is excluded from the host coverage ratio, mirroring
//! `core/svc_process`'s `main.rs` exclusion precedent.

use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::error::SdkError;

/// One inbound platform event, normalized by `svc_ingest` before it ever
/// reaches a bundle. Mirrors `interface types` `record platform-event`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlatformEvent {
    pub platform: String,
    pub event_type: String,
    pub actor: Option<String>,
    /// Canonical JSON object text (spec Assumption A2) -- use
    /// [`PlatformEvent::payload`] for a typed view.
    pub payload_json: String,
    /// RFC 3339 UTC, millisecond precision.
    pub occurred_at: String,
}

impl PlatformEvent {
    /// Deserializes [`Self::payload_json`] into `T`. `T` is almost always a
    /// `#[derive(Deserialize)]` struct the bundle author owns; open-ended
    /// payload shapes should deserialize into `serde_json::Value` instead.
    pub fn payload<T: DeserializeOwned>(&self) -> Result<T, SdkError> {
        serde_json::from_str(&self.payload_json).map_err(SdkError::from)
    }

    /// Builds a new event with `payload` serialized to canonical JSON text.
    /// Returns [`SdkError::PayloadJson`] if `payload` is not a JSON object
    /// (the WIT world requires `payload-json` to be an object, never a
    /// scalar or array -- see the WIT doc comment on `payload-json`).
    pub fn with_payload<T: Serialize>(
        platform: impl Into<String>,
        event_type: impl Into<String>,
        actor: Option<String>,
        occurred_at: impl Into<String>,
        payload: &T,
    ) -> Result<Self, SdkError> {
        let payload_json = to_canonical_json_object(payload)?;
        Ok(Self {
            platform: platform.into(),
            event_type: event_type.into(),
            actor,
            payload_json,
            occurred_at: occurred_at.into(),
        })
    }
}

/// One process/action-stage delivery envelope. Mirrors `record
/// stage-envelope`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StageEnvelope {
    pub tenant: String,
    pub community: Option<String>,
    pub app_id: String,
    pub stage: String,
    pub event: PlatformEvent,
    pub ts: String,
    pub target_app_id: Option<String>,
    /// W3C traceparent, when the stage had one.
    pub trace_context: Option<String>,
}

/// The `action-stage` export's success return. Mirrors `record
/// transport-result`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct TransportResult {
    pub ok: bool,
    pub status: Option<u16>,
    pub detail: Option<String>,
    pub provider_message_id: Option<String>,
}

impl TransportResult {
    /// A successful delivery with no extra detail.
    pub fn ok() -> Self {
        Self {
            ok: true,
            ..Default::default()
        }
    }
}

/// The `action-stage` export's error return. Mirrors `record
/// transport-error`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransportError {
    /// The single field the action stage branches on for retry scheduling.
    pub retryable: bool,
    pub code: String,
    pub message: String,
    pub retry_after_ms: Option<u32>,
}

impl TransportError {
    /// A non-retryable error with the given `code`/`message`.
    pub fn fatal(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            retryable: false,
            code: code.into(),
            message: message.into(),
            retry_after_ms: None,
        }
    }

    /// A retryable error, optionally hinting a retry-after delay.
    pub fn retryable(
        code: impl Into<String>,
        message: impl Into<String>,
        retry_after_ms: Option<u32>,
    ) -> Self {
        Self {
            retryable: true,
            code: code.into(),
            message: message.into(),
            retry_after_ms,
        }
    }

    /// The canonical stub response for a stage export a bundle does not
    /// implement (spec SS6.5: "Tier 1 SDKs generate the stub
    /// automatically"). `code = "UNSUPPORTED_STAGE"`, `retryable = false`.
    pub fn unsupported_stage() -> Self {
        Self::fatal(
            "UNSUPPORTED_STAGE",
            "this bundle does not implement the action stage",
        )
    }
}

/// Returned by `process-stage.transform` when a bundle does not implement
/// the process stage. Mirrors `record unsupported-stage`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnsupportedStage {
    pub stage: String,
}

impl UnsupportedStage {
    pub fn process() -> Self {
        Self {
            stage: "process".to_string(),
        }
    }
}

/// Serializes `value` to compact JSON and verifies it is a JSON object --
/// the WIT world requires every `*-json` field to carry canonical JSON
/// *object* text, never a bare scalar or array (spec A2, and the WIT doc
/// comment on `payload-json`).
pub(crate) fn to_canonical_json_object<T: Serialize>(value: &T) -> Result<String, SdkError> {
    let json = serde_json::to_value(value).map_err(SdkError::from)?;
    if !json.is_object() {
        return Err(SdkError::NonObjectJson);
    }
    serde_json::to_string(&json).map_err(SdkError::from)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Serialize, Deserialize, PartialEq)]
    struct Payload {
        alias: String,
        count: u32,
    }

    #[test]
    fn payload_round_trips_through_json() {
        let event = PlatformEvent::with_payload(
            "discord",
            "message.create",
            Some("actor-1".to_string()),
            "2026-09-22T00:00:00.000Z",
            &Payload {
                alias: "sr".to_string(),
                count: 3,
            },
        )
        .expect("valid object payload serializes");

        let decoded: Payload = event.payload().expect("payload deserializes back");
        assert_eq!(
            decoded,
            Payload {
                alias: "sr".to_string(),
                count: 3
            }
        );
        assert!(event.payload_json.starts_with('{'));
    }

    #[test]
    fn payload_rejects_non_object_json() {
        let err = to_canonical_json_object(&42u32).unwrap_err();
        assert!(matches!(err, SdkError::NonObjectJson));
    }

    #[test]
    fn payload_deserialize_error_is_reported() {
        let event = PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload_json: "{not valid json".to_string(),
            occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
        };
        let result: Result<Payload, _> = event.payload();
        assert!(matches!(result, Err(SdkError::PayloadJson(_))));
    }

    #[test]
    fn transport_result_ok_defaults_other_fields_none() {
        let result = TransportResult::ok();
        assert!(result.ok);
        assert_eq!(result.status, None);
        assert_eq!(result.detail, None);
    }

    #[test]
    fn transport_error_unsupported_stage_matches_spec_contract() {
        let err = TransportError::unsupported_stage();
        assert!(!err.retryable);
        assert_eq!(err.code, "UNSUPPORTED_STAGE");
    }

    #[test]
    fn transport_error_retryable_carries_hint() {
        let err = TransportError::retryable("RATE_LIMITED", "slow down", Some(2_000));
        assert!(err.retryable);
        assert_eq!(err.retry_after_ms, Some(2_000));
    }

    #[test]
    fn unsupported_stage_process_names_the_stage() {
        assert_eq!(UnsupportedStage::process().stage, "process");
    }
}

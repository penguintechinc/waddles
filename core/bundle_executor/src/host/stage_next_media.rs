//! `media` (the `media.generate` permission): the `world stage-next` import that
//! lets a bundle ask the platform to generate an image / music / video with the
//! community's own Gemini key. Cloned from the `ai` import in
//! [`super::stage_next_ai`].
//!
//! Every call rides the same [`imports::call`](crate::host::imports::call) round
//! trip as every other capability, carried as `capability = db`,
//! `op = "media.*"` (`penguin-bundle-host`'s closed `CapabilityKind` has no
//! media member). **This executor performs no network I/O, no tier check and no
//! prompt inspection** -- it only bridges; the tier gate, BYOK lookup, provider
//! calls, storage and job store all live stage-side
//! (`bundle_host_media` via `core/svc_process`). A stage with no `media`
//! handler answers `not_implemented`, which surfaces to the guest as
//! `unavailable` -- fail-loud.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::media;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

fn malformed(e: impl std::fmt::Display) -> media::Error {
    media::Error::Backend(format!("malformed host-result: {e}"))
}

/// Pulls one string field out of a successful host-result; a shape the stage and
/// executor disagree about is a loud `backend` error, never an empty value.
fn string_field(value: &serde_json::Value, field: &str) -> Result<String, media::Error> {
    match value.get(field).and_then(serde_json::Value::as_str) {
        Some(s) => Ok(s.to_string()),
        None => Err(malformed(format!("missing string field {field}"))),
    }
}

/// Maps a stage-side error code onto the WIT `media.error` variant. Gate denial
/// codes (`not_granted`, `rate_limited`, `contains_pii`, ...) surface as
/// `denied(<code>)`; wiring states are `unavailable`.
fn media_error_from(err: ExecutorError) -> media::Error {
    match err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "tier_required" => media::Error::TierRequired(message),
            "byok_missing" => media::Error::ByokMissing(message),
            "provider_refused" => media::Error::ProviderRefused(message),
            "quota" => media::Error::Quota(message),
            "timeout" => media::Error::Timeout,
            "not_found" => media::Error::NotFound,
            "invalid_args" => media::Error::Invalid(message),
            "not_implemented" | "feature_disabled" | "unavailable" => {
                media::Error::Unavailable(message)
            }
            "backend" => media::Error::Backend(message),
            _ => media::Error::Denied(code),
        },
        other => media::Error::Backend(other.to_string()),
    }
}

/// Decodes a `media.job_status` host-result into the WIT `job-state`.
fn decode_job_state(value: &serde_json::Value) -> Result<media::JobState, media::Error> {
    let state = string_field(value, "state")?;
    match state.as_str() {
        "pending" => Ok(media::JobState::Pending),
        "done" => Ok(media::JobState::Done(string_field(value, "handle")?)),
        "failed" => {
            let code = string_field(value, "code")?;
            let message = string_field(value, "message")?;
            Ok(media::JobState::Failed(media_error_from(
                ExecutorError::HostCallDenied {
                    capability: "db",
                    op: "media.job_status",
                    code,
                    message,
                },
            )))
        }
        other => Err(malformed(format!("unknown job state {other}"))),
    }
}

impl media::Host for ExecState {
    async fn generate_image(&mut self, prompt: String) -> Result<String, media::Error> {
        let args = serde_json::json!({ "prompt": prompt });
        match call(self, CapabilityKind::Db, "media.generate_image", args).await {
            Ok(v) => string_field(&v, "handle"),
            Err(e) => Err(media_error_from(e)),
        }
    }

    async fn submit_music(&mut self, prompt: String) -> Result<String, media::Error> {
        let args = serde_json::json!({ "prompt": prompt });
        match call(self, CapabilityKind::Db, "media.submit_music", args).await {
            Ok(v) => string_field(&v, "job_id"),
            Err(e) => Err(media_error_from(e)),
        }
    }

    async fn submit_video(&mut self, prompt: String) -> Result<String, media::Error> {
        let args = serde_json::json!({ "prompt": prompt });
        match call(self, CapabilityKind::Db, "media.submit_video", args).await {
            Ok(v) => string_field(&v, "job_id"),
            Err(e) => Err(media_error_from(e)),
        }
    }

    async fn job_status(&mut self, job_id: String) -> Result<media::JobState, media::Error> {
        let args = serde_json::json!({ "job_id": job_id });
        match call(self, CapabilityKind::Db, "media.job_status", args).await {
            Ok(v) => decode_job_state(&v),
            Err(e) => Err(media_error_from(e)),
        }
    }

    async fn object_url(&mut self, handle: String) -> Result<String, media::Error> {
        let args = serde_json::json!({ "handle": handle });
        match call(self, CapabilityKind::Db, "media.object_url", args).await {
            Ok(v) => string_field(&v, "url"),
            Err(e) => Err(media_error_from(e)),
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    fn denied(code: &str, message: &str) -> ExecutorError {
        ExecutorError::HostCallDenied {
            capability: "db",
            op: "media.generate_image",
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    #[test]
    fn typed_codes_map_to_their_variants() {
        assert!(
            matches!(media_error_from(denied("tier_required", "professional")), media::Error::TierRequired(m) if m == "professional")
        );
        assert!(
            matches!(media_error_from(denied("byok_missing", "gemini")), media::Error::ByokMissing(m) if m == "gemini")
        );
        assert!(matches!(
            media_error_from(denied("provider_refused", "safety")),
            media::Error::ProviderRefused(_)
        ));
        assert!(matches!(
            media_error_from(denied("quota", "minute")),
            media::Error::Quota(_)
        ));
        assert!(matches!(
            media_error_from(denied("timeout", "")),
            media::Error::Timeout
        ));
        assert!(matches!(
            media_error_from(denied("not_found", "")),
            media::Error::NotFound
        ));
        assert!(matches!(
            media_error_from(denied("invalid_args", "x")),
            media::Error::Invalid(_)
        ));
        assert!(matches!(
            media_error_from(denied("backend", "x")),
            media::Error::Backend(_)
        ));
    }

    #[test]
    fn wiring_codes_are_unavailable_and_gate_codes_are_denied() {
        for c in ["not_implemented", "feature_disabled", "unavailable"] {
            assert!(matches!(
                media_error_from(denied(c, "m")),
                media::Error::Unavailable(_)
            ));
        }
        assert!(
            matches!(media_error_from(denied("not_granted", "m")), media::Error::Denied(c) if c == "not_granted")
        );
        assert!(matches!(
            media_error_from(denied("contains_pii", "m")),
            media::Error::Denied(_)
        ));
    }

    #[test]
    fn job_state_decodes_every_shape_and_rejects_garbage() {
        use serde_json::json;
        assert!(matches!(
            decode_job_state(&json!({"state":"pending"})).unwrap(),
            media::JobState::Pending
        ));
        assert!(
            matches!(decode_job_state(&json!({"state":"done","handle":"media://1/2/x.png"})).unwrap(), media::JobState::Done(h) if h.starts_with("media://"))
        );
        assert!(matches!(
            decode_job_state(&json!({"state":"failed","code":"timeout","message":""})).unwrap(),
            media::JobState::Failed(media::Error::Timeout)
        ));
        assert!(matches!(
            decode_job_state(&json!({"state":"done"})),
            Err(media::Error::Backend(_))
        ));
        assert!(matches!(
            decode_job_state(&json!({"state":"weird"})),
            Err(media::Error::Backend(_))
        ));
        assert!(matches!(
            decode_job_state(&json!({})),
            Err(media::Error::Backend(_))
        ));
    }

    #[test]
    fn non_denial_executor_errors_are_backend() {
        assert!(matches!(
            media_error_from(ExecutorError::ConnectionUnavailable),
            media::Error::Backend(_)
        ));
    }
}

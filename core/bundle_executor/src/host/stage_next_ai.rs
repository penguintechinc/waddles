//! `ai` (the `ai.generate` permission): the `world stage-next` import that lets
//! a bundle ask the platform's self-hosted model for a completion. Cloned from
//! the `economy` import in [`super::stage_next_economy`].
//!
//! Every call rides the same [`imports::call`](crate::host::imports::call)
//! round trip as every other capability. `penguin-bundle-host`'s closed
//! `CapabilityKind` has no `ai` member, so the call is carried as
//! `capability = db`, `op = "ai.generate"`; the stage
//! (`core/svc_process::capabilities`) dispatches on that op prefix BEFORE its
//! `storage.tables` path and authorizes through the capability gate first.
//! **This executor performs no network I/O and no prompt inspection** -- it only
//! bridges: PII rejection, prompt framing, token bounds, the model call and its
//! deadline all live stage-side. A stage with no `ai` handler answers
//! `not_implemented`, which surfaces to the guest as `unavailable` -- fail-loud,
//! never a canned completion.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::ai;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Wire shape of a successful `ai.generate` host-result.
#[derive(Debug, serde::Deserialize)]
struct GenerateWire {
    text: String,
}

fn malformed(e: impl std::fmt::Display) -> ai::Error {
    ai::Error::Backend(format!("malformed host-result: {e}"))
}

/// Decodes a successful `ai.generate` host-result into the completion text. A
/// shape the stage and executor disagree about is a loud `backend` error, never
/// an empty or made-up completion.
fn decode_text(value: serde_json::Value) -> Result<String, ai::Error> {
    serde_json::from_value::<GenerateWire>(value)
        .map(|w| w.text)
        .map_err(malformed)
}

/// Builds the JSON wire args for one `ai.generate` call. Only the four fields a
/// bundle may influence cross the wire -- never a model, endpoint, tenant or
/// system prompt.
fn generate_args(prompt: &str, opts: &ai::Options) -> serde_json::Value {
    serde_json::json!({
        "prompt": prompt,
        "max_tokens": opts.max_tokens,
        "temperature": opts.temperature,
        "json": opts.json,
    })
}

/// Parses the bare-decimal wire message the stage uses for `too_many_tokens`
/// (the bound that was exceeded). A non-numeric message means the stage and
/// executor disagree about the contract: surfaced loudly as `backend`, never as
/// a made-up number.
fn bound_message(code: &str, message: &str) -> Result<u32, ai::Error> {
    message
        .trim()
        .parse::<u32>()
        .map_err(|_| ai::Error::Backend(format!("malformed {code} message from the stage")))
}

/// Maps a stage-side error code onto the WIT `ai.error` variant. Gate denial
/// codes (`not_granted`, `rate_limited`, `quota_exceeded`, `instance_denied`,
/// ...) and the content refusal `contains_pii` all surface as `denied(<code>)` so
/// a bundle can branch on the stable code; wiring states (`not_implemented`,
/// `feature_disabled`, `unavailable`) are `unavailable`.
fn ai_error_from(err: ExecutorError) -> ai::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "too_many_tokens" => match bound_message(code, message) {
                Ok(bound) => ai::Error::TooManyTokens(bound),
                Err(e) => e,
            },
            "content_filtered" => ai::Error::ContentFiltered,
            "invalid_args" => ai::Error::Invalid(message.clone()),
            "not_implemented" | "feature_disabled" | "unavailable" => {
                ai::Error::Unavailable(message.clone())
            }
            "backend" => ai::Error::Backend(message.clone()),
            other => ai::Error::Denied(other.to_string()),
        },
        other => ai::Error::Backend(other.to_string()),
    }
}

impl ai::Host for ExecState {
    async fn generate(&mut self, prompt: String, opts: ai::Options) -> Result<String, ai::Error> {
        let args = generate_args(&prompt, &opts);
        match call(self, CapabilityKind::Db, "ai.generate", args).await {
            Ok(value) => decode_text(value),
            Err(e) => Err(ai_error_from(e)),
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
            op: "ai.generate",
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    #[test]
    fn too_many_tokens_decodes_its_bound() {
        assert!(matches!(
            ai_error_from(denied("too_many_tokens", " 512 ")),
            ai::Error::TooManyTokens(512)
        ));
    }

    #[test]
    fn a_malformed_bound_is_a_loud_backend_error_not_a_made_up_number() {
        for bad in ["not a number", "-1", "99999999999"] {
            match ai_error_from(denied("too_many_tokens", bad)) {
                ai::Error::Backend(m) => assert!(m.contains("too_many_tokens"), "{m}"),
                other => panic!("expected backend, got {other:?}"),
            }
        }
    }

    #[test]
    fn content_filtered_and_invalid_map_to_their_variants() {
        assert!(matches!(
            ai_error_from(denied("content_filtered", "x")),
            ai::Error::ContentFiltered
        ));
        assert!(matches!(
            ai_error_from(denied("invalid_args", "bad")),
            ai::Error::Invalid(m) if m == "bad"
        ));
    }

    #[test]
    fn wiring_codes_are_unavailable_and_gate_or_content_codes_are_denied() {
        for code in ["not_implemented", "feature_disabled", "unavailable"] {
            assert!(matches!(
                ai_error_from(denied(code, "m")),
                ai::Error::Unavailable(m) if m == "m"
            ));
        }
        for code in [
            "not_granted",
            "rate_limited",
            "quota_exceeded",
            "instance_denied",
            "contains_pii",
        ] {
            assert!(matches!(
                ai_error_from(denied(code, "m")),
                ai::Error::Denied(c) if c == code
            ));
        }
        assert!(matches!(
            ai_error_from(denied("backend", "b")),
            ai::Error::Backend(m) if m == "b"
        ));
    }

    #[test]
    fn non_denial_executor_errors_are_backend() {
        assert!(matches!(
            ai_error_from(ExecutorError::ConnectionUnavailable),
            ai::Error::Backend(_)
        ));
    }

    #[test]
    fn success_payload_decodes_and_malformed_ones_fail_loud() {
        assert_eq!(
            decode_text(serde_json::json!({ "text": "hello" })).unwrap(),
            "hello"
        );
        for bad in [
            serde_json::json!({}),
            serde_json::json!({ "text": 3 }),
            serde_json::json!(null),
        ] {
            assert!(matches!(decode_text(bad), Err(ai::Error::Backend(_))));
        }
    }

    #[test]
    fn wire_args_carry_only_the_four_guest_controlled_fields() {
        let args = generate_args(
            "hi",
            &ai::Options {
                max_tokens: Some(64),
                temperature: None,
                json: true,
            },
        );
        let obj = args.as_object().unwrap();
        let mut keys: Vec<&str> = obj.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, ["json", "max_tokens", "prompt", "temperature"]);
        assert_eq!(args["max_tokens"], 64);
        assert!(args["temperature"].is_null());
        assert_eq!(args["json"], true);
    }
}

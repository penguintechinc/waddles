//! `identity`: the `world stage-next` import that resolves WHO an invocation is
//! about -- the triggering actor, or a mention target the triggering message
//! carried -- to the community `user-uuid` that the `economy` and `reputation`
//! imports name their targets by. Cloned from the recipe in
//! [`super::stage_next_economy`].
//!
//! Every call rides the same [`imports::call`](crate::host::imports::call)
//! round trip as every other capability. `penguin-bundle-host`'s closed
//! `CapabilityKind` has no `identity` member, so calls are carried as
//! `capability = db`, `op = "identity.resolve_actor" |
//! "identity.resolve_mention"`; the stage (`core/svc_process::capabilities`)
//! dispatches on that op prefix BEFORE its `storage.tables` path and
//! authorizes through the capability gate (`identity.resolve`). Nothing here
//! decides authority or resolves anything: the executor only encodes the
//! call, decodes the answer and maps refusals onto the WIT `identity.error`
//! variant. A stage that has no identity handler answers `not_implemented`,
//! which surfaces to the guest as `unavailable` -- fail-loud, never a default
//! identity.
//!
//! **PII-safe in the success position.** The only thing the executor ever
//! hands a guest from a successful resolve is a canonical lower-case
//! hyphenated UUID string ([`decode_user`] rejects anything else as a
//! `backend` error): a stage bug that leaked a platform id, a handle or a
//! `{user:..}` placeholder in the `user` field is surfaced as a loud error,
//! never forwarded to the bundle as if it were an identity.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::identity;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Longest mention token the executor will even forward. A real token is a
/// 36-byte UUID (or, with inbound tokenization off, a `<@id>`/`@handle`
/// reference of at most ~30 bytes); anything near this bound is a malformed
/// or hostile argument, refused before it costs a host round trip.
const MAX_MENTION_TOKEN_LEN: usize = 256;

/// Wire shape of a successful `identity.resolve_*` host-result.
#[derive(Debug, serde::Deserialize)]
struct UserWire {
    user: String,
}

/// `true` iff `s` is exactly a canonical lower-case hyphenated UUID
/// (`8-4-4-4-12` hex) -- the only string shape allowed in the success
/// position of `identity.resolve-*`.
fn is_canonical_uuid(s: &str) -> bool {
    let bytes = s.as_bytes();
    if bytes.len() != 36 {
        return false;
    }
    bytes.iter().enumerate().all(|(i, b)| match i {
        8 | 13 | 18 | 23 => *b == b'-',
        _ => b.is_ascii_digit() || (b'a'..=b'f').contains(b),
    })
}

fn malformed(e: impl std::fmt::Display) -> identity::Error {
    identity::Error::Backend(format!("malformed host-result: {e}"))
}

/// Decodes a successful host-result into the guest-visible `user-uuid`,
/// refusing (loudly, as `backend`) any value that is not a canonical UUID.
fn decode_user(value: serde_json::Value) -> Result<String, identity::Error> {
    let wire = serde_json::from_value::<UserWire>(value).map_err(malformed)?;
    if !is_canonical_uuid(&wire.user) {
        // Deliberately NOT echoing the offending value: it could be exactly
        // the raw identity this capability exists to keep out of a bundle.
        return Err(identity::Error::Backend(
            "malformed host-result: user is not a canonical UUID".to_string(),
        ));
    }
    Ok(wire.user)
}

/// Maps a stage-side error code onto the WIT `identity.error` variant.
/// Gate denial codes (`not_granted`, `rate_limited`, `instance_denied`, ...)
/// all surface as `denied(<code>)` so a bundle can branch on the stable code.
fn identity_error_from(err: ExecutorError) -> identity::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "not_linked" => identity::Error::NotLinked,
            "not_a_member" => identity::Error::NotAMember,
            "not_found" => identity::Error::NotFound,
            "ambiguous" => identity::Error::Ambiguous,
            "invalid_args" => identity::Error::Invalid(message.clone()),
            "not_implemented" | "feature_disabled" | "unavailable" => {
                identity::Error::Unavailable(message.clone())
            }
            "backend" => identity::Error::Backend(message.clone()),
            other => identity::Error::Denied(other.to_string()),
        },
        other => identity::Error::Backend(other.to_string()),
    }
}

impl identity::Host for ExecState {
    async fn resolve_actor(&mut self) -> Result<String, identity::Error> {
        match call(
            self,
            CapabilityKind::Db,
            "identity.resolve_actor",
            serde_json::json!({}),
        )
        .await
        {
            Ok(value) => decode_user(value),
            Err(e) => Err(identity_error_from(e)),
        }
    }

    async fn resolve_mention(&mut self, token: String) -> Result<String, identity::Error> {
        if token.is_empty() || token.len() > MAX_MENTION_TOKEN_LEN {
            return Err(identity::Error::Invalid(format!(
                "mention token must be 1..={MAX_MENTION_TOKEN_LEN} bytes"
            )));
        }
        let args = serde_json::json!({ "token": token });
        match call(self, CapabilityKind::Db, "identity.resolve_mention", args).await {
            Ok(value) => decode_user(value),
            Err(e) => Err(identity_error_from(e)),
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
            op: "identity.resolve_actor",
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    const UUID: &str = "6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b34";

    #[test]
    fn only_a_canonical_lowercase_hyphenated_uuid_is_accepted_in_the_success_position() {
        assert!(is_canonical_uuid(UUID));
        for bad in [
            "",
            "{user:6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b34}",
            "6F1C0A52-9D3E-4B7A-8C10-2E5D7F9A1B34", // upper-case: not canonical
            "6f1c0a529d3e4b7a8c102e5d7f9a1b34",     // un-hyphenated
            "6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b3",  // short
            "6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b345", // long
            "6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b3g", // non-hex
            "12345",                                // a raw platform id
            "bob",                                  // a raw handle
            "<@123>",
        ] {
            assert!(!is_canonical_uuid(bad), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn a_uuid_result_decodes_and_anything_else_fails_loud_without_echoing_it() {
        assert_eq!(
            decode_user(serde_json::json!({ "user": UUID })).unwrap(),
            UUID
        );
        for bad in [
            serde_json::json!({ "user": "12345" }),
            serde_json::json!({ "user": "bob" }),
            serde_json::json!({ "user": "{user:6f1c0a52-9d3e-4b7a-8c10-2e5d7f9a1b34}" }),
            serde_json::json!({}),
            serde_json::json!({ "user": 7 }),
            serde_json::json!(null),
        ] {
            match decode_user(bad.clone()) {
                Err(identity::Error::Backend(m)) => {
                    assert!(
                        !m.contains("12345") && !m.contains("bob"),
                        "the offending value must never be echoed: {m}"
                    );
                }
                other => panic!("{bad}: expected backend error, got {other:?}"),
            }
        }
    }

    #[test]
    fn identity_refusal_codes_map_to_their_explicit_variants() {
        assert!(matches!(
            identity_error_from(denied("not_linked", "x")),
            identity::Error::NotLinked
        ));
        assert!(matches!(
            identity_error_from(denied("not_a_member", "x")),
            identity::Error::NotAMember
        ));
        assert!(matches!(
            identity_error_from(denied("not_found", "x")),
            identity::Error::NotFound
        ));
        assert!(matches!(
            identity_error_from(denied("ambiguous", "x")),
            identity::Error::Ambiguous
        ));
        assert!(matches!(
            identity_error_from(denied("invalid_args", "bad")),
            identity::Error::Invalid(m) if m == "bad"
        ));
        assert!(matches!(
            identity_error_from(denied("backend", "b")),
            identity::Error::Backend(m) if m == "b"
        ));
    }

    #[test]
    fn wiring_codes_are_unavailable_and_gate_codes_are_denied() {
        for code in ["not_implemented", "feature_disabled", "unavailable"] {
            assert!(matches!(
                identity_error_from(denied(code, "m")),
                identity::Error::Unavailable(m) if m == "m"
            ));
        }
        for code in [
            "not_granted",
            "rate_limited",
            "instance_denied",
            "resource_scope_mismatch",
        ] {
            assert!(matches!(
                identity_error_from(denied(code, "m")),
                identity::Error::Denied(c) if c == code
            ));
        }
    }

    #[test]
    fn an_unknown_refusal_code_is_a_denial_never_a_success_or_a_default_identity() {
        assert!(matches!(
            identity_error_from(denied("some_future_code", "m")),
            identity::Error::Denied(c) if c == "some_future_code"
        ));
    }

    #[test]
    fn non_denial_executor_errors_are_backend() {
        assert!(matches!(
            identity_error_from(ExecutorError::ConnectionUnavailable),
            identity::Error::Backend(_)
        ));
    }

    #[tokio::test]
    async fn an_oversized_or_empty_mention_token_is_refused_before_any_host_call() {
        // No bridge at all: if the call were attempted it would surface as a
        // `backend` ConnectionUnavailable, so `Invalid` proves it never was.
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        assert!(matches!(
            identity::Host::resolve_mention(&mut state, String::new()).await,
            Err(identity::Error::Invalid(_))
        ));
        assert!(matches!(
            identity::Host::resolve_mention(&mut state, "x".repeat(MAX_MENTION_TOKEN_LEN + 1))
                .await,
            Err(identity::Error::Invalid(_))
        ));
        // A well-sized token does reach the (absent) bridge.
        assert!(matches!(
            identity::Host::resolve_mention(&mut state, UUID.to_string()).await,
            Err(identity::Error::Backend(_))
        ));
        assert!(matches!(
            identity::Host::resolve_actor(&mut state).await,
            Err(identity::Error::Backend(_))
        ));
    }
}

//! `authz`: the `stage-next` / `stage-v2` / `stage-tools` import that answers
//! "does the member whose event triggered this invocation hold community scope
//! `S`?". Cloned from the recipe in [`super::stage_next_identity`].
//!
//! Carried as `capability = db`, `op = "authz.caller_can"`, args
//! `{"scope": s}`, result `{"allowed": bool}`; the stage
//! (`core/svc_process::capabilities`) dispatches on that op BEFORE its
//! `storage.tables` path and needs no gate grant. The executor validates the
//! scope shape before any host call, decodes the answer strictly and maps
//! refusals onto the WIT `authz.error` variant. A stage with no handler
//! answers `not_implemented`, surfaced as `unavailable` -- fail-loud, and
//! SDK wrappers fail closed on any error.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::authz;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Longest scope string forwarded to the stage.
const MAX_SCOPE_LEN: usize = 64;

/// Wire shape of a successful `authz.caller_can` host-result.
#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct AllowedWire {
    allowed: bool,
}

/// `true` iff `scope` is 1..=64 bytes matching
/// `^[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*$`.
fn scope_is_valid(scope: &str) -> bool {
    if scope.is_empty() || scope.len() > MAX_SCOPE_LEN {
        return false;
    }
    let mut parts = scope.split(':');
    let (Some(a), Some(b), None) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    let seg_ok = |p: &str| {
        let mut chars = p.chars();
        matches!(chars.next(), Some('a'..='z'))
            && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '.')
    };
    seg_ok(a) && seg_ok(b)
}

/// Strictly decodes `{"allowed": bool}`; anything else is a `backend` error.
fn decode_allowed(value: serde_json::Value) -> Result<bool, authz::Error> {
    serde_json::from_value::<AllowedWire>(value)
        .map(|w| w.allowed)
        .map_err(|e| authz::Error::Backend(format!("malformed host-result: {e}")))
}

/// Maps a stage-side error onto the WIT `authz.error` variant.
fn authz_error_from(err: ExecutorError) -> authz::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "not_implemented" | "feature_disabled" | "unavailable" => {
                authz::Error::Unavailable(message.clone())
            }
            "invalid_args" => authz::Error::Invalid(message.clone()),
            _ => authz::Error::Backend(message.clone()),
        },
        other => authz::Error::Backend(other.to_string()),
    }
}

impl authz::Host for ExecState {
    async fn caller_can(&mut self, scope: String) -> Result<bool, authz::Error> {
        if !scope_is_valid(&scope) {
            return Err(authz::Error::Invalid(format!(
                "scope must be 1..={MAX_SCOPE_LEN} bytes matching ^[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*$"
            )));
        }
        match call(
            self,
            CapabilityKind::Db,
            "authz.caller_can",
            serde_json::json!({ "scope": scope }),
        )
        .await
        {
            Ok(value) => decode_allowed(value),
            Err(e) => Err(authz_error_from(e)),
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
            op: "authz.caller_can",
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    #[test]
    fn scope_charset_and_length_are_enforced() {
        for ok in [
            "community:admin",
            "community:ban",
            "a:b",
            "a.b_c1:d.e_f2",
        ] {
            assert!(scope_is_valid(ok), "{ok:?}");
        }
        let long = format!("a:{}", "b".repeat(63));
        assert_eq!(long.len(), 65);
        for bad in [
            "",
            "community",
            "community:",
            ":admin",
            "Community:admin",
            "community:Admin",
            "community:admin:x",
            "1community:admin",
            "community:1admin",
            "community:ad min",
            "community:ad-min",
            "community:admin\n",
            long.as_str(),
        ] {
            assert!(!scope_is_valid(bad), "{bad:?}");
        }
        let max = format!("a:{}", "b".repeat(62));
        assert_eq!(max.len(), 64);
        assert!(scope_is_valid(&max));
    }

    #[test]
    fn decode_is_strict() {
        assert!(decode_allowed(serde_json::json!({"allowed": true})).unwrap());
        assert!(!decode_allowed(serde_json::json!({"allowed": false})).unwrap());
        for bad in [
            serde_json::json!({}),
            serde_json::json!({"allowed": "true"}),
            serde_json::json!({"allowed": 1}),
            serde_json::json!({"allowed": null}),
            serde_json::json!({"allowed": true, "extra": 1}),
            serde_json::json!(true),
            serde_json::json!(null),
        ] {
            assert!(
                matches!(decode_allowed(bad.clone()), Err(authz::Error::Backend(_))),
                "{bad}"
            );
        }
    }

    #[test]
    fn error_codes_map_per_contract() {
        for code in ["not_implemented", "feature_disabled", "unavailable"] {
            assert!(matches!(
                authz_error_from(denied(code, "m")),
                authz::Error::Unavailable(m) if m == "m"
            ));
        }
        assert!(matches!(
            authz_error_from(denied("invalid_args", "bad")),
            authz::Error::Invalid(m) if m == "bad"
        ));
        for code in ["backend", "not_granted", "weird"] {
            assert!(matches!(
                authz_error_from(denied(code, "m")),
                authz::Error::Backend(m) if m == "m"
            ));
        }
        assert!(matches!(
            authz_error_from(ExecutorError::ConnectionUnavailable),
            authz::Error::Backend(_)
        ));
    }

    #[tokio::test]
    async fn a_bad_scope_is_refused_before_any_host_call() {
        // No bridge: an attempted call would surface as `backend`, so
        // `Invalid` proves none was made.
        let mut state = ExecState::new(None, "waddles.test.app".to_string(), 1);
        for bad in ["", "admin", "Community:admin", &"a".repeat(65)] {
            assert!(matches!(
                authz::Host::caller_can(&mut state, bad.to_string()).await,
                Err(authz::Error::Invalid(_))
            ));
        }
        assert!(matches!(
            authz::Host::caller_can(&mut state, "community:admin".to_string()).await,
            Err(authz::Error::Backend(_))
        ));
    }
}

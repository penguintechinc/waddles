//! `economy` (issue #714): the `world stage-next` import that lets a bundle
//! move the shared community currency. Cloned from the `reputation` import in
//! [`super::stage_next_imports`].
//!
//! Every call rides the same [`imports::call`](crate::host::imports::call)
//! round trip as every other capability. `penguin-bundle-host`'s closed
//! `CapabilityKind` has no `economy` member, so calls are carried as
//! `capability = db`, `op = "economy.balance" | "economy.wager" |
//! "economy.transfer" | "economy.max_bet" | "economy.leaderboard"`; the stage
//! (`core/svc_process::capabilities`) dispatches on that op prefix BEFORE its
//! `storage.tables` path and authorizes through the capability gate with an
//! `EconomyScoped` resource. Nothing here decides authority, and a stage that
//! has no economy handler answers `not_implemented`, which surfaces to the
//! guest as `unavailable` -- fail-loud, never a default balance.

use penguin_bundle_host::wire::CapabilityKind;

use crate::engine::stage_next_world::waddle::bundle::economy;
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

/// Wire shape of a successful `economy.balance` / `economy.wager` host-result.
#[derive(Debug, serde::Deserialize)]
struct BalanceWire {
    balance: i64,
}

/// Wire shape of a successful `economy.max_bet` host-result.
#[derive(Debug, serde::Deserialize)]
struct MaxBetWire {
    max_bet: i64,
}

/// One leaderboard row on the wire.
#[derive(Debug, serde::Deserialize)]
struct EntryWire {
    user: String,
    balance: i64,
}

/// Wire shape of a successful `economy.leaderboard` host-result.
#[derive(Debug, serde::Deserialize)]
struct LeaderboardWire {
    entries: Vec<EntryWire>,
}

fn malformed(e: impl std::fmt::Display) -> economy::Error {
    economy::Error::Backend(format!("malformed host-result: {e}"))
}

fn decode_balance(value: serde_json::Value) -> Result<i64, economy::Error> {
    serde_json::from_value::<BalanceWire>(value)
        .map(|w| w.balance)
        .map_err(malformed)
}

fn decode_max_bet(value: serde_json::Value) -> Result<i64, economy::Error> {
    serde_json::from_value::<MaxBetWire>(value)
        .map(|w| w.max_bet)
        .map_err(malformed)
}

fn decode_leaderboard(value: serde_json::Value) -> Result<Vec<economy::Entry>, economy::Error> {
    serde_json::from_value::<LeaderboardWire>(value)
        .map(|w| {
            w.entries
                .into_iter()
                .map(|e| economy::Entry {
                    user: e.user,
                    balance: e.balance,
                })
                .collect()
        })
        .map_err(malformed)
}

/// Parses the bare-decimal wire message the stage uses for the two numeric
/// refusals (`insufficient_funds`, `over_cap`). A non-numeric message means
/// the stage and executor disagree about the contract: surfaced loudly as
/// `backend`, never as a made-up number.
fn numeric_message(code: &str, message: &str) -> Result<u64, economy::Error> {
    message
        .trim()
        .parse::<u64>()
        .map_err(|_| economy::Error::Backend(format!("malformed {code} message from the stage")))
}

/// Maps a stage-side error code onto the WIT `economy.error` variant.
/// Gate denial codes (`not_granted`, `amount_out_of_bounds`, `quota_exceeded`,
/// `rate_limited`, `instance_denied`, ...) all surface as `denied(<code>)` so
/// a bundle can branch on the stable code; `user_not_in_scope` (gate) and
/// `not_a_member` (store) both mean "a named user is not in this community".
/// The money-safety refusals are `denied(<code>)` too: `actor_mismatch` (the
/// account named as payer is not the invocation's actor), `idempotency_conflict`
/// (a replayed call's parameters differ from the original) and the identity
/// resolution refusals that can stop an actor from being bound (`not_linked`,
/// `not_found`, `ambiguous`); an identity backend outage (`unavailable`) is
/// `unavailable`, like every other wiring state.
fn economy_error_from(err: ExecutorError) -> economy::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "user_not_in_scope" | "not_a_member" => economy::Error::NotAMember,
            "insufficient_funds" => match numeric_message(code, message) {
                Ok(balance) => economy::Error::InsufficientFunds(balance),
                Err(e) => e,
            },
            "over_cap" => match numeric_message(code, message) {
                Ok(cap) => economy::Error::OverCap(cap),
                Err(e) => e,
            },
            "invalid_args" => economy::Error::Invalid(message.clone()),
            "not_implemented" | "feature_disabled" | "unavailable" => {
                economy::Error::Unavailable(message.clone())
            }
            "backend" => economy::Error::Backend(message.clone()),
            other => economy::Error::Denied(other.to_string()),
        },
        other => economy::Error::Backend(other.to_string()),
    }
}

impl economy::Host for ExecState {
    async fn balance(&mut self, user: String) -> Result<i64, economy::Error> {
        let args = serde_json::json!({ "user": user });
        match call(self, CapabilityKind::Db, "economy.balance", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn wager(
        &mut self,
        user: String,
        stake: u64,
        payout: u64,
    ) -> Result<i64, economy::Error> {
        let args = serde_json::json!({ "user": user, "stake": stake, "payout": payout });
        match call(self, CapabilityKind::Db, "economy.wager", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn transfer(
        &mut self,
        from_user: String,
        to_user: String,
        amount: u64,
    ) -> Result<(), economy::Error> {
        let args = serde_json::json!({ "from": from_user, "to": to_user, "amount": amount });
        match call(self, CapabilityKind::Db, "economy.transfer", args).await {
            Ok(_) => Ok(()),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn max_bet(&mut self, user: String) -> Result<i64, economy::Error> {
        let args = serde_json::json!({ "user": user });
        match call(self, CapabilityKind::Db, "economy.max_bet", args).await {
            Ok(value) => decode_max_bet(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn leaderboard(&mut self, limit: u32) -> Result<Vec<economy::Entry>, economy::Error> {
        let args = serde_json::json!({ "limit": limit });
        match call(self, CapabilityKind::Db, "economy.leaderboard", args).await {
            Ok(value) => decode_leaderboard(value),
            Err(e) => Err(economy_error_from(e)),
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
            op: "economy.wager",
            code: code.to_string(),
            message: message.to_string(),
        }
    }

    #[test]
    fn numeric_refusals_decode_their_number() {
        assert!(matches!(
            economy_error_from(denied("insufficient_funds", "42")),
            economy::Error::InsufficientFunds(42)
        ));
        assert!(matches!(
            economy_error_from(denied("over_cap", " 1000 ")),
            economy::Error::OverCap(1000)
        ));
    }

    #[test]
    fn a_malformed_numeric_refusal_is_a_loud_backend_error_not_a_made_up_number() {
        for code in ["insufficient_funds", "over_cap"] {
            match economy_error_from(denied(code, "not a number")) {
                economy::Error::Backend(m) => assert!(m.contains(code), "{m}"),
                other => panic!("expected backend, got {other:?}"),
            }
        }
        // Negative numbers are not valid u64s either.
        assert!(matches!(
            economy_error_from(denied("over_cap", "-1")),
            economy::Error::Backend(_)
        ));
    }

    #[test]
    fn membership_codes_collapse_to_not_a_member() {
        for code in ["user_not_in_scope", "not_a_member"] {
            assert!(matches!(
                economy_error_from(denied(code, "x")),
                economy::Error::NotAMember
            ));
        }
    }

    #[test]
    fn wiring_codes_are_unavailable_and_gate_codes_are_denied() {
        for code in ["not_implemented", "feature_disabled", "unavailable"] {
            assert!(matches!(
                economy_error_from(denied(code, "m")),
                economy::Error::Unavailable(m) if m == "m"
            ));
        }
        for code in [
            "not_granted",
            "amount_out_of_bounds",
            "quota_exceeded",
            "rate_limited",
            "instance_denied",
            "actor_mismatch",
            "idempotency_conflict",
            "not_linked",
        ] {
            assert!(matches!(
                economy_error_from(denied(code, "m")),
                economy::Error::Denied(c) if c == code
            ));
        }
        assert!(matches!(
            economy_error_from(denied("invalid_args", "bad")),
            economy::Error::Invalid(m) if m == "bad"
        ));
        assert!(matches!(
            economy_error_from(denied("backend", "b")),
            economy::Error::Backend(m) if m == "b"
        ));
    }

    #[test]
    fn non_denial_executor_errors_are_backend() {
        assert!(matches!(
            economy_error_from(ExecutorError::ConnectionUnavailable),
            economy::Error::Backend(_)
        ));
    }

    #[test]
    fn success_payloads_decode_and_malformed_ones_fail_loud() {
        assert_eq!(
            decode_balance(serde_json::json!({ "balance": 7 })).unwrap(),
            7
        );
        assert_eq!(
            decode_max_bet(serde_json::json!({ "max_bet": 9 })).unwrap(),
            9
        );
        let board = decode_leaderboard(serde_json::json!({
            "entries": [{ "user": "u1", "balance": 5 }, { "user": "u2", "balance": 3 }]
        }))
        .unwrap();
        assert_eq!(board.len(), 2);
        assert_eq!((board[0].user.as_str(), board[0].balance), ("u1", 5));
        for bad in [
            serde_json::json!({}),
            serde_json::json!({ "balance": "x" }),
            serde_json::json!(null),
        ] {
            assert!(matches!(
                decode_balance(bad.clone()),
                Err(economy::Error::Backend(_))
            ));
            assert!(matches!(
                decode_max_bet(bad.clone()),
                Err(economy::Error::Backend(_))
            ));
            assert!(matches!(
                decode_leaderboard(bad),
                Err(economy::Error::Backend(_))
            ));
        }
    }
}

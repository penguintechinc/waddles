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

/// Wire shape of a successful `economy.transfer_from` host-result.
#[derive(Debug, serde::Deserialize)]
struct TransferResultWire {
    from_balance: i64,
    to_balance: i64,
}

fn decode_transfer_result(
    value: serde_json::Value,
) -> Result<economy::TransferResult, economy::Error> {
    serde_json::from_value::<TransferResultWire>(value)
        .map(|w| economy::TransferResult {
            from_balance: w.from_balance,
            to_balance: w.to_balance,
        })
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

    async fn credit(&mut self, user: String, amount: u64) -> Result<i64, economy::Error> {
        let args = serde_json::json!({ "user": user, "amount": amount });
        match call(self, CapabilityKind::Db, "economy.credit", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn batch_credit(
        &mut self,
        entries: Vec<economy::CreditEntry>,
    ) -> Result<(), economy::Error> {
        let args = serde_json::json!({ "entries": entries_json(&entries) });
        match call(self, CapabilityKind::Db, "economy.batch_credit", args).await {
            Ok(_) => Ok(()),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn debit(&mut self, user: String, amount: u64) -> Result<i64, economy::Error> {
        let args = serde_json::json!({ "user": user, "amount": amount });
        match call(self, CapabilityKind::Db, "economy.debit", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn conditional_debit(
        &mut self,
        user: String,
        amount: u64,
        min_balance: u64,
    ) -> Result<i64, economy::Error> {
        let args =
            serde_json::json!({ "user": user, "amount": amount, "min_balance": min_balance });
        match call(self, CapabilityKind::Db, "economy.conditional_debit", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn admin_adjust(
        &mut self,
        user: String,
        adjust: economy::BalanceAdjust,
    ) -> Result<i64, economy::Error> {
        let args = admin_adjust_args(&user, &adjust);
        match call(self, CapabilityKind::Db, "economy.admin_adjust", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn pay_pot(&mut self, winners: Vec<economy::CreditEntry>) -> Result<(), economy::Error> {
        let args = serde_json::json!({ "winners": entries_json(&winners) });
        match call(self, CapabilityKind::Db, "economy.pay_pot", args).await {
            Ok(_) => Ok(()),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn transfer_from(
        &mut self,
        from_user: String,
        to_user: String,
        amount: u64,
        reason: String,
    ) -> Result<economy::TransferResult, economy::Error> {
        let args = serde_json::json!({
            "from": from_user,
            "to": to_user,
            "amount": amount,
            "reason": reason,
        });
        match call(self, CapabilityKind::Db, "economy.transfer_from", args).await {
            Ok(value) => decode_transfer_result(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }

    async fn exchange(
        &mut self,
        user: String,
        from_amount: u64,
        rate_num: u32,
        rate_den: u32,
    ) -> Result<i64, economy::Error> {
        let args = serde_json::json!({
            "user": user,
            "from_amount": from_amount,
            "rate_num": rate_num,
            "rate_den": rate_den,
        });
        match call(self, CapabilityKind::Db, "economy.exchange", args).await {
            Ok(value) => decode_balance(value),
            Err(e) => Err(economy_error_from(e)),
        }
    }
}

/// Wire form of a `credit-entry` list (`[{user, amount}]`).
fn entries_json(entries: &[economy::CreditEntry]) -> serde_json::Value {
    serde_json::Value::Array(
        entries
            .iter()
            .map(|e| serde_json::json!({ "user": e.user, "amount": e.amount }))
            .collect(),
    )
}

/// Wire args of an `admin-adjust`: `{user, mode: "delta"|"set", value}`.
fn admin_adjust_args(user: &str, adjust: &economy::BalanceAdjust) -> serde_json::Value {
    match adjust {
        economy::BalanceAdjust::Delta(d) => {
            serde_json::json!({ "user": user, "mode": "delta", "value": d })
        }
        economy::BalanceAdjust::Set(t) => {
            serde_json::json!({ "user": user, "mode": "set", "value": t })
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

    #[test]
    fn compound_op_wire_args_and_error_codes() {
        let e = vec![
            economy::CreditEntry {
                user: "u1".into(),
                amount: 5,
            },
            economy::CreditEntry {
                user: "u2".into(),
                amount: 7,
            },
        ];
        assert_eq!(
            entries_json(&e),
            serde_json::json!([{"user": "u1", "amount": 5}, {"user": "u2", "amount": 7}])
        );
        assert_eq!(
            admin_adjust_args("u", &economy::BalanceAdjust::Delta(-3)),
            serde_json::json!({"user": "u", "mode": "delta", "value": -3})
        );
        assert_eq!(
            admin_adjust_args("u", &economy::BalanceAdjust::Set(9)),
            serde_json::json!({"user": "u", "mode": "set", "value": 9})
        );
        // The stage's supply refusal rides `over_cap` with the ceiling.
        assert!(matches!(
            economy_error_from(denied("over_cap", "1000000")),
            economy::Error::OverCap(1_000_000)
        ));
        // A non-admin caller surfaces as a typed denial, never a success.
        assert!(matches!(
            economy_error_from(denied("admin_required", "operation requires admin authorization")),
            economy::Error::Denied(c) if c == "admin_required"
        ));
    }

    use crate::host::imports::tests::{one_shot_bridge_capturing, state_with};
    use economy::Host as _;
    use serde_json::json;

    fn credit(user: &str, amount: u64) -> economy::CreditEntry {
        economy::CreditEntry {
            user: user.to_string(),
            amount,
        }
    }

    /// Balance-returning ops each send exactly their args and decode `balance`.
    #[tokio::test]
    async fn balance_ops_cross_the_bridge_with_their_args() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 90})));
        assert_eq!(state_with(bridge).credit("u".into(), 10).await.unwrap(), 90);
        assert_eq!(args.await.unwrap(), json!({"user": "u", "amount": 10}));

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 80})));
        assert_eq!(state_with(bridge).debit("u".into(), 10).await.unwrap(), 80);
        assert_eq!(args.await.unwrap(), json!({"user": "u", "amount": 10}));

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 70})));
        let got = state_with(bridge)
            .conditional_debit("u".into(), 10, 50)
            .await
            .unwrap();
        assert_eq!(got, 70);
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u", "amount": 10, "min_balance": 50})
        );

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 33})));
        let got = state_with(bridge)
            .exchange("u".into(), 100, 1, 3)
            .await
            .unwrap();
        assert_eq!(got, 33);
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u", "from_amount": 100, "rate_num": 1, "rate_den": 3})
        );

        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"max_bet": 25})));
        assert_eq!(state_with(bridge).max_bet("u".into()).await.unwrap(), 25);
        assert_eq!(args.await.unwrap(), json!({"user": "u"}));
    }

    /// `admin_adjust` encodes both modes and decodes the new balance.
    #[tokio::test]
    async fn admin_adjust_encodes_delta_and_set_modes() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 5})));
        let got = state_with(bridge)
            .admin_adjust("u".into(), economy::BalanceAdjust::Delta(-3))
            .await
            .unwrap();
        assert_eq!(got, 5);
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u", "mode": "delta", "value": -3})
        );
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"balance": 9})));
        state_with(bridge)
            .admin_adjust("u".into(), economy::BalanceAdjust::Set(9))
            .await
            .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"user": "u", "mode": "set", "value": 9})
        );
    }

    /// List-carrying ops send `[{user, amount}]` and ignore the result body.
    #[tokio::test]
    async fn batch_credit_and_pay_pot_send_entry_lists() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
        state_with(bridge)
            .batch_credit(vec![credit("a", 1), credit("b", 2)])
            .await
            .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"entries": [{"user": "a", "amount": 1}, {"user": "b", "amount": 2}]})
        );
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
        state_with(bridge)
            .pay_pot(vec![credit("w", 7)])
            .await
            .unwrap();
        assert_eq!(
            args.await.unwrap(),
            json!({"winners": [{"user": "w", "amount": 7}]})
        );
    }

    /// `leaderboard` and `transfer_from` decode their structured results.
    #[tokio::test]
    async fn leaderboard_and_transfer_from_decode() {
        let (bridge, args) = one_shot_bridge_capturing(Ok(json!({
            "entries": [{"user": "a", "balance": 9}, {"user": "b", "balance": 4}]
        })));
        let rows = state_with(bridge).leaderboard(2).await.unwrap();
        assert_eq!(args.await.unwrap(), json!({"limit": 2}));
        assert_eq!(rows.len(), 2);
        assert_eq!((rows[0].user.as_str(), rows[0].balance), ("a", 9));

        let (bridge, args) =
            one_shot_bridge_capturing(Ok(json!({"from_balance": 4, "to_balance": 16})));
        let r = state_with(bridge)
            .transfer_from("f".into(), "t".into(), 6, "steal.win".into())
            .await
            .unwrap();
        assert_eq!((r.from_balance, r.to_balance), (4, 16));
        assert_eq!(
            args.await.unwrap(),
            json!({"from": "f", "to": "t", "amount": 6, "reason": "steal.win"})
        );

        let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"from_balance": 4})));
        assert!(matches!(
            state_with(bridge)
                .transfer_from("f".into(), "t".into(), 6, "r".into())
                .await,
            Err(economy::Error::Backend(m)) if m.contains("malformed host-result")
        ));
    }

    /// Stage refusals reach the typed variants through the host methods.
    #[tokio::test]
    async fn stage_refusals_surface_as_typed_economy_errors() {
        let refuse = |code: &str, msg: &str| {
            one_shot_bridge_capturing(Err(penguin_bundle_host::wire::HostResultError {
                code: code.to_string(),
                message: msg.to_string(),
            }))
            .0
        };
        assert!(matches!(
            state_with(refuse("insufficient_funds", "3"))
                .debit("u".into(), 9)
                .await,
            Err(economy::Error::InsufficientFunds(3))
        ));
        assert!(matches!(
            state_with(refuse("over_cap", "100"))
                .credit("u".into(), 999)
                .await,
            Err(economy::Error::OverCap(100))
        ));
        assert!(matches!(
            state_with(refuse("not_a_member", ""))
                .max_bet("u".into())
                .await,
            Err(economy::Error::NotAMember)
        ));
        assert!(matches!(
            state_with(refuse("quota_exceeded", "")).pay_pot(vec![]).await,
            Err(economy::Error::Denied(c)) if c == "quota_exceeded"
        ));
        assert!(matches!(
            state_with(refuse("feature_disabled", "flag off")).leaderboard(1).await,
            Err(economy::Error::Unavailable(m)) if m == "flag off"
        ));
        assert!(matches!(
            state_with(refuse("backend", "db down"))
                .batch_credit(vec![credit("a", 1)])
                .await,
            Err(economy::Error::Backend(m)) if m == "db down"
        ));
    }
}

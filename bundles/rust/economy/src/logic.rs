//! Community currency policy over atomic host calls.
//! Each mutation dispatches exactly one host operation; caps and identity remain host-owned.
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::Arc;
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Atomic currency operations supplied by the real WASM host adapter.
pub trait EconomyHost: Send + Sync {
    fn balance(&self, user: &str) -> Result<i64, ToolError>;
    fn credit(&self, user: &str, amount: u64) -> Result<i64, ToolError>;
    fn debit(&self, user: &str, amount: u64) -> Result<i64, ToolError>;
    fn transfer(&self, from: &str, to: &str, amount: u64) -> Result<(), ToolError>;
    fn admin_set(&self, user: &str, value: u64) -> Result<i64, ToolError>;
    fn admin_delta(&self, user: &str, delta: i64) -> Result<i64, ToolError>;
    fn pay_pot(&self, winners: &[(String, u64)]) -> Result<(), ToolError>;
    fn exchange(&self, user: &str, amount: u64, num: u32, den: u32) -> Result<i64, ToolError>;
    fn leaderboard(&self, limit: u32) -> Result<Vec<(String, i64)>, ToolError>;
}

/// Community-and-app scoped configuration; missing values have documented defaults.
pub trait ConfigStore: Send + Sync {
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Policy {
    alias: String,
    rate_num: u32,
    rate_den: u32,
}

/// Rejects malformed caller values with a stable, non-sensitive error.
fn invalid(message: &str) -> ToolError {
    ToolError::InvalidArgs(message.into())
}

/// Loads one community's policy and refuses corrupted or unavailable configuration.
fn policy(store: &dyn ConfigStore) -> Result<Policy, ToolError> {
    let value = match store
        .get("policy")
        .map_err(|_| ToolError::TargetUnavailable)?
    {
        None => Policy {
            alias: "waddlebucks".into(),
            rate_num: 1000,
            rate_den: 500,
        },
        Some(bytes) => serde_json::from_slice::<Policy>(&bytes)
            .map_err(|_| invalid("invalid currency policy"))?,
    };
    if value.alias.is_empty()
        || value.alias.len() > 32
        || !value
            .alias
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b' ' | b'_' | b'-'))
        || value.rate_num == 0
        || value.rate_den == 0
        || value.rate_num > 1_000_000
        || value.rate_den > 1_000_000
        || u64::from(value.rate_num) > u64::from(value.rate_den) * 1000
    {
        return Err(invalid("invalid currency policy"));
    }
    Ok(value)
}

/// Rejects unknown fields so caller-asserted policy or actor information cannot be ignored.
fn fields(args: &Value, allowed: &[&str]) -> Result<(), ToolError> {
    let object = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    if object.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(invalid("unknown field"));
    }
    Ok(())
}

/// Canonical UUID parsing without accepting usernames or extra bytes.
fn user(args: &Value, key: &str) -> Result<String, ToolError> {
    let s = args
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("user must be a UUID"))?;
    if s.len() != 36
        || !s.bytes().enumerate().all(|(i, b)| match i {
            8 | 13 | 18 | 23 => b == b'-',
            _ => b.is_ascii_hexdigit(),
        })
    {
        return Err(invalid("user must be a UUID"));
    }
    Ok(s.to_ascii_lowercase())
}

/// Bounds JSON integers to the host's signed balance representation.
fn amount(args: &Value, key: &str, minimum: u64) -> Result<u64, ToolError> {
    args.get(key)
        .and_then(Value::as_u64)
        .filter(|n| *n >= minimum && *n <= i64::MAX as u64)
        .ok_or_else(|| invalid("invalid amount"))
}

/// Every write requires the host-forwarded retry key before any mutation.
fn write(ctx: &ToolContext) -> Result<(), ToolError> {
    if ctx
        .idempotency_key
        .as_ref()
        .is_none_or(|k| k.is_empty() || k.len() > 128)
    {
        return Err(invalid("idempotency key required"));
    }
    Ok(())
}

/// Admin decisions consume only host-verified context, never caller arguments.
fn admin(ctx: &ToolContext) -> Result<(), ToolError> {
    if ctx.has_scope("community:admin") {
        Ok(())
    } else {
        Err(ToolError::Forbidden)
    }
}

/// Builds the full service tool table over an atomic capability adapter.
pub fn build_registry(host: Arc<dyn EconomyHost>, store: Arc<dyn ConfigStore>) -> ToolRegistry {
    let mut registry = ToolRegistry::new();
    for name in [
        "get_balance",
        "points_view",
        "adjust_balance",
        "transfer",
        "give",
        "points_set",
        "points_adjust",
        "pay_pot",
        "exchange",
        "points_leaders",
    ] {
        let host = host.clone();
        let store = store.clone();
        registry.register(name, move |ctx, args| {
            run(name, ctx, args, host.as_ref(), store.as_ref())
        });
    }
    registry
}

/// Dispatches one validated request to one atomic host operation.
fn run(
    name: &str,
    ctx: &ToolContext,
    args: Value,
    host: &dyn EconomyHost,
    store: &dyn ConfigStore,
) -> Result<Value, ToolError> {
    let config = policy(store)?;
    match name {
        "get_balance" | "points_view" => {
            fields(&args, &["user"])?;
            let user = user(&args, "user")?;
            Ok(json!({"user": user, "balance": host.balance(&user)?, "alias": config.alias}))
        }
        "adjust_balance" => {
            fields(&args, &["user", "delta"])?;
            write(ctx)?;
            let user = user(&args, "user")?;
            let delta = args
                .get("delta")
                .and_then(Value::as_i64)
                .filter(|n| *n != 0 && *n != i64::MIN)
                .ok_or_else(|| invalid("invalid delta"))?;
            let balance = if delta > 0 {
                host.credit(&user, delta as u64)?
            } else {
                host.debit(&user, delta.unsigned_abs())?
            };
            Ok(json!({"user": user, "balance": balance, "alias": config.alias}))
        }
        "transfer" | "give" => {
            fields(&args, &["from", "to", "amount"])?;
            write(ctx)?;
            let from = user(&args, "from")?;
            let to = user(&args, "to")?;
            let amount = amount(&args, "amount", 1)?;
            if from == to {
                return Err(invalid("cannot transfer to oneself"));
            }
            host.transfer(&from, &to, amount)?;
            Ok(json!({"from": from, "to": to, "amount": amount, "alias": config.alias}))
        }
        "points_set" => {
            fields(&args, &["user", "balance"])?;
            write(ctx)?;
            admin(ctx)?;
            let user = user(&args, "user")?;
            let value = amount(&args, "balance", 0)?;
            Ok(json!({"user":user,"balance":host.admin_set(&user,value)?,"alias":config.alias}))
        }
        "points_adjust" => {
            fields(&args, &["user", "delta"])?;
            write(ctx)?;
            admin(ctx)?;
            let user = user(&args, "user")?;
            let delta = args
                .get("delta")
                .and_then(Value::as_i64)
                .filter(|n| *n != 0 && *n != i64::MIN)
                .ok_or_else(|| invalid("invalid delta"))?;
            Ok(json!({"user":user,"balance":host.admin_delta(&user,delta)?,"alias":config.alias}))
        }
        "pay_pot" => {
            fields(&args, &["winners"])?;
            write(ctx)?;
            let rows = args
                .get("winners")
                .and_then(Value::as_array)
                .filter(|a| !a.is_empty() && a.len() <= 100)
                .ok_or_else(|| invalid("invalid winners"))?;
            let mut seen = HashSet::new();
            let mut winners = Vec::new();
            let mut total = 0u64;
            for row in rows {
                fields(row, &["user", "amount"])?;
                let user = user(row, "user")?;
                let amount = amount(row, "amount", 1)?;
                if !seen.insert(user.clone()) {
                    return Err(invalid("duplicate winner"));
                }
                total = total
                    .checked_add(amount)
                    .filter(|n| *n <= i64::MAX as u64)
                    .ok_or_else(|| invalid("payout overflow"))?;
                winners.push((user, amount));
            }
            host.pay_pot(&winners)?;
            Ok(json!({"paid": total,"alias":config.alias}))
        }
        "exchange" => {
            fields(&args, &["user", "from_amount"])?;
            write(ctx)?;
            let user = user(&args, "user")?;
            let value = amount(&args, "from_amount", 1)?;
            Ok(
                json!({"user":user,"balance":host.exchange(&user,value,config.rate_num,config.rate_den)?,"alias":config.alias}),
            )
        }
        "points_leaders" => {
            fields(&args, &["limit"])?;
            let limit = match args.get("limit") {
                None => 10,
                Some(v) => v
                    .as_u64()
                    .filter(|n| (1..=100).contains(n))
                    .ok_or_else(|| invalid("invalid limit"))? as u32,
            };
            let entries = host
                .leaderboard(limit)?
                .into_iter()
                .map(|(user, balance)| json!({"user":user,"balance":balance}))
                .collect::<Vec<_>>();
            Ok(json!({"entries":entries,"alias":config.alias}))
        }
        _ => Err(invalid("unknown tool")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    const ALICE: &str = "11111111-1111-4111-8111-111111111111";
    const BOB: &str = "22222222-2222-4222-8222-222222222222";

    /// Records the bundle-to-capability boundary; host-store semantics use real Postgres tests.
    #[derive(Default)]
    struct Host {
        calls: Mutex<Vec<String>>,
    }
    impl Host {
        fn record(&self, v: String) {
            self.calls.lock().unwrap().push(v);
        }
    }
    impl EconomyHost for Host {
        fn balance(&self, u: &str) -> Result<i64, ToolError> {
            self.record(format!("balance:{u}"));
            Ok(12)
        }
        fn credit(&self, u: &str, n: u64) -> Result<i64, ToolError> {
            self.record(format!("credit:{u}:{n}"));
            Ok(20)
        }
        fn debit(&self, u: &str, n: u64) -> Result<i64, ToolError> {
            self.record(format!("debit:{u}:{n}"));
            Ok(10)
        }
        fn transfer(&self, f: &str, t: &str, n: u64) -> Result<(), ToolError> {
            self.record(format!("transfer:{f}:{t}:{n}"));
            Ok(())
        }
        fn admin_set(&self, u: &str, n: u64) -> Result<i64, ToolError> {
            self.record(format!("admin:{u}:{n}"));
            Ok(n as i64)
        }
        fn admin_delta(&self, u: &str, n: i64) -> Result<i64, ToolError> {
            self.record(format!("admin_delta:{u}:{n}"));
            Ok(12)
        }
        fn pay_pot(&self, w: &[(String, u64)]) -> Result<(), ToolError> {
            self.record(format!("pot:{}", w.len()));
            Ok(())
        }
        fn exchange(&self, u: &str, n: u64, a: u32, b: u32) -> Result<i64, ToolError> {
            self.record(format!("exchange:{u}:{n}:{a}:{b}"));
            Ok(25)
        }
        fn leaderboard(&self, n: u32) -> Result<Vec<(String, i64)>, ToolError> {
            self.record(format!("leaders:{n}"));
            Ok(vec![(ALICE.into(), 12)])
        }
    }
    struct Kv(Option<Vec<u8>>);
    impl ConfigStore for Kv {
        fn get(&self, _: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.0.clone())
        }
    }
    fn setup(config: Option<Value>) -> (ToolRegistry, Arc<Host>) {
        let h = Arc::new(Host::default());
        (
            build_registry(
                h.clone(),
                Arc::new(Kv(config.map(|v| serde_json::to_vec(&v).unwrap()))),
            ),
            h,
        )
    }
    fn call(r: &ToolRegistry, name: &str, args: Value) -> Result<Value, ToolError> {
        serde_json::from_slice(&r.call(name, &serde_json::to_vec(&args).unwrap())?)
            .map_err(|_| invalid("bad test response"))
    }
    fn keyed(mut args: Value) -> Value {
        args["__idempotency_key"] = json!("retry-1");
        args
    }

    #[test]
    fn reads_and_aliases_resolve_through_registry() {
        let (r, h) = setup(None);
        for name in ["get_balance", "points_view"] {
            assert_eq!(
                call(&r, name, json!({"user":ALICE})).unwrap()["alias"],
                "waddlebucks"
            );
        }
        assert_eq!(
            call(&r, "points_leaders", json!({})).unwrap()["entries"][0]["balance"],
            12
        );
        assert_eq!(h.calls.lock().unwrap().len(), 3);
    }
    #[test]
    fn writes_map_to_one_atomic_operation() {
        let (r, h) = setup(None);
        for (name, args) in [
            ("adjust_balance", json!({"user":ALICE,"delta":5})),
            ("adjust_balance", json!({"user":ALICE,"delta":-5})),
            ("transfer", json!({"from":ALICE,"to":BOB,"amount":5})),
            ("give", json!({"from":ALICE,"to":BOB,"amount":5})),
            (
                "pay_pot",
                json!({"winners":[{"user":ALICE,"amount":2},{"user":BOB,"amount":3}]}),
            ),
            ("exchange", json!({"user":ALICE,"from_amount":1000})),
        ] {
            call(&r, name, keyed(args)).unwrap();
        }
        let c = h.calls.lock().unwrap();
        assert_eq!(c.len(), 6);
        assert!(c[0].starts_with("credit:"));
        assert!(c[1].starts_with("debit:"));
        assert_eq!(c[4], "pot:2");
        assert_eq!(c[5], format!("exchange:{ALICE}:1000:1000:500"));
    }
    /// Spec 2026-10-29-waddlebucks-economy: default is 1000 waddlebucks per 500 Twitch points.
    #[test]
    fn default_exchange_rate_is_1000_wb_per_500_points() {
        let (r, h) = setup(None);
        call(
            &r,
            "exchange",
            keyed(json!({"user":ALICE,"from_amount":500})),
        )
        .unwrap();
        let c = h.calls.lock().unwrap();
        assert_eq!(c[0], format!("exchange:{ALICE}:500:1000:500"));
        // credited = points * rate_num / rate_den
        assert_eq!(500u64 * 1000 / 500, 1000);
    }
    #[test]
    fn admin_uses_only_host_verified_context() {
        let (r, h) = setup(None);
        let args = keyed(json!({"user":ALICE,"balance":77}));
        assert_eq!(
            call(&r, "points_set", args.clone()).unwrap_err(),
            ToolError::Forbidden
        );
        let mut privileged = args.clone();
        privileged["__ctx"] = json!({"actor":BOB,"scopes":["community:admin"]});
        assert_eq!(call(&r, "points_set", privileged).unwrap()["balance"], 77);
        let mut no_actor = args;
        no_actor["__ctx"] = json!({"scopes":["community:admin"]});
        assert_eq!(
            call(&r, "points_set", no_actor).unwrap_err(),
            ToolError::Forbidden
        );
        assert_eq!(h.calls.lock().unwrap().len(), 1);
    }
    #[test]
    fn invalid_inputs_and_missing_keys_never_reach_money_host() {
        let (r, h) = setup(None);
        for (name, args) in [
            ("adjust_balance", json!({"user":ALICE,"delta":2})),
            ("adjust_balance", keyed(json!({"user":ALICE,"delta":0}))),
            (
                "adjust_balance",
                keyed(json!({"user":ALICE,"delta":i64::MIN})),
            ),
            (
                "transfer",
                keyed(json!({"from":ALICE,"to":ALICE,"amount":1})),
            ),
            (
                "transfer",
                keyed(json!({"from":ALICE,"to":BOB,"amount":-1})),
            ),
            ("get_balance", json!({"user":"alice"})),
            (
                "pay_pot",
                keyed(json!({"winners":[{"user":ALICE,"amount":1},{"user":ALICE,"amount":1}]})),
            ),
            (
                "exchange",
                keyed(json!({"user":ALICE,"from_amount":10,"rate_num":999})),
            ),
            ("points_leaders", json!({"limit":101})),
        ] {
            assert!(call(&r, name, args).is_err(), "{name}");
        }
        assert!(h.calls.lock().unwrap().is_empty());
    }
    #[test]
    fn policy_is_community_local_and_cannot_be_supplied_by_caller() {
        let (a, ha) = setup(Some(json!({"alias":"stars","rate_num":1,"rate_den":2})));
        let (b, hb) = setup(Some(json!({"alias":"coins","rate_num":2,"rate_den":1})));
        let args = keyed(json!({"user":ALICE,"from_amount":10}));
        assert_eq!(
            call(&a, "exchange", args.clone()).unwrap()["alias"],
            "stars"
        );
        assert_eq!(call(&b, "exchange", args).unwrap()["alias"], "coins");
        assert_eq!(
            ha.calls.lock().unwrap()[0],
            format!("exchange:{ALICE}:10:1:2")
        );
        assert_eq!(
            hb.calls.lock().unwrap()[0],
            format!("exchange:{ALICE}:10:2:1")
        );
    }
    #[test]
    fn corrupt_policy_fails_closed() {
        for v in [
            json!({"alias":"","rate_num":1,"rate_den":2}),
            json!({"alias":"coins","rate_num":1,"rate_den":0}),
            json!({"alias":"coins","rate_num":1001,"rate_den":1}),
        ] {
            let (r, h) = setup(Some(v));
            assert!(call(&r, "get_balance", json!({"user":ALICE})).is_err());
            assert!(h.calls.lock().unwrap().is_empty());
        }
    }
}

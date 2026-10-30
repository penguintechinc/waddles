//! Host-independent reputation tool logic (bundle-MCP-tools P5 Task 3).
//!
//! Every tool is a pure function over two seams -- [`RepHost`] (the
//! `reputation` host capability) and [`ConfigStore`] (the bundle's own kv) --
//! so the whole tool table is unit-tested on the host target with fakes; the
//! wasm glue in `lib.rs` supplies the real implementations.
//!
//! Safety shape: every tool validates its args first (UUIDs, bounded batches,
//! machine-code reasons) and fails LOUD with a typed error -- never a default
//! answer. The caller never names a delta: `adjust` takes an EVENT
//! (`win`/`loss`/`loyalty`) and the community's delta policy decides the
//! points. Writes ride the host's event+ordinal-keyed `adjust-many` /
//! `override-score` (replay-safe) and require the caller's idempotency key.
//! `override` is admin-only: the caller-attested `actor` must be in the
//! community's `admins` kv list (fail-closed when absent or unreadable); the
//! host separately requires the `reputation.community.admin` grant.
//! No tool ever logs or returns anything but UUIDs, scores and tier names.

use std::collections::HashSet;
use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Largest `get_many` batch (matches the host cap's bound).
pub const MAX_BATCH: usize = 100;
/// Largest `leaderboard` page (matches the host cap's bound).
pub const MAX_LEADERBOARD: u32 = 100;
/// Default `leaderboard` page.
pub const DEFAULT_LEADERBOARD: u32 = 10;
/// Largest per-event delta a policy may configure (the grant's declared bound).
pub const MAX_POLICY_DELTA: i32 = 5;
/// Most tiers a community may configure.
pub const MAX_TIERS: usize = 16;
/// Most admins a community may list.
pub const MAX_ADMINS: usize = 100;
/// Longest tier name.
pub const MAX_TIER_NAME: usize = 32;
/// Longest caller-supplied override reason (host ledger column is 100).
const DEFAULT_OVERRIDE_REASON: &str = "admin.override";

/// kv key holding the community's delta policy JSON.
pub const KEY_POLICY: &str = "policy";
/// kv key holding the community's tier ladder JSON.
pub const KEY_TIERS: &str = "tiers";
/// kv key holding the community's admin-actor UUID list JSON.
pub const KEY_ADMINS: &str = "admins";

/// One member's score row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub user: String,
    pub score: i64,
}

/// Every way a host-capability call can fail (mirrors the WIT
/// `reputation.error`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostError {
    NotAMember,
    DailyCapExceeded,
    Denied(String),
    Invalid(String),
    Unavailable(String),
    Backend(String),
}

/// The `reputation` host capability, as the tools use it.
pub trait RepHost: Send + Sync {
    /// Scores for `users` in request order.
    fn get_many(&self, users: &[String]) -> Result<Vec<Entry>, HostError>;
    /// Atomically applies `(user, delta)` items (host-keyed, replay-safe).
    fn adjust_many(&self, items: &[(String, i32)], reason: &str) -> Result<(), HostError>;
    /// ADMIN override to an absolute score; returns the new (clamped) score.
    fn override_score(&self, user: &str, absolute: i64, reason: &str) -> Result<i64, HostError>;
    /// Top `limit` members by score.
    fn leaderboard(&self, limit: u32) -> Result<Vec<Entry>, HostError>;
}

/// The bundle's own (per-community, host-scoped) key/value config store.
pub trait ConfigStore: Send + Sync {
    /// Raw bytes at `key`, `None` if unset; `Err` carries a PII-free reason.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
}

fn invalid(msg: impl Into<String>) -> ToolError {
    ToolError::InvalidArgs(msg.into())
}

/// Maps a host-capability failure onto the tool error taxonomy (stable codes).
pub fn host_to_tool(e: HostError) -> ToolError {
    match e {
        HostError::NotAMember => invalid("not_a_member"),
        HostError::DailyCapExceeded => invalid("daily_cap_exceeded"),
        HostError::Denied(code) if code == "not_granted" => ToolError::NotGranted,
        HostError::Denied(code) => invalid(format!("denied:{code}")),
        HostError::Invalid(m) => invalid(m),
        HostError::Unavailable(_) => ToolError::TargetUnavailable,
        HostError::Backend(_) => invalid("backend"),
    }
}

/// Canonicalizes a UUID string (8-4-4-4-12 hex) to lowercase, else `InvalidArgs`.
pub fn parse_uuid(raw: &str, what: &str) -> Result<String, ToolError> {
    let b = raw.as_bytes();
    let ok = b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        });
    if ok {
        Ok(raw.to_ascii_lowercase())
    } else {
        Err(invalid(format!("{what} must be a UUID")))
    }
}

fn uuid_field(args: &Value, key: &str) -> Result<String, ToolError> {
    let raw = args
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("{key} must be a UUID string")))?;
    parse_uuid(raw, key)
}

/// Rejects any field not in `allowed` (a typo must fail, not be ignored).
fn only_fields(args: &Value, allowed: &[&str]) -> Result<(), ToolError> {
    let obj = args
        .as_object()
        .ok_or_else(|| invalid("args must be an object"))?;
    match obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(invalid(format!("unknown field {k}"))),
        None => Ok(()),
    }
}

/// Validates a machine-code reason (`[a-z0-9._:-]`, 1..=100), as the host does.
fn valid_reason(reason: &str) -> bool {
    !reason.is_empty()
        && reason.len() <= 100
        && reason.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b':' | b'-')
        })
}

// ---- config -------------------------------------------------------------

/// The community's per-event point deltas.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DeltaPolicy {
    pub win: i32,
    pub loss: i32,
    pub loyalty: i32,
}

impl Default for DeltaPolicy {
    fn default() -> Self {
        Self {
            win: 1,
            loss: -1,
            loyalty: 1,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawPolicy {
    win: Option<i32>,
    loss: Option<i32>,
    loyalty: Option<i32>,
}

/// Loads the delta policy: absent -> defaults; present-but-invalid -> LOUD error.
pub fn load_policy(cfg: &dyn ConfigStore) -> Result<DeltaPolicy, ToolError> {
    let Some(bytes) = cfg
        .get(KEY_POLICY)
        .map_err(|_| ToolError::TargetUnavailable)?
    else {
        return Ok(DeltaPolicy::default());
    };
    let raw: RawPolicy =
        serde_json::from_slice(&bytes).map_err(|_| invalid("invalid policy config"))?;
    let d = DeltaPolicy::default();
    let p = DeltaPolicy {
        win: raw.win.unwrap_or(d.win),
        loss: raw.loss.unwrap_or(d.loss),
        loyalty: raw.loyalty.unwrap_or(d.loyalty),
    };
    let in_bound = |v: i32| v != 0 && v.abs() <= MAX_POLICY_DELTA;
    if in_bound(p.win)
        && in_bound(p.loss)
        && in_bound(p.loyalty)
        && p.win > 0
        && p.loss < 0
        && p.loyalty > 0
    {
        Ok(p)
    } else {
        Err(invalid("invalid policy config"))
    }
}

/// One rung of the tier ladder.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tier {
    pub name: String,
    pub min_score: i64,
}

fn default_tiers() -> Vec<Tier> {
    [
        ("outcast", -1_000_000),
        ("newcomer", 0),
        ("regular", 25),
        ("trusted", 100),
        ("legend", 500),
    ]
    .into_iter()
    .map(|(name, min_score)| Tier {
        name: name.to_string(),
        min_score,
    })
    .collect()
}

/// Loads the tier ladder: absent -> defaults; present-but-invalid -> LOUD error.
/// Valid = 1..=16 tiers, strictly ascending `min_score`, the first reaching down
/// to the lowest possible score (so every score has a tier), names
/// `[a-z0-9_-]`, 1..=32 chars.
pub fn load_tiers(cfg: &dyn ConfigStore) -> Result<Vec<Tier>, ToolError> {
    let Some(bytes) = cfg
        .get(KEY_TIERS)
        .map_err(|_| ToolError::TargetUnavailable)?
    else {
        return Ok(default_tiers());
    };
    let tiers: Vec<Tier> =
        serde_json::from_slice(&bytes).map_err(|_| invalid("invalid tier config"))?;
    let name_ok = |n: &str| {
        !n.is_empty()
            && n.len() <= MAX_TIER_NAME
            && n.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'_' | b'-'))
    };
    let ascending = tiers.windows(2).all(|w| w[0].min_score < w[1].min_score);
    let covers = tiers.first().is_some_and(|t| t.min_score <= -1_000_000);
    if tiers.is_empty()
        || tiers.len() > MAX_TIERS
        || !ascending
        || !covers
        || !tiers.iter().all(|t| name_ok(&t.name))
    {
        return Err(invalid("invalid tier config"));
    }
    Ok(tiers)
}

/// The tier a `score` falls in (highest rung whose `min_score` it reaches).
pub fn tier_for(tiers: &[Tier], score: i64) -> &Tier {
    tiers
        .iter()
        .rev()
        .find(|t| score >= t.min_score)
        .unwrap_or(&tiers[0])
}

/// The community's admin-actor UUIDs: absent -> none (override always denied);
/// present-but-invalid -> LOUD error.
pub fn load_admins(cfg: &dyn ConfigStore) -> Result<HashSet<String>, ToolError> {
    let Some(bytes) = cfg
        .get(KEY_ADMINS)
        .map_err(|_| ToolError::TargetUnavailable)?
    else {
        return Ok(HashSet::new());
    };
    let list: Vec<String> =
        serde_json::from_slice(&bytes).map_err(|_| invalid("invalid admins config"))?;
    if list.len() > MAX_ADMINS {
        return Err(invalid("invalid admins config"));
    }
    list.iter()
        .map(|u| parse_uuid(u, "admins[]").map_err(|_| invalid("invalid admins config")))
        .collect()
}

// ---- tools --------------------------------------------------------------

fn entry_json(tiers: &[Tier], e: &Entry) -> Value {
    json!({ "user": e.user, "score": e.score, "tier": tier_for(tiers, e.score).name })
}

fn one_score(host: &dyn RepHost, user: &str) -> Result<Entry, ToolError> {
    host.get_many(&[user.to_string()])
        .map_err(host_to_tool)?
        .into_iter()
        .next()
        .ok_or_else(|| invalid("backend"))
}

/// `get_score {user}` -> `{user, score, tier}`.
pub fn get_score(
    host: &dyn RepHost,
    cfg: &dyn ConfigStore,
    _ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    only_fields(&args, &["user"])?;
    let user = uuid_field(&args, "user")?;
    let tiers = load_tiers(cfg)?;
    Ok(entry_json(&tiers, &one_score(host, &user)?))
}

/// `get_many {users}` -> `{entries: [{user, score, tier}]}` (<= 100, no empty batch).
pub fn get_many(
    host: &dyn RepHost,
    cfg: &dyn ConfigStore,
    _ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    only_fields(&args, &["users"])?;
    let raw = args
        .get("users")
        .and_then(Value::as_array)
        .filter(|a| (1..=MAX_BATCH).contains(&a.len()))
        .ok_or_else(|| invalid(format!("users must hold 1..={MAX_BATCH} UUIDs")))?;
    let users = raw
        .iter()
        .map(|v| {
            v.as_str()
                .ok_or_else(|| invalid("users[] must be UUID strings"))
                .and_then(|s| parse_uuid(s, "users[]"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let tiers = load_tiers(cfg)?;
    let rows = host.get_many(&users).map_err(host_to_tool)?;
    Ok(json!({ "entries": rows.iter().map(|e| entry_json(&tiers, e)).collect::<Vec<_>>() }))
}

fn require_key(ctx: &ToolContext) -> Result<(), ToolError> {
    if ctx.idempotency_key.is_some() {
        Ok(())
    } else {
        Err(invalid("idempotency key required"))
    }
}

/// `adjust {user, event}` (WRITE) -> `{user, delta, score, tier}`. The delta is
/// the community policy's value for `event` (`win`/`loss`/`loyalty`); the caller
/// can never name a number. Applied through the host's keyed `adjust-many` so a
/// redelivered event applies once.
pub fn adjust(
    host: &dyn RepHost,
    cfg: &dyn ConfigStore,
    ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    only_fields(&args, &["user", "event"])?;
    require_key(ctx)?;
    let user = uuid_field(&args, "user")?;
    let (delta, reason) = match args.get("event").and_then(Value::as_str) {
        Some("win") => (load_policy(cfg)?.win, "game.win"),
        Some("loss") => (load_policy(cfg)?.loss, "game.loss"),
        Some("loyalty") => (load_policy(cfg)?.loyalty, "loyalty.bonus"),
        _ => return Err(invalid("event must be one of win, loss, loyalty")),
    };
    let tiers = load_tiers(cfg)?;
    host.adjust_many(&[(user.clone(), delta)], reason)
        .map_err(host_to_tool)?;
    let now = one_score(host, &user)?;
    let mut out = entry_json(&tiers, &now);
    out["delta"] = json!(delta);
    Ok(out)
}

/// `override {actor, user, absolute_score, reason?}` (WRITE, ADMIN) -> `{user,
/// score, tier}`. The caller-attested `actor` MUST be in the community's `admins`
/// list, else `forbidden` BEFORE any host call.
pub fn override_tool(
    host: &dyn RepHost,
    cfg: &dyn ConfigStore,
    ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    only_fields(&args, &["actor", "user", "absolute_score", "reason"])?;
    require_key(ctx)?;
    let actor = uuid_field(&args, "actor")?;
    let user = uuid_field(&args, "user")?;
    let absolute = args
        .get("absolute_score")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("absolute_score must be an integer"))?;
    let reason = match args.get("reason") {
        None | Some(Value::Null) => DEFAULT_OVERRIDE_REASON,
        Some(Value::String(r)) if valid_reason(r) => r.as_str(),
        Some(_) => return Err(invalid("reason must match [a-z0-9._:-], 1..=100")),
    };
    if !load_admins(cfg)?.contains(&actor) {
        return Err(ToolError::Forbidden);
    }
    let tiers = load_tiers(cfg)?;
    let score = host
        .override_score(&user, absolute, reason)
        .map_err(host_to_tool)?;
    Ok(entry_json(&tiers, &Entry { user, score }))
}

/// `leaderboard {limit?}` -> `{entries: [{rank, user, score, tier}]}`.
pub fn leaderboard(
    host: &dyn RepHost,
    cfg: &dyn ConfigStore,
    _ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    only_fields(&args, &["limit"])?;
    let limit = match args.get("limit") {
        None | Some(Value::Null) => DEFAULT_LEADERBOARD,
        Some(v) => v
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| (1..=MAX_LEADERBOARD).contains(n))
            .ok_or_else(|| invalid(format!("limit must be 1..={MAX_LEADERBOARD}")))?,
    };
    let tiers = load_tiers(cfg)?;
    let rows = host.leaderboard(limit).map_err(host_to_tool)?;
    let entries: Vec<Value> = rows
        .iter()
        .enumerate()
        .map(|(i, e)| {
            let mut v = entry_json(&tiers, e);
            v["rank"] = json!(i + 1);
            v
        })
        .collect();
    Ok(json!({ "entries": entries }))
}

/// `rep {user}` (the `!rep` read UX) -> `{user, score, tier, next_tier, text}`
/// where `next_tier` is `{name, needed}` or null at the top rung.
pub fn rep(
    host: &dyn RepHost,
    cfg: &dyn ConfigStore,
    _ctx: &ToolContext,
    args: Value,
) -> Result<Value, ToolError> {
    only_fields(&args, &["user"])?;
    let user = uuid_field(&args, "user")?;
    let tiers = load_tiers(cfg)?;
    let e = one_score(host, &user)?;
    let tier = tier_for(&tiers, e.score);
    let next = tiers.iter().find(|t| t.min_score > e.score);
    let (next_json, text) = match next {
        Some(n) => (
            json!({ "name": n.name, "needed": n.min_score - e.score }),
            format!(
                "Reputation {} ({}); {} more for {}",
                e.score,
                tier.name,
                n.min_score - e.score,
                n.name
            ),
        ),
        None => (
            Value::Null,
            format!("Reputation {} ({}); top tier", e.score, tier.name),
        ),
    };
    Ok(
        json!({ "user": e.user, "score": e.score, "tier": tier.name, "next_tier": next_json, "text": text }),
    )
}

/// Builds the tool table: each tool name routes to its handler over `host`/`cfg`.
pub fn build_registry(host: Arc<dyn RepHost>, cfg: Arc<dyn ConfigStore>) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    type Tool = fn(&dyn RepHost, &dyn ConfigStore, &ToolContext, Value) -> Result<Value, ToolError>;
    let tools: [(&str, Tool); 6] = [
        ("get_score", get_score),
        ("get_many", get_many),
        ("adjust", adjust),
        ("override", override_tool),
        ("leaderboard", leaderboard),
        ("rep", rep),
    ];
    for (name, f) in tools {
        let (h, c) = (Arc::clone(&host), Arc::clone(&cfg));
        r.register(name, move |ctx, args| f(h.as_ref(), c.as_ref(), ctx, args));
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Mutex;

    const U1: &str = "11111111-1111-1111-1111-111111111111";
    const U2: &str = "22222222-2222-2222-2222-222222222222";
    const ADMIN: &str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";

    #[derive(Default)]
    struct FakeHost {
        scores: Mutex<HashMap<String, i64>>,
        calls: Mutex<Vec<String>>,
        fail: Mutex<Option<HostError>>,
    }
    impl FakeHost {
        fn with(scores: &[(&str, i64)]) -> Arc<Self> {
            let h = Self::default();
            *h.scores.lock().unwrap() = scores.iter().map(|(u, s)| (u.to_string(), *s)).collect();
            Arc::new(h)
        }
        fn failing(e: HostError) -> Arc<Self> {
            let h = Self::default();
            *h.fail.lock().unwrap() = Some(e);
            Arc::new(h)
        }
        fn log(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
        fn gate(&self) -> Result<(), HostError> {
            self.fail.lock().unwrap().clone().map_or(Ok(()), Err)
        }
    }
    impl RepHost for FakeHost {
        fn get_many(&self, users: &[String]) -> Result<Vec<Entry>, HostError> {
            self.gate()?;
            self.calls
                .lock()
                .unwrap()
                .push(format!("get_many:{}", users.len()));
            let s = self.scores.lock().unwrap();
            users
                .iter()
                .map(|u| {
                    s.get(u)
                        .map(|v| Entry {
                            user: u.clone(),
                            score: *v,
                        })
                        .ok_or(HostError::NotAMember)
                })
                .collect()
        }
        fn adjust_many(&self, items: &[(String, i32)], reason: &str) -> Result<(), HostError> {
            self.gate()?;
            self.calls
                .lock()
                .unwrap()
                .push(format!("adjust_many:{items:?}:{reason}"));
            let mut s = self.scores.lock().unwrap();
            for (u, d) in items {
                *s.get_mut(u).ok_or(HostError::NotAMember)? += i64::from(*d);
            }
            Ok(())
        }
        fn override_score(
            &self,
            user: &str,
            absolute: i64,
            reason: &str,
        ) -> Result<i64, HostError> {
            self.gate()?;
            self.calls
                .lock()
                .unwrap()
                .push(format!("override:{user}:{absolute}:{reason}"));
            self.scores
                .lock()
                .unwrap()
                .insert(user.to_string(), absolute);
            Ok(absolute)
        }
        fn leaderboard(&self, limit: u32) -> Result<Vec<Entry>, HostError> {
            self.gate()?;
            self.calls
                .lock()
                .unwrap()
                .push(format!("leaderboard:{limit}"));
            let mut v: Vec<Entry> = self
                .scores
                .lock()
                .unwrap()
                .iter()
                .map(|(u, s)| Entry {
                    user: u.clone(),
                    score: *s,
                })
                .collect();
            v.sort_by(|a, b| b.score.cmp(&a.score).then(a.user.cmp(&b.user)));
            v.truncate(limit as usize);
            Ok(v)
        }
    }

    #[derive(Default)]
    struct FakeCfg(HashMap<String, Vec<u8>>, bool);
    impl FakeCfg {
        fn with(pairs: &[(&str, &str)]) -> Arc<Self> {
            Arc::new(Self(
                pairs
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.as_bytes().to_vec()))
                    .collect(),
                false,
            ))
        }
        fn broken() -> Arc<Self> {
            Arc::new(Self(HashMap::new(), true))
        }
    }
    impl ConfigStore for FakeCfg {
        fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            if self.1 {
                return Err("kv down".into());
            }
            Ok(self.0.get(key).cloned())
        }
    }

    fn call(
        h: &Arc<FakeHost>,
        c: &Arc<FakeCfg>,
        tool: &str,
        args: Value,
        key: Option<&str>,
    ) -> Result<Value, ToolError> {
        let reg = build_registry(h.clone(), c.clone());
        let mut args = args;
        if let Some(k) = key {
            args[waddle_sdk::tool::IDEMPOTENCY_ARG] = json!(k);
        }
        reg.call(tool, &serde_json::to_vec(&args).unwrap())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }

    #[test]
    fn the_tool_table_routes_every_tool_and_rejects_unknown_ones() {
        let (h, c) = (FakeHost::with(&[(U1, 30)]), FakeCfg::with(&[]));
        for (t, a) in [
            ("get_score", json!({"user": U1})),
            ("get_many", json!({"users": [U1]})),
            ("leaderboard", json!({})),
            ("rep", json!({"user": U1})),
        ] {
            assert!(call(&h, &c, t, a, None).is_ok(), "{t}");
        }
        assert!(
            call(
                &h,
                &c,
                "adjust",
                json!({"user": U1, "event": "win"}),
                Some("k")
            )
            .is_ok()
        );
        let reg = build_registry(h, c);
        assert!(
            matches!(reg.call("nope", b"{}"), Err(ToolError::InvalidArgs(m)) if m.contains("unknown tool"))
        );
    }

    #[test]
    fn get_score_returns_score_and_tier_and_canonicalizes_the_uuid() {
        let (h, c) = (FakeHost::with(&[(U1, 30)]), FakeCfg::with(&[]));
        let out = call(
            &h,
            &c,
            "get_score",
            json!({"user": U1.to_uppercase()}),
            None,
        )
        .unwrap();
        assert_eq!(out, json!({"user": U1, "score": 30, "tier": "regular"}));
    }

    #[test]
    fn arguments_are_validated_before_any_host_call() {
        let (h, c) = (FakeHost::with(&[(U1, 0)]), FakeCfg::with(&[]));
        for (t, a) in [
            ("get_score", json!({})),
            ("get_score", json!({"user": "bob"})),
            ("get_score", json!({"user": U1, "extra": 1})),
            ("get_many", json!({"users": []})),
            ("get_many", json!({"users": ["x"]})),
            ("get_many", json!({"users": vec![U1; MAX_BATCH + 1]})),
            ("leaderboard", json!({"limit": 0})),
            ("leaderboard", json!({"limit": MAX_LEADERBOARD + 1})),
            ("rep", json!({"user": 5})),
        ] {
            assert!(
                matches!(
                    call(&h, &c, t, a.clone(), None),
                    Err(ToolError::InvalidArgs(_))
                ),
                "{t} {a}"
            );
        }
        assert!(call(&h, &c, "get_score", json!([]), None).is_err());
        assert!(h.log().is_empty(), "nothing reached the host");
    }

    #[test]
    fn get_many_keeps_order_and_surfaces_a_non_member_loudly() {
        let (h, c) = (FakeHost::with(&[(U1, 5), (U2, -3)]), FakeCfg::with(&[]));
        let out = call(&h, &c, "get_many", json!({"users": [U2, U1]}), None).unwrap();
        assert_eq!(out["entries"][0]["user"], json!(U2));
        assert_eq!(out["entries"][0]["tier"], json!("outcast"));
        assert_eq!(out["entries"][1]["score"], json!(5));
        let err = call(
            &h,
            &c,
            "get_many",
            json!({"users": [U1, "33333333-3333-3333-3333-333333333333"]}),
            None,
        )
        .unwrap_err();
        assert_eq!(err, ToolError::InvalidArgs("not_a_member".into()));
    }

    #[test]
    fn adjust_applies_the_policy_delta_never_a_caller_number() {
        let h = FakeHost::with(&[(U1, 10)]);
        let c = FakeCfg::with(&[("policy", r#"{"win": 3, "loss": -2}"#)]);
        let out = call(
            &h,
            &c,
            "adjust",
            json!({"user": U1, "event": "win"}),
            Some("k1"),
        )
        .unwrap();
        assert_eq!(
            out,
            json!({"user": U1, "score": 13, "tier": "newcomer", "delta": 3})
        );
        call(
            &h,
            &c,
            "adjust",
            json!({"user": U1, "event": "loss"}),
            Some("k2"),
        )
        .unwrap();
        call(
            &h,
            &c,
            "adjust",
            json!({"user": U1, "event": "loyalty"}),
            Some("k3"),
        )
        .unwrap();
        let log = h.log();
        assert!(
            log.iter()
                .any(|l| l.contains(&format!("[(\"{U1}\", 3)]:game.win")))
        );
        assert!(log.iter().any(|l| l.contains("-2)]:game.loss")));
        assert!(
            log.iter().any(|l| l.contains("1)]:loyalty.bonus")),
            "default loyalty delta"
        );
        // A caller-supplied delta is an unknown field, not honoured.
        let err = call(
            &h,
            &c,
            "adjust",
            json!({"user": U1, "event": "win", "delta": 5}),
            Some("k4"),
        )
        .unwrap_err();
        assert!(matches!(err, ToolError::InvalidArgs(_)));
    }

    #[test]
    fn adjust_is_a_write_it_needs_a_key_and_a_known_event() {
        let (h, c) = (FakeHost::with(&[(U1, 0)]), FakeCfg::with(&[]));
        let no_key = call(&h, &c, "adjust", json!({"user": U1, "event": "win"}), None).unwrap_err();
        assert_eq!(
            no_key,
            ToolError::InvalidArgs("idempotency key required".into())
        );
        for ev in [json!("jackpot"), json!(7), Value::Null] {
            let e = call(
                &h,
                &c,
                "adjust",
                json!({"user": U1, "event": ev}),
                Some("k"),
            )
            .unwrap_err();
            assert!(matches!(e, ToolError::InvalidArgs(_)));
        }
        assert!(h.log().is_empty());
    }

    #[test]
    fn a_bad_policy_config_fails_loud_it_is_never_silently_defaulted() {
        let h = FakeHost::with(&[(U1, 0)]);
        for bad in [
            r#"{"win": 0}"#,
            r#"{"win": 9}"#,
            r#"{"loss": 1}"#,
            r#"{"loyalty": -1}"#,
            r#"{"typo": 1}"#,
            "not json",
        ] {
            let c = FakeCfg::with(&[("policy", bad)]);
            let e = call(
                &h,
                &c,
                "adjust",
                json!({"user": U1, "event": "win"}),
                Some("k"),
            )
            .unwrap_err();
            assert_eq!(
                e,
                ToolError::InvalidArgs("invalid policy config".into()),
                "{bad}"
            );
        }
        assert!(h.log().is_empty());
        assert_eq!(
            call(
                &h,
                &FakeCfg::broken(),
                "adjust",
                json!({"user": U1, "event": "win"}),
                Some("k")
            )
            .unwrap_err(),
            ToolError::TargetUnavailable
        );
    }

    #[test]
    fn tier_config_is_validated_and_drives_the_tier_names() {
        let h = FakeHost::with(&[(U1, 50)]);
        let ok = FakeCfg::with(&[(
            "tiers",
            r#"[{"name":"low","min_score":-1000000},{"name":"high","min_score":40}]"#,
        )]);
        assert_eq!(
            call(&h, &ok, "get_score", json!({"user": U1}), None).unwrap()["tier"],
            json!("high")
        );
        for bad in [
            "[]",
            r#"[{"name":"a","min_score":0}]"#,
            r#"[{"name":"a","min_score":-1000000},{"name":"a","min_score":-1000000}]"#,
            r#"[{"name":"A b","min_score":-1000000}]"#,
            r#"[{"name":"a","min_score":-1000000,"x":1}]"#,
        ] {
            let c = FakeCfg::with(&[("tiers", bad)]);
            let e = call(&h, &c, "get_score", json!({"user": U1}), None).unwrap_err();
            assert_eq!(
                e,
                ToolError::InvalidArgs("invalid tier config".into()),
                "{bad}"
            );
        }
    }

    #[test]
    fn override_is_admin_only_and_checked_before_any_host_call() {
        let h = FakeHost::with(&[(U1, 5)]);
        let args = json!({"actor": ADMIN, "user": U1, "absolute_score": 100});
        // No admins configured: fail closed.
        let none = FakeCfg::with(&[]);
        assert_eq!(
            call(&h, &none, "override", args.clone(), Some("k")).unwrap_err(),
            ToolError::Forbidden
        );
        // Configured, but the actor is not listed.
        let other = FakeCfg::with(&[("admins", &format!(r#"["{U2}"]"#))]);
        assert_eq!(
            call(&h, &other, "override", args.clone(), Some("k")).unwrap_err(),
            ToolError::Forbidden
        );
        assert!(h.log().is_empty(), "the host was never reached");
        // Listed admin (case-insensitive UUID).
        let ok = FakeCfg::with(&[("admins", &format!(r#"["{}"]"#, ADMIN.to_uppercase()))]);
        let out = call(&h, &ok, "override", args, Some("k")).unwrap();
        assert_eq!(out, json!({"user": U1, "score": 100, "tier": "trusted"}));
        assert_eq!(h.log(), vec![format!("override:{U1}:100:admin.override")]);
    }

    #[test]
    fn override_validates_its_arguments_and_a_broken_admins_list_fails_loud() {
        let h = FakeHost::with(&[(U1, 5)]);
        let c = FakeCfg::with(&[("admins", &format!(r#"["{ADMIN}"]"#))]);
        let base = json!({"actor": ADMIN, "user": U1, "absolute_score": 1});
        assert_eq!(
            call(&h, &c, "override", base.clone(), None).unwrap_err(),
            ToolError::InvalidArgs("idempotency key required".into())
        );
        for patch in [
            json!({"absolute_score": "x"}),
            json!({"absolute_score": 1.5}),
            json!({"reason": "Bad Reason"}),
            json!({"actor": "nope"}),
            json!({"extra": 1}),
        ] {
            let mut a = base.clone();
            for (k, v) in patch.as_object().unwrap() {
                a[k] = v.clone();
            }
            assert!(
                matches!(
                    call(&h, &c, "override", a, Some("k")),
                    Err(ToolError::InvalidArgs(_))
                ),
                "{patch}"
            );
        }
        let mut with_reason = base.clone();
        with_reason["reason"] = json!("admin.correction");
        call(&h, &c, "override", with_reason, Some("k")).unwrap();
        assert!(h.log().last().unwrap().ends_with(":admin.correction"));
        for bad in ["not json", r#"["nope"]"#] {
            let c = FakeCfg::with(&[("admins", bad)]);
            assert_eq!(
                call(&h, &c, "override", base.clone(), Some("k2")).unwrap_err(),
                ToolError::InvalidArgs("invalid admins config".into())
            );
        }
    }

    #[test]
    fn leaderboard_ranks_entries_and_defaults_its_limit() {
        let (h, c) = (FakeHost::with(&[(U1, 200), (U2, 10)]), FakeCfg::with(&[]));
        let out = call(&h, &c, "leaderboard", json!({}), None).unwrap();
        assert_eq!(h.log(), vec!["leaderboard:10".to_string()]);
        assert_eq!(
            out["entries"][0],
            json!({"rank": 1, "user": U1, "score": 200, "tier": "trusted"})
        );
        assert_eq!(out["entries"][1]["rank"], json!(2));
        let one = call(&h, &c, "leaderboard", json!({"limit": 1}), None).unwrap();
        assert_eq!(one["entries"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn rep_reports_progress_to_the_next_tier_and_the_top_rung() {
        let c = FakeCfg::with(&[]);
        let h = FakeHost::with(&[(U1, 30), (U2, 900)]);
        let mid = call(&h, &c, "rep", json!({"user": U1}), None).unwrap();
        assert_eq!(mid["next_tier"], json!({"name": "trusted", "needed": 70}));
        assert_eq!(
            mid["text"],
            json!("Reputation 30 (regular); 70 more for trusted")
        );
        let top = call(&h, &c, "rep", json!({"user": U2}), None).unwrap();
        assert_eq!(top["next_tier"], Value::Null);
        assert_eq!(top["text"], json!("Reputation 900 (legend); top tier"));
    }

    #[test]
    fn host_failures_map_to_stable_tool_errors_without_leaking_detail() {
        let c = FakeCfg::with(&[]);
        for (e, want) in [
            (
                HostError::NotAMember,
                ToolError::InvalidArgs("not_a_member".into()),
            ),
            (
                HostError::DailyCapExceeded,
                ToolError::InvalidArgs("daily_cap_exceeded".into()),
            ),
            (
                HostError::Denied("not_granted".into()),
                ToolError::NotGranted,
            ),
            (
                HostError::Denied("quota_exceeded".into()),
                ToolError::InvalidArgs("denied:quota_exceeded".into()),
            ),
            (
                HostError::Unavailable("db password=hunter2".into()),
                ToolError::TargetUnavailable,
            ),
            (
                HostError::Backend("secret detail".into()),
                ToolError::InvalidArgs("backend".into()),
            ),
        ] {
            let h = FakeHost::failing(e);
            assert_eq!(
                call(&h, &c, "get_score", json!({"user": U1}), None).unwrap_err(),
                want
            );
        }
    }
}

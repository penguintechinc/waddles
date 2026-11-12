//! Host-independent reputation SERVICE tool logic (v1.1.0).
//!
//! The service owns the STORE, the TOOLS and the I/O: apply/adjust, reads,
//! history, soft-ban state, caps/idempotency/ledger/audit (all enforced by the
//! host capability) and FLOOR ENFORCEMENT (soft/hard community floors and the
//! tenant floor) -- floors are admin limits, not scoring logic. It holds NO
//! scoring weights and no event->delta mapping: the caller (the default
//! first-party logic bundle `waddles.core.app.reputation-accrual`, or a
//! third-party replacement) names the delta, and the host bounds it.
//!
//! Every tool is a pure function over three seams -- [`RepHost`] (the
//! `reputation` host capability), [`ConfigStore`] (the bundle's own kv) and
//! [`Platform`] (flags + the platform ban tool) -- so the whole tool table is
//! unit-tested on the host target with fakes; `lib.rs` supplies the real wasm
//! implementations.
//!
//! Safety shape: every tool validates its args first (UUIDs, bounded batches,
//! machine-code reasons) and fails LOUD with a typed error -- never a default
//! answer. Scope-restricted tools (`override`, `at_risk`, `set_policy`,
//! `get_policy`) take the verified actor from `ToolContext`, never from args.
//! No tool ever logs or returns anything but UUIDs, scores and tier names.

use std::sync::Arc;

use serde::Deserialize;
use serde_json::{Value, json};
use waddle_sdk::tool::{ToolContext, ToolError, ToolRegistry};

/// Largest `get_many` batch (matches the host cap's bound).
pub const MAX_BATCH: usize = 100;
/// Largest `leaderboard` / `at_risk` / `history` page (host bound).
pub const MAX_PAGE: u32 = 100;
/// Default `leaderboard` page.
pub const DEFAULT_LEADERBOARD: u32 = 10;
/// Default `history` page.
pub const DEFAULT_HISTORY: u32 = 20;
/// Lowest legacy score (mirrors the host's `SCALE_MIN`).
pub const SCALE_MIN: i64 = 300;
/// Highest legacy score (mirrors the host's `SCALE_MAX`).
pub const SCALE_MAX: i64 = 850;
/// Largest activity delta the host accepts per `adjust`.
pub const ACTIVITY_CAP: i32 = 5;
/// Largest moderation delta the host accepts per `apply_moderation`.
pub const MODERATION_CAP: i32 = 200;
/// Most tiers a community may configure.
pub const MAX_TIERS: usize = 16;
/// Longest tier name.
pub const MAX_TIER_NAME: usize = 32;
const DEFAULT_OVERRIDE_REASON: &str = "admin.override";

/// kv key holding the community's floor/threshold policy JSON.
pub const KEY_FLOORS: &str = "floors";
/// kv key holding the community's tier ladder JSON.
pub const KEY_TIERS: &str = "tiers";
/// Scope the verified actor must hold for admin tools (host-enforced too).
pub const ADMIN_SCOPE: &str = "community:admin";
/// PostHog flag gating every floor action (default OFF).
pub const FLAG_AUTO_BAN: &str = "waddles.reputation-auto-ban";
/// Audit reason of a community soft-floor soft ban.
pub const REASON_SOFT: &str = "community_soft_floor";
/// Audit reason of a community hard-floor ban.
pub const REASON_HARD: &str = "community_hard_floor";
/// Audit reason of a tenant-floor ban.
pub const REASON_TENANT: &str = "tenant_floor";
/// Default recommended tenant hard-ban floor (disabled unless the tenant
/// admin enables it in the hub-api tenant policy).
pub const DEFAULT_TENANT_FLOOR: i64 = 350;

/// One member's score row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub user: String,
    pub score: i64,
}

/// Result of an apply call.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Applied {
    pub new_score: i64,
    pub applied: bool,
}

/// One audit-ledger row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoryRow {
    pub id: i64,
    pub delta: i32,
    pub reason_code: String,
    pub kind: String,
    pub balance_after: Option<i64>,
    pub occurred_at: String,
}

/// A user's tenant aggregate.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TenantScore {
    pub score: i64,
    pub communities: u32,
}

/// The tenant's auto-ban policy (hub-api-owned, read-only here).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TenantPolicy {
    pub auto_ban_enabled: bool,
    pub auto_ban_floor: i64,
    pub at_risk_threshold: i64,
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
    /// ACTIVITY-class idempotent delta (host-keyed by event + ordinal).
    fn apply(&self, user: &str, delta: i32, reason: &str) -> Result<Applied, HostError>;
    /// MODERATION-class idempotent delta (`reputation.moderation.write`).
    fn apply_moderation(&self, user: &str, delta: i32, reason: &str) -> Result<Applied, HostError>;
    /// ADMIN override to an absolute score; returns the new (clamped) score.
    fn override_score(&self, user: &str, absolute: i64, reason: &str) -> Result<i64, HostError>;
    /// Top `limit` members by score.
    fn leaderboard(&self, limit: u32) -> Result<Vec<Entry>, HostError>;
    /// `user`'s tenant aggregate.
    fn get_tenant(&self, user: &str) -> Result<TenantScore, HostError>;
    /// The tenant's auto-ban policy, if set.
    fn tenant_policy(&self) -> Result<Option<TenantPolicy>, HostError>;
    /// Newest-first ledger page.
    fn history(
        &self,
        user: &str,
        limit: u32,
        before: Option<i64>,
    ) -> Result<Vec<HistoryRow>, HostError>;
    /// Members strictly below `threshold`, lowest first.
    fn below_threshold(&self, threshold: i64, limit: u32) -> Result<Vec<Entry>, HostError>;
    /// Whether `user` is soft-banned in this community.
    fn is_soft_banned(&self, user: &str) -> Result<bool, HostError>;
    /// Records a soft ban; `true` when newly recorded.
    fn soft_ban(&self, user: &str, reason: &str) -> Result<bool, HostError>;
    /// Lifts a soft ban recorded with exactly `only_reason`.
    fn lift_soft_ban(&self, user: &str, only_reason: &str) -> Result<bool, HostError>;
}

/// The bundle's own (per-community, host-scoped) key/value config store.
pub trait ConfigStore: Send + Sync {
    /// Raw bytes at `key`, `None` if unset; `Err` carries a PII-free reason.
    fn get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Writes `value` at `key` (no TTL).
    fn set(&self, key: &str, value: &[u8]) -> Result<(), String>;
}

/// Ambient platform I/O: the feature flag and the platform ban tool.
pub trait Platform: Send + Sync {
    /// `flags::enabled(key, false)`.
    fn flag_enabled(&self, key: &str) -> bool;
    /// Bans `user` on `platform` through that platform bundle's
    /// `<platform>.moderation.ban` tool. `Err` carries the stable code only.
    fn ban(&self, platform: &str, user: &str, reason: &str, key: &str) -> Result<(), String>;
}

/// Everything a tool handler needs.
pub struct Env {
    pub host: Arc<dyn RepHost>,
    pub cfg: Arc<dyn ConfigStore>,
    pub platform: Arc<dyn Platform>,
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
        HostError::Denied(code) if code == "forbidden" => ToolError::Forbidden,
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

fn reason_field(args: &Value, default: Option<&str>) -> Result<String, ToolError> {
    match args.get("reason") {
        None | Some(Value::Null) => default
            .map(str::to_string)
            .ok_or_else(|| invalid("reason is required")),
        Some(Value::String(r)) if valid_reason(r) => Ok(r.clone()),
        Some(_) => Err(invalid("reason must match [a-z0-9._:-], 1..=100")),
    }
}

fn platform_field(args: &Value) -> Result<Option<String>, ToolError> {
    match args.get("platform") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(p)) if matches!(p.as_str(), "twitch" | "discord" | "youtube") => {
            Ok(Some(p.clone()))
        }
        Some(_) => Err(invalid("platform must be one of twitch, discord, youtube")),
    }
}

fn page_field(args: &Value, default: u32) -> Result<u32, ToolError> {
    match args.get("limit") {
        None | Some(Value::Null) => Ok(default),
        Some(v) => v
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| (1..=MAX_PAGE).contains(n))
            .ok_or_else(|| invalid(format!("limit must be 1..={MAX_PAGE}"))),
    }
}

fn require_key(ctx: &ToolContext) -> Result<&str, ToolError> {
    ctx.idempotency_key
        .as_deref()
        .ok_or_else(|| invalid("idempotency key required"))
}

fn require_admin(ctx: &ToolContext) -> Result<(), ToolError> {
    if ctx.has_scope(ADMIN_SCOPE) {
        Ok(())
    } else {
        Err(ToolError::Forbidden)
    }
}

// ---- floor policy (config only: floors and thresholds, no scoring) --------

/// The community's floor policy. All enforcement is OFF by default; the
/// numbers are the recommended legacy-scale defaults.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FloorPolicy {
    pub soft_ban_enabled: bool,
    pub soft_ban_floor: i64,
    pub hard_ban_enabled: bool,
    pub hard_ban_floor: i64,
    pub at_risk_threshold: i64,
}

impl Default for FloorPolicy {
    fn default() -> Self {
        Self {
            soft_ban_enabled: false,
            soft_ban_floor: 550,
            hard_ban_enabled: false,
            hard_ban_floor: 400,
            at_risk_threshold: 600,
        }
    }
}

impl FloorPolicy {
    /// `300 <= hard < soft < at_risk <= 850` (same rule hub-api enforces).
    pub fn validate(&self) -> Result<(), ToolError> {
        if self.hard_ban_floor >= SCALE_MIN
            && self.hard_ban_floor < self.soft_ban_floor
            && self.soft_ban_floor < self.at_risk_threshold
            && self.at_risk_threshold <= SCALE_MAX
        {
            Ok(())
        } else {
            Err(invalid(
                "policy must satisfy 300 <= hard_ban_floor < soft_ban_floor < at_risk_threshold <= 850",
            ))
        }
    }

    fn to_json(self) -> Value {
        json!({
            "soft_ban_enabled": self.soft_ban_enabled,
            "soft_ban_floor": self.soft_ban_floor,
            "hard_ban_enabled": self.hard_ban_enabled,
            "hard_ban_floor": self.hard_ban_floor,
            "at_risk_threshold": self.at_risk_threshold,
        })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawFloors {
    soft_ban_enabled: Option<bool>,
    soft_ban_floor: Option<i64>,
    hard_ban_enabled: Option<bool>,
    hard_ban_floor: Option<i64>,
    at_risk_threshold: Option<i64>,
}

fn merge_floors(base: FloorPolicy, raw: RawFloors) -> FloorPolicy {
    FloorPolicy {
        soft_ban_enabled: raw.soft_ban_enabled.unwrap_or(base.soft_ban_enabled),
        soft_ban_floor: raw.soft_ban_floor.unwrap_or(base.soft_ban_floor),
        hard_ban_enabled: raw.hard_ban_enabled.unwrap_or(base.hard_ban_enabled),
        hard_ban_floor: raw.hard_ban_floor.unwrap_or(base.hard_ban_floor),
        at_risk_threshold: raw.at_risk_threshold.unwrap_or(base.at_risk_threshold),
    }
}

/// Loads the floor policy: absent -> defaults (all disabled); present but
/// invalid -> LOUD error, never silently defaulted.
pub fn load_floors(cfg: &dyn ConfigStore) -> Result<FloorPolicy, ToolError> {
    let Some(bytes) = cfg
        .get(KEY_FLOORS)
        .map_err(|_| ToolError::TargetUnavailable)?
    else {
        return Ok(FloorPolicy::default());
    };
    let raw: RawFloors =
        serde_json::from_slice(&bytes).map_err(|_| invalid("invalid floors config"))?;
    let p = merge_floors(FloorPolicy::default(), raw);
    p.validate().map_err(|_| invalid("invalid floors config"))?;
    Ok(p)
}

// ---- tiers (display only) -------------------------------------------------

/// One rung of the tier ladder.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Tier {
    pub name: String,
    pub min_score: i64,
}

fn default_tiers() -> Vec<Tier> {
    [
        ("poor", SCALE_MIN),
        ("fair", 580),
        ("good", 670),
        ("very_good", 740),
        ("exceptional", 800),
    ]
    .into_iter()
    .map(|(name, min_score)| Tier {
        name: name.to_string(),
        min_score,
    })
    .collect()
}

/// Loads the tier ladder: absent -> defaults; present but invalid -> LOUD
/// error. Valid = 1..=16 tiers, strictly ascending `min_score` within the
/// scale, the first at or below the scale floor (every score has a tier),
/// names `[a-z0-9_-]`, 1..=32 chars.
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
    let covers = tiers.first().is_some_and(|t| t.min_score <= SCALE_MIN);
    let bounded = tiers.iter().all(|t| t.min_score <= SCALE_MAX);
    if tiers.is_empty()
        || tiers.len() > MAX_TIERS
        || !ascending
        || !covers
        || !bounded
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

// ---- floor enforcement ------------------------------------------------------

/// What the community floors decided for one user after a score change.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FloorAction {
    /// Floors not evaluated (flag OFF).
    Disabled,
    /// An idempotent replay: nothing was applied, so no floor side effect re-fires.
    ReplaySkipped,
    /// Nothing to do.
    None,
    /// Banned on the platform (hard floor / tenant floor).
    Ban,
    /// Soft-banned (floor-triggered).
    SoftBan,
    /// A floor-triggered soft ban was lifted (score recovered).
    SoftBanLifted,
    /// A ban was due but could not be executed; carries the stable code.
    BanFailed(String),
}

impl FloorAction {
    fn label(&self) -> String {
        match self {
            Self::Disabled => "disabled".into(),
            Self::ReplaySkipped => "replay_skipped".into(),
            Self::None => "none".into(),
            Self::Ban => "ban".into(),
            Self::SoftBan => "soft_ban".into(),
            Self::SoftBanLifted => "soft_ban_lifted".into(),
            Self::BanFailed(c) => format!("ban_failed:{c}"),
        }
    }
}

fn try_ban(env: &Env, platform: Option<&str>, user: &str, reason: &str, key: &str) -> FloorAction {
    let Some(platform) = platform else {
        return FloorAction::BanFailed("platform_required".into());
    };
    match env.platform.ban(platform, user, reason, key) {
        Ok(()) => FloorAction::Ban,
        Err(code) => FloorAction::BanFailed(code),
    }
}

/// Evaluates both independent floor policies for `user` at `score`, behind
/// the `waddles.reputation-auto-ban` flag (default OFF):
///
/// * community: `score < hard floor` (enabled) -> ban in this community
///   (`community_hard_floor`); else `score < soft floor` (enabled) -> soft ban
///   (`community_soft_floor`); else a floor-triggered soft ban is lifted
///   (a manual soft ban, recorded under another reason, is never cleared);
/// * tenant: the user's tenant aggregate `< tenant floor` (policy enabled)
///   -> ban (`tenant_floor`). The ban runs through the current community's
///   platform tool; fan-out to the tenant's other communities is a follow-up.
///
/// A failed host/ban call never fails the score change that already applied:
/// it is reported in the result (`ban_failed:<code>`) so it is never silent.
pub fn enforce_floors(
    env: &Env,
    user: &str,
    score: i64,
    platform: Option<&str>,
    key: &str,
) -> Result<(FloorAction, FloorAction), ToolError> {
    if !env.platform.flag_enabled(FLAG_AUTO_BAN) {
        return Ok((FloorAction::Disabled, FloorAction::Disabled));
    }
    let p = load_floors(env.cfg.as_ref())?;
    let community = if p.hard_ban_enabled && score < p.hard_ban_floor {
        try_ban(env, platform, user, REASON_HARD, &format!("{key}:hard"))
    } else if p.soft_ban_enabled && score < p.soft_ban_floor {
        match env.host.soft_ban(user, REASON_SOFT) {
            Ok(_) => FloorAction::SoftBan,
            Err(e) => FloorAction::BanFailed(host_code(&e)),
        }
    } else {
        match env.host.lift_soft_ban(user, REASON_SOFT) {
            Ok(true) => FloorAction::SoftBanLifted,
            Ok(false) => FloorAction::None,
            Err(e) => FloorAction::BanFailed(host_code(&e)),
        }
    };
    let tenant = match env.host.tenant_policy() {
        Ok(Some(tp)) if tp.auto_ban_enabled => match env.host.get_tenant(user) {
            Ok(t) if t.score < tp.auto_ban_floor => {
                try_ban(env, platform, user, REASON_TENANT, &format!("{key}:tenant"))
            }
            Ok(_) => FloorAction::None,
            Err(e) => FloorAction::BanFailed(host_code(&e)),
        },
        Ok(_) => FloorAction::None,
        Err(e) => FloorAction::BanFailed(host_code(&e)),
    };
    Ok((community, tenant))
}

fn host_code(e: &HostError) -> String {
    match e {
        HostError::NotAMember => "not_a_member".into(),
        HostError::DailyCapExceeded => "daily_cap_exceeded".into(),
        HostError::Denied(c) => c.clone(),
        HostError::Invalid(_) => "invalid".into(),
        HostError::Unavailable(_) => "unavailable".into(),
        HostError::Backend(_) => "backend".into(),
    }
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
pub fn get_score(env: &Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user"])?;
    let user = uuid_field(&args, "user")?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    Ok(entry_json(&tiers, &one_score(env.host.as_ref(), &user)?))
}

/// `get_many {users}` -> `{entries: [{user, score, tier}]}` (<= 100).
pub fn get_many(env: &Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
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
    let tiers = load_tiers(env.cfg.as_ref())?;
    let rows = env.host.get_many(&users).map_err(host_to_tool)?;
    Ok(json!({ "entries": rows.iter().map(|e| entry_json(&tiers, e)).collect::<Vec<_>>() }))
}

fn apply_result(
    env: &Env,
    tiers: &[Tier],
    user: &str,
    delta: i32,
    out: Applied,
    platform: Option<&str>,
    key: &str,
) -> Result<Value, ToolError> {
    // A replay (`applied == false`) moved nothing: never re-run floor side effects
    // (a redelivered event must not re-ban or re-soft-ban, e.g. after a manual clear).
    let (community, tenant) = if out.applied {
        enforce_floors(env, user, out.new_score, platform, key)?
    } else {
        (FloorAction::ReplaySkipped, FloorAction::ReplaySkipped)
    };
    let mut v = entry_json(
        tiers,
        &Entry {
            user: user.to_string(),
            score: out.new_score,
        },
    );
    v["delta"] = json!(delta);
    v["applied"] = json!(out.applied);
    v["floors"] = json!({ "community": community.label(), "tenant": tenant.label() });
    Ok(v)
}

/// `adjust {user, delta, reason, platform?}` (WRITE, ACTIVITY class) ->
/// `{user, score, tier, delta, applied, floors}`. The caller (the logic
/// bundle) names the delta; the host bounds it to +/-5 and applies it
/// idempotently (event + ordinal key), then the floors are enforced.
pub fn adjust(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user", "delta", "reason", "platform"])?;
    let key = require_key(ctx)?;
    let user = uuid_field(&args, "user")?;
    let delta = delta_field(&args, ACTIVITY_CAP)?;
    let reason = reason_field(&args, None)?;
    let platform = platform_field(&args)?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let out = env
        .host
        .apply(&user, delta, &reason)
        .map_err(host_to_tool)?;
    apply_result(env, &tiers, &user, delta, out, platform.as_deref(), key)
}

/// `apply_moderation {user, delta, reason, platform?}` (WRITE, dangerous,
/// MODERATION class, +/-200): needs the service's `reputation.moderation.write`
/// grant AND the caller's approved dangerous-tool grant, so a third-party
/// logic bundle cannot make moderation-size moves unless granted.
pub fn apply_moderation(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user", "delta", "reason", "platform"])?;
    let key = require_key(ctx)?;
    let user = uuid_field(&args, "user")?;
    let delta = delta_field(&args, MODERATION_CAP)?;
    let reason = reason_field(&args, None)?;
    let platform = platform_field(&args)?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let out = env
        .host
        .apply_moderation(&user, delta, &reason)
        .map_err(host_to_tool)?;
    apply_result(env, &tiers, &user, delta, out, platform.as_deref(), key)
}

fn delta_field(args: &Value, cap: i32) -> Result<i32, ToolError> {
    let d = args
        .get("delta")
        .and_then(Value::as_i64)
        .and_then(|v| i32::try_from(v).ok())
        .filter(|d| *d != 0 && d.unsigned_abs() <= cap.unsigned_abs())
        .ok_or_else(|| invalid(format!("delta must be a non-zero integer within +/-{cap}")))?;
    Ok(d)
}

/// `override {user, absolute_score, reason?, platform?}` (WRITE, ADMIN).
pub fn override_tool(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user", "absolute_score", "reason", "platform"])?;
    let key = require_key(ctx)?;
    let user = uuid_field(&args, "user")?;
    let absolute = args
        .get("absolute_score")
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid("absolute_score must be an integer"))?;
    let reason = reason_field(&args, Some(DEFAULT_OVERRIDE_REASON))?;
    let platform = platform_field(&args)?;
    require_admin(ctx)?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let score = env
        .host
        .override_score(&user, absolute, &reason)
        .map_err(host_to_tool)?;
    let (community, tenant) = enforce_floors(env, &user, score, platform.as_deref(), key)?;
    let mut v = entry_json(
        &tiers,
        &Entry {
            user: user.clone(),
            score,
        },
    );
    v["floors"] = json!({ "community": community.label(), "tenant": tenant.label() });
    Ok(v)
}

/// `leaderboard {limit?}` -> `{entries: [{rank, user, score, tier}]}`.
/// COMMUNITY-scoped only: there is deliberately no tenant-wide leaderboard.
pub fn leaderboard(env: &Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["limit"])?;
    let limit = page_field(&args, DEFAULT_LEADERBOARD)?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let rows = env.host.leaderboard(limit).map_err(host_to_tool)?;
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

/// `rep {user}` (the `!rep` read UX) -> `{user, score, tier, next_tier, text}`.
pub fn rep(env: &Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user"])?;
    let user = uuid_field(&args, "user")?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let e = one_score(env.host.as_ref(), &user)?;
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

/// `get_tenant_score {}` -> the VERIFIED ACTOR's own tenant aggregate. There
/// is no `user` argument: a caller can only ever read their own aggregate, and
/// a call with no verified actor is `forbidden`.
pub fn get_tenant_score(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &[])?;
    let user = ctx.actor.as_deref().ok_or(ToolError::Forbidden)?;
    let user = parse_uuid(user, "actor")?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let t = env.host.get_tenant(&user).map_err(host_to_tool)?;
    let mut v = entry_json(
        &tiers,
        &Entry {
            user,
            score: t.score,
        },
    );
    v["communities"] = json!(t.communities);
    Ok(v)
}

/// `history {user, limit?, before?}` -> `{entries}`: a member may read their
/// OWN ledger; reading anyone else's needs `community:admin`.
pub fn history(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user", "limit", "before"])?;
    let user = uuid_field(&args, "user")?;
    let own = ctx
        .actor
        .as_deref()
        .is_some_and(|a| a.eq_ignore_ascii_case(&user));
    if !own {
        require_admin(ctx)?;
    }
    let limit = page_field(&args, DEFAULT_HISTORY)?;
    let before = match args.get("before") {
        None | Some(Value::Null) => None,
        Some(v) => Some(
            v.as_i64()
                .filter(|b| *b > 0)
                .ok_or_else(|| invalid("before must be a positive id"))?,
        ),
    };
    let rows = env
        .host
        .history(&user, limit, before)
        .map_err(host_to_tool)?;
    Ok(json!({ "entries": rows.iter().map(|r| json!({
        "id": r.id, "delta": r.delta, "reason_code": r.reason_code, "kind": r.kind,
        "balance_after": r.balance_after, "occurred_at": r.occurred_at,
    })).collect::<Vec<_>>() }))
}

/// `check {user}` -> score, tier, whether the member is soft-banned and
/// whether they are at risk (below the community at-risk threshold).
pub fn check(env: &Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user"])?;
    let user = uuid_field(&args, "user")?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let floors = load_floors(env.cfg.as_ref())?;
    let e = one_score(env.host.as_ref(), &user)?;
    let soft = env.host.is_soft_banned(&user).map_err(host_to_tool)?;
    let mut v = entry_json(&tiers, &e);
    v["soft_banned"] = json!(soft);
    v["at_risk"] = json!(e.score < floors.at_risk_threshold);
    Ok(v)
}

/// `at_risk {limit?}` (ADMIN) -> members below the community at-risk
/// threshold, lowest first.
pub fn at_risk(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["limit"])?;
    require_admin(ctx)?;
    let limit = page_field(&args, DEFAULT_LEADERBOARD)?;
    let tiers = load_tiers(env.cfg.as_ref())?;
    let floors = load_floors(env.cfg.as_ref())?;
    let rows = env
        .host
        .below_threshold(floors.at_risk_threshold, limit)
        .map_err(host_to_tool)?;
    Ok(json!({
        "threshold": floors.at_risk_threshold,
        "entries": rows.iter().map(|e| entry_json(&tiers, e)).collect::<Vec<_>>(),
    }))
}

/// `is_soft_banned {user}` -> `{user, soft_banned}` (cheap read for the
/// game/raffle/giveaway/economy bundles that must not pay a soft-banned user).
pub fn is_soft_banned(env: &Env, _ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &["user"])?;
    let user = uuid_field(&args, "user")?;
    let banned = env.host.is_soft_banned(&user).map_err(host_to_tool)?;
    Ok(json!({ "user": user, "soft_banned": banned }))
}

/// `get_policy {}` (ADMIN) -> the community floor policy plus the read-only
/// tenant policy and the auto-ban flag state.
pub fn get_policy(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(&args, &[])?;
    require_admin(ctx)?;
    let floors = load_floors(env.cfg.as_ref())?;
    let tenant = env.host.tenant_policy().map_err(host_to_tool)?;
    Ok(json!({
        "community": floors.to_json(),
        "tenant": tenant.map(|t| json!({
            "tenant_auto_ban_enabled": t.auto_ban_enabled,
            "tenant_auto_ban_floor": t.auto_ban_floor,
            "tenant_at_risk_threshold": t.at_risk_threshold,
        })),
        "recommended_tenant_floor": DEFAULT_TENANT_FLOOR,
        "auto_ban_flag": env.platform.flag_enabled(FLAG_AUTO_BAN),
    }))
}

/// `set_policy {..}` (ADMIN, WRITE): merges the given floor fields over the
/// current policy, validates `300 <= hard < soft < at_risk <= 850` and stores
/// it. Floors are limits only; this never touches scoring weights.
pub fn set_policy(env: &Env, ctx: &ToolContext, args: Value) -> Result<Value, ToolError> {
    only_fields(
        &args,
        &[
            "soft_ban_enabled",
            "soft_ban_floor",
            "hard_ban_enabled",
            "hard_ban_floor",
            "at_risk_threshold",
        ],
    )?;
    require_key(ctx)?;
    require_admin(ctx)?;
    let raw: RawFloors = serde_json::from_value(args).map_err(|_| invalid("invalid policy"))?;
    let merged = merge_floors(load_floors(env.cfg.as_ref())?, raw);
    merged.validate()?;
    let bytes = serde_json::to_vec(&merged.to_json()).map_err(|_| invalid("invalid policy"))?;
    env.cfg
        .set(KEY_FLOORS, &bytes)
        .map_err(|_| ToolError::TargetUnavailable)?;
    Ok(merged.to_json())
}

/// The tool names this bundle serves -- the closed set safe to put in a log line.
pub const TOOL_NAMES: [&str; 14] = [
    "get_score",
    "get_many",
    "adjust",
    "apply_moderation",
    "override",
    "leaderboard",
    "rep",
    "get_tenant_score",
    "history",
    "check",
    "at_risk",
    "is_soft_banned",
    "get_policy",
    "set_policy",
];

/// The log label for a caller-supplied tool `name`: the name itself when it is
/// one of [`TOOL_NAMES`], otherwise the constant `unknown_tool`. Bundle logs
/// must be PII-free, so an unrecognized name is never logged verbatim.
pub fn tool_log_label(name: &str) -> &'static str {
    TOOL_NAMES
        .iter()
        .copied()
        .find(|known| *known == name)
        .unwrap_or("unknown_tool")
}

/// Builds the tool table: each tool name routes to its handler over `env`.
pub fn build_registry(env: Env) -> ToolRegistry {
    let mut r = ToolRegistry::new();
    type Tool = fn(&Env, &ToolContext, Value) -> Result<Value, ToolError>;
    let tools: [(&str, Tool); TOOL_NAMES.len()] = [
        ("get_score", get_score),
        ("get_many", get_many),
        ("adjust", adjust),
        ("apply_moderation", apply_moderation),
        ("override", override_tool),
        ("leaderboard", leaderboard),
        ("rep", rep),
        ("get_tenant_score", get_tenant_score),
        ("history", history),
        ("check", check),
        ("at_risk", at_risk),
        ("is_soft_banned", is_soft_banned),
        ("get_policy", get_policy),
        ("set_policy", set_policy),
    ];
    let env = Arc::new(env);
    for (name, f) in tools {
        let e = Arc::clone(&env);
        r.register(name, move |ctx, args| f(e.as_ref(), ctx, args));
    }
    r
}

#[cfg(test)]
mod tests;

//! Host-independent accrual logic: event classification, delta math,
//! per-user cooldowns, config parsing and the service tool calls.
//!
//! Everything touching the outside world goes through [`Host`] so the same
//! code runs under test against an in-memory host and in the component
//! against the real WIT imports (`lib.rs`).
//!
//! Intentional deviations and non-goals:
//! * Activity deltas are clamped to the host's +/-5 per-call cap, so legacy
//!   weights above 5 (sub tier2 +10, tier3 +20) become +5 here.
//! * `decay` is a documented NON-GOAL: legacy v2 had no score decay.
//! * No tier-change notifications: legacy only REPORTED the tier, so none is
//!   emitted here. The only notice is the low-score WARN (legacy
//!   policy_enforcer), at most once per user per 24h.
//! * Soft/hard/tenant floors and bans are NOT implemented here: the
//!   reputation service enforces them after each adjust. This bundle only
//!   passes the event's `platform` so the service can ban.
//!
//! Subject user: non-moderation events use the host `identity::resolve_actor`
//! (the verified actor's community UUID; payload ids are tokenized upstream).
//! Moderation events target the MODERATED user, read from the payload keys
//! `target_user_id` / `target_user` / `moderated_user_id` when they hold a
//! UUID; platform-inbound moderation delivery is a known upstream gap.

use std::time::Duration;

use serde::Deserialize;
use serde_json::{Value, json};

/// App id of the reputation service bundle whose tools are called.
pub const TARGET: &str = "waddles.core.service.reputation";
/// Gate flag (default OFF) enabling accrual.
pub const FLAG_ENABLE: &str = "waddles.reputation-accrual";
/// Opt-out kill switch: ON means do nothing.
pub const FLAG_KILL: &str = "waddles.disable-bundle-reputation";
/// This bundle's own kv key holding per-community JSON config.
pub const CONFIG_KEY: &str = "accrual";
/// Outcome counter name (host maps onto an OTel counter).
pub const METRIC_EVENTS: &str = "reputation_accrual_events";
/// Duration histogram name in milliseconds.
pub const METRIC_DURATION: &str = "reputation_accrual_duration";

const ACTIVITY_CAP: i64 = 5;
const MODERATION_CAP: i64 = 200;
const NOTICE_TTL_S: u32 = 86_400;
const MAX_COOLDOWN_S: u32 = 604_800;
const SCALE_MIN: i64 = 300;
const SCALE_MAX: i64 = 850;

/// Mirrors the WIT `tool.tool-error` variant.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolErr {
    NotGranted,
    Forbidden,
    Timeout,
    DepthExceeded,
    PoolExhausted,
    TargetUnavailable,
    InvalidArgs(String),
}

/// Log levels the host accepts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Error,
    Warn,
    Info,
    Debug,
}

/// Host capabilities used by the accrual logic.
pub trait Host {
    /// Raw kv read.
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Atomic kv increment; the first claim of a TTL window returns 1.
    fn kv_increment(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, String>;
    /// Flag lookup; unknown flags are OFF.
    fn flag(&self, key: &str) -> bool;
    /// The verified actor's community user UUID, when linked.
    fn resolve_actor(&self) -> Option<String>;
    /// Resolves a payload user token through the HOST-verified mention bindings
    /// of this event (`identity::resolve-mention`); an unbound token is `None`.
    fn resolve_mention(&self, token: &str) -> Option<String>;
    /// Monotonic nanoseconds for duration measurement.
    fn monotonic_nanos(&self) -> u64;
    /// Gated bundle-to-bundle tool call (JSON in, JSON out).
    fn invoke(
        &self,
        target: &str,
        tool: &str,
        args: &Value,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ToolErr>;
    /// PII-free structured log line.
    fn log(&self, level: Level, message: &str, fields: Value);
    /// Records the outcome counter and duration histogram.
    fn metric(&self, outcome: &str, reason_class: &str, elapsed_ms: f64);
}

/// One inbound event as seen by the process stage.
pub struct Input<'a> {
    pub platform: &'a str,
    pub event_type: &'a str,
    pub payload_json: &'a str,
    pub occurred_at: &'a str,
    /// Stable per-delivery id from the stage context.
    pub message_id: &'a str,
}

/// Result label pair reported to metrics and logs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    pub outcome: &'static str,
    pub reason_class: &'static str,
}

/// Per-event-class weights; legacy v2 defaults.
#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Weights {
    pub chat_message: i32,
    pub command_usage: i32,
    pub follow: i32,
    pub subscription: i32,
    pub tier2: i32,
    pub tier3: i32,
    pub gift_subscription: i32,
    pub cheer_per_100_bits: i32,
    pub raid: i32,
    pub donation_per_dollar: i32,
    pub boost: i32,
    pub giveaway_entry: i32,
    pub warn: i32,
    pub timeout: i32,
    pub kick: i32,
    pub ban: i32,
}

impl Default for Weights {
    /// Legacy v2 default weights.
    fn default() -> Self {
        Self {
            chat_message: 1,
            command_usage: 1,
            follow: 1,
            subscription: 5,
            tier2: 10,
            tier3: 20,
            gift_subscription: 3,
            cheer_per_100_bits: 1,
            raid: 2,
            donation_per_dollar: 1,
            boost: 5,
            giveaway_entry: -1,
            warn: -25,
            timeout: -50,
            kick: -75,
            ban: -200,
        }
    }
}

/// Per-user cooldown seconds (0 disables).
#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Cooldowns {
    pub chat_message: u32,
    pub command_usage: u32,
}

impl Default for Cooldowns {
    /// Legacy cooldowns: 60s chat, 10s commands.
    fn default() -> Self {
        Self {
            chat_message: 60,
            command_usage: 10,
        }
    }
}

/// Per-community accrual config stored under kv key `accrual`.
#[derive(Debug, Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Accrual {
    pub weights: Weights,
    pub cooldowns: Cooldowns,
    pub low_score_notice_at: i64,
    pub low_score_hard_floor: i64,
}

impl Default for Accrual {
    /// Legacy at-risk line 450 above the service's default hard floor 400.
    fn default() -> Self {
        Self {
            weights: Weights::default(),
            cooldowns: Cooldowns::default(),
            low_score_notice_at: 450,
            low_score_hard_floor: 400,
        }
    }
}

impl Accrual {
    /// Rejects out-of-range values so a bad config is loud, never defaulted.
    fn validate(&self) -> Result<(), String> {
        let w = &self.weights;
        let all = [
            w.chat_message,
            w.command_usage,
            w.follow,
            w.subscription,
            w.tier2,
            w.tier3,
            w.gift_subscription,
            w.cheer_per_100_bits,
            w.raid,
            w.donation_per_dollar,
            w.boost,
            w.giveaway_entry,
            w.warn,
            w.timeout,
            w.kick,
            w.ban,
        ];
        if all.iter().any(|v| v.unsigned_abs() > 1000) {
            return Err("weight out of range".into());
        }
        // Moderation weights are penalties: a positive one would turn the larger
        // moderation cap into a score-raising channel.
        if [w.warn, w.timeout, w.kick, w.ban].iter().any(|v| *v >= 0) {
            return Err("moderation weights must be negative".into());
        }
        let c = &self.cooldowns;
        if c.chat_message > MAX_COOLDOWN_S || c.command_usage > MAX_COOLDOWN_S {
            return Err("cooldown out of range".into());
        }
        let (n, h) = (self.low_score_notice_at, self.low_score_hard_floor);
        if !(SCALE_MIN..=SCALE_MAX).contains(&n) || !(SCALE_MIN..=SCALE_MAX).contains(&h) || h >= n
        {
            return Err("low-score thresholds out of range".into());
        }
        Ok(())
    }
}

/// Loads the community config: absent = defaults; present-but-invalid = Err.
fn load_config<H: Host>(host: &H) -> Result<Accrual, String> {
    match host.kv_get(CONFIG_KEY)? {
        None => Ok(Accrual::default()),
        Some(bytes) => {
            let cfg: Accrual = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
            cfg.validate()?;
            Ok(cfg)
        }
    }
}

/// Event class derived from the event type and payload.
#[derive(Debug, Clone, Copy, PartialEq)]
enum Class {
    Chat,
    Command,
    Follow,
    Subscription(u8),
    Gift(f64),
    Cheer(f64),
    Raid,
    Donation(f64),
    Boost,
    Giveaway,
    Moderation(Mod),
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum Mod {
    Warn,
    Timeout,
    Kick,
    Ban,
}

impl Class {
    /// Metric reason class label.
    fn label(self) -> &'static str {
        match self {
            Class::Chat => "chat",
            Class::Command => "command",
            Class::Follow => "follow",
            Class::Subscription(_) => "subscription",
            Class::Gift(_) => "gift",
            Class::Cheer(_) => "cheer",
            Class::Raid => "raid",
            Class::Donation(_) => "donation",
            Class::Boost => "boost",
            Class::Giveaway => "giveaway",
            Class::Moderation(_) => "moderation",
        }
    }

    /// Ledger reason string (`[a-z0-9._:-]`).
    fn reason(self) -> &'static str {
        match self {
            Class::Chat => "chat.message",
            Class::Command => "command.usage",
            Class::Follow => "follow",
            Class::Subscription(2) => "subscription.tier2",
            Class::Subscription(3) => "subscription.tier3",
            Class::Subscription(_) => "subscription",
            Class::Gift(_) => "gift.subscription",
            Class::Cheer(_) => "cheer",
            Class::Raid => "raid",
            Class::Donation(_) => "donation",
            Class::Boost => "boost",
            Class::Giveaway => "giveaway.entry",
            Class::Moderation(Mod::Warn) => "moderation.warn",
            Class::Moderation(Mod::Timeout) => "moderation.timeout",
            Class::Moderation(Mod::Kick) => "moderation.kick",
            Class::Moderation(Mod::Ban) => "moderation.ban",
        }
    }

    /// Configured weight and the multiplier applied to it.
    fn weight_and_mult(self, w: &Weights) -> (i32, f64) {
        match self {
            Class::Chat => (w.chat_message, 1.0),
            Class::Command => (w.command_usage, 1.0),
            Class::Follow => (w.follow, 1.0),
            Class::Subscription(2) => (w.tier2, 1.0),
            Class::Subscription(3) => (w.tier3, 1.0),
            Class::Subscription(_) => (w.subscription, 1.0),
            Class::Gift(n) => (w.gift_subscription, n),
            Class::Cheer(m) => (w.cheer_per_100_bits, m),
            Class::Raid => (w.raid, 1.0),
            Class::Donation(a) => (w.donation_per_dollar, a),
            Class::Boost => (w.boost, 1.0),
            Class::Giveaway => (w.giveaway_entry, 1.0),
            Class::Moderation(Mod::Warn) => (w.warn, 1.0),
            Class::Moderation(Mod::Timeout) => (w.timeout, 1.0),
            Class::Moderation(Mod::Kick) => (w.kick, 1.0),
            Class::Moderation(Mod::Ban) => (w.ban, 1.0),
        }
    }

    /// Cooldown slot name and configured seconds, for cooled-down classes.
    fn cooldown(self, c: &Cooldowns) -> Option<(&'static str, u32)> {
        match self {
            Class::Chat => Some(("chat", c.chat_message)),
            Class::Command => Some(("cmd", c.command_usage)),
            _ => None,
        }
    }
}

/// Reads a JSON number or numeric string as a finite f64.
fn num(v: &Value) -> Option<f64> {
    let f = match v {
        Value::Number(n) => n.as_f64()?,
        Value::String(s) => s.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    f.is_finite().then_some(f)
}

/// Looks a field up in `metadata` first, then at the payload top level.
fn field<'a>(p: &'a Value, key: &str) -> Option<&'a Value> {
    p.get("metadata")
        .and_then(|m| m.get(key))
        .or_else(|| p.get(key))
}

/// Subscription tier from `metadata.tier` ("2000"/2 -> 2, "3000"/3 -> 3).
fn sub_tier(p: &Value) -> u8 {
    match field(p, "tier").and_then(num) {
        Some(t) if t == 2.0 || t == 2000.0 => 2,
        Some(t) if t == 3.0 || t == 3000.0 => 3,
        _ => 1,
    }
}

/// Maps an event to its accrual class; `None` means not an accrual event.
fn classify(event_type: &str, p: &Value) -> Option<Class> {
    Some(match event_type {
        "chat.message" | "message" => {
            let text = p.get("text")?.as_str()?;
            if text.starts_with('!') {
                Class::Command
            } else {
                Class::Chat
            }
        }
        "channel.follow" | "channel.followed" => Class::Follow,
        "channel.subscribe" => Class::Subscription(sub_tier(p)),
        "channel.subscription.gift" | "channel.subscription.gifts" => {
            let total = field(p, "total").and_then(num).unwrap_or(1.0);
            Class::Gift(total.max(1.0))
        }
        "channel.cheer" => Class::Cheer(field(p, "bits").and_then(num)? / 100.0),
        "channel.raid" => Class::Raid,
        "donation" | "tip" | "channel.donation" | "channel.tip" => {
            Class::Donation(field(p, "amount").and_then(num)?)
        }
        "boost" | "server_boost" | "guild.boost" | "channel.boost" => Class::Boost,
        "giveaway.entry" | "raffle.entry" | "giveaway_entry" | "raffle_entry" => Class::Giveaway,
        "moderation.warn" => Class::Moderation(Mod::Warn),
        "moderation.timeout" => Class::Moderation(Mod::Timeout),
        "moderation.kick" => Class::Moderation(Mod::Kick),
        "moderation.ban" => Class::Moderation(Mod::Ban),
        _ => return None,
    })
}

/// Rounds half away from zero exactly once and clamps to the class cap.
fn compute_delta(class: Class, w: &Weights) -> i64 {
    let (weight, mult) = class.weight_and_mult(w);
    let mut raw = (f64::from(weight) * mult).round();
    if !raw.is_finite() {
        return 0;
    }
    // Moderation deltas are ALWAYS penalties, whatever the configured sign.
    if matches!(class, Class::Moderation(_)) {
        raw = -raw.abs();
    }
    let cap = if matches!(class, Class::Moderation(_)) {
        MODERATION_CAP
    } else {
        ACTIVITY_CAP
    };
    (raw.clamp(-(cap as f64), cap as f64)) as i64
}

/// True when `s` is a canonical 8-4-4-4-12 hex UUID.
fn is_uuid(s: &str) -> bool {
    let b = s.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        })
}

/// The payload token naming the moderated user. It is NEVER trusted as a user
/// id: it must resolve through the host-verified mention bindings.
fn moderation_token(p: &Value) -> Option<&str> {
    ["target_user_id", "target_user", "moderated_user_id"]
        .iter()
        .filter_map(|k| p.get(*k).and_then(Value::as_str))
        .find(|s| !s.is_empty() && s.len() <= 128)
}

/// Moderation outcomes the host treats as system-originated (the same
/// allowlist as `svc_process::identity::is_system_event_type`); warn/kick have
/// no verified moderation source today, so they are skipped, never applied.
fn moderation_verified(m: Mod) -> bool {
    matches!(m, Mod::Ban | Mod::Timeout)
}

/// FNV-1a 64-bit hash for the fallback idempotency key.
fn fnv1a(parts: &[&str]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for part in parts {
        for b in part.bytes().chain(std::iter::once(0u8)) {
            h ^= u64::from(b);
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
    }
    h
}

/// Deterministic per-event idempotency key: the stage message id plus a fixed
/// suffix, or a stable hash of platform/type/subject/time when no id exists.
pub fn idempotency_key(input: &Input, subject: &str) -> String {
    let id = input.message_id;
    let ok = !id.is_empty()
        && id.len() <= 100
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b':' | b'.' | b'-'));
    if ok {
        format!("{id}:rep")
    } else {
        let h = fnv1a(&[input.platform, input.event_type, subject, input.occurred_at]);
        format!("d.{h:016x}:rep")
    }
}

/// True when a tool error message means an expected host cap/limit.
fn is_cap_message(m: &str) -> bool {
    ["daily_cap_exceeded", "quota_exceeded", "rate_limited"]
        .iter()
        .any(|c| m.contains(c))
}

/// Short stable label for a tool error (never the message text).
fn err_label(e: &ToolErr) -> &'static str {
    match e {
        ToolErr::NotGranted => "not_granted",
        ToolErr::Forbidden => "forbidden",
        ToolErr::Timeout => "timeout",
        ToolErr::DepthExceeded => "depth_exceeded",
        ToolErr::PoolExhausted => "pool_exhausted",
        ToolErr::TargetUnavailable => "target_unavailable",
        ToolErr::InvalidArgs(_) => "invalid_args",
    }
}

/// Claims a once-per-window kv slot; true when this caller won it.
fn claim<H: Host>(host: &H, key: &str, ttl_s: u32) -> Result<bool, String> {
    Ok(host.kv_increment(key, 1, ttl_s)? == 1)
}

/// Processes one event end to end and emits metrics plus a PII-free debug log.
pub fn process<H: Host>(host: &H, input: &Input) -> Outcome {
    let start = host.monotonic_nanos();
    let out = run(host, input);
    let elapsed = Duration::from_nanos(host.monotonic_nanos().saturating_sub(start));
    let ms = elapsed.as_secs_f64() * 1000.0;
    host.metric(out.outcome, out.reason_class, ms);
    host.log(
        Level::Debug,
        "reputation accrual event",
        json!({"outcome": out.outcome, "reason_class": out.reason_class, "elapsed_ms": ms}),
    );
    out
}

/// Builds an [`Outcome`] from labels.
fn done(outcome: &'static str, reason_class: &'static str) -> Outcome {
    Outcome {
        outcome,
        reason_class,
    }
}

/// The gated pipeline: flags, classify, config, subject, cooldown, tool call.
fn run<H: Host>(host: &H, input: &Input) -> Outcome {
    if host.flag(FLAG_KILL) {
        return done("kill_switch", "none");
    }
    if !host.flag(FLAG_ENABLE) {
        return done("flag_off", "none");
    }
    let payload: Value = match serde_json::from_str(input.payload_json) {
        Ok(v @ Value::Object(_)) => v,
        _ => return done("ignored", "unknown"),
    };
    let Some(class) = classify(input.event_type, &payload) else {
        return done("ignored", "unknown");
    };
    let label = class.label();
    let cfg = match load_config(host) {
        Ok(c) => c,
        Err(_) => {
            host.log(
                Level::Error,
                "reputation accrual config invalid or unreadable",
                json!({"outcome": "config_invalid"}),
            );
            return done("config_invalid", label);
        }
    };
    let delta = compute_delta(class, &cfg.weights);
    if delta == 0 {
        return done("zero_delta", label);
    }
    let moderation = matches!(class, Class::Moderation(_));
    if let Class::Moderation(m) = class
        && !moderation_verified(m)
    {
        return done("unverified_moderation", label);
    }
    let subject = if moderation {
        moderation_token(&payload)
            .and_then(|t| host.resolve_mention(t))
            .filter(|u| is_uuid(u))
            .map(|u| u.to_ascii_lowercase())
    } else {
        host.resolve_actor().filter(|u| is_uuid(u))
    };
    let Some(user) = subject else {
        return done("no_user", label);
    };
    if let Some((slot, secs)) = class.cooldown(&cfg.cooldowns).filter(|(_, s)| *s > 0) {
        match claim(host, &format!("cd.{slot}.{user}"), secs) {
            Ok(true) => {}
            Ok(false) => return done("cooldown", label),
            Err(_) => {
                host.log(
                    Level::Error,
                    "reputation accrual cooldown store failed",
                    json!({"outcome": "error"}),
                );
                return done("error", label);
            }
        }
    }
    let mut args = json!({"user": user, "delta": delta, "reason": class.reason()});
    if matches!(input.platform, "twitch" | "discord" | "youtube") {
        args["platform"] = json!(input.platform);
    }
    let tool = if moderation {
        "apply_moderation"
    } else {
        "adjust"
    };
    let key = idempotency_key(input, &user);
    match host.invoke(TARGET, tool, &args, Some(&key)) {
        Ok(v) => {
            if v.get("applied").and_then(Value::as_bool) == Some(false) {
                return done("capped", label);
            }
            if let Some(score) = v.get("score").and_then(Value::as_i64) {
                low_score_notice(host, &cfg, &user, score);
            }
            done("applied", label)
        }
        Err(ToolErr::InvalidArgs(m)) if is_cap_message(&m) => done("capped", label),
        Err(e) => {
            host.log(
                Level::Error,
                "reputation accrual tool call failed",
                json!({"outcome": "error", "error": err_label(&e)}),
            );
            done("error", label)
        }
    }
}

/// Emits at most one low-score WARN per user per 24h while the score sits at
/// or below the notice line and above the hard floor.
fn low_score_notice<H: Host>(host: &H, cfg: &Accrual, user: &str, score: i64) {
    if score > cfg.low_score_notice_at || score <= cfg.low_score_hard_floor {
        return;
    }
    if matches!(claim(host, &format!("ln.{user}"), NOTICE_TTL_S), Ok(true)) {
        host.log(
            Level::Warn,
            "reputation low-score notice",
            json!({"outcome": "low_score_notice"}),
        );
    }
}

#[cfg(test)]
mod tests;

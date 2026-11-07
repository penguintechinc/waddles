//! Event dispatch: turns one inbound platform event into at most one reply. Everything here is
//! process-stage work (identity, tools, kv); the action stage only relays the finished reply.

use serde_json::{Map, Value};
use waddle_sdk::PlatformEvent;

use crate::command::{self, Parsed, ProducerCmd, TeammateCmd};
use crate::config::{self, Config, TEAMMATES};
use crate::host::{Ctx, Host, HostError, Level};
use crate::producer::{self, Tick};
use crate::receptionist;
use crate::researcher::{self, AskPolicy};

/// A finished reply to relay: where and what (already sanitized).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reply {
    pub platform: String,
    pub channel_id: String,
    pub text: String,
}

const OVERRIDES_KEY: &str = "overrides";
/// Fixed limits for the Receptionist's onboarding Q&A (KB-only).
const ONBOARD_COOLDOWN_S: u32 = 30;
const ONBOARD_PER_MIN: u32 = 10;

fn log(h: &impl Host, level: Level, op: &str, extra: &str) {
    h.log(
        level,
        "ai-teammates",
        &serde_json::json!({"op": op, "detail": extra}).to_string(),
    );
}

/// Loads the moderator runtime overrides (teammate -> object).
fn load_overrides<H: Host>(h: &H) -> Result<Map<String, Value>, HostError> {
    match h.kv_get(OVERRIDES_KEY)? {
        None => Ok(Map::new()),
        Some(b) if b.is_empty() => Ok(Map::new()),
        Some(b) => match serde_json::from_slice::<Value>(&b) {
            Ok(Value::Object(m)) => Ok(m),
            _ => Err(HostError::Backend("overrides corrupt".into())),
        },
    }
}

fn save_overrides<H: Host>(h: &H, o: &Map<String, Value>) -> Result<(), HostError> {
    let bytes = serde_json::to_vec(o).map_err(|e| HostError::Backend(e.to_string()))?;
    h.kv_set(OVERRIDES_KEY, &bytes, 0)
}

/// Effective config (hub config overlaid with overrides).
pub fn load_config<H: Host>(h: &H, ctx: &Ctx) -> Result<Config, HostError> {
    Ok(config::effective(&ctx.config_json, &load_overrides(h)?))
}

/// Strict-boolean moderator/broadcaster signal; absent or non-boolean badges DENY.
pub fn is_moderator(payload: &Value) -> bool {
    payload.get("is_mod") == Some(&Value::Bool(true))
        || payload.get("is_broadcaster") == Some(&Value::Bool(true))
}

fn state_word(c: &Result<bool, config::ConfigError>) -> String {
    match c {
        Ok(true) => "on".into(),
        Ok(false) => "off".into(),
        Err(e) => format!("off (config invalid: {e})"),
    }
}

fn enabled_of(cfg: &Config, t: &str) -> Result<bool, config::ConfigError> {
    match t {
        "researcher" => cfg
            .researcher
            .as_ref()
            .map(|c| c.enabled)
            .map_err(Clone::clone),
        "receptionist" => cfg
            .receptionist
            .as_ref()
            .map(|c| c.enabled)
            .map_err(Clone::clone),
        _ => cfg
            .producer
            .as_ref()
            .map(|c| c.enabled)
            .map_err(Clone::clone),
    }
}

fn section_err(cfg: &Config, t: &str) -> Option<config::ConfigError> {
    match t {
        "researcher" => cfg.researcher.as_ref().err().cloned(),
        "receptionist" => cfg.receptionist.as_ref().err().cloned(),
        _ => cfg.producer.as_ref().err().cloned(),
    }
}

/// Applies an override mutation; persists only when the resulting config is valid.
fn mutate<H: Host>(
    h: &H,
    ctx: &Ctx,
    teammate: &str,
    f: impl FnOnce(&mut Map<String, Value>),
) -> String {
    let mut all = match load_overrides(h) {
        Ok(o) => o,
        Err(_) => return "Settings are unavailable right now, please try again later.".into(),
    };
    let mut mine = all
        .get(teammate)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    f(&mut mine);
    if mine.is_empty() {
        all.remove(teammate);
    } else {
        all.insert(teammate.to_string(), Value::Object(mine));
    }
    let candidate = config::effective(&ctx.config_json, &all);
    if let Some(e) = section_err(&candidate, teammate) {
        return format!("Not saved: {e}");
    }
    match save_overrides(h, &all) {
        Ok(()) => "Saved.".into(),
        Err(_) => "Settings are unavailable right now, please try again later.".into(),
    }
}

/// Handles `!teammate`. Listing/status is open to everyone; every change needs a moderator.
fn teammate_cmd<H: Host>(
    h: &H,
    ctx: &Ctx,
    cmd: &Result<TeammateCmd, String>,
    moderator: bool,
) -> String {
    let cmd = match cmd {
        Ok(c) => c,
        Err(usage) => return usage.clone(),
    };
    let cfg = match load_config(h, ctx) {
        Ok(c) => c,
        Err(_) => return "Settings are unavailable right now, please try again later.".into(),
    };
    let describe = |t: &str| format!("{t}: {}", state_word(&enabled_of(&cfg, t)));
    match cmd {
        TeammateCmd::List => TEAMMATES
            .iter()
            .map(|t| describe(t))
            .collect::<Vec<_>>()
            .join(" | "),
        TeammateCmd::Status(t) => describe(t),
        _ if !moderator => "Only moderators can change teammates.".into(),
        TeammateCmd::Enable(t) => mutate(h, ctx, t, |m| {
            m.insert("enabled".into(), Value::Bool(true));
        }),
        TeammateCmd::Disable(t) => mutate(h, ctx, t, |m| {
            m.insert("enabled".into(), Value::Bool(false));
        }),
        TeammateCmd::Reset(t) => mutate(h, ctx, t, |m| m.clear()),
        TeammateCmd::Set {
            teammate,
            key,
            value,
        } => {
            let Some(kind) = config::settable(teammate, key) else {
                return format!("{teammate} has no chat-settable key '{key}'.");
            };
            match config::parse_value(kind, value) {
                Ok(v) => mutate(h, ctx, teammate, |m| {
                    m.insert(key.clone(), v);
                }),
                Err(e) => format!("Bad value for {key}: {e}."),
            }
        }
    }
}

fn producer_cmd<H: Host>(h: &H, cmd: &Result<ProducerCmd, String>, moderator: bool) -> String {
    let cmd = match cmd {
        Ok(c) => *c,
        Err(u) => return u.clone(),
    };
    let unavailable = || "Settings are unavailable right now, please try again later.".to_string();
    match cmd {
        ProducerCmd::Status => match producer::is_paused(h) {
            Ok(p) => format!("Producer is {}.", if p { "paused" } else { "not paused" }),
            Err(_) => unavailable(),
        },
        _ if !moderator => "Only moderators can pause or resume the Producer.".into(),
        ProducerCmd::Pause => producer::set_paused(h, true)
            .map_or_else(|_| unavailable(), |()| "Producer paused.".into()),
        ProducerCmd::Resume => producer::set_paused(h, false)
            .map_or_else(|_| unavailable(), |()| "Producer resumed.".into()),
    }
}

fn identity_text(_: &HostError) -> String {
    "I couldn't identify you - make sure your account is linked to this community.".into()
}

/// Handles one recognised command; returns the reply text.
fn run_command<H: Host>(h: &H, ctx: &Ctx, parsed: Parsed, payload: &Value) -> String {
    let moderator = is_moderator(payload);
    match parsed {
        Parsed::Teammate(c) => teammate_cmd(h, ctx, &c, moderator),
        Parsed::Producer(c) => {
            let cfg = load_config(h, ctx);
            match cfg {
                Ok(cfg) if cfg.producer.is_ok() => producer_cmd(h, &c, moderator),
                Ok(_) => "The Producer's configuration is invalid; see !teammate producer.".into(),
                Err(_) => "Settings are unavailable right now, please try again later.".into(),
            }
        }
        Parsed::Research(q) => {
            let cfg = match load_config(h, ctx) {
                Ok(c) => c,
                Err(_) => {
                    return "Settings are unavailable right now, please try again later.".into();
                }
            };
            let cfg = match cfg.researcher {
                Ok(c) if c.enabled => c,
                _ => return "The Researcher teammate is not enabled in this community.".into(),
            };
            let user = match h.resolve_actor() {
                Ok(u) => u,
                Err(e) => return identity_text(&e),
            };
            researcher::answer(
                h,
                &AskPolicy {
                    ns: "researcher",
                    user_cooldown_s: cfg.user_cooldown_s,
                    community_per_min: cfg.community_per_min,
                    max_chars: cfg.max_answer_chars,
                    collection: cfg.collection.as_deref(),
                    allow_general: cfg.allow_general,
                },
                &user,
                &q,
            )
        }
        Parsed::Onboard(q) => {
            let cfg = match load_config(h, ctx) {
                Ok(c) => c,
                Err(_) => {
                    return "Settings are unavailable right now, please try again later.".into();
                }
            };
            let cfg = match cfg.receptionist {
                Ok(c) if c.enabled => c,
                _ => return "The Receptionist teammate is not enabled in this community.".into(),
            };
            let user = match h.resolve_actor() {
                Ok(u) => u,
                Err(e) => return identity_text(&e),
            };
            researcher::answer(
                h,
                &AskPolicy {
                    ns: "onboard",
                    user_cooldown_s: ONBOARD_COOLDOWN_S,
                    community_per_min: ONBOARD_PER_MIN,
                    max_chars: cfg.max_greeting_chars,
                    collection: Some(&cfg.onboarding_collection),
                    allow_general: false,
                },
                &user,
                &q,
            )
        }
    }
}

/// Maps one event to at most one reply. Errors never escape: each is logged (closed
/// vocabulary, PII-free) and, for a user-issued command, answered with an explicit message.
pub fn handle_event<H: Host>(h: &H, ctx: &Ctx, ev: &PlatformEvent) -> Option<Reply> {
    match ev.event_type.as_str() {
        "timer.tick" => run_tick(h, ctx),
        "member.join" => {
            let payload: Value = ev.payload().ok()?;
            let cfg = load_config(h, ctx)
                .ok()?
                .receptionist
                .ok()
                .filter(|c| c.enabled && c.greet_on_join)?;
            let origin = payload.get("channel_id").and_then(Value::as_str);
            receptionist::greet(h, &cfg, &ev.platform, origin).ok()
        }
        "chat.message" => {
            let payload: Value = ev.payload().ok()?;
            let text = payload.get("text")?.as_str()?;
            let channel = payload
                .get("channel_id")
                .and_then(Value::as_str)
                .filter(|c| !c.is_empty());
            if let Some(parsed) = command::parse(text) {
                let Some(channel) = channel else {
                    log(h, Level::Warn, "command_without_channel", "dropped");
                    return None;
                };
                let reply = run_command(h, ctx, parsed, &payload);
                return crate::guard::sanitize_command_reply(&reply).map(|text| Reply {
                    platform: ev.platform.clone(),
                    channel_id: channel.to_string(),
                    text,
                });
            }
            passive(h, ctx, ev, text, channel)
        }
        _ => None,
    }
}

/// Ordinary chat line: record Producer activity, and run the first-message welcome.
fn passive<H: Host>(
    h: &H,
    ctx: &Ctx,
    ev: &PlatformEvent,
    text: &str,
    channel: Option<&str>,
) -> Option<Reply> {
    let cfg = load_config(h, ctx).ok()?;
    if let (Ok(p), Some(ch)) = (&cfg.producer, channel) {
        producer::note_activity(h, p, &ev.platform, ch);
    }
    let r = cfg
        .receptionist
        .ok()
        .filter(|c| c.enabled && c.greet_on_first_message)?;
    if text.trim_start().starts_with(['!', '/', '.']) {
        return None;
    }
    receptionist::greet(h, &r, &ev.platform, channel).ok()
}

/// Producer tick entry (timer event or stream-lifecycle hook).
pub fn run_tick<H: Host>(h: &H, ctx: &Ctx) -> Option<Reply> {
    let cfg = load_config(h, ctx).ok()?.producer.ok()?;
    match producer::tick(h, &cfg) {
        Tick::Posted(r) => Some(r),
        Tick::Skipped(why) => {
            log(h, Level::Debug, "producer_skip", why);
            None
        }
    }
}

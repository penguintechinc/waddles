//! Twitch clips policy: command parsing, gates, cooldowns, the create/poll/announce flow and the
//! bundle-admin actions. Everything host-facing goes through the [`Host`] trait so the exact same
//! code runs natively under test and inside the WASM component (see `lib.rs`). No Twitch
//! credential or API detail lives here: clips are made only through the Twitch service bundle's
//! `twitch.clip.*` tools, whose broadcaster is pinned host-side.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

/// App id of the Twitch platform service bundle that owns the clip tools.
pub const TWITCH_SERVICE: &str = "waddles.core.service.twitch";
/// Tool: Helix `POST /clips` (broadcaster pinned host-side).
pub const TOOL_CREATE: &str = "twitch.clip.create";
/// Tool: Helix `GET /clips?id=` (empty Helix data => `ready:false`).
pub const TOOL_GET: &str = "twitch.clip.get";
/// Tool: Helix `GET /clips?broadcaster_id=` (broadcaster pinned host-side).
pub const TOOL_LIST: &str = "twitch.clip.list";

/// Total age after which an unfinished clip is reported as timed out (milliseconds).
pub const PROCESSING_TIMEOUT_MS: u64 = 15_000;
/// Number of self-expiring pending-clip slots.
pub const PENDING_SLOTS: u64 = 8;
/// Pending slots self-expire (seconds) so a lost sweep can never leak state.
const PENDING_TTL_S: u32 = 120;
/// Size of the recent-clip index ring.
pub const RING_SLOTS: u64 = 25;
/// Redelivery dedupe window (seconds).
const DEDUPE_TTL_S: u32 = 600;
/// In-flight lock lifetime (seconds); must exceed the invocation deadline.
const LOCK_TTL_S: u32 = 30;
/// Pending clips resolved opportunistically per chat command.
const SWEEP_CAP_CHAT: usize = 2;
/// Hard chat message length cap (Twitch allows 500).
const MAX_CHAT_CHARS: usize = 450;
const MAX_TITLE_CHARS: usize = 100;
const POLL_DELAYS_MS: [u64; 5] = [1000, 1500, 2000, 2500, 3000];

/// A relayed chat message; wire shape `{channel,text}` goes to `provider`'s relay queue.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OutMsg {
    pub provider: String,
    pub channel: String,
    pub text: String,
}

/// What a process-stage invocation produced.
#[derive(Debug, Clone, PartialEq)]
pub enum Output {
    /// Event is not for this bundle, or it is a redelivery.
    None,
    /// Chat messages for the action stage to relay.
    Messages(Vec<OutMsg>),
    /// Result of a bundle-admin action, handed back to the gateway.
    Admin(Value),
}

/// A loud, typed failure surfaced to the stage (never swallowed).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogicError {
    pub code: &'static str,
    pub message: String,
}

impl LogicError {
    fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

/// Failure of a host capability (kv, clock); detail is never user-visible.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostError(pub String);

/// Why the triggering actor could not be resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IdentityError {
    NotLinked,
    Other(String),
}

/// Every host capability the bundle uses. WASM impl is in `lib.rs`; tests use an in-memory fake.
pub trait Host {
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, HostError>;
    fn kv_set(&self, key: &str, value: &[u8], ttl_s: u32) -> Result<(), HostError>;
    fn kv_delete(&self, key: &str) -> Result<(), HostError>;
    fn kv_increment(&self, key: &str, delta: i64, ttl_s: u32) -> Result<i64, HostError>;
    fn now_millis(&self) -> u64;
    fn sleep_ms(&self, ms: u64);
    /// Community user-UUID of the actor whose message triggered this invocation.
    fn resolve_actor(&self) -> Result<String, IdentityError>;
    /// Calls a Twitch-service tool. `args` already has no broadcaster/community fields.
    fn invoke_tool(&self, tool: &str, args: &Value, idem: Option<&str>)
    -> Result<Value, ToolError>;
    /// PII-free structured log line: `level` is `debug|info|warn|error`.
    fn log(&self, level: &str, op: &str, outcome: &str);
}

/// Minimum chat standing for a command.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Everyone,
    Subscribers,
    Vip,
    Mods,
    Broadcaster,
}

/// Per-community configuration (3-tier resolved by the host, delivered as `config_json`).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub who_can_clip: Role,
    pub who_can_list: Role,
    pub user_cooldown_secs: u32,
    pub channel_cooldown_secs: u32,
    pub list_cooldown_secs: u32,
    pub default_title: Option<String>,
    pub announce_template: String,
    pub discord_share_enabled: bool,
    pub discord_share_channel_id: Option<String>,
    pub list_limit: u32,
    pub poll_budget_ms: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            who_can_clip: Role::Mods,
            who_can_list: Role::Everyone,
            user_cooldown_secs: 60,
            channel_cooldown_secs: 30,
            list_cooldown_secs: 10,
            default_title: None,
            announce_template: "Clip ready: {url}".to_string(),
            discord_share_enabled: false,
            discord_share_channel_id: None,
            list_limit: 5,
            poll_budget_ms: 6000,
        }
    }
}

impl Config {
    /// Parses and validates config JSON; returns every violation, never a partial config.
    pub fn parse(json_text: &str) -> Result<Config, Vec<String>> {
        let text = if json_text.trim().is_empty() {
            "{}"
        } else {
            json_text
        };
        let cfg: Config =
            serde_json::from_str(text).map_err(|e| vec![format!("config is not valid: {e}")])?;
        cfg.validate().map(|()| cfg)
    }

    /// Range/format checks shared by runtime load and the admin `config` validator.
    pub fn validate(&self) -> Result<(), Vec<String>> {
        let mut errs = Vec::new();
        for (name, v) in [
            ("user_cooldown_secs", self.user_cooldown_secs),
            ("channel_cooldown_secs", self.channel_cooldown_secs),
            ("list_cooldown_secs", self.list_cooldown_secs),
        ] {
            if v > 3600 {
                errs.push(format!("{name} must be 0..=3600"));
            }
        }
        if !(1..=10).contains(&self.list_limit) {
            errs.push("list_limit must be 1..=10".into());
        }
        if !(1000..=7000).contains(&self.poll_budget_ms) {
            errs.push("poll_budget_ms must be 1000..=7000".into());
        }
        if let Some(t) = &self.default_title
            && (t.chars().count() > MAX_TITLE_CHARS || t.chars().any(char::is_control))
        {
            errs.push("default_title must be <=100 chars without control characters".into());
        }
        if !self.announce_template.contains("{url}") || self.announce_template.chars().count() > 200
        {
            errs.push("announce_template must contain {url} and be <=200 chars".into());
        }
        if self.discord_share_enabled {
            match &self.discord_share_channel_id {
                Some(id)
                    if (15..=25).contains(&id.len()) && id.bytes().all(|b| b.is_ascii_digit()) => {}
                _ => errs.push(
                    "discord_share_channel_id must be a Discord snowflake when sharing is enabled"
                        .into(),
                ),
            }
        }
        if errs.is_empty() { Ok(()) } else { Err(errs) }
    }
}

/// One Twitch clip as returned by `twitch.clip.get` / `.list` (no creator handle: PII).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Clip {
    pub clip_id: String,
    pub url: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub created_at: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Pending {
    provider: String,
    channel: String,
    created_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Item {
    seq: i64,
    clip_id: String,
    url: String,
    title: String,
    created_ms: u64,
}

/// Inbound event as the stage hands it over, with the invocation's context folded in.
#[derive(Debug, Clone)]
pub struct EventIn {
    pub platform: String,
    pub event_type: String,
    pub actor: Option<String>,
    pub payload: Value,
    pub message_id: String,
    pub config_json: String,
}

/// Parsed chat command.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Clip { title: Option<String> },
    Clips { limit: Option<u32> },
    ClipsUsage,
}

/// Recognises `!clip [title]` and `!clips [n]` (case-insensitive command word, exact match).
pub fn parse_command(text: &str) -> Option<Command> {
    let t = text.trim();
    let word_end = t.find(char::is_whitespace).unwrap_or(t.len());
    let (word, rest) = t.split_at(word_end);
    let rest = rest.trim();
    match word.to_ascii_lowercase().as_str() {
        "!clip" => Some(Command::Clip {
            title: if rest.is_empty() {
                None
            } else {
                Some(rest.to_string())
            },
        }),
        "!clips" => Some(if rest.is_empty() {
            Command::Clips { limit: None }
        } else {
            match rest.parse::<u32>() {
                Ok(n) => Command::Clips { limit: Some(n) },
                Err(_) => Command::ClipsUsage,
            }
        }),
        _ => None,
    }
}

/// Cleans a viewer-supplied title: strips control chars, rejects host mention tokens, caps length.
pub fn sanitize_title(raw: &str) -> Result<Option<String>, &'static str> {
    if raw.contains("{user:") {
        return Err("Clip titles can't contain mentions.");
    }
    let cleaned: String = raw.chars().filter(|c| !c.is_control()).collect();
    let cleaned = cleaned.trim();
    if cleaned.is_empty() {
        return Ok(None);
    }
    Ok(Some(cleaned.chars().take(MAX_TITLE_CHARS).collect()))
}

/// Twitch clip ids are `[A-Za-z0-9_-]`; also guarantees kv-key safety (no ':').
pub fn valid_clip_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 100
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn valid_clip_url(url: &str) -> bool {
    [
        "https://clips.twitch.tv/",
        "https://www.twitch.tv/",
        "https://twitch.tv/",
    ]
    .iter()
    .any(|p| url.starts_with(p))
        && !url.chars().any(|c| c.is_control() || c.is_whitespace())
}

fn truncate_chars(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let mut out: String = s.chars().take(n.saturating_sub(1)).collect();
        out.push('…');
        out
    }
}

/// Whether the chat badges in `payload` satisfy `role`.
pub fn role_allows(role: Role, payload: &Value) -> bool {
    let flag = |k: &str| payload.get(k).and_then(Value::as_bool).unwrap_or(false);
    let (b, m, v, s) = (
        flag("is_broadcaster"),
        flag("is_mod"),
        flag("is_vip"),
        flag("is_subscriber"),
    );
    match role {
        Role::Everyone => true,
        Role::Subscribers => s || v || m || b,
        Role::Vip => v || m || b,
        Role::Mods => m || b,
        Role::Broadcaster => b,
    }
}

fn role_name(role: Role) -> &'static str {
    match role {
        Role::Everyone => "everyone",
        Role::Subscribers => "subscribers",
        Role::Vip => "VIPs",
        Role::Mods => "moderators",
        Role::Broadcaster => "the broadcaster",
    }
}

fn kv_err(e: HostError) -> LogicError {
    LogicError::new("KV_UNAVAILABLE", e.0)
}

fn get_json<T: for<'de> Deserialize<'de>>(
    host: &dyn Host,
    key: &str,
) -> Result<Option<T>, LogicError> {
    match host.kv_get(key).map_err(kv_err)? {
        None => Ok(None),
        Some(b) => Ok(serde_json::from_slice(&b).ok()),
    }
}

fn set_json<T: Serialize>(host: &dyn Host, key: &str, v: &T, ttl: u32) -> Result<(), LogicError> {
    let bytes = serde_json::to_vec(v).map_err(|e| LogicError::new("ENCODE", e.to_string()))?;
    host.kv_set(key, &bytes, ttl).map_err(kv_err)
}

fn is_transient(e: &ToolError) -> bool {
    matches!(
        e,
        ToolError::Timeout | ToolError::TargetUnavailable | ToolError::PoolExhausted
    )
}

/// Maps a tool failure to (outcome code, viewer-facing text); never echoes backend detail.
pub fn tool_error_text(e: &ToolError) -> (&'static str, &'static str) {
    match e {
        ToolError::NotGranted | ToolError::Forbidden => (
            "not_granted",
            "Clips aren't enabled for this community yet.",
        ),
        ToolError::Timeout => (
            "timeout",
            "Twitch took too long to answer; try again shortly.",
        ),
        ToolError::TargetUnavailable | ToolError::PoolExhausted | ToolError::DepthExceeded => (
            "unavailable",
            "Twitch is unavailable right now; try again shortly.",
        ),
        ToolError::NoTransport => ("no_transport", "Clips aren't available right now."),
        ToolError::InvalidArgs(m) => {
            let code = m
                .split(|c: char| c == ':' || c.is_whitespace())
                .next()
                .unwrap_or("");
            match code {
                "not_live" => (
                    "not_live",
                    "The channel isn't live, so clips can't be created.",
                ),
                "rate_limited" => (
                    "rate_limited",
                    "Twitch is rate limiting clips right now; try again shortly.",
                ),
                "not_connected" => (
                    "not_connected",
                    "Twitch isn't connected for this community.",
                ),
                "clips_disabled" => ("clips_disabled", "Clips are disabled for this channel."),
                "platform_unavailable" => (
                    "unavailable",
                    "Twitch is unavailable right now; try again shortly.",
                ),
                _ => ("rejected", "Twitch rejected the clip request."),
            }
        }
    }
}

fn parse_clip(v: &Value) -> Result<Clip, LogicError> {
    let clip: Clip = serde_json::from_value(v.clone())
        .map_err(|e| LogicError::new("BAD_TOOL_RESPONSE", format!("clip shape: {e}")))?;
    if !valid_clip_id(&clip.clip_id) || !valid_clip_url(&clip.url) {
        return Err(LogicError::new(
            "BAD_TOOL_RESPONSE",
            "clip id or url failed validation",
        ));
    }
    let title: String = clip
        .title
        .chars()
        .filter(|c| !c.is_control())
        .take(MAX_TITLE_CHARS)
        .collect();
    Ok(Clip { title, ..clip })
}

/// `twitch.clip.get`: `Ok(None)` while Twitch is still processing the clip.
fn get_clip(host: &dyn Host, clip_id: &str) -> Result<Option<Clip>, ToolErrOrLogic> {
    let out = host
        .invoke_tool(TOOL_GET, &json!({ "clip_id": clip_id }), None)
        .map_err(ToolErrOrLogic::Tool)?;
    match out.get("ready").and_then(Value::as_bool) {
        Some(false) => Ok(None),
        Some(true) => {
            let c = out
                .get("clip")
                .ok_or_else(|| LogicError::new("BAD_TOOL_RESPONSE", "ready without clip"))
                .and_then(parse_clip)
                .map_err(ToolErrOrLogic::Logic)?;
            if c.clip_id != clip_id {
                return Err(ToolErrOrLogic::Logic(LogicError::new(
                    "BAD_TOOL_RESPONSE",
                    "clip id mismatch",
                )));
            }
            Ok(Some(c))
        }
        None => Err(ToolErrOrLogic::Logic(LogicError::new(
            "BAD_TOOL_RESPONSE",
            "missing ready flag",
        ))),
    }
}

#[derive(Debug)]
enum ToolErrOrLogic {
    Tool(ToolError),
    Logic(LogicError),
}

/// Sleeps with backoff polling `twitch.clip.get` until ready or `budget_ms` is spent.
/// Transient tool failures count as "not ready yet" (the pending sweep retries them).
fn poll_ready(
    host: &dyn Host,
    clip_id: &str,
    budget_ms: u64,
) -> Result<Option<Clip>, ToolErrOrLogic> {
    let start = host.now_millis();
    let mut i = 0usize;
    loop {
        let elapsed = host.now_millis().saturating_sub(start);
        if elapsed >= budget_ms {
            return Ok(None);
        }
        let d = POLL_DELAYS_MS[i.min(POLL_DELAYS_MS.len() - 1)].min(budget_ms - elapsed);
        host.sleep_ms(d);
        i += 1;
        match get_clip(host, clip_id) {
            Ok(Some(c)) => return Ok(Some(c)),
            Ok(None) => {}
            Err(ToolErrOrLogic::Tool(e)) if is_transient(&e) => {}
            Err(e) => return Err(e),
        }
    }
}

fn announce_text(cfg: &Config, clip: &Clip) -> String {
    truncate_chars(
        &cfg.announce_template
            .replace("{url}", &clip.url)
            .replace("{title}", &clip.title),
        MAX_CHAT_CHARS,
    )
}

/// Chat reply (to the origin channel) plus the optional Discord share for a finished clip.
fn ready_messages(cfg: &Config, provider: &str, channel: &str, clip: &Clip) -> Vec<OutMsg> {
    let mut out = vec![OutMsg {
        provider: provider.to_string(),
        channel: channel.to_string(),
        text: announce_text(cfg, clip),
    }];
    if cfg.discord_share_enabled
        && let Some(ch) = &cfg.discord_share_channel_id
    {
        let t = if clip.title.is_empty() {
            format!("New Twitch clip: {}", clip.url)
        } else {
            format!("New Twitch clip - {}: {}", clip.title, clip.url)
        };
        out.push(OutMsg {
            provider: "discord".into(),
            channel: ch.clone(),
            text: truncate_chars(&t, 1800),
        });
    }
    out
}

fn record_item(host: &dyn Host, clip: &Clip) -> Result<(), LogicError> {
    let seq = host.kv_increment("clip.item.seq", 1, 0).map_err(kv_err)?;
    let slot = (seq.max(1) - 1) as u64 % RING_SLOTS;
    let item = Item {
        seq,
        clip_id: clip.clip_id.clone(),
        url: clip.url.clone(),
        title: clip.title.clone(),
        created_ms: host.now_millis(),
    };
    set_json(host, &format!("clip.item.{slot}"), &item, 0)
}

fn read_items(host: &dyn Host) -> Result<Vec<(u64, Item)>, LogicError> {
    let mut v = Vec::new();
    for slot in 0..RING_SLOTS {
        if let Some(it) = get_json::<Item>(host, &format!("clip.item.{slot}"))? {
            v.push((slot, it));
        }
    }
    v.sort_by_key(|e| std::cmp::Reverse(e.1.seq));
    Ok(v)
}

fn store_pending(host: &dyn Host, p: &Pending, clip_id: &str) -> Result<(), LogicError> {
    let seq = host
        .kv_increment("clip.pending.seq", 1, 0)
        .map_err(kv_err)?;
    let slot = (seq.max(1) - 1) as u64 % PENDING_SLOTS;
    set_json(
        host,
        &format!("clip.pend.{slot}"),
        &json!({"p": p, "clip_id": clip_id}),
        PENDING_TTL_S,
    )
}

#[derive(Deserialize)]
struct PendingRec {
    p: Pending,
    clip_id: String,
}

/// Resolves up to `cap` pending clips: announce when ready, explicit timeout past 15s.
fn sweep(host: &dyn Host, cfg: &Config, cap: usize) -> Result<Vec<OutMsg>, LogicError> {
    let mut out = Vec::new();
    let mut worked = 0usize;
    for slot in 0..PENDING_SLOTS {
        if worked >= cap {
            break;
        }
        let key = format!("clip.pend.{slot}");
        let Some(raw) = host.kv_get(&key).map_err(kv_err)? else {
            continue;
        };
        let Ok(rec) = serde_json::from_slice::<PendingRec>(&raw) else {
            host.log("warn", "sweep", "corrupt_pending_dropped");
            host.kv_delete(&key).map_err(kv_err)?;
            continue;
        };
        worked += 1;
        let age = host.now_millis().saturating_sub(rec.p.created_ms);
        let timeout_msg = || {
            OutMsg {
            provider: rec.p.provider.clone(),
            channel: rec.p.channel.clone(),
            text: "Clip processing timed out after 15s; it may still show up on the channel's clips page."
                .to_string(),
        }
        };
        match get_clip(host, &rec.clip_id) {
            Ok(Some(c)) => {
                record_item(host, &c)?;
                out.extend(ready_messages(cfg, &rec.p.provider, &rec.p.channel, &c));
                host.kv_delete(&key).map_err(kv_err)?;
                host.log("info", "sweep", "announced");
            }
            Ok(None) if age >= PROCESSING_TIMEOUT_MS => {
                out.push(timeout_msg());
                host.kv_delete(&key).map_err(kv_err)?;
                host.log("warn", "sweep", "timed_out");
            }
            Ok(None) => {}
            Err(ToolErrOrLogic::Tool(e)) if is_transient(&e) && age < PROCESSING_TIMEOUT_MS => {}
            Err(ToolErrOrLogic::Tool(e)) => {
                let (code, text) = tool_error_text(&e);
                host.log("warn", "sweep", code);
                out.push(OutMsg {
                    provider: rec.p.provider.clone(),
                    channel: rec.p.channel.clone(),
                    text: text.to_string(),
                });
                host.kv_delete(&key).map_err(kv_err)?;
            }
            Err(ToolErrOrLogic::Logic(e)) => {
                host.log("error", "sweep", e.code);
                host.kv_delete(&key).map_err(kv_err)?;
            }
        }
    }
    Ok(out)
}

fn deadline_left_s(host: &dyn Host, key: &str) -> Result<Option<u64>, LogicError> {
    let Some(b) = host.kv_get(key).map_err(kv_err)? else {
        return Ok(None);
    };
    let Ok(s) = String::from_utf8(b) else {
        return Ok(None);
    };
    let Ok(deadline) = s.parse::<u64>() else {
        return Ok(None);
    };
    let now = host.now_millis();
    Ok(if deadline > now {
        Some((deadline - now).div_ceil(1000))
    } else {
        None
    })
}

fn set_deadline(host: &dyn Host, key: &str, secs: u32, now: u64) -> Result<(), LogicError> {
    if secs == 0 {
        return Ok(());
    }
    let deadline = now + u64::from(secs) * 1000;
    host.kv_set(key, deadline.to_string().as_bytes(), secs + 1)
        .map_err(kv_err)
}

fn reply(provider: &str, channel: &str, text: impl Into<String>) -> Output {
    Output::Messages(vec![OutMsg {
        provider: provider.to_string(),
        channel: channel.to_string(),
        text: truncate_chars(&text.into(), MAX_CHAT_CHARS),
    }])
}

/// Single entry point for a process-stage invocation.
pub fn handle_event(host: &dyn Host, ev: &EventIn) -> Result<Output, LogicError> {
    match (ev.platform.as_str(), ev.event_type.as_str()) {
        ("twitch", "chat.message") => handle_chat(host, ev),
        ("hub", "bundle.admin") => handle_admin(host, ev).map(Output::Admin),
        ("hub", "bundle.reconcile") => {
            let cfg = load_config(host, ev)?;
            let msgs = sweep(host, &cfg, PENDING_SLOTS as usize)?;
            Ok(if msgs.is_empty() {
                Output::None
            } else {
                Output::Messages(msgs)
            })
        }
        _ => Ok(Output::None),
    }
}

fn load_config(host: &dyn Host, ev: &EventIn) -> Result<Config, LogicError> {
    Config::parse(&ev.config_json).map_err(|errs| {
        host.log("error", "config", "invalid");
        LogicError::new("BAD_CONFIG", errs.join("; "))
    })
}

fn handle_chat(host: &dyn Host, ev: &EventIn) -> Result<Output, LogicError> {
    let Some(text) = ev.payload.get("text").and_then(Value::as_str) else {
        return Ok(Output::None);
    };
    let Some(cmd) = parse_command(text) else {
        return Ok(Output::None);
    };
    let channel = ev
        .payload
        .get("channel_id")
        .and_then(Value::as_str)
        .filter(|c| !c.is_empty())
        .ok_or_else(|| LogicError::new("MISSING_CHANNEL", "clips require an origin channel_id"))?;
    let provider = "twitch";

    if host
        .kv_increment(&format!("clip.dedupe.{}", ev.message_id), 1, DEDUPE_TTL_S)
        .map_err(kv_err)?
        > 1
    {
        host.log("debug", "chat", "redelivery_dropped");
        return Ok(Output::None);
    }
    let cfg = match load_config(host, ev) {
        Ok(c) => c,
        Err(_) => {
            return Ok(reply(
                provider,
                channel,
                "Clips are misconfigured; ask a community admin.",
            ));
        }
    };
    let mut extra = sweep(host, &cfg, SWEEP_CAP_CHAT)?;

    let primary = match cmd {
        Command::Clip { title } => cmd_clip(host, ev, &cfg, channel, title)?,
        Command::Clips { limit } => cmd_clips(host, ev, &cfg, channel, limit)?,
        Command::ClipsUsage => reply(provider, channel, "Usage: !clips [1-10]"),
    };
    if extra.is_empty() {
        return Ok(primary);
    }
    if let Output::Messages(m) = primary {
        extra.extend(m);
    }
    Ok(Output::Messages(extra))
}

fn cmd_clip(
    host: &dyn Host,
    ev: &EventIn,
    cfg: &Config,
    channel: &str,
    title: Option<String>,
) -> Result<Output, LogicError> {
    let p = "twitch";
    if !role_allows(cfg.who_can_clip, &ev.payload) {
        return Ok(reply(
            p,
            channel,
            format!("Clips are limited to {}.", role_name(cfg.who_can_clip)),
        ));
    }
    let title = match title.as_deref().map(sanitize_title).transpose() {
        Ok(t) => t.flatten().or_else(|| cfg.default_title.clone()),
        Err(msg) => return Ok(reply(p, channel, msg)),
    };
    let actor = match host.resolve_actor() {
        Ok(a) => a,
        Err(IdentityError::NotLinked) => {
            return Ok(reply(
                p,
                channel,
                "Link your account before creating clips.",
            ));
        }
        Err(IdentityError::Other(_)) => {
            host.log("warn", "clip", "identity_unavailable");
            return Ok(reply(
                p,
                channel,
                "Couldn't verify you right now; try again shortly.",
            ));
        }
    };
    if !actor.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-') || actor.len() > 64 {
        return Err(LogicError::new("BAD_IDENTITY", "actor is not a UUID"));
    }

    // Channel-wide in-flight lock: serialises create+poll so cooldown checks cannot race.
    if host
        .kv_increment("clip.lock", 1, LOCK_TTL_S)
        .map_err(kv_err)?
        > 1
    {
        return Ok(reply(
            p,
            channel,
            "A clip is already being created; try again in a moment.",
        ));
    }
    let out = clip_locked(host, cfg, channel, title, &actor, &ev.message_id);
    let _ = host.kv_delete("clip.lock");
    out
}

fn clip_locked(
    host: &dyn Host,
    cfg: &Config,
    channel: &str,
    title: Option<String>,
    actor: &str,
    message_id: &str,
) -> Result<Output, LogicError> {
    let p = "twitch";
    let user_key = format!("clip.cd.user.{actor}");
    if let Some(s) = deadline_left_s(host, &user_key)? {
        return Ok(reply(p, channel, format!("You can clip again in {s}s.")));
    }
    if let Some(s) = deadline_left_s(host, "clip.cd.channel")? {
        return Ok(reply(
            p,
            channel,
            format!("Clip cooldown: try again in {s}s."),
        ));
    }
    let args = match &title {
        Some(t) => json!({ "title": t }),
        None => json!({}),
    };
    let idem = format!("clip-{message_id}");
    let created = match host.invoke_tool(TOOL_CREATE, &args, Some(&idem)) {
        Ok(v) => v,
        Err(e) => {
            let (code, text) = tool_error_text(&e);
            host.log("warn", "clip_create", code);
            return Ok(reply(p, channel, text));
        }
    };
    let clip_id = created
        .get("clip_id")
        .and_then(Value::as_str)
        .filter(|id| valid_clip_id(id))
        .ok_or_else(|| LogicError::new("BAD_TOOL_RESPONSE", "create returned no valid clip_id"))?
        .to_string();
    let now = host.now_millis();
    set_deadline(host, &user_key, cfg.user_cooldown_secs, now)?;
    set_deadline(host, "clip.cd.channel", cfg.channel_cooldown_secs, now)?;

    match poll_ready(host, &clip_id, u64::from(cfg.poll_budget_ms)) {
        Ok(Some(clip)) => {
            record_item(host, &clip)?;
            host.log("info", "clip", "announced");
            Ok(Output::Messages(ready_messages(cfg, p, channel, &clip)))
        }
        Ok(None) => {
            store_pending(
                host,
                &Pending {
                    provider: p.into(),
                    channel: channel.into(),
                    created_ms: now,
                },
                &clip_id,
            )?;
            host.log("info", "clip", "pending");
            Ok(reply(
                p,
                channel,
                "Clip created, still processing - I'll post the link when it's ready.",
            ))
        }
        Err(ToolErrOrLogic::Tool(e)) => {
            let (code, text) = tool_error_text(&e);
            host.log("warn", "clip_poll", code);
            Ok(reply(p, channel, text))
        }
        Err(ToolErrOrLogic::Logic(e)) => Err(e),
    }
}

fn cmd_clips(
    host: &dyn Host,
    ev: &EventIn,
    cfg: &Config,
    channel: &str,
    limit: Option<u32>,
) -> Result<Output, LogicError> {
    let p = "twitch";
    if !role_allows(cfg.who_can_list, &ev.payload) {
        return Ok(reply(
            p,
            channel,
            format!(
                "Clip listing is limited to {}.",
                role_name(cfg.who_can_list)
            ),
        ));
    }
    let n = limit.unwrap_or(cfg.list_limit);
    if !(1..=10).contains(&n) {
        return Ok(reply(p, channel, "Usage: !clips [1-10]"));
    }
    if let Some(s) = deadline_left_s(host, "clip.cd.list")? {
        return Ok(reply(
            p,
            channel,
            format!("Clip list cooldown: try again in {s}s."),
        ));
    }
    // Over-fetch so tombstoned clips can be filtered without under-filling.
    let out = match host.invoke_tool(TOOL_LIST, &json!({ "limit": (n * 2).min(100) }), None) {
        Ok(v) => v,
        Err(e) => {
            let (code, text) = tool_error_text(&e);
            host.log("warn", "clip_list", code);
            return Ok(reply(p, channel, text));
        }
    };
    set_deadline(
        host,
        "clip.cd.list",
        cfg.list_cooldown_secs,
        host.now_millis(),
    )?;
    let arr = out
        .get("clips")
        .and_then(Value::as_array)
        .ok_or_else(|| LogicError::new("BAD_TOOL_RESPONSE", "list returned no clips array"))?;
    let mut shown = Vec::new();
    for v in arr {
        if shown.len() >= n as usize {
            break;
        }
        let clip = parse_clip(v)?;
        if host
            .kv_get(&format!("clip.hidden.{}", clip.clip_id))
            .map_err(kv_err)?
            .is_some()
        {
            continue;
        }
        shown.push(clip);
    }
    if shown.is_empty() {
        return Ok(reply(p, channel, "No clips yet."));
    }
    let body = shown
        .iter()
        .enumerate()
        .map(|(i, c)| {
            if c.title.is_empty() {
                format!("{}. {}", i + 1, c.url)
            } else {
                format!("{}. {} {}", i + 1, truncate_chars(&c.title, 40), c.url)
            }
        })
        .collect::<Vec<_>>()
        .join(" | ");
    Ok(reply(p, channel, format!("Recent clips: {body}")))
}

fn handle_admin(host: &dyn Host, ev: &EventIn) -> Result<Value, LogicError> {
    let action = ev
        .payload
        .get("action")
        .and_then(Value::as_str)
        .ok_or_else(|| LogicError::new("BAD_ADMIN", "action required"))?;
    let args = ev.payload.get("args").cloned().unwrap_or_else(|| json!({}));
    host.log("info", "admin", action);
    let data = match action {
        "list" => {
            let items: Vec<Value> = read_items(host)?
                .into_iter()
                .map(|(_, i)| json!({"clip_id": i.clip_id, "url": i.url, "title": i.title, "created_ms": i.created_ms}))
                .collect();
            let mut pending = 0u64;
            for s in 0..PENDING_SLOTS {
                if host
                    .kv_get(&format!("clip.pend.{s}"))
                    .map_err(kv_err)?
                    .is_some()
                {
                    pending += 1;
                }
            }
            json!({ "items": items, "pending": pending })
        }
        "delete" => {
            let id = args
                .get("clip_id")
                .and_then(Value::as_str)
                .filter(|i| valid_clip_id(i))
                .ok_or_else(|| LogicError::new("BAD_ADMIN", "valid clip_id required"))?;
            let mut removed = 0u64;
            for (slot, it) in read_items(host)? {
                if it.clip_id == id {
                    host.kv_delete(&format!("clip.item.{slot}"))
                        .map_err(kv_err)?;
                    removed += 1;
                }
            }
            for s in 0..PENDING_SLOTS {
                let key = format!("clip.pend.{s}");
                if let Some(raw) = host.kv_get(&key).map_err(kv_err)?
                    && serde_json::from_slice::<PendingRec>(&raw)
                        .map(|r| r.clip_id == id)
                        .unwrap_or(false)
                {
                    host.kv_delete(&key).map_err(kv_err)?;
                    removed += 1;
                }
            }
            host.kv_set(&format!("clip.hidden.{id}"), b"1", 0)
                .map_err(kv_err)?;
            json!({ "clip_id": id, "removed_index_entries": removed, "hidden_from_chat": true })
        }
        "config" => {
            let effective = Config::parse(&ev.config_json);
            let candidate = args.get("config").map(|c| {
                serde_json::from_value::<Config>(c.clone())
                    .map_err(|e| vec![format!("config is not valid: {e}")])
                    .and_then(|c| c.validate().map(|()| c))
            });
            json!({
                "effective": effective.as_ref().ok(),
                "effective_errors": effective.err().unwrap_or_default(),
                "candidate_valid": candidate.as_ref().map(Result::is_ok),
                "candidate_errors": candidate.and_then(Result::err).unwrap_or_default(),
            })
        }
        "sweep" => {
            let cfg = load_config(host, ev)?;
            let msgs = sweep(host, &cfg, PENDING_SLOTS as usize)?;
            json!({ "resolved_messages": msgs })
        }
        other => return Err(LogicError::new("UNKNOWN_ADMIN_ACTION", other.to_string())),
    };
    Ok(json!({ "ok": true, "action": action, "data": data }))
}

/// Validates the action-stage payload (`{"messages":[...]}`) before anything is relayed.
pub fn relay_plan(payload: &Value) -> Result<Vec<OutMsg>, LogicError> {
    let Some(arr) = payload.get("messages") else {
        return Ok(Vec::new());
    };
    let msgs: Vec<OutMsg> = serde_json::from_value(arr.clone())
        .map_err(|e| LogicError::new("BAD_PAYLOAD", e.to_string()))?;
    for m in &msgs {
        let ok_provider = matches!(m.provider.as_str(), "twitch" | "discord");
        if !ok_provider
            || m.channel.is_empty()
            || m.text.is_empty()
            || m.text.chars().count() > 2000
        {
            return Err(LogicError::new(
                "BAD_PAYLOAD",
                "relay message failed validation",
            ));
        }
    }
    Ok(msgs)
}

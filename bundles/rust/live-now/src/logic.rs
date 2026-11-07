//! Host-testable core of the `live-now` app bundle.
//!
//! Consumes the common `<platform>.live.channels` tool contract
//! (`bundles/contracts/live-channels-contract.v1.json`) from the platform SERVICE bundles,
//! aggregates a community "who's live" snapshot, answers `!live`, and produces go-live
//! announcements deduped per stream session with a per-channel cooldown. Every host
//! capability (tool invoke, kv, clock, flags, log) sits behind [`Host`] so the same code runs
//! natively in tests and inside the WASM component (`lib.rs` supplies the real seam).

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

/// Platforms with a `<platform>.live.channels` provider in the contract.
pub const CONTRACT_PLATFORMS: [&str; 4] = ["twitch", "youtube", "discord", "kick"];
/// Platforms enabled when the community config names none (kick ships later).
pub const DEFAULT_PLATFORMS: [&str; 3] = ["twitch", "youtube", "discord"];
/// Platforms this bundle may post announcements to (declared `chat.send:*` permissions).
pub const CHAT_PLATFORMS: [&str; 3] = ["discord", "twitch", "youtube"];
/// Platform service bundle app-id prefix; the target is `<prefix><platform>`.
pub const SERVICE_PREFIX: &str = "waddles.core.service.";
/// Contract error codes a provider's invalid-args message may start with.
pub const ERROR_CODES: [&str; 3] = ["platform_unavailable", "rate_limited", "not_connected"];

/// PostHog flag gating the `!live` command (default OFF).
pub const FLAG_COMMAND: &str = "live-now.command";
/// PostHog flag gating go-live announcements (default OFF).
pub const FLAG_ANNOUNCE: &str = "live-now.announce";

const SNAPSHOT_KEY: &str = "snapshot";
const SNAPSHOT_TTL_S: u32 = 600;
/// A cached snapshot younger than this answers `!live` without re-calling the platforms.
const SNAPSHOT_FRESH_MS: u64 = 30_000;
const SESSION_TTL_S: u32 = 12 * 3600;
const MAX_PAGES: usize = 5;
const MAX_REPLY_CHARS: usize = 400;
const MAX_REPLY_ENTRIES: usize = 5;
const DEFAULT_TEMPLATE: &str = "{name} is live on {platform}: {title}";
const PLACEHOLDERS: [&str; 5] = ["name", "platform", "title", "category", "viewers"];

/// Severity for [`Host::log`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Level {
    Debug,
    Info,
    Warn,
    Error,
}

/// Every host capability the logic needs.
pub trait Host {
    /// `tool.invoke(target, tool, args)` returning the result JSON bytes.
    fn invoke(&self, target: &str, tool: &str, args: &Value) -> Result<Vec<u8>, ToolError>;
    /// Raw kv read (community+app scoped by the host).
    fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String>;
    /// Raw kv write with TTL.
    fn kv_set(&self, key: &str, value: &[u8], ttl_seconds: u32) -> Result<(), String>;
    /// Atomic kv add; the first claimant of a fresh key sees `1`.
    fn kv_increment(&self, key: &str, delta: i64, ttl_seconds: u32) -> Result<i64, String>;
    /// Milliseconds since the Unix epoch.
    fn now_millis(&self) -> u64;
    /// PostHog flag lookup.
    fn flag(&self, key: &str, default: bool) -> bool;
    /// PII-free structured log line (ids/op/counts only).
    fn log(&self, level: Level, message: &str, fields: Value);
}

/// One announcement destination.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct Target {
    pub platform: String,
    pub channel_id: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawConfig {
    platforms: Option<Vec<String>>,
    #[serde(default)]
    include_linked: bool,
    #[serde(default)]
    announce_targets: Vec<Target>,
    template: Option<String>,
    cooldown_seconds: Option<u64>,
    max_age_seconds: Option<u64>,
}

/// Validated per-community configuration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    pub platforms: Vec<String>,
    pub include_linked: bool,
    pub announce_targets: Vec<Target>,
    pub template: String,
    pub cooldown_seconds: u64,
    pub max_age_seconds: u64,
}

impl Config {
    /// Parses and validates the resolved config JSON; unknown keys, unknown platforms,
    /// unknown template placeholders and out-of-range numbers are all hard errors.
    pub fn parse(config_json: &str) -> Result<Self, String> {
        let text = if config_json.trim().is_empty() {
            "{}"
        } else {
            config_json
        };
        let raw: RawConfig =
            serde_json::from_str(text).map_err(|e| format!("invalid config: {e}"))?;
        let platforms: Vec<String> = match raw.platforms {
            None => DEFAULT_PLATFORMS.iter().map(|s| s.to_string()).collect(),
            Some(p) if p.is_empty() => return Err("platforms must not be empty".into()),
            Some(p) => p,
        };
        let mut seen = BTreeSet::new();
        for p in &platforms {
            if !CONTRACT_PLATFORMS.contains(&p.as_str()) {
                return Err(format!("unknown platform: {p}"));
            }
            if !seen.insert(p.clone()) {
                return Err(format!("duplicate platform: {p}"));
            }
        }
        if raw.announce_targets.len() > 10 {
            return Err("at most 10 announce_targets".into());
        }
        for t in &raw.announce_targets {
            if !CHAT_PLATFORMS.contains(&t.platform.as_str()) {
                return Err(format!(
                    "announce target platform not supported: {}",
                    t.platform
                ));
            }
            if t.channel_id.is_empty() || t.channel_id.len() > 128 {
                return Err("announce target channel_id must be 1-128 chars".into());
            }
        }
        let template = raw.template.unwrap_or_else(|| DEFAULT_TEMPLATE.to_string());
        validate_template(&template)?;
        let cooldown_seconds = raw.cooldown_seconds.unwrap_or(1800);
        if !(60..=86_400).contains(&cooldown_seconds) {
            return Err("cooldown_seconds must be 60-86400".into());
        }
        let max_age_seconds = raw.max_age_seconds.unwrap_or(900);
        if !(60..=3600).contains(&max_age_seconds) {
            return Err("max_age_seconds must be 60-3600".into());
        }
        Ok(Self {
            platforms,
            include_linked: raw.include_linked,
            announce_targets: raw.announce_targets,
            template,
            cooldown_seconds,
            max_age_seconds,
        })
    }
}

fn validate_template(t: &str) -> Result<(), String> {
    if t.is_empty() || t.chars().count() > 300 {
        return Err("template must be 1-300 chars".into());
    }
    let mut rest = t;
    while let Some(i) = rest.find('{') {
        let after = &rest[i + 1..];
        let Some(j) = after.find('}') else {
            return Err("template has an unclosed '{'".into());
        };
        let name = &after[..j];
        if !PLACEHOLDERS.contains(&name) {
            return Err(format!("unknown template placeholder: {{{name}}}"));
        }
        rest = &after[j + 1..];
    }
    if rest.contains('}') {
        return Err("template has a stray '}'".into());
    }
    Ok(())
}

/// Contract channel (`definitions.channel`).
#[derive(Debug, Clone, Deserialize)]
struct ContractChannel {
    channel_ref: String,
    display_name: String,
    is_live: bool,
    title: Option<String>,
    category: Option<String>,
    viewers: Option<u64>,
    started_at: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ContractOutput {
    channels: Vec<ContractChannel>,
    cursor: Option<String>,
    #[allow(dead_code)]
    as_of: String,
}

/// One aggregated stream entry (shape mirrors the old `live_status` `streams[]`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Stream {
    pub platform: String,
    pub channel_ref: String,
    pub channel: String,
    pub live: bool,
    pub since: Option<String>,
    pub viewer_count: u64,
    pub title: Option<String>,
    pub category: Option<String>,
    pub linked: bool,
}

/// A platform that failed during refresh, with the contract error code.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PlatformError {
    pub platform: String,
    pub code: String,
}

/// The community "who's live" snapshot (parity with `live_status`'s community summary).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Snapshot {
    pub live: bool,
    pub platform: Option<String>,
    pub channel: Option<String>,
    pub since: Option<String>,
    pub viewer_count: u64,
    pub streams: Vec<Stream>,
    pub errors: Vec<PlatformError>,
    pub as_of_ms: u64,
}

/// One outbound chat message for the action stage to relay.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct OutMessage {
    pub platform: String,
    pub channel: String,
    pub text: String,
}

/// Classifies a provider failure into a contract error code (or a transport label).
pub fn error_code(e: &ToolError) -> String {
    match e {
        ToolError::InvalidArgs(m) => {
            for code in ERROR_CODES {
                if m == code || m.strip_prefix(code).is_some_and(|r| r.starts_with(':')) {
                    return code.to_string();
                }
            }
            "contract_violation".to_string()
        }
        ToolError::TargetUnavailable => "not_installed".to_string(),
        ToolError::NotGranted => "not_granted".to_string(),
        ToolError::Forbidden => "forbidden".to_string(),
        ToolError::Timeout => "timeout".to_string(),
        ToolError::DepthExceeded => "depth_exceeded".to_string(),
        ToolError::PoolExhausted => "pool_exhausted".to_string(),
        ToolError::NoTransport => "no_transport".to_string(),
    }
}

/// Removes control chars and mention/markup characters from platform-supplied text, collapses
/// whitespace and truncates to `max` chars, so a stream title can never ping or inject markup.
pub fn clean_text(s: &str, max: usize) -> String {
    let filtered: String = s
        .chars()
        .filter(|c| !c.is_control() || c.is_whitespace())
        .filter(|c| !matches!(c, '@' | '<' | '>' | '`' | '\u{200b}'..='\u{200f}' | '\u{202a}'..='\u{202e}'))
        .collect();
    let collapsed = filtered.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed.chars().take(max).collect()
}

fn fetch_platform(host: &dyn Host, platform: &str, cfg: &Config) -> Result<Vec<Stream>, ToolError> {
    let target = format!("{SERVICE_PREFIX}{platform}");
    let tool = format!("{platform}.live.channels");
    let mut scopes = vec!["community"];
    if cfg.include_linked {
        scopes.push("linked");
    }
    let mut out: Vec<Stream> = Vec::new();
    for scope in scopes {
        let mut cursor: Option<String> = None;
        for page in 0.. {
            if page >= MAX_PAGES {
                return Err(ToolError::InvalidArgs(
                    "contract_violation: cursor never ended".into(),
                ));
            }
            let mut args = json!({"scope": scope, "limit": 100});
            if let Some(c) = &cursor {
                args["cursor"] = json!(c);
            }
            let bytes = host.invoke(&target, &tool, &args)?;
            let parsed: ContractOutput = serde_json::from_slice(&bytes)
                .map_err(|_| ToolError::InvalidArgs("contract_violation: bad output".into()))?;
            for ch in parsed.channels {
                if out.iter().any(|s| s.channel_ref == ch.channel_ref) {
                    continue; // community scope wins over linked
                }
                out.push(Stream {
                    platform: platform.to_string(),
                    channel_ref: ch.channel_ref,
                    channel: clean_text(&ch.display_name, 100),
                    live: ch.is_live,
                    since: if ch.is_live { ch.started_at } else { None },
                    viewer_count: ch.viewers.unwrap_or(0),
                    title: ch
                        .title
                        .map(|t| clean_text(&t, 300))
                        .filter(|t| !t.is_empty()),
                    category: ch
                        .category
                        .map(|t| clean_text(&t, 100))
                        .filter(|t| !t.is_empty()),
                    linked: scope == "linked",
                });
            }
            match parsed.cursor {
                Some(c) if !c.is_empty() => cursor = Some(c),
                _ => break,
            }
        }
    }
    Ok(out)
}

/// Calls every configured platform's `live.channels` tool and aggregates the result. A failing
/// platform is recorded in `errors` (loud, never an empty success); if EVERY platform fails the
/// whole refresh is an `Err`. Live streams sort first, then by viewers descending.
pub fn refresh(host: &dyn Host, cfg: &Config) -> Result<Snapshot, String> {
    let mut streams = Vec::new();
    let mut errors = Vec::new();
    for p in &cfg.platforms {
        match fetch_platform(host, p, cfg) {
            Ok(mut s) => streams.append(&mut s),
            Err(e) => {
                let code = error_code(&e);
                host.log(
                    Level::Warn,
                    "live-now platform refresh failed",
                    json!({"platform": p, "code": code}),
                );
                errors.push(PlatformError {
                    platform: p.clone(),
                    code,
                });
            }
        }
    }
    if errors.len() == cfg.platforms.len() {
        return Err(format!(
            "all platforms failed: {}",
            errors
                .iter()
                .map(|e| format!("{}={}", e.platform, e.code))
                .collect::<Vec<_>>()
                .join(",")
        ));
    }
    streams.sort_by(|a, b| {
        b.live
            .cmp(&a.live)
            .then(b.viewer_count.cmp(&a.viewer_count))
    });
    let primary = streams.iter().find(|s| s.live).cloned();
    let snap = Snapshot {
        live: primary.is_some(),
        platform: primary.as_ref().map(|s| s.platform.clone()),
        channel: primary.as_ref().map(|s| s.channel.clone()),
        since: primary.as_ref().and_then(|s| s.since.clone()),
        viewer_count: primary.as_ref().map_or(0, |s| s.viewer_count),
        streams,
        errors,
        as_of_ms: host.now_millis(),
    };
    host.log(
        Level::Debug,
        "live-now refreshed",
        json!({"streams": snap.streams.len(), "live": snap.streams.iter().filter(|s| s.live).count(), "errors": snap.errors.len()}),
    );
    match serde_json::to_vec(&snap) {
        Ok(bytes) => {
            if let Err(e) = host.kv_set(SNAPSHOT_KEY, &bytes, SNAPSHOT_TTL_S) {
                host.log(
                    Level::Warn,
                    "live-now snapshot not persisted",
                    json!({"error": e}),
                );
            }
        }
        Err(e) => return Err(format!("snapshot encode: {e}")),
    }
    Ok(snap)
}

/// Returns the cached snapshot when fresh (<30 s), otherwise refreshes.
pub fn snapshot_cached(host: &dyn Host, cfg: &Config) -> Result<Snapshot, String> {
    if let Ok(Some(bytes)) = host.kv_get(SNAPSHOT_KEY)
        && let Ok(snap) = serde_json::from_slice::<Snapshot>(&bytes)
        && host.now_millis().saturating_sub(snap.as_of_ms) < SNAPSHOT_FRESH_MS
    {
        return Ok(snap);
    }
    refresh(host, cfg)
}

/// Parses an RFC 3339 timestamp (`Z` or `+hh:mm` offset, optional fraction) to epoch millis.
pub fn parse_rfc3339_millis(s: &str) -> Option<u64> {
    let b = s.as_bytes();
    if b.len() < 20
        || b[4] != b'-'
        || b[7] != b'-'
        || !(b[10] == b'T' || b[10] == b't')
        || b[13] != b':'
        || b[16] != b':'
    {
        return None;
    }
    let num = |r: std::ops::Range<usize>| -> Option<i64> { s.get(r)?.parse::<i64>().ok() };
    let (y, mo, d, h, mi, sec) = (
        num(0..4)?,
        num(5..7)?,
        num(8..10)?,
        num(11..13)?,
        num(14..16)?,
        num(17..19)?,
    );
    if !(1..=12).contains(&mo)
        || !(1..=31).contains(&d)
        || h > 23
        || mi > 59
        || sec > 60
        || y < 1970
    {
        return None;
    }
    let mut idx = 19;
    let mut millis = 0i64;
    if b.get(idx) == Some(&b'.') {
        let start = idx + 1;
        idx = start;
        while b.get(idx).is_some_and(u8::is_ascii_digit) {
            idx += 1;
        }
        if idx == start {
            return None;
        }
        let frac: String = s[start..idx].chars().take(3).collect();
        millis = format!("{frac:0<3}").parse().ok()?;
    }
    let offset_s: i64 = match b.get(idx)? {
        b'Z' | b'z' if idx + 1 == b.len() => 0,
        sign @ (b'+' | b'-') if idx + 6 == b.len() && b[idx + 3] == b':' => {
            let oh = num(idx + 1..idx + 3)?;
            let om = num(idx + 4..idx + 6)?;
            if oh > 23 || om > 59 {
                return None;
            }
            let v = oh * 3600 + om * 60;
            if *sign == b'+' { v } else { -v }
        }
        _ => return None,
    };
    // days-from-civil (Howard Hinnant)
    let yy = if mo <= 2 { y - 1 } else { y };
    let era = yy.div_euclid(400);
    let yoe = yy - era * 400;
    let doy = (153 * (mo + if mo > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146_097 + doe - 719_468;
    let secs = days * 86_400 + h * 3600 + mi * 60 + sec - offset_s;
    u64::try_from(secs * 1000 + millis).ok()
}

fn fnv64(seed: u64, data: &str) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64 ^ seed;
    for byte in data.bytes() {
        h ^= u64::from(byte);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

/// 128-bit-ish stable hex digest (two FNV-1a lanes) -- kv-key-safe (no ':'), not security-relevant.
pub fn key_hash(data: &str) -> String {
    format!(
        "{:016x}{:016x}",
        fnv64(0, data),
        fnv64(0x9e37_79b9_7f4a_7c15, data)
    )
}

fn render(template: &str, s: &Stream) -> String {
    let viewers = s.viewer_count.to_string();
    let title = s.title.clone().unwrap_or_else(|| "(no title)".to_string());
    let category = s.category.clone().unwrap_or_default();
    let out = template
        .replace("{name}", &s.channel)
        .replace("{platform}", &s.platform)
        .replace("{title}", &title)
        .replace("{category}", &category)
        .replace("{viewers}", &viewers);
    clean_text(&out, 400)
}

/// Decides which live channels newly went live and renders the announcement for each target.
/// Dedupe: an atomic kv claim per stream SESSION (platform + channel_ref + started_at); a
/// per-channel cooldown claim then suppresses rapid re-announcements. A stream older than
/// `max_age_seconds` is never announced (enabling the feature must not flood chat), and a
/// `started_at` that is present but unparsable is skipped and logged as a contract violation.
pub fn announcements(host: &dyn Host, cfg: &Config, snap: &Snapshot) -> Vec<OutMessage> {
    if cfg.announce_targets.is_empty() {
        return Vec::new();
    }
    let now = host.now_millis();
    let (mut sent, mut dup, mut cooled, mut stale, mut invalid) = (0u32, 0u32, 0u32, 0u32, 0u32);
    let mut out = Vec::new();
    for s in snap.streams.iter().filter(|s| s.live) {
        if let Some(since) = s.since.as_deref() {
            match parse_rfc3339_millis(since) {
                Some(start) if now.saturating_sub(start) > cfg.max_age_seconds * 1000 => {
                    stale += 1;
                    continue;
                }
                Some(_) => {}
                None => {
                    invalid += 1;
                    continue;
                }
            }
        }
        let session = format!(
            "{}|{}|{}",
            s.platform,
            s.channel_ref,
            s.since.as_deref().unwrap_or("-")
        );
        let claimed = host.kv_increment(&format!("ann.{}", key_hash(&session)), 1, SESSION_TTL_S);
        match claimed {
            Ok(1) => {}
            Ok(_) => {
                dup += 1;
                continue;
            }
            Err(e) => {
                host.log(
                    Level::Error,
                    "live-now dedupe claim failed; not announcing",
                    json!({"error": e}),
                );
                continue;
            }
        }
        let cd_key = format!(
            "cd.{}",
            key_hash(&format!("{}|{}", s.platform, s.channel_ref))
        );
        let cd_ttl = u32::try_from(cfg.cooldown_seconds).unwrap_or(86_400);
        match host.kv_increment(&cd_key, 1, cd_ttl) {
            Ok(1) => {}
            Ok(_) => {
                cooled += 1;
                continue;
            }
            Err(e) => {
                host.log(
                    Level::Error,
                    "live-now cooldown claim failed; not announcing",
                    json!({"error": e}),
                );
                continue;
            }
        }
        let text = render(&cfg.template, s);
        for t in &cfg.announce_targets {
            out.push(OutMessage {
                platform: t.platform.clone(),
                channel: t.channel_id.clone(),
                text: text.clone(),
            });
        }
        sent += 1;
    }
    host.log(
        Level::Info,
        "live-now announcements evaluated",
        json!({"announced": sent, "deduped": dup, "cooldown": cooled, "stale": stale, "invalid_started_at": invalid}),
    );
    out
}

/// Formats the `!live` chat reply from a snapshot (optionally filtered to one platform).
pub fn format_reply(snap: &Snapshot, platform: Option<&str>) -> String {
    let live: Vec<&Stream> = snap
        .streams
        .iter()
        .filter(|s| s.live && platform.is_none_or(|p| s.platform == p))
        .collect();
    let mut text = if live.is_empty() {
        "Nobody is live right now.".to_string()
    } else {
        let parts: Vec<String> = live
            .iter()
            .take(MAX_REPLY_ENTRIES)
            .map(|s| {
                let mut p = format!("{} ({}, {} viewers)", s.channel, s.platform, s.viewer_count);
                if let Some(t) = &s.title {
                    p.push_str(&format!(" - {}", clean_text(t, 60)));
                }
                p
            })
            .collect();
        let more = live.len().saturating_sub(MAX_REPLY_ENTRIES);
        let mut t = format!("Live now: {}", parts.join(" | "));
        if more > 0 {
            t.push_str(&format!(" (+{more} more)"));
        }
        t
    };
    if !snap.errors.is_empty() {
        let names: Vec<String> = snap
            .errors
            .iter()
            .map(|e| format!("{} ({})", e.platform, e.code))
            .collect();
        text.push_str(&format!(" [unavailable: {}]", names.join(", ")));
    }
    text.chars().take(MAX_REPLY_CHARS).collect()
}

/// What an inbound event asks of the bundle.
#[derive(Debug, PartialEq, Eq)]
enum Trigger {
    Command(Option<String>),
    Refresh,
    Ignore,
}

fn classify(event_type: &str, payload: &Value) -> Result<Trigger, String> {
    match event_type {
        "chat.message" => {
            let Some(text) = payload.get("text").and_then(Value::as_str) else {
                return Ok(Trigger::Ignore);
            };
            let mut it = text.split_whitespace();
            match it.next() {
                Some(cmd) if cmd.eq_ignore_ascii_case("!live") => {}
                _ => return Ok(Trigger::Ignore),
            }
            let arg = it.next().map(str::to_ascii_lowercase);
            if it.next().is_some() {
                return Ok(Trigger::Ignore);
            }
            match arg {
                None => Ok(Trigger::Command(None)),
                Some(p) if CONTRACT_PLATFORMS.contains(&p.as_str()) => {
                    Ok(Trigger::Command(Some(p)))
                }
                Some(_) => Ok(Trigger::Ignore),
            }
        }
        "stream.online" | "stream.offline" | "live.refresh" => Ok(Trigger::Refresh),
        _ => Ok(Trigger::Ignore),
    }
}

/// Process-stage entry: maps one inbound event to the outbound messages (possibly none).
/// `Err` is a loud configuration/refresh failure; the caller decides how to surface it.
pub fn handle_event(
    host: &dyn Host,
    cfg: &Result<Config, String>,
    platform: &str,
    event_type: &str,
    payload: &Value,
) -> Result<Vec<OutMessage>, String> {
    let trigger = classify(event_type, payload)?;
    let mut out = Vec::new();
    match trigger {
        Trigger::Ignore => {}
        Trigger::Command(filter) => {
            if !host.flag(FLAG_COMMAND, false) {
                return Ok(out);
            }
            let channel = payload
                .get("channel_id")
                .and_then(Value::as_str)
                .filter(|c| !c.is_empty())
                .ok_or_else(|| "chat.message has no channel_id".to_string())?;
            let result = cfg
                .clone()
                .and_then(|c| snapshot_cached(host, &c).map(|s| (c, s)));
            match result {
                Ok((cfg, snap)) => {
                    out.push(OutMessage {
                        platform: platform.to_string(),
                        channel: channel.to_string(),
                        text: format_reply(&snap, filter.as_deref()),
                    });
                    if host.flag(FLAG_ANNOUNCE, false) {
                        out.extend(announcements(host, &cfg, &snap));
                    }
                }
                Err(e) => {
                    // Loud: logged with the reason, and the asker is told instead of silence.
                    host.log(Level::Error, "live-now command failed", json!({"error": e}));
                    out.push(OutMessage {
                        platform: platform.to_string(),
                        channel: channel.to_string(),
                        text: "Live status is unavailable right now.".to_string(),
                    });
                }
            }
        }
        Trigger::Refresh => {
            if !host.flag(FLAG_ANNOUNCE, false) {
                return Ok(out);
            }
            let cfg = cfg.as_ref().map_err(|e| e.clone())?;
            let snap = refresh(host, cfg)?;
            out.extend(announcements(host, cfg, &snap));
        }
    }
    Ok(out)
}

/// Decodes and validates the action-stage payload `{"messages":[{platform,channel,text}]}`
/// the process stage produced; anything malformed is a hard error (never a partial send).
pub fn parse_messages(payload_json: &str) -> Result<Vec<OutMessage>, String> {
    #[derive(Deserialize)]
    struct P {
        messages: Vec<OutMessage>,
    }
    let p: P =
        serde_json::from_str(payload_json).map_err(|e| format!("bad action payload: {e}"))?;
    if p.messages.len() > 64 {
        return Err("too many messages".into());
    }
    for m in &p.messages {
        if !CHAT_PLATFORMS.contains(&m.platform.as_str()) {
            return Err(format!("unsupported relay platform: {}", m.platform));
        }
        if m.channel.is_empty()
            || m.channel.len() > 128
            || m.text.is_empty()
            || m.text.chars().count() > 2000
        {
            return Err("message channel/text out of bounds".into());
        }
    }
    Ok(p.messages)
}

/// In-memory [`Host`] for tests (native only).
#[allow(clippy::unwrap_used, clippy::expect_used)]
pub mod testing {
    use super::*;
    use std::cell::RefCell;
    use std::collections::HashMap;

    /// Scripted tool results keyed by `target|tool|scope|cursor`, in-memory kv, fixed clock.
    #[derive(Default)]
    pub struct FakeHost {
        pub replies: RefCell<HashMap<String, Result<Value, ToolError>>>,
        pub kv: RefCell<HashMap<String, Vec<u8>>>,
        pub calls: RefCell<Vec<(String, String, Value)>>,
        pub logs: RefCell<Vec<(Level, String, Value)>>,
        pub now: u64,
        pub flags: RefCell<HashMap<String, bool>>,
    }

    impl FakeHost {
        /// Scripts the first page for `platform`/`scope`.
        pub fn script(&self, platform: &str, scope: &str, r: Result<Value, ToolError>) {
            self.replies
                .borrow_mut()
                .insert(format!("{platform}|{scope}|"), r);
        }
        /// Scripts a follow-up page.
        pub fn script_page(
            &self,
            platform: &str,
            scope: &str,
            cursor: &str,
            r: Result<Value, ToolError>,
        ) {
            self.replies
                .borrow_mut()
                .insert(format!("{platform}|{scope}|{cursor}"), r);
        }
        pub fn set_flag(&self, k: &str, v: bool) {
            self.flags.borrow_mut().insert(k.to_string(), v);
        }
    }

    impl Host for FakeHost {
        fn invoke(&self, target: &str, tool: &str, args: &Value) -> Result<Vec<u8>, ToolError> {
            self.calls
                .borrow_mut()
                .push((target.to_string(), tool.to_string(), args.clone()));
            let platform = target.strip_prefix(SERVICE_PREFIX).unwrap_or(target);
            let key = format!(
                "{platform}|{}|{}",
                args["scope"].as_str().unwrap_or(""),
                args.get("cursor").and_then(Value::as_str).unwrap_or("")
            );
            match self.replies.borrow().get(&key) {
                Some(Ok(v)) => Ok(serde_json::to_vec(v).unwrap()),
                Some(Err(e)) => Err(e.clone()),
                None => Err(ToolError::TargetUnavailable),
            }
        }
        fn kv_get(&self, key: &str) -> Result<Option<Vec<u8>>, String> {
            Ok(self.kv.borrow().get(key).cloned())
        }
        fn kv_set(&self, key: &str, value: &[u8], _ttl: u32) -> Result<(), String> {
            self.kv.borrow_mut().insert(key.to_string(), value.to_vec());
            Ok(())
        }
        fn kv_increment(&self, key: &str, delta: i64, _ttl: u32) -> Result<i64, String> {
            let mut kv = self.kv.borrow_mut();
            let cur: i64 = kv
                .get(key)
                .and_then(|b| std::str::from_utf8(b).ok()?.parse().ok())
                .unwrap_or(0);
            let n = cur + delta;
            kv.insert(key.to_string(), n.to_string().into_bytes());
            Ok(n)
        }
        fn now_millis(&self) -> u64 {
            self.now
        }
        fn flag(&self, key: &str, default: bool) -> bool {
            self.flags.borrow().get(key).copied().unwrap_or(default)
        }
        fn log(&self, level: Level, message: &str, fields: Value) {
            self.logs
                .borrow_mut()
                .push((level, message.to_string(), fields));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::FakeHost;
    use super::*;

    const NOW: &str = "2026-11-07T12:00:00Z";

    fn now_ms() -> u64 {
        parse_rfc3339_millis(NOW).unwrap()
    }

    fn host() -> FakeHost {
        FakeHost {
            now: now_ms(),
            ..Default::default()
        }
    }

    fn chan(r: &str, name: &str, live: bool, viewers: u64, started: Option<&str>) -> Value {
        json!({"channel_ref": r, "display_name": name, "is_live": live, "title": "Hello @everyone <#1>",
               "category": "Games", "viewers": viewers, "started_at": started})
    }

    fn page(channels: Vec<Value>) -> Result<Value, ToolError> {
        Ok(json!({"channels": channels, "as_of": NOW}))
    }

    fn cfg(extra: &str) -> Config {
        Config::parse(extra).unwrap()
    }

    fn announce_cfg() -> Config {
        cfg(
            r#"{"platforms":["twitch"],"announce_targets":[{"platform":"discord","channel_id":"c1"}]}"#,
        )
    }

    #[test]
    fn rfc3339_parses_known_instants() {
        assert_eq!(parse_rfc3339_millis("1970-01-01T00:00:00Z"), Some(0));
        assert_eq!(
            parse_rfc3339_millis("2026-11-07T12:00:00.250Z"),
            Some(1_794_052_800_250)
        );
        assert_eq!(
            parse_rfc3339_millis("2026-11-07T14:00:00+02:00"),
            parse_rfc3339_millis("2026-11-07T12:00:00Z")
        );
    }

    #[test]
    fn rfc3339_rejects_garbage() {
        for bad in [
            "",
            "yesterday",
            "2026-13-07T12:00:00Z",
            "2026-11-07 12:00:00Z",
            "2026-11-07T12:00:00",
            "1969-01-01T00:00:00Z",
        ] {
            assert_eq!(parse_rfc3339_millis(bad), None, "{bad}");
        }
    }

    #[test]
    fn config_defaults_and_validation() {
        let c = cfg("{}");
        assert_eq!(c.platforms, vec!["twitch", "youtube", "discord"]);
        assert_eq!(c.cooldown_seconds, 1800);
        assert_eq!(cfg("").platforms.len(), 3);
        for bad in [
            r#"{"platforms":[]}"#,
            r#"{"platforms":["myspace"]}"#,
            r#"{"platforms":["twitch","twitch"]}"#,
            r#"{"bogus":1}"#,
            r#"{"template":"{nope}"}"#,
            r#"{"template":"{name"}"#,
            r#"{"cooldown_seconds":5}"#,
            r#"{"max_age_seconds":99999}"#,
            r#"{"announce_targets":[{"platform":"kick","channel_id":"x"}]}"#,
            r#"{"announce_targets":[{"platform":"discord","channel_id":""}]}"#,
        ] {
            assert!(Config::parse(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn clean_text_strips_mentions_and_controls() {
        assert_eq!(
            clean_text("hi @everyone <@123>\n`x`\u{202e}z", 100),
            "hi everyone 123 xz"
        );
        assert_eq!(clean_text("abcdef", 3), "abc");
    }

    #[test]
    fn error_codes_classify_by_prefix() {
        assert_eq!(
            error_code(&ToolError::InvalidArgs("rate_limited: 30s".into())),
            "rate_limited"
        );
        assert_eq!(
            error_code(&ToolError::InvalidArgs("not_connected".into())),
            "not_connected"
        );
        assert_eq!(
            error_code(&ToolError::InvalidArgs("rate_limitedX".into())),
            "contract_violation"
        );
        assert_eq!(error_code(&ToolError::TargetUnavailable), "not_installed");
    }

    #[test]
    fn refresh_aggregates_sorts_and_dedupes_scopes() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![
                chan("t1", "Off", false, 0, None),
                chan("t2", "Small", true, 5, Some(NOW)),
            ]),
        );
        h.script(
            "twitch",
            "linked",
            page(vec![
                chan("t2", "Small", true, 5, Some(NOW)),
                chan("t3", "Big", true, 50, Some(NOW)),
            ]),
        );
        let c = cfg(r#"{"platforms":["twitch"],"include_linked":true}"#);
        let snap = refresh(&h, &c).unwrap();
        let names: Vec<_> = snap.streams.iter().map(|s| s.channel.as_str()).collect();
        assert_eq!(names, vec!["Big", "Small", "Off"]);
        assert!(snap.live);
        assert_eq!(snap.channel.as_deref(), Some("Big"));
        assert_eq!(snap.viewer_count, 50);
        assert!(snap.streams[0].linked);
        assert!(!snap.streams[1].linked);
        assert!(snap.streams[2].since.is_none());
        assert!(
            snap.streams[0]
                .title
                .as_deref()
                .unwrap()
                .find('@')
                .is_none()
        );
        assert!(h.kv.borrow().contains_key("snapshot"));
        // community scope args: contract-shaped, no caller-supplied channel ids
        let calls = h.calls.borrow();
        assert_eq!(calls[0].0, "waddles.core.service.twitch");
        assert_eq!(calls[0].1, "twitch.live.channels");
        assert_eq!(calls[0].2, json!({"scope": "community", "limit": 100}));
    }

    #[test]
    fn refresh_follows_cursor_and_caps_pages() {
        let h = host();
        h.script("twitch", "community", Ok(json!({"channels": [chan("a", "A", true, 1, Some(NOW))], "cursor": "p2", "as_of": NOW})));
        h.script_page(
            "twitch",
            "community",
            "p2",
            page(vec![chan("b", "B", true, 2, Some(NOW))]),
        );
        let c = cfg(r#"{"platforms":["twitch"]}"#);
        assert_eq!(refresh(&h, &c).unwrap().streams.len(), 2);

        let h2 = host();
        h2.script(
            "twitch",
            "community",
            Ok(json!({"channels": [], "cursor": "p2", "as_of": NOW})),
        );
        h2.script_page(
            "twitch",
            "community",
            "p2",
            Ok(json!({"channels": [], "cursor": "p2", "as_of": NOW})),
        );
        assert!(refresh(&h2, &c).unwrap_err().contains("contract_violation"));
    }

    #[test]
    fn one_platform_failing_is_reported_not_hidden_and_all_failing_errors() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![chan("t", "T", true, 1, Some(NOW))]),
        );
        h.script(
            "youtube",
            "community",
            Err(ToolError::InvalidArgs("rate_limited: 60".into())),
        );
        let c = cfg(r#"{"platforms":["twitch","youtube"]}"#);
        let snap = refresh(&h, &c).unwrap();
        assert_eq!(
            snap.errors,
            vec![PlatformError {
                platform: "youtube".into(),
                code: "rate_limited".into()
            }]
        );
        assert!(format_reply(&snap, None).contains("[unavailable: youtube (rate_limited)]"));

        let h2 = host();
        h2.script(
            "twitch",
            "community",
            Err(ToolError::InvalidArgs("not_connected".into())),
        );
        h2.script("youtube", "community", Err(ToolError::TargetUnavailable));
        let err = refresh(&h2, &c).unwrap_err();
        assert!(err.contains("twitch=not_connected") && err.contains("youtube=not_installed"));
    }

    #[test]
    fn malformed_provider_output_is_a_contract_violation() {
        let h = host();
        h.script(
            "twitch",
            "community",
            Ok(json!({"channels": [{"display_name": "x"}], "as_of": NOW})),
        );
        let c = cfg(r#"{"platforms":["twitch"]}"#);
        assert!(
            refresh(&h, &c)
                .unwrap_err()
                .contains("twitch=contract_violation")
        );
    }

    #[test]
    fn announcements_dedupe_per_session_and_respect_cooldown() {
        let h = host();
        let started = "2026-11-07T11:55:00Z";
        h.script(
            "twitch",
            "community",
            page(vec![chan("t1", "Pen", true, 9, Some(started))]),
        );
        let c = announce_cfg();
        let snap = refresh(&h, &c).unwrap();
        let first = announcements(&h, &c, &snap);
        assert_eq!(first.len(), 1);
        assert_eq!(first[0].platform, "discord");
        assert_eq!(first[0].channel, "c1");
        assert_eq!(first[0].text, "Pen is live on twitch: Hello everyone #1");
        // same session again: deduped
        assert!(announcements(&h, &c, &snap).is_empty());
        // new session of the same channel (restart): session is new but cooldown blocks it
        h.script(
            "twitch",
            "community",
            page(vec![chan(
                "t1",
                "Pen",
                true,
                9,
                Some("2026-11-07T11:58:00Z"),
            )]),
        );
        let snap2 = refresh(&h, &c).unwrap();
        assert!(announcements(&h, &c, &snap2).is_empty());
        let log = h.logs.borrow();
        let last = log
            .iter()
            .rev()
            .find(|l| l.1 == "live-now announcements evaluated")
            .unwrap();
        assert_eq!(last.2["cooldown"], 1);
    }

    #[test]
    fn stale_and_invalid_started_at_are_not_announced() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![
                chan("old", "Old", true, 1, Some("2026-11-07T08:00:00Z")),
                chan("bad", "Bad", true, 1, Some("soon")),
                chan("new", "New", true, 1, Some("2026-11-07T11:59:00Z")),
            ]),
        );
        let c = announce_cfg();
        let snap = refresh(&h, &c).unwrap();
        let out = announcements(&h, &c, &snap);
        assert_eq!(out.len(), 1);
        assert!(out[0].text.starts_with("New is live"));
        let log = h.logs.borrow();
        let l = log
            .iter()
            .find(|l| l.1 == "live-now announcements evaluated")
            .unwrap();
        assert_eq!(
            (l.2["stale"].as_u64(), l.2["invalid_started_at"].as_u64()),
            (Some(1), Some(1))
        );
    }

    #[test]
    fn announce_fans_out_to_every_target_and_uses_template() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![chan(
                "t1",
                "Pen",
                true,
                7,
                Some("2026-11-07T11:59:00Z"),
            )]),
        );
        let c = cfg(
            r#"{"platforms":["twitch"],"template":"{name} ({viewers}) {category} {platform}",
                "announce_targets":[{"platform":"discord","channel_id":"a"},{"platform":"twitch","channel_id":"b"}]}"#,
        );
        let snap = refresh(&h, &c).unwrap();
        let out = announcements(&h, &c, &snap);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].text, "Pen (7) Games twitch");
        assert_eq!(out[1].channel, "b");
    }

    #[test]
    fn no_targets_means_no_announcements_and_no_kv_claims() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![chan("t1", "Pen", true, 7, Some(NOW))]),
        );
        let c = cfg(r#"{"platforms":["twitch"]}"#);
        let snap = refresh(&h, &c).unwrap();
        assert!(announcements(&h, &c, &snap).is_empty());
        assert!(!h.kv.borrow().keys().any(|k| k.starts_with("ann.")));
    }

    #[test]
    fn kv_keys_never_contain_colon() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![chan("a:b|c", "Pen", true, 7, Some(NOW))]),
        );
        let c = announce_cfg();
        let snap = refresh(&h, &c).unwrap();
        announcements(&h, &c, &snap);
        assert!(h.kv.borrow().keys().all(|k| !k.contains(':')));
    }

    fn chat(text: &str) -> Value {
        json!({"text": text, "channel_id": "chan-1"})
    }

    #[test]
    fn command_is_flag_gated_default_off() {
        let h = host();
        let c = Ok(cfg(r#"{"platforms":["twitch"]}"#));
        assert!(
            handle_event(&h, &c, "twitch", "chat.message", &chat("!live"))
                .unwrap()
                .is_empty()
        );
        assert!(h.calls.borrow().is_empty());
    }

    #[test]
    fn live_command_replies_to_origin_channel_and_filters() {
        let h = host();
        h.set_flag(FLAG_COMMAND, true);
        h.script(
            "twitch",
            "community",
            page(vec![chan("t1", "Pen", true, 9, Some(NOW))]),
        );
        h.script(
            "youtube",
            "community",
            page(vec![chan("y1", "Tube", true, 3, Some(NOW))]),
        );
        let c = Ok(cfg(r#"{"platforms":["twitch","youtube"]}"#));
        let out = handle_event(&h, &c, "discord", "chat.message", &chat("!LIVE")).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(
            (out[0].platform.as_str(), out[0].channel.as_str()),
            ("discord", "chan-1")
        );
        assert!(out[0].text.starts_with("Live now: Pen (twitch, 9 viewers)"));
        assert!(out[0].text.contains("Tube (youtube, 3 viewers)"));
        let calls_after_first = h.calls.borrow().len();
        // cached within 30s: no further tool calls; platform filter applies
        let out = handle_event(&h, &c, "discord", "chat.message", &chat("!live youtube")).unwrap();
        assert_eq!(h.calls.borrow().len(), calls_after_first);
        assert!(out[0].text.contains("Tube") && !out[0].text.contains("Pen ("));
    }

    #[test]
    fn nobody_live_and_non_commands_are_handled() {
        let h = host();
        h.set_flag(FLAG_COMMAND, true);
        h.script(
            "twitch",
            "community",
            page(vec![chan("t1", "Pen", false, 0, None)]),
        );
        let c = Ok(cfg(r#"{"platforms":["twitch"]}"#));
        let out = handle_event(&h, &c, "twitch", "chat.message", &chat("!live")).unwrap();
        assert_eq!(out[0].text, "Nobody is live right now.");
        for text in [
            "!livex",
            "live",
            "!live a b",
            "!live nonsense",
            "hello !live",
            "",
        ] {
            assert!(
                handle_event(&h, &c, "twitch", "chat.message", &chat(text))
                    .unwrap()
                    .is_empty(),
                "{text}"
            );
        }
        assert!(
            handle_event(&h, &c, "twitch", "chat.message", &json!({"text": 7}))
                .unwrap()
                .is_empty()
        );
        assert!(
            handle_event(&h, &c, "twitch", "channel.follow", &json!({}))
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn command_without_channel_errors_and_bad_config_tells_the_asker() {
        let h = host();
        h.set_flag(FLAG_COMMAND, true);
        let good = Ok(cfg(r#"{"platforms":["twitch"]}"#));
        assert!(
            handle_event(
                &h,
                &good,
                "twitch",
                "chat.message",
                &json!({"text": "!live"})
            )
            .is_err()
        );
        let bad = Config::parse(r#"{"platforms":["x"]}"#);
        let out = handle_event(&h, &bad, "twitch", "chat.message", &chat("!live")).unwrap();
        assert_eq!(out[0].text, "Live status is unavailable right now.");
        assert!(
            h.logs.borrow().iter().any(|l| l.0 == Level::Error
                && l.2["error"].as_str().unwrap().contains("unknown platform"))
        );
    }

    #[test]
    fn stream_online_event_refreshes_and_announces_when_flag_on() {
        let h = host();
        h.script(
            "twitch",
            "community",
            page(vec![chan(
                "t1",
                "Pen",
                true,
                9,
                Some("2026-11-07T11:59:30Z"),
            )]),
        );
        let c = Ok(announce_cfg());
        assert!(
            handle_event(&h, &c, "twitch", "stream.online", &json!({}))
                .unwrap()
                .is_empty()
        );
        assert!(h.calls.borrow().is_empty(), "flag off: no tool calls");
        h.set_flag(FLAG_ANNOUNCE, true);
        let out = handle_event(&h, &c, "twitch", "stream.online", &json!({})).unwrap();
        assert_eq!(out.len(), 1);
        // redelivery of the same event: deduped
        assert!(
            handle_event(&h, &c, "twitch", "stream.online", &json!({}))
                .unwrap()
                .is_empty()
        );
        // offline events refresh without announcing anything new
        assert!(
            handle_event(&h, &c, "twitch", "stream.offline", &json!({}))
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn parse_messages_validates() {
        let ok = r#"{"messages":[{"platform":"discord","channel":"c","text":"hi"}]}"#;
        assert_eq!(parse_messages(ok).unwrap().len(), 1);
        for bad in [
            "{}",
            "not json",
            r#"{"messages":[{"platform":"kick","channel":"c","text":"hi"}]}"#,
            r#"{"messages":[{"platform":"discord","channel":"","text":"hi"}]}"#,
            r#"{"messages":[{"platform":"discord","channel":"c","text":""}]}"#,
        ] {
            assert!(parse_messages(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn reply_is_capped() {
        let streams: Vec<Stream> = (0..9)
            .map(|i| Stream {
                platform: "twitch".into(),
                channel_ref: format!("r{i}"),
                channel: format!("Chan{i}"),
                live: true,
                since: None,
                viewer_count: i,
                title: Some("t".repeat(300)),
                category: None,
                linked: false,
            })
            .collect();
        let snap = Snapshot {
            live: true,
            platform: None,
            channel: None,
            since: None,
            viewer_count: 0,
            streams,
            errors: vec![],
            as_of_ms: 0,
        };
        assert!(format_reply(&snap, None).chars().count() <= MAX_REPLY_CHARS);
    }
}

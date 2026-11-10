//! Community config and the deterministic song policy.
//!
//! Everything in [`evaluate`] is pure and AI-free: provider allowlists
//! (DMCA-safe mode), deny-lists, duration, explicit flag, replay cooldown,
//! duplicate guard and the ban list. The AI judge in `engine` only runs on
//! tracks that survive this layer and only for rules that cannot be
//! expressed deterministically.

use serde::{Deserialize, Serialize};

use crate::model::{DecidedBy, Mode, PROVIDERS, State, Track, clean_text};

const MAX_ENTRIES: usize = 50;
const MAX_ENTRY_CHARS: usize = 80;
const MAX_FUZZY: usize = 3;
const MAX_FUZZY_CHARS: usize = 120;

/// Normalizes text for matching: lowercase, alphanumerics only, single spaces.
pub fn norm(s: &str) -> String {
    let mut out = String::new();
    let mut sp = true;
    for c in s.chars() {
        if c.is_alphanumeric() {
            out.extend(c.to_lowercase());
            sp = false;
        } else if !sp {
            out.push(' ');
            sp = true;
        }
    }
    out.trim().to_string()
}

/// One allowlist entry; every field that is set must match.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceAllow {
    #[serde(default)]
    pub provider: Option<String>,
    #[serde(default)]
    pub channel_id: Option<String>,
    #[serde(default)]
    pub artist: Option<String>,
}

impl SourceAllow {
    fn matches(&self, t: &Track) -> bool {
        if self.provider.is_none() && self.channel_id.is_none() && self.artist.is_none() {
            return false;
        }
        self.provider.as_deref().is_none_or(|p| p == t.provider)
            && self.channel_id.as_deref().is_none_or(|c| {
                t.channel_id
                    .as_deref()
                    .is_some_and(|tc| tc.eq_ignore_ascii_case(c))
            })
            && self
                .artist
                .as_deref()
                .is_none_or(|a| norm(a) == norm(&t.artist))
    }
}

/// Structured rules compiled from the plain-language instructions. Every
/// field is validated and bounded by [`Rules::validate`]; unknown fields are
/// rejected so a model cannot smuggle in extra behaviour.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct Rules {
    pub allow_channels: Vec<String>,
    pub allow_artists: Vec<String>,
    pub allow_providers: Vec<String>,
    pub deny_artists: Vec<String>,
    pub deny_title_terms: Vec<String>,
    pub deny_tracks: Vec<String>,
    pub max_duration_s: Option<u32>,
    pub explicit_allowed: Option<bool>,
    pub genres: Vec<String>,
    /// Fuzzy rules judged per track by `ai.classify` (at most [`MAX_FUZZY`]).
    pub fuzzy: Vec<String>,
}

fn clean_list(v: &mut Vec<String>, max_chars: usize) {
    let mut out: Vec<String> = Vec::new();
    for s in v.drain(..) {
        let c = clean_text(&s, max_chars);
        if c.chars().count() >= 2 && !out.iter().any(|o| norm(o) == norm(&c)) {
            out.push(c);
        }
        if out.len() >= MAX_ENTRIES {
            break;
        }
    }
    *v = out;
}

impl Rules {
    /// Clamps and sanitizes an untrusted rule set in place.
    pub fn validate(mut self) -> Rules {
        clean_list(&mut self.allow_channels, MAX_ENTRY_CHARS);
        clean_list(&mut self.allow_artists, MAX_ENTRY_CHARS);
        clean_list(&mut self.deny_artists, MAX_ENTRY_CHARS);
        clean_list(&mut self.deny_title_terms, MAX_ENTRY_CHARS);
        clean_list(&mut self.deny_tracks, MAX_ENTRY_CHARS);
        clean_list(&mut self.genres, 40);
        self.genres.truncate(10);
        clean_list(&mut self.fuzzy, MAX_FUZZY_CHARS);
        self.fuzzy.truncate(MAX_FUZZY);
        self.allow_providers
            .retain(|p| PROVIDERS.contains(&p.as_str()));
        self.allow_providers.dedup();
        self.max_duration_s = self.max_duration_s.map(|d| d.clamp(30, 7200));
        self
    }

    /// One-line human summary shown to the admin for confirmation.
    pub fn summary(&self) -> String {
        let mut parts = Vec::new();
        let mut add = |label: &str, v: &[String]| {
            if !v.is_empty() {
                parts.push(format!("{label}: {}", v.join(", ")));
            }
        };
        add("only channels", &self.allow_channels);
        add("only artists", &self.allow_artists);
        add("only providers", &self.allow_providers);
        add("never artists", &self.deny_artists);
        add("never title terms", &self.deny_title_terms);
        add("never tracks", &self.deny_tracks);
        add("genres", &self.genres);
        add("judged", &self.fuzzy);
        if let Some(d) = self.max_duration_s {
            parts.push(format!("max {d}s"));
        }
        if self.explicit_allowed == Some(false) {
            parts.push("no explicit".to_string());
        }
        if parts.is_empty() {
            "no rules".to_string()
        } else {
            parts.join(" | ")
        }
    }
}

/// AI DJ settings (off by default).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct AiConfig {
    pub enabled: bool,
    pub genre: String,
    pub instructions: String,
    /// Hard cap on AI-sourced tracks added per rolling hour.
    pub hourly_cap: u32,
    /// Tracks requested from the model per fill.
    pub batch: u32,
}

impl Default for AiConfig {
    fn default() -> Self {
        AiConfig {
            enabled: false,
            genre: String::new(),
            instructions: String::new(),
            hourly_cap: 10,
            batch: 3,
        }
    }
}

/// Per-community configuration, one kv value.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub requests_enabled: bool,
    pub providers: Vec<String>,
    pub max_per_user: u32,
    pub cooldown_s: u32,
    pub max_duration_s: u32,
    pub max_queue: u32,
    pub replay_cooldown_s: u32,
    pub explicit_allowed: bool,
    pub dmca_safe: bool,
    pub approval_required: bool,
    pub auto_remove: bool,
    pub request_cost: u64,
    pub sources_allow: Vec<SourceAllow>,
    pub deny_artists: Vec<String>,
    pub deny_title_terms: Vec<String>,
    pub deny_tracks: Vec<String>,
    pub mode: Mode,
    pub active_playlist: Option<String>,
    pub ai: AiConfig,
    /// Confirmed compiled instruction rules (enforced).
    pub rules: Option<Rules>,
    /// Compiled but not yet confirmed (never enforced).
    pub pending_rules: Option<Rules>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            requests_enabled: true,
            providers: vec!["youtube".to_string()],
            max_per_user: 3,
            cooldown_s: 30,
            max_duration_s: 600,
            max_queue: 50,
            replay_cooldown_s: 3600,
            explicit_allowed: true,
            dmca_safe: false,
            approval_required: false,
            auto_remove: false,
            request_cost: 0,
            sources_allow: Vec::new(),
            deny_artists: Vec::new(),
            deny_title_terms: Vec::new(),
            deny_tracks: Vec::new(),
            mode: Mode::Requests,
            active_playlist: None,
            ai: AiConfig::default(),
            rules: None,
            pending_rules: None,
        }
    }
}

/// Error from a config mutation, shown to the moderator verbatim.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("{0}")]
pub struct ConfigError(pub String);

fn parse_bool(v: &str) -> Result<bool, ConfigError> {
    match v.to_ascii_lowercase().as_str() {
        "on" | "true" | "yes" | "1" => Ok(true),
        "off" | "false" | "no" | "0" => Ok(false),
        _ => Err(ConfigError("expected on|off".to_string())),
    }
}

fn parse_num(v: &str, lo: u64, hi: u64) -> Result<u64, ConfigError> {
    let n: u64 = v
        .parse()
        .map_err(|_| ConfigError("expected a number".to_string()))?;
    if (lo..=hi).contains(&n) {
        Ok(n)
    } else {
        Err(ConfigError(format!("must be between {lo} and {hi}")))
    }
}

impl Config {
    /// Sets one scalar setting by key; bounds-checked, nothing is applied on error.
    pub fn set(&mut self, key: &str, value: &str) -> Result<(), ConfigError> {
        match key {
            "requests" => self.requests_enabled = parse_bool(value)?,
            "explicit" => self.explicit_allowed = parse_bool(value)?,
            "dmca_safe" => self.dmca_safe = parse_bool(value)?,
            "approval" => self.approval_required = parse_bool(value)?,
            "auto_remove" => self.auto_remove = parse_bool(value)?,
            "max_per_user" => self.max_per_user = parse_num(value, 1, 20)? as u32,
            "cooldown_s" => self.cooldown_s = parse_num(value, 0, 3600)? as u32,
            "max_duration_s" => self.max_duration_s = parse_num(value, 30, 7200)? as u32,
            "max_queue" => {
                self.max_queue = parse_num(value, 1, crate::model::MAX_QUEUE_HARD as u64)? as u32
            }
            "replay_cooldown_s" => self.replay_cooldown_s = parse_num(value, 0, 86_400)? as u32,
            "cost" => self.request_cost = parse_num(value, 0, 1_000_000)?,
            "ai_hourly_cap" => self.ai.hourly_cap = parse_num(value, 0, 60)? as u32,
            "ai_batch" => self.ai.batch = parse_num(value, 1, 5)? as u32,
            "providers" => {
                let list: Vec<String> = value
                    .split(',')
                    .map(|p| p.trim().to_ascii_lowercase())
                    .collect();
                if list.is_empty() || list.iter().any(|p| !PROVIDERS.contains(&p.as_str())) {
                    return Err(ConfigError(format!(
                        "providers must be a comma list of {}",
                        PROVIDERS.join("|")
                    )));
                }
                self.providers = list;
            }
            _ => {
                return Err(ConfigError(format!(
                    "unknown setting '{}'",
                    clean_text(key, 32)
                )));
            }
        }
        Ok(())
    }

    /// Adds/removes an entry on one of the list settings.
    pub fn edit_list(&mut self, list: &str, add: bool, value: &str) -> Result<(), ConfigError> {
        let v = clean_text(value, MAX_ENTRY_CHARS);
        if v.chars().count() < 2 {
            return Err(ConfigError("value too short".to_string()));
        }
        let strings = match list {
            "deny-artist" => &mut self.deny_artists,
            "deny-term" => &mut self.deny_title_terms,
            "deny-track" => &mut self.deny_tracks,
            "allow-channel" | "allow-artist" => {
                let entry = if list == "allow-channel" {
                    SourceAllow {
                        channel_id: Some(v),
                        ..SourceAllow::default()
                    }
                } else {
                    SourceAllow {
                        artist: Some(v),
                        ..SourceAllow::default()
                    }
                };
                if add {
                    if self.sources_allow.len() >= MAX_ENTRIES {
                        return Err(ConfigError("allowlist full".to_string()));
                    }
                    if !self.sources_allow.iter().any(|e| same_entry(e, &entry)) {
                        self.sources_allow.push(entry);
                    }
                } else {
                    self.sources_allow.retain(|e| !same_entry(e, &entry));
                }
                return Ok(());
            }
            _ => {
                return Err(ConfigError(
                    "list must be allow-channel|allow-artist|deny-artist|deny-term|deny-track"
                        .to_string(),
                ));
            }
        };
        if add {
            if strings.len() >= MAX_ENTRIES {
                return Err(ConfigError("list full".to_string()));
            }
            if !strings.iter().any(|s| norm(s) == norm(&v)) {
                strings.push(v);
            }
        } else {
            strings.retain(|s| norm(s) != norm(&v));
        }
        Ok(())
    }
}

/// Case/punctuation-insensitive equality of two allowlist entries.
fn same_entry(a: &SourceAllow, b: &SourceAllow) -> bool {
    let n = |o: &Option<String>| o.as_deref().map(norm);
    n(&a.channel_id) == n(&b.channel_id) && n(&a.artist) == n(&b.artist) && a.provider == b.provider
}

/// Verdict of the deterministic layer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Violation {
    /// Stable rule id (audit + chat reason).
    pub rule: &'static str,
    pub reason: String,
    pub decided_by: DecidedBy,
}

fn v(rule: &'static str, reason: impl Into<String>) -> Option<Violation> {
    Some(Violation {
        rule,
        reason: reason.into(),
        decided_by: DecidedBy::Deterministic,
    })
}

fn contains_term(haystack_norm: &str, term: &str) -> bool {
    let t = norm(term);
    t.len() >= 2 && haystack_norm.contains(&t)
}

/// Runs the deterministic policy against `track`. `None` = passes every rule.
/// `skip_dup_id` lets a restore/approve ignore the queue entry being acted on.
pub fn evaluate(
    cfg: &Config,
    state: &State,
    track: &Track,
    now_ms: u64,
    skip_dup_id: Option<u64>,
) -> Option<Violation> {
    let default_rules = Rules::default();
    let rules = cfg.rules.as_ref().unwrap_or(&default_rules);

    if state.banned.contains(&track.key()) {
        return v("banned_track", "that song is banned here");
    }
    if !cfg.providers.contains(&track.provider) {
        return v(
            "provider_disabled",
            format!("{} is not enabled here", track.provider),
        );
    }
    if !rules.allow_providers.is_empty() && !rules.allow_providers.contains(&track.provider) {
        return v(
            "provider_not_allowed",
            format!("only {} are allowed", rules.allow_providers.join(", ")),
        );
    }

    let rule_allow = |t: &Track| {
        rules.allow_channels.iter().any(|c| {
            t.channel_id
                .as_deref()
                .is_some_and(|tc| tc.eq_ignore_ascii_case(c))
        }) || rules
            .allow_artists
            .iter()
            .any(|a| norm(a) == norm(&t.artist))
    };
    let has_rule_allow = !rules.allow_channels.is_empty() || !rules.allow_artists.is_empty();
    if cfg.dmca_safe && cfg.sources_allow.is_empty() && !has_rule_allow {
        return v(
            "dmca_no_allowlist",
            "DMCA-safe mode is on but no allowed sources are configured",
        );
    }
    if cfg.dmca_safe || !cfg.sources_allow.is_empty() || has_rule_allow {
        let ok = cfg.sources_allow.iter().any(|e| e.matches(track)) || rule_allow(track);
        if !ok {
            return v(
                "source_not_allowed",
                "that source is not on the allowed list",
            );
        }
    }

    let hay = norm(&format!("{} {}", track.title, track.artist));
    let title = norm(&track.title);
    let artist = norm(&track.artist);
    for d in cfg.deny_artists.iter().chain(rules.deny_artists.iter()) {
        if contains_term(&artist, d) {
            return v("deny_artist", "that artist is not allowed here");
        }
    }
    for d in cfg
        .deny_title_terms
        .iter()
        .chain(rules.deny_title_terms.iter())
    {
        if contains_term(&title, d) {
            return v("deny_title_term", "that title is not allowed here");
        }
    }
    for d in cfg.deny_tracks.iter().chain(rules.deny_tracks.iter()) {
        if d == &track.key() || contains_term(&hay, d) || contains_term(&title, d) {
            return v("deny_track", "that song is not allowed here");
        }
    }

    let max = rules
        .max_duration_s
        .map_or(cfg.max_duration_s, |r| r.min(cfg.max_duration_s));
    if track.duration_s == 0 || track.duration_s > max {
        return v(
            "duration",
            format!("songs must be at most {}:{:02} long", max / 60, max % 60),
        );
    }
    let no_explicit = !cfg.explicit_allowed || rules.explicit_allowed == Some(false);
    if no_explicit && track.explicit == Some(true) {
        return v("explicit", "explicit songs are not allowed here");
    }
    if no_explicit && track.explicit.is_none() {
        return v(
            "explicit_unknown",
            "the provider cannot confirm this song is clean",
        );
    }
    let key = track.key();
    let cool_ms = u64::from(cfg.replay_cooldown_s) * 1000;
    if cool_ms > 0
        && state
            .history
            .iter()
            .any(|p| p.key == key && now_ms.saturating_sub(p.ms) < cool_ms)
    {
        return v("replay_cooldown", "that song was played recently");
    }
    let in_queue = state
        .queue
        .iter()
        .any(|i| i.track.key() == key && Some(i.id) != skip_dup_id);
    let is_now = state.now.as_ref().is_some_and(|i| i.track.key() == key);
    if in_queue || is_now {
        return v("duplicate", "that song is already in the queue");
    }
    None
}

/// The fuzzy rules to AI-judge (confirmed rules only; empty = no AI judge).
pub fn fuzzy_rules(cfg: &Config) -> Vec<String> {
    cfg.rules
        .as_ref()
        .map_or_else(Vec::new, |r| r.fuzzy.clone())
}

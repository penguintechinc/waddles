//! Per-community teammate configuration: hub-managed `config-json` (validated by hub-api
//! against the manifest `config_schema`) overlaid with moderator runtime overrides kept in
//! the bundle's own community-scoped kv. Every teammate is OFF by default. An invalid
//! configuration fails CLOSED (teammate disabled, reason surfaced), never silently defaulted.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// The three teammates (closed set; also the `!teammate` sub-module names).
pub const TEAMMATES: [&str; 3] = ["researcher", "receptionist", "producer"];

/// Configuration validation failure; the message is safe to show to a moderator.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

fn bad<T>(msg: impl Into<String>) -> Result<T, ConfigError> {
    Err(ConfigError(msg.into()))
}

fn t() -> bool {
    true
}

/// Researcher: answers from the community knowledge base, optionally general questions.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct ResearcherCfg {
    pub enabled: bool,
    pub allow_general: bool,
    pub user_cooldown_s: u32,
    pub community_per_min: u32,
    pub max_answer_chars: usize,
    pub collection: Option<String>,
}

impl Default for ResearcherCfg {
    fn default() -> Self {
        Self {
            enabled: false,
            allow_general: true,
            user_cooldown_s: 30,
            community_per_min: 10,
            max_answer_chars: 300,
            collection: None,
        }
    }
}

/// Receptionist: greets new members, optional AI personalization, onboarding Q&A.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct ReceptionistCfg {
    pub enabled: bool,
    pub templates: Vec<String>,
    pub ai_personalize: bool,
    pub welcome_channel_id: Option<String>,
    #[serde(default = "t")]
    pub greet_on_join: bool,
    #[serde(default = "t")]
    pub greet_on_first_message: bool,
    pub onboarding_collection: String,
    pub max_greeting_chars: usize,
}

impl Default for ReceptionistCfg {
    fn default() -> Self {
        Self {
            enabled: false,
            templates: vec!["Welcome to the community, {user}!".to_string()],
            ai_personalize: false,
            welcome_channel_id: None,
            greet_on_join: true,
            greet_on_first_message: true,
            onboarding_collection: "onboarding".to_string(),
            max_greeting_chars: 240,
        }
    }
}

/// One channel the Producer may post into.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ChannelRef {
    pub platform: String,
    pub channel_id: String,
}

/// Local-time window (hours, `[start, end)`, wraps past midnight) during which nothing posts.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QuietHours {
    pub start_hour: u8,
    pub end_hour: u8,
    #[serde(default)]
    pub utc_offset_minutes: i32,
}

/// Producer: posts a short conversation starter when a live chat has gone quiet.
#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProducerCfg {
    pub enabled: bool,
    pub channels: Vec<ChannelRef>,
    pub quiet_minutes: u32,
    pub min_interval_minutes: u32,
    pub max_per_hour: u32,
    pub quiet_hours: Option<QuietHours>,
    pub topics: Vec<String>,
    pub blocklist: Vec<String>,
    pub max_chars: usize,
}

impl Default for ProducerCfg {
    fn default() -> Self {
        Self {
            enabled: false,
            channels: Vec::new(),
            quiet_minutes: 10,
            min_interval_minutes: 20,
            max_per_hour: 3,
            quiet_hours: None,
            topics: Vec::new(),
            blocklist: Vec::new(),
            max_chars: 200,
        }
    }
}

/// Producer hard bounds (the floor and ceiling cannot be configured away).
pub const PRODUCER_MIN_INTERVAL_FLOOR: u32 = 5;
pub const PRODUCER_MAX_PER_HOUR_CEIL: u32 = 6;

fn id_ok(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 100
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
}

impl ResearcherCfg {
    /// Validates ranges.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if !(0..=3600).contains(&self.user_cooldown_s) {
            return bad("researcher.user_cooldown_s must be 0-3600");
        }
        if !(1..=120).contains(&self.community_per_min) {
            return bad("researcher.community_per_min must be 1-120");
        }
        if !(40..=400).contains(&self.max_answer_chars) {
            return bad("researcher.max_answer_chars must be 40-400");
        }
        if self.collection.as_deref().is_some_and(|c| !id_ok(c)) {
            return bad("researcher.collection must be an identifier");
        }
        Ok(())
    }
}

impl ReceptionistCfg {
    /// Validates ranges and template shape (only the `{user}` placeholder may use braces).
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.templates.is_empty() || self.templates.len() > 20 {
            return bad("receptionist.templates must hold 1-20 entries");
        }
        for tpl in &self.templates {
            if tpl.trim().is_empty() || tpl.chars().count() > 300 {
                return bad("receptionist.templates entries must be 1-300 chars");
            }
            if tpl.replace("{user}", "").contains(['{', '}', '\\']) {
                return bad("receptionist.templates may only use the {user} placeholder");
            }
        }
        if self
            .welcome_channel_id
            .as_deref()
            .is_some_and(|c| c.is_empty() || c.len() > 100)
        {
            return bad("receptionist.welcome_channel_id must be 1-100 chars");
        }
        if !id_ok(&self.onboarding_collection) {
            return bad("receptionist.onboarding_collection must be an identifier");
        }
        if !(40..=400).contains(&self.max_greeting_chars) {
            return bad("receptionist.max_greeting_chars must be 40-400");
        }
        Ok(())
    }
}

impl ProducerCfg {
    /// Validates ranges; the interval floor and hourly ceiling are hard limits.
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.enabled && self.channels.is_empty() {
            return bad("producer.channels must list at least one channel to enable the Producer");
        }
        if self.channels.len() > 10 {
            return bad("producer.channels holds at most 10 entries");
        }
        for c in &self.channels {
            if !matches!(c.platform.as_str(), "twitch" | "discord" | "youtube") {
                return bad("producer.channels[].platform must be twitch, discord or youtube");
            }
            if c.channel_id.is_empty() || c.channel_id.len() > 100 {
                return bad("producer.channels[].channel_id must be 1-100 chars");
            }
        }
        if !(1..=240).contains(&self.quiet_minutes) {
            return bad("producer.quiet_minutes must be 1-240");
        }
        if !(PRODUCER_MIN_INTERVAL_FLOOR..=240).contains(&self.min_interval_minutes) {
            return bad(format!(
                "producer.min_interval_minutes must be {PRODUCER_MIN_INTERVAL_FLOOR}-240"
            ));
        }
        if !(1..=PRODUCER_MAX_PER_HOUR_CEIL).contains(&self.max_per_hour) {
            return bad(format!(
                "producer.max_per_hour must be 1-{PRODUCER_MAX_PER_HOUR_CEIL}"
            ));
        }
        if let Some(q) = self.quiet_hours
            && (q.start_hour > 23
                || q.end_hour > 23
                || q.start_hour == q.end_hour
                || q.utc_offset_minutes.abs() > 14 * 60)
        {
            return bad(
                "producer.quiet_hours needs distinct start/end hours 0-23 and |utc_offset_minutes| <= 840",
            );
        }
        if self.topics.len() > 20
            || self
                .topics
                .iter()
                .any(|t| t.is_empty() || t.chars().count() > 120)
        {
            return bad("producer.topics holds at most 20 entries of 1-120 chars");
        }
        if self.blocklist.len() > 200 || self.blocklist.iter().any(|w| w.chars().count() > 60) {
            return bad("producer.blocklist holds at most 200 entries of up to 60 chars");
        }
        if !(40..=300).contains(&self.max_chars) {
            return bad("producer.max_chars must be 40-300");
        }
        Ok(())
    }
}

/// Effective configuration of the whole bundle. A teammate whose section failed validation
/// carries its error and is treated as disabled.
#[derive(Debug, Clone, PartialEq)]
pub struct Config {
    pub researcher: Result<ResearcherCfg, ConfigError>,
    pub receptionist: Result<ReceptionistCfg, ConfigError>,
    pub producer: Result<ProducerCfg, ConfigError>,
}

fn section<T: for<'de> Deserialize<'de> + Default>(
    base: &Map<String, Value>,
    over: Option<&Map<String, Value>>,
    name: &str,
    validate: impl Fn(&T) -> Result<(), ConfigError>,
) -> Result<T, ConfigError> {
    let mut merged = match base.get(name) {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(m)) => m.clone(),
        Some(_) => return bad(format!("{name} must be an object")),
    };
    if let Some(over) = over {
        for (k, v) in over {
            merged.insert(k.clone(), v.clone());
        }
    }
    let cfg: T = serde_json::from_value(Value::Object(merged))
        .map_err(|e| ConfigError(format!("{name}: {e}")))?;
    validate(&cfg)?;
    Ok(cfg)
}

/// Builds the effective [`Config`]. `config_json` is the host-resolved 3-tier config (empty
/// string is treated as `{}`); `overrides` maps teammate name -> JSON object of runtime
/// overrides. A config that is not a JSON object is an error for ALL teammates.
pub fn effective(config_json: &str, overrides: &Map<String, Value>) -> Config {
    let parsed: Result<Map<String, Value>, ConfigError> = if config_json.trim().is_empty() {
        Ok(Map::new())
    } else {
        match serde_json::from_str::<Value>(config_json) {
            Ok(Value::Object(m)) => Ok(m),
            Ok(_) => bad("config must be a JSON object"),
            Err(e) => bad(format!("config is not valid JSON: {e}")),
        }
    };
    let over = |n: &str| overrides.get(n).and_then(Value::as_object);
    match parsed {
        Err(e) => Config {
            researcher: Err(e.clone()),
            receptionist: Err(e.clone()),
            producer: Err(e),
        },
        Ok(base) => Config {
            researcher: section(
                &base,
                over("researcher"),
                "researcher",
                ResearcherCfg::validate,
            ),
            receptionist: section(
                &base,
                over("receptionist"),
                "receptionist",
                ReceptionistCfg::validate,
            ),
            producer: section(&base, over("producer"), "producer", ProducerCfg::validate),
        },
    }
}

/// A runtime-settable key and its value type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Bool,
    Int,
    Text,
}

/// The keys a moderator may change from chat (lists stay hub-managed).
pub fn settable(teammate: &str, key: &str) -> Option<Kind> {
    match (teammate, key) {
        ("researcher", "allow_general") | ("receptionist", "ai_personalize") => Some(Kind::Bool),
        ("receptionist", "greet_on_join") | ("receptionist", "greet_on_first_message") => {
            Some(Kind::Bool)
        }
        ("researcher", "user_cooldown_s" | "community_per_min" | "max_answer_chars") => {
            Some(Kind::Int)
        }
        ("producer", "quiet_minutes" | "min_interval_minutes" | "max_per_hour") => Some(Kind::Int),
        ("receptionist", "welcome_channel_id") => Some(Kind::Text),
        _ => None,
    }
}

/// Parses a chat-supplied override value into JSON of the key's type.
pub fn parse_value(kind: Kind, raw: &str) -> Result<Value, ConfigError> {
    let raw = raw.trim();
    match kind {
        Kind::Bool => match raw.to_ascii_lowercase().as_str() {
            "true" | "on" | "yes" | "1" => Ok(Value::Bool(true)),
            "false" | "off" | "no" | "0" => Ok(Value::Bool(false)),
            _ => bad("expected on/off"),
        },
        Kind::Int => raw
            .parse::<u32>()
            .map(Value::from)
            .or_else(|_| bad("expected a whole number")),
        Kind::Text if !raw.is_empty() && raw.len() <= 100 && !raw.contains(char::is_whitespace) => {
            Ok(Value::String(raw.to_string()))
        }
        Kind::Text => bad("expected a single token up to 100 chars"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn none() -> Map<String, Value> {
        Map::new()
    }

    #[test]
    fn everything_defaults_off() {
        for json in ["", "{}"] {
            let c = effective(json, &none());
            assert!(!c.researcher.unwrap().enabled);
            assert!(!c.receptionist.unwrap().enabled);
            assert!(!c.producer.unwrap().enabled);
        }
    }

    #[test]
    fn unknown_keys_and_bad_ranges_fail_closed_per_teammate() {
        let c = effective(
            r#"{"researcher":{"enabled":true,"bogus":1},"producer":{"max_per_hour":99}}"#,
            &none(),
        );
        assert!(c.researcher.is_err());
        assert!(c.producer.is_err());
        assert!(c.receptionist.is_ok());
        assert!(effective("[]", &none()).researcher.is_err());
        assert!(effective("{not json", &none()).producer.is_err());
    }

    #[test]
    fn producer_hard_limits_cannot_be_configured_away() {
        let ch = r#""channels":[{"platform":"twitch","channel_id":"c1"}]"#;
        for bad in [
            r#""min_interval_minutes":4"#,
            r#""max_per_hour":7"#,
            r#""max_per_hour":0"#,
            r#""quiet_minutes":0"#,
        ] {
            let c = effective(
                &format!(r#"{{"producer":{{"enabled":true,{ch},{bad}}}}}"#),
                &none(),
            );
            assert!(c.producer.is_err(), "{bad}");
        }
        let ok = effective(
            &format!(
                r#"{{"producer":{{"enabled":true,{ch},"min_interval_minutes":5,"max_per_hour":6}}}}"#
            ),
            &none(),
        );
        assert!(ok.producer.unwrap().enabled);
        // enabling with no channel is refused
        assert!(
            effective(r#"{"producer":{"enabled":true}}"#, &none())
                .producer
                .is_err()
        );
    }

    #[test]
    fn templates_only_allow_the_user_placeholder() {
        for t in ["hi {user:x}", "hi {x}", "a \\ b", "{"] {
            let c = effective(
                &serde_json::json!({"receptionist": {"templates": [t]}}).to_string(),
                &none(),
            );
            assert!(c.receptionist.is_err(), "{t}");
        }
        let c = effective(
            r#"{"receptionist":{"templates":["hello {user}!"]}}"#,
            &none(),
        );
        assert!(c.receptionist.is_ok());
    }

    #[test]
    fn overrides_win_over_hub_config_and_are_validated() {
        let mut o = Map::new();
        o.insert(
            "researcher".into(),
            serde_json::json!({"enabled": true, "user_cooldown_s": 5}),
        );
        let c = effective(
            r#"{"researcher":{"enabled":false,"user_cooldown_s":60}}"#,
            &o,
        )
        .researcher
        .unwrap();
        assert!(c.enabled);
        assert_eq!(c.user_cooldown_s, 5);
        o.insert(
            "researcher".into(),
            serde_json::json!({"user_cooldown_s": 999999}),
        );
        assert!(effective("{}", &o).researcher.is_err());
    }

    #[test]
    fn settable_keys_and_value_parsing() {
        assert_eq!(settable("producer", "max_per_hour"), Some(Kind::Int));
        assert_eq!(settable("producer", "blocklist"), None);
        assert_eq!(settable("researcher", "collection"), None);
        assert_eq!(parse_value(Kind::Bool, "ON").unwrap(), Value::Bool(true));
        assert!(parse_value(Kind::Bool, "maybe").is_err());
        assert!(parse_value(Kind::Int, "-1").is_err());
        assert_eq!(parse_value(Kind::Int, " 7 ").unwrap(), Value::from(7));
        assert!(parse_value(Kind::Text, "a b").is_err());
        assert!(parse_value(Kind::Text, "").is_err());
    }

    #[test]
    fn quiet_hours_validation() {
        let ch = r#""channels":[{"platform":"discord","channel_id":"c"}]"#;
        let mk = |q: &str| {
            effective(
                &format!(r#"{{"producer":{{{ch},"quiet_hours":{q}}}}}"#),
                &none(),
            )
            .producer
        };
        assert!(mk(r#"{"start_hour":22,"end_hour":6,"utc_offset_minutes":-300}"#).is_ok());
        assert!(mk(r#"{"start_hour":5,"end_hour":5}"#).is_err());
        assert!(mk(r#"{"start_hour":24,"end_hour":5}"#).is_err());
    }
}

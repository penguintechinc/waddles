//! Synthetic canary: configuration and the pure matching rules shared by the three services.
//!
//! The canary drives a harmless registered command through the REAL in-cluster path, and its
//! "outbound" lands in a sink that records latency instead of calling Discord:
//!
//! ```text
//! svc-ingest canary driver --publish_event(synthetic discord chat.message, source = canary guild)-->
//!   ingest stream -> process consumer group -> action stream -> executor (the REAL bundle)
//!   -> relay chat.send to the origin channel (the SENTINEL canary channel id)
//!   -> svc-ingest outbound drain: [canary sink: matches tenant+community+channel exactly]
//!        acks "ok" WITHOUT calling Discord
//!   -> svc-action observes the ack: records waddles_canary_e2e_seconds, SETs the result key
//! svc-ingest driver polls the result key; > 5 s => ERROR + waddles_canary_failures_total{timeout}
//! ```
//!
//! Safety properties (each tested): the sink matches ONLY the exact configured
//! `(tenant, community, sentinel channel)` triple, so it can never swallow a real community's send;
//! the canary never posts to Discord, never uses a real user account or token, and is OFF unless the
//! flag `waddles.command-latency-canary` is ON AND every config variable is present and valid
//! (otherwise it fails LOUD instead of silently doing nothing).

use std::time::Duration;

/// PostHog flag key; default OFF.
pub const FLAG_KEY: &str = "waddles.command-latency-canary";
/// Docker ENV baseline for [`FLAG_KEY`].
pub const ENV_BASELINE: &str = "FLAG_WADDLES_COMMAND_LATENCY_CANARY";
/// Valkey key the action stage writes the canary result into (read by the driver).
pub const RESULT_KEY: &str = "waddles:canary:latency:result";
/// Valkey key guarding one-driver-per-minute across ingest replicas (`SET NX EX`).
pub const LOCK_KEY: &str = "waddles:canary:latency:lock";
/// How often the driver fires.
pub const INTERVAL: Duration = Duration::from_secs(60);
/// A canary slower than this (or never confirmed) is a loud failure.
pub const TIMEOUT: Duration = Duration::from_secs(5);
/// TTL of the result key, seconds.
pub const RESULT_TTL_SECS: u64 = 30;

/// The owner-provisioned canary identity. All fields are required; none is a secret.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CanaryConfig {
    /// Numeric `tenants.id` of the DEDICATED canary tenant.
    pub tenant_id: i64,
    /// Numeric `communities.id` of the dedicated canary community (paired to the sentinel guild).
    pub community_id: i64,
    /// Sentinel Discord guild snowflake the synthetic event claims as its source. Never a real guild.
    pub guild_id: String,
    /// Sentinel Discord channel snowflake: the canary sink target. Never a real channel.
    pub channel_id: String,
    /// The harmless registered command text to drive, e.g. `!ping`.
    pub command_text: String,
}

fn is_snowflake(s: &str) -> bool {
    (1..=20).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}

/// Why the canary cannot run (reported as an ERROR + `misconfigured` failure, never silently).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl CanaryConfig {
    /// Builds the config from `CANARY_TENANT_ID`, `CANARY_COMMUNITY_ID`, `CANARY_GUILD_ID`,
    /// `CANARY_CHANNEL_ID` and `CANARY_COMMAND_TEXT` (default `!ping`).
    pub fn from_env() -> Result<Self, ConfigError> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// The process-wide config, parsed from the environment once; `None` when it is absent or
    /// invalid (the canary classifier then simply never matches).
    pub fn from_env_cached() -> Option<&'static CanaryConfig> {
        static CFG: std::sync::OnceLock<Option<CanaryConfig>> = std::sync::OnceLock::new();
        CFG.get_or_init(|| Self::from_env().ok()).as_ref()
    }

    /// [`Self::from_env`] over an injectable lookup (tests).
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Result<Self, ConfigError> {
        let req = |k: &str| {
            get(k)
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
                .ok_or_else(|| ConfigError(format!("{k} is not set")))
        };
        let id = |k: &str| -> Result<i64, ConfigError> {
            req(k)?
                .parse::<i64>()
                .ok()
                .filter(|n| *n > 0)
                .ok_or_else(|| ConfigError(format!("{k} must be a positive integer")))
        };
        let snow = |k: &str| -> Result<String, ConfigError> {
            let v = req(k)?;
            if is_snowflake(&v) {
                Ok(v)
            } else {
                Err(ConfigError(format!("{k} must be a numeric snowflake")))
            }
        };
        let cfg = Self {
            tenant_id: id("CANARY_TENANT_ID")?,
            community_id: id("CANARY_COMMUNITY_ID")?,
            guild_id: snow("CANARY_GUILD_ID")?,
            channel_id: snow("CANARY_CHANNEL_ID")?,
            command_text: get("CANARY_COMMAND_TEXT")
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| "!ping".to_string()),
        };
        if cfg.command_text.len() > 64 || cfg.command_text.chars().any(char::is_control) {
            return Err(ConfigError(
                "CANARY_COMMAND_TEXT is not a short plain command".into(),
            ));
        }
        Ok(cfg)
    }

    /// `true` when an outbound `chat.send` is exactly the canary's: same numeric tenant, same
    /// numeric community, same sentinel channel. This is the ONLY condition under which the drain
    /// skips the platform call; any other value (including an absent stamp) returns `false`.
    pub fn is_sink_target(
        &self,
        tenant_id: Option<i64>,
        community_id: Option<i64>,
        channel: &str,
    ) -> bool {
        tenant_id == Some(self.tenant_id)
            && community_id == Some(self.community_id)
            && channel == self.channel_id
    }

    /// `true` when an invoke's origin matches the canary's sentinel channel and numeric scope (the
    /// action stage's test for "record this as a canary sample, not a user command").
    pub fn is_canary_origin(
        &self,
        tenant_id: i32,
        community_id: i32,
        channel: Option<&str>,
    ) -> bool {
        i64::from(tenant_id) == self.tenant_id
            && i64::from(community_id) == self.community_id
            && channel == Some(self.channel_id.as_str())
    }
}

/// `true` when the canary ENV baseline flag is ON (`1/true/on/yes`); unset = OFF (default).
pub fn env_baseline_enabled() -> bool {
    std::env::var(ENV_BASELINE).is_ok_and(|raw| {
        matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "on" | "yes"
        )
    })
}

/// The result value the action stage stores: `<outcome>:<e2e_ms>`.
pub fn render_result(outcome: &str, e2e_ms: u64) -> String {
    format!("{outcome}:{e2e_ms}")
}

/// Parses [`render_result`]'s output.
pub fn parse_result(raw: &str) -> Option<(String, u64)> {
    let (outcome, ms) = raw.split_once(':')?;
    if !matches!(outcome, "ok" | "refused" | "timeout" | "error") {
        return None;
    }
    Some((outcome.to_string(), ms.parse().ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let m: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect();
        move |k| m.get(k).cloned()
    }

    fn good() -> CanaryConfig {
        CanaryConfig::from_lookup(lookup(&[
            ("CANARY_TENANT_ID", "900"),
            ("CANARY_COMMUNITY_ID", "901"),
            ("CANARY_GUILD_ID", "1000000000000000001"),
            ("CANARY_CHANNEL_ID", "1000000000000000002"),
        ]))
        .unwrap()
    }

    #[test]
    fn config_requires_every_variable_and_validates_shape() {
        let c = good();
        assert_eq!(c.command_text, "!ping");
        for (k, v) in [
            ("CANARY_TENANT_ID", "0"),
            ("CANARY_TENANT_ID", "abc"),
            ("CANARY_COMMUNITY_ID", "-1"),
            ("CANARY_GUILD_ID", "not-a-snowflake"),
            ("CANARY_CHANNEL_ID", ""),
        ] {
            let mut pairs = vec![
                ("CANARY_TENANT_ID", "900"),
                ("CANARY_COMMUNITY_ID", "901"),
                ("CANARY_GUILD_ID", "1000000000000000001"),
                ("CANARY_CHANNEL_ID", "1000000000000000002"),
            ];
            pairs.retain(|(pk, _)| *pk != k);
            pairs.push((k, v));
            assert!(
                CanaryConfig::from_lookup(lookup(&pairs)).is_err(),
                "{k}={v}"
            );
        }
        assert!(CanaryConfig::from_lookup(lookup(&[])).is_err());
    }

    #[test]
    fn sink_matches_only_the_exact_triple() {
        let c = good();
        let ch = "1000000000000000002";
        assert!(c.is_sink_target(Some(900), Some(901), ch));
        assert!(!c.is_sink_target(Some(900), Some(901), "1000000000000000003"));
        assert!(!c.is_sink_target(Some(1), Some(901), ch));
        assert!(!c.is_sink_target(Some(900), Some(2), ch));
        assert!(
            !c.is_sink_target(None, Some(901), ch),
            "absent stamp never matches"
        );
        assert!(!c.is_sink_target(Some(900), None, ch));
    }

    #[test]
    fn canary_origin_uses_the_same_triple() {
        let c = good();
        assert!(c.is_canary_origin(900, 901, Some("1000000000000000002")));
        assert!(!c.is_canary_origin(900, 901, None));
        assert!(!c.is_canary_origin(900, 902, Some("1000000000000000002")));
    }

    #[test]
    fn result_value_round_trips_and_rejects_garbage() {
        assert_eq!(
            parse_result(&render_result("ok", 412)),
            Some(("ok".into(), 412))
        );
        assert_eq!(parse_result("weird:5"), None);
        assert_eq!(parse_result("ok:x"), None);
        assert_eq!(parse_result("ok"), None);
    }
}

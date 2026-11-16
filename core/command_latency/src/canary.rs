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
//! the canary never uses a real user account or token, and is OFF unless the flag
//! `waddles.command-latency-canary` is ON AND every config variable is present and valid (otherwise it
//! fails LOUD instead of silently doing nothing).
//!
//! Two egress modes ([`SinkMode`], `CANARY_SINK_MODE`):
//! - `post` (default, the dedicated-testing-guild setup): the reply is REALLY posted to the dedicated
//!   canary channel of the testing guild, so the measurement includes the Discord API ack. Rate-limit
//!   safe: one message per [`DEFAULT_INTERVAL`] (>= [`MIN_INTERVAL`]) with jitter, one in flight (a
//!   Valkey lock + a sequential driver), and failure backoff.
//! - `swallow`: the legacy sink; the drain acks the exact canary triple WITHOUT calling Discord.
//!
//! Hard guard: the canary runs ONLY in the alpha environment ([`ALLOWED_ENVIRONMENT`]). A config
//! without `CANARY_ENVIRONMENT=alpha`, or contradicting `WADDLES_ENVIRONMENT`/`SPIFFE_ID`, is rejected,
//! so a stray values file can never start a canary against beta/gamma/production.

use std::time::Duration;

/// PostHog flag key; default OFF.
pub const FLAG_KEY: &str = "waddles.command-latency-canary";
/// Docker ENV baseline for [`FLAG_KEY`].
pub const ENV_BASELINE: &str = "FLAG_WADDLES_COMMAND_LATENCY_CANARY";
/// Valkey key the action stage writes the canary result into (read by the driver).
pub const RESULT_KEY: &str = "waddles:canary:latency:result";
/// Valkey key guarding one-driver-per-minute across ingest replicas (`SET NX EX`).
pub const LOCK_KEY: &str = "waddles:canary:latency:lock";
/// Default driver cadence (one real Discord message per interval in `post` mode).
pub const DEFAULT_INTERVAL: Duration = Duration::from_secs(60);
/// Floor for `CANARY_INTERVAL_SECS`: Discord channel rate limits are never approached below this.
pub const MIN_INTERVAL: Duration = Duration::from_secs(30);
/// Ceiling for `CANARY_INTERVAL_SECS`.
pub const MAX_INTERVAL: Duration = Duration::from_secs(600);
/// The only environment the canary may run in.
pub const ALLOWED_ENVIRONMENT: &str = "alpha";
/// Longest backoff after consecutive failures (a broken channel/permission never gets hammered).
pub const MAX_BACKOFF: Duration = Duration::from_secs(600);
/// A canary slower than this (or never confirmed) is a loud failure.
pub const TIMEOUT: Duration = Duration::from_secs(5);
/// TTL of the result key, seconds.
pub const RESULT_TTL_SECS: u64 = 30;

/// Where the canary's reply goes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SinkMode {
    /// The reply is really posted to the canary channel (true end-to-end incl. the Discord ack).
    Post,
    /// The drain acknowledges the exact canary triple without calling Discord.
    Swallow,
}

/// The owner-provisioned canary identity. All identity fields are required; none is a secret.
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
    /// `post` (default) or `swallow`.
    pub sink_mode: SinkMode,
    /// Driver cadence (before jitter), within [`MIN_INTERVAL`]..=[`MAX_INTERVAL`].
    pub interval: Duration,
}

/// The delay before the next tick: the base `interval` doubled per consecutive failure (capped at
/// [`MAX_BACKOFF`]) with +-10% jitter from `jitter_frac` (0.0..=1.0) so replicas and restarts never
/// align. Pure, so it is unit-tested.
pub fn next_delay(interval: Duration, consecutive_failures: u32, jitter_frac: f64) -> Duration {
    let backoff = interval
        .saturating_mul(1u32 << consecutive_failures.min(6))
        .min(MAX_BACKOFF.max(interval));
    let j = 1.0 + (jitter_frac.clamp(0.0, 1.0) - 0.5) * 0.2;
    backoff.mul_f64(j)
}

fn is_snowflake(s: &str) -> bool {
    (1..=20).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
}

/// Why the canary cannot run (reported as an ERROR + `misconfigured` failure, never silently).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl CanaryConfig {
    /// Builds the config from `CANARY_TENANT_ID`, `CANARY_COMMUNITY_ID`, `CANARY_GUILD_ID`,
    /// `CANARY_CHANNEL_ID`, `CANARY_ENVIRONMENT` (must be `alpha`), and optionally
    /// `CANARY_COMMAND_TEXT` (default `!ping`), `CANARY_SINK_MODE` (`post`|`swallow`, default
    /// `post`), `CANARY_INTERVAL_SECS` (30..=600, default 60).
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
        // Hard environment guard: alpha only, positively declared, never contradicted.
        let declared = req("CANARY_ENVIRONMENT")?;
        if declared != ALLOWED_ENVIRONMENT {
            return Err(ConfigError(format!(
                "CANARY_ENVIRONMENT={declared} -- the canary only runs in {ALLOWED_ENVIRONMENT}"
            )));
        }
        if let Some(env) = get("WADDLES_ENVIRONMENT")
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty() && v != ALLOWED_ENVIRONMENT)
        {
            return Err(ConfigError(format!(
                "WADDLES_ENVIRONMENT={env} -- the canary refuses to run outside {ALLOWED_ENVIRONMENT}"
            )));
        }
        if let Some(id) = get("SPIFFE_ID")
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
        {
            let marker = format!("/{ALLOWED_ENVIRONMENT}/");
            if !id.contains(&marker) {
                return Err(ConfigError(format!(
                    "SPIFFE_ID is not an {ALLOWED_ENVIRONMENT} identity -- the canary refuses to run"
                )));
            }
        }
        let sink_mode = match get("CANARY_SINK_MODE")
            .map(|v| v.trim().to_ascii_lowercase())
            .filter(|v| !v.is_empty())
            .as_deref()
        {
            None | Some("post") => SinkMode::Post,
            Some("swallow") => SinkMode::Swallow,
            Some(other) => {
                return Err(ConfigError(format!(
                    "CANARY_SINK_MODE={other} must be `post` or `swallow`"
                )))
            }
        };
        let interval = match get("CANARY_INTERVAL_SECS")
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
        {
            None => DEFAULT_INTERVAL,
            Some(raw) => {
                let secs: u64 = raw.parse().map_err(|_| {
                    ConfigError("CANARY_INTERVAL_SECS must be a positive integer".into())
                })?;
                Duration::from_secs(secs)
            }
        };
        if !(MIN_INTERVAL..=MAX_INTERVAL).contains(&interval) {
            return Err(ConfigError(format!(
                "CANARY_INTERVAL_SECS must be {}..={} (Discord rate-limit safety)",
                MIN_INTERVAL.as_secs(),
                MAX_INTERVAL.as_secs()
            )));
        }
        let cfg = Self {
            sink_mode,
            interval,
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
            ("CANARY_ENVIRONMENT", "alpha"),
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
            ("CANARY_ENVIRONMENT", ""),
            ("CANARY_ENVIRONMENT", "beta"),
            ("CANARY_ENVIRONMENT", "production"),
            ("WADDLES_ENVIRONMENT", "beta"),
            ("SPIFFE_ID", "spiffe://penguintech.io/beta/svc-ingest"),
            ("CANARY_SINK_MODE", "both"),
            ("CANARY_INTERVAL_SECS", "5"),
            ("CANARY_INTERVAL_SECS", "99999"),
            ("CANARY_INTERVAL_SECS", "x"),
            ("CANARY_GUILD_ID", "REPLACE_ME"),
        ] {
            let mut pairs = vec![
                ("CANARY_TENANT_ID", "900"),
                ("CANARY_COMMUNITY_ID", "901"),
                ("CANARY_GUILD_ID", "1000000000000000001"),
                ("CANARY_CHANNEL_ID", "1000000000000000002"),
                ("CANARY_ENVIRONMENT", "alpha"),
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

    #[test]
    fn defaults_are_post_mode_and_sixty_seconds_and_alpha_ids_pass() {
        let c = good();
        assert_eq!(c.sink_mode, SinkMode::Post);
        assert_eq!(c.interval, DEFAULT_INTERVAL);
        let mut pairs = vec![
            ("CANARY_TENANT_ID", "1"),
            ("CANARY_COMMUNITY_ID", "5"),
            ("CANARY_GUILD_ID", "1000000000000000001"),
            ("CANARY_CHANNEL_ID", "1000000000000000002"),
            ("CANARY_ENVIRONMENT", "alpha"),
            ("WADDLES_ENVIRONMENT", "alpha"),
            ("SPIFFE_ID", "spiffe://penguintech.io/alpha/svc-ingest"),
            ("CANARY_SINK_MODE", "Swallow"),
            ("CANARY_INTERVAL_SECS", "120"),
        ];
        let c = CanaryConfig::from_lookup(lookup(&pairs)).unwrap();
        assert_eq!(c.sink_mode, SinkMode::Swallow);
        assert_eq!(c.interval, Duration::from_secs(120));
        pairs.retain(|(k, _)| *k != "CANARY_ENVIRONMENT");
        assert!(
            CanaryConfig::from_lookup(lookup(&pairs)).is_err(),
            "the alpha declaration is mandatory even when other signals say alpha"
        );
    }

    #[test]
    fn backoff_doubles_is_capped_and_jitter_stays_within_ten_percent() {
        let base = Duration::from_secs(60);
        assert_eq!(next_delay(base, 0, 0.5), base);
        assert_eq!(next_delay(base, 1, 0.5), Duration::from_secs(120));
        assert_eq!(next_delay(base, 3, 0.5), Duration::from_secs(480));
        assert_eq!(next_delay(base, 20, 0.5), MAX_BACKOFF);
        assert_eq!(next_delay(base, 0, 0.0), Duration::from_secs(54));
        assert_eq!(next_delay(base, 0, 1.0), Duration::from_secs(66));
    }
}

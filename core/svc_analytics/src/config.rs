//! Environment-driven configuration. Every knob has a bounded default; secrets
//! (`VALKEY_URL`, `DATABASE_URL`) come from the environment only, never CLI args.

use std::time::Duration;

/// Free-tier retention for layer-A rows (days).
pub const FREE_RETENTION_DAYS: i32 = 90;
/// Premium-tier retention for layer-A rows (days).
pub const PREMIUM_RETENTION_DAYS: i32 = 400;

/// A secret string whose `Debug` never prints the value.
#[derive(Clone)]
pub struct Secret(String);

impl Secret {
    /// The secret value (use only to connect).
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for Secret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Secret(<redacted>)")
    }
}

/// Returns `url` with `password` injected as the userinfo password when the URL carries none
/// (percent-encoded). The chart supplies `VALKEY_URL` and `VALKEY_PASSWORD` separately.
pub fn url_with_password(url: &str, password: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return url.to_string();
    };
    let authority = rest.split('/').next().unwrap_or("");
    if authority.contains('@') {
        return url.to_string();
    }
    let enc: String = password
        .bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect();
    format!("{scheme}://:{enc}@{rest}")
}

/// Runtime configuration for the consumer, writer, purge and health surfaces.
#[derive(Debug, Clone)]
pub struct Config {
    /// Valkey URL with the password applied (secret; `VALKEY_URL` + `VALKEY_PASSWORD`).
    pub valkey_url: Secret,
    /// Postgres URL for the `waddles_svc_analytics` LOGIN role (secret; `DATABASE_URL`).
    pub database_url: Secret,
    /// Input stream name.
    pub stream: String,
    /// Consumer group name.
    pub group: String,
    /// Dead-letter stream name.
    pub dlq_stream: String,
    /// This consumer's name within the group.
    pub consumer: String,
    /// Max entries per XREADGROUP (bounded batch).
    pub batch_size: usize,
    /// XREADGROUP BLOCK timeout.
    pub block: Duration,
    /// Entries pending longer than this on a dead consumer are claimed.
    pub claim_idle: Duration,
    /// Failed attempts (exclusion unavailable / data fault) before an entry is dead-lettered.
    pub max_attempts: u32,
    /// Approximate MAXLEN of the DLQ stream.
    pub dlq_maxlen: usize,
    /// Exclusion cache entry TTL.
    pub exclusion_ttl: Duration,
    /// Max exclusion entries held (seed cap and cache bound).
    pub exclusion_cap: usize,
    /// Community-tier cache TTL.
    pub tier_ttl: Duration,
    /// Max tier cache entries.
    pub tier_cap: usize,
    /// Open sessions older than this (seconds) are closed by the sweeper, and a session
    /// is never credited with more than this much presence.
    pub session_max_secs: i64,
    /// Purge window start hour (UTC, inclusive).
    pub purge_start_hour: i64,
    /// Purge window end hour (UTC, exclusive).
    pub purge_end_hour: i64,
    /// Rows deleted per purge batch.
    pub purge_batch: i64,
    /// Max batches per table per purge run.
    pub purge_max_batches: u32,
    /// Minimum spacing between maintenance passes (cohort refresh, stale sweep).
    pub maintenance_interval: Duration,
    /// gRPC health listener address (`0.0.0.0:<GRPC_HEALTH_PORT>`).
    pub grpc_addr: String,
    /// Flag-off idle poll interval.
    pub idle_poll: Duration,
}

impl Config {
    /// Builds a config from a variable lookup (production passes `std::env::var`).
    ///
    /// # Errors
    /// Returns a message naming the offending variable when a required value is
    /// missing or a numeric value fails to parse or is out of bounds.
    pub fn from_lookup<F>(get: F) -> Result<Self, String>
    where
        F: Fn(&str) -> Option<String>,
    {
        let req = |k: &str| {
            get(k)
                .filter(|v| !v.is_empty())
                .ok_or_else(|| format!("{k} is required"))
        };
        let num = |k: &str, d: i64, lo: i64, hi: i64| -> Result<i64, String> {
            match get(k) {
                None => Ok(d),
                Some(v) => {
                    let n: i64 = v.parse().map_err(|_| format!("{k} must be an integer"))?;
                    if (lo..=hi).contains(&n) {
                        Ok(n)
                    } else {
                        Err(format!("{k} must be within {lo}..={hi}"))
                    }
                }
            }
        };
        let text = |k: &str, d: &str| {
            get(k)
                .filter(|v| !v.is_empty())
                .unwrap_or_else(|| d.to_string())
        };
        let secs = |k: &str, d: i64, lo: i64, hi: i64| {
            num(k, d, lo, hi).map(|n| Duration::from_secs(n as u64))
        };
        let host = get("HOSTNAME")
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| "local".into());
        Ok(Self {
            valkey_url: Secret(match get("VALKEY_PASSWORD").filter(|p| !p.is_empty()) {
                Some(pw) => url_with_password(&req("VALKEY_URL")?, &pw),
                None => req("VALKEY_URL")?,
            }),
            database_url: Secret(req("DATABASE_URL")?),
            stream: text("ANALYTICS_STREAM_KEY", "waddles:analytics"),
            group: text("ANALYTICS_CONSUMER_GROUP", "svc_analytics"),
            dlq_stream: text("ANALYTICS_DLQ_STREAM", "waddles:analytics:dlq"),
            consumer: text("ANALYTICS_CONSUMER", &format!("svc-analytics-{host}")),
            batch_size: num("ANALYTICS_BATCH_SIZE", 200, 1, 5000)? as usize,
            block: Duration::from_millis(num("ANALYTICS_BLOCK_MS", 2000, 10, 30000)? as u64),
            claim_idle: secs("ANALYTICS_CLAIM_IDLE_SECS", 60, 1, 3600)?,
            max_attempts: num("ANALYTICS_MAX_ATTEMPTS", 5, 1, 100)? as u32,
            dlq_maxlen: num("ANALYTICS_DLQ_MAXLEN", 10_000, 10, 10_000_000)? as usize,
            exclusion_ttl: secs("ANALYTICS_EXCLUSION_TTL_SECS", 5, 1, 3600)?,
            exclusion_cap: num("ANALYTICS_EXCLUSION_CAP", 200_000, 10, 5_000_000)? as usize,
            tier_ttl: secs("ANALYTICS_TIER_TTL_SECS", 300, 1, 86_400)?,
            tier_cap: num("ANALYTICS_TIER_CAP", 100_000, 10, 5_000_000)? as usize,
            session_max_secs: num("ANALYTICS_SESSION_MAX_SECS", 43_200, 60, 604_800)?,
            purge_start_hour: num("ANALYTICS_PURGE_START_HOUR_UTC", 3, 0, 23)?,
            purge_end_hour: num("ANALYTICS_PURGE_END_HOUR_UTC", 5, 1, 24)?,
            purge_batch: num("ANALYTICS_PURGE_BATCH", 5000, 1, 100_000)?,
            purge_max_batches: num("ANALYTICS_PURGE_MAX_BATCHES", 20, 1, 10_000)? as u32,
            maintenance_interval: secs("ANALYTICS_MAINTENANCE_SECS", 3600, 1, 86_400)?,
            grpc_addr: format!("0.0.0.0:{}", num("GRPC_HEALTH_PORT", 50209, 1, 65535)?),
            idle_poll: secs("ANALYTICS_IDLE_POLL_SECS", 5, 1, 300)?,
        })
    }

    /// Builds the config from the process environment.
    ///
    /// # Errors
    /// See [`Config::from_lookup`].
    pub fn from_env() -> Result<Self, String> {
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Whether `hour_utc` (0..24) falls inside the configured off-peak purge window
    /// (wrapping windows such as 22..4 are supported).
    pub fn in_purge_window(&self, hour_utc: i64) -> bool {
        let (s, e) = (self.purge_start_hour, self.purge_end_hour % 24);
        if s < e {
            (s..e).contains(&hour_utc)
        } else if s > e {
            hour_utc >= s || hour_utc < e
        } else {
            true
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lookup(extra: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let mut m: HashMap<String, String> = HashMap::new();
        m.insert("VALKEY_URL".into(), "redis://x".into());
        m.insert("DATABASE_URL".into(), "postgres://x".into());
        for (k, v) in extra {
            m.insert((*k).into(), (*v).into());
        }
        move |k| m.get(k).cloned()
    }

    #[test]
    fn defaults_apply() {
        let c = Config::from_lookup(lookup(&[])).unwrap();
        assert_eq!(c.stream, "waddles:analytics");
        assert_eq!(c.group, "svc_analytics");
        assert_eq!(c.batch_size, 200);
        assert_eq!(c.consumer, "svc-analytics-local");
    }

    #[test]
    fn missing_required_is_error() {
        let e = Config::from_lookup(|_| None).unwrap_err();
        assert!(e.contains("VALKEY_URL"));
        let e = Config::from_lookup(|k| (k == "VALKEY_URL").then(|| "r".into())).unwrap_err();
        assert!(e.contains("DATABASE_URL"));
    }

    #[test]
    fn chart_env_names_and_secret_handling() {
        let c = Config::from_lookup(lookup(&[
            ("VALKEY_URL", "rediss://valkey:6380/0"),
            ("VALKEY_PASSWORD", "p@ss/w:rd"),
            ("GRPC_HEALTH_PORT", "50300"),
            ("ANALYTICS_STREAM_KEY", "s:k"),
            ("ANALYTICS_CONSUMER_GROUP", "g"),
        ]))
        .unwrap();
        assert_eq!(
            c.valkey_url.expose(),
            "rediss://:p%40ss%2Fw%3Ard@valkey:6380/0"
        );
        assert_eq!(c.grpc_addr, "0.0.0.0:50300");
        assert_eq!((c.stream.as_str(), c.group.as_str()), ("s:k", "g"));
        let shown = format!("{c:?}");
        assert!(
            !shown.contains("p%40ss") && !shown.contains("postgres://x"),
            "Debug must redact secrets"
        );
        assert_eq!(
            Config::from_lookup(lookup(&[])).unwrap().grpc_addr,
            "0.0.0.0:50209"
        );
        assert_eq!(url_with_password("redis://u:p@h/0", "x"), "redis://u:p@h/0");
        assert_eq!(url_with_password("not-a-url", "x"), "not-a-url");
        let c = Config::from_lookup(lookup(&[("VALKEY_PASSWORD", "")])).unwrap();
        assert_eq!(c.valkey_url.expose(), "redis://x");
        assert!(Config::from_lookup(lookup(&[("GRPC_HEALTH_PORT", "70000")])).is_err());
    }

    #[test]
    fn bad_numbers_rejected() {
        for (k, v) in [
            ("ANALYTICS_BATCH_SIZE", "0"),
            ("ANALYTICS_BATCH_SIZE", "abc"),
            ("ANALYTICS_PURGE_START_HOUR_UTC", "24"),
        ] {
            assert!(Config::from_lookup(lookup(&[(k, v)])).is_err(), "{k}={v}");
        }
        let c = Config::from_lookup(lookup(&[
            ("HOSTNAME", "pod-1"),
            ("ANALYTICS_BATCH_SIZE", "7"),
        ]))
        .unwrap();
        assert_eq!(
            (c.batch_size, c.consumer.as_str()),
            (7, "svc-analytics-pod-1")
        );
    }

    #[test]
    fn purge_window_normal_wrapping_and_degenerate() {
        let mut c = Config::from_lookup(lookup(&[])).unwrap();
        assert!(c.in_purge_window(3) && c.in_purge_window(4) && !c.in_purge_window(5));
        c.purge_start_hour = 22;
        c.purge_end_hour = 4;
        assert!(c.in_purge_window(23) && c.in_purge_window(1) && !c.in_purge_window(12));
        c.purge_start_hour = 0;
        c.purge_end_hour = 24;
        assert!(c.in_purge_window(13));
    }
}

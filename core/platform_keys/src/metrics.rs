//! OpenTelemetry instruments for the key cache. Labels are an allowlist: the numeric key type and
//! a fixed `reason` enum only -- never a kid (attacker-controlled on a miss), key bytes or a URL.

use opentelemetry::metrics::{Counter, Histogram, Meter};
use opentelemetry::KeyValue;

/// Instrument names (stable; documented in the crate README).
pub const HITS: &str = "platform_keys_cache_hits_total";
pub const MISSES: &str = "platform_keys_cache_misses_total";
pub const REFRESH_FAILURES: &str = "platform_keys_refresh_failures_total";
pub const UNKNOWN_KID: &str = "platform_keys_unknown_kid_total";
pub const REJECTED: &str = "platform_keys_rejected_total";
pub const REFRESHES: &str = "platform_keys_refreshes_total";
pub const REFRESH_DURATION: &str = "platform_keys_refresh_duration_seconds";

/// Why a lookup was refused (the `reason` label; a closed set).
#[derive(Debug, Clone, Copy)]
pub enum Reason {
    UnknownKid,
    Expired,
    Stale,
    Unavailable,
}

impl Reason {
    fn as_str(self) -> &'static str {
        match self {
            Self::UnknownKid => "unknown_kid",
            Self::Expired => "expired",
            Self::Stale => "stale",
            Self::Unavailable => "unavailable",
        }
    }
}

/// The cache's instruments, built from one [`Meter`].
#[derive(Clone)]
pub struct Metrics {
    hits: Counter<u64>,
    misses: Counter<u64>,
    refresh_failures: Counter<u64>,
    unknown_kid: Counter<u64>,
    rejected: Counter<u64>,
    refreshes: Counter<u64>,
    refresh_duration: Histogram<f64>,
}

impl Metrics {
    /// Registers every instrument on `meter`.
    pub fn new(meter: &Meter) -> Self {
        Self {
            hits: meter
                .u64_counter(HITS)
                .with_description("Key lookups served from the cache")
                .build(),
            misses: meter
                .u64_counter(MISSES)
                .with_description("Key lookups not in the cache")
                .build(),
            refresh_failures: meter
                .u64_counter(REFRESH_FAILURES)
                .with_description("Failed key store reads (seed, poll or lazy load)")
                .build(),
            unknown_kid: meter
                .u64_counter(UNKNOWN_KID)
                .with_description("Lookups refused because no such kid exists")
                .build(),
            rejected: meter
                .u64_counter(REJECTED)
                .with_description("Lookups refused, by reason")
                .build(),
            refreshes: meter
                .u64_counter(REFRESHES)
                .with_description("Successful full snapshot refreshes")
                .build(),
            refresh_duration: meter
                .f64_histogram(REFRESH_DURATION)
                .with_unit("s")
                .with_description("Duration of a full snapshot refresh")
                .build(),
        }
    }

    fn ty(key_type: i16) -> [KeyValue; 1] {
        [KeyValue::new("key_type", i64::from(key_type))]
    }

    pub fn hit(&self, key_type: i16) {
        self.hits.add(1, &Self::ty(key_type));
    }

    pub fn miss(&self, key_type: i16) {
        self.misses.add(1, &Self::ty(key_type));
    }

    pub fn unknown_kid(&self, key_type: i16) {
        self.unknown_kid.add(1, &Self::ty(key_type));
    }

    pub fn refresh_failure(&self) {
        self.refresh_failures.add(1, &[]);
    }

    pub fn refreshed(&self, seconds: f64) {
        self.refreshes.add(1, &[]);
        self.refresh_duration.record(seconds, &[]);
    }

    pub fn rejected(&self, key_type: i16, reason: Reason) {
        self.rejected.add(
            1,
            &[
                KeyValue::new("key_type", i64::from(key_type)),
                KeyValue::new("reason", reason.as_str()),
            ],
        );
    }
}

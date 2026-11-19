//! OTel metric instruments (pushed over OTLP via the global meter provider installed by
//! `penguin_logging::init`). Every label comes from a fixed, bounded set; no user, content
//! or community identifiers are ever attached.

use opentelemetry::metrics::{Counter, Gauge, Histogram};
use opentelemetry::{global, KeyValue};

/// All service instruments.
#[derive(Clone)]
pub struct Metrics {
    /// Stream entries by outcome (`read`, `written`, `rejected`, `excluded`, `duplicate`, ...).
    pub events: Counter<u64>,
    /// Contract rejections by reason code.
    pub rejected: Counter<u64>,
    /// Events dropped (never written) by reason.
    pub dropped: Counter<u64>,
    /// Dead-lettered entries by reason.
    pub dlq: Counter<u64>,
    /// ALERT: exclusion lookup unavailable (batch held, nothing written).
    pub exclusion_lookup_failures: Counter<u64>,
    /// Tier lookup failures (communities fell back to Free).
    pub tier_lookup_failures: Counter<u64>,
    /// Cache lookups by cache and result.
    pub cache: Counter<u64>,
    /// Database faults by kind.
    pub db_faults: Counter<u64>,
    /// Session transitions by outcome.
    pub sessions: Counter<u64>,
    /// Rows purged by table.
    pub purge_rows: Counter<u64>,
    /// Maintenance job runs by job and result.
    pub jobs: Counter<u64>,
    /// XACK failures.
    pub ack_failures: Counter<u64>,
    /// XREADGROUP failures or malformed replies.
    pub read_failures: Counter<u64>,
    /// Batch processing latency (seconds).
    pub batch_seconds: Histogram<f64>,
    /// Batch size (entries).
    pub batch_size: Histogram<u64>,
    /// 1 when the consumer flag is on.
    pub flag_enabled: Gauge<u64>,
    /// 1 while the process is up.
    pub up: Gauge<u64>,
    /// Pending (delivered, unacked) entries for the group.
    pub pending: Gauge<u64>,
}

impl Metrics {
    /// Creates the instruments on the global meter.
    pub fn new() -> Self {
        let m = global::meter("svc-analytics");
        Self {
            events: m
                .u64_counter("svc_analytics_events_total")
                .with_description("Stream entries by outcome")
                .build(),
            rejected: m
                .u64_counter("svc_analytics_rejected_total")
                .with_description("Contract rejections by reason")
                .build(),
            dropped: m
                .u64_counter("svc_analytics_dropped_total")
                .with_description("Events dropped without a write, by reason")
                .build(),
            dlq: m
                .u64_counter("svc_analytics_dlq_total")
                .with_description("Dead-lettered entries by reason")
                .build(),
            exclusion_lookup_failures: m
                .u64_counter("svc_analytics_exclusion_lookup_failures_total")
                .with_description("ALERT: exclusion lookup unavailable, batch held (fail closed)")
                .build(),
            tier_lookup_failures: m
                .u64_counter("svc_analytics_tier_lookup_failures_total")
                .with_description("Tier lookup failures (Free fallback)")
                .build(),
            cache: m
                .u64_counter("svc_analytics_cache_total")
                .with_description("Cache lookups")
                .build(),
            db_faults: m
                .u64_counter("svc_analytics_db_faults_total")
                .with_description("Database faults by kind")
                .build(),
            sessions: m
                .u64_counter("svc_analytics_sessions_total")
                .with_description("Presence session transitions")
                .build(),
            purge_rows: m
                .u64_counter("svc_analytics_purge_rows_total")
                .with_description("Rows purged by table")
                .build(),
            jobs: m
                .u64_counter("svc_analytics_jobs_total")
                .with_description("Maintenance jobs")
                .build(),
            ack_failures: m
                .u64_counter("svc_analytics_ack_failures_total")
                .with_description("XACK failures")
                .build(),
            read_failures: m
                .u64_counter("svc_analytics_stream_read_failures_total")
                .with_description("XREADGROUP failures or malformed replies")
                .build(),
            batch_seconds: m
                .f64_histogram("svc_analytics_batch_duration_seconds")
                .with_description("Batch processing latency")
                .build(),
            batch_size: m
                .u64_histogram("svc_analytics_batch_size")
                .with_description("Entries per batch")
                .build(),
            flag_enabled: m
                .u64_gauge("svc_analytics_flag_enabled")
                .with_description("Consumer flag state")
                .build(),
            up: m
                .u64_gauge("svc_analytics_up")
                .with_description("Process up")
                .build(),
            pending: m
                .u64_gauge("svc_analytics_stream_pending")
                .with_description("Pending entries in the consumer group")
                .build(),
        }
    }

    /// Increments a labeled counter.
    pub fn inc(c: &Counter<u64>, n: u64, key: &'static str, value: &'static str) {
        if n > 0 {
            c.add(n, &[KeyValue::new(key, value)]);
        }
    }
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

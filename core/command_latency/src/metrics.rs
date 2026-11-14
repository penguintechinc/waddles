//! The command-latency OTel instruments.
//!
//! Exposed names (the instrument names below carry no `_total`/unit suffix; the Prometheus exporter
//! and OTLP translators append them -- see `docs/observability/data-plane-metrics.md` Naming):
//!
//! | Exposed name | Type | Labels |
//! |---|---|---|
//! | `waddles_command_hop_seconds` | histogram | `hop` |
//! | `waddles_command_e2e_seconds` | histogram | `command, bundle_lang, outcome, door` |
//! | `waddles_command_e2e_over_budget_total` | counter | `command, bundle_lang, outcome` |
//! | `waddles_command_latency_marker_invalid_total` | counter | `reason` |
//! | `waddles_canary_e2e_seconds` | histogram | `outcome` |
//! | `waddles_canary_failures_total` | counter | `reason` |
//!
//! Every label is a closed or registry-bounded vocabulary; no id, user, message text, channel or
//! tenant slug ever becomes a label.

use std::sync::OnceLock;
use std::time::Duration;

use opentelemetry::metrics::{Counter, Histogram, Meter};
use opentelemetry::KeyValue;

use crate::stamp::{Door, Stamps};

/// Instrumentation scope name.
pub const SCOPE: &str = "command_latency";

/// The owner SLA budget: a non-external command must finish (first door -> outbound confirmed)
/// under this. p99 < 1 s.
pub const BUDGET: Duration = Duration::from_secs(1);

/// Histogram bounds in seconds: 5 ms .. 5 s with extra resolution across 0.5-1.5 s, where the SLO
/// threshold lives (the SDK default boundaries are millisecond-shaped and would collapse every
/// sub-second sample into one bucket).
pub const BUCKETS_S: [f64; 24] = [
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1,
    1.25, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0,
];

/// The hops of the command path. A closed set (the `hop` label).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Hop {
    /// First door -> ingest-stream XADD acknowledged (receive, permissions lookup, encrypt, publish).
    IngestPublish,
    /// XADD -> process-stage consume (consumer-group lag + `XREADGROUP BLOCK` wake-up).
    QueueWait,
    /// Process consume -> action-stream XADD (hop verify, decrypt, tokenize, transform, route).
    ProcessStage,
    /// Action-stream XADD -> action-stage consume.
    ActionQueueWait,
    /// Action consume -> executor invoke sent (tenant resolve, bundle load check, setup).
    DispatchToInvoke,
    /// Invoke sent -> outbound request issued (executor queue + pool wait + instantiate + guest).
    ExecutorToOutbound,
    /// Outbound request issued -> platform op confirmed (relay queue + ingest drain + REST + ack).
    OutboundCall,
}

impl Hop {
    /// The bounded `hop` label value.
    pub fn label(self) -> &'static str {
        match self {
            Hop::IngestPublish => "ingest_publish",
            Hop::QueueWait => "queue_wait",
            Hop::ProcessStage => "process_stage",
            Hop::ActionQueueWait => "action_queue_wait",
            Hop::DispatchToInvoke => "dispatch_to_invoke",
            Hop::ExecutorToOutbound => "executor_to_outbound",
            Hop::OutboundCall => "outbound_call",
        }
    }
}

/// Terminal outcome of a command at the outbound-confirm point. A closed set.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Outcome {
    /// The platform op was confirmed.
    Ok,
    /// The platform/drain refused or failed the op.
    Refused,
    /// The op was queued but not confirmed in time.
    Timeout,
    /// Any other failure at the outbound step.
    Error,
}

impl Outcome {
    /// The bounded `outcome` label value.
    pub fn label(self) -> &'static str {
        match self {
            Outcome::Ok => "ok",
            Outcome::Refused => "refused",
            Outcome::Timeout => "timeout",
            Outcome::Error => "error",
        }
    }
}

/// The instrument set. Production uses [`metrics`]; tests build one against an in-memory provider.
pub struct CommandMetrics {
    hop: Histogram<f64>,
    e2e: Histogram<f64>,
    over_budget: Counter<u64>,
    marker_invalid: Counter<u64>,
    canary_e2e: Histogram<f64>,
    canary_failures: Counter<u64>,
}

impl CommandMetrics {
    /// Registers the instruments on `meter`.
    pub fn new(meter: &Meter) -> Self {
        let hist = |name: &'static str, desc: &'static str| {
            meter
                .f64_histogram(name)
                .with_description(desc)
                .with_unit("s")
                .with_boundaries(BUCKETS_S.to_vec())
                .build()
        };
        Self {
            hop: hist(
                "waddles_command_hop",
                "Seconds one command spent in one hop of the data-plane path",
            ),
            e2e: hist(
                "waddles_command_e2e",
                "Seconds from the first door to the outbound platform op confirmed",
            ),
            over_budget: meter
                .u64_counter("waddles_command_e2e_over_budget")
                .with_description("Commands whose end-to-end latency exceeded the 1.0 s budget")
                .build(),
            marker_invalid: meter
                .u64_counter("waddles_command_latency_marker_invalid")
                .with_description("First-door stamps refused as implausible (skewed or stale)")
                .build(),
            canary_e2e: hist(
                "waddles_canary_e2e",
                "Seconds for the synthetic canary command, first door to outbound sink confirmed",
            ),
            canary_failures: meter
                .u64_counter("waddles_canary_failures")
                .with_description(
                    "Synthetic canary runs that failed (timeout, misconfigured, error)",
                )
                .build(),
        }
    }

    /// Records one hop duration. No-op under the kill switch.
    pub fn record_hop(&self, hop: Hop, elapsed: Duration) {
        if crate::switch::is_disabled() {
            return;
        }
        self.hop
            .record(elapsed.as_secs_f64(), &[KeyValue::new("hop", hop.label())]);
    }

    /// Records the end-to-end histogram and, at or over [`BUDGET`], the over-budget counter. Returns
    /// `true` when the budget was exceeded so the caller can emit the WARN with its breakdown.
    pub fn record_e2e(
        &self,
        command: &str,
        bundle_lang: &str,
        outcome: Outcome,
        door: Door,
        elapsed: Duration,
    ) -> bool {
        if crate::switch::is_disabled() {
            return false;
        }
        let over = elapsed >= BUDGET;
        self.e2e.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("command", command.to_string()),
                KeyValue::new("bundle_lang", bundle_lang.to_string()),
                KeyValue::new("outcome", outcome.label()),
                KeyValue::new("door", door.label()),
            ],
        );
        if over {
            self.over_budget.add(
                1,
                &[
                    KeyValue::new("command", command.to_string()),
                    KeyValue::new("bundle_lang", bundle_lang.to_string()),
                    KeyValue::new("outcome", outcome.label()),
                ],
            );
        }
        over
    }

    /// Counts a first-door stamp refused (`skewed`, `stale`) or absent (`missing`).
    pub fn record_marker_invalid(&self, reason: &'static str) {
        if crate::switch::is_disabled() {
            return;
        }
        self.marker_invalid
            .add(1, &[KeyValue::new("reason", reason)]);
    }

    /// Records one synthetic-canary e2e sample (not subject to the kill switch: the canary is its own
    /// flag and exists to prove the path).
    pub fn record_canary(&self, outcome: Outcome, elapsed: Duration) {
        self.canary_e2e.record(
            elapsed.as_secs_f64(),
            &[KeyValue::new("outcome", outcome.label())],
        );
    }

    /// Counts a failed canary run; `reason` is a closed vocabulary (`timeout`, `misconfigured`,
    /// `publish_failed`, `result_unreadable`).
    pub fn record_canary_failure(&self, reason: &'static str) {
        self.canary_failures
            .add(1, &[KeyValue::new("reason", reason)]);
    }
}

/// The process-wide instrument set on the global meter provider. Call it (or any record helper) only
/// after telemetry init so the instruments bind to the real provider.
pub fn metrics() -> &'static CommandMetrics {
    static GLOBAL: OnceLock<CommandMetrics> = OnceLock::new();
    GLOBAL.get_or_init(|| CommandMetrics::new(&opentelemetry::global::meter(SCOPE)))
}

/// The per-hop millisecond breakdown computable from a full stamp set plus the two action-side
/// instants (`outbound_request_ms`: relay LPUSH, `confirm_ms`: ack observed). Hops with a missing
/// stamp are omitted. Used for the over-budget WARN so one line splits the slow command by hop.
pub fn breakdown(
    stamps: &Stamps,
    outbound_request_ms: u64,
    confirm_ms: u64,
) -> Vec<(&'static str, u64)> {
    let mut out = Vec::with_capacity(7);
    let mut push = |hop: Hop, from: Option<u64>, to: Option<u64>| {
        if let (Some(a), Some(b)) = (from, to) {
            out.push((hop.label(), b.saturating_sub(a)));
        }
    };
    push(Hop::IngestPublish, stamps.t0_ms, stamps.publish_ms);
    push(Hop::QueueWait, stamps.publish_ms, stamps.consume_ms);
    push(Hop::ProcessStage, stamps.consume_ms, stamps.handoff_ms);
    push(
        Hop::ActionQueueWait,
        stamps.handoff_ms,
        stamps.action_consume_ms,
    );
    push(
        Hop::DispatchToInvoke,
        stamps.action_consume_ms,
        stamps.invoke_ms,
    );
    push(
        Hop::ExecutorToOutbound,
        stamps.invoke_ms,
        Some(outbound_request_ms),
    );
    push(
        Hop::OutboundCall,
        Some(outbound_request_ms),
        Some(confirm_ms),
    );
    out
}

/// Renders a breakdown as `hop=ms,hop=ms` for a log field (numbers and closed hop names only).
pub fn render_breakdown(parts: &[(&'static str, u64)]) -> String {
    parts
        .iter()
        .map(|(h, ms)| format!("{h}={ms}"))
        .collect::<Vec<_>>()
        .join(",")
}

#[cfg(test)]
mod tests {
    use super::*;
    use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
    use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};

    pub(crate) fn provider() -> (SdkMeterProvider, InMemoryMetricExporter) {
        let exporter = InMemoryMetricExporter::default();
        let reader = PeriodicReader::builder(exporter.clone()).build();
        (
            SdkMeterProvider::builder().with_reader(reader).build(),
            exporter,
        )
    }

    /// Sums histogram counts per metric name from the flushed data.
    fn hist_counts(exporter: &InMemoryMetricExporter) -> std::collections::HashMap<String, u64> {
        let mut out = std::collections::HashMap::new();
        for rm in exporter.get_finished_metrics().unwrap() {
            for sm in rm.scope_metrics() {
                for m in sm.metrics() {
                    if let AggregatedMetrics::F64(MetricData::Histogram(h)) = m.data() {
                        let n: u64 = h.data_points().map(|dp| dp.count()).sum();
                        *out.entry(m.name().to_string()).or_default() += n;
                    }
                }
            }
        }
        out
    }

    #[test]
    fn buckets_are_sorted_span_5ms_to_10s_and_resolve_the_slo_band() {
        assert!(BUCKETS_S.windows(2).all(|w| w[0] < w[1]));
        assert_eq!(BUCKETS_S[0], 0.005);
        assert!(BUCKETS_S.contains(&1.0), "the budget is a bucket edge");
        let band = BUCKETS_S
            .iter()
            .filter(|b| (0.5..=1.5).contains(*b))
            .count();
        assert!(band >= 9, "extra resolution around 0.5-1.5 s, got {band}");
    }

    #[test]
    fn hop_and_outcome_labels_are_closed_and_distinct() {
        let hops = [
            Hop::IngestPublish,
            Hop::QueueWait,
            Hop::ProcessStage,
            Hop::ActionQueueWait,
            Hop::DispatchToInvoke,
            Hop::ExecutorToOutbound,
            Hop::OutboundCall,
        ];
        let set: std::collections::HashSet<_> = hops.iter().map(|h| h.label()).collect();
        assert_eq!(set.len(), hops.len());
    }

    #[test]
    fn e2e_records_histogram_and_flags_over_budget() {
        let _g = crate::switch::tests::SWITCH_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        crate::switch::set_disabled(false);
        let (provider, exporter) = provider();
        let m = CommandMetrics::new(&opentelemetry::metrics::MeterProvider::meter(
            &provider, "t",
        ));
        assert!(!m.record_e2e(
            "ping",
            "rust",
            Outcome::Ok,
            Door::Gateway,
            Duration::from_millis(300)
        ));
        assert!(m.record_e2e(
            "ping",
            "rust",
            Outcome::Ok,
            Door::Gateway,
            Duration::from_millis(1000)
        ));
        assert!(m.record_e2e(
            "ping",
            "rust",
            Outcome::Timeout,
            Door::Sink,
            Duration::from_millis(5200)
        ));
        m.record_hop(Hop::QueueWait, Duration::from_millis(40));
        provider.force_flush().unwrap();
        let counts = hist_counts(&exporter);
        assert_eq!(counts.get("waddles_command_e2e"), Some(&3));
        assert_eq!(counts.get("waddles_command_hop"), Some(&1));
    }

    #[test]
    fn kill_switch_makes_every_recorder_a_noop() {
        let _g = crate::switch::tests::SWITCH_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let (provider, exporter) = provider();
        let m = CommandMetrics::new(&opentelemetry::metrics::MeterProvider::meter(
            &provider, "t",
        ));
        crate::switch::set_disabled(true);
        assert!(!m.record_e2e(
            "ping",
            "rust",
            Outcome::Ok,
            Door::Gateway,
            Duration::from_secs(9)
        ));
        m.record_hop(Hop::QueueWait, Duration::from_millis(40));
        m.record_marker_invalid("stale");
        crate::switch::set_disabled(false);
        provider.force_flush().unwrap();
        assert!(
            hist_counts(&exporter).is_empty(),
            "nothing recorded while disabled"
        );
    }

    #[test]
    fn breakdown_attributes_each_hop_and_sums_to_the_end_to_end() {
        let s = Stamps {
            t0_ms: Some(1000),
            door: Some(Door::Gateway),
            publish_ms: Some(1020),
            consume_ms: Some(1100),
            handoff_ms: Some(1500),
            action_consume_ms: Some(1510),
            invoke_ms: Some(1520),
            command: Some("ping".into()),
        };
        let b = breakdown(&s, 1700, 1850);
        let get = |h: &str| b.iter().find(|(n, _)| *n == h).map(|(_, v)| *v);
        assert_eq!(get("ingest_publish"), Some(20));
        assert_eq!(get("queue_wait"), Some(80));
        assert_eq!(get("process_stage"), Some(400));
        assert_eq!(get("action_queue_wait"), Some(10));
        assert_eq!(get("dispatch_to_invoke"), Some(10));
        assert_eq!(get("executor_to_outbound"), Some(180));
        assert_eq!(get("outbound_call"), Some(150));
        assert_eq!(
            b.iter().map(|(_, v)| v).sum::<u64>(),
            850,
            "hops tile first door -> confirm"
        );
        assert_eq!(
            render_breakdown(&b),
            "ingest_publish=20,queue_wait=80,process_stage=400,action_queue_wait=10,dispatch_to_invoke=10,executor_to_outbound=180,outbound_call=150"
        );
    }

    #[test]
    fn breakdown_omits_hops_whose_stamp_is_missing() {
        let s = Stamps {
            t0_ms: Some(1000),
            ..Stamps::default()
        };
        let b = breakdown(&s, 1700, 1850);
        assert_eq!(b, vec![("outbound_call", 150)]);
    }
}

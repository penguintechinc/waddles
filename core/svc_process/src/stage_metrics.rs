//! OTel metrics for the process stage (spec §13.1): the histograms and
//! counters that answer "is the pipeline healthy" -- stage latency, executor
//! call duration, host-capability call latency, bundle load cost, drain
//! batch size, sandbox trips -- plus the real [`penguin_spine::SpineMetrics`]
//! implementation that replaces the former `NoopMetrics` stub.
//!
//! Every instrument is built from the **global** meter provider that
//! `penguin-logging` installs in [`crate::telemetry::init`], so the same
//! recording reaches BOTH the OTLP push pipeline (`OTEL_EXPORTER_OTLP_*`) and
//! the `/metrics` Prometheus scrape surface (the `opentelemetry-prometheus`
//! reader `penguin-logging` attaches to the same provider). Nothing here
//! names a backend.
//!
//! **Label policy** (`rules/critical-rules.md` Observability, PII boundary):
//! every label value is either a compile-time `&'static str` drawn from a
//! closed vocabulary, or a host-derived bundle identity (`app_id`, the same
//! label `waddles_bundle_kv_authorize_denied_total` already carries). Nothing
//! a guest or an end user controls -- an op string, a message, a username, a
//! stream/channel name, a destination host -- ever becomes a label; guest
//! op strings are folded through [`KNOWN_OPS`] so a hostile bundle cannot
//! mint unbounded series.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use opentelemetry::metrics::{Counter, Gauge, Histogram, Meter};
use opentelemetry::KeyValue;
use penguin_bundle_host::wire::{CapabilityKind, ErrorCode};

/// The `stage` label value every metric from this crate carries.
pub const STAGE: &str = "process";

/// Instrumentation scope name (`otel_scope_name` on the Prometheus surface).
pub const SCOPE: &str = "svc_process";

/// Histogram bucket upper bounds, in seconds, for every latency instrument:
/// 1 ms to 30 s. The OTel SDK default boundaries are millisecond-shaped
/// (`0, 5, 10, 25 ... 10000`) and would collapse every sub-second sample
/// into the first bucket of a `_seconds` histogram.
pub const LATENCY_BUCKETS_S: [f64; 14] = [
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
];

/// Histogram bucket upper bounds for count-shaped instruments (batch size,
/// queue depth, PEL size).
pub const COUNT_BUCKETS: [f64; 12] = [
    0.0, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 5000.0,
];

/// Every `waddles_*` instrument name this module registers -- asserted
/// against the emitted data in tests and mirrored in
/// `docs/observability/data-plane-metrics.md`, so a rename is a conscious,
/// documented act.
pub const METRIC_NAMES: [&str; 15] = [
    "waddles_stage_latency_seconds",
    "waddles_executor_call_seconds",
    "waddles_host_call_latency_seconds",
    "waddles_bundle_load_seconds",
    "waddles_spine_batch_size",
    "waddles_sandbox_trip_total",
    "waddles_stream_events_total",
    "waddles_stream_trimmed_total",
    "waddles_stream_claimed_total",
    "waddles_consumer_skipped_total",
    "waddles_spine_dlq_total",
    "waddles_spine_queue_depth",
    "waddles_group_pending",
    "waddles_insecure_transport",
    "waddles_tenant_boundary_violations_total",
];

/// The OTel instrument name behind a documented (Prometheus-form) metric
/// name. OTel instrument names carry NO unit or `_total` suffix: the
/// Prometheus exporter appends `_total` to counters and `_<unit>` to unit-
/// bearing instruments unconditionally (a counter named `x_total` scrapes as
/// `x_total_total`, a `_seconds` histogram with unit `s` as `x_seconds_seconds`),
/// and OTLP -> Prometheus translators append the same suffixes. Registering
/// `waddles_stage_latency` (unit `s`) therefore surfaces as the documented
/// `waddles_stage_latency_seconds` on every path.
pub fn instrument_name(documented: &str) -> &str {
    documented
        .strip_suffix("_total")
        .or_else(|| documented.strip_suffix("_seconds"))
        .unwrap_or(documented)
}

/// Host-call op names that may appear as the `op` label. Anything else is
/// folded to `"other"` -- the op string comes straight off the guest's
/// `host-call` frame and must never mint a new series.
pub const KNOWN_OPS: [&str; 25] = [
    "get",
    "set",
    "delete",
    "increment",
    "insert",
    "update",
    "query",
    "send",
    "enabled",
    "tier",
    "write",
    "now-rfc3339",
    "now-millis",
    "monotonic-nanos",
    "balance",
    "max_bet",
    "wager",
    "transfer",
    "leaderboard",
    "adjust",
    "resolve_actor",
    "resolve_mention",
    "list",
    "count",
    "other",
];

/// Error `code` values a host call returns when policy (the capability gate,
/// the egress guard, a quota) refused it, as opposed to an operational
/// failure. Folded into `result="denied"`; everything else non-ok is
/// `result="error"`.
const DENIED_CODES: [&str; 15] = [
    "not_granted",
    "quota_exceeded",
    "rate_limited",
    "resource_scope_mismatch",
    "user_not_in_scope",
    "delta_out_of_bounds",
    "amount_out_of_bounds",
    "unsupported_platform",
    "contains_pii",
    "instance_denied",
    "host_denylisted",
    "redirect_off_allowlist",
    "scheme_not_https",
    "feature_disabled",
    "egress_denied",
];

/// Terminal outcome of one delivered entry, the `result` label of
/// `waddles_stage_latency_seconds`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StageResult {
    /// The bundle replied and the reply was enqueued on the next stage.
    Delivered,
    /// The bundle returned no reply; the entry was acked and dropped.
    NoReply,
    /// A cross-app route was refused; the entry was acked and dropped.
    RouteDenied,
    /// The entry was dead-lettered (hop failure, no executor, trap, ...).
    DeadLettered,
}

impl StageResult {
    /// The stable label value for this outcome.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Delivered => "delivered",
            Self::NoReply => "no_reply",
            Self::RouteDenied => "route_denied",
            Self::DeadLettered => "dead_lettered",
        }
    }
}

/// Maps a host-call [`CapabilityKind`] plus its guest-supplied `op` string
/// to the bounded `(capability, op)` label pair. `db`-kind calls whose op is
/// namespaced (`reputation.*`, `economy.*`, `identity.*`) are attributed to
/// that namespace as their own capability, matching how the dispatcher
/// routes them.
pub fn host_call_labels(kind: CapabilityKind, op: &str) -> (&'static str, &'static str) {
    let (capability, bare_op) = match kind {
        CapabilityKind::Clock => ("clock", op),
        CapabilityKind::Context => ("context", op),
        CapabilityKind::Log => ("log", op),
        CapabilityKind::Kv => ("kv", op),
        CapabilityKind::Http => ("http", op),
        CapabilityKind::Flags => ("flags", op),
        CapabilityKind::Relay => ("relay", op),
        CapabilityKind::Db => match op.split_once('.') {
            Some(("reputation", rest)) => ("reputation", rest),
            Some(("economy", rest)) => ("economy", rest),
            Some(("identity", rest)) => ("identity", rest),
            _ => ("db", op),
        },
    };
    let op_label = KNOWN_OPS
        .iter()
        .copied()
        .find(|known| *known == bare_op)
        .unwrap_or("other");
    (capability, op_label)
}

/// Classifies a host call's outcome into the bounded `result` label:
/// `ok`, `denied` (policy refusal) or `error` (anything else).
pub fn host_call_result(error_code: Option<&str>) -> &'static str {
    match error_code {
        None => "ok",
        Some(code) if DENIED_CODES.contains(&code) => "denied",
        Some(_) => "error",
    }
}

/// Maps an executor-reported [`ErrorCode`] to the `limit` label of
/// `waddles_sandbox_trip_total`, or `None` when the code is not a sandbox
/// trip (an unknown-bundle or protocol error is not the sandbox firing).
pub fn sandbox_limit(code: ErrorCode) -> Option<&'static str> {
    match code {
        ErrorCode::ExecutorDeadline => Some("timeout"),
        ErrorCode::MemoryLimit => Some("memory"),
        ErrorCode::WasmTrap => Some("trap"),
        ErrorCode::HostCallDenied => Some("denied"),
        _ => None,
    }
}

/// The OTel instruments for this stage. Cheap to share (`Arc`); every record
/// method is non-blocking and infallible, so telemetry can never fail a
/// request.
pub struct StageMetrics {
    stage_latency: Histogram<f64>,
    executor_call: Histogram<f64>,
    host_call_latency: Histogram<f64>,
    bundle_load: Histogram<f64>,
    batch_size: Histogram<u64>,
    sandbox_trips: Counter<u64>,
    stream_events: Counter<u64>,
    stream_trimmed: Counter<u64>,
    stream_claimed: Counter<u64>,
    consumer_skipped: Counter<u64>,
    spine_dlq: Counter<u64>,
    queue_depth: Histogram<u64>,
    group_pending: Histogram<u64>,
    insecure_transport: Gauge<u64>,
    boundary_violations: Counter<u64>,
}

impl StageMetrics {
    /// Builds every instrument from `meter`. Production passes the global
    /// meter ([`global`]); tests pass a meter backed by an in-memory
    /// exporter so they can read back what was recorded.
    pub fn new(meter: &Meter) -> Self {
        let latency = |name: &'static str, description: &'static str| {
            meter
                .f64_histogram(instrument_name(name))
                .with_description(description)
                .with_unit("s")
                .with_boundaries(LATENCY_BUCKETS_S.to_vec())
                .build()
        };
        let count_hist = |name: &'static str, description: &'static str| {
            meter
                .u64_histogram(instrument_name(name))
                .with_description(description)
                .with_boundaries(COUNT_BUCKETS.to_vec())
                .build()
        };
        let counter = |name: &'static str, description: &'static str| {
            meter
                .u64_counter(instrument_name(name))
                .with_description(description)
                .build()
        };
        Self {
            stage_latency: latency(
                "waddles_stage_latency_seconds",
                "Take-to-terminal-outcome time for one delivered entry, by result",
            ),
            executor_call: latency(
                "waddles_executor_call_seconds",
                "One bundle-executor invoke as measured by the stage, by export and result",
            ),
            host_call_latency: latency(
                "waddles_host_call_latency_seconds",
                "One bundle host-capability call serviced by the stage, by capability/op/result",
            ),
            bundle_load: latency(
                "waddles_bundle_load_seconds",
                "Bundle load cost: stage-observed round trip (phase=load) and the \
                 executor-reported load time (phase=precompile)",
            ),
            batch_size: count_hist(
                "waddles_spine_batch_size",
                "Entries returned by one spine XREADGROUP drain pass",
            ),
            sandbox_trips: counter(
                "waddles_sandbox_trip_total",
                "Executor-reported sandbox trips, by limit (timeout/memory/trap/denied)",
            ),
            stream_events: counter(
                "waddles_stream_events_total",
                "Entries written to a spine stream (one per XADD of an ingest-source stream)",
            ),
            stream_trimmed: counter(
                "waddles_stream_trimmed_total",
                "XADDs that observed the stream at its approximate MAXLEN cap",
            ),
            stream_claimed: counter(
                "waddles_stream_claimed_total",
                "Entries recovered by XAUTOCLAIM from a dead consumer",
            ),
            consumer_skipped: counter(
                "waddles_consumer_skipped_total",
                "Entries read and acked without an executor call, by reason",
            ),
            spine_dlq: counter(
                "waddles_spine_dlq_total",
                "Dead-letter writes, by stage and the record's error kind",
            ),
            queue_depth: count_hist(
                "waddles_spine_queue_depth",
                "Consumer-group lag sampled from XINFO GROUPS",
            ),
            group_pending: count_hist(
                "waddles_group_pending",
                "Consumer-group PEL size sampled from XINFO GROUPS (the stuck-bundle signal)",
            ),
            insecure_transport: meter
                .u64_gauge(instrument_name("waddles_insecure_transport"))
                .with_description("1 when a component runs without TLS or auth, 0 when secure")
                .build(),
            boundary_violations: counter(
                "waddles_tenant_boundary_violations_total",
                "Hop-verification (tenant boundary) failures, by stage and reason",
            ),
        }
    }

    /// Records one delivered entry's total handling time and terminal
    /// outcome (`waddles_stage_latency_seconds`).
    pub fn record_stage(&self, app_id: &str, result: &'static str, elapsed: Duration) {
        self.stage_latency.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("result", result),
            ],
        );
    }

    /// Records one executor `invoke` as the stage experienced it
    /// (`waddles_executor_call_seconds`); `result` is `ok`, `executor_error`,
    /// `host_api_error` or `malformed`.
    pub fn record_executor_call(
        &self,
        app_id: &str,
        export: &'static str,
        result: &'static str,
        elapsed: Duration,
    ) {
        self.executor_call.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("export", export),
                KeyValue::new("result", result),
            ],
        );
    }

    /// Records one host-capability call (`waddles_host_call_latency_seconds`).
    /// `capability`/`op` must come from [`host_call_labels`] and `result`
    /// from [`host_call_result`] so the label sets stay closed.
    pub fn record_host_call(
        &self,
        capability: &'static str,
        op: &'static str,
        result: &'static str,
        elapsed: Duration,
    ) {
        self.host_call_latency.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("capability", capability),
                KeyValue::new("op", op),
                KeyValue::new("result", result),
            ],
        );
    }

    /// Records one bundle-load phase (`waddles_bundle_load_seconds`); `phase`
    /// is `load` or `precompile`, `result` is `ok` or `error`.
    pub fn record_bundle_load(
        &self,
        app_id: &str,
        phase: &'static str,
        result: &'static str,
        elapsed: Duration,
    ) {
        self.bundle_load.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("phase", phase),
                KeyValue::new("result", result),
            ],
        );
    }

    /// Records how many entries one drain pass returned
    /// (`waddles_spine_batch_size`). Empty passes (idle `XREADGROUP`
    /// timeouts) are recorded as `0` so the distribution shows idleness.
    pub fn record_batch(&self, size: usize) {
        self.batch_size.record(
            u64::try_from(size).unwrap_or(u64::MAX),
            &[KeyValue::new("stage", STAGE)],
        );
    }

    /// Counts one sandbox trip (`waddles_sandbox_trip_total`); `limit` comes
    /// from [`sandbox_limit`].
    pub fn record_sandbox_trip(&self, app_id: &str, limit: &'static str) {
        self.sandbox_trips.add(
            1,
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("limit", limit),
            ],
        );
    }
}

/// The real [`penguin_spine::SpineMetrics`] backend (replacing
/// `penguin_spine::NoopMetrics`): each spine callback lands on an OTel
/// instrument. Stream names and source ids are deliberately NOT labels --
/// they embed tenant/community slugs and channel identifiers, which are
/// both unbounded and identity-bearing.
impl penguin_spine::SpineMetrics for StageMetrics {
    fn stream_event_written(&self, platform: &str, _source_id: &str) {
        self.stream_events.add(
            1,
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("platform", platform.to_string()),
            ],
        );
    }

    fn stream_trimmed(&self, _stream: &str) {
        self.stream_trimmed.add(1, &[KeyValue::new("stage", STAGE)]);
    }

    fn stream_claimed(&self, app_id: &str) {
        self.stream_claimed.add(
            1,
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
            ],
        );
    }

    fn consumer_skipped(&self, app_id: &str, reason: &str) {
        self.consumer_skipped.add(
            1,
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("reason", reason.to_string()),
            ],
        );
    }

    fn dlq_written(&self, stage: &str, reason: &str) {
        self.spine_dlq.add(
            1,
            &[
                KeyValue::new("stage", stage.to_string()),
                KeyValue::new("reason", reason.to_string()),
            ],
        );
    }

    fn group_lag(&self, _app_id: &str, _stream: &str, lag: Option<u64>) {
        // `None` means the server reported no lag for the group -- not a
        // zero, so it is skipped rather than recorded as one.
        if let Some(lag) = lag {
            self.queue_depth
                .record(lag, &[KeyValue::new("stage", STAGE)]);
        }
    }

    fn group_pending(&self, app_id: &str, _stream: &str, pending: u64) {
        self.group_pending.record(
            pending,
            &[
                KeyValue::new("stage", STAGE),
                KeyValue::new("app_id", app_id.to_string()),
            ],
        );
    }

    fn insecure_transport(&self, component: &str, aspect: &str, insecure: bool) {
        self.insecure_transport.record(
            u64::from(insecure),
            &[
                KeyValue::new("component", component.to_string()),
                KeyValue::new("aspect", aspect.to_string()),
            ],
        );
    }

    fn tenant_boundary_violation(&self, stage: &str, reason: &str) {
        self.boundary_violations.add(
            1,
            &[
                KeyValue::new("stage", stage.to_string()),
                KeyValue::new("reason", reason.to_string()),
            ],
        );
    }
}

/// The process-wide [`StageMetrics`], built on first use from the global
/// meter provider. [`crate::run_with_shutdown`] calls this immediately after
/// [`crate::telemetry::init`] so the instruments bind to the provider
/// `penguin-logging` installed, never to the no-op default.
pub fn stage_metrics() -> Arc<StageMetrics> {
    static GLOBAL: OnceLock<Arc<StageMetrics>> = OnceLock::new();
    Arc::clone(GLOBAL.get_or_init(|| {
        #[cfg(test)]
        let meter = test_support::shared().meter();
        #[cfg(not(test))]
        let meter = opentelemetry::global::meter(SCOPE);
        Arc::new(StageMetrics::new(&meter))
    }))
}

/// In-memory OTel harness for tests: a real `SdkMeterProvider` whose
/// exports are captured so assertions run against the data a collector
/// would actually receive, not against a mock.
#[cfg(test)]
pub(crate) mod test_support {
    use std::collections::BTreeMap;
    use std::sync::OnceLock;

    use opentelemetry::metrics::{Meter, MeterProvider};
    use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
    use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};

    /// One exported data point, flattened for easy assertions.
    #[derive(Debug, Clone, PartialEq)]
    pub struct Point {
        /// Instrument name as exported.
        pub name: String,
        /// Attribute key -> stringified value.
        pub attrs: BTreeMap<String, String>,
        /// Histogram sample count, counter sum (as f64) or gauge value.
        pub value: f64,
        /// Histogram sample sum (0.0 for non-histograms).
        pub sum: f64,
        /// Histogram explicit bucket upper bounds (empty for non-histograms).
        pub bounds: Vec<f64>,
    }

    /// A private meter provider plus the exporter that captures it.
    pub struct Harness {
        provider: SdkMeterProvider,
        exporter: InMemoryMetricExporter,
    }

    impl Harness {
        /// A fresh, isolated provider.
        pub fn new() -> Self {
            let exporter = InMemoryMetricExporter::default();
            let provider = SdkMeterProvider::builder()
                .with_reader(PeriodicReader::builder(exporter.clone()).build())
                .build();
            Self { provider, exporter }
        }

        /// A meter on this harness's provider.
        pub fn meter(&self) -> Meter {
            self.provider.meter("test")
        }

        /// Flushes and returns every data point exported so far across all
        /// snapshots (cumulative temporality: later snapshots supersede
        /// earlier ones, so queries take the max/last).
        pub fn points(&self) -> Vec<Point> {
            self.provider
                .force_flush()
                .expect("in-memory meter provider flush must succeed");
            let finished = self
                .exporter
                .get_finished_metrics()
                .expect("in-memory exporter must be readable");
            let mut out = Vec::new();
            for resource in &finished {
                for scope in resource.scope_metrics() {
                    for metric in scope.metrics() {
                        flatten(metric.name(), metric.data(), &mut out);
                    }
                }
            }
            out
        }

        /// The highest histogram count / counter sum seen for the series
        /// whose documented (Prometheus-form) name is `name` -- looked up by
        /// its [`super::instrument_name`] -- and whose attributes include every `(key, value)` in
        /// `filter`; `0.0` when no such series was ever exported.
        pub fn count(&self, name: &str, filter: &[(&str, &str)]) -> f64 {
            self.points()
                .into_iter()
                .filter(|p| p.name == super::instrument_name(name) && matches(p, filter))
                .map(|p| p.value)
                .fold(0.0, f64::max)
        }

        /// The most recently exported point for `name` matching `filter`.
        pub fn last(&self, name: &str, filter: &[(&str, &str)]) -> Option<Point> {
            self.points()
                .into_iter()
                .rfind(|p| p.name == super::instrument_name(name) && matches(p, filter))
        }
    }

    fn matches(point: &Point, filter: &[(&str, &str)]) -> bool {
        filter
            .iter()
            .all(|(k, v)| point.attrs.get(*k).map(String::as_str) == Some(*v))
    }

    /// Lossy numeric widening to `f64` for the three OTel data-point types
    /// (`u64` has no `Into<f64>`).
    pub trait ToF64: Copy {
        /// The value as an `f64`.
        fn to_f64(self) -> f64;
    }
    impl ToF64 for f64 {
        fn to_f64(self) -> f64 {
            self
        }
    }
    impl ToF64 for u64 {
        fn to_f64(self) -> f64 {
            self as f64
        }
    }
    impl ToF64 for i64 {
        fn to_f64(self) -> f64 {
            self as f64
        }
    }

    fn attrs<'a>(
        iter: impl Iterator<Item = &'a opentelemetry::KeyValue>,
    ) -> BTreeMap<String, String> {
        iter.map(|kv| (kv.key.to_string(), kv.value.to_string()))
            .collect()
    }

    fn flatten(name: &str, data: &AggregatedMetrics, out: &mut Vec<Point>) {
        fn hist<T: ToF64>(
            name: &str,
            h: &opentelemetry_sdk::metrics::data::Histogram<T>,
            out: &mut Vec<Point>,
        ) {
            for dp in h.data_points() {
                out.push(Point {
                    name: name.to_string(),
                    attrs: attrs(dp.attributes()),
                    value: dp.count() as f64,
                    sum: dp.sum().to_f64(),
                    bounds: dp.bounds().collect(),
                });
            }
        }
        fn sum<T: ToF64>(
            name: &str,
            s: &opentelemetry_sdk::metrics::data::Sum<T>,
            out: &mut Vec<Point>,
        ) {
            for dp in s.data_points() {
                out.push(Point {
                    name: name.to_string(),
                    attrs: attrs(dp.attributes()),
                    value: dp.value().to_f64(),
                    sum: 0.0,
                    bounds: Vec::new(),
                });
            }
        }
        fn gauge<T: ToF64>(
            name: &str,
            g: &opentelemetry_sdk::metrics::data::Gauge<T>,
            out: &mut Vec<Point>,
        ) {
            for dp in g.data_points() {
                out.push(Point {
                    name: name.to_string(),
                    attrs: attrs(dp.attributes()),
                    value: dp.value().to_f64(),
                    sum: 0.0,
                    bounds: Vec::new(),
                });
            }
        }
        match data {
            AggregatedMetrics::F64(MetricData::Histogram(h)) => hist(name, h, out),
            AggregatedMetrics::F64(MetricData::Sum(s)) => sum(name, s, out),
            AggregatedMetrics::F64(MetricData::Gauge(g)) => gauge(name, g, out),
            AggregatedMetrics::U64(MetricData::Histogram(h)) => hist(name, h, out),
            AggregatedMetrics::U64(MetricData::Sum(s)) => sum(name, s, out),
            AggregatedMetrics::U64(MetricData::Gauge(g)) => gauge(name, g, out),
            AggregatedMetrics::I64(MetricData::Sum(s)) => sum(name, s, out),
            AggregatedMetrics::I64(MetricData::Gauge(g)) => gauge(name, g, out),
            _ => {}
        }
    }

    /// The process-wide harness backing [`super::stage_metrics`] in unit
    /// tests. Tests sharing it must filter on a unique `app_id`.
    pub fn shared() -> &'static Harness {
        static SHARED: OnceLock<Harness> = OnceLock::new();
        SHARED.get_or_init(Harness::new)
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::Harness;
    use super::*;
    use penguin_spine::SpineMetrics;

    fn fresh() -> (Harness, StageMetrics) {
        let harness = Harness::new();
        let metrics = StageMetrics::new(&harness.meter());
        (harness, metrics)
    }

    #[test]
    fn record_stage_emits_a_second_scaled_histogram_with_stage_app_and_result() {
        let (h, m) = fresh();
        m.record_stage("waddles.test.a", "delivered", Duration::from_millis(40));
        m.record_stage("waddles.test.a", "delivered", Duration::from_millis(60));
        m.record_stage("waddles.test.a", "dead_lettered", Duration::from_millis(5));

        let filter = [
            ("stage", "process"),
            ("app_id", "waddles.test.a"),
            ("result", "delivered"),
        ];
        assert_eq!(h.count("waddles_stage_latency_seconds", &filter), 2.0);
        let point = h
            .last("waddles_stage_latency_seconds", &filter)
            .expect("series must be exported");
        assert!((point.sum - 0.1).abs() < 1e-9, "sum was {}", point.sum);
        // Explicit second-scale boundaries, not the SDK's millisecond-shaped
        // defaults -- otherwise every sample lands in the first bucket.
        assert_eq!(point.bounds, LATENCY_BUCKETS_S.to_vec());
        assert_eq!(
            h.count(
                "waddles_stage_latency_seconds",
                &[("result", "dead_lettered")]
            ),
            1.0
        );
    }

    #[test]
    fn record_executor_call_labels_export_and_result() {
        let (h, m) = fresh();
        m.record_executor_call(
            "waddles.test.b",
            "transform",
            "ok",
            Duration::from_millis(3),
        );
        m.record_executor_call(
            "waddles.test.b",
            "transform",
            "executor_error",
            Duration::from_millis(2000),
        );
        assert_eq!(
            h.count(
                "waddles_executor_call_seconds",
                &[
                    ("export", "transform"),
                    ("result", "ok"),
                    ("stage", "process")
                ]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_executor_call_seconds",
                &[("app_id", "waddles.test.b"), ("result", "executor_error")]
            ),
            1.0
        );
    }

    #[test]
    fn record_host_call_labels_capability_op_and_result() {
        let (h, m) = fresh();
        m.record_host_call("kv", "get", "ok", Duration::from_micros(300));
        m.record_host_call("kv", "get", "denied", Duration::from_micros(20));
        assert_eq!(
            h.count(
                "waddles_host_call_latency_seconds",
                &[("capability", "kv"), ("op", "get"), ("result", "ok")]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_host_call_latency_seconds",
                &[("capability", "kv"), ("result", "denied")]
            ),
            1.0
        );
    }

    #[test]
    fn record_bundle_load_labels_phase_and_result() {
        let (h, m) = fresh();
        m.record_bundle_load("waddles.test.c", "load", "ok", Duration::from_millis(900));
        m.record_bundle_load(
            "waddles.test.c",
            "precompile",
            "ok",
            Duration::from_millis(850),
        );
        m.record_bundle_load("waddles.test.c", "load", "error", Duration::from_millis(10));
        for (phase, result, want) in [
            ("load", "ok", 1.0),
            ("precompile", "ok", 1.0),
            ("load", "error", 1.0),
        ] {
            assert_eq!(
                h.count(
                    "waddles_bundle_load_seconds",
                    &[
                        ("app_id", "waddles.test.c"),
                        ("phase", phase),
                        ("result", result)
                    ]
                ),
                want,
                "phase={phase} result={result}"
            );
        }
    }

    #[test]
    fn record_batch_emits_count_shaped_samples_including_idle_zero() {
        let (h, m) = fresh();
        m.record_batch(0);
        m.record_batch(7);
        let point = h
            .last("waddles_spine_batch_size", &[("stage", "process")])
            .expect("batch size must be exported");
        assert_eq!(point.value, 2.0);
        assert_eq!(point.sum, 7.0);
        assert_eq!(point.bounds, COUNT_BUCKETS.to_vec());
    }

    #[test]
    fn record_sandbox_trip_counts_by_limit() {
        let (h, m) = fresh();
        m.record_sandbox_trip("waddles.test.d", "timeout");
        m.record_sandbox_trip("waddles.test.d", "timeout");
        m.record_sandbox_trip("waddles.test.d", "memory");
        assert_eq!(
            h.count(
                "waddles_sandbox_trip_total",
                &[("app_id", "waddles.test.d"), ("limit", "timeout")]
            ),
            2.0
        );
        assert_eq!(
            h.count("waddles_sandbox_trip_total", &[("limit", "memory")]),
            1.0
        );
    }

    #[test]
    fn spine_callbacks_emit_without_stream_or_source_labels() {
        let (h, m) = fresh();
        m.stream_event_written("twitch", "tw-some-channel-login");
        m.stream_trimmed("waddles:t:acme:c:main:app:x:action");
        m.stream_claimed("waddles.test.e");
        m.consumer_skipped("waddles.test.e", "route_denied");
        m.dlq_written("process", "call_timeout");
        m.group_lag(
            "waddles.test.e",
            "waddles:t:acme:c:main:app:x:action",
            Some(12),
        );
        m.group_lag("waddles.test.e", "waddles:t:acme:c:main:app:x:action", None);
        m.group_pending("waddles.test.e", "waddles:t:acme:c:main:app:x:action", 3);
        m.insecure_transport("valkey", "tls", true);
        m.tenant_boundary_violation("process", "mac_mismatch");

        assert_eq!(
            h.count("waddles_stream_events_total", &[("platform", "twitch")]),
            1.0
        );
        assert_eq!(
            h.count("waddles_stream_trimmed_total", &[("stage", "process")]),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_stream_claimed_total",
                &[("app_id", "waddles.test.e")]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_consumer_skipped_total",
                &[("app_id", "waddles.test.e"), ("reason", "route_denied")]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_spine_dlq_total",
                &[("stage", "process"), ("reason", "call_timeout")]
            ),
            1.0
        );
        // `lag: None` (server reported nothing) must NOT be recorded as 0.
        let depth = h
            .last("waddles_spine_queue_depth", &[("stage", "process")])
            .expect("queue depth must be exported");
        assert_eq!((depth.value, depth.sum), (1.0, 12.0));
        let pending = h
            .last("waddles_group_pending", &[("app_id", "waddles.test.e")])
            .expect("group pending must be exported");
        assert_eq!((pending.value, pending.sum), (1.0, 3.0));
        let insecure = h
            .last(
                "waddles_insecure_transport",
                &[("component", "valkey"), ("aspect", "tls")],
            )
            .expect("insecure_transport gauge must be exported");
        assert_eq!(insecure.value, 1.0);
        assert_eq!(
            h.count(
                "waddles_tenant_boundary_violations_total",
                &[("stage", "process"), ("reason", "mac_mismatch")]
            ),
            1.0
        );

        // PII/cardinality guard: no exported series may carry a stream name,
        // channel login or source id as an attribute value.
        for point in h.points() {
            for (key, value) in &point.attrs {
                assert!(
                    !value.contains("waddles:t:") && !value.contains("tw-some-channel-login"),
                    "{} leaked {key}={value}",
                    point.name
                );
            }
        }
    }

    #[test]
    fn insecure_transport_gauge_reports_secure_as_an_explicit_zero() {
        let (h, m) = fresh();
        m.insecure_transport("valkey", "auth", true);
        m.insecure_transport("valkey", "auth", false);
        let point = h
            .last("waddles_insecure_transport", &[("aspect", "auth")])
            .expect("gauge must be exported");
        assert_eq!(point.value, 0.0, "0 must be reported, not an absent series");
    }

    #[test]
    fn every_documented_metric_name_is_actually_emitted() {
        let (h, m) = fresh();
        m.record_stage("a", "delivered", Duration::from_millis(1));
        m.record_executor_call("a", "transform", "ok", Duration::from_millis(1));
        m.record_host_call("kv", "get", "ok", Duration::from_millis(1));
        m.record_bundle_load("a", "load", "ok", Duration::from_millis(1));
        m.record_batch(1);
        m.record_sandbox_trip("a", "trap");
        m.stream_event_written("twitch", "s");
        m.stream_trimmed("s");
        m.stream_claimed("a");
        m.consumer_skipped("a", "filter");
        m.dlq_written("process", "bundle_error");
        m.group_lag("a", "s", Some(1));
        m.group_pending("a", "s", 1);
        m.insecure_transport("valkey", "tls", false);
        m.tenant_boundary_violation("process", "unknown_kid");

        let exported: std::collections::BTreeSet<String> =
            h.points().into_iter().map(|p| p.name).collect();
        for name in METRIC_NAMES {
            assert!(
                exported.contains(instrument_name(name)),
                "{name} (instrument {}) was never exported",
                instrument_name(name)
            );
        }
        // Verification Integrity: report the denominator, and fail on zero.
        assert_eq!(METRIC_NAMES.len(), 15);
        assert!(
            exported.len() >= METRIC_NAMES.len(),
            "examined {} exported series for {} documented names",
            exported.len(),
            METRIC_NAMES.len()
        );
    }

    #[test]
    fn instrument_names_drop_the_suffixes_the_prometheus_exporter_adds() {
        assert_eq!(
            instrument_name("waddles_stage_latency_seconds"),
            "waddles_stage_latency"
        );
        assert_eq!(
            instrument_name("waddles_sandbox_trip_total"),
            "waddles_sandbox_trip"
        );
        assert_eq!(
            instrument_name("waddles_spine_batch_size"),
            "waddles_spine_batch_size"
        );
        // Every documented name round-trips: a documented suffix is always
        // exactly one of the two the exporter would re-add.
        for name in METRIC_NAMES {
            let instrument = instrument_name(name);
            assert!(
                name == instrument
                    || *name == format!("{instrument}_total")
                    || *name == format!("{instrument}_seconds"),
                "{name} -> {instrument}"
            );
        }
    }

    #[test]
    fn host_call_labels_fold_unknown_and_hostile_ops_to_other() {
        for hostile in [
            "",
            "drop table users",
            "get; --",
            "GET",
            &"a".repeat(10_000),
            "reputation.",
            "économie.balance",
        ] {
            let (_, op) = host_call_labels(CapabilityKind::Kv, hostile);
            assert_eq!(op, "other", "op {hostile:?} must not become a label");
        }
        assert_eq!(host_call_labels(CapabilityKind::Kv, "get"), ("kv", "get"));
        assert_eq!(
            host_call_labels(CapabilityKind::Clock, "now-rfc3339"),
            ("clock", "now-rfc3339")
        );
    }

    #[test]
    fn host_call_labels_attribute_namespaced_db_ops_to_their_own_capability() {
        assert_eq!(
            host_call_labels(CapabilityKind::Db, "reputation.adjust"),
            ("reputation", "adjust")
        );
        assert_eq!(
            host_call_labels(CapabilityKind::Db, "economy.wager"),
            ("economy", "wager")
        );
        assert_eq!(
            host_call_labels(CapabilityKind::Db, "identity.resolve_actor"),
            ("identity", "resolve_actor")
        );
        assert_eq!(
            host_call_labels(CapabilityKind::Db, "insert"),
            ("db", "insert")
        );
        assert_eq!(
            host_call_labels(CapabilityKind::Db, "economy.drop_everything"),
            ("economy", "other")
        );
        for (kind, name) in [
            (CapabilityKind::Context, "context"),
            (CapabilityKind::Log, "log"),
            (CapabilityKind::Http, "http"),
            (CapabilityKind::Flags, "flags"),
            (CapabilityKind::Relay, "relay"),
        ] {
            assert_eq!(host_call_labels(kind, "x").0, name);
        }
    }

    #[test]
    fn host_call_result_separates_policy_denials_from_errors() {
        assert_eq!(host_call_result(None), "ok");
        for code in DENIED_CODES {
            assert_eq!(host_call_result(Some(code)), "denied", "{code}");
        }
        for code in ["backend", "invalid_args", "timeout", "not_implemented", ""] {
            assert_eq!(host_call_result(Some(code)), "error", "{code}");
        }
    }

    #[test]
    fn sandbox_limit_maps_only_sandbox_trips() {
        assert_eq!(sandbox_limit(ErrorCode::ExecutorDeadline), Some("timeout"));
        assert_eq!(sandbox_limit(ErrorCode::MemoryLimit), Some("memory"));
        assert_eq!(sandbox_limit(ErrorCode::WasmTrap), Some("trap"));
        assert_eq!(sandbox_limit(ErrorCode::HostCallDenied), Some("denied"));
        assert_eq!(sandbox_limit(ErrorCode::UnknownBundle), None);
        assert_eq!(sandbox_limit(ErrorCode::LoadFailed), None);
    }

    #[test]
    fn stage_result_labels_are_stable() {
        assert_eq!(StageResult::Delivered.as_str(), "delivered");
        assert_eq!(StageResult::NoReply.as_str(), "no_reply");
        assert_eq!(StageResult::RouteDenied.as_str(), "route_denied");
        assert_eq!(StageResult::DeadLettered.as_str(), "dead_lettered");
    }

    #[test]
    fn the_global_set_is_a_process_wide_singleton() {
        let a = stage_metrics();
        let b = stage_metrics();
        assert!(Arc::ptr_eq(&a, &b));
        a.record_stage(
            "waddles.test.singleton",
            "delivered",
            Duration::from_millis(1),
        );
        assert_eq!(
            test_support::shared().count(
                "waddles_stage_latency_seconds",
                &[("app_id", "waddles.test.singleton")]
            ),
            1.0
        );
    }
}

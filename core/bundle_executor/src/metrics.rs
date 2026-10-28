//! OTel instruments for the executor (spec §13.1, executor-side half): bundle
//! load cost by phase, invocation and guest-execution latency, host-call
//! round trips as the executor sees them, sandbox trips, resident-bundle and
//! in-flight gauges, and the host-API session's health (attempts, outages,
//! reconnects, heartbeat timeouts).
//!
//! Spec §13.2 says the executor creates no *spans* -- the stage records the
//! durations it returns -- and that stays true: this module is metrics only,
//! and every number here is measured by trusted host code around the guest,
//! never reported by it, so a hostile bundle cannot forge a series. The stage
//! still records its own view (`waddles_executor_call_seconds`, ...) in
//! `core/svc_process`/`core/svc_action`; the two views differ by exactly the
//! wire round trip and are deliberately both kept.
//!
//! **Label policy** (`rules/critical-rules.md` Observability): label values
//! are `&'static str` from closed vocabularies, or the stage-supplied
//! `app_id` of the bundle being loaded/invoked (the same bundle identity every
//! other platform metric already labels by). No op string, payload, user id,
//! host name or error message ever becomes a label.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

use opentelemetry::metrics::{Counter, Gauge, Histogram, Meter, UpDownCounter};
use opentelemetry::KeyValue;
use penguin_bundle_host::wire::ErrorCode;

/// Instrumentation scope name (`otel_scope_name` on the Prometheus surface).
pub const SCOPE: &str = "bundle_executor";

/// Latency bucket upper bounds, seconds: 0.5 ms to 30 s. Guest calls are
/// usually sub-millisecond; the SDK's default boundaries are
/// millisecond-shaped (`0, 5, 10 ...`) and would collapse them all into the
/// first bucket of a `_seconds` histogram.
pub const LATENCY_BUCKETS_S: [f64; 14] = [
    0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 30.0,
];

/// Bucket upper bounds, seconds, for the slow phases (bucket fetch, cold
/// compile -- spec §7.2 measured ~3-4 s for a large component, and a 21 MB
/// CPython component takes far longer) and for session/outage lengths.
pub const SLOW_BUCKETS_S: [f64; 13] = [
    0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 600.0,
];

/// Every instrument name this module registers -- asserted against emitted
/// data in tests and mirrored in `docs/observability/data-plane-metrics.md`.
pub const METRIC_NAMES: [&str; 21] = [
    "waddles_executor_load_seconds",
    "waddles_executor_loads_total",
    "waddles_executor_unloads_total",
    "waddles_executor_invoke_seconds",
    "waddles_executor_instantiate_seconds",
    "waddles_executor_guest_seconds",
    "waddles_executor_host_call_seconds",
    "waddles_executor_sandbox_trip_total",
    "waddles_executor_invocations_active",
    "waddles_executor_bundles_loaded",
    "waddles_executor_connection_attempts_total",
    "waddles_executor_connection_up",
    "waddles_executor_session_seconds",
    "waddles_executor_outage_seconds",
    "waddles_executor_reconnects_total",
    "waddles_executor_heartbeat_timeouts_total",
    "waddles_executor_orphaned_unloads_total",
    "waddles_executor_connector_calls_total",
    "waddles_executor_connector_call_seconds",
    "waddles_executor_wire_frames_shed_total",
    "waddles_executor_wire_backpressure_total",
];

/// The OTel instrument name behind a documented (Prometheus-form) metric
/// name. OTel instrument names carry NO unit or `_total` suffix: the
/// Prometheus exporter and the OTLP -> Prometheus translators append `_total`
/// to counters and `_<unit>` to unit-bearing instruments unconditionally (a
/// counter named `x_total` scrapes as `x_total_total`, a `_seconds` histogram
/// with unit `s` as `x_seconds_seconds`). Registering `waddles_executor_load`
/// (unit `s`) therefore surfaces as the documented
/// `waddles_executor_load_seconds` on every path.
pub fn instrument_name(documented: &str) -> &str {
    documented
        .strip_suffix("_total")
        .or_else(|| documented.strip_suffix("_seconds"))
        .unwrap_or(documented)
}

/// Error `code` values a stage returns for a host call that policy (the
/// capability gate, the egress guard, a quota) refused, as opposed to an
/// operational failure. Folded into `result="denied"`.
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

/// The stable label for an [`ErrorCode`] reported on a failed `invoke`
/// (`waddles_executor_invoke_seconds{result}`). Exhaustive on purpose: a new
/// wire error code must be given a label here, not silently bucketed.
pub fn error_code_label(code: ErrorCode) -> &'static str {
    match code {
        ErrorCode::ExecutorDeadline => "executor_deadline",
        ErrorCode::MemoryLimit => "memory_limit",
        ErrorCode::WasmTrap => "wasm_trap",
        ErrorCode::HostCallDenied => "host_call_denied",
        ErrorCode::HostCallFailed => "host_call_failed",
        ErrorCode::UnknownBundle => "unknown_bundle",
        ErrorCode::DigestMismatch => "digest_mismatch",
        ErrorCode::LoadFailed => "load_failed",
        ErrorCode::ExportMissing => "export_missing",
        ErrorCode::MalformedFrame => "malformed_frame",
        ErrorCode::ProtocolVersion => "protocol_version",
        ErrorCode::FrameTooLarge => "frame_too_large",
        ErrorCode::UnsandboxedExecutor => "unsandboxed_executor",
        ErrorCode::ShuttingDown => "shutting_down",
    }
}

/// The `limit` label of `waddles_executor_sandbox_trip_total` for an error
/// code, or `None` when the code is not the sandbox firing (an unknown
/// bundle or a malformed frame is not a trip).
pub fn sandbox_limit(code: ErrorCode) -> Option<&'static str> {
    match code {
        ErrorCode::ExecutorDeadline => Some("timeout"),
        ErrorCode::MemoryLimit => Some("memory"),
        ErrorCode::WasmTrap => Some("trap"),
        _ => None,
    }
}

/// Detail-aware `result` label for a failed `invoke`: like
/// [`error_code_label`], but the two `ExecutorDeadline` sub-cases the wire
/// code cannot express get their own bounded label -- `fuel_exhausted` (a
/// runaway guest hit its instruction budget) and `pool_exhausted`
/// (back-pressure: no free instance slot, NOT a guest fault) -- so neither
/// is indistinguishable from a wall-clock `executor_deadline` timeout.
pub fn error_label(code: ErrorCode, detail: Option<&str>) -> &'static str {
    match (code, detail) {
        (ErrorCode::ExecutorDeadline, Some("fuel_exhausted")) => "fuel_exhausted",
        (ErrorCode::ExecutorDeadline, Some("pool_exhausted")) => "pool_exhausted",
        _ => error_code_label(code),
    }
}

/// Detail-aware `limit` label for `waddles_executor_sandbox_trip_total`:
/// `fuel_exhausted` is its own `fuel` limit, and `pool_exhausted`
/// back-pressure is not a sandbox trip at all (`None`).
pub fn trip_limit(code: ErrorCode, detail: Option<&str>) -> Option<&'static str> {
    match (code, detail) {
        (ErrorCode::ExecutorDeadline, Some("fuel_exhausted")) => Some("fuel"),
        (ErrorCode::ExecutorDeadline, Some("pool_exhausted")) => None,
        _ => sandbox_limit(code),
    }
}

/// Classifies one host-call outcome into the bounded `result` label: `ok`,
/// `denied` (a stage-side policy refusal), `error` (any other stage-side
/// failure), `connection_lost` (no stage to answer) or `malformed` (the
/// reply was not a valid host-result).
pub fn host_call_result(outcome: &Result<(), &crate::error::ExecutorError>) -> &'static str {
    use crate::error::ExecutorError;
    match outcome {
        Ok(()) => "ok",
        Err(ExecutorError::HostCallDenied { code, .. })
            if DENIED_CODES.contains(&code.as_str()) =>
        {
            "denied"
        }
        Err(ExecutorError::HostCallDenied { .. }) => "error",
        Err(ExecutorError::ConnectionUnavailable) => "connection_lost",
        Err(ExecutorError::WriterBackpressure(_)) => "backpressure",
        Err(_) => "malformed",
    }
}

/// The executor's OTel instruments. Cheap to share (`Arc`); every record
/// method is non-blocking and infallible, so telemetry can never fail a call.
pub struct ExecutorMetrics {
    load_seconds: Histogram<f64>,
    loads_total: Counter<u64>,
    unloads_total: Counter<u64>,
    invoke_seconds: Histogram<f64>,
    instantiate_seconds: Histogram<f64>,
    guest_seconds: Histogram<f64>,
    host_call_seconds: Histogram<f64>,
    sandbox_trips: Counter<u64>,
    invocations_active: UpDownCounter<i64>,
    bundles_loaded: Gauge<u64>,
    connection_attempts: Counter<u64>,
    connection_up: Gauge<u64>,
    session_seconds: Histogram<f64>,
    outage_seconds: Histogram<f64>,
    reconnects: Counter<u64>,
    heartbeat_timeouts: Counter<u64>,
    orphaned_unloads: Counter<u64>,
    connector_calls: Counter<u64>,
    connector_call_seconds: Histogram<f64>,
    wire_frames_shed: Counter<u64>,
    wire_backpressure: Counter<u64>,
}

/// Keeps `waddles_executor_invocations_active` accurate on every exit path
/// of an invoke (early `?` return, trap, cancellation): created by
/// [`ExecutorMetrics::invocation_started`], it decrements on drop.
pub struct InvocationGuard {
    metrics: Arc<ExecutorMetrics>,
}

impl Drop for InvocationGuard {
    fn drop(&mut self) {
        self.metrics.invocations_active.add(-1, &[]);
    }
}

impl ExecutorMetrics {
    /// Builds every instrument from `meter`. Production passes the global
    /// meter ([`executor_metrics`]); tests pass a meter backed by an
    /// in-memory exporter so they can read back what was recorded.
    pub fn new(meter: &Meter) -> Self {
        let hist = |name: &'static str, description: &'static str, bounds: &[f64]| {
            meter
                .f64_histogram(instrument_name(name))
                .with_description(description)
                .with_unit("s")
                .with_boundaries(bounds.to_vec())
                .build()
        };
        let counter = |name: &'static str, description: &'static str| {
            meter
                .u64_counter(instrument_name(name))
                .with_description(description)
                .build()
        };
        Self {
            load_seconds: hist(
                "waddles_executor_load_seconds",
                "Bundle load cost by phase (fetch, verify, precompile) and end to end (total)",
                &SLOW_BUCKETS_S,
            ),
            loads_total: counter(
                "waddles_executor_loads_total",
                "Load requests by outcome (ok, already_resident, digest_mismatch, load_failed)",
            ),
            unloads_total: counter(
                "waddles_executor_unloads_total",
                "Unload requests by outcome (evicted, still_referenced, orphaned, unknown_bundle)",
            ),
            invoke_seconds: hist(
                "waddles_executor_invoke_seconds",
                "One invoke end to end inside the executor (lookup, instantiate, guest, encode)",
                &LATENCY_BUCKETS_S,
            ),
            instantiate_seconds: hist(
                "waddles_executor_instantiate_seconds",
                "Component instantiation time per invoke",
                &LATENCY_BUCKETS_S,
            ),
            guest_seconds: hist(
                "waddles_executor_guest_seconds",
                "Guest export execution time per invoke, host-call waits included",
                &LATENCY_BUCKETS_S,
            ),
            host_call_seconds: hist(
                "waddles_executor_host_call_seconds",
                "One host call round trip (executor -> stage -> executor), by capability/op/result",
                &LATENCY_BUCKETS_S,
            ),
            sandbox_trips: counter(
                "waddles_executor_sandbox_trip_total",
                "Sandbox trips by limit (timeout, memory, trap)",
            ),
            invocations_active: meter
                .i64_up_down_counter(instrument_name("waddles_executor_invocations_active"))
                .with_description("Invocations currently executing a guest")
                .build(),
            bundles_loaded: meter
                .u64_gauge(instrument_name("waddles_executor_bundles_loaded"))
                .with_description("Compiled components currently resident (distinct digests)")
                .build(),
            connection_attempts: counter(
                "waddles_executor_connection_attempts_total",
                "Host-API connection attempts by result (connected, failed)",
            ),
            connection_up: meter
                .u64_gauge(instrument_name("waddles_executor_connection_up"))
                .with_description("1 while the host-API session to the stage is established")
                .build(),
            session_seconds: hist(
                "waddles_executor_session_seconds",
                "Length of each established host-API session",
                &SLOW_BUCKETS_S,
            ),
            outage_seconds: hist(
                "waddles_executor_outage_seconds",
                "Time the host-API link was down between one session ending and the next connecting",
                &SLOW_BUCKETS_S,
            ),
            reconnects: counter(
                "waddles_executor_reconnects_total",
                "Times the dial loop went round again after a session ended or a dial failed",
            ),
            heartbeat_timeouts: counter(
                "waddles_executor_heartbeat_timeouts_total",
                "Sessions dropped because no frame arrived within the stale threshold",
            ),
            wire_frames_shed: counter(
                "waddles_executor_wire_frames_shed_total",
                "Sheddable (telemetry) frames dropped because the outbound writer queue was full",
            ),
            wire_backpressure: counter(
                "waddles_executor_wire_backpressure_total",
                "Must-deliver frames that hit a full writer queue, by outcome (waited, timeout)",
            ),
            orphaned_unloads: counter(
                "waddles_executor_orphaned_unloads_total",
                "Unloads naming a scope that did not reference the digest (duplicate or orphaned)",
            ),
            connector_calls: counter(
                "waddles_executor_connector_calls_total",
                "Connector-world export calls by export and outcome (ok, guest_error, fuel_exhausted, \
                 deadline, memory_limit, trap, pool_exhausted, instantiate_failed, internal)",
            ),
            connector_call_seconds: hist(
                "waddles_executor_connector_call_seconds",
                "Connector-world export call time (slot wait excluded), by export and outcome",
                &LATENCY_BUCKETS_S,
            ),
        }
    }

    /// Records one load phase (`waddles_executor_load_seconds`): `phase` is
    /// `fetch`, `verify`, `precompile` or `total`; `result` is `ok` or
    /// `error`.
    pub fn record_load_phase(
        &self,
        app_id: &str,
        phase: &'static str,
        result: &'static str,
        elapsed: Duration,
    ) {
        self.load_seconds.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("phase", phase),
                KeyValue::new("result", result),
            ],
        );
    }

    /// Counts one load request by outcome (`waddles_executor_loads_total`).
    pub fn record_load(&self, result: &'static str) {
        self.loads_total.add(1, &[KeyValue::new("result", result)]);
    }

    /// Counts one unload request by outcome (`waddles_executor_unloads_total`).
    /// An `orphaned` outcome also feeds `waddles_executor_orphaned_unloads_total`.
    pub fn record_unload(&self, result: &'static str) {
        self.unloads_total
            .add(1, &[KeyValue::new("result", result)]);
        if result == "orphaned" {
            self.orphaned_unloads.add(1, &[]);
        }
    }

    /// Sets the resident-component gauge (`waddles_executor_bundles_loaded`).
    pub fn set_bundles_loaded(&self, resident_digests: usize) {
        self.bundles_loaded
            .record(u64::try_from(resident_digests).unwrap_or(u64::MAX), &[]);
    }

    /// Marks one invocation as executing; the returned guard marks it done
    /// when dropped (`waddles_executor_invocations_active`).
    pub fn invocation_started(self: &Arc<Self>) -> InvocationGuard {
        self.invocations_active.add(1, &[]);
        InvocationGuard {
            metrics: Arc::clone(self),
        }
    }

    /// Records one invoke end to end (`waddles_executor_invoke_seconds`);
    /// `result` is `ok` or an [`error_code_label`]. A sandbox-trip result
    /// also bumps `waddles_executor_sandbox_trip_total`.
    pub fn record_invoke(
        &self,
        app_id: &str,
        export: &'static str,
        result: &'static str,
        trip_limit: Option<&'static str>,
        elapsed: Duration,
    ) {
        self.invoke_seconds.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("export", export),
                KeyValue::new("result", result),
            ],
        );
        if let Some(limit) = trip_limit {
            self.sandbox_trips.add(
                1,
                &[
                    KeyValue::new("app_id", app_id.to_string()),
                    KeyValue::new("limit", limit),
                ],
            );
        }
    }

    /// Records component instantiation time for one invoke
    /// (`waddles_executor_instantiate_seconds`).
    pub fn record_instantiate(&self, app_id: &str, elapsed: Duration) {
        self.instantiate_seconds.record(
            elapsed.as_secs_f64(),
            &[KeyValue::new("app_id", app_id.to_string())],
        );
    }

    /// Records guest export execution time for one invoke
    /// (`waddles_executor_guest_seconds`).
    pub fn record_guest(&self, app_id: &str, export: &'static str, elapsed: Duration) {
        self.guest_seconds.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("app_id", app_id.to_string()),
                KeyValue::new("export", export),
            ],
        );
    }

    /// Records one host-call round trip (`waddles_executor_host_call_seconds`).
    /// `capability`/`op` are the executor's own `&'static str` constants and
    /// `result` comes from [`host_call_result`].
    pub fn record_host_call(
        &self,
        capability: &'static str,
        op: &'static str,
        result: &'static str,
        elapsed: Duration,
    ) {
        self.host_call_seconds.record(
            elapsed.as_secs_f64(),
            &[
                KeyValue::new("capability", capability),
                KeyValue::new("op", op),
                KeyValue::new("result", result),
            ],
        );
    }

    /// Records one connector-world export call
    /// (`waddles_executor_connector_calls_total` +
    /// `waddles_executor_connector_call_seconds`). `export` is the WIT
    /// function name and `outcome` a [`crate::connector::ConnectorCallError::kind`]
    /// label or `ok` -- both `&'static str` constants, never guest- or
    /// platform-supplied, so the label set is bounded. Deliberately carries
    /// no connection/source id: that would be an unbounded-cardinality label.
    pub fn record_connector_call(
        &self,
        app_id: &str,
        export: &'static str,
        outcome: &'static str,
        elapsed: Duration,
    ) {
        let attrs = [
            KeyValue::new("app_id", app_id.to_string()),
            KeyValue::new("export", export),
            KeyValue::new("outcome", outcome),
        ];
        self.connector_calls.add(1, &attrs);
        self.connector_call_seconds
            .record(elapsed.as_secs_f64(), &attrs);
    }

    /// Counts one host-API connection attempt (`connected` or `failed`).
    pub fn record_connection_attempt(&self, result: &'static str) {
        self.connection_attempts
            .add(1, &[KeyValue::new("result", result)]);
    }

    /// Sets the session gauge (`waddles_executor_connection_up`): `1` while
    /// connected, an explicit `0` otherwise so "down" and "not reporting" are
    /// distinguishable.
    pub fn set_connection_up(&self, up: bool) {
        self.connection_up.record(u64::from(up), &[]);
    }

    /// Records the length of a session that just ended
    /// (`waddles_executor_session_seconds`).
    pub fn record_session(&self, elapsed: Duration) {
        self.session_seconds.record(elapsed.as_secs_f64(), &[]);
    }

    /// Records how long the link was down before this connection
    /// (`waddles_executor_outage_seconds`).
    pub fn record_outage(&self, elapsed: Duration) {
        self.outage_seconds.record(elapsed.as_secs_f64(), &[]);
    }

    /// Counts one trip round the dial loop (`waddles_executor_reconnects_total`).
    pub fn record_reconnect(&self) {
        self.reconnects.add(1, &[]);
    }

    /// Counts one session dropped as stale
    /// (`waddles_executor_heartbeat_timeouts_total`).
    pub fn record_heartbeat_timeout(&self) {
        self.heartbeat_timeouts.add(1, &[]);
    }

    /// Counts one sheddable frame dropped on a full writer queue
    /// (`waddles_executor_wire_frames_shed_total`); `class` is a static
    /// capability name, never guest-supplied.
    pub fn record_frame_shed(&self, class: &'static str) {
        self.wire_frames_shed
            .add(1, &[KeyValue::new("class", class)]);
    }

    /// Counts one must-deliver frame that met a full writer queue
    /// (`waddles_executor_wire_backpressure_total`); `outcome` is `waited`
    /// or `timeout`.
    pub fn record_wire_backpressure(&self, outcome: &'static str) {
        self.wire_backpressure
            .add(1, &[KeyValue::new("outcome", outcome)]);
    }
}

/// The process-wide [`ExecutorMetrics`], built on first use from the global
/// meter provider [`crate::telemetry::init_metrics`] installs. `crate::run`
/// calls `init_metrics` first, so the instruments bind to that provider and
/// never to the no-op default.
pub fn executor_metrics() -> Arc<ExecutorMetrics> {
    static GLOBAL: OnceLock<Arc<ExecutorMetrics>> = OnceLock::new();
    Arc::clone(GLOBAL.get_or_init(|| {
        #[cfg(test)]
        let meter = test_support::shared().meter();
        #[cfg(not(test))]
        let meter = opentelemetry::global::meter(SCOPE);
        Arc::new(ExecutorMetrics::new(&meter))
    }))
}

/// In-memory OTel harness for tests: a real `SdkMeterProvider` whose exports
/// are captured, so assertions run against the data a collector would
/// actually receive, not against a mock.
#[cfg(test)]
pub(crate) mod test_support {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
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
        /// Histogram sample count, counter sum or gauge value.
        pub value: f64,
        /// Histogram sample sum (0.0 for non-histograms).
        pub sum: f64,
        /// Histogram explicit bucket bounds (empty for non-histograms).
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

    /// The process-wide harness backing [`super::executor_metrics`] in unit
    /// tests. Tests sharing it must filter on a unique `app_id`.
    pub fn shared() -> &'static Harness {
        static SHARED: OnceLock<Harness> = OnceLock::new();
        SHARED.get_or_init(Harness::new)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::test_support::Harness;
    use super::*;
    use crate::error::ExecutorError;

    fn fresh() -> (Harness, Arc<ExecutorMetrics>) {
        let harness = Harness::new();
        let metrics = Arc::new(ExecutorMetrics::new(&harness.meter()));
        (harness, metrics)
    }

    #[test]
    fn load_phases_and_outcomes_are_recorded_with_second_scaled_buckets() {
        let (h, m) = fresh();
        m.record_load_phase("waddles.t.a", "fetch", "ok", Duration::from_millis(120));
        m.record_load_phase("waddles.t.a", "verify", "ok", Duration::from_millis(5));
        m.record_load_phase("waddles.t.a", "precompile", "error", Duration::from_secs(3));
        m.record_load_phase("waddles.t.a", "total", "ok", Duration::from_secs(4));
        m.record_load("ok");
        m.record_load("already_resident");
        m.record_load("already_resident");

        for (phase, result) in [
            ("fetch", "ok"),
            ("verify", "ok"),
            ("precompile", "error"),
            ("total", "ok"),
        ] {
            assert_eq!(
                h.count(
                    "waddles_executor_load_seconds",
                    &[
                        ("app_id", "waddles.t.a"),
                        ("phase", phase),
                        ("result", result)
                    ]
                ),
                1.0,
                "phase={phase}"
            );
        }
        let fetch = h
            .last("waddles_executor_load_seconds", &[("phase", "fetch")])
            .expect("fetch exported");
        assert!((fetch.sum - 0.12).abs() < 1e-9);
        assert_eq!(fetch.bounds, SLOW_BUCKETS_S.to_vec());
        assert_eq!(
            h.count(
                "waddles_executor_loads_total",
                &[("result", "already_resident")]
            ),
            2.0
        );
    }

    #[test]
    fn unloads_count_by_outcome_and_orphans_also_feed_their_own_counter() {
        let (h, m) = fresh();
        m.record_unload("evicted");
        m.record_unload("still_referenced");
        m.record_unload("orphaned");
        m.record_unload("orphaned");
        m.record_unload("unknown_bundle");
        for (result, want) in [
            ("evicted", 1.0),
            ("still_referenced", 1.0),
            ("orphaned", 2.0),
            ("unknown_bundle", 1.0),
        ] {
            assert_eq!(
                h.count("waddles_executor_unloads_total", &[("result", result)]),
                want,
                "{result}"
            );
        }
        assert_eq!(h.count("waddles_executor_orphaned_unloads_total", &[]), 2.0);
    }

    #[test]
    fn invoke_records_latency_and_a_sandbox_trip_only_when_one_occurred() {
        let (h, m) = fresh();
        m.record_invoke(
            "waddles.t.b",
            "transform",
            "ok",
            None,
            Duration::from_micros(300),
        );
        m.record_invoke(
            "waddles.t.b",
            "transform",
            "executor_deadline",
            Some("timeout"),
            Duration::from_millis(500),
        );
        m.record_instantiate("waddles.t.b", Duration::from_micros(80));
        m.record_guest("waddles.t.b", "transform", Duration::from_micros(200));

        assert_eq!(
            h.count(
                "waddles_executor_invoke_seconds",
                &[
                    ("app_id", "waddles.t.b"),
                    ("export", "transform"),
                    ("result", "ok")
                ]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_executor_invoke_seconds",
                &[("result", "executor_deadline")]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_executor_sandbox_trip_total",
                &[("app_id", "waddles.t.b"), ("limit", "timeout")]
            ),
            1.0
        );
        // The ok invoke tripped nothing: exactly one trip series sample.
        assert_eq!(h.count("waddles_executor_sandbox_trip_total", &[]), 1.0);
        let invoke = h
            .last("waddles_executor_invoke_seconds", &[("result", "ok")])
            .expect("invoke exported");
        assert_eq!(invoke.bounds, LATENCY_BUCKETS_S.to_vec());
        assert_eq!(
            h.count(
                "waddles_executor_instantiate_seconds",
                &[("app_id", "waddles.t.b")]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_executor_guest_seconds",
                &[("app_id", "waddles.t.b"), ("export", "transform")]
            ),
            1.0
        );
    }

    #[test]
    fn the_in_flight_guard_decrements_on_every_exit_path() {
        let (h, m) = fresh();
        let g1 = m.invocation_started();
        let g2 = m.invocation_started();
        assert_eq!(
            h.last("waddles_executor_invocations_active", &[])
                .expect("exported")
                .value,
            2.0
        );
        drop(g1);
        assert_eq!(
            h.last("waddles_executor_invocations_active", &[])
                .expect("exported")
                .value,
            1.0
        );
        // An early return / panic unwinding drops the guard just the same.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _g3 = m.invocation_started();
            panic!("guest bridge blew up");
        }));
        assert!(result.is_err());
        drop(g2);
        assert_eq!(
            h.last("waddles_executor_invocations_active", &[])
                .expect("exported")
                .value,
            0.0,
            "every guard, panicking path included, must give its slot back"
        );
    }

    #[test]
    fn gauges_report_explicit_values_including_zero() {
        let (h, m) = fresh();
        m.set_bundles_loaded(3);
        m.set_bundles_loaded(0);
        m.set_connection_up(true);
        m.set_connection_up(false);
        assert_eq!(
            h.last("waddles_executor_bundles_loaded", &[])
                .expect("exported")
                .value,
            0.0
        );
        assert_eq!(
            h.last("waddles_executor_connection_up", &[])
                .expect("exported")
                .value,
            0.0,
            "down must scrape as an explicit 0, not as an absent series"
        );
    }

    #[test]
    fn session_health_series_are_recorded() {
        let (h, m) = fresh();
        m.record_connection_attempt("connected");
        m.record_connection_attempt("failed");
        m.record_connection_attempt("failed");
        m.record_session(Duration::from_secs(90));
        m.record_outage(Duration::from_secs(7));
        m.record_reconnect();
        m.record_heartbeat_timeout();
        assert_eq!(
            h.count(
                "waddles_executor_connection_attempts_total",
                &[("result", "failed")]
            ),
            2.0
        );
        assert_eq!(
            h.last("waddles_executor_session_seconds", &[])
                .expect("exported")
                .sum,
            90.0
        );
        assert_eq!(
            h.last("waddles_executor_outage_seconds", &[])
                .expect("exported")
                .sum,
            7.0
        );
        assert_eq!(h.count("waddles_executor_reconnects_total", &[]), 1.0);
        assert_eq!(
            h.count("waddles_executor_heartbeat_timeouts_total", &[]),
            1.0
        );
    }

    #[test]
    fn host_calls_are_labeled_by_capability_op_and_result() {
        let (h, m) = fresh();
        m.record_host_call("kv", "get", "ok", Duration::from_micros(400));
        m.record_host_call("kv", "get", "denied", Duration::from_micros(30));
        assert_eq!(
            h.count(
                "waddles_executor_host_call_seconds",
                &[("capability", "kv"), ("op", "get"), ("result", "ok")]
            ),
            1.0
        );
        assert_eq!(
            h.count(
                "waddles_executor_host_call_seconds",
                &[("capability", "kv"), ("result", "denied")]
            ),
            1.0
        );
    }

    #[test]
    fn every_documented_metric_name_is_actually_emitted() {
        let (h, m) = fresh();
        m.record_load_phase("a", "total", "ok", Duration::from_millis(1));
        m.record_load("ok");
        m.record_unload("orphaned");
        m.record_invoke(
            "a",
            "transform",
            "wasm_trap",
            Some("trap"),
            Duration::from_millis(1),
        );
        m.record_instantiate("a", Duration::from_millis(1));
        m.record_guest("a", "transform", Duration::from_millis(1));
        m.record_host_call("kv", "get", "ok", Duration::from_millis(1));
        let _active = m.invocation_started();
        m.set_bundles_loaded(1);
        m.record_connection_attempt("connected");
        m.set_connection_up(true);
        m.record_session(Duration::from_secs(1));
        m.record_outage(Duration::from_secs(1));
        m.record_reconnect();
        m.record_heartbeat_timeout();
        m.record_connector_call("a", "on-frame", "ok", Duration::from_millis(1));
        m.record_frame_shed("log");
        m.record_wire_backpressure("waited");

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
        assert_eq!(METRIC_NAMES.len(), 21);
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
            instrument_name("waddles_executor_load_seconds"),
            "waddles_executor_load"
        );
        assert_eq!(
            instrument_name("waddles_executor_sandbox_trip_total"),
            "waddles_executor_sandbox_trip"
        );
        assert_eq!(
            instrument_name("waddles_executor_bundles_loaded"),
            "waddles_executor_bundles_loaded"
        );
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
    fn fuel_and_pool_exhaustion_get_distinct_labels_and_back_pressure_is_no_trip() {
        let d = ErrorCode::ExecutorDeadline;
        assert_eq!(error_label(d, None), "executor_deadline");
        assert_eq!(error_label(d, Some("fuel_exhausted")), "fuel_exhausted");
        assert_eq!(error_label(d, Some("pool_exhausted")), "pool_exhausted");
        assert_eq!(error_label(ErrorCode::WasmTrap, Some("x")), "wasm_trap");
        assert_eq!(trip_limit(d, None), Some("timeout"));
        assert_eq!(trip_limit(d, Some("fuel_exhausted")), Some("fuel"));
        assert_eq!(
            trip_limit(d, Some("pool_exhausted")),
            None,
            "pool back-pressure is not a sandbox trip"
        );
        assert_eq!(trip_limit(ErrorCode::MemoryLimit, None), Some("memory"));
    }

    #[test]
    fn error_code_labels_are_distinct_and_sandbox_limits_cover_only_trips() {
        let codes = [
            ErrorCode::ExecutorDeadline,
            ErrorCode::MemoryLimit,
            ErrorCode::WasmTrap,
            ErrorCode::HostCallDenied,
            ErrorCode::HostCallFailed,
            ErrorCode::UnknownBundle,
            ErrorCode::DigestMismatch,
            ErrorCode::LoadFailed,
            ErrorCode::ExportMissing,
            ErrorCode::MalformedFrame,
            ErrorCode::ProtocolVersion,
            ErrorCode::FrameTooLarge,
            ErrorCode::UnsandboxedExecutor,
            ErrorCode::ShuttingDown,
        ];
        let labels: std::collections::BTreeSet<&str> =
            codes.iter().map(|c| error_code_label(*c)).collect();
        assert_eq!(labels.len(), codes.len(), "every code needs its own label");
        assert_eq!(sandbox_limit(ErrorCode::ExecutorDeadline), Some("timeout"));
        assert_eq!(sandbox_limit(ErrorCode::MemoryLimit), Some("memory"));
        assert_eq!(sandbox_limit(ErrorCode::WasmTrap), Some("trap"));
        for code in [
            ErrorCode::UnknownBundle,
            ErrorCode::LoadFailed,
            ErrorCode::HostCallDenied,
            ErrorCode::MalformedFrame,
        ] {
            assert_eq!(sandbox_limit(code), None, "{code:?} is not a sandbox trip");
        }
    }

    #[test]
    fn host_call_results_separate_policy_denials_from_failures() {
        let denied = |code: &str| ExecutorError::HostCallDenied {
            capability: "kv",
            op: "get",
            code: code.to_string(),
            message: "m".to_string(),
        };
        assert_eq!(host_call_result(&Ok(())), "ok");
        for code in DENIED_CODES {
            assert_eq!(host_call_result(&Err(&denied(code))), "denied", "{code}");
        }
        assert_eq!(host_call_result(&Err(&denied("backend"))), "error");
        assert_eq!(
            host_call_result(&Err(&ExecutorError::ConnectionUnavailable)),
            "connection_lost"
        );
        assert_eq!(
            host_call_result(&Err(&ExecutorError::UnexpectedFrame("x"))),
            "malformed"
        );
    }

    #[test]
    fn the_global_set_is_a_process_wide_singleton() {
        let a = executor_metrics();
        let b = executor_metrics();
        assert!(Arc::ptr_eq(&a, &b));
        a.record_load_phase(
            "waddles.test.singleton",
            "total",
            "ok",
            Duration::from_millis(1),
        );
        assert_eq!(
            test_support::shared().count(
                "waddles_executor_load_seconds",
                &[("app_id", "waddles.test.singleton")]
            ),
            1.0
        );
    }
}

//! Telemetry bootstrap: delegates `tracing` + sanitizing OTel logs/traces/
//! metrics entirely to `penguin-logging` (spec §4.9), keeping only this
//! service's own Prometheus request metrics (`RequestMetrics`) local, since
//! those are svc-action-specific counters, not part of the shared crate's
//! surface. Mirrors `core/svc_process/src/telemetry.rs` (the M4 reference,
//! itself the second half of the penguin-libs dependency pattern established
//! there) exactly, with the `svc_process` metric-name prefix swapped for
//! `svc_action`.
//!
//! `penguin-logging` reads the exact same standard OTLP environment
//! variables this module used to read by hand
//! (`OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`,
//! `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, plus `LOG_LEVEL`) --
//! see `rules/critical-rules.md` Observability (OTel) -- and preserves the
//! same graceful-degradation contract: an unset `OTEL_EXPORTER_OTLP_ENDPOINT`
//! skips OTLP export entirely (stdout JSON + Prometheus `/metrics` still
//! work), and a failed exporter build downgrades rather than panicking or
//! propagating. Closes the "no Rust penguin logging crate exists yet" gap
//! recorded in `rules/backend-rust.md`.

/// Re-exported so callers (`crate::run_with_shutdown`) hold the guard type
/// without needing to depend on `penguin_logging` directly for it.
pub use penguin_logging::TelemetryGuard;

/// Initializes structured logging + OTel logs/traces/metrics via
/// `penguin_logging::init`, and returns the guard plus a fresh Prometheus
/// [`prometheus::Registry`] for this service's own `/metrics` HTTP surface.
/// Must be called exactly once, before any other `tracing` macro use.
///
/// `penguin_logging::init` also returns a `LevelHandle` for runtime
/// log-level changes; this service doesn't yet expose a control-plane
/// endpoint to use it, so it is intentionally dropped here rather than
/// plumbed through unused.
pub fn init(default_service_name: &str) -> (TelemetryGuard, prometheus::Registry) {
    let cfg = penguin_logging::ServiceConfig::from_env(default_service_name);
    let (guard, _level_handle, registry) = penguin_logging::init(cfg);
    (guard, registry)
}

/// Renders the Prometheus text-format exposition body for `/metrics`.
pub fn render_metrics(registry: &prometheus::Registry) -> anyhow::Result<String> {
    use prometheus::Encoder;
    let metric_families = registry.gather();
    let mut buf = Vec::new();
    prometheus::TextEncoder::new().encode(&metric_families, &mut buf)?;
    Ok(String::from_utf8(buf)?)
}

/// Base HTTP request metrics registered once against the Prometheus
/// registry and shared via [`crate::http::AppState`] so the request-path
/// middleware can record into them without re-registering (a
/// `prometheus::Registry` panics on duplicate registration). Histograms
/// for load/latency come first per `rules/critical-rules.md`
/// Observability -- a lone request counter is not instrumentation.
#[derive(Clone)]
pub struct RequestMetrics {
    pub http_requests_total: prometheus::IntCounterVec,
    pub http_request_duration_seconds: prometheus::HistogramVec,
}

/// Registers the service's base Prometheus metrics (an `up` gauge plus a
/// per-request counter and latency histogram, both labeled by
/// method/path/status where applicable) against `registry` and returns
/// handles for request-path code to record into. Must be called exactly
/// once per `registry` -- see [`crate::http::AppState::new`].
pub fn register_request_metrics(registry: &prometheus::Registry) -> RequestMetrics {
    let up = prometheus::IntGauge::new("svc_action_up", "1 if the process is running")
        .expect("valid metric definition");
    registry
        .register(Box::new(up.clone()))
        .expect("register svc_action_up");
    up.set(1);

    let http_requests_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_http_requests_total",
            "Total HTTP requests handled, labeled by method/path/status",
        ),
        &["method", "path", "status"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_requests_total.clone()))
        .expect("register svc_action_http_requests_total");

    let http_request_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_action_http_request_duration_seconds",
            "HTTP request duration in seconds, labeled by method/path",
        ),
        &["method", "path"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_request_duration_seconds.clone()))
        .expect("register svc_action_http_request_duration_seconds");

    RequestMetrics {
        http_requests_total,
        http_request_duration_seconds,
    }
}

/// Registers `waddles_egress_denied_total{app_id,reason}` (spec §8.2: "Each
/// step that rejects ... increments `waddles_egress_denied_total{app_id,
/// reason}`") against `registry` -- called once at startup and shared into
/// `crate::egress::EgressGuard`. Separate from [`register_request_metrics`]
/// because it is registered before `crate::http::AppState` exists (the
/// host-API listener and its `EgressGuard` start before the control-plane
/// router is built) -- see `crate::try_start_host_api`.
pub fn register_egress_metrics(registry: &prometheus::Registry) -> prometheus::IntCounterVec {
    let denied_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_egress_denied_total",
            "Bundle http.send calls denied or rate-limited by the egress guard, by app_id/reason",
        ),
        &["app_id", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(denied_total.clone()))
        .expect("register svc_action_egress_denied_total");
    denied_total
}

/// Prometheus handles for `crate::host_api`'s heartbeat/dead-letter
/// visibility (fix/executor-link-heartbeat, alpha 2026-10-02 incident: a
/// rolled svc pod left the executor bound to a terminated peer with zero
/// observable signal -- every pod showed `Running`/`Ready` while silently
/// dead-lettering everything). `connected_executors` is a gauge (0 or 1 in
/// this M3/M4 single-active-connection model -- see `ConnectionRegistry`'s
/// own `TODO(M3+)` on multiplexing several connections); the two counters
/// are monotonic so a dashboard can alert on rate-of-change, not just the
/// current value. Mirrors `core/svc_process::telemetry::HostApiMetrics`
/// field-for-field, same metric names (unprefixed -- shared across both
/// stages so one dashboard panel covers both).
#[derive(Clone)]
pub struct HostApiMetrics {
    pub connected_executors: prometheus::IntGauge,
    pub heartbeat_timeouts_total: prometheus::IntCounter,
    pub dead_lettered_no_executor_total: prometheus::IntCounter,
}

/// Registers [`HostApiMetrics`]. Must be called exactly once per `registry`
/// -- see [`register_request_metrics`]'s identical constraint.
pub fn register_host_api_metrics(registry: &prometheus::Registry) -> HostApiMetrics {
    let connected_executors = prometheus::IntGauge::new(
        "host_api_connected_executors",
        "Number of live bundle-executor sessions this stage currently holds (0 or 1 in the \
         current single-active-connection model)",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(connected_executors.clone()))
        .expect("register host_api_connected_executors");

    let heartbeat_timeouts_total = prometheus::IntCounter::new(
        "host_api_heartbeat_timeouts_total",
        "Executor sessions dropped after missing HEARTBEAT_MISSED_LIMIT consecutive heartbeats",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(heartbeat_timeouts_total.clone()))
        .expect("register host_api_heartbeat_timeouts_total");

    let dead_lettered_no_executor_total = prometheus::IntCounter::new(
        "dispatch_dead_lettered_no_executor_total",
        "Dispatch entries dead-lettered because no executor connection was available",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(dead_lettered_no_executor_total.clone()))
        .expect("register dispatch_dead_lettered_no_executor_total");

    HostApiMetrics {
        connected_executors,
        heartbeat_timeouts_total,
        dead_lettered_no_executor_total,
    }
}

/// Ops-visibility fix (security review): the DB-driven active-bundle
/// loader (`crate::bundle_loader`) previously only logged when
/// `bundle_active_set::read_active_set` excluded an active row (no current
/// approval / missing digest / referential-integrity gap) -- a feature
/// silently going dark (e.g. an approval expiring with nothing
/// re-approving it) is easy to miss in a log stream alone. Incremented
/// once per excluded row by `bundle_loader::run_tick`, labeled by
/// `app_id`/`reason` (`bundle_active_set::ExclusionReason::as_str`), same
/// shape as [`register_egress_metrics`] above.
pub fn register_bundle_loader_excluded_metrics(
    registry: &prometheus::Registry,
) -> prometheus::IntCounterVec {
    let excluded_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_bundle_active_set_excluded_total",
            "Active app_active_versions rows excluded from the DB-driven bundle loader's \
             active set, by app_id/reason",
        ),
        &["app_id", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(excluded_total.clone()))
        .expect("register svc_action_bundle_active_set_excluded_total");
    excluded_total
}

/// Prometheus handles for `crate::dispatch_supervisor` (the multi-tenant,
/// per-app dispatch-consumer supervisor -- regression: svc-action had no
/// multi-tenant dispatch consumers; replies never sent after legacy env
/// removal, alpha 2026-10-03). Direct port of `core/svc_process::telemetry::
/// SourceBindingSupervisorMetrics` under this stage's own metric names.
#[derive(Clone)]
pub struct DispatchSupervisorMetrics {
    /// Number of per-`(tenant_id, community_id, app_id)` dispatch consumer
    /// tasks currently running -- `dispatch_consumers_running`, readiness
    /// counts the consumer loops being alive, not just executor presence
    /// (PR #528/#534).
    pub active_consumers: prometheus::IntGauge,
    /// Dispatch consumer spawn/stop transitions, labeled by `action`.
    pub consumer_transitions_total: prometheus::IntCounterVec,
}

/// Registers [`DispatchSupervisorMetrics`] against `registry`. Must be
/// called exactly once per `registry`.
pub fn register_dispatch_supervisor_metrics(
    registry: &prometheus::Registry,
) -> DispatchSupervisorMetrics {
    let active_consumers = prometheus::IntGauge::new(
        "svc_action_dispatch_consumers_running",
        "Number of per-(tenant_id, community_id, app_id) multi-tenant dispatch consumer tasks currently running",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(active_consumers.clone()))
        .expect("register svc_action_dispatch_consumers_running");

    let consumer_transitions_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_dispatch_consumer_transitions_total",
            "Dispatch consumer spawn/stop transitions, labeled by action",
        ),
        &["action"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(consumer_transitions_total.clone()))
        .expect("register svc_action_dispatch_consumer_transitions_total");

    DispatchSupervisorMetrics {
        active_consumers,
        consumer_transitions_total,
    }
}

/// Prometheus handles for `crate::changelog_consumer` (dataplane scale
/// design rev 4, §7/§8 step 2 -- multi-tenant, change-log-driven active-set
/// loader). Direct port of `core/svc_process::telemetry::
/// ChangelogConsumerMetrics` under the `svc_action_` metric-name prefix.
#[derive(Clone)]
pub struct ChangelogConsumerMetrics {
    pub applied_scopes_total: prometheus::IntCounter,
    pub scope_failures_total: prometheus::IntCounterVec,
    pub changelog_lag: prometheus::IntGauge,
    pub reconcile_duration_seconds: prometheus::Histogram,
    pub tenant_active_apps: prometheus::IntGaugeVec,
    /// A scope evicted after failing its active-set re-read for longer than
    /// `changelog_consumer::SCOPE_STALE_EVICTION_BOUND` -- fail-closed,
    /// never silent.
    pub scope_stale_evicted_total: prometheus::IntCounter,
    /// A change-log gap detected (the lowest returned `seq` exceeded
    /// `last_seq + 1`, most plausibly retention truncation) -- forces a full
    /// reconcile instead of a partial apply, never silent.
    pub changelog_gap_detected_total: prometheus::IntCounter,
    /// `last_seq + 1 < min_retained_seq` detected via the primary's
    /// authoritative `bundle_active_set_watermark.min_retained_seq` column
    /// (hub-api migration `0026`/PR #397) -- forces a full reconcile, never
    /// silent. Distinct from `changelog_gap_detected_total` (the heuristic
    /// fallback).
    pub changelog_retention_exceeded_total: prometheus::IntCounter,
    /// A NEW executor connection detected (by pointer identity) -- gh
    /// security review item 4 on PR #406: the executor wipes its bundle
    /// registry on every disconnect, so this consumer resets its own
    /// `loaded` bookkeeping in lockstep.
    pub executor_reconnect_detected_total: prometheus::IntCounter,
    /// Per-bundle `Load` outcomes, labeled by `result` (`"success"`/
    /// `"failure"`) -- regression: loads waited for 15-min full reconcile
    /// after startup/reconnect, UnknownBundle (alpha 2026-10-03).
    pub bundle_loads_total: prometheus::IntCounterVec,
    /// Forced full authoritative active-set sends, labeled by `reason`
    /// (`"startup"`/`"reconnect"`/`"reconcile"`/`"diverged"`) -- see
    /// `bundle_active_set::FullSyncReason`.
    pub bundle_full_sync_total: prometheus::IntCounterVec,
    /// Current count of `(session, AppScope)` pairs this consumer believes
    /// are loaded across every live executor session, refreshed after every
    /// apply -- per-session fix (alpha 2026-10-03): a bundle loaded on two
    /// live sessions now counts twice, surfacing fan-out, not just presence.
    pub bundles_loaded: prometheus::IntGauge,
    /// An active bundle found loaded on ZERO live executor sessions after a
    /// sync -- fail-closed, never silent: regression: bundles loaded only
    /// onto a terminating executor during rollout; live executor got none
    /// (alpha 2026-10-03).
    pub bundle_zero_session_total: prometheus::IntCounter,
}

/// Registers [`ChangelogConsumerMetrics`] against `registry`. Must be
/// called exactly once per `registry` -- see
/// [`register_bundle_loader_excluded_metrics`]'s identical constraint.
pub fn register_changelog_consumer_metrics(
    registry: &prometheus::Registry,
) -> ChangelogConsumerMetrics {
    let applied_scopes_total = prometheus::IntCounter::new(
        "svc_action_changelog_applied_scopes_total",
        "Tenant/community scopes successfully re-read and applied by the change-log consumer",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(applied_scopes_total.clone()))
        .expect("register svc_action_changelog_applied_scopes_total");

    let scope_failures_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_changelog_scope_failures_total",
            "Per-scope re-read failures, fail-closed (skip, never abort the tick), by reason",
        ),
        &["reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(scope_failures_total.clone()))
        .expect("register svc_action_changelog_scope_failures_total");

    let changelog_lag = prometheus::IntGauge::new(
        "svc_action_changelog_lag",
        "safe_seq minus last_seq after the most recent incremental change-log poll",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(changelog_lag.clone()))
        .expect("register svc_action_changelog_lag");

    let reconcile_duration_seconds = prometheus::Histogram::with_opts(
        prometheus::HistogramOpts::new(
            "svc_action_changelog_reconcile_duration_seconds",
            "Wall-clock duration of each periodic full active-set reconcile",
        )
        .buckets(vec![0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0]),
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(reconcile_duration_seconds.clone()))
        .expect("register svc_action_changelog_reconcile_duration_seconds");

    let tenant_active_apps = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_action_tenant_active_apps",
            "Active app count per tenant, summed across every community it owns",
        ),
        &["tenant_id"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(tenant_active_apps.clone()))
        .expect("register svc_action_tenant_active_apps");

    let scope_stale_evicted_total = prometheus::IntCounter::new(
        "svc_action_changelog_scope_stale_evicted_total",
        "Scopes evicted after failing their active-set re-read longer than the staleness bound",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(scope_stale_evicted_total.clone()))
        .expect("register svc_action_changelog_scope_stale_evicted_total");

    let changelog_gap_detected_total = prometheus::IntCounter::new(
        "svc_action_changelog_gap_detected_total",
        "Change-log gaps detected (likely retention truncation), forcing a full reconcile",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(changelog_gap_detected_total.clone()))
        .expect("register svc_action_changelog_gap_detected_total");

    let changelog_retention_exceeded_total = prometheus::IntCounter::new(
        "svc_action_changelog_retention_exceeded_total",
        "last_seq fell behind min_retained_seq (primary-confirmed), forcing a full reconcile",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(changelog_retention_exceeded_total.clone()))
        .expect("register svc_action_changelog_retention_exceeded_total");

    let executor_reconnect_detected_total = prometheus::IntCounter::new(
        "svc_action_executor_reconnect_detected_total",
        "New executor connections detected (by pointer identity), resetting loaded-state",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(executor_reconnect_detected_total.clone()))
        .expect("register svc_action_executor_reconnect_detected_total");

    let bundle_loads_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_bundle_loads_total",
            "Per-bundle Load outcomes, by result (success/failure)",
        ),
        &["result"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(bundle_loads_total.clone()))
        .expect("register svc_action_bundle_loads_total");

    let bundle_full_sync_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_bundle_full_sync_total",
            "Forced full authoritative active-set sends, by reason \
             (startup/reconnect/reconcile/diverged)",
        ),
        &["reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(bundle_full_sync_total.clone()))
        .expect("register svc_action_bundle_full_sync_total");

    let bundles_loaded = prometheus::IntGauge::new(
        "svc_action_bundles_loaded",
        "Current count of AppScopes this consumer believes are loaded on the connected executor",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(bundles_loaded.clone()))
        .expect("register svc_action_bundles_loaded");

    let bundle_zero_session_total = prometheus::IntCounter::new(
        "svc_action_bundle_zero_session_total",
        "An active bundle found loaded on zero live executor sessions after a sync (fail-closed)",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(bundle_zero_session_total.clone()))
        .expect("register svc_action_bundle_zero_session_total");

    ChangelogConsumerMetrics {
        applied_scopes_total,
        scope_failures_total,
        changelog_lag,
        reconcile_duration_seconds,
        tenant_active_apps,
        scope_stale_evicted_total,
        changelog_gap_detected_total,
        executor_reconnect_detected_total,
        changelog_retention_exceeded_total,
        bundle_loads_total,
        bundle_full_sync_total,
        bundles_loaded,
        bundle_zero_session_total,
    }
}

/// Prometheus handles for the action-stage dispatch loop's connect/
/// self-heal retry (`crate::lib::try_start_dispatch`) -- regression: drain
/// loop exited on NOGROUP (alpha 2026-10-02), same bug class as
/// `core/svc_process`.
#[derive(Clone)]
pub struct DrainLoopMetrics {
    /// Total spine connect attempts, labeled by loop name.
    pub spine_connect_attempts_total: prometheus::IntCounterVec,
    /// 1 while the dispatch loop is connected and actively reading, 0
    /// while down/retrying, labeled by loop name.
    pub consumer_loop_running: prometheus::IntGaugeVec,
    /// Total times `crate::dispatch::ensure_consumer_group` actually
    /// created a (previously-missing) consumer group -- `BUSYGROUP` never
    /// increments this.
    pub consumer_group_created_total: prometheus::IntCounterVec,
}

/// Registers [`DrainLoopMetrics`] against `registry`. Must be called
/// exactly once per `registry`.
pub fn register_drain_loop_metrics(registry: &prometheus::Registry) -> DrainLoopMetrics {
    let spine_connect_attempts_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_spine_connect_attempts_total",
            "Total spine connect attempts made by a dispatch loop's connect-retry wrapper, \
             labeled by loop",
        ),
        &["loop"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(spine_connect_attempts_total.clone()))
        .expect("register svc_action_spine_connect_attempts_total");

    let consumer_loop_running = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_action_consumer_loop_running",
            "1 while the dispatch loop is connected and actively reading, 0 while \
             down/retrying, labeled by loop",
        ),
        &["loop"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(consumer_loop_running.clone()))
        .expect("register svc_action_consumer_loop_running");

    let consumer_group_created_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_action_consumer_group_created_total",
            "Total times a Valkey consumer group was newly created (XGROUP CREATE, not \
             BUSYGROUP) by the self-heal/startup provisioning step, labeled by loop",
        ),
        &["loop"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(consumer_group_created_total.clone()))
        .expect("register svc_action_consumer_group_created_total");

    DrainLoopMetrics {
        spine_connect_attempts_total,
        consumer_loop_running,
        consumer_group_created_total,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // regression: drain loop exited on NOGROUP (alpha 2026-10-02)
    #[test]
    fn register_drain_loop_metrics_produces_the_expected_series() {
        let registry = prometheus::Registry::new();
        let metrics = register_drain_loop_metrics(&registry);
        metrics
            .spine_connect_attempts_total
            .with_label_values(&["dispatch"])
            .inc();
        metrics
            .consumer_loop_running
            .with_label_values(&["dispatch"])
            .set(1);
        metrics
            .consumer_group_created_total
            .with_label_values(&["dispatch"])
            .inc();
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_action_spine_connect_attempts_total"));
        assert!(rendered.contains("svc_action_consumer_loop_running"));
        assert!(rendered.contains("svc_action_consumer_group_created_total"));
    }

    #[test]
    fn register_request_metrics_produces_a_non_empty_exposition() {
        let registry = prometheus::Registry::new();
        let metrics = register_request_metrics(&registry);
        metrics
            .http_requests_total
            .with_label_values(&["GET", "/health", "200"])
            .inc();
        metrics
            .http_request_duration_seconds
            .with_label_values(&["GET", "/health"])
            .observe(0.001);

        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_action_up 1"));
        assert!(rendered.contains("svc_action_http_requests_total"));
        assert!(rendered.contains("svc_action_http_request_duration_seconds"));
    }

    #[test]
    fn up_gauge_alone_is_a_non_empty_series_before_any_request() {
        let registry = prometheus::Registry::new();
        register_request_metrics(&registry);
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_action_up 1"));
    }

    #[test]
    fn register_egress_metrics_produces_a_labeled_counter() {
        let registry = prometheus::Registry::new();
        let denied_total = register_egress_metrics(&registry);
        denied_total
            .with_label_values(&["waddles.a.b.c", "host_not_declared"])
            .inc();
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_action_egress_denied_total"));
        assert!(rendered.contains("host_not_declared"));
    }

    #[test]
    fn register_bundle_loader_excluded_metrics_produces_a_labeled_counter() {
        let registry = prometheus::Registry::new();
        let excluded_total = register_bundle_loader_excluded_metrics(&registry);
        excluded_total
            .with_label_values(&["waddles.a", "no_approval"])
            .inc();
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_action_bundle_active_set_excluded_total"));
        assert!(rendered.contains(r#"app_id="waddles.a""#));
        assert!(rendered.contains(r#"reason="no_approval""#));
    }

    #[test]
    fn register_changelog_consumer_metrics_produces_the_expected_series() {
        let registry = prometheus::Registry::new();
        let metrics = register_changelog_consumer_metrics(&registry);
        metrics.applied_scopes_total.inc();
        metrics
            .scope_failures_total
            .with_label_values(&["read_failed"])
            .inc();
        metrics.changelog_lag.set(42);
        metrics.reconcile_duration_seconds.observe(0.25);
        metrics.tenant_active_apps.with_label_values(&["7"]).set(3);
        metrics.scope_stale_evicted_total.inc();
        metrics.changelog_gap_detected_total.inc();
        metrics.changelog_retention_exceeded_total.inc();
        metrics.executor_reconnect_detected_total.inc();
        metrics
            .bundle_loads_total
            .with_label_values(&["success"])
            .inc();
        metrics
            .bundle_full_sync_total
            .with_label_values(&["startup"])
            .inc();
        metrics.bundles_loaded.set(3);
        metrics.bundle_zero_session_total.inc();

        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_action_changelog_applied_scopes_total 1"));
        assert!(rendered.contains(r#"result="success""#));
        assert!(rendered.contains(r#"reason="startup""#));
        assert!(rendered.contains("svc_action_bundles_loaded 3"));
        assert!(rendered.contains(r#"reason="read_failed""#));
        assert!(rendered.contains("svc_action_changelog_lag 42"));
        assert!(rendered.contains("svc_action_changelog_reconcile_duration_seconds"));
        assert!(rendered.contains(r#"tenant_id="7""#));
        assert!(rendered.contains("svc_action_changelog_scope_stale_evicted_total 1"));
        assert!(rendered.contains("svc_action_changelog_gap_detected_total 1"));
        assert!(rendered.contains("svc_action_changelog_retention_exceeded_total 1"));
        assert!(rendered.contains("svc_action_executor_reconnect_detected_total 1"));
        assert!(rendered.contains("svc_action_bundle_zero_session_total 1"));
    }

    #[test]
    fn render_metrics_on_empty_registry_is_empty_string() {
        let registry = prometheus::Registry::new();
        let rendered = render_metrics(&registry).expect("empty registry still encodes");
        assert!(rendered.is_empty());
    }
}

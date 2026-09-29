//! Telemetry bootstrap: delegates `tracing` + sanitizing OTel logs/traces/
//! metrics entirely to `penguin-logging` (spec S4.9), keeping only this
//! service's own Prometheus request metrics (`RequestMetrics`) local, since
//! those are svc-ingest-specific counters, not part of the shared crate's
//! surface.
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
//!
//! Mirrors `core/svc_process/src/telemetry.rs` (established there, M4) --
//! once wired, a service should never hand-roll `tracing-subscriber`/OTel
//! plumbing again.

/// Re-exported so callers (`crate::lib::run_with_shutdown`) hold the guard
/// type without needing to depend on `penguin_logging` directly for it.
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
    let up = prometheus::IntGauge::new("svc_ingest_up", "1 if the process is running")
        .expect("valid metric definition");
    registry
        .register(Box::new(up.clone()))
        .expect("register svc_ingest_up");
    up.set(1);

    let http_requests_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_http_requests_total",
            "Total HTTP requests handled, labeled by method/path/status",
        ),
        &["method", "path", "status"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_requests_total.clone()))
        .expect("register svc_ingest_http_requests_total");

    let http_request_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_http_request_duration_seconds",
            "HTTP request duration in seconds, labeled by method/path",
        ),
        &["method", "path"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_request_duration_seconds.clone()))
        .expect("register svc_ingest_http_request_duration_seconds");

    RequestMetrics {
        http_requests_total,
        http_request_duration_seconds,
    }
}

/// Ingest-specific Prometheus metrics: platform events published onto the
/// spine and platform receiver connection state, labeled by platform so an
/// operator can see per-source health at a glance -- see `src/ingest/`.
#[derive(Clone)]
pub struct IngestMetrics {
    pub events_published_total: prometheus::IntCounterVec,
    pub publish_errors_total: prometheus::IntCounterVec,
    pub receiver_reconnects_total: prometheus::IntCounterVec,
    /// Twitch EventSub webhook verification outcomes, labeled by result
    /// (`ok`/`bad_signature`/`replay_rejected`/`secret_not_found`/
    /// `missing_header`/`bad_content_type`/`oversized_body`) --
    /// `crate::ingest::twitch_eventsub::handle_webhook`.
    pub eventsub_verifications_total: prometheus::IntCounterVec,
    /// Twitch EventSub message-ids rejected as duplicates by the dedup
    /// guard.
    pub eventsub_dedup_hits_total: prometheus::IntCounter,
    /// Twitch EventSub webhook handler latency, labeled by outcome -- the
    /// fast-path histogram `rules/critical-rules.md` Observability requires
    /// (load/latency histograms first, not just a counter).
    pub eventsub_request_duration_seconds: prometheus::HistogramVec,
    pub receiver_connection_healthy: prometheus::IntGaugeVec,
    /// #500 guild-pairing routing resolution latency (`crate::routing`),
    /// labeled by platform and outcome (`resolved`/`fail_closed`) --
    /// `rules/critical-rules.md` Observability's "histograms for
    /// load/latency first" applied to the new resolution path.
    pub guild_routing_resolution_seconds: prometheus::HistogramVec,
    /// #500 guild-pairing fail-closed resolutions, labeled by platform and
    /// `crate::routing::RouteError::reason()` -- never a broadcast
    /// fallback, always a drop; this is the metric proving that.
    pub guild_routing_fail_closed_total: prometheus::IntCounterVec,
}

/// Registers this service's ingest-path metrics against `registry`. Must be
/// called exactly once per `registry` -- see [`crate::http::AppState::new`].
pub fn register_ingest_metrics(registry: &prometheus::Registry) -> IngestMetrics {
    let events_published_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_events_published_total",
            "Total normalized platform events XADDed onto the spine, labeled by platform/source",
        ),
        &["platform", "source_id"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(events_published_total.clone()))
        .expect("register svc_ingest_events_published_total");

    let publish_errors_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_publish_errors_total",
            "Total publish_event failures, labeled by platform/reason",
        ),
        &["platform", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(publish_errors_total.clone()))
        .expect("register svc_ingest_publish_errors_total");

    let receiver_reconnects_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_receiver_reconnects_total",
            "Total platform receiver reconnect attempts, labeled by platform and triggering reason \
             (e.g. resumable_close/reconnect_fresh_close/session_invalidated/other)",
        ),
        &["platform", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(receiver_reconnects_total.clone()))
        .expect("register svc_ingest_receiver_reconnects_total");

    let eventsub_verifications_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_eventsub_verifications_total",
            "Total Twitch EventSub webhook verification attempts, labeled by outcome",
        ),
        &["outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(eventsub_verifications_total.clone()))
        .expect("register svc_ingest_eventsub_verifications_total");

    let eventsub_dedup_hits_total = prometheus::IntCounter::new(
        "svc_ingest_eventsub_dedup_hits_total",
        "Total Twitch EventSub deliveries rejected as duplicate message-ids",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(eventsub_dedup_hits_total.clone()))
        .expect("register svc_ingest_eventsub_dedup_hits_total");

    let eventsub_request_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_eventsub_request_duration_seconds",
            "Twitch EventSub webhook handler latency in seconds, labeled by outcome",
        ),
        &["outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(eventsub_request_duration_seconds.clone()))
        .expect("register svc_ingest_eventsub_request_duration_seconds");

    // Starts at 1 (healthy) for every platform the first time it's touched
    // via `with_label_values` -- there is no fixed set of platform labels
    // to pre-populate at registration time (unlike `up`), so this gauge
    // only exists in the exposition once a receiver loop has run at least
    // once. See `ReceiverHealthMetrics::receiver_marked_unhealthy`.
    let receiver_connection_healthy = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_ingest_receiver_connection_healthy",
            "1 if the platform receiver's connection is healthy, 0 once a fatal \
             (non-retryable) close has stopped that connection's reconnect loop -- \
             the service process itself keeps running either way",
        ),
        &["platform"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(receiver_connection_healthy.clone()))
        .expect("register svc_ingest_receiver_connection_healthy");

    let guild_routing_resolution_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_guild_routing_resolution_seconds",
            "#500 guild<->community routing resolution latency in seconds, labeled by platform/outcome",
        ),
        &["platform", "outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(guild_routing_resolution_seconds.clone()))
        .expect("register svc_ingest_guild_routing_resolution_seconds");

    let guild_routing_fail_closed_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_guild_routing_fail_closed_total",
            "#500 guild<->community routing resolutions that failed closed (dropped, never broadcast), \
             labeled by platform and RouteError::reason()",
        ),
        &["platform", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(guild_routing_fail_closed_total.clone()))
        .expect("register svc_ingest_guild_routing_fail_closed_total");

    IngestMetrics {
        events_published_total,
        publish_errors_total,
        receiver_reconnects_total,
        eventsub_verifications_total,
        eventsub_dedup_hits_total,
        eventsub_request_duration_seconds,
        receiver_connection_healthy,
        guild_routing_resolution_seconds,
        guild_routing_fail_closed_total,
    }
}

impl IngestMetrics {
    /// Records one Twitch EventSub webhook verification outcome --
    /// `crate::ingest::twitch_eventsub::handle_webhook`.
    pub fn record_eventsub_verification(&self, outcome: &str) {
        self.eventsub_verifications_total
            .with_label_values(&[outcome])
            .inc();
    }

    /// Records one Twitch EventSub duplicate-message-id rejection.
    pub fn record_eventsub_dedup_hit(&self) {
        self.eventsub_dedup_hits_total.inc();
    }

    /// Records one #500 guild-pairing routing resolution outcome --
    /// `crate::routing::GuildRouter::resolve`'s caller. `outcome` is
    /// `"resolved"` or `"fail_closed"`; on `fail_closed`, `reason` must be
    /// `Some(RouteError::reason())` so [`Self::guild_routing_fail_closed_total`]
    /// is incremented too.
    pub fn record_guild_routing_resolution(
        &self,
        platform: &str,
        elapsed_seconds: f64,
        outcome: &str,
        reason: Option<&str>,
    ) {
        self.guild_routing_resolution_seconds
            .with_label_values(&[platform, outcome])
            .observe(elapsed_seconds);
        if let Some(reason) = reason {
            self.guild_routing_fail_closed_total
                .with_label_values(&[platform, reason])
                .inc();
        }
    }

    /// Observes one Twitch EventSub webhook handler's end-to-end latency,
    /// labeled by `outcome` (mirrors [`Self::record_eventsub_verification`]'s
    /// label set, plus the terminal response outcomes: `ack`/
    /// `duplicate_ignored`/`acknowledged`/`ignored`/`unknown_type`/
    /// `challenge`/an error variant name).
    pub fn observe_eventsub_duration(&self, outcome: &str, seconds: f64) {
        self.eventsub_request_duration_seconds
            .with_label_values(&[outcome])
            .observe(seconds);
    }
}

/// Extends [`penguin_spine::SpineMetrics`] with the reconnect/health
/// counters this crate's own platform receiver loops need
/// (`ingest::discord`, and any future `ingest::twitch`-style caller) --
/// kept as a separate trait rather than folding into the shared spine
/// crate's `SpineMetrics` because these are svc-ingest's own receiver-
/// observability surface, not part of `penguin-spine`'s public contract.
/// Every method has a no-op default so a test double (e.g.
/// `ingest::discord::tests::NoopTestMetrics`) needs no implementation at
/// all unless a specific test wants to assert on the recorded values.
pub trait ReceiverHealthMetrics {
    /// Records one reconnect attempt for `platform`, labeled by the
    /// triggering `reason` -- e.g. `"resumable_close"`,
    /// `"reconnect_fresh_close"`, `"session_invalidated"`, `"other"`.
    fn receiver_reconnect(&self, _platform: &str, _reason: &str) {}

    /// Marks `platform`'s receiver connection as unhealthy: a fatal,
    /// non-retryable close (e.g. Discord `4004` auth failed, `4010`-`4014`
    /// sharding/intents) has stopped that connection's reconnect loop.
    /// `code` is the raw close code, when the peer sent one -- never a
    /// token, this is a small documented integer, not caller-provided
    /// secret material. The service process keeps running; only this one
    /// platform's ingest loop has stopped.
    fn receiver_marked_unhealthy(&self, _platform: &str, _code: Option<u16>) {}
}

impl ReceiverHealthMetrics for IngestMetrics {
    fn receiver_reconnect(&self, platform: &str, reason: &str) {
        self.receiver_reconnects_total
            .with_label_values(&[platform, reason])
            .inc();
    }

    fn receiver_marked_unhealthy(&self, platform: &str, code: Option<u16>) {
        self.receiver_connection_healthy
            .with_label_values(&[platform])
            .set(0);
        tracing::error!(
            platform,
            code = ?code,
            "receiver connection marked unhealthy (fatal, non-retryable close)"
        );
    }
}

/// Adapts [`IngestMetrics`] to [`penguin_spine::SpineMetrics`] so
/// [`crate::publish::publish_event`] can record `stream_event_written`
/// straight into this service's own Prometheus counters without
/// `publish.rs` depending on `prometheus` types directly.
impl penguin_spine::SpineMetrics for IngestMetrics {
    fn stream_event_written(&self, platform: &str, source_id: &str) {
        self.events_published_total
            .with_label_values(&[platform, source_id])
            .inc();
    }

    fn insecure_transport(&self, component: &str, aspect: &str, insecure: bool) {
        if insecure {
            tracing::warn!(component, aspect, "insecure transport in use");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_spine::SpineMetrics;

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
        assert!(rendered.contains("svc_ingest_up 1"));
        assert!(rendered.contains("svc_ingest_http_requests_total"));
        assert!(rendered.contains("svc_ingest_http_request_duration_seconds"));
    }

    #[test]
    fn up_gauge_alone_is_a_non_empty_series_before_any_request() {
        let registry = prometheus::Registry::new();
        register_request_metrics(&registry);
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_up 1"));
    }

    #[test]
    fn render_metrics_on_empty_registry_is_empty_string() {
        let registry = prometheus::Registry::new();
        let rendered = render_metrics(&registry).expect("empty registry still encodes");
        assert!(rendered.is_empty());
    }

    #[test]
    fn ingest_metrics_stream_event_written_increments_the_labeled_counter() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.stream_event_written("twitch", "tw-channelA");
        metrics.stream_event_written("twitch", "tw-channelA");
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_events_published_total"));
        assert_eq!(
            metrics
                .events_published_total
                .with_label_values(&["twitch", "tw-channelA"])
                .get(),
            2
        );
    }

    #[test]
    fn eventsub_metrics_record_and_render() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.record_eventsub_verification("ok");
        metrics.record_eventsub_verification("bad_signature");
        metrics.record_eventsub_dedup_hit();
        metrics.observe_eventsub_duration("ack", 0.002);

        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_eventsub_verifications_total"));
        assert!(rendered.contains("svc_ingest_eventsub_dedup_hits_total"));
        assert!(rendered.contains("svc_ingest_eventsub_request_duration_seconds"));
        assert_eq!(
            metrics
                .eventsub_verifications_total
                .with_label_values(&["ok"])
                .get(),
            1
        );
        assert_eq!(metrics.eventsub_dedup_hits_total.get(), 1);
    }

    #[test]
    fn ingest_metrics_insecure_transport_does_not_panic() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.insecure_transport("valkey", "tls", true);
        metrics.insecure_transport("valkey", "tls", false);
    }
}

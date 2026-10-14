//! Prometheus metrics for the dynamic Twitch connection resolver.
//!
//! Registered against the same registry `crate::telemetry::init` returns
//! (`/metrics`, port 9090) -- `penguin-logging` bridges that registry's
//! signals alongside its own OTel pipeline, the same arrangement every other
//! `svc_ingest` metric uses (`crate::telemetry::IngestMetrics`). Histograms
//! first (list/resolve latency, reconcile duration), then counters for events,
//! then gauges for state -- `rules/critical-rules.md` Observability (OTel).
//!
//! **No token value, IRC nick, channel, tenant or community identifier is ever
//! a metric label** -- labels are small fixed enums only (an operation name, an
//! outcome, a bounded refusal reason), so cardinality is bounded and PII
//! cannot reach the metrics sink.

use prometheus::{
    HistogramOpts, HistogramVec, IntCounterVec, IntGauge, Opts, Registry,
};

/// Latency buckets (seconds) for hub-api REST calls and reconcile passes: a
/// healthy in-cluster call is a few ms, a refresh-under-lock resolve can reach
/// seconds, and the client's own timeout bounds the tail.
const LATENCY_BUCKETS: &[f64] = &[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0];

/// The fixed vocabulary of hub-api refusal reasons (`ConnectionNotUsable.
/// reason`, `hub_api/services/provider_connections/errors.py`) a metric label
/// may carry. Anything else collapses to `other` so a misbehaving or newer
/// server can never create unbounded label cardinality.
const KNOWN_REASONS: &[&str] = &[
    "disabled",
    "pending_approval",
    "revoked_grant",
    "connection_revoked",
    "connection_expired",
    "refresh_rejected",
    "token_expired",
    "not_found",
    "identity_mismatch",
];

/// Maps a server-supplied reason to a bounded label value.
#[must_use]
pub fn bounded_reason(reason: &str) -> &'static str {
    KNOWN_REASONS
        .iter()
        .find(|known| **known == reason)
        .copied()
        .unwrap_or("other")
}

/// Handles every instrument the dynamic-connections subsystem records into.
/// Cheap to clone (each field is an `Arc` internally) and shared by the
/// hub-api client, the supervisor, and every per-bot task.
#[derive(Clone)]
pub struct ConnectionsMetrics {
    /// hub-api REST call latency, labeled `op` (`list`/`resolve`) and
    /// `outcome` (`ok`/`not_usable`/`transient`/`permanent`).
    pub request_duration_seconds: HistogramVec,
    /// hub-api REST calls, same labels as the histogram.
    pub requests_total: IntCounterVec,
    /// Resolves refused by hub-api with a "do not retry" answer (HTTP 409/404),
    /// labeled by the bounded refusal `reason` -- the 409 counter.
    pub resolve_not_usable_total: IntCounterVec,
    /// Successful resolves -- hub-api refreshes a stale token inside the same
    /// call, so this is also the "token (re)acquired" count.
    pub tokens_acquired_total: IntCounterVec,
    /// Bindings skipped before any resolve, labeled `reason`
    /// (`invalid_metadata`/`no_scope`/`wrong_provider`).
    pub bindings_skipped_total: IntCounterVec,
    /// Reconcile (poll) passes, labeled `result` (`ok`/`list_failed`).
    pub reconcile_total: IntCounterVec,
    /// Wall-clock duration of one reconcile pass.
    pub reconcile_duration_seconds: HistogramVec,
    /// Per-bot IRC socket (re)connects, labeled `cause`
    /// (`initial`/`membership_changed`/`closed`/`error`/`auth_rejected`).
    pub bot_reconnects_total: IntCounterVec,
    /// Chat events routed to the spine, labeled `result` (`published`/
    /// `publish_failed`/`self_echo`/`unrouted`).
    pub events_routed_total: IntCounterVec,
    /// Twitch channels currently joined by dynamic bot sockets.
    pub channels_joined: IntGauge,
    /// Bot IRC sockets currently running (one per distinct bot account).
    pub bots_running: IntGauge,
    /// Bindings returned by the most recent successful list.
    pub bindings_listed: IntGauge,
}

impl ConnectionsMetrics {
    /// Registers every instrument against `registry`. Must be called at most
    /// once per registry (a `prometheus::Registry` rejects duplicate names);
    /// returns an error rather than panicking so startup can fail loudly.
    pub fn register(registry: &Registry) -> Result<Self, prometheus::Error> {
        let request_duration_seconds = HistogramVec::new(
            HistogramOpts::new(
                "svc_ingest_hubconn_request_duration_seconds",
                "hub-api provider-connections REST call latency, by op/outcome",
            )
            .buckets(LATENCY_BUCKETS.to_vec()),
            &["op", "outcome"],
        )?;
        let requests_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_requests_total",
                "hub-api provider-connections REST calls, by op/outcome",
            ),
            &["op", "outcome"],
        )?;
        let resolve_not_usable_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_resolve_not_usable_total",
                "Resolves hub-api refused as not usable (HTTP 409/404); the channel is dropped, by bounded reason",
            ),
            &["reason"],
        )?;
        let tokens_acquired_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_tokens_acquired_total",
                "Successful credential resolves (hub-api refreshes stale tokens inside the call), by cause",
            ),
            &["cause"],
        )?;
        let bindings_skipped_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_bindings_skipped_total",
                "Listed bindings skipped before resolve, by reason",
            ),
            &["reason"],
        )?;
        let reconcile_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_reconcile_total",
                "Supervisor reconcile (poll) passes, by result",
            ),
            &["result"],
        )?;
        let reconcile_duration_seconds = HistogramVec::new(
            HistogramOpts::new(
                "svc_ingest_hubconn_reconcile_duration_seconds",
                "Duration of one supervisor reconcile pass, by result",
            )
            .buckets(LATENCY_BUCKETS.to_vec()),
            &["result"],
        )?;
        let bot_reconnects_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_bot_reconnects_total",
                "Per-bot Twitch IRC socket (re)connect attempts, by cause",
            ),
            &["cause"],
        )?;
        let events_routed_total = IntCounterVec::new(
            Opts::new(
                "svc_ingest_hubconn_events_routed_total",
                "Inbound dynamic-connection chat events, by routing result",
            ),
            &["result"],
        )?;
        let channels_joined = IntGauge::new(
            "svc_ingest_hubconn_channels_joined",
            "Twitch channels currently joined by dynamic bot sockets",
        )?;
        let bots_running = IntGauge::new(
            "svc_ingest_hubconn_bots_running",
            "Dynamic Twitch bot IRC sockets currently running (one per bot account)",
        )?;
        let bindings_listed = IntGauge::new(
            "svc_ingest_hubconn_bindings_listed",
            "Bindings returned by the most recent successful hub-api list",
        )?;

        registry.register(Box::new(request_duration_seconds.clone()))?;
        registry.register(Box::new(requests_total.clone()))?;
        registry.register(Box::new(resolve_not_usable_total.clone()))?;
        registry.register(Box::new(tokens_acquired_total.clone()))?;
        registry.register(Box::new(bindings_skipped_total.clone()))?;
        registry.register(Box::new(reconcile_total.clone()))?;
        registry.register(Box::new(reconcile_duration_seconds.clone()))?;
        registry.register(Box::new(bot_reconnects_total.clone()))?;
        registry.register(Box::new(events_routed_total.clone()))?;
        registry.register(Box::new(channels_joined.clone()))?;
        registry.register(Box::new(bots_running.clone()))?;
        registry.register(Box::new(bindings_listed.clone()))?;

        Ok(Self {
            request_duration_seconds,
            requests_total,
            resolve_not_usable_total,
            tokens_acquired_total,
            bindings_skipped_total,
            reconcile_total,
            reconcile_duration_seconds,
            bot_reconnects_total,
            events_routed_total,
            channels_joined,
            bots_running,
            bindings_listed,
        })
    }

    /// Records one hub-api REST call's latency and outcome.
    pub fn observe_request(&self, op: &str, outcome: &str, seconds: f64) {
        self.request_duration_seconds
            .with_label_values(&[op, outcome])
            .observe(seconds);
        self.requests_total.with_label_values(&[op, outcome]).inc();
    }

    /// Records one resolve refused as not usable (409/404), by bounded reason.
    pub fn record_not_usable(&self, reason: &str) {
        self.resolve_not_usable_total
            .with_label_values(&[bounded_reason(reason)])
            .inc();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounded_reason_passes_known_values_and_collapses_the_rest() {
        assert_eq!(bounded_reason("revoked_grant"), "revoked_grant");
        assert_eq!(bounded_reason("token_expired"), "token_expired");
        // An unknown/hostile server string must never become a label value.
        assert_eq!(bounded_reason("oauth:supersecret"), "other");
        assert_eq!(bounded_reason(""), "other");
    }

    #[test]
    fn register_is_exposed_through_the_registry_and_rejects_a_second_registration() {
        let registry = Registry::new();
        let metrics = ConnectionsMetrics::register(&registry).expect("first registration");
        metrics.observe_request("list", "ok", 0.012);
        metrics.record_not_usable("disabled");
        metrics.channels_joined.set(3);

        let names: Vec<String> = registry
            .gather()
            .iter()
            .map(|family| family.name().to_string())
            .collect();
        for expected in [
            "svc_ingest_hubconn_request_duration_seconds",
            "svc_ingest_hubconn_requests_total",
            "svc_ingest_hubconn_resolve_not_usable_total",
            "svc_ingest_hubconn_channels_joined",
        ] {
            assert!(names.iter().any(|n| n == expected), "missing {expected}");
        }
        assert!(
            ConnectionsMetrics::register(&registry).is_err(),
            "duplicate registration must fail loudly, not silently double-count"
        );
    }
}

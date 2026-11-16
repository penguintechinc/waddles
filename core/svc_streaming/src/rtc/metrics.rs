//! Prometheus metrics for WHIP/WHEP session lifecycle and RTP flow, per the
//! S6 task spec's required series: `rtc_sessions_active{kind}`,
//! `rtc_rtp_packets_total{direction}`, `rtc_packets_dropped_total`,
//! `rtc_ice_failures_total`.
//!
//! **Integration gap:** `src/http/mod.rs`'s `AppState` owns the single
//! `Arc<prometheus::Registry>` this service scrapes at `/metrics`
//! (`crate::http::AppState::new` -> `crate::telemetry::register_request_metrics`),
//! and that file is outside this chunk's ownership. [`RtcMetrics::register`]
//! mirrors that same registration pattern so wiring it into `AppState` is a
//! one-line follow-up (`rtc::metrics::RtcMetrics::register(&metrics)`
//! alongside the existing `register_request_metrics` call) -- not done here.

use prometheus::{
    Histogram, HistogramOpts, HistogramVec, IntCounter, IntCounterVec, IntGaugeVec, Opts, Registry,
};

/// Registered WHIP/WHEP metric handles, cheap to clone (each inner type is
/// itself a cheap `Arc`-backed handle per the `prometheus` crate).
#[derive(Debug, Clone)]
pub struct RtcMetrics {
    /// Active sessions, labeled `kind` = `"whip"` | `"whep"`.
    pub sessions_active: IntGaugeVec,
    /// RTP packets, labeled `direction` = `"ingress"` (WHIP inbound),
    /// `"egress"` (WHEP outbound to a viewer), or `"transcoded_ingest"`
    /// (local UDP read from an ffmpeg `-f rtp` leg -- see
    /// [`crate::rtc::rtp_leg`]).
    pub rtp_packets_total: IntCounterVec,
    /// Fanout packets dropped because a subscriber (WHEP viewer) fell
    /// behind -- see [`crate::rtc::fanout::TrackFanout`].
    pub packets_dropped_total: IntCounter,
    /// ICE/DTLS connection failures across both WHIP and WHEP sessions.
    pub ice_failures_total: IntCounter,
    /// Room moderation operations, labeled `op` and `outcome` = `"ok"` | `"refused"`.
    pub moderation_ops_total: IntCounterVec,
    /// Live peer connections torn down by leave/kick/token-revoke.
    pub moderation_sessions_closed_total: IntCounter,
    /// RoomsService gRPC calls (`rooms::grpc`), labeled `rpc` and gRPC status `code`
    /// (closed vocabulary: `ok`, `unauthenticated`, `permission_denied`, ...).
    pub grpc_requests_total: IntCounterVec,
    /// RoomsService gRPC call latency in seconds, labeled `rpc`.
    pub grpc_request_seconds: HistogramVec,
    /// Hands newly raised (an idempotent re-raise is not counted).
    pub hands_raised_total: IntCounter,
    /// Hands acknowledged by a moderator (first acknowledgement only).
    pub hands_acknowledged_total: IntCounter,
    /// Hands dropped from a queue, labeled `reason` = `lower` | `leave` | `kick` | `reset` |
    /// `delete` | `expiry`.
    pub hands_cleared_total: IntCounterVec,
    /// Seconds a hand waited between being raised and being acknowledged.
    pub hand_ack_wait_seconds: Histogram,
    /// Rooms deleted by the idle-room reaper (empty for their `empty_timeout_secs`).
    pub rooms_expired_total: IntCounter,
}

impl RtcMetrics {
    /// Registers every WHIP/WHEP series against `registry`. Safe to call
    /// once per registry; a second registration attempt against the same
    /// registry returns an `AlreadyReg` [`prometheus::Error`].
    pub fn register(registry: &Registry) -> Result<Self, prometheus::Error> {
        let sessions_active = IntGaugeVec::new(
            Opts::new("rtc_sessions_active", "Active WHIP/WHEP WebRTC sessions"),
            &["kind"],
        )?;
        let rtp_packets_total = IntCounterVec::new(
            Opts::new("rtc_rtp_packets_total", "RTP packets processed"),
            &["direction"],
        )?;
        let packets_dropped_total = IntCounter::new(
            "rtc_packets_dropped_total",
            "RTP packets dropped from a fanout subscriber that fell behind",
        )?;
        let ice_failures_total = IntCounter::new(
            "rtc_ice_failures_total",
            "WHIP/WHEP sessions that reached RTCPeerConnectionState::Failed",
        )?;

        let moderation_ops_total = IntCounterVec::new(
            Opts::new(
                "rooms_moderation_ops_total",
                "Room moderation operations by op and outcome",
            ),
            &["op", "outcome"],
        )?;
        let moderation_sessions_closed_total = IntCounter::new(
            "rooms_moderation_sessions_closed_total",
            "Live room peer connections closed by leave, kick or token revocation",
        )?;
        let grpc_requests_total = IntCounterVec::new(
            Opts::new(
                "rooms_grpc_requests_total",
                "RoomsService gRPC calls by rpc and gRPC status code",
            ),
            &["rpc", "code"],
        )?;
        let grpc_request_seconds = HistogramVec::new(
            HistogramOpts::new(
                "rooms_grpc_request_seconds",
                "RoomsService gRPC call latency in seconds by rpc",
            ),
            &["rpc"],
        )?;
        let hands_raised_total = IntCounter::new(
            "rooms_hands_raised_total",
            "Raised hands newly added to a room queue",
        )?;
        let hands_acknowledged_total = IntCounter::new(
            "rooms_hands_acknowledged_total",
            "Raised hands acknowledged by a moderator",
        )?;
        let hands_cleared_total = IntCounterVec::new(
            Opts::new(
                "rooms_hands_cleared_total",
                "Raised hands dropped from a room queue by reason",
            ),
            &["reason"],
        )?;
        let hand_ack_wait_seconds = Histogram::with_opts(
            HistogramOpts::new(
                "rooms_hand_ack_wait_seconds",
                "Seconds between a hand being raised and acknowledged",
            )
            .buckets(vec![
                1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0,
            ]),
        )?;
        let rooms_expired_total = IntCounter::new(
            "rooms_expired_total",
            "Rooms deleted after staying empty for their empty_timeout_secs",
        )?;
        registry.register(Box::new(hands_raised_total.clone()))?;
        registry.register(Box::new(hands_acknowledged_total.clone()))?;
        registry.register(Box::new(hands_cleared_total.clone()))?;
        registry.register(Box::new(hand_ack_wait_seconds.clone()))?;
        registry.register(Box::new(rooms_expired_total.clone()))?;
        registry.register(Box::new(grpc_requests_total.clone()))?;
        registry.register(Box::new(grpc_request_seconds.clone()))?;
        registry.register(Box::new(moderation_ops_total.clone()))?;
        registry.register(Box::new(moderation_sessions_closed_total.clone()))?;
        registry.register(Box::new(sessions_active.clone()))?;
        registry.register(Box::new(rtp_packets_total.clone()))?;
        registry.register(Box::new(packets_dropped_total.clone()))?;
        registry.register(Box::new(ice_failures_total.clone()))?;

        Ok(Self {
            sessions_active,
            rtp_packets_total,
            packets_dropped_total,
            ice_failures_total,
            moderation_ops_total,
            moderation_sessions_closed_total,
            grpc_requests_total,
            grpc_request_seconds,
            hands_raised_total,
            hands_acknowledged_total,
            hands_cleared_total,
            hand_ack_wait_seconds,
            rooms_expired_total,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_all_four_series_once() {
        let registry = Registry::new();
        let metrics = RtcMetrics::register(&registry).expect("first registration succeeds");
        metrics.sessions_active.with_label_values(&["whip"]).set(1);
        metrics
            .rtp_packets_total
            .with_label_values(&["ingress"])
            .inc();
        metrics.packets_dropped_total.inc_by(3);
        metrics.ice_failures_total.inc();

        let families = registry.gather();
        let names: Vec<_> = families.iter().map(|f| f.name().to_string()).collect();
        for expected in [
            "rtc_sessions_active",
            "rtc_rtp_packets_total",
            "rtc_packets_dropped_total",
            "rtc_ice_failures_total",
        ] {
            assert!(names.contains(&expected.to_string()), "missing {expected}");
        }
    }

    #[test]
    fn double_registration_against_same_registry_errors() {
        let registry = Registry::new();
        RtcMetrics::register(&registry).unwrap();
        assert!(RtcMetrics::register(&registry).is_err());
    }
}

//! P3 push hub: a Tokio broadcast-backed fan-out keyed by (community_id,
//! surface), reached by every connected overlay viewer (SSE or websocket,
//! `crate::http::overlay`) for that community+surface.
//!
//! One `tokio::sync::broadcast` channel per key -- its built-in `Lagged`
//! semantics are exactly the backpressure policy this hub needs: a bounded
//! per-subscriber buffer ([`CHANNEL_CAPACITY`]) that drops the oldest
//! frames out from under a slow subscriber rather than ever blocking the
//! producer or growing without bound. A dropped-count metric
//! ([`HubMetrics::dropped_frames_total`]) makes that loss observable
//! instead of silent.
//!
//! This module owns no HTTP/SSE/websocket framing itself -- that's P4's
//! job (`crate::http::overlay`), which is the only intended caller of
//! [`PresentationHub::publish`]/[`PresentationHub::subscribe`].

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Instant;

use overlay_schema::{OverlayPush, Surface};
use tokio::sync::broadcast;

/// Bounded per-(community, surface) channel capacity. Overlay frames
/// (chat/alerts/goals/crawler text) are human-paced, not a tight
/// per-frame video loop, so 64 queued-but-undelivered frames is generous
/// slack for a momentarily slow subscriber while still bounding memory for
/// an idle/stalled one -- a subscriber more than 64 frames behind the
/// fastest publisher is lagged forward (see [`RecvOutcome::Lagged`]),
/// never backing up the channel or blocking [`PresentationHub::publish`].
pub const CHANNEL_CAPACITY: usize = 64;

/// Prometheus series this hub records into -- registered once against the
/// service's shared registry, same contract as
/// [`crate::telemetry::register_request_metrics`]. Histograms first per
/// `rules/critical-rules.md` Observability: connection duration and
/// fan-out latency are load/latency signals, not afterthought counters.
#[derive(Clone)]
pub struct HubMetrics {
    subscribers: prometheus::IntGaugeVec,
    dropped_frames_total: prometheus::IntCounterVec,
    connection_duration_seconds: prometheus::HistogramVec,
    fanout_latency_seconds: prometheus::HistogramVec,
}

/// Registers this hub's Prometheus series against `registry`. Must be
/// called exactly once per registry (see [`crate::http::AppState::new`]).
/// Labeled only by `surface` (a closed, 9-member set) rather than also by
/// `community_id` -- community count is open-ended and per-community
/// label cardinality would grow unbounded as communities are added.
pub fn register_hub_metrics(registry: &prometheus::Registry) -> HubMetrics {
    let subscribers = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_presentation_overlay_subscribers",
            "Current connected overlay viewers, labeled by surface",
        ),
        &["surface"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(subscribers.clone()))
        .expect("register svc_presentation_overlay_subscribers");

    let dropped_frames_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_presentation_overlay_dropped_frames_total",
            "Frames dropped because a subscriber fell behind the hub's bounded buffer",
        ),
        &["surface"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(dropped_frames_total.clone()))
        .expect("register svc_presentation_overlay_dropped_frames_total");

    let connection_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_presentation_overlay_connection_duration_seconds",
            "Overlay viewer connection lifetime in seconds",
        ),
        &["surface"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(connection_duration_seconds.clone()))
        .expect("register svc_presentation_overlay_connection_duration_seconds");

    let fanout_latency_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_presentation_overlay_fanout_latency_seconds",
            "Time to enqueue a published frame onto every subscriber channel for a surface",
        ),
        &["surface"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(fanout_latency_seconds.clone()))
        .expect("register svc_presentation_overlay_fanout_latency_seconds");

    HubMetrics {
        subscribers,
        dropped_frames_total,
        connection_duration_seconds,
        fanout_latency_seconds,
    }
}

type ChannelKey = (i64, Surface);

/// The in-process push fan-out: one broadcast channel per (community,
/// surface), created lazily on first publish or subscribe.
pub struct PresentationHub {
    channels: Mutex<HashMap<ChannelKey, broadcast::Sender<Arc<OverlayPush>>>>,
    capacity: usize,
    metrics: HubMetrics,
}

impl PresentationHub {
    /// Builds a hub with the production [`CHANNEL_CAPACITY`].
    pub fn new(metrics: HubMetrics) -> Self {
        Self::with_capacity(CHANNEL_CAPACITY, metrics)
    }

    /// Builds a hub with an explicit capacity -- the entry point the
    /// backpressure/drop-path tests use to force a lag with only a
    /// handful of sends instead of 64 real ones.
    pub fn with_capacity(capacity: usize, metrics: HubMetrics) -> Self {
        Self {
            channels: Mutex::new(HashMap::new()),
            capacity,
            metrics,
        }
    }

    /// Returns the channel for `(community_id, surface)`, creating it (with
    /// zero current subscribers) on first use. The lock is held only for
    /// the hashmap lookup/insert -- never across an `.await` point.
    fn sender_for(
        &self,
        community_id: i64,
        surface: Surface,
    ) -> broadcast::Sender<Arc<OverlayPush>> {
        let mut channels = self.channels.lock().expect("hub channel map lock poisoned");
        channels
            .entry((community_id, surface))
            .or_insert_with(|| broadcast::channel(self.capacity).0)
            .clone()
    }

    /// Fans `push` out to every current subscriber of `community_id`/
    /// `surface`. Never blocks, regardless of how slow or numerous the
    /// subscribers are: a publish with zero subscribers, or one whose
    /// subscribers have all already disconnected, is simply a no-op
    /// (`broadcast::Sender::send`'s `Err` means "no active receivers",
    /// not a failure this caller needs to react to or retry).
    pub fn publish(&self, community_id: i64, surface: Surface, push: OverlayPush) {
        let start = Instant::now();
        let sender = self.sender_for(community_id, surface);
        let _ = sender.send(Arc::new(push));
        self.metrics
            .fanout_latency_seconds
            .with_label_values(&[surface.as_str()])
            .observe(start.elapsed().as_secs_f64());
    }

    /// Registers a new subscriber for `community_id`/`surface`. Only
    /// frames published *after* this call are ever delivered -- a
    /// `tokio::sync::broadcast::Receiver` never replays history, matching
    /// the legacy Python hub's own per-connection queue semantics (a late
    /// subscriber there was never handed anything published before it
    /// connected either).
    pub fn subscribe(&self, community_id: i64, surface: Surface) -> HubSubscription {
        let sender = self.sender_for(community_id, surface);
        let receiver = sender.subscribe();
        self.metrics
            .subscribers
            .with_label_values(&[surface.as_str()])
            .inc();
        HubSubscription {
            receiver,
            metrics: self.metrics.clone(),
            surface,
            connected_at: Instant::now(),
        }
    }
}

/// One subscriber's live handle into [`PresentationHub`]. Dropping this --
/// clean close, client disconnect, or a failed send on the caller's side --
/// always decrements the subscriber gauge and records the connection's
/// lifetime via [`Drop`], so P4's route handlers never need to remember to
/// do that bookkeeping themselves on every exit path.
pub struct HubSubscription {
    receiver: broadcast::Receiver<Arc<OverlayPush>>,
    metrics: HubMetrics,
    surface: Surface,
    connected_at: Instant,
}

/// One outcome of [`HubSubscription::recv`].
pub enum RecvOutcome {
    /// A frame to forward to the client.
    Push(Arc<OverlayPush>),
    /// This subscriber fell behind by `n` frames, which were dropped
    /// (never delivered) rather than queued without bound. Already
    /// counted in [`HubMetrics::dropped_frames_total`] by the time this is
    /// returned -- callers log/skip, they don't need to record it again.
    Lagged(u64),
}

impl HubSubscription {
    /// Waits for the next frame (or lag notification). Returns `None` once
    /// the channel is permanently closed (every sender side dropped --
    /// in practice only at process shutdown, since [`PresentationHub`]
    /// itself lives for the process lifetime behind `Arc` in `AppState`).
    pub async fn recv(&mut self) -> Option<RecvOutcome> {
        match self.receiver.recv().await {
            Ok(push) => Some(RecvOutcome::Push(push)),
            Err(broadcast::error::RecvError::Lagged(n)) => {
                self.metrics
                    .dropped_frames_total
                    .with_label_values(&[self.surface.as_str()])
                    .inc_by(n);
                Some(RecvOutcome::Lagged(n))
            }
            Err(broadcast::error::RecvError::Closed) => None,
        }
    }
}

impl Drop for HubSubscription {
    fn drop(&mut self) {
        self.metrics
            .subscribers
            .with_label_values(&[self.surface.as_str()])
            .dec();
        self.metrics
            .connection_duration_seconds
            .with_label_values(&[self.surface.as_str()])
            .observe(self.connected_at.elapsed().as_secs_f64());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_metrics() -> HubMetrics {
        register_hub_metrics(&prometheus::Registry::new())
    }

    fn push_with_title(title: &str) -> OverlayPush {
        OverlayPush {
            title: Some(title.to_string()),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn subscribe_then_publish_delivers_the_frame() {
        let hub = PresentationHub::new(test_metrics());
        let mut sub = hub.subscribe(42, Surface::Media);
        hub.publish(42, Surface::Media, push_with_title("hello"));
        match sub.recv().await {
            Some(RecvOutcome::Push(push)) => assert_eq!(push.title.as_deref(), Some("hello")),
            Some(RecvOutcome::Lagged(_)) => panic!("unexpected lag on a fresh subscriber"),
            None => panic!("channel closed unexpectedly"),
        }
    }

    #[tokio::test]
    async fn publish_before_subscribe_is_never_replayed() {
        let hub = PresentationHub::new(test_metrics());
        hub.publish(42, Surface::Media, push_with_title("missed"));
        let mut sub = hub.subscribe(42, Surface::Media);
        hub.publish(42, Surface::Media, push_with_title("seen"));
        match sub.recv().await {
            Some(RecvOutcome::Push(push)) => assert_eq!(push.title.as_deref(), Some("seen")),
            _ => panic!("expected only the post-subscribe frame, got a different outcome"),
        }
    }

    #[tokio::test]
    async fn publish_is_isolated_per_community() {
        let hub = PresentationHub::new(test_metrics());
        let mut sub_a = hub.subscribe(1, Surface::Chat);
        let _sub_b = hub.subscribe(2, Surface::Chat);
        hub.publish(1, Surface::Chat, push_with_title("for community 1"));
        hub.publish(2, Surface::Chat, push_with_title("for community 2"));
        match sub_a.recv().await {
            Some(RecvOutcome::Push(push)) => {
                assert_eq!(push.title.as_deref(), Some("for community 1"))
            }
            _ => panic!("community 1's subscriber must never see community 2's frame"),
        }
    }

    #[tokio::test]
    async fn publish_is_isolated_per_surface() {
        let hub = PresentationHub::new(test_metrics());
        let mut media_sub = hub.subscribe(9, Surface::Media);
        hub.publish(9, Surface::Crawler, push_with_title("crawler only"));
        // Media's own channel has nothing published to it -- publish() to
        // a different surface must not have also created/filled it.
        hub.publish(9, Surface::Media, push_with_title("media only"));
        match media_sub.recv().await {
            Some(RecvOutcome::Push(push)) => assert_eq!(push.title.as_deref(), Some("media only")),
            _ => panic!("media subscriber must only ever see media-surface frames"),
        }
    }

    #[tokio::test]
    async fn publish_with_no_subscribers_is_a_safe_no_op() {
        let hub = PresentationHub::new(test_metrics());
        // No panic, no error surfaced to the caller -- publish() returns ().
        hub.publish(99, Surface::Goals, push_with_title("nobody listening"));
    }

    /// The backpressure/drop-path requirement: a subscriber that falls
    /// behind the hub's bounded buffer is lagged forward (frames dropped,
    /// counted) rather than the producer ever blocking on it.
    #[tokio::test]
    async fn a_slow_subscriber_is_lagged_forward_instead_of_blocking_the_producer() {
        let registry = prometheus::Registry::new();
        let metrics = register_hub_metrics(&registry);
        let hub = PresentationHub::with_capacity(2, metrics);
        let mut sub = hub.subscribe(7, Surface::Ticker);

        // Publish more frames than the channel's capacity before the
        // subscriber ever calls recv() -- proves the producer (this test
        // thread) never blocks on a slow/absent reader.
        for i in 0..5 {
            hub.publish(7, Surface::Ticker, push_with_title(&format!("frame-{i}")));
        }

        match sub.recv().await {
            Some(RecvOutcome::Lagged(n)) => assert!(n > 0, "expected at least one dropped frame"),
            Some(RecvOutcome::Push(_)) => panic!("expected the lagged outcome first"),
            None => panic!("channel closed unexpectedly"),
        }

        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("svc_presentation_overlay_dropped_frames_total"));
    }

    #[tokio::test]
    async fn subscriber_gauge_increments_on_subscribe_and_decrements_on_drop() {
        let registry = prometheus::Registry::new();
        let metrics = register_hub_metrics(&registry);
        let hub = PresentationHub::new(metrics);
        {
            let _sub = hub.subscribe(5, Surface::AlertBox);
            let rendered = crate::telemetry::render_metrics(&registry).unwrap();
            assert!(
                rendered.contains("svc_presentation_overlay_subscribers{surface=\"alert_box\"} 1")
            );
        }
        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("svc_presentation_overlay_subscribers{surface=\"alert_box\"} 0"));
    }

    #[tokio::test]
    async fn dropping_a_subscription_records_its_connection_duration() {
        let registry = prometheus::Registry::new();
        let metrics = register_hub_metrics(&registry);
        let hub = PresentationHub::new(metrics);
        drop(hub.subscribe(11, Surface::FullScreen));
        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("svc_presentation_overlay_connection_duration_seconds"));
    }

    #[tokio::test]
    async fn publish_records_fanout_latency() {
        let registry = prometheus::Registry::new();
        let metrics = register_hub_metrics(&registry);
        let hub = PresentationHub::new(metrics);
        hub.publish(13, Surface::Goals, push_with_title("x"));
        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("svc_presentation_overlay_fanout_latency_seconds"));
    }

    #[tokio::test]
    async fn channel_closes_once_the_hub_and_all_senders_are_dropped() {
        let hub = PresentationHub::new(test_metrics());
        let mut sub = hub.subscribe(3, Surface::Image);
        drop(hub);
        assert!(sub.recv().await.is_none());
    }
}

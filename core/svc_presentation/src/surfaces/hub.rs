//! Fan-out + bounded replay cache for bundle-published surface frames.
//!
//! Frames are keyed by `(community_id, surface_id)` so one community's
//! publish can never reach another community's subscribers. Each key has a
//! lazily created broadcast channel (dropped when its last subscriber goes)
//! and, for surfaces whose contract says `replay`, the last frame is kept in
//! a bounded, TTL-limited cache so a freshly opened overlay renders at once.
//!
//! Subscribe-then-replay protocol: a handler subscribes FIRST, then reads the
//! replay frame; any frame published in between is seen on both paths, so the
//! handler drops live frames whose `seq` is not newer than the replayed one.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::Value;
use tokio::sync::broadcast;

/// Per-key broadcast buffer; a subscriber further behind than this loses the
/// oldest frames (counted) instead of ever blocking a publisher.
pub const CHANNEL_CAPACITY: usize = 32;

/// One validated, sanitized surface frame as delivered to viewers.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SurfaceFrame {
    /// Surface id (`music.now_playing`).
    pub surface: String,
    /// Process-wide monotonically increasing publish sequence number.
    pub seq: u64,
    /// Publish time, milliseconds since the Unix epoch.
    pub published_at_ms: u64,
    /// `true` when this frame is a replay of cached state, not a live publish.
    pub replay: bool,
    /// The validated structured payload (plain text strings, no markup).
    pub payload: Value,
}

type Key = (i64, String);
type Channels = HashMap<Key, broadcast::Sender<Arc<SurfaceFrame>>>;

struct Cached {
    frame: Arc<SurfaceFrame>,
    stored: Instant,
}

/// The hub. Shared behind an `Arc` in `AppState`.
pub struct SurfaceHub {
    channels: Arc<Mutex<Channels>>,
    cache: Mutex<HashMap<Key, Cached>>,
    seq: AtomicU64,
    max_entries: usize,
    ttl: Duration,
}

impl SurfaceHub {
    /// Builds a hub with the contract's replay bounds.
    pub fn new(max_entries: usize, ttl: Duration) -> Self {
        Self {
            channels: Arc::new(Mutex::new(HashMap::new())),
            cache: Mutex::new(HashMap::new()),
            seq: AtomicU64::new(0),
            max_entries: max_entries.max(1),
            ttl,
        }
    }

    fn lock_channels(&self) -> std::sync::MutexGuard<'_, Channels> {
        self.channels.lock().unwrap_or_else(|p| {
            tracing::warn!("surface hub channel lock poisoned; recovering");
            p.into_inner()
        })
    }

    fn lock_cache(&self) -> std::sync::MutexGuard<'_, HashMap<Key, Cached>> {
        self.cache.lock().unwrap_or_else(|p| {
            tracing::warn!("surface hub replay lock poisoned; recovering");
            p.into_inner()
        })
    }

    /// Publishes `payload` for `(community_id, surface)`: caches it when
    /// `replay`, then fans it out. Never blocks on slow subscribers.
    pub fn publish(
        &self,
        community_id: i64,
        surface: &str,
        replay: bool,
        payload: Value,
    ) -> Arc<SurfaceFrame> {
        let frame = Arc::new(SurfaceFrame {
            surface: surface.to_string(),
            seq: self.seq.fetch_add(1, Ordering::SeqCst) + 1,
            published_at_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis() as u64)
                .unwrap_or(0),
            replay: false,
            payload,
        });
        let key = (community_id, surface.to_string());
        if replay {
            let mut cache = self.lock_cache();
            if !cache.contains_key(&key) && cache.len() >= self.max_entries {
                let now = Instant::now();
                cache.retain(|_, c| now.saturating_duration_since(c.stored) < self.ttl);
                if cache.len() >= self.max_entries {
                    let oldest = cache
                        .iter()
                        .min_by_key(|(_, c)| c.stored)
                        .map(|(k, _)| k.clone());
                    if let Some(k) = oldest {
                        cache.remove(&k);
                    }
                }
            }
            cache.insert(
                key.clone(),
                Cached {
                    frame: frame.clone(),
                    stored: Instant::now(),
                },
            );
        }
        let sender = self.lock_channels().get(&key).cloned();
        if let Some(sender) = sender {
            // Err = no live receivers; not a failure.
            let _ = sender.send(frame.clone());
        }
        frame
    }

    /// The cached frame to replay to a new subscriber, marked `replay: true`;
    /// `None` when nothing is cached or the entry outlived the TTL.
    pub fn replay_for(&self, community_id: i64, surface: &str) -> Option<Arc<SurfaceFrame>> {
        let key = (community_id, surface.to_string());
        let mut cache = self.lock_cache();
        let fresh = match cache.get(&key) {
            Some(c) if c.stored.elapsed() < self.ttl => Some(c.frame.clone()),
            Some(_) => None,
            None => return None,
        };
        match fresh {
            Some(frame) => {
                let mut replayed = (*frame).clone();
                replayed.replay = true;
                Some(Arc::new(replayed))
            }
            None => {
                cache.remove(&key);
                None
            }
        }
    }

    /// Subscribes to live frames for `(community_id, surface)`.
    pub fn subscribe(&self, community_id: i64, surface: &str) -> SurfaceSubscription {
        let key = (community_id, surface.to_string());
        let receiver = {
            let mut channels = self.lock_channels();
            channels
                .entry(key.clone())
                .or_insert_with(|| broadcast::channel(CHANNEL_CAPACITY).0)
                .subscribe()
        };
        SurfaceSubscription {
            receiver,
            key,
            channels: Arc::downgrade(&self.channels),
        }
    }

    /// Cached replay entries (diagnostics/tests).
    pub fn replay_len(&self) -> usize {
        self.lock_cache().len()
    }

    /// Live channel entries (diagnostics/tests).
    pub fn channel_count(&self) -> usize {
        self.lock_channels().len()
    }
}

/// One subscriber's handle; dropping it garbage-collects an unused channel.
pub struct SurfaceSubscription {
    receiver: broadcast::Receiver<Arc<SurfaceFrame>>,
    key: Key,
    channels: Weak<Mutex<Channels>>,
}

/// One outcome of [`SurfaceSubscription::recv`].
pub enum Recv {
    /// A live frame.
    Frame(Arc<SurfaceFrame>),
    /// The subscriber fell behind; this many frames were dropped.
    Lagged(u64),
}

impl SurfaceSubscription {
    /// Waits for the next frame; `None` once the channel is closed.
    pub async fn recv(&mut self) -> Option<Recv> {
        match self.receiver.recv().await {
            Ok(f) => Some(Recv::Frame(f)),
            Err(broadcast::error::RecvError::Lagged(n)) => Some(Recv::Lagged(n)),
            Err(broadcast::error::RecvError::Closed) => None,
        }
    }
}

impl Drop for SurfaceSubscription {
    fn drop(&mut self) {
        let Some(channels) = self.channels.upgrade() else {
            return;
        };
        let mut channels = channels.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(sender) = channels.get(&self.key) {
            // Our own receiver is still alive while dropping.
            if sender.receiver_count() <= 1 {
                channels.remove(&self.key);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn hub() -> SurfaceHub {
        SurfaceHub::new(3, Duration::from_secs(60))
    }

    #[tokio::test]
    async fn publish_reaches_only_the_same_community_and_surface() {
        let h = hub();
        let mut a = h.subscribe(1, "music.queue");
        let mut other_community = h.subscribe(2, "music.queue");
        let mut other_surface = h.subscribe(1, "music.now_playing");
        h.publish(1, "music.queue", true, json!({"queue": []}));
        match a.recv().await {
            Some(Recv::Frame(f)) => assert_eq!(f.surface, "music.queue"),
            _ => panic!("expected frame"),
        }
        let none = tokio::time::timeout(Duration::from_millis(50), other_community.recv()).await;
        assert!(none.is_err(), "cross-community leak");
        let none = tokio::time::timeout(Duration::from_millis(50), other_surface.recv()).await;
        assert!(none.is_err(), "cross-surface leak");
    }

    #[test]
    fn replay_returns_last_frame_marked_and_isolated() {
        let h = hub();
        h.publish(1, "s", true, json!(1));
        h.publish(1, "s", true, json!(2));
        let r = h.replay_for(1, "s").expect("cached");
        assert!(r.replay);
        assert_eq!(r.payload, json!(2));
        assert!(h.replay_for(2, "s").is_none());
    }

    #[test]
    fn non_replay_surfaces_are_never_cached() {
        let h = hub();
        h.publish(1, "clip.alert", false, json!({}));
        assert!(h.replay_for(1, "clip.alert").is_none());
        assert_eq!(h.replay_len(), 0);
    }

    #[test]
    fn replay_cache_is_bounded_evicting_oldest() {
        let h = hub();
        for c in 1..=5 {
            h.publish(c, "s", true, json!(c));
        }
        assert_eq!(h.replay_len(), 3);
        assert!(h.replay_for(1, "s").is_none());
        assert!(h.replay_for(5, "s").is_some());
    }

    #[test]
    fn expired_entries_are_not_replayed() {
        let h = SurfaceHub::new(3, Duration::from_millis(1));
        h.publish(1, "s", true, json!(1));
        std::thread::sleep(Duration::from_millis(5));
        assert!(h.replay_for(1, "s").is_none());
        assert_eq!(h.replay_len(), 0);
    }

    #[test]
    fn sequence_is_monotonic_and_channels_are_collected() {
        let h = hub();
        let a = h.publish(1, "s", false, json!(1));
        let b = h.publish(1, "s", false, json!(2));
        assert!(b.seq > a.seq);
        let sub = h.subscribe(1, "s");
        assert_eq!(h.channel_count(), 1);
        drop(sub);
        assert_eq!(h.channel_count(), 0);
    }

    #[tokio::test]
    async fn slow_subscriber_lags_instead_of_blocking() {
        let h = hub();
        let mut sub = h.subscribe(1, "s");
        for i in 0..(CHANNEL_CAPACITY + 8) {
            h.publish(1, "s", false, json!(i));
        }
        assert!(matches!(sub.recv().await, Some(Recv::Lagged(n)) if n >= 8));
    }
}

//! The always-pinned connector registry (spec S2.3): the set of core
//! connector bundles this replica serves, registered once at startup and held
//! for the replica's lifetime.
//!
//! "Pinned" is structural: the registry has **no** removal, eviction, idle
//! timer or byte cap -- a connector is core, global and load-bearing for every
//! tenant's ingest on the replica, unlike a lazily-loaded, LRU-evicted tenant
//! app bundle. Re-registering a platform is an error, never a silent replace
//! (a swap must be a deliberate restart with the new digest).

use std::collections::HashMap;

/// Default ceiling on events accepted from one `on-frame` call (spec S3.5
/// `MAX_EVENTS_PER_FRAME`); the host truncates beyond it, logs a WARN and
/// counts a metric.
pub const MAX_EVENTS_PER_FRAME: usize = 64;

/// Default ceiling on one event's `payload_json`, bytes.
pub const MAX_EVENT_PAYLOAD_BYTES: usize = 64 * 1024;
/// Default ceiling on one event's `actor`, bytes.
pub const MAX_EVENT_ACTOR_BYTES: usize = 256;
/// Default ceiling on one event's `event_type`, bytes.
pub const MAX_EVENT_TYPE_BYTES: usize = 128;
/// Default ceiling on the summed `payload_json` bytes of ONE `on-frame`'s
/// events -- bounds the worst case far below `64 events x payload cap`.
pub const MAX_FRAME_EVENTS_BYTES: usize = 1024 * 1024;

/// Host-enforced size ceilings on what a connector returns (spec S3.5). A
/// violation is a typed [`super::SinkError`] (counted, loud), never a silent
/// truncation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EventLimits {
    /// Max bytes of one event's `payload_json`.
    pub max_payload_bytes: usize,
    /// Max bytes of one event's `actor`.
    pub max_actor_bytes: usize,
    /// Max bytes of one event's `event_type`.
    pub max_event_type_bytes: usize,
    /// Max summed `payload_json` bytes across one frame's events.
    pub max_frame_events_bytes: usize,
}

impl Default for EventLimits {
    fn default() -> Self {
        Self {
            max_payload_bytes: MAX_EVENT_PAYLOAD_BYTES,
            max_actor_bytes: MAX_EVENT_ACTOR_BYTES,
            max_event_type_bytes: MAX_EVENT_TYPE_BYTES,
            max_frame_events_bytes: MAX_FRAME_EVENTS_BYTES,
        }
    }
}

/// One pinned connector: which executor-held component serves a platform, and
/// the host-enforced per-frame ceiling for it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinnedConnector {
    /// Platform slug (`twitch`, `discord`, `echo`, ...); the registry key.
    pub platform: String,
    /// The connector bundle's `app_id` (core namespace).
    pub app_id: String,
    /// The component's content digest (`sha256:<hex>`), for logs/diagnostics.
    pub digest: String,
    /// Platform-specific frame-size ceiling; the **host** rejects an oversize
    /// frame before any guest runs (spec S3.5) -- never a guest-side check.
    pub max_frame_bytes: usize,
    /// Events accepted per `on-frame`; defaults to [`MAX_EVENTS_PER_FRAME`].
    pub max_events_per_frame: usize,
    /// Per-event and per-frame size ceilings; defaults to [`EventLimits::default`].
    pub event_limits: EventLimits,
}

impl PinnedConnector {
    /// Builds a pinned entry with the default events-per-frame cap and
    /// [`EventLimits`].
    #[must_use]
    pub fn new(
        platform: impl Into<String>,
        app_id: impl Into<String>,
        digest: impl Into<String>,
        max_frame_bytes: usize,
    ) -> Self {
        Self {
            platform: platform.into(),
            app_id: app_id.into(),
            digest: digest.into(),
            max_frame_bytes,
            max_events_per_frame: MAX_EVENTS_PER_FRAME,
            event_limits: EventLimits::default(),
        }
    }
}

/// Why registering or resolving a connector failed. Always typed and loud.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RegistryError {
    /// A required field was empty or a ceiling was zero.
    #[error("invalid pinned connector: {0}")]
    Invalid(&'static str),
    /// The platform already has a pinned connector; replacement is refused.
    #[error("a connector is already pinned for platform {0:?}")]
    AlreadyPinned(String),
    /// No connector is pinned for the platform: the transport has nothing to
    /// drive. Fail loud -- never fall back to a native normalizer silently.
    #[error("no connector is pinned for platform {0:?}")]
    NotRegistered(String),
}

/// The pinned set. Built once, then shared read-only (`Arc`).
#[derive(Debug, Default)]
pub struct ConnectorRegistry {
    pinned: HashMap<String, PinnedConnector>,
}

impl ConnectorRegistry {
    /// An empty registry.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Pins `connector` for its platform.
    pub fn register(&mut self, connector: PinnedConnector) -> Result<(), RegistryError> {
        if connector.platform.is_empty() {
            return Err(RegistryError::Invalid("platform must be non-empty"));
        }
        if connector.app_id.is_empty() || connector.digest.is_empty() {
            return Err(RegistryError::Invalid(
                "app_id and digest must be non-empty",
            ));
        }
        if connector.max_frame_bytes == 0 || connector.max_events_per_frame == 0 {
            return Err(RegistryError::Invalid(
                "max_frame_bytes and max_events_per_frame must be > 0",
            ));
        }
        let l = &connector.event_limits;
        if l.max_payload_bytes == 0
            || l.max_actor_bytes == 0
            || l.max_event_type_bytes == 0
            || l.max_frame_events_bytes == 0
        {
            return Err(RegistryError::Invalid("every event limit must be > 0"));
        }
        if self.pinned.contains_key(&connector.platform) {
            return Err(RegistryError::AlreadyPinned(connector.platform));
        }
        self.pinned.insert(connector.platform.clone(), connector);
        Ok(())
    }

    /// The pinned connector for `platform`, or [`RegistryError::NotRegistered`].
    pub fn resolve(&self, platform: &str) -> Result<&PinnedConnector, RegistryError> {
        self.pinned
            .get(platform)
            .ok_or_else(|| RegistryError::NotRegistered(platform.to_string()))
    }

    /// Number of pinned connectors.
    #[must_use]
    pub fn len(&self) -> usize {
        self.pinned.len()
    }

    /// True when nothing is pinned.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.pinned.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pinned(platform: &str) -> PinnedConnector {
        PinnedConnector::new(platform, "waddles.core.example.echo", "sha256:aa", 4096)
    }

    #[test]
    fn registers_and_resolves_a_pinned_connector() {
        let mut r = ConnectorRegistry::new();
        r.register(pinned("echo")).unwrap();
        assert_eq!(r.len(), 1);
        let p = r.resolve("echo").unwrap();
        assert_eq!(p.max_events_per_frame, MAX_EVENTS_PER_FRAME);
        assert_eq!(p.max_frame_bytes, 4096);
    }

    #[test]
    fn an_unregistered_platform_is_a_typed_error_never_a_default() {
        let r = ConnectorRegistry::new();
        assert_eq!(
            r.resolve("slack"),
            Err(RegistryError::NotRegistered("slack".to_string()))
        );
        assert!(r.is_empty());
    }

    #[test]
    fn re_registering_a_platform_is_refused_not_silently_replaced() {
        let mut r = ConnectorRegistry::new();
        r.register(pinned("echo")).unwrap();
        let mut other = pinned("echo");
        other.digest = "sha256:bb".to_string();
        assert_eq!(
            r.register(other),
            Err(RegistryError::AlreadyPinned("echo".to_string()))
        );
        assert_eq!(r.resolve("echo").unwrap().digest, "sha256:aa");
    }

    #[test]
    fn invalid_entries_are_refused() {
        let mut r = ConnectorRegistry::new();
        let mut bad = pinned("");
        assert!(matches!(
            r.register(bad.clone()),
            Err(RegistryError::Invalid(_))
        ));
        bad = pinned("echo");
        bad.max_frame_bytes = 0;
        assert!(matches!(r.register(bad), Err(RegistryError::Invalid(_))));
        let mut bad = pinned("echo");
        bad.app_id.clear();
        assert!(matches!(r.register(bad), Err(RegistryError::Invalid(_))));
        let mut bad = pinned("echo");
        bad.max_events_per_frame = 0;
        assert!(matches!(r.register(bad), Err(RegistryError::Invalid(_))));
        let mut bad = pinned("echo");
        bad.event_limits.max_payload_bytes = 0;
        assert!(matches!(r.register(bad), Err(RegistryError::Invalid(_))));
        assert!(r.is_empty(), "no invalid entry may have been inserted");
    }
}

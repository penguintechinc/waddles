//! Generic bundle -> presentation path: a bundle publishes structured state to
//! a contract-declared *surface* (`music.now_playing`, `live.channels`, ...);
//! svc-presentation validates it, caches the last state for replay-on-connect,
//! and fans it out to subscribed overlays and viewers.
//!
//! * [`contract`] -- the embedded `surfaces.v1.json`: ids, JSON Schemas, caps.
//! * [`SurfaceService::process`] -- the publish pipeline (size cap, rate
//!   limit, parse, sanitize, schema-validate, fan out), transport-free so it
//!   is unit-testable; [`http`] is the axum shell around it.
//! * [`hub`] -- per-(community, surface) fan-out + bounded replay cache.
//! * [`page`] -- the static OBS/webui page that templates frames (no markup
//!   ever comes from a payload).
//!
//! Trust model: only a hub-api-minted PUSH JWT scoped to the community may
//! publish; viewers hold the community's VIEW key (read-only). Every
//! structure is keyed by the credential's `community_id`, never by anything
//! in the request body.

pub mod contract;
pub mod http;
pub mod hub;
pub mod limiter;
pub mod page;
pub mod sanitize;

use std::sync::Arc;
use std::time::{Duration, Instant};

use prometheus::{IntCounterVec, Opts};

use contract::{ContractError, SurfaceContract, Violation};
use hub::{SurfaceFrame, SurfaceHub};
use limiter::{Denied, RateLimiter};

/// Why a publish was refused.
#[derive(Debug)]
pub enum PublishError {
    /// The surface id is not in the contract.
    UnknownSurface,
    /// The body exceeds the surface's cap.
    TooLarge {
        /// The cap that was exceeded.
        max: usize,
    },
    /// The (community, surface) bucket is empty.
    RateLimited {
        /// Whole seconds to wait.
        retry_after_secs: u64,
    },
    /// The body is not a JSON object/value.
    NotJson,
    /// A string held a control character, or nesting was too deep.
    Unsanitary(String),
    /// Schema violations (locations and rules only).
    Invalid(Vec<Violation>),
    /// The limiter's key table is saturated with live buckets.
    Saturated,
}

/// Prometheus handles: publish outcomes by surface (closed, contract-bounded
/// label set; unknown ids are folded into `unknown`).
#[derive(Clone)]
pub struct SurfaceMetrics {
    publishes_total: IntCounterVec,
    replays_total: IntCounterVec,
}

impl SurfaceMetrics {
    /// Registers the series on `registry`; call once per registry.
    pub fn register(registry: &prometheus::Registry) -> Result<Self, prometheus::Error> {
        let publishes_total = IntCounterVec::new(
            Opts::new(
                "svc_presentation_surface_publishes_total",
                "Bundle surface publishes by surface and outcome",
            ),
            &["surface", "outcome"],
        )?;
        registry.register(Box::new(publishes_total.clone()))?;
        let replays_total = IntCounterVec::new(
            Opts::new(
                "svc_presentation_surface_replays_total",
                "Cached surface frames replayed to a newly connected viewer",
            ),
            &["surface"],
        )?;
        registry.register(Box::new(replays_total.clone()))?;
        Ok(Self {
            publishes_total,
            replays_total,
        })
    }

    fn outcome(&self, surface: &str, outcome: &str) {
        self.publishes_total
            .with_label_values(&[surface, outcome])
            .inc();
    }

    /// Counts one replayed frame.
    pub fn replayed(&self, surface: &str) {
        self.replays_total.with_label_values(&[surface]).inc();
    }
}

/// Everything the surface routes share.
pub struct SurfaceService {
    /// The compiled contract.
    pub contract: SurfaceContract,
    /// Fan-out and replay cache.
    pub hub: SurfaceHub,
    /// Per-(community, surface) limiter.
    pub limiter: RateLimiter,
    /// Metrics.
    pub metrics: SurfaceMetrics,
}

impl SurfaceService {
    /// Builds the service from the embedded contract.
    pub fn new(registry: &prometheus::Registry) -> Result<Arc<Self>, SurfaceInitError> {
        let contract = SurfaceContract::load()?;
        let limits = contract.limits();
        let hub = SurfaceHub::new(
            limits.replay_max_entries,
            Duration::from_secs(limits.replay_ttl_seconds),
        );
        Ok(Arc::new(Self {
            contract,
            hub,
            limiter: RateLimiter::new(),
            metrics: SurfaceMetrics::register(registry)?,
        }))
    }

    /// The publish pipeline for one request. On success the frame has been
    /// cached (when the surface replays) and fanned out.
    pub fn process(
        &self,
        community_id: i64,
        surface_id: &str,
        body: &[u8],
        now: Instant,
    ) -> Result<Arc<SurfaceFrame>, PublishError> {
        let result = self.process_inner(community_id, surface_id, body, now);
        let label = if self.contract.get(surface_id).is_some() {
            surface_id
        } else {
            "unknown"
        };
        self.metrics.outcome(
            label,
            match &result {
                Ok(_) => "published",
                Err(PublishError::UnknownSurface) => "unknown_surface",
                Err(PublishError::TooLarge { .. }) => "too_large",
                Err(PublishError::RateLimited { .. }) | Err(PublishError::Saturated) => {
                    "rate_limited"
                }
                Err(PublishError::NotJson) => "not_json",
                Err(PublishError::Unsanitary(_)) => "unsanitary",
                Err(PublishError::Invalid(_)) => "invalid",
            },
        );
        result
    }

    fn process_inner(
        &self,
        community_id: i64,
        surface_id: &str,
        body: &[u8],
        now: Instant,
    ) -> Result<Arc<SurfaceFrame>, PublishError> {
        let spec = self
            .contract
            .get(surface_id)
            .ok_or(PublishError::UnknownSurface)?;
        if body.len() > spec.max_payload_bytes() {
            return Err(PublishError::TooLarge {
                max: spec.max_payload_bytes(),
            });
        }
        match self
            .limiter
            .check(community_id, surface_id, spec.rate_limit_per_minute(), now)
        {
            Ok(()) => {}
            Err(Denied::RateLimited(wait)) => {
                return Err(PublishError::RateLimited {
                    retry_after_secs: wait.as_secs().saturating_add(1),
                })
            }
            Err(Denied::Saturated) => return Err(PublishError::Saturated),
        }
        let value: serde_json::Value =
            serde_json::from_slice(body).map_err(|_| PublishError::NotJson)?;
        let value =
            sanitize::sanitize(value).map_err(|e| PublishError::Unsanitary(e.to_string()))?;
        spec.validate(&value).map_err(PublishError::Invalid)?;
        Ok(self
            .hub
            .publish(community_id, spec.id(), spec.replay(), value))
    }
}

/// Startup failure building the surface service.
#[derive(Debug, thiserror::Error)]
pub enum SurfaceInitError {
    /// The embedded contract is invalid.
    #[error(transparent)]
    Contract(#[from] ContractError),
    /// Metric registration failed.
    #[error("surface metrics: {0}")]
    Metrics(#[from] prometheus::Error),
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn svc() -> Arc<SurfaceService> {
        SurfaceService::new(&prometheus::Registry::new()).unwrap()
    }

    fn track() -> serde_json::Value {
        json!({"id":1,"provider":"yt","track_id":"abc","title":"T","artist":"A","duration_s":200,"url":"https://example.com/x"})
    }

    #[test]
    fn embedded_contract_loads_and_declares_the_required_surfaces() {
        let s = svc();
        for id in [
            "music.now_playing",
            "music.queue",
            "live.channels",
            "clip.alert",
            "alert",
        ] {
            assert!(s.contract.get(id).is_some(), "{id}");
        }
        assert!(s.contract.get("music.queue").unwrap().replay());
        assert!(!s.contract.get("clip.alert").unwrap().replay());
    }

    #[test]
    fn contract_copies_are_byte_identical() {
        let canonical = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../bundles/contracts/surfaces.v1.json"
        ))
        .expect("bundles/contracts/surfaces.v1.json must exist");
        assert_eq!(canonical, contract::CONTRACT_JSON);
    }

    #[test]
    fn valid_publish_is_fanned_out_and_cached() {
        let s = svc();
        let body = json!({"now_playing": track(), "updated_ms": 5}).to_string();
        let f = s
            .process(7, "music.now_playing", body.as_bytes(), Instant::now())
            .unwrap();
        assert_eq!(f.surface, "music.now_playing");
        assert!(s.hub.replay_for(7, "music.now_playing").is_some());
        assert!(s.hub.replay_for(8, "music.now_playing").is_none());
    }

    #[test]
    fn schema_violations_are_rejected_without_echoing_values() {
        let s = svc();
        for (surface, bad) in [
            ("music.now_playing", json!({"now_playing": null})),
            (
                "music.now_playing",
                json!({"now_playing": null, "updated_ms": 1, "html": "<b>"}),
            ),
            ("music.queue", json!({"queue": "x", "updated_ms": 1})),
            (
                "clip.alert",
                json!({"clip_id":"a b","url":"https://x.tv/c","title":"t","created_ms":1}),
            ),
            (
                "clip.alert",
                json!({"clip_id":"abc","url":"javascript:alert(1)","title":"t","created_ms":1}),
            ),
            (
                "clip.alert",
                json!({"clip_id":"abc","url":"http://insecure.tv/c","title":"t","created_ms":1}),
            ),
            ("alert", json!({"alert_type":"nope","message":"m"})),
        ] {
            match s.process(1, surface, bad.to_string().as_bytes(), Instant::now()) {
                Err(PublishError::Invalid(v)) => {
                    assert!(!v.is_empty(), "{surface}");
                    for violation in &v {
                        assert!(!violation.path.contains("javascript"));
                    }
                }
                other => panic!("{surface} {bad}: expected Invalid, got {other:?}"),
            }
        }
        assert_eq!(s.hub.replay_len(), 0, "nothing invalid may be cached");
    }

    #[test]
    fn queue_over_fifty_tracks_is_rejected() {
        let s = svc();
        let tracks: Vec<_> = (0..51).map(|_| json!({"id":1,"provider":"p","track_id":"t","title":"","artist":"","duration_s":1})).collect();
        let body = json!({"queue": tracks, "updated_ms": 1}).to_string();
        assert!(matches!(
            s.process(1, "music.queue", body.as_bytes(), Instant::now()),
            Err(PublishError::Invalid(_))
        ));
    }

    #[test]
    fn oversize_unknown_notjson_and_control_chars_are_refused() {
        let s = svc();
        let big = vec![b' '; 5000];
        assert!(matches!(
            s.process(1, "music.now_playing", &big, Instant::now()),
            Err(PublishError::TooLarge { max: 4096 })
        ));
        assert!(matches!(
            s.process(1, "nope", b"{}", Instant::now()),
            Err(PublishError::UnknownSurface)
        ));
        assert!(matches!(
            s.process(1, "alert", b"not json", Instant::now()),
            Err(PublishError::NotJson)
        ));
        let ctl = json!({"alert_type":"info","message":"a\u{1b}[31m"}).to_string();
        assert!(matches!(
            s.process(1, "alert", ctl.as_bytes(), Instant::now()),
            Err(PublishError::Unsanitary(_))
        ));
    }

    #[test]
    fn rate_limit_is_per_community_and_surface() {
        let s = svc();
        let body = json!({"alert_type":"info","message":"m"}).to_string();
        let now = Instant::now();
        let mut limited = false;
        for _ in 0..100 {
            if matches!(
                s.process(1, "alert", body.as_bytes(), now),
                Err(PublishError::RateLimited { .. })
            ) {
                limited = true;
                break;
            }
        }
        assert!(limited, "burst must eventually be limited");
        assert!(s.process(2, "alert", body.as_bytes(), now).is_ok());
    }

    #[test]
    fn user_tokens_in_payload_strings_never_reach_the_frame() {
        let s = svc();
        let body =
            json!({"alert_type":"info","message":"hi {user:11111111-1111-1111-1111-111111111111}"})
                .to_string();
        let f = s
            .process(1, "alert", body.as_bytes(), Instant::now())
            .unwrap();
        assert_eq!(f.payload["message"], "hi Unknown User");
    }

    #[test]
    fn bad_contracts_fail_loudly() {
        assert!(SurfaceContract::from_json("{").is_err());
        let bad_limit = r#"{"limits":{"hard_max_payload_bytes":10,"replay_max_entries":1,"replay_ttl_seconds":1},"surfaces":{"x":{"replay":true,"max_payload_bytes":99,"rate_limit_per_minute":1,"schema":{}}}}"#;
        assert!(matches!(
            SurfaceContract::from_json(bad_limit),
            Err(ContractError::BadLimit { .. })
        ));
        let bad_schema = r#"{"limits":{"hard_max_payload_bytes":10,"replay_max_entries":1,"replay_ttl_seconds":1},"surfaces":{"x":{"replay":true,"max_payload_bytes":5,"rate_limit_per_minute":1,"schema":{"type":"nonsense"}}}}"#;
        assert!(matches!(
            SurfaceContract::from_json(bad_schema),
            Err(ContractError::BadSchema { .. })
        ));
    }
}

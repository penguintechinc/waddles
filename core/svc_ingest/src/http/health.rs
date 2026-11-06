//! `/health` (rich), `/healthz` (bare, for the Kubernetes probes), and the
//! Prometheus `/metrics` handler mounted on the secondary metrics router --
//! see `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` S13.4.
//!
//! `/health` proves the process is alive and additionally reports
//! configured dependencies without ever crashing the process on a
//! transient dependency outage; `/healthz` is a bare liveness probe for
//! Kubernetes, deliberately as cheap as possible.

use axum::extract::State;
use axum::Json;
use serde::Serialize;

use crate::connector_runtime::ready::PlatformSnapshot;
use crate::error::ApiError;
use crate::http::AppState;

/// Per-dependency configuration snapshot reported on `/health`.
#[derive(Debug, Serialize)]
pub struct DependencyStatus {
    pub name: &'static str,
    pub configured: bool,
    pub detail: String,
}

/// `GET /health` response body.
#[derive(Debug, Serialize)]
pub struct HealthBody {
    pub status: &'static str,
    pub uptime_seconds: u64,
    pub dependencies: Vec<DependencyStatus>,
}

/// `GET /health` -- rich liveness: the process is up and answering HTTP,
/// plus a configuration snapshot of this service's dependencies. This
/// skeleton has exactly one: the hub-api distribution poll target, which
/// nothing calls yet (see the `TODO(M5)` seam in `src/lib.rs`) -- reporting
/// `configured` here is a config check, not a connectivity check, matching
/// `svc_streaming`'s own scaffold-stage readiness handler.
pub async fn liveness(State(state): State<AppState>) -> Json<HealthBody> {
    let dependencies = vec![DependencyStatus {
        name: "hub_api",
        configured: !state.config.cli.hub_api_url.is_empty(),
        detail: state.config.cli.hub_api_url.clone(),
    }];
    Json(HealthBody {
        status: "ok",
        uptime_seconds: state.started_at.elapsed().as_secs(),
        dependencies,
    })
}

/// `GET /healthz` -- bare liveness probe for Kubernetes: no dependency
/// reporting, no JSON body, just `200 ok`. Never checks external
/// dependencies; a slow/unreachable dependency must not fail liveness and
/// trigger a restart loop.
pub async fn healthz() -> &'static str {
    "ok"
}

/// Per-receiver readiness status reported on `/readyz`.
#[derive(Debug, Serialize)]
pub struct ReceiverReadinessStatus {
    pub name: &'static str,
    pub enabled: bool,
    pub ready: bool,
}

/// Connector-runtime platform readiness reported on `/readyz`.
#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct OverlayReadinessStatus {
    pub platform: &'static str,
    pub enabled: bool,
    pub ready: bool,
    /// Fixed PII-free reason label while not ready.
    pub reason: Option<&'static str>,
    pub live: u32,
    pub desired: u32,
}

/// Maps one overlay status onto its `/readyz` line.
fn overlay_line(s: &crate::OverlayStatus) -> OverlayReadinessStatus {
    use crate::connector_runtime::Readiness;
    let (ready, reason, live, desired) = match s.readiness {
        Readiness::Disabled => (true, None, 0, 0),
        Readiness::NotReady(r) => (false, Some(r), 0, 0),
        Readiness::Ready { live, desired } => (true, None, live, desired),
    };
    OverlayReadinessStatus {
        platform: s.platform,
        enabled: !matches!(s.readiness, Readiness::Disabled),
        ready,
        reason,
        live,
        desired,
    }
}

/// `GET /readyz` response body.
#[derive(Debug, Serialize)]
pub struct ReadyBody {
    pub status: &'static str,
    pub receivers: Vec<ReceiverReadinessStatus>,
    pub overlay: Vec<OverlayReadinessStatus>,
    /// Connector-runtime overlay platforms (enabled, executor_attached,
    /// pinned_ok, desired, live, discovery_failed); absent when the runtime is
    /// not enabled in this process.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub overlays: Vec<PlatformSnapshot>,
}

/// `GET /readyz` -- real Kubernetes readiness: `503` while any *enabled*
/// receiver (one that passed its own config checks and started attempting a
/// spine connect, `crate::lib::try_start_twitch_irc`/`try_start_discord`/
/// `try_start_twitch_outbound`) has not yet connected. Closes the "pod
/// stayed Running 1/1 forever with the Discord/Twitch receivers permanently
/// disabled" gap -- regression: one-shot valkey probe disabled discord
/// receiver (alpha 2026-10-02). Unlike `/healthz` (always `ok`, the cheap
/// liveness target), this is the endpoint the Kubernetes readinessProbe
/// must point at.
pub async fn readyz(State(state): State<AppState>) -> (axum::http::StatusCode, Json<ReadyBody>) {
    let r = &state.receiver_readiness;
    let receivers = vec![
        ReceiverReadinessStatus {
            name: "twitch_irc",
            enabled: r.twitch_irc.is_enabled(),
            ready: r.twitch_irc.is_ready(),
        },
        ReceiverReadinessStatus {
            name: "discord_gateway",
            enabled: r.discord.is_enabled(),
            ready: r.discord.is_ready(),
        },
        ReceiverReadinessStatus {
            name: "twitch_outbound",
            enabled: r.twitch_outbound.is_enabled(),
            ready: r.twitch_outbound.is_ready(),
        },
    ];
    let overlays = match crate::connector_runtime::ready::installed() {
        Some(status) => status.snapshots(),
        None => Vec::new(),
    };
    let overlay: Vec<OverlayReadinessStatus> =
        r.overlay_status().iter().map(overlay_line).collect();
    compose_ready(r.all_ready(), receivers, overlay, overlays)
}

/// Builds the `/readyz` answer: `503` unless every enabled receiver AND every
/// enabled overlay platform is ready.
fn compose_ready(
    receivers_ok: bool,
    receivers: Vec<ReceiverReadinessStatus>,
    overlay: Vec<OverlayReadinessStatus>,
    overlays: Vec<PlatformSnapshot>,
) -> (axum::http::StatusCode, Json<ReadyBody>) {
    let ok = receivers_ok && overlays.iter().all(|o| o.ready);
    let code = if ok {
        axum::http::StatusCode::OK
    } else {
        axum::http::StatusCode::SERVICE_UNAVAILABLE
    };
    (
        code,
        Json(ReadyBody {
            status: if ok { "ok" } else { "degraded" },
            receivers,
            overlay,
            overlays,
        }),
    )
}

/// `GET /metrics` (secondary router, `METRICS_PORT`) -- Prometheus text
/// exposition. A registry gather/encode failure returns 500 rather than
/// panicking; a scrape failure must never crash the process.
pub async fn metrics(State(state): State<AppState>) -> Result<String, ApiError> {
    crate::telemetry::render_metrics(&state.metrics).map_err(ApiError::Internal)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config};
    use crate::http::AppState;
    use clap::Parser;

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let config = Config::from_cli(cli).expect("defaults require no secrets");
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn liveness_reports_ok_and_one_dependency() {
        let Json(body) = liveness(State(test_state())).await;
        assert_eq!(body.status, "ok");
        assert_eq!(body.dependencies.len(), 1);
        assert_eq!(body.dependencies[0].name, "hub_api");
        assert!(body.dependencies[0].configured);
    }

    #[tokio::test]
    async fn healthz_returns_bare_ok() {
        assert_eq!(healthz().await, "ok");
    }

    // regression: one-shot valkey probe disabled discord receiver (alpha 2026-10-02)
    #[tokio::test]
    async fn readyz_is_ready_when_no_receiver_is_enabled() {
        let (code, Json(body)) = readyz(State(test_state())).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert_eq!(body.status, "ok");
        assert!(body.receivers.iter().all(|r| !r.enabled));
    }

    // regression: one-shot valkey probe disabled discord receiver (alpha 2026-10-02)
    #[tokio::test]
    async fn readyz_is_not_ready_when_an_enabled_receiver_has_not_connected() {
        let state = test_state();
        state.receiver_readiness.discord.mark_enabled();
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body.status, "degraded");
        let discord = body
            .receivers
            .iter()
            .find(|r| r.name == "discord_gateway")
            .unwrap();
        assert!(discord.enabled);
        assert!(!discord.ready);
    }

    // regression: one-shot valkey probe disabled discord receiver (alpha 2026-10-02)
    #[tokio::test]
    async fn readyz_is_ready_once_the_enabled_receiver_connects() {
        let state = test_state();
        state.receiver_readiness.discord.mark_enabled();
        state.receiver_readiness.discord.set_ready(true);
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert_eq!(body.status, "ok");
    }

    fn snap(ready: bool) -> PlatformSnapshot {
        PlatformSnapshot {
            platform: "twitch",
            enabled: true,
            executor_attached: true,
            pinned_ok: ready,
            desired: 2,
            live: u32::from(ready),
            discovery_failed: false,
            ready,
            reason: if ready {
                None
            } else {
                Some("digest_unverified")
            },
        }
    }

    #[test]
    fn readyz_is_degraded_when_an_overlay_platform_is_not_ready() {
        let (code, Json(body)) = compose_ready(true, vec![], vec![], vec![snap(false)]);
        assert_eq!(code, axum::http::StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body.status, "degraded");
        let v = serde_json::to_value(&body).unwrap();
        let o = &v["overlays"][0];
        for k in [
            "enabled",
            "executor_attached",
            "pinned_ok",
            "desired",
            "live",
            "discovery_failed",
        ] {
            assert!(o.get(k).is_some(), "overlay field {k} missing");
        }
        assert_eq!(o["reason"], "digest_unverified");
    }

    #[test]
    fn readyz_is_ok_when_overlays_and_receivers_are_ready_and_omits_empty_overlays() {
        let (code, _) = compose_ready(true, vec![], vec![], vec![snap(true)]);
        assert_eq!(code, axum::http::StatusCode::OK);
        let (code, Json(body)) = compose_ready(true, vec![], vec![], vec![]);
        assert_eq!(code, axum::http::StatusCode::OK);
        assert!(serde_json::to_value(&body)
            .unwrap()
            .get("overlays")
            .is_none());
        let (code, _) = compose_ready(false, vec![], vec![], vec![snap(true)]);
        assert_eq!(code, axum::http::StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn readyz_overlay_on_without_a_runtime_is_not_ready_with_reason() {
        let state = test_state();
        state.receiver_readiness.set_overlay(vec!["twitch"], None);
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(body.status, "degraded");
        assert_eq!(
            body.overlay,
            vec![OverlayReadinessStatus {
                platform: "twitch",
                enabled: true,
                ready: false,
                reason: Some("runtime_not_wired"),
                live: 0,
                desired: 0,
            }]
        );
    }

    struct Fixed(crate::connector_runtime::Readiness);
    impl crate::connector_runtime::RuntimeReady for Fixed {
        fn ready(&self, _platform: &str) -> crate::connector_runtime::Readiness {
            self.0
        }
    }

    #[tokio::test]
    async fn readyz_maps_runtime_readiness_per_platform() {
        use crate::connector_runtime::Readiness;
        for (r, code, ready, reason, live) in [
            (
                Readiness::Ready {
                    live: 2,
                    desired: 3,
                },
                axum::http::StatusCode::OK,
                true,
                None,
                2,
            ),
            (
                Readiness::NotReady("first_reconcile_pending"),
                axum::http::StatusCode::SERVICE_UNAVAILABLE,
                false,
                Some("first_reconcile_pending"),
                0,
            ),
        ] {
            let state = test_state();
            state
                .receiver_readiness
                .set_overlay(vec!["twitch"], Some(std::sync::Arc::new(Fixed(r))));
            let (c, Json(body)) = readyz(State(state)).await;
            assert_eq!(c, code);
            assert_eq!(body.overlay[0].ready, ready);
            assert_eq!(body.overlay[0].reason, reason);
            assert_eq!(body.overlay[0].live, live);
        }
    }

    #[tokio::test]
    async fn readyz_disabled_runtime_platform_does_not_block() {
        let state = test_state();
        state.receiver_readiness.set_overlay(
            vec!["youtube"],
            Some(std::sync::Arc::new(Fixed(
                crate::connector_runtime::Readiness::Disabled,
            ))),
        );
        let (code, Json(body)) = readyz(State(state)).await;
        assert_eq!(code, axum::http::StatusCode::OK);
        assert!(!body.overlay[0].enabled);
    }

    #[tokio::test]
    async fn readyz_without_overlay_has_no_overlay_lines() {
        let (_, Json(body)) = readyz(State(test_state())).await;
        assert!(body.overlay.is_empty());
    }

    #[test]
    fn set_overlay_twice_keeps_the_first() {
        let r = crate::ReceiverReadiness::new();
        r.set_overlay(vec!["twitch"], None);
        r.set_overlay(vec!["youtube"], None);
        assert_eq!(r.overlay_status()[0].platform, "twitch");
    }

    #[tokio::test]
    async fn metrics_renders_base_metrics_without_error() {
        // `AppState::new` registers the `up` gauge (and request
        // counter/histogram) eagerly, so `/metrics` is never an empty body
        // even before the first request is served.
        let body = metrics(State(test_state())).await.expect("must not error");
        assert!(body.contains("svc_ingest_up 1"));
    }
}

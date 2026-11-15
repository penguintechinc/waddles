//! Stage side of the bundle `overlay.publish-surface` host capability
//! (`wit/waddle-bundle/stage.wit`, host-call `db` / op `surface.publish`) for PROCESS-stage
//! bundles (chat-command bundles such as the music queue publish their own state this way).
//!
//! Mirrors `core/svc_action/src/surface.rs`; the decision logic lives in
//! `bundle_host_surface::publish_for_app` so both stages enforce the same rules: the community and
//! app id come from the host-verified invocation scope (never an argument), a bundle owns only the
//! surfaces under its own `<app_id>.` namespace, delivery is gRPC to svc-presentation with a
//! community-bound JWT.
//!
//! Fail-loud order: unwired -> `not_implemented`; flag OFF -> `feature_disabled`; otherwise the
//! host's own stable code. Logs carry ids and codes only, never payload content.

use std::sync::Arc;

use bundle_host_surface::{SurfaceError, SurfaceHost};
use penguin_bundle_host::wire::HostResultError;

use crate::capabilities::denied;
use crate::config::CliConfig;
use crate::license::FeatureGate;

/// Production wiring: the delivery host plus the default-OFF flag that gates the capability.
#[derive(Clone)]
pub struct SurfaceWiring {
    /// Delivery to svc-presentation over gRPC.
    pub host: Arc<SurfaceHost>,
    /// `waddles.overlay-surfaces`: OFF denies `feature_disabled` before any token is minted.
    pub flag: Arc<dyn FeatureGate>,
}

/// Machine-JWT scope hub-api mints for presentation publishes (bound per community at mint time).
pub const PUSH_BASE_SCOPE: &str = "presentation.overlay:push";

/// Builds the wiring, or `None` with a loud log naming what is missing (every `surface.publish`
/// then denies `not_implemented`). Must run inside a Tokio runtime.
pub fn build_surface_wiring(cli: &CliConfig, flag: Arc<dyn FeatureGate>) -> Option<SurfaceWiring> {
    let endpoint = cli.presentation_grpc_endpoint.trim();
    if endpoint.is_empty() {
        tracing::warn!(
            "surface capability: PRESENTATION_GRPC_ENDPOINT unset -- capability unavailable \
             (surface.publish reports not_implemented)"
        );
        return None;
    }
    if cli.service_jwt_token_endpoint.trim().is_empty() {
        tracing::error!(
            "surface capability: SERVICE_JWT_TOKEN_ENDPOINT unset (the community-bound push JWT \
             needs hub-api) -- capability unavailable (surface.publish reports not_implemented)"
        );
        return None;
    }
    let tokens = match service_auth::BoundMachineJwtClient::with_timeouts(
        cli.service_jwt_token_endpoint.trim(),
        cli.service_jwt_sa_token_path.clone(),
        PUSH_BASE_SCOPE,
        service_auth::HttpTimeouts::from_env(),
    ) {
        Ok(t) => t,
        Err(e) => {
            tracing::error!(error = %e, "surface capability: machine-JWT client misconfigured -- capability unavailable");
            return None;
        }
    };
    let ca = Some(cli.presentation_grpc_ca_file.trim()).filter(|p| !p.is_empty());
    match SurfaceHost::new(endpoint, ca, Arc::new(tokens)) {
        Ok(host) => {
            tracing::info!("surface capability: configured (flag-gated, default OFF)");
            Some(SurfaceWiring {
                host: Arc::new(host),
                flag,
            })
        }
        Err(e) => {
            tracing::error!(error = %e, "surface capability: presentation endpoint misconfigured -- capability unavailable");
            None
        }
    }
}

/// Maps a delivery failure to the executor's error body. A rate limit's message carries
/// `retry_after_ms=<n>` (parsed by the executor into `retry-after-ms`).
fn error_body(err: &SurfaceError) -> HostResultError {
    denied(err.code(), err.to_string())
}

/// Handles one `surface.publish` host call for `app_id` / `community_id` (both host-derived).
pub async fn publish(
    wiring: Option<&SurfaceWiring>,
    app_id: &str,
    community_id: i64,
    args: &serde_json::Value,
) -> Result<serde_json::Value, HostResultError> {
    let wiring = wiring.ok_or_else(|| {
        denied(
            "not_implemented",
            "surface publishing is not wired on this stage (PRESENTATION_GRPC_ENDPOINT)",
        )
    })?;
    if !wiring.flag.enabled().await {
        return Err(denied(
            "feature_disabled",
            "surface publishing is disabled (waddles.overlay-surfaces)",
        ));
    }
    bundle_host_surface::publish_for_app(&wiring.host, app_id, community_id, args)
        .await
        .map_err(|e| error_body(&e))?;
    Ok(serde_json::json!({ "published": true }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use bundle_host_surface::pb::surface_service_server::{SurfaceService, SurfaceServiceServer};
    use bundle_host_surface::pb::{PublishRequest, PublishResponse};
    use bundle_host_surface::PushTokenSource;
    use std::future::Future;
    use std::pin::Pin;
    use std::sync::Mutex;
    use tonic::{Request, Response, Status};

    const APP: &str = "waddles.core.example.music";
    const OWN: &str = "waddles.core.example.music.now_playing";

    struct Flag(bool);

    impl FeatureGate for Flag {
        fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
            Box::pin(async move { self.0 })
        }
    }

    #[derive(Default)]
    struct Seen(Mutex<Vec<(i64, String, String)>>);

    struct Fake(Arc<Seen>);

    #[tonic::async_trait]
    impl SurfaceService for Fake {
        async fn publish(
            &self,
            request: Request<PublishRequest>,
        ) -> Result<Response<PublishResponse>, Status> {
            let bearer = request
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_string();
            let r = request.into_inner();
            self.0
                 .0
                .lock()
                .unwrap()
                .push((r.community_id, r.surface_id.clone(), bearer));
            if r.payload_json.contains("limit") {
                let mut s = Status::resource_exhausted("rate limited");
                s.metadata_mut().insert(
                    bundle_host_surface::RETRY_AFTER_MS_KEY,
                    "1500".parse().unwrap(),
                );
                return Err(s);
            }
            Ok(Response::new(PublishResponse { seq: 1 }))
        }
    }

    struct Tokens;

    #[async_trait]
    impl PushTokenSource for Tokens {
        async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String> {
            Ok(Arc::from(format!("jwt-for-{community_id}")))
        }
    }

    async fn wiring(flag: bool) -> (SurfaceWiring, Arc<Seen>) {
        let seen = Arc::new(Seen::default());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let svc = SurfaceServiceServer::new(Fake(seen.clone()));
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(svc)
                .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
                .await
                .unwrap();
        });
        let host = SurfaceHost::new(&format!("http://{addr}"), None, Arc::new(Tokens)).unwrap();
        (
            SurfaceWiring {
                host: Arc::new(host),
                flag: Arc::new(Flag(flag)),
            },
            seen,
        )
    }

    fn args(surface: &str, payload: &str) -> serde_json::Value {
        serde_json::json!({"surface_id": surface, "payload_json": payload})
    }

    #[tokio::test]
    async fn an_unwired_stage_fails_loud_not_implemented() {
        let e = publish(None, APP, 7, &args(OWN, "{}")).await.unwrap_err();
        assert_eq!(e.code, "not_implemented");
    }

    #[tokio::test]
    async fn the_flag_off_denies_before_any_delivery() {
        let (w, seen) = wiring(false).await;
        let e = publish(Some(&w), APP, 7, &args(OWN, "{}"))
            .await
            .unwrap_err();
        assert_eq!(e.code, "feature_disabled");
        assert!(seen.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn an_owned_surface_is_delivered_for_the_invocations_own_community() {
        let (w, seen) = wiring(true).await;
        let v = publish(Some(&w), APP, 7, &args(OWN, r#"{"now_playing":null}"#))
            .await
            .unwrap();
        assert_eq!(v["published"], true);
        assert_eq!(
            seen.0.lock().unwrap().clone(),
            vec![(7, OWN.to_string(), "Bearer jwt-for-7".to_string())]
        );
    }

    #[tokio::test]
    async fn a_bundle_cannot_publish_into_another_bundles_namespace() {
        let (w, seen) = wiring(true).await;
        for surface in [
            "waddles.core.example.dj.now_playing",
            "waddles.core.example.musicbox.queue",
            "alert",
        ] {
            let e = publish(Some(&w), APP, 7, &args(surface, "{}"))
                .await
                .unwrap_err();
            assert_eq!(e.code, "not_granted", "{surface}");
        }
        assert!(seen.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn bad_args_a_missing_community_and_rate_limits_map_to_stable_codes() {
        let (w, seen) = wiring(true).await;
        for bad in [
            serde_json::json!({"surface_id": OWN}),
            serde_json::json!({"surface_id": OWN, "payload_json": "{}", "community_id": 9}),
        ] {
            assert_eq!(
                publish(Some(&w), APP, 7, &bad).await.unwrap_err().code,
                "invalid"
            );
        }
        assert_eq!(
            publish(Some(&w), APP, 0, &args(OWN, "{}"))
                .await
                .unwrap_err()
                .code,
            "invalid"
        );
        assert!(seen.0.lock().unwrap().is_empty());
        let e = publish(Some(&w), APP, 7, &args(OWN, r#"{"x":"limit"}"#))
            .await
            .unwrap_err();
        assert_eq!(e.code, "rate_limited");
        assert!(e.message.contains("retry_after_ms=1500"), "{}", e.message);
    }

    #[tokio::test]
    async fn build_wiring_requires_both_endpoints() {
        use clap::Parser;
        let cli = CliConfig::parse_from(["svc-process"]);
        assert!(build_surface_wiring(&cli, Arc::new(Flag(true))).is_none());
        let cli = CliConfig::parse_from([
            "svc-process",
            "--presentation-grpc-endpoint",
            "http://127.0.0.1:50051",
        ]);
        assert!(build_surface_wiring(&cli, Arc::new(Flag(true))).is_none());
    }
}

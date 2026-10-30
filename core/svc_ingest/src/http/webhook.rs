//! `POST /intake/webhook/{route_key}` -- the axum adapter over
//! [`crate::connector::webhook::WebhookIngress`]. Thin by design: extraction
//! and status mapping only; every verification branch is unit-tested in
//! `connector::webhook{,_verify}`. The route key is the ONLY thing taken from
//! the request for lookup; scheme, secret reference and owner are host
//! registration data. Every rejection is content-free and uniform.

use axum::body::Bytes;
use std::net::SocketAddr;

type ConnectInfoRejection =
    <ConnectInfo<SocketAddr> as axum::extract::FromRequestParts<AppState>>::Rejection;

use axum::extract::{ConnectInfo, DefaultBodyLimit, Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};

use crate::connector::webhook::{WebhookOutcome, HARD_MAX_BODY_BYTES};
use crate::http::AppState;

/// Mounts the generic webhook route with the hard body ceiling (the per-route
/// `body_limit` is enforced inside the ingress).
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/intake/webhook/{route_key}", post(intake))
        .route_layer(DefaultBodyLimit::max(HARD_MAX_BODY_BYTES))
        .with_state(state)
}

/// Maps an outcome to a fixed status/body -- no oracle beyond the status.
fn render(outcome: WebhookOutcome) -> Response {
    let (status, tag) = match outcome {
        WebhookOutcome::Accepted => (StatusCode::OK, "ok"),
        WebhookOutcome::Duplicate => (StatusCode::OK, "duplicate_ignored"),
        WebhookOutcome::NotFound => (StatusCode::NOT_FOUND, "not_found"),
        WebhookOutcome::TooLarge => (StatusCode::PAYLOAD_TOO_LARGE, "too_large"),
        WebhookOutcome::RateLimited => (StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
        WebhookOutcome::Rejected => (StatusCode::FORBIDDEN, "forbidden"),
        WebhookOutcome::Unavailable => (StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
    };
    (status, Json(serde_json::json!({ "status": tag }))).into_response()
}

async fn intake(
    State(state): State<AppState>,
    Path(route_key): Path<String>,
    peer: Result<ConnectInfo<SocketAddr>, ConnectInfoRejection>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Some(ingress) = state.webhook.as_ref() else {
        tracing::warn!("generic webhook ingress not configured; rejecting delivery");
        return render(WebhookOutcome::Unavailable);
    };
    // Peer IP (when the server provides ConnectInfo) is used only to
    // throttle that source's failed verifications.
    let source = peer.ok().map(|ConnectInfo(a)| a.ip().to_string());
    render(
        ingress
            .handle_from(
                &route_key,
                source.as_deref(),
                &headers,
                &body,
                chrono::Utc::now().timestamp(),
            )
            .await,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config};
    use crate::connector::webhook::{
        BoxFut, DedupToken, RateLimit, SecretError, WebhookDedup, WebhookIngress, WebhookRoute,
        WebhookSecretSource,
    };
    use crate::connector::webhook_verify::{
        hex_encode, Encoding, HmacSpec, Scheme, SignedTemplate, TimestampFormat,
    };
    use axum::body::Body;
    use axum::http::{HeaderName, Request};
    use clap::Parser;
    use connection_resolver::{ConnectionRef, SecretString};
    use hmac::{Hmac, Mac};
    use std::sync::Arc;
    use std::time::Duration;
    use tower::ServiceExt;

    struct Sec;
    impl WebhookSecretSource for Sec {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            Box::pin(async { Ok(SecretString::new("s3cret".into())) })
        }
    }
    struct Dd;
    impl WebhookDedup for Dd {
        fn first_seen<'a>(&'a self, _k: &'a str) -> BoxFut<'a, Result<Option<DedupToken>, ()>> {
            Box::pin(async { Ok(Some(DedupToken("t".into()))) })
        }
        fn release<'a>(&'a self, _k: &'a str, _t: &'a DedupToken) -> BoxFut<'a, Result<(), ()>> {
            Box::pin(async { Ok(()) })
        }
    }

    fn state(
        with_ingress: bool,
    ) -> (
        AppState,
        Option<crate::connector::webhook::WebhookTransport>,
    ) {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let mut st = AppState::new(
            Config::from_cli(cli).expect("defaults"),
            prometheus::Registry::new(),
        );
        if !with_ingress {
            return (st, None);
        }
        let ing = WebhookIngress::new(Arc::new(Sec), Arc::new(Dd), None);
        ing.register(
            WebhookRoute {
                route_key: "route-key-http-1".into(),
                owner_bundle: "acme".into(),
                scheme: Scheme::HmacSha256(HmacSpec {
                    signature_header: HeaderName::from_static("x-sig"),
                    timestamp_header: HeaderName::from_static("x-ts"),
                    id_header: None,
                    template: SignedTemplate::parse("{ts}.{body}").unwrap(),
                    signature_prefix: String::new(),
                    encoding: Encoding::Hex,
                    timestamp_format: TimestampFormat::UnixSeconds,
                }),
                secret_ref: ConnectionRef {
                    community_id: 1,
                    connection_id: 2,
                },
                replay_window: Duration::from_secs(300),
                rate_limit: RateLimit {
                    max: 100,
                    per: Duration::from_secs(60),
                },
                body_limit: 64,
            },
            1,
        )
        .unwrap();
        let t = ing.bind_transport("route-key-http-1", "acme").unwrap();
        st.webhook = Some(Arc::new(ing));
        (st, Some(t))
    }

    fn req(uri: &str, body: Vec<u8>, sig: Option<String>) -> Request<Body> {
        let ts = chrono::Utc::now().timestamp();
        let mut b = Request::builder()
            .method("POST")
            .uri(uri)
            .header("x-ts", ts.to_string());
        if let Some(s) = sig {
            b = b.header("x-sig", s);
        }
        b.body(Body::from(body)).unwrap()
    }

    fn sign(body: &[u8]) -> String {
        let ts = chrono::Utc::now().timestamp();
        let mut m = Hmac::<sha2::Sha256>::new_from_slice(b"s3cret").unwrap();
        m.update(format!("{ts}.").as_bytes());
        m.update(body);
        hex_encode(&m.finalize().into_bytes())
    }

    #[tokio::test]
    async fn unconfigured_ingress_is_503() {
        let (st, _) = state(false);
        let r = router(st)
            .oneshot(req("/intake/webhook/route-key-http-1", vec![], None))
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn valid_signed_post_is_200_and_forged_is_403_unknown_is_404() {
        let (st, mut t) = state(true);
        let app = router(st);
        let body = b"{\"k\":1}".to_vec();
        let ok = app
            .clone()
            .oneshot(req(
                "/intake/webhook/route-key-http-1",
                body.clone(),
                Some(sign(&body)),
            ))
            .await
            .unwrap();
        assert_eq!(ok.status(), StatusCode::OK);
        use crate::connector::transport::Transport;
        assert_eq!(
            t.as_mut().unwrap().next_frame().await.unwrap().unwrap(),
            body
        );
        let forged = app
            .clone()
            .oneshot(req(
                "/intake/webhook/route-key-http-1",
                body.clone(),
                Some("00".into()),
            ))
            .await
            .unwrap();
        assert_eq!(forged.status(), StatusCode::FORBIDDEN);
        let unknown = app
            .oneshot(req("/intake/webhook/no-such-route", body, None))
            .await
            .unwrap();
        assert_eq!(unknown.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn oversized_body_is_413_via_the_route_limit() {
        let (st, _t) = state(true);
        let body = vec![b'a'; 65];
        let r = router(st)
            .oneshot(req(
                "/intake/webhook/route-key-http-1",
                body.clone(),
                Some(sign(&body)),
            ))
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn hard_ceiling_rejects_before_the_handler() {
        let (st, _t) = state(true);
        let body = vec![b'a'; HARD_MAX_BODY_BYTES + 1];
        let r = router(st)
            .oneshot(req("/intake/webhook/route-key-http-1", body, None))
            .await
            .unwrap();
        assert_eq!(r.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[test]
    fn render_maps_every_outcome() {
        for (o, s) in [
            (WebhookOutcome::Accepted, 200),
            (WebhookOutcome::Duplicate, 200),
            (WebhookOutcome::NotFound, 404),
            (WebhookOutcome::TooLarge, 413),
            (WebhookOutcome::RateLimited, 429),
            (WebhookOutcome::Rejected, 403),
            (WebhookOutcome::Unavailable, 503),
        ] {
            assert_eq!(render(o).status().as_u16(), s);
        }
    }
}

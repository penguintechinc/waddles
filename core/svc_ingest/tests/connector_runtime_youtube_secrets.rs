//! The REAL `TransportSecrets` (over the real resolver and a loopback hub)
//! releases ONLY the OAuth token, ONLY for the profile's own connection, ONLY
//! toward the profile's host constant; every other host/community is refused.
//! The profile registers no handle besides `youtube.oauth`, so no non-secret
//! value (chat id, tenant, ...) can ever resolve through the credential layer.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{
    BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolver, ResolverConfig,
    ResolverMetrics,
};
use svc_ingest::connector::secrets::{Destination, TransportSecretSource, TransportSecrets};
use svc_ingest::connector::webhook::SecretError;
use svc_ingest::connector_runtime::feed::TransportPlan;
use svc_ingest::connector_runtime::plan::{ConnectorProfile, RuntimeEnv};
use svc_ingest::connector_runtime::profiles::youtube::YoutubeProfile;

const CHAT: &str = "Cg0KC-abc_123";
const TOKEN: &str = "ya29.test-token";

struct NoMetrics;
impl ResolverMetrics for NoMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
    fn list(&self, _: &'static str) {}
}

fn binding() -> BindingSummary {
    BindingSummary {
        tenant_id: 1,
        secret_version: 1,
        community_id: 7,
        connection_id: 12,
        provider: "youtube".to_owned(),
        kind: ConnectionKind::PlatformOauth,
        metadata: [("live_chat_id", CHAT)]
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
    }
}

fn dest(url: &str) -> Destination {
    Destination::from_url(&reqwest::Url::parse(url).unwrap()).unwrap()
}

#[tokio::test]
async fn real_transport_secrets_release_only_the_oauth_token_to_the_pinned_host() {
    // The mint is gRPC: a real in-process `ServiceAuthService` answers only community 7.
    let mint_endpoint = service_auth::test_support::MockHub::builder()
        .mint(vec![service_auth::test_support::MintReply::when(
            |call| call.community_id == Some(7),
            "synthetic-machine-token",
            3600,
        )])
        .start_detached()
        .await;
    let running =
        connection_resolver::test_support::start(&connection_resolver::test_support::json_hub(
            &serde_json::json!({"items": [], "nextAfter": null}),
            &serde_json::json!({
                "communityId": 7, "connectionId": 12, "provider": "youtube",
                "kind": 1, "accessToken": TOKEN, "expiresAt": null,
            }),
        ))
        .await;
    let sa = std::env::temp_dir().join(format!("yt-profile-sa-{}", uuid::Uuid::new_v4()));
    std::fs::write(&sa, "synthetic-service-account").unwrap();
    let resolver = ConnectionResolver::new(
        ResolverConfig::new(
            running.endpoint.clone(),
            "alpha",
            mint_endpoint,
            sa.to_string_lossy().to_string(),
        ),
        Arc::new(NoMetrics),
    )
    .unwrap();
    let secrets = TransportSecrets::new(Arc::new(resolver), 7)
        .unwrap()
        .with_expected_provider("youtube");

    // The profile's UNMODIFIED config (no loopback overrides).
    let env = RuntimeEnv {
        environment: "test".to_owned(),
        max_frame_bytes: 262_144,
        egress_allow: Vec::new(),
        heartbeat_enabled: false,
    };
    let mut plans = YoutubeProfile::new().plan(&binding(), &env).unwrap();
    let TransportPlan::Poll(cfg) = plans.remove(0).plan else {
        panic!("poll plan expected");
    };
    assert_eq!(
        cfg.handles.keys().map(String::as_str).collect::<Vec<_>>(),
        vec!["youtube.oauth"],
        "only the credential handle exists"
    );
    assert!(cfg.lifecycle.query_secret.is_none());
    let conn = cfg.handles["youtube.oauth"];
    let real = dest(&cfg.url);

    let ok = secrets.resolve(conn, real.clone()).await.unwrap();
    assert_eq!(ok.secret.expose(), TOKEN);
    assert!(ok.header.is_none(), "bearer row: connector header + prefix");

    let mut refused = 0;
    for bad in [
        "https://evil.example/x",
        "http://www.googleapis.com/x",
        "https://www.googleapis.com:8443/x",
        "https://127.0.0.1/x",
    ] {
        assert!(
            matches!(
                secrets.resolve(conn, dest(bad)).await,
                Err(SecretError::Missing)
            ),
            "{bad}"
        );
        refused += 1;
    }
    for other in [
        ConnectionRef {
            community_id: 8,
            connection_id: 12,
        },
        ConnectionRef {
            community_id: 7,
            connection_id: 0,
        },
    ] {
        assert!(matches!(
            secrets.resolve(other, real.clone()).await,
            Err(SecretError::Missing)
        ));
        refused += 1;
    }
    assert_eq!(refused, 6);
    let _ = std::fs::remove_file(&sa);
}

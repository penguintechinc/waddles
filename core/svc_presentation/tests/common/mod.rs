//! Shared fixtures for the caption/guard integration tests. Each
//! `tests/*.rs` file is its own binary, so unused items are expected in any
//! given file -- hence the blanket `dead_code` allowance.
#![allow(dead_code)]

use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use axum::routing::get;
use axum::Router;
use chrono::{DateTime, Utc};
use clap::Parser;
use overlay_auth::{hash_token, push_scope, PushCredential};
use overlay_schema::{CaptionPayload, OverlayPush};
use sea_orm::{DatabaseBackend, MockDatabase};
use uuid::Uuid;

use svc_presentation::config::{CliConfig, Config, Secret};
use svc_presentation::db::entities::overlay_view_credential::Model as ViewCredentialModel;
use svc_presentation::flags::{boxed, StaticFlag};
use svc_presentation::http::AppState;
use svc_presentation::overlay::caption_store::{
    CaptionStore, CaptionStoreError, NewCaptionEvent, StoredCaption,
};

pub const AUTHOR: &str = "11111111-1111-1111-1111-111111111111";

pub fn config_with(extra_args: &[&str]) -> Config {
    let mut args = vec!["svc-presentation"];
    args.extend_from_slice(extra_args);
    let cli = CliConfig::try_parse_from(args).expect("test CLI args parse");
    Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        image_bucket_access_key_id: None,
        image_bucket_secret_access_key: None,
    }
}

/// State whose mock DB answers exactly one VIEW-credential lookup (for
/// `community_id`/`token`), with the captions flag forced to `enabled`.
pub fn state_with_view_credential(community_id: i64, token: &str, enabled: bool) -> AppState {
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([vec![ViewCredentialModel {
            id: 1,
            community_id,
            key_hash: hash_token(token),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }]])
        .into_connection();
    let mut state = AppState::new(config_with(&[]), prometheus::Registry::new(), db);
    state.captions_flag = boxed(StaticFlag(enabled));
    state
}

/// State whose mock DB expects no queries at all.
pub fn state_with_no_queries(enabled: bool) -> AppState {
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    let mut state = AppState::new(config_with(&[]), prometheus::Registry::new(), db);
    state.captions_flag = boxed(StaticFlag(enabled));
    state
}

/// In-memory [`CaptionStore`] recording inserts and serving canned history.
#[derive(Default)]
pub struct FakeCaptionStore {
    pub history: Mutex<Vec<StoredCaption>>,
    pub inserted: Mutex<Vec<NewCaptionEvent>>,
    pub fail_insert: bool,
    pub fail_recent: bool,
    pub recent_calls: Mutex<Vec<(i64, DateTime<Utc>, u64)>>,
}

#[async_trait]
impl CaptionStore for FakeCaptionStore {
    async fn insert(&self, event: NewCaptionEvent) -> Result<(), CaptionStoreError> {
        if self.fail_insert {
            return Err(CaptionStoreError::Db(sea_orm::DbErr::Custom(
                "insert failed".to_string(),
            )));
        }
        self.inserted.lock().unwrap().push(event);
        Ok(())
    }

    async fn recent(
        &self,
        community_id: i64,
        since: DateTime<Utc>,
        limit: u64,
    ) -> Result<Vec<StoredCaption>, CaptionStoreError> {
        self.recent_calls
            .lock()
            .unwrap()
            .push((community_id, since, limit));
        if self.fail_recent {
            return Err(CaptionStoreError::Db(sea_orm::DbErr::Custom(
                "history query failed".to_string(),
            )));
        }
        Ok(self.history.lock().unwrap().clone())
    }

    async fn purge_older_than(&self, _cutoff: DateTime<Utc>) -> Result<u64, CaptionStoreError> {
        Ok(0)
    }
}

pub fn stored_caption(original: &str, created_at: DateTime<Utc>) -> StoredCaption {
    StoredCaption {
        user_ref: Some(Uuid::parse_str(AUTHOR).unwrap()),
        platform: "twitch".to_string(),
        original: original.to_string(),
        translated: Some(format!("{original}-translated")),
        detected_lang: Some("es".to_string()),
        target_lang: Some("en".to_string()),
        confidence: Some(0.9),
        created_at,
    }
}

/// A valid caption push as a bundle/adapter would send it.
pub fn caption_push(original: &str) -> OverlayPush {
    OverlayPush {
        caption: Some(CaptionPayload {
            user: AUTHOR.to_string(),
            display_name: "Display Name".to_string(),
            platform: "twitch".to_string(),
            original: original.to_string(),
            translated: Some("hello".to_string()),
            detected_lang: Some("es".to_string()),
            target_lang: Some("en".to_string()),
            confidence: Some(0.9),
        }),
        ..Default::default()
    }
}

/// A `PushCredential` as `overlay_auth::require_push_credential` would have
/// inserted it, for handler-level tests that bypass the guard.
pub fn push_credential(community_id: i64) -> PushCredential {
    PushCredential {
        claims: service_auth::ServiceClaims {
            iss: "hub-api".into(),
            aud: "waddlebot-internal".into(),
            sub: "spiffe://penguintech.io/alpha/svc-action".into(),
            scope: push_scope(community_id),
            iat: 0,
            nbf: 0,
            exp: u64::MAX,
            jti: "test-jti".into(),
        },
        community_id,
    }
}

// -- Real-JWT fixtures: an Ed25519 keypair (PKCS8 private DER / raw public)
// generated once with `openssl genpkey -algorithm ed25519`, identical to
// the test keypair `core/overlay_auth` and `core/service_auth` use. Never
// used outside tests.

const TEST_PRIV_DER: &[u8] = &[
    48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 68, 3, 89, 171, 105, 151, 46, 132,
    159, 252, 253, 237, 160, 158, 76, 76, 117, 168, 49, 93, 237, 107, 129, 150, 211, 28, 65, 232,
    226, 2, 111, 69,
];
const TEST_PUB_RAW: &[u8] = &[
    89, 221, 212, 205, 236, 61, 210, 204, 150, 160, 132, 29, 103, 16, 191, 115, 187, 222, 12, 175,
    169, 67, 5, 83, 51, 1, 220, 184, 65, 145, 95, 187,
];

/// Unpadded base64url, the encoding a JWKS `x` member uses.
fn base64url(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let b = [
            chunk[0],
            chunk.get(1).copied().unwrap_or(0),
            chunk.get(2).copied().unwrap_or(0),
        ];
        let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
        out.push(ALPHABET[(n >> 18) as usize & 63] as char);
        out.push(ALPHABET[(n >> 12) as usize & 63] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[(n >> 6) as usize & 63] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[n as usize & 63] as char);
        }
    }
    out
}

/// Serves `/jwks` (the test public key as `kid` `k1`) on an ephemeral
/// loopback port and returns its URL. The server task lives for the test
/// process.
pub async fn spawn_jwks_server() -> String {
    let body = serde_json::json!({
        "keys": [{ "kid": "k1", "kty": "OKP", "crv": "Ed25519", "x": base64url(TEST_PUB_RAW) }]
    });
    let app = Router::new().route(
        "/jwks",
        get(move || {
            let body = body.clone();
            async move { axum::Json(body) }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind JWKS listener");
    let addr = listener.local_addr().expect("JWKS addr");
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("JWKS server");
    });
    format!("http://{addr}/jwks")
}

/// Signs a hub-api-style PUSH machine JWT scoped to `community_id`.
pub fn sign_push_token(community_id: i64) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after epoch")
        .as_secs();
    let claims = service_auth::ServiceClaims {
        iss: "hub-api".into(),
        aud: "waddlebot-internal".into(),
        sub: "spiffe://penguintech.io/alpha/svc-action".into(),
        scope: push_scope(community_id),
        iat: now,
        nbf: now,
        exp: now + 300,
        jti: "test-jti".into(),
    };
    let mut header = jsonwebtoken::Header::new(jsonwebtoken::Algorithm::EdDSA);
    header.kid = Some("k1".to_string());
    jsonwebtoken::encode(
        &header,
        &claims,
        &jsonwebtoken::EncodingKey::from_ed_der(TEST_PRIV_DER),
    )
    .expect("sign test JWT")
}

/// State wired to a live local JWKS server, so the real
/// `overlay_auth::require_push_credential` guard verifies real signatures.
pub async fn state_with_real_push_trust(enabled: bool) -> AppState {
    let jwks_url = spawn_jwks_server().await;
    let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
    let mut state = AppState::new(
        config_with(&["--push-jwks-url", &jwks_url]),
        prometheus::Registry::new(),
        db,
    );
    state.captions_flag = boxed(StaticFlag(enabled));
    state
}

/// Like [`state_with_real_push_trust`], and additionally answers one VIEW
/// credential lookup for `community_id`/`token` -- everything a full
/// push-then-view pipeline test needs behind the real guards.
pub async fn state_with_real_push_trust_and_view(
    community_id: i64,
    token: &str,
    enabled: bool,
) -> AppState {
    let jwks_url = spawn_jwks_server().await;
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([vec![ViewCredentialModel {
            id: 1,
            community_id,
            key_hash: hash_token(token),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }]])
        .into_connection();
    let mut state = AppState::new(
        config_with(&["--push-jwks-url", &jwks_url]),
        prometheus::Registry::new(),
        db,
    );
    state.captions_flag = boxed(StaticFlag(enabled));
    state
}

/// Serves `router` on an ephemeral loopback port; returns its address.
pub async fn serve(router: Router) -> std::net::SocketAddr {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind test listener");
    let addr = listener.local_addr().expect("listener addr");
    tokio::spawn(async move {
        axum::serve(listener, router).await.expect("test server");
    });
    addr
}

pub fn arc_store(store: FakeCaptionStore) -> Arc<FakeCaptionStore> {
    Arc::new(store)
}

/// A [`DisplayNameResolver`] answering from a fixed table and recording the
/// tenant of every call -- the hub-api stand-in for the render pipeline
/// tests (no gRPC; the real `hub_client` path needs a live hub-api).
#[derive(Default)]
pub struct TableResolver {
    pub table: std::collections::HashMap<String, String>,
    pub tenants: Mutex<Vec<String>>,
    pub fail: bool,
}

impl TableResolver {
    pub fn with(pairs: &[(&str, &str)]) -> Self {
        Self {
            table: pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            ..Default::default()
        }
    }

    pub fn failing() -> Self {
        Self {
            fail: true,
            ..Default::default()
        }
    }
}

type ResolveFuture<'a> = std::pin::Pin<
    Box<
        dyn std::future::Future<
                Output = Result<
                    std::collections::HashMap<String, String>,
                    egress_detokenizer::DetokenizeError,
                >,
            > + Send
            + 'a,
    >,
>;

impl egress_detokenizer::DisplayNameResolver for TableResolver {
    fn resolve_many<'a>(&'a self, tenant_id: &'a str, tokens: Vec<String>) -> ResolveFuture<'a> {
        self.tenants.lock().unwrap().push(tenant_id.to_string());
        Box::pin(async move {
            if self.fail {
                return Err(egress_detokenizer::DetokenizeError::ResolutionUnavailable(
                    "hub-api unreachable (simulated)".to_string(),
                ));
            }
            Ok(tokens
                .into_iter()
                .filter_map(|t| self.table.get(&t).cloned().map(|n| (t, n)))
                .collect())
        })
    }
}

/// Points `state` at `resolver` (hub-api stand-in) and a community lookup
/// that answers `tenant_id` for every community.
pub fn with_overlay_fakes(
    mut state: AppState,
    resolver: Arc<TableResolver>,
    tenant_id: &str,
) -> AppState {
    state.detokenizer = Arc::new(
        svc_presentation::overlay::detok::OverlayDetokenizer::new(resolver)
            .with_metrics(state.detok_metrics.clone()),
    );
    state.community_ctx = Arc::new(
        svc_presentation::overlay::community_ctx::StaticCommunityContextStore::ok(tenant_id),
    );
    state
}

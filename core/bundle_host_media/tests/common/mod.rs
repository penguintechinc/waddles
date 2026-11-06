//! Shared fakes: a Gemini-shaped HTTP server, tier/credential stubs, a manual clock.
#![allow(dead_code, clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use base64::Engine;
use bundle_host_media::credentials::{ApiKey, CredentialError, CredentialResolver};
use bundle_host_media::gemini::{GeminiModels, GeminiProvider};
use bundle_host_media::host::{Clock, MediaConfig, MediaHost};
use bundle_host_media::jobs::{InMemoryJobStore, JobStore};
use bundle_host_media::store::{InMemoryStore, MediaStore};
use bundle_host_media::tier::{TierError, TierResolver};
use bundle_host_media::{Scope, Tier};

pub const GOOD_KEY: &str = "k-good-123";

pub fn png() -> Vec<u8> {
    let mut v = vec![0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
    v.extend_from_slice(b"fake-png-body");
    v
}
pub fn mp4() -> Vec<u8> {
    let mut v = vec![0, 0, 0, 0x20];
    v.extend_from_slice(b"ftypisom");
    v.extend_from_slice(b"fake-mp4-body");
    v
}
pub fn mp3() -> Vec<u8> {
    let mut v = b"ID3".to_vec();
    v.extend_from_slice(b"fake-mp3-body");
    v
}

#[derive(Default)]
pub struct FakeState {
    /// Every POST body + (key header, revision header).
    pub posts: Mutex<Vec<(serde_json::Value, String, String)>>,
    pub gets: Mutex<Vec<(String, String)>>,
    /// polls remaining as pending per interaction id.
    pub pending_polls: AtomicU64,
    pub next_id: AtomicU64,
    pub kinds: Mutex<HashMap<String, String>>,
    pub image_delay_ms: AtomicU64,
    pub bogus_image_bytes: Mutex<bool>,
}

fn block(kind: &str, bytes: &[u8], mime: &str) -> serde_json::Value {
    serde_json::json!({"type": kind, "mime_type": mime, "data": base64::engine::general_purpose::STANDARD.encode(bytes)})
}

fn completed(kind: &str, bytes: &[u8], mime: &str, id: &str) -> serde_json::Value {
    serde_json::json!({"id": id, "status": "completed", "steps": [
        {"type": "user_input", "content": [{"type": "text", "text": "x"}]},
        {"type": "model_output", "content": [block(kind, bytes, mime)]}
    ]})
}

async fn create(
    State(st): State<Arc<FakeState>>,
    headers: HeaderMap,
    Json(body): Json<serde_json::Value>,
) -> (StatusCode, Json<serde_json::Value>) {
    let key = headers
        .get("x-goog-api-key")
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default();
    let rev = headers
        .get("api-revision")
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default();
    st.posts
        .lock()
        .unwrap()
        .push((body.clone(), key.clone(), rev));
    if key != GOOD_KEY {
        return (
            StatusCode::FORBIDDEN,
            Json(serde_json::json!({"error": {"status": "PERMISSION_DENIED"}})),
        );
    }
    let prompt = body["input"]
        .as_str()
        .map(str::to_string)
        .or_else(|| body["input"][0]["text"].as_str().map(str::to_string))
        .unwrap_or_default();
    if prompt.contains("BLOCKME") {
        return (
            StatusCode::BAD_REQUEST,
            Json(
                serde_json::json!({"error": {"status": "INVALID_ARGUMENT", "message": "Prompt blocked by safety settings"}}),
            ),
        );
    }
    if prompt.contains("QUOTA") {
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Json(serde_json::json!({"error": {"status": "RESOURCE_EXHAUSTED"}})),
        );
    }
    if prompt.contains("SILENT") {
        return (
            StatusCode::OK,
            Json(
                serde_json::json!({"id": "x", "status": "completed", "steps": [{"type":"model_output","content":[{"type":"text","text":"no"}]}]}),
            ),
        );
    }
    let model = body["model"].as_str().unwrap_or_default().to_string();
    if body.get("background") == Some(&serde_json::Value::Bool(true)) {
        let id = format!("op-{}", st.next_id.fetch_add(1, Ordering::SeqCst));
        let kind = if model.contains("lyria") {
            "audio"
        } else {
            "video"
        };
        st.kinds
            .lock()
            .unwrap()
            .insert(id.clone(), kind.to_string());
        return (
            StatusCode::OK,
            Json(serde_json::json!({"id": id, "status": "in_progress"})),
        );
    }
    let delay = st.image_delay_ms.load(Ordering::SeqCst);
    if delay > 0 {
        tokio::time::sleep(Duration::from_millis(delay)).await;
    }
    let bytes = if *st.bogus_image_bytes.lock().unwrap() {
        b"<html>not an image</html>".to_vec()
    } else {
        png()
    };
    (
        StatusCode::OK,
        Json(completed("image", &bytes, "image/png", "img-1")),
    )
}

async fn poll(
    State(st): State<Arc<FakeState>>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> (StatusCode, Json<serde_json::Value>) {
    let key = headers
        .get("x-goog-api-key")
        .map(|v| v.to_str().unwrap().to_string())
        .unwrap_or_default();
    st.gets.lock().unwrap().push((id.clone(), key));
    let remaining = st.pending_polls.load(Ordering::SeqCst);
    if remaining > 0 {
        st.pending_polls.fetch_sub(1, Ordering::SeqCst);
        return (
            StatusCode::OK,
            Json(serde_json::json!({"id": id, "status": "in_progress"})),
        );
    }
    let kind = st.kinds.lock().unwrap().get(&id).cloned();
    match kind.as_deref() {
        Some("audio") => (
            StatusCode::OK,
            Json(completed("audio", &mp3(), "audio/mpeg", &id)),
        ),
        Some("video") => (
            StatusCode::OK,
            Json(completed("video", &mp4(), "video/mp4", &id)),
        ),
        _ => (StatusCode::NOT_FOUND, Json(serde_json::json!({}))),
    }
}

/// Starts the fake on 127.0.0.1:0.
pub async fn fake_gemini() -> (Arc<FakeState>, String) {
    let st = Arc::new(FakeState::default());
    let app = Router::new()
        .route("/v1beta/interactions", post(create))
        .route("/v1beta/interactions/{id}", get(poll))
        .with_state(st.clone());
    let l = tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
        .await
        .unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    (st, format!("http://{addr}"))
}

pub fn provider(base: String, timeout: Duration) -> GeminiProvider {
    GeminiProvider::for_test_endpoint(
        base,
        vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        GeminiModels::default(),
        timeout,
    )
}

pub struct FixedTier(pub Result<Tier, TierError>);
#[async_trait]
impl TierResolver for FixedTier {
    async fn effective_tier(&self, _s: &Scope) -> Result<Tier, TierError> {
        match &self.0 {
            Ok(t) => Ok(*t),
            Err(TierError::Unknown) => Err(TierError::Unknown),
            Err(TierError::Unavailable(m)) => Err(TierError::Unavailable(m.clone())),
        }
    }
}

pub struct FixedCreds(pub Option<&'static str>);
#[async_trait]
impl CredentialResolver for FixedCreds {
    async fn resolve(&self, _s: &Scope, provider: &str) -> Result<ApiKey, CredentialError> {
        assert_eq!(provider, "gemini");
        match self.0 {
            Some(k) => Ok(ApiKey::new(k.to_string())),
            None => Err(CredentialError::NotConfigured),
        }
    }
}

pub struct ManualClock(pub AtomicU64);
impl Clock for ManualClock {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}

pub fn scope(app: &str) -> Scope {
    Scope {
        tenant_id: 7,
        community_id: 42,
        app_id: app.into(),
    }
}

pub fn fast_cfg() -> MediaConfig {
    MediaConfig {
        poll_interval: Duration::from_millis(10),
        ..MediaConfig::default()
    }
}

pub struct Rig {
    pub host: Arc<MediaHost>,
    pub fake: Arc<FakeState>,
    pub store: Arc<InMemoryStore>,
    pub jobs: Arc<InMemoryJobStore>,
    pub clock: Arc<ManualClock>,
}

pub async fn rig(
    tier: Result<Tier, TierError>,
    key: Option<&'static str>,
    cfg: MediaConfig,
) -> Rig {
    let (fake, base) = fake_gemini().await;
    let store = Arc::new(InMemoryStore::default());
    let jobs = Arc::new(InMemoryJobStore::default());
    let clock = Arc::new(ManualClock(AtomicU64::new(1_000_000)));
    let timeout = cfg.image_timeout;
    let host = Arc::new(MediaHost::new(
        Arc::new(FixedTier(tier)),
        Arc::new(FixedCreds(key)),
        Arc::new(provider(base, timeout.max(Duration::from_secs(5)))),
        store.clone() as Arc<dyn MediaStore>,
        jobs.clone() as Arc<dyn JobStore>,
        clock.clone(),
        cfg,
    ));
    Rig {
        host,
        fake,
        store,
        jobs,
        clock,
    }
}

/// Polls `job_status` until it leaves `Pending` (bounded).
pub async fn wait_done(host: &MediaHost, scope: &Scope, id: &str) -> bundle_host_media::JobState {
    for _ in 0..400 {
        let s = host.job_status(scope, id).await.unwrap();
        if s != bundle_host_media::JobState::Pending {
            return s;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("job never finished");
}

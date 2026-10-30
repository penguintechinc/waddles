//! Generic, platform-agnostic polling [`Transport`]. URL, auth header, cursor
//! query parameter / response pointer and interval all come from
//! [`PollConfig`] (the overlay's config). One poll = one frame (the response
//! body). The cursor only advances when the driver reports the frame
//! `Published` via [`Transport::on_frame_outcome`]; a dropped frame holds the
//! cursor so the same window is re-fetched. The auth secret is resolved per
//! request, never cached, and only ever placed in a sensitive header.

use std::collections::HashMap;
use std::time::Duration;

use connection_resolver::ConnectionRef;
use reqwest::header::{HeaderName, HeaderValue};
use tokio::time::Instant;
use zeroize::Zeroizing;

use super::transport::{FrameOutcome, Transport, TransportError};
use super::transport_common::{opaque_id, resolve_handle, validate_endpoint};
use super::types::{ConnectionCtx, Handshake};
use super::webhook::WebhookSecretSource;

/// Header carrying a per-call resolved secret.
#[derive(Debug, Clone)]
pub struct PollAuth {
    /// Header name, e.g. `Authorization`.
    pub header: String,
    /// Literal prefix before the secret, e.g. `Bearer `.
    pub prefix: String,
    /// Handle into [`PollConfig::handles`].
    pub handle: String,
}

/// Overlay-supplied configuration for one polled source.
#[derive(Debug, Clone)]
pub struct PollConfig {
    /// Platform slug (must have a pinned connector).
    pub platform: String,
    /// `https://` endpoint from the overlay config.
    pub url: String,
    /// Host-side key hashed into the opaque `source_id` (never exposed).
    pub source_key: String,
    /// Handle -> connection map for [`PollAuth`].
    pub handles: HashMap<String, ConnectionRef>,
    /// Optional auth header.
    pub auth: Option<PollAuth>,
    /// Query parameter that carries the cursor (`None` = stateless poll).
    pub cursor_param: Option<String>,
    /// RFC 6901 JSON pointer into the response yielding the NEXT cursor.
    pub cursor_pointer: Option<String>,
    /// Cursor to resume from (restored by the host after a restart).
    pub initial_cursor: Option<String>,
    /// Minimum gap between polls.
    pub interval: Duration,
    /// Per-request timeout.
    pub request_timeout: Duration,
    /// Max response body accepted.
    pub max_body_bytes: usize,
    /// Permit `http://` to a loopback host (tests only).
    pub allow_loopback_insecure: bool,
}

/// Polling transport over reqwest (rustls, redirects disabled).
pub struct PollTransport<S: WebhookSecretSource> {
    cfg: PollConfig,
    url: reqwest::Url,
    client: reqwest::Client,
    source_id: String,
    connection_id: String,
    secrets: S,
    committed: Option<String>,
    pending: Option<String>,
    last_poll: Option<Instant>,
}

impl<S: WebhookSecretSource> PollTransport<S> {
    /// Validates the config and builds the HTTP client.
    pub fn new(cfg: PollConfig, secrets: S) -> Result<Self, TransportError> {
        let url = validate_endpoint(&cfg.url, &["https"], "http", cfg.allow_loopback_insecure)
            .map_err(TransportError::Connect)?;
        if cfg.cursor_pointer.is_some() != cfg.cursor_param.is_some() {
            return Err(TransportError::Connect(
                "cursor_param and cursor_pointer must be set together".into(),
            ));
        }
        if let Some(a) = &cfg.auth {
            HeaderName::from_bytes(a.header.as_bytes())
                .map_err(|_| TransportError::Connect("invalid auth header name".into()))?;
            if !cfg.handles.contains_key(&a.handle) {
                return Err(TransportError::Connect("auth handle not configured".into()));
            }
        }
        let client = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(cfg.request_timeout)
            .build()
            .map_err(|e| TransportError::Connect(e.to_string()))?;
        Ok(Self {
            source_id: opaque_id("poll", &cfg.source_key),
            connection_id: uuid::Uuid::new_v4().to_string(),
            committed: cfg.initial_cursor.clone(),
            cfg,
            url,
            client,
            secrets,
            pending: None,
            last_poll: None,
        })
    }

    /// The last cursor the driver confirmed published (for host persistence).
    #[must_use]
    pub fn cursor(&self) -> Option<&str> {
        self.committed.as_deref()
    }

    /// One request; `Ok(None)` = nothing new.
    async fn fetch(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        let mut url = self.url.clone();
        if let (Some(p), Some(c)) = (&self.cfg.cursor_param, &self.committed) {
            url.query_pairs_mut().append_pair(p, c);
        }
        let mut req = self.client.get(url);
        if let Some(a) = &self.cfg.auth {
            let secret = resolve_handle(&self.secrets, &self.cfg.handles, &a.handle).await?;
            let joined = Zeroizing::new(format!("{}{}", a.prefix, secret.expose()));
            let mut v = HeaderValue::from_str(&joined)
                .map_err(|_| TransportError::Fatal("secret not usable as header".into()))?;
            v.set_sensitive(true);
            req = req.header(
                HeaderName::from_bytes(a.header.as_bytes())
                    .map_err(|_| TransportError::Fatal("invalid auth header name".into()))?,
                v,
            );
        }
        // reqwest errors can embed the URL; the URL holds no secret (auth is
        // header-only) but the cursor is opaque host state, so report kind only.
        let mut resp = req.send().await.map_err(|e| {
            TransportError::Read(
                if e.is_timeout() {
                    "timeout"
                } else {
                    "request failed"
                }
                .into(),
            )
        })?;
        let status = resp.status();
        if matches!(status.as_u16(), 401 | 403) {
            return Err(TransportError::Fatal(format!(
                "upstream rejected credentials ({status})"
            )));
        }
        if matches!(status.as_u16(), 204 | 304) {
            return Ok(None);
        }
        if !status.is_success() {
            return Err(TransportError::Read(format!("http {status}")));
        }
        if resp
            .content_length()
            .is_some_and(|n| n > self.cfg.max_body_bytes as u64)
        {
            return Err(TransportError::Read("response over size ceiling".into()));
        }
        let mut body = Vec::new();
        while let Some(chunk) = resp
            .chunk()
            .await
            .map_err(|_| TransportError::Read("body read failed".into()))?
        {
            if body.len() + chunk.len() > self.cfg.max_body_bytes {
                return Err(TransportError::Read("response over size ceiling".into()));
            }
            body.extend_from_slice(&chunk);
        }
        if body.is_empty() {
            return Ok(None);
        }
        self.pending = self.extract_cursor(&body)?;
        Ok(Some(body))
    }

    /// Reads the next cursor from the body per the configured pointer.
    fn extract_cursor(&self, body: &[u8]) -> Result<Option<String>, TransportError> {
        let Some(ptr) = &self.cfg.cursor_pointer else {
            return Ok(None);
        };
        let v: serde_json::Value = serde_json::from_slice(body)
            .map_err(|_| TransportError::Read("response is not JSON".into()))?;
        Ok(match v.pointer(ptr) {
            Some(serde_json::Value::String(s)) if !s.is_empty() => Some(s.clone()),
            Some(serde_json::Value::Number(n)) => Some(n.to_string()),
            _ => None,
        })
    }
}

impl<S: WebhookSecretSource + 'static> Transport for PollTransport<S> {
    fn platform(&self) -> &str {
        &self.cfg.platform
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        // Same connection_id across reconnects: the committed cursor (kept on
        // self) is the resume state, so a reconnect resumes, never restarts.
        Ok(ConnectionCtx {
            platform: self.cfg.platform.clone(),
            source_id: self.source_id.clone(),
            connection_id: self.connection_id.clone(),
            session_token: None,
        })
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        if handshake.bytes.is_empty() && handshake.secret_refs.is_empty() {
            Ok(())
        } else {
            Err(TransportError::Handshake(
                "poll transport takes no handshake".into(),
            ))
        }
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        loop {
            if let Some(t) = self.last_poll {
                tokio::time::sleep_until(t + self.cfg.interval).await;
            }
            self.pending = None;
            let res = self.fetch().await;
            self.last_poll = Some(Instant::now());
            if let Some(body) = res? {
                return Ok(Some(body));
            }
        }
    }

    async fn close(&mut self, _reason: &str) {
        self.pending = None;
    }

    async fn on_frame_outcome(&mut self, outcome: FrameOutcome) {
        let pending = self.pending.take();
        if outcome == FrameOutcome::Published {
            if let Some(c) = pending {
                self.committed = Some(c);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::webhook::{BoxFut, SecretError};
    use axum::extract::{Query, State};
    use axum::http::{HeaderMap, StatusCode};
    use axum::routing::get;
    use axum::Router;
    use connection_resolver::SecretString;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    const REF: ConnectionRef = ConnectionRef {
        community_id: 1,
        connection_id: 2,
    };

    #[derive(Clone)]
    struct CountingSecrets(Arc<AtomicUsize>);
    impl WebhookSecretSource for CountingSecrets {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Box::pin(async { Ok(SecretString::new("tok123".into())) })
        }
    }

    type Seen = (Option<String>, Option<String>);

    /// Records (cursor, auth header) per request; replies from `script`.
    #[derive(Clone, Default)]
    struct Srv {
        seen: Arc<Mutex<Vec<Seen>>>,
        status: Arc<Mutex<u16>>,
    }

    async fn handler(
        State(s): State<Srv>,
        Query(q): Query<HashMap<String, String>>,
        h: HeaderMap,
    ) -> (StatusCode, String) {
        let cur = q.get("after").cloned();
        let auth = h
            .get("authorization")
            .map(|v| v.to_str().unwrap().to_string());
        s.seen.lock().unwrap().push((cur.clone(), auth));
        let code = *s.status.lock().unwrap();
        if code != 200 {
            return (StatusCode::from_u16(code).unwrap(), String::new());
        }
        let body = match cur.as_deref() {
            None => r#"{"items":[1],"next":"c1"}"#,
            Some("c1") => r#"{"items":[2],"next":"c2"}"#,
            _ => r#"{"items":[3],"next":"c3"}"#,
        };
        (StatusCode::OK, body.into())
    }

    async fn start() -> (std::net::SocketAddr, Srv) {
        let srv = Srv {
            status: Arc::new(Mutex::new(200)),
            ..Default::default()
        };
        let app = Router::new()
            .route("/poll", get(handler))
            .with_state(srv.clone());
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
        (addr, srv)
    }

    fn cfg(addr: std::net::SocketAddr) -> PollConfig {
        PollConfig {
            platform: "testplat".into(),
            url: format!("http://{addr}/poll"),
            source_key: "room-name".into(),
            handles: HashMap::from([("h1".to_string(), REF)]),
            auth: Some(PollAuth {
                header: "Authorization".into(),
                prefix: "Bearer ".into(),
                handle: "h1".into(),
            }),
            cursor_param: Some("after".into()),
            cursor_pointer: Some("/next".into()),
            initial_cursor: None,
            interval: Duration::from_millis(5),
            request_timeout: Duration::from_secs(2),
            max_body_bytes: 1024,
            allow_loopback_insecure: true,
        }
    }

    fn mk(addr: std::net::SocketAddr) -> (PollTransport<CountingSecrets>, Arc<AtomicUsize>) {
        let n = Arc::new(AtomicUsize::new(0));
        (
            PollTransport::new(cfg(addr), CountingSecrets(n.clone())).unwrap(),
            n,
        )
    }

    #[tokio::test]
    async fn cursor_advances_only_on_published_and_secret_per_poll() {
        let (addr, srv) = start().await;
        let (mut t, n) = mk(addr);
        let ctx = t.connect().await.unwrap();
        assert!(ctx.source_id.starts_with("poll-") && !ctx.source_id.contains("room"));
        t.next_frame().await.unwrap().unwrap();
        t.on_frame_outcome(FrameOutcome::Published).await;
        assert_eq!(t.cursor(), Some("c1"));
        t.next_frame().await.unwrap().unwrap();
        t.on_frame_outcome(FrameOutcome::Published).await;
        assert_eq!(t.cursor(), Some("c2"));
        let seen = srv.seen.lock().unwrap().clone();
        assert_eq!(seen[0].0, None);
        assert_eq!(seen[1].0.as_deref(), Some("c1"));
        assert_eq!(seen[1].1.as_deref(), Some("Bearer tok123"));
        assert_eq!(
            n.load(Ordering::SeqCst),
            2,
            "one resolve per poll, no cache"
        );
    }

    #[tokio::test]
    async fn dropped_frame_holds_cursor_and_refetches() {
        let (addr, srv) = start().await;
        let (mut t, _) = mk(addr);
        t.connect().await.unwrap();
        let a = t.next_frame().await.unwrap().unwrap();
        t.on_frame_outcome(FrameOutcome::Dropped(
            crate::connector::FrameDropReason::GuestError,
        ))
        .await;
        assert_eq!(t.cursor(), None);
        let b = t.next_frame().await.unwrap().unwrap();
        assert_eq!(a, b);
        let seen = srv.seen.lock().unwrap().clone();
        assert_eq!(seen[0].0, seen[1].0);
    }

    #[tokio::test]
    async fn cursor_survives_reconnect_and_initial_cursor_resumes() {
        let (addr, srv) = start().await;
        let (mut t, _) = mk(addr);
        let c1 = t.connect().await.unwrap();
        t.next_frame().await.unwrap().unwrap();
        t.on_frame_outcome(FrameOutcome::Published).await;
        t.close("test").await;
        let c2 = t.connect().await.unwrap();
        assert_eq!(c1.connection_id, c2.connection_id);
        t.next_frame().await.unwrap().unwrap();
        assert_eq!(srv.seen.lock().unwrap()[1].0.as_deref(), Some("c1"));
        // Restart: host restores the persisted cursor.
        let mut c = cfg(addr);
        c.initial_cursor = Some("c2".into());
        let mut t2 = PollTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        t2.next_frame().await.unwrap().unwrap();
        assert_eq!(srv.seen.lock().unwrap()[2].0.as_deref(), Some("c2"));
    }

    #[tokio::test]
    async fn auth_rejection_is_fatal_and_server_errors_retry() {
        let (addr, srv) = start().await;
        let (mut t, _) = mk(addr);
        *srv.status.lock().unwrap() = 401;
        assert!(t.next_frame().await.unwrap_err().is_fatal());
        *srv.status.lock().unwrap() = 503;
        assert!(matches!(t.next_frame().await, Err(TransportError::Read(_))));
    }

    #[tokio::test]
    async fn empty_responses_are_skipped_not_yielded() {
        let (addr, srv) = start().await;
        let (mut t, _) = mk(addr);
        *srv.status.lock().unwrap() = 204;
        let st = srv.status.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(40)).await;
            *st.lock().unwrap() = 200;
        });
        assert!(t.next_frame().await.unwrap().is_some());
        assert!(srv.seen.lock().unwrap().len() >= 2);
    }

    #[tokio::test]
    async fn oversize_body_rejected_and_config_validated() {
        let (addr, _srv) = start().await;
        let mut c = cfg(addr);
        c.max_body_bytes = 4;
        let mut t = PollTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        assert!(matches!(t.next_frame().await, Err(TransportError::Read(_))));
        let s = || CountingSecrets(Arc::new(AtomicUsize::new(0)));
        let mut c = cfg(addr);
        c.cursor_pointer = None;
        assert!(PollTransport::new(c, s()).is_err());
        let mut c = cfg(addr);
        c.allow_loopback_insecure = false;
        assert!(PollTransport::new(c, s()).is_err());
        let mut c = cfg(addr);
        c.auth.as_mut().unwrap().handle = "nope".into();
        assert!(PollTransport::new(c, s()).is_err());
    }
}

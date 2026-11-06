//! Generic, platform-agnostic polling [`Transport`]. URL, auth header, cursor
//! query parameter / response pointer and interval all come from
//! [`PollConfig`] (the connector's config). One poll = one frame (the response
//! body). The cursor only advances when the driver reports the frame
//! `Published` via [`Transport::on_frame_outcome`]; a dropped frame holds the
//! cursor so the same window is re-fetched, up to
//! [`PollConfig::max_drop_attempts`] times, after which the window is
//! POISON-skipped (loud error log, cursor advanced past it) so one bad frame
//! cannot wedge the poller.
//!
//! No-advance rule (also covers stateless polls): when a response carries no
//! (valid, <= [`MAX_CURSOR_BYTES`]) next cursor, the window is yielded once;
//! a later response with the identical body at the unchanged cursor is
//! treated as "nothing new" and skipped, so the same window is never
//! re-published every interval.
//!
//! SSRF: each request resolves + validates the host ([`resolve_pinned`]) and
//! pins the connection to that address on a fresh client (no DNS rebind,
//! redirects off). The auth secret is resolved per request, never cached, and
//! only ever placed in a sensitive header. Residual (library-forced):
//! `HeaderValue::from_str` copies the value into a non-zeroizing buffer
//! owned by `http`/hyper; our own copies are zeroized.

use std::collections::HashMap;
use std::net::IpAddr;
use std::time::Duration;

use connection_resolver::ConnectionRef;
use reqwest::header::{HeaderName, HeaderValue};
use sha2::{Digest, Sha256};
use tokio::time::Instant;
use zeroize::Zeroizing;

use super::egress_guard::resolve_pinned;
use super::lifecycle::{backoff_wait, HttpAction, PollLifecycle};
use super::secrets::{Destination, TransportSecretSource};
use super::transport::{FrameOutcome, Transport, TransportError};
use super::transport_common::{opaque_id, resolve_handle, validate_endpoint, FrameRuleMetrics};
use super::types::{ConnectionCtx, Handshake};

/// Longest cursor accepted from a response (longer is treated as absent).
pub const MAX_CURSOR_BYTES: usize = 512;

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

/// Largest JSON request body a host profile may configure.
pub const MAX_REQUEST_BODY_BYTES: usize = 16 * 1024;

/// Header names a profile may never set: the transport owns them.
const RESERVED_HEADERS: [&str; 6] = [
    "host",
    "content-length",
    "content-type",
    "transfer-encoding",
    "connection",
    "accept-encoding",
];

/// Host-owned shape of the poll request beyond the default `GET`. Every field
/// is a profile constant (never guest- or metadata-controlled): the JSON body
/// and the credential-bearing headers. `Default` = plain `GET`, one `auth`.
#[derive(Debug, Clone, Default)]
pub struct PollRequest {
    /// `Some(json)` switches the poll to `POST` with
    /// `Content-Type: application/json`; must be valid JSON,
    /// <= [`MAX_REQUEST_BODY_BYTES`]. `None` = `GET` (unchanged behaviour).
    pub json_body: Option<Vec<u8>>,
    /// Further credential headers, each resolved per request from its own
    /// anchored handle (`PollConfig::handles`) and placed as a sensitive
    /// header with the configured literal prefix (e.g. `Cookie: Rsi-Token=`).
    pub extra_auth: Vec<PollAuth>,
}

impl PollRequest {
    /// Validates the request shape against the transport's handle set.
    fn validate(&self, handles: &HashMap<String, ConnectionRef>) -> Result<(), TransportError> {
        if let Some(b) = &self.json_body {
            if b.len() > MAX_REQUEST_BODY_BYTES {
                return Err(TransportError::Connect("request body too large".into()));
            }
            serde_json::from_slice::<serde_json::Value>(b)
                .map_err(|_| TransportError::Connect("request body is not JSON".into()))?;
        }
        for a in &self.extra_auth {
            let name = HeaderName::from_bytes(a.header.as_bytes())
                .map_err(|_| TransportError::Connect("invalid auth header name".into()))?;
            if RESERVED_HEADERS.contains(&name.as_str()) {
                return Err(TransportError::Connect("reserved auth header name".into()));
            }
            if !handles.contains_key(&a.handle) {
                return Err(TransportError::Connect("auth handle not configured".into()));
            }
        }
        Ok(())
    }
}

/// Connector-supplied configuration for one polled source.
#[derive(Debug, Clone)]
pub struct PollConfig {
    /// Platform slug (must have a pinned connector).
    pub platform: String,
    /// `https://` endpoint from the connector config.
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
    /// Minimum gap between polls (1 ms ..= 24 h).
    pub interval: Duration,
    /// Per-request timeout.
    pub request_timeout: Duration,
    /// Max response body accepted.
    pub max_body_bytes: usize,
    /// Consecutive `Dropped` outcomes for one window before it is
    /// poison-skipped (1 ..= 100).
    pub max_drop_attempts: u32,
    /// Exact IPs the host explicitly grants despite the SSRF denylist.
    pub egress_allow: Vec<IpAddr>,
    /// Permit `http://` to a loopback host (tests only; the address must
    /// still be in `egress_allow`).
    pub allow_loopback_insecure: bool,
    /// Host-owned poll lifecycle (interval hint, HTTP error classification,
    /// API-key query slot); `PollLifecycle::default()` = none.
    pub lifecycle: PollLifecycle,
    /// Host-owned request shape (method/body/extra credential headers).
    pub request: PollRequest,
}

/// Polling transport over reqwest (rustls, redirects disabled).
pub struct PollTransport<S: TransportSecretSource> {
    cfg: PollConfig,
    url: reqwest::Url,
    source_id: String,
    connection_id: String,
    secrets: S,
    committed: Option<String>,
    /// Next cursor of the frame most recently yielded.
    pending: Option<String>,
    /// Digest of the frame most recently yielded.
    pending_digest: Option<[u8; 32]>,
    /// Digest of the last Published frame that did not advance the cursor.
    last_static_digest: Option<[u8; 32]>,
    drop_streak: u32,
    last_poll: Option<Instant>,
    /// Gap before the next poll; the configured interval unless the last
    /// response carried an in-bounds hint.
    interval_now: Duration,
    /// Polling is paused until then (classified quota/rate back-off).
    backoff_until: Option<Instant>,
    /// Consecutive classified back-offs (drives the exponential wait).
    backoff_streak: u32,
    metrics: Option<FrameRuleMetrics>,
}

impl<S: TransportSecretSource> PollTransport<S> {
    /// Validates the config.
    pub fn new(cfg: PollConfig, secrets: S) -> Result<Self, TransportError> {
        let url = validate_endpoint(&cfg.url, &["https"], "http", cfg.allow_loopback_insecure)
            .map_err(TransportError::Connect)?;
        if cfg.cursor_pointer.is_some() != cfg.cursor_param.is_some() {
            return Err(TransportError::Connect(
                "cursor_param and cursor_pointer must be set together".into(),
            ));
        }
        if cfg.interval < Duration::from_millis(1)
            || cfg.interval > Duration::from_secs(86_400)
            || cfg.max_drop_attempts == 0
            || cfg.max_drop_attempts > 100
            || cfg
                .initial_cursor
                .as_ref()
                .is_some_and(|c| c.len() > MAX_CURSOR_BYTES)
        {
            return Err(TransportError::Connect("invalid poll limits".into()));
        }
        cfg.lifecycle
            .validate(&cfg.handles, cfg.cursor_param.as_deref(), &url)
            .map_err(|e| TransportError::Connect(e.to_string()))?;
        cfg.request.validate(&cfg.handles)?;
        if let Some(a) = &cfg.auth {
            HeaderName::from_bytes(a.header.as_bytes())
                .map_err(|_| TransportError::Connect("invalid auth header name".into()))?;
            if !cfg.handles.contains_key(&a.handle) {
                return Err(TransportError::Connect("auth handle not configured".into()));
            }
        }
        Ok(Self {
            source_id: opaque_id("poll", &cfg.source_key),
            connection_id: uuid::Uuid::new_v4().to_string(),
            committed: cfg.initial_cursor.clone(),
            url,
            secrets,
            pending: None,
            pending_digest: None,
            last_static_digest: None,
            drop_streak: 0,
            last_poll: None,
            interval_now: cfg.interval,
            backoff_until: None,
            backoff_streak: 0,
            metrics: None,
            cfg,
        })
    }

    /// Attaches the lifecycle counters (registered once per registry via
    /// [`FrameRuleMetrics::register`]).
    #[must_use]
    pub fn with_metrics(mut self, metrics: FrameRuleMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    fn count(&self, rule: &str, outcome: &str) {
        if let Some(m) = &self.metrics {
            m.add(&self.cfg.platform, rule, outcome, 1);
        }
    }

    /// The gap before the next poll: the configured interval unless the last
    /// response carried an in-bounds hint.
    #[must_use]
    pub fn interval(&self) -> Duration {
        self.interval_now
    }

    /// The last cursor the driver confirmed published (for host persistence).
    #[must_use]
    pub fn cursor(&self) -> Option<&str> {
        self.committed.as_deref()
    }

    /// Builds a one-shot client pinned to the validated address.
    async fn pinned_client(&self) -> Result<reqwest::Client, TransportError> {
        let addr = resolve_pinned(&self.url, &self.cfg.egress_allow)
            .await
            .map_err(TransportError::Connect)?;
        let mut b = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            // Ambient HTTP(S)_PROXY would resolve/connect the host itself and
            // bypass the pinned address.
            .no_proxy()
            .timeout(self.cfg.request_timeout);
        if let Some(host) = self.url.host_str() {
            b = b.resolve(host, addr);
        }
        b.build()
            .map_err(|e| TransportError::Connect(e.to_string()))
    }

    /// One request; `Ok(None)` = nothing new.
    async fn fetch(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        let client = self.pinned_client().await?;
        let mut url = self.url.clone();
        if let (Some(p), Some(c)) = (&self.cfg.cursor_param, &self.committed) {
            url.query_pairs_mut().append_pair(p, c);
        }
        if let Some(q) = &self.cfg.lifecycle.query_secret {
            let dest = Destination::from_url(&self.url)
                .ok_or_else(|| TransportError::Fatal("secret unavailable for handle".into()))?;
            let secret = resolve_handle(&self.secrets, &self.cfg.handles, &q.handle, &dest)
                .await?
                .secret;
            if secret.expose().is_empty() {
                return Err(TransportError::Fatal(
                    "secret unavailable for handle".into(),
                ));
            }
            // `append_pair` percent-encodes everything outside the
            // form-urlencoded unreserved set, so the value can never end the
            // parameter or inject another. Residual (library-forced): the
            // `Url` owns a non-zeroizing copy until it drops after the send.
            url.query_pairs_mut().append_pair(&q.param, secret.expose());
        }
        let mut req = match &self.cfg.request.json_body {
            Some(body) => client
                .post(url)
                .header(reqwest::header::CONTENT_TYPE, "application/json")
                .body(body.clone()),
            None => client.get(url),
        };
        if let Some(a) = &self.cfg.auth {
            let dest = Destination::from_url(&self.url)
                .ok_or_else(|| TransportError::Fatal("secret unavailable for handle".into()))?;
            let auth = resolve_handle(&self.secrets, &self.cfg.handles, &a.handle, &dest).await?;
            // API-key rows use the STORED header name and a raw value; only
            // bearer/OAuth rows use the connector's header + prefix.
            let (hname, prefix) = match &auth.header {
                Some(stored) => (stored.as_str(), ""),
                None => (a.header.as_str(), a.prefix.as_str()),
            };
            let joined = Zeroizing::new(format!("{prefix}{}", auth.secret.expose()));
            let mut v = HeaderValue::from_str(&joined)
                .map_err(|_| TransportError::Fatal("secret not usable as header".into()))?;
            v.set_sensitive(true);
            let name = HeaderName::from_bytes(hname.as_bytes())
                .map_err(|_| TransportError::Fatal("invalid auth header name".into()))?;
            req = req.header(name, v);
        }
        for a in &self.cfg.request.extra_auth {
            let dest = Destination::from_url(&self.url)
                .ok_or_else(|| TransportError::Fatal("secret unavailable for handle".into()))?;
            let auth = resolve_handle(&self.secrets, &self.cfg.handles, &a.handle, &dest).await?;
            let joined = Zeroizing::new(format!("{}{}", a.prefix, auth.secret.expose()));
            let mut v = HeaderValue::from_str(&joined)
                .map_err(|_| TransportError::Fatal("secret not usable as header".into()))?;
            v.set_sensitive(true);
            let name = HeaderName::from_bytes(a.header.as_bytes())
                .map_err(|_| TransportError::Fatal("invalid auth header name".into()))?;
            req = req.header(name, v);
        }
        // reqwest errors can embed the URL; report the kind only.
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
        let code = status.as_u16();
        // Host-classified error responses (e.g. quota 403 -> back off, not
        // fatal). The body is only read when a rule targets this status. The
        // URL (which may carry the query secret) is never part of any error.
        if self
            .cfg
            .lifecycle
            .error_rules
            .iter()
            .any(|r| r.status == code)
        {
            let retry_after = resp
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.trim().parse::<u64>().ok())
                .map(Duration::from_secs);
            // An unreadable/oversize error body just means "no rule can
            // match" (falls through to the default fail-loud handling below);
            // it is counted, never silently swallowed.
            let body = match self.read_body(&mut resp).await {
                Ok(b) => b,
                Err(_) => {
                    self.count("poll_http", "body_unreadable");
                    Vec::new()
                }
            };
            match self
                .cfg
                .lifecycle
                .classify(code, &body)
                .map(|r| r.action.clone())
            {
                Some(HttpAction::Backoff {
                    base,
                    max,
                    honor_retry_after,
                }) => {
                    let wait = backoff_wait(
                        base,
                        max,
                        self.backoff_streak,
                        retry_after.filter(|_| honor_retry_after),
                    );
                    self.backoff_streak = self.backoff_streak.saturating_add(1);
                    self.backoff_until = Instant::now().checked_add(wait);
                    self.count("poll_http", "backoff");
                    tracing::warn!(
                        source_id = %self.source_id,
                        wait_secs = wait.as_secs(),
                        "poll upstream classified as quota/rate limited; backing off"
                    );
                    return Ok(None);
                }
                Some(HttpAction::Park) => {
                    self.count("poll_http", "park");
                    return Err(TransportError::Fatal(format!(
                        "upstream error ({status}) classified fatal"
                    )));
                }
                None => self.count("poll_http", "unclassified"),
            }
        }
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
        let body = self.read_body(&mut resp).await?;
        if body.is_empty() {
            return Ok(None);
        }
        self.backoff_streak = 0;
        if self.cfg.lifecycle.interval_hint.is_some() {
            match self.cfg.lifecycle.next_interval(&body) {
                Some((d, clamped)) => {
                    self.interval_now = d;
                    self.count("poll_interval", if clamped { "clamped" } else { "applied" });
                }
                None => {
                    self.interval_now = self.cfg.interval;
                    self.count("poll_interval", "ignored");
                }
            }
        }
        let digest: [u8; 32] = Sha256::digest(&body).into();
        let next = self.extract_cursor(&body)?;
        // Cursor configured but it cannot advance: yield this window once,
        // never again while it stays identical.
        let advances = next
            .as_ref()
            .is_some_and(|n| Some(n) != self.committed.as_ref());
        if !advances && self.last_static_digest == Some(digest) {
            return Ok(None);
        }
        self.pending = if advances { next } else { None };
        self.pending_digest = Some(digest);
        Ok(Some(body))
    }

    /// Reads a response body up to the size ceiling.
    async fn read_body(&self, resp: &mut reqwest::Response) -> Result<Vec<u8>, TransportError> {
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
        Ok(body)
    }

    /// Reads the next cursor from the body per the configured pointer; an
    /// over-long value is rejected as absent (loud warn, no content logged).
    fn extract_cursor(&self, body: &[u8]) -> Result<Option<String>, TransportError> {
        let Some(ptr) = &self.cfg.cursor_pointer else {
            return Ok(None);
        };
        let v: serde_json::Value = serde_json::from_slice(body)
            .map_err(|_| TransportError::Read("response is not JSON".into()))?;
        let c = match v.pointer(ptr) {
            Some(serde_json::Value::String(s)) if !s.is_empty() => Some(s.clone()),
            Some(serde_json::Value::Number(n)) => Some(n.to_string()),
            _ => None,
        };
        Ok(c.filter(|c| {
            let ok = c.len() <= MAX_CURSOR_BYTES;
            if !ok {
                tracing::warn!(source_id = %self.source_id, "poll cursor over length cap; ignored");
            }
            ok
        }))
    }
}

impl<S: TransportSecretSource + 'static> Transport for PollTransport<S> {
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
            if let Some(until) = self.backoff_until {
                // Cancel-safe: the deadline lives on self, so a cancelled
                // read resumes the same wait.
                tokio::time::sleep_until(until).await;
                self.backoff_until = None;
            }
            if let Some(t) = self.last_poll {
                // interval is validated <= 24h, so overflow is unreachable;
                // checked_add keeps it total regardless.
                match t.checked_add(self.interval_now) {
                    Some(at) => tokio::time::sleep_until(at).await,
                    None => tokio::time::sleep(self.interval_now).await,
                }
            }
            self.pending = None;
            self.pending_digest = None;
            let res = self.fetch().await;
            self.last_poll = Some(Instant::now());
            if let Some(body) = res? {
                return Ok(Some(body));
            }
        }
    }

    async fn close(&mut self, _reason: &str) {
        self.pending = None;
        self.pending_digest = None;
    }

    async fn on_frame_outcome(&mut self, outcome: FrameOutcome) {
        let pending = self.pending.take();
        let digest = self.pending_digest.take();
        match outcome {
            FrameOutcome::Published => {
                self.drop_streak = 0;
                self.last_static_digest = if pending.is_some() { None } else { digest };
                if let Some(c) = pending {
                    self.committed = Some(c);
                }
            }
            FrameOutcome::Dropped(reason) => {
                self.drop_streak += 1;
                if self.drop_streak >= self.cfg.max_drop_attempts {
                    // Poison frame: stop re-fetching it. Advance past it when
                    // the response named a next cursor; otherwise remember its
                    // digest so the identical window is skipped.
                    tracing::error!(
                        source_id = %self.source_id,
                        attempts = self.drop_streak,
                        reason = ?reason,
                        "poll frame dropped repeatedly; skipping poison window"
                    );
                    self.drop_streak = 0;
                    match pending {
                        Some(c) => {
                            self.committed = Some(c);
                            self.last_static_digest = None;
                        }
                        None => self.last_static_digest = digest,
                    }
                }
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
    impl TransportSecretSource for CountingSecrets {
        fn resolve(
            &self,
            _r: ConnectionRef,
            _d: Destination,
        ) -> BoxFut<'_, Result<crate::connector::secrets::ResolvedAuth, SecretError>> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Box::pin(async {
                Ok(crate::connector::secrets::ResolvedAuth {
                    secret: SecretString::new("tok123".into()),
                    header: None,
                })
            })
        }
    }

    type Seen = (Option<String>, Option<String>);

    /// Records (cursor, auth header) per request; replies from `script`.
    #[derive(Clone, Default)]
    struct Srv {
        seen: Arc<Mutex<Vec<Seen>>>,
        status: Arc<Mutex<u16>>,
        mode: Arc<Mutex<u8>>,
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
        match *s.mode.lock().unwrap() {
            1 => {
                return (
                    StatusCode::OK,
                    format!(r#"{{"items":[1],"next":"{}"}}"#, "x".repeat(600)),
                )
            }
            2 => return (StatusCode::OK, r#"{"items":[1]}"#.into()),
            _ => {}
        }
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
            max_drop_attempts: 3,
            egress_allow: vec!["127.0.0.1".parse().unwrap()],
            allow_loopback_insecure: true,
            lifecycle: PollLifecycle::default(),
            request: PollRequest::default(),
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

    #[tokio::test]
    async fn poison_frame_is_skipped_after_n_drops() {
        let (addr, srv) = start().await;
        let (mut t, _) = mk(addr);
        for _ in 0..3 {
            t.next_frame().await.unwrap().unwrap();
            t.on_frame_outcome(FrameOutcome::Dropped(
                crate::connector::FrameDropReason::GuestError,
            ))
            .await;
        }
        assert_eq!(t.cursor(), Some("c1"), "advanced past the poison window");
        t.next_frame().await.unwrap().unwrap();
        let seen = srv.seen.lock().unwrap().clone();
        assert_eq!(seen.len(), 4);
        assert!(seen[..3].iter().all(|s| s.0.is_none()));
        assert_eq!(seen[3].0.as_deref(), Some("c1"));
    }

    #[tokio::test]
    async fn missing_or_oversize_cursor_never_republishes_same_window() {
        for mode in [2u8, 1u8] {
            let (addr, srv) = start().await;
            *srv.mode.lock().unwrap() = mode;
            let (mut t, _) = mk(addr);
            t.next_frame().await.unwrap().unwrap();
            t.on_frame_outcome(FrameOutcome::Published).await;
            assert_eq!(t.cursor(), None, "mode {mode}: cursor must not advance");
            let again = tokio::time::timeout(Duration::from_millis(120), t.next_frame()).await;
            assert!(again.is_err(), "mode {mode}: identical window re-published");
            assert!(srv.seen.lock().unwrap().len() >= 2, "kept polling");
        }
    }

    #[tokio::test]
    async fn ssrf_targets_refused_and_limits_validated() {
        let (addr, _s) = start().await;
        for u in [
            "https://169.254.169.254/x",
            "https://10.1.1.1/x",
            "https://localhost/x",
        ] {
            let mut c = cfg(addr);
            c.url = u.into();
            c.allow_loopback_insecure = false;
            c.egress_allow = vec![];
            let mut t =
                PollTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
            assert!(
                matches!(t.next_frame().await, Err(TransportError::Connect(_))),
                "{u}"
            );
        }
        let s = || CountingSecrets(Arc::new(AtomicUsize::new(0)));
        let mut c = cfg(addr);
        c.interval = Duration::ZERO;
        assert!(PollTransport::new(c, s()).is_err());
        let mut c = cfg(addr);
        c.interval = Duration::MAX;
        assert!(PollTransport::new(c, s()).is_err());
        let mut c = cfg(addr);
        c.max_drop_attempts = 0;
        assert!(PollTransport::new(c, s()).is_err());
    }

    #[tokio::test]
    async fn ambient_proxy_env_does_not_bypass_pin() {
        let _env = crate::connector::transport_common::PROXY_ENV.write().await;
        let (addr, srv) = start().await;
        // A dead proxy: if honored the request would fail / not reach srv.
        std::env::set_var("HTTP_PROXY", "http://127.0.0.1:1");
        std::env::set_var("http_proxy", "http://127.0.0.1:1");
        let (mut t, _) = mk(addr);
        let r = t.next_frame().await;
        std::env::remove_var("HTTP_PROXY");
        std::env::remove_var("http_proxy");
        assert!(r.unwrap().is_some());
        assert_eq!(
            srv.seen.lock().unwrap().len(),
            1,
            "went to the pinned server"
        );
    }

    #[tokio::test]
    async fn newly_denied_ranges_refused_by_poll() {
        let (addr, _s) = start().await;
        for u in [
            "https://[2002:0808:0808::1]/x",
            "https://[2001:2::1]/x",
            "https://[100::1]/x",
            "https://[64:ff9b:1::1]/x",
            "https://[3fff::1]/x",
            "https://[fec0::1]/x",
            "https://192.88.99.1/x",
        ] {
            let mut c = cfg(addr);
            c.url = u.into();
            c.allow_loopback_insecure = false;
            c.egress_allow = vec![];
            let mut t =
                PollTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
            assert!(
                matches!(t.next_frame().await, Err(TransportError::Connect(_))),
                "{u}"
            );
        }
    }

    /// Records method, content-type, body and credential headers of one request.
    /// (method, content-type, body, cookie, x-rsi-token)
    type PostSeen = (
        String,
        Option<String>,
        String,
        Option<String>,
        Option<String>,
    );

    #[derive(Clone, Default)]
    struct PostSrv {
        seen: Arc<Mutex<Vec<PostSeen>>>,
    }

    async fn post_handler(
        State(s): State<PostSrv>,
        method: axum::http::Method,
        h: HeaderMap,
        body: String,
    ) -> (StatusCode, String) {
        let hv = |n: &str| h.get(n).map(|v| v.to_str().unwrap().to_string());
        s.seen.lock().unwrap().push((
            method.to_string(),
            hv("content-type"),
            body,
            hv("cookie"),
            hv("x-rsi-token"),
        ));
        (StatusCode::OK, r#"{"success":1,"data":[1]}"#.into())
    }

    async fn start_post() -> (std::net::SocketAddr, PostSrv) {
        let srv = PostSrv::default();
        let app = Router::new()
            .route("/poll", axum::routing::any(post_handler))
            .with_state(srv.clone());
        let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
        (addr, srv)
    }

    fn post_cfg(addr: std::net::SocketAddr) -> PollConfig {
        let mut c = cfg(addr);
        c.cursor_param = None;
        c.cursor_pointer = None;
        c.auth = Some(PollAuth {
            header: "X-Rsi-Token".into(),
            prefix: String::new(),
            handle: "h1".into(),
        });
        c.request = PollRequest {
            json_body: Some(br#"{"channel_id":"7"}"#.to_vec()),
            extra_auth: vec![PollAuth {
                header: "Cookie".into(),
                prefix: "Rsi-Token=".into(),
                handle: "h1".into(),
            }],
        };
        c
    }

    #[tokio::test]
    async fn post_json_body_with_host_owned_credential_headers() {
        let (addr, srv) = start_post().await;
        let n = Arc::new(AtomicUsize::new(0));
        let mut t = PollTransport::new(post_cfg(addr), CountingSecrets(n.clone())).unwrap();
        assert!(t.next_frame().await.unwrap().is_some());
        let seen = srv.seen.lock().unwrap().clone();
        assert_eq!(seen.len(), 1);
        let (m, ct, body, cookie, tok) = &seen[0];
        assert_eq!(m, "POST");
        assert_eq!(ct.as_deref(), Some("application/json"));
        assert_eq!(body, r#"{"channel_id":"7"}"#);
        assert_eq!(cookie.as_deref(), Some("Rsi-Token=tok123"));
        assert_eq!(tok.as_deref(), Some("tok123"));
        assert_eq!(
            n.load(Ordering::SeqCst),
            2,
            "one resolve per credential header"
        );
    }

    #[tokio::test]
    async fn default_request_stays_get_without_body() {
        let (addr, srv) = start_post().await;
        let mut c = post_cfg(addr);
        c.request = PollRequest::default();
        let mut t = PollTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        t.next_frame().await.unwrap().unwrap();
        let seen = srv.seen.lock().unwrap().clone();
        assert_eq!(seen[0].0, "GET");
        assert_eq!(seen[0].1, None);
        assert_eq!(seen[0].3, None, "no extra credential header on plain GET");
    }

    type Mutator = Box<dyn Fn(&mut PollRequest)>;

    #[tokio::test]
    async fn request_shape_is_validated_fail_loud() {
        let (addr, _s) = start_post().await;
        let s = || CountingSecrets(Arc::new(AtomicUsize::new(0)));
        let bad: Vec<Mutator> = vec![
            Box::new(|r| r.json_body = Some(b"not json".to_vec())),
            Box::new(|r| r.json_body = Some(vec![b' '; MAX_REQUEST_BODY_BYTES + 1])),
            Box::new(|r| r.extra_auth[0].header = "bad header".into()),
            Box::new(|r| r.extra_auth[0].header = "Content-Type".into()),
            Box::new(|r| r.extra_auth[0].header = "Host".into()),
            Box::new(|r| r.extra_auth[0].handle = "nope".into()),
        ];
        for (i, f) in bad.iter().enumerate() {
            let mut c = post_cfg(addr);
            f(&mut c.request);
            assert!(PollTransport::new(c, s()).is_err(), "case {i}");
        }
    }

    #[tokio::test]
    async fn post_stateless_identical_window_not_republished() {
        let (addr, _srv) = start_post().await;
        let mut t = PollTransport::new(
            post_cfg(addr),
            CountingSecrets(Arc::new(AtomicUsize::new(0))),
        )
        .unwrap();
        t.next_frame().await.unwrap().unwrap();
        t.on_frame_outcome(FrameOutcome::Published).await;
        let again = tokio::time::timeout(Duration::from_millis(120), t.next_frame()).await;
        assert!(again.is_err(), "identical POST window re-published");
    }
}

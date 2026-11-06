//! Generic, platform-agnostic WebSocket [`Transport`]. URL, handshake slots,
//! secret handles, resume token and limits all come from [`WsConfig`] (the
//! connector's config); nothing here names a platform. The connector-built
//! handshake is sent on every (re)connect with secret slots substituted per
//! call via the injected [`TransportSecretSource`] (`connection_resolver` in
//! production): the resolved value lives only in a zeroizing buffer for the
//! duration of the send and is never cached, logged or returned to a guest.
//!
//! Residual (library-forced): `Message::text` copies the payload into
//! tungstenite's own non-zeroizing `Utf8Bytes`/write buffer, so a copy of
//! the substituted handshake outlives our `Zeroizing` buffer until that
//! allocation is reused. We zeroize everything we own and do not pretend
//! otherwise.
//!
//! SSRF: the endpoint host is resolved by [`resolve_pinned`], every address
//! checked against the egress denylist, and the TCP connection is made to the
//! pinned address (no client-side re-resolution).

use std::collections::{HashMap, HashSet, VecDeque};
use std::net::IpAddr;
use std::time::Duration;

use connection_resolver::ConnectionRef;
use futures_util::{SinkExt, StreamExt};
use sha2::{Digest, Sha256};
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, WebSocketConfig};
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use super::egress_guard::resolve_pinned;
use super::lifecycle::{LifecycleAction, SessionEngine};
pub use super::lifecycle::{LifecycleError, SessionRules};
use super::secrets::{Destination, TransportSecretSource};
use super::transport::{Transport, TransportError};
use super::transport_common::{
    opaque_id, slot_refusal, slot_refused_error, substitute_slots, validate_endpoint, Applied,
    FrameRuleMetrics, FrameRules, SlotPrefixes, SECRET_NOT_SPLICE_SAFE,
};
pub use super::transport_common::{AutoReplyRule, FrameRuleError, SelfEchoFilter};
use super::types::{ConnectionCtx, Handshake};

/// Close code sent on a heartbeat-ACK timeout (anything but 1000/1001 keeps a
/// gateway session resumable).
const DEAD_CONNECTION_CLOSE_CODE: u16 = 4000;

/// Largest accepted `dedup_window`.
pub const MAX_DEDUP_WINDOW: usize = 65_536;

type Stream = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

/// Connector-supplied configuration for one WebSocket source.
#[derive(Debug, Clone)]
pub struct WsConfig {
    /// Platform slug (must have a pinned connector).
    pub platform: String,
    /// `wss://` endpoint from the connector config.
    pub url: String,
    /// Host-side key hashed into the opaque `source_id` (never exposed).
    pub source_key: String,
    /// Handle -> connection the handshake's `secret_refs` may name.
    pub handles: HashMap<String, ConnectionRef>,
    /// Opaque RESUME handle handed to the connector in the ctx (the host is
    /// the only reader); when set the `connection_id` stays stable across
    /// reconnects of this transport.
    pub resume_token: Option<String>,
    /// Connect + handshake-send timeout.
    pub connect_timeout: Duration,
    /// Max time without a DATA frame before the read fails (driver
    /// reconnects). Ping/pong/control frames do NOT reset it.
    pub idle_timeout: Duration,
    /// Hard cap on one connection's lifetime regardless of traffic, so a
    /// control-frame flood cannot hold a connection open forever.
    pub max_lifetime: Duration,
    /// Exact IPs the host explicitly grants despite the SSRF denylist
    /// (loopback/private/etc.); empty in production unless granted.
    pub egress_allow: Vec<IpAddr>,
    /// Max inbound message size; larger is a protocol error.
    pub max_frame_bytes: usize,
    /// Recent-frame digest window used to drop replays after a reconnect
    /// (0 = off, the default stance; max [`MAX_DEDUP_WINDOW`]). Identical
    /// legitimate frames inside the window are dropped too, so enable only
    /// for platforms that replay on resume; otherwise the downstream
    /// consumer must be idempotent.
    pub dedup_window: usize,
    /// Permit `ws://` to a loopback host (tests only; the address must
    /// still be in `egress_allow`).
    pub allow_loopback_insecure: bool,
    /// Host-pinned byte prefixes, PER secret handle, that the handle's
    /// credential slot may directly follow in the connector-authored handshake
    /// (e.g. `pass` -> `PASS oauth:`, `discord-bot` -> `"token":"`). A handle
    /// with no entry may not be substituted (fail closed), one handle's prefix
    /// never authorizes another's slot, and every key must be a declared
    /// handle (refused at construction). Never derived from guest data.
    pub slot_prefixes: SlotPrefixes,
    /// Host-owned, declarative auto-reply rules (e.g. a keepalive
    /// `PING :<t>` -> `PONG :<t>`), answered by the transport itself because
    /// the connector's `on-frame` can only return events. Empty = none (the
    /// default stance); an invalid rule set is refused at construction.
    pub auto_replies: Vec<AutoReplyRule>,
    /// Host-side drop of the connection's own echoed lines; `None` = off.
    pub self_echo: Option<SelfEchoFilter>,
    /// How often the driver asks the connector for a heartbeat body
    /// (`None` = this source has no heartbeat). The driver range-checks it.
    pub heartbeat_interval: Option<Duration>,
    /// Host-owned session lifecycle (captured session state, dynamic
    /// heartbeat interval, reconnect/park directives, close-code
    /// classification); `SessionRules::default()` = none.
    pub session: SessionRules,
}

/// WebSocket transport over tokio-tungstenite.
pub struct WsTransport<S: TransportSecretSource> {
    cfg: WsConfig,
    url: reqwest::Url,
    source_id: String,
    secrets: S,
    stream: Option<Stream>,
    connected_at: Instant,
    last_data: Instant,
    connection_id: String,
    seen: HashSet<[u8; 32]>,
    order: VecDeque<[u8; 32]>,
    rules: FrameRules,
    /// Host-built replies not yet written; popped only after a completed
    /// send, so a cancelled `next_frame` re-sends instead of losing one.
    pending: VecDeque<Vec<u8>>,
    metrics: Option<FrameRuleMetrics>,
    /// Per-source session state + lifecycle rules (survives reconnects so a
    /// RESUME can use it; dropped by `reconnect_fresh`).
    engine: SessionEngine,
    /// Heartbeat interval captured from the CURRENT connection's frames.
    dyn_heartbeat: Option<Duration>,
    /// Set when a missed heartbeat ACK ended the connection: the close is
    /// sent with a non-1000 code so the upstream keeps the session
    /// resumable (a 1000/1001 close invalidates it).
    dead_close: bool,
}

impl<S: TransportSecretSource> WsTransport<S> {
    /// Validates the config; a bad endpoint or limit is refused up front.
    pub fn new(cfg: WsConfig, secrets: S) -> Result<Self, TransportError> {
        let url = validate_endpoint(&cfg.url, &["wss"], "ws", cfg.allow_loopback_insecure)
            .map_err(TransportError::Connect)?;
        if cfg.max_frame_bytes == 0
            || cfg.dedup_window > MAX_DEDUP_WINDOW
            || cfg.idle_timeout.is_zero()
            || cfg.idle_timeout > Duration::from_secs(86_400)
            || cfg.max_lifetime.is_zero()
            || cfg.max_lifetime > Duration::from_secs(7 * 86_400)
        {
            return Err(TransportError::Connect("invalid ws limits".into()));
        }
        let rules = FrameRules::new(cfg.auto_replies.clone(), cfg.self_echo.clone())
            .map_err(|e| TransportError::Connect(e.to_string()))?;
        if cfg.slot_prefixes.iter().any(|(h, v)| {
            !cfg.handles.contains_key(h) || v.is_empty() || v.iter().any(String::is_empty)
        }) {
            return Err(TransportError::Connect(
                "slot_prefixes must name declared handles with non-empty prefixes".into(),
            ));
        }
        let engine = SessionEngine::new_with(cfg.session.clone(), cfg.allow_loopback_insecure)
            .map_err(|e| TransportError::Connect(e.to_string()))?;
        if engine.has_token_template() && cfg.resume_token.is_some() {
            return Err(TransportError::Connect(
                "session_token_template and a static resume_token are mutually exclusive".into(),
            ));
        }
        let now = Instant::now();
        Ok(Self {
            source_id: opaque_id("ws", &cfg.source_key),
            connection_id: uuid::Uuid::new_v4().to_string(),
            cfg,
            url,
            secrets,
            stream: None,
            connected_at: now,
            last_data: now,
            seen: HashSet::new(),
            order: VecDeque::new(),
            rules,
            pending: VecDeque::new(),
            metrics: None,
            engine,
            dyn_heartbeat: None,
            dead_close: false,
        })
    }

    /// Attaches the frame-rule counters (the integrator registers them once
    /// per registry via [`FrameRuleMetrics::register`]).
    #[must_use]
    pub fn with_metrics(mut self, metrics: FrameRuleMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    fn count(&self, rule: &str, outcome: &str, n: u64) {
        if n > 0 {
            if let Some(m) = &self.metrics {
                m.add(&self.cfg.platform, rule, outcome, n);
            }
        }
    }

    /// Writes one frame (text when UTF-8, else binary) bounded by the
    /// connect timeout.
    async fn write_frame(&mut self, bytes: &[u8]) -> Result<(), TransportError> {
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| TransportError::Send("not connected".into()))?;
        let msg = match std::str::from_utf8(bytes) {
            Ok(t) => Message::text(t),
            Err(_) => Message::binary(bytes.to_vec()),
        };
        tokio::time::timeout(self.cfg.connect_timeout, stream.send(msg))
            .await
            .map_err(|_| TransportError::Send("send timed out".into()))?
            .map_err(|e| TransportError::Send(e.to_string()))
    }

    /// Flushes queued auto-replies, oldest first. Cancel-safe: an entry
    /// leaves the queue only after its send completed.
    async fn flush_pending(&mut self) -> Result<(), TransportError> {
        while let Some(front) = self.pending.front().cloned() {
            if let Err(e) = self.write_frame(&front).await {
                self.count("auto_reply", "reply_failed", 1);
                return Err(TransportError::Read(format!("auto-reply {e}")));
            }
            self.pending.pop_front();
            self.count("auto_reply", "replied", 1);
        }
        Ok(())
    }

    /// True when `frame` was seen within the dedup window; records it.
    fn is_replay(&mut self, frame: &[u8]) -> bool {
        if self.cfg.dedup_window == 0 {
            return false;
        }
        let d: [u8; 32] = Sha256::digest(frame).into();
        if !self.seen.insert(d) {
            return true;
        }
        self.order.push_back(d);
        if self.order.len() > self.cfg.dedup_window {
            if let Some(old) = self.order.pop_front() {
                self.seen.remove(&old);
            }
        }
        false
    }

    /// Substitutes and sends the handshake; the caller tears the connection
    /// down on any error.
    async fn handshake_inner(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        if handshake.bytes.is_empty() && handshake.secret_refs.is_empty() {
            return Ok(());
        }
        let dest = Destination::from_url(&self.url)
            .ok_or_else(|| TransportError::Handshake("not connected".into()))?;
        // Host session slots first (validated, charset-safe values; secret
        // slots are not state names and pass through untouched), then the
        // secret pass over the rendered bytes.
        let handshake = if self.engine.is_inert() {
            handshake
        } else {
            let bytes = self
                .engine
                .render_handshake(&handshake.bytes)
                .ok_or_else(|| TransportError::Handshake("session slot has no value".into()))?;
            Handshake {
                bytes,
                secret_refs: handshake.secret_refs,
            }
        };
        if let Some(refusal) = slot_refusal(
            &handshake.bytes,
            &handshake.secret_refs,
            &self.cfg.slot_prefixes,
        ) {
            self.count("slot_pairing", refusal.outcome(), 1);
            tracing::warn!(platform = %self.cfg.platform, outcome = refusal.outcome(), "handshake credential slot refused: handle/prefix pairing not declared");
            return Err(slot_refused_error());
        }
        let payload = substitute_slots(
            &self.secrets,
            &self.cfg.handles,
            &handshake.bytes,
            &handshake.secret_refs,
            &self.cfg.slot_prefixes,
            &dest,
        )
        .await
        .inspect_err(|e| {
            if matches!(e, TransportError::Fatal(m) if m == SECRET_NOT_SPLICE_SAFE) {
                self.count("slot_pairing", "refused_secret_charset", 1);
                tracing::warn!(platform = %self.cfg.platform, "handshake secret refused: not splice-safe");
            }
        })?;
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| TransportError::Handshake("not connected".into()))?;
        let msg = match std::str::from_utf8(&payload) {
            Ok(t) => Message::text(t),
            Err(_) => Message::binary(payload.to_vec()),
        };
        tokio::time::timeout(self.cfg.connect_timeout, stream.send(msg))
            .await
            .map_err(|_| TransportError::Handshake("handshake send timed out".into()))?
            .map_err(|e| TransportError::Handshake(e.to_string()))
    }
}

impl<S: TransportSecretSource> WsTransport<S> {
    /// Test-only view of the token the next connect would carry.
    #[cfg(test)]
    pub(crate) fn session_token_for_test(&self) -> Option<String> {
        self.engine.render_session_token()
    }

    /// Applies a lifecycle action: reconnect = clean end (the driver
    /// reconnects under backoff), fresh = drop state first, park = terminal.
    fn apply_action(
        &mut self,
        action: LifecycleAction,
        what: &'static str,
    ) -> Result<Option<Vec<u8>>, TransportError> {
        match action {
            LifecycleAction::Reconnect => Ok(None),
            LifecycleAction::ReconnectFresh => {
                self.engine.clear();
                Ok(None)
            }
            LifecycleAction::Park => Err(TransportError::Fatal(format!("{what} classified fatal"))),
        }
    }

    /// Handles a received Close frame: classified codes act per the host
    /// rules; an unclassified code is a plain remote close (reconnect, state
    /// kept) exactly as before.
    fn on_close(&mut self, code: Option<u16>) -> Result<Option<Vec<u8>>, TransportError> {
        match code.and_then(|c| self.engine.classify_close(c)) {
            Some(action) => {
                self.count("close_code", action.label(), 1);
                self.apply_action(action, "close code")
            }
            None => {
                if code.is_some() {
                    self.count("close_code", "unclassified", 1);
                }
                Ok(None)
            }
        }
    }
}

fn classify_connect(e: WsError) -> TransportError {
    match e {
        WsError::Http(r) if matches!(r.status().as_u16(), 401 | 403) => {
            TransportError::Fatal(format!("upstream rejected credentials ({})", r.status()))
        }
        WsError::Http(r) => TransportError::Connect(format!("http {}", r.status())),
        // Display of tungstenite errors carries no frame or credential data.
        other => TransportError::Connect(other.to_string()),
    }
}

impl<S: TransportSecretSource + 'static> Transport for WsTransport<S> {
    fn platform(&self) -> &str {
        &self.cfg.platform
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        self.stream = None;
        // The interval is per connection (re-announced in the next HELLO).
        self.dyn_heartbeat = None;
        // A reply owed to the previous connection must never reach this one.
        self.pending.clear();
        self.engine.begin_connection();
        self.dead_close = false;
        let wsc = WebSocketConfig::default()
            .max_message_size(Some(self.cfg.max_frame_bytes))
            .max_frame_size(Some(self.cfg.max_frame_bytes));
        // A validated reconnect URL (e.g. a gateway resume host) replaces the
        // configured one only while a session exists to resume.
        let resume = match self.engine.reconnect_target() {
            Some(t) => Some(
                reqwest::Url::parse(t)
                    .map_err(|_| TransportError::Connect("invalid reconnect url".into()))?,
            ),
            None => None,
        };
        let target = match &resume {
            Some(u) => u,
            None => &self.url,
        };
        // Resolve + validate + PIN the address; the client never re-resolves.
        let pinned = match resolve_pinned(target, &self.cfg.egress_allow).await {
            Ok(p) => p,
            Err(e) => {
                if resume.is_some() {
                    // The SSRF guard refused the reconnect host: drop the
                    // session so the next attempt is a fresh identify on the
                    // configured URL -- counted and logged, never silent.
                    self.engine.clear();
                    self.count("reconnect_url", "refused_cleared", 1);
                    tracing::warn!(platform = %self.cfg.platform, "reconnect url refused by the egress guard; session dropped, next connect is fresh");
                }
                return Err(TransportError::Connect(e));
            }
        };
        let req = target
            .as_str()
            .into_client_request()
            .map_err(|_| TransportError::Connect("invalid websocket request".into()))?;
        let dial = async {
            let tcp = tokio::net::TcpStream::connect(pinned)
                .await
                .map_err(WsError::Io)?;
            let _ = tcp.set_nodelay(true);
            tokio_tungstenite::client_async_tls_with_config(req, tcp, Some(wsc), None).await
        };
        let (stream, _) = tokio::time::timeout(self.cfg.connect_timeout, dial)
            .await
            .map_err(|_| TransportError::Connect("connect timed out".into()))?
            .map_err(classify_connect)?;
        if resume.is_some() {
            self.count("reconnect_url", "used", 1);
        }
        self.stream = Some(stream);
        self.connected_at = Instant::now();
        self.last_data = self.connected_at;
        if self.cfg.resume_token.is_none() {
            self.connection_id = uuid::Uuid::new_v4().to_string();
        }
        Ok(ConnectionCtx {
            platform: self.cfg.platform.clone(),
            source_id: self.source_id.clone(),
            connection_id: self.connection_id.clone(),
            session_token: self
                .cfg
                .resume_token
                .clone()
                .or_else(|| self.engine.render_session_token()),
        })
    }

    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError> {
        let res = self.handshake_inner(handshake).await;
        if res.is_err() {
            // Fail closed: never read frames from a connection whose
            // handshake did not complete.
            self.close("handshake failed").await;
        }
        res
    }

    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError> {
        loop {
            self.flush_pending().await?;
            let idle_at = self.last_data + self.cfg.idle_timeout;
            let life_at = self.connected_at + self.cfg.max_lifetime;
            let stream = self
                .stream
                .as_mut()
                .ok_or_else(|| TransportError::Read("not connected".into()))?;
            let item = tokio::time::timeout_at(idle_at.min(life_at), stream.next())
                .await
                .map_err(|_| TransportError::Read("idle or lifetime limit reached".into()))?;
            let bytes = match item {
                Some(Ok(Message::Close(frame))) => {
                    return self.on_close(frame.map(|f| u16::from(f.code)))
                }
                None => return Ok(None),
                Some(Err(WsError::ConnectionClosed | WsError::AlreadyClosed)) => return Ok(None),
                Some(Err(e)) => return Err(TransportError::Read(e.to_string())),
                Some(Ok(Message::Text(t))) => t.as_bytes().to_vec(),
                Some(Ok(Message::Binary(b))) => b.to_vec(),
                // Control frames are answered inside tungstenite and do not
                // reset the idle clock (flood resistance).
                Some(Ok(_)) => continue,
            };
            self.last_data = Instant::now();
            match self.engine.observe(&bytes) {
                Ok(obs) => {
                    self.count("session_capture", "stored", obs.captured as u64);
                    if obs.reconnect_url {
                        self.count("reconnect_url", "stored", 1);
                    }
                    if let Some(d) = obs.heartbeat {
                        self.dyn_heartbeat = Some(d);
                        self.count("heartbeat_interval", "applied", 1);
                    }
                    if let Some(action) = obs.action {
                        self.count("directive", action.label(), 1);
                        return self.apply_action(action, "frame directive");
                    }
                }
                Err(e) => {
                    let rule = match e {
                        LifecycleError::BadHeartbeat => "heartbeat_interval",
                        LifecycleError::BadReconnectUrl => "reconnect_url",
                        _ => "session_capture",
                    };
                    self.count(rule, "rejected", 1);
                    return Err(TransportError::Read(e.to_string()));
                }
            }
            let bytes = match self.rules.apply(&bytes) {
                Ok(Applied {
                    forward,
                    replies,
                    echo_dropped,
                }) => {
                    self.count("self_echo", "dropped", echo_dropped as u64);
                    self.pending.extend(replies);
                    match forward {
                        Some(b) => b,
                        None => continue,
                    }
                }
                Err(e) => {
                    self.count("auto_reply", "rejected", 1);
                    return Err(TransportError::Read(e.to_string()));
                }
            };
            if self.is_replay(&bytes) {
                continue;
            }
            return Ok(Some(bytes));
        }
    }

    fn heartbeat_interval(&self) -> Option<Duration> {
        self.dyn_heartbeat.or(self.cfg.heartbeat_interval)
    }

    async fn send_heartbeat(&mut self, bytes: Vec<u8>) -> Result<(), TransportError> {
        if self.heartbeat_interval().is_none() {
            return Err(TransportError::Send(
                "heartbeat sent but no heartbeat_interval configured".into(),
            ));
        }
        // Zombie-connection rule: a heartbeat is due but the previous one was
        // never acknowledged -> the connection is dead. End it (the driver
        // reconnects; session state is kept so it RESUMEs).
        if !self.engine.heartbeat_due() {
            if let Some(m) = &self.metrics {
                m.ack_timeout(&self.cfg.platform);
            }
            self.dead_close = true;
            tracing::warn!(platform = %self.cfg.platform, "heartbeat not acknowledged before the next was due; treating the connection as dead, reconnecting to resume");
            return Err(TransportError::Send("heartbeat ack timeout".into()));
        }
        // The guest cannot see the live sequence; a host-owned heartbeat
        // replaces its body with the host-rendered one.
        let bytes = if self.engine.host_owned_heartbeat() {
            self.engine
                .render_default_heartbeat()
                .ok_or_else(|| TransportError::Send("heartbeat slot has no value".into()))?
        } else {
            bytes
        };
        if bytes.is_empty() || bytes.len() > self.cfg.max_frame_bytes {
            return Err(TransportError::Send("heartbeat body size invalid".into()));
        }
        self.write_frame(&bytes).await
    }

    async fn send_default_heartbeat(&mut self) -> Result<(), TransportError> {
        let body = self.engine.render_default_heartbeat().ok_or_else(|| {
            TransportError::Send(
                "no default_heartbeat configured (or a slot has no value) for a connector that asked for the platform default"
                    .into(),
            )
        })?;
        self.send_heartbeat(body).await
    }

    fn handshake_after_first_frame(&self) -> Option<Duration> {
        self.cfg.session.handshake_after.first_frame_wait()
    }

    fn first_heartbeat_jitter(&self) -> bool {
        self.cfg.session.jitter_first_heartbeat
    }

    async fn close(&mut self, _reason: &str) {
        if let Some(mut s) = self.stream.take() {
            // Non-1000 on a dead connection keeps the session resumable; the
            // wait is bounded because a zombie peer never answers the close.
            let frame = self.dead_close.then(|| CloseFrame {
                code: CloseCode::Library(DEAD_CONNECTION_CLOSE_CODE),
                reason: "heartbeat ack timeout".into(),
            });
            match tokio::time::timeout(self.cfg.connect_timeout, s.close(frame)).await {
                Ok(Ok(())) => {}
                Ok(Err(_)) | Err(_) => {
                    tracing::debug!(platform = %self.cfg.platform, "websocket close did not complete cleanly");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::connector::transport::TransportError as TE;
    use crate::connector::webhook::{BoxFut, SecretError};
    use connection_resolver::SecretString;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio_tungstenite::accept_async;

    const REF: ConnectionRef = ConnectionRef {
        community_id: 7,
        connection_id: 9,
    };

    /// Counts every resolve; never caches -- the count proves per-call.
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

    fn cfg(addr: std::net::SocketAddr) -> WsConfig {
        WsConfig {
            platform: "testplat".into(),
            url: format!("ws://{addr}/gw"),
            source_key: "channel-secret-name".into(),
            handles: HashMap::from([("h1".to_string(), REF)]),
            resume_token: None,
            connect_timeout: Duration::from_secs(2),
            idle_timeout: Duration::from_secs(2),
            max_lifetime: Duration::from_secs(60),
            egress_allow: vec!["127.0.0.1".parse().unwrap()],
            max_frame_bytes: 1024,
            dedup_window: 0,
            allow_loopback_insecure: true,
            slot_prefixes: HashMap::from([(
                "h1".to_string(),
                vec![r#"{"op":"auth","token":""#.to_string()],
            )]),
            auto_replies: vec![],
            self_echo: None,
            heartbeat_interval: None,
            session: SessionRules::default(),
        }
    }

    /// Server script per accepted connection: optional expected-handshake
    /// capture, then send `frames`, then close.
    async fn serve(
        l: TcpListener,
        scripts: Vec<(bool, Vec<&'static str>)>,
        got: tokio::sync::mpsc::UnboundedSender<String>,
    ) {
        for (read_hs, frames) in scripts {
            let (tcp, _) = l.accept().await.unwrap();
            let mut ws = accept_async(tcp).await.unwrap();
            if read_hs {
                if let Some(Ok(Message::Text(t))) = ws.next().await {
                    got.send(t.to_string()).unwrap();
                }
            }
            for f in frames {
                ws.send(Message::text(f)).await.unwrap();
            }
            let _ = ws.close(None).await;
        }
    }

    fn hs() -> Handshake {
        Handshake {
            bytes: br#"{"op":"auth","token":"{{TOKEN}}"}"#.to_vec(),
            secret_refs: vec![("{{TOKEN}}".into(), "h1".into())],
        }
    }

    /// regression: p982-A -- a hostile handshake template that places the
    /// token in a message body is refused before any bytes are sent.
    #[tokio::test]
    async fn handshake_slot_outside_pinned_prefix_is_refused() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(l, vec![(false, vec![])], tx));
        let n = Arc::new(AtomicUsize::new(0));
        let mut t = WsTransport::new(cfg(addr), CountingSecrets(n.clone())).unwrap();
        t.connect().await.unwrap();
        let evil = Handshake {
            bytes: br#"{"op":"say","text":"{{TOKEN}}"}"#.to_vec(),
            secret_refs: vec![("{{TOKEN}}".into(), "h1".into())],
        };
        assert!(t.send_handshake(evil).await.is_err());
        assert_eq!(n.load(Ordering::SeqCst), 0, "secret never resolved");
    }

    /// Slot after a prefix declared only for a DIFFERENT handle is refused,
    /// counted with its typed reason, and the secret is never resolved.
    #[tokio::test]
    async fn handshake_slot_after_other_handles_prefix_is_refused_and_counted() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(l, vec![(false, vec![])], tx));
        let n = Arc::new(AtomicUsize::new(0));
        let mut c = cfg(addr);
        c.handles.insert("h2".into(), REF);
        c.slot_prefixes = HashMap::from([
            ("h1".to_string(), vec!["PASS oauth:".to_string()]),
            ("h2".to_string(), vec!["NICK ".to_string()]),
        ]);
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        let mut t = WsTransport::new(c, CountingSecrets(n.clone()))
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        let evil = Handshake {
            bytes: b"NICK {{TOKEN}}\r\n".to_vec(),
            secret_refs: vec![("{{TOKEN}}".into(), "h1".into())],
        };
        assert!(t.send_handshake(evil).await.is_err());
        // Handle with no declared prefix at all.
        let undeclared = Handshake {
            bytes: b"x{{N}}".to_vec(),
            secret_refs: vec![("{{N}}".into(), "h1".into())],
        };
        let mut t2 = t;
        t2.cfg.slot_prefixes = HashMap::new();
        assert!(t2.send_handshake(undeclared).await.is_err());
        assert_eq!(n.load(Ordering::SeqCst), 0, "secret never resolved");
        let fam = reg
            .gather()
            .into_iter()
            .find(|f| f.name() == "svc_ingest_transport_frame_rules_total")
            .unwrap();
        let mut got: Vec<(String, u64)> = fam
            .get_metric()
            .iter()
            .map(|m| {
                let outcome = m
                    .get_label()
                    .iter()
                    .find(|l| l.name() == "outcome")
                    .unwrap()
                    .value()
                    .to_string();
                (outcome, m.get_counter().value() as u64)
            })
            .collect();
        got.sort();
        assert_eq!(
            got,
            vec![
                ("refused_placement".to_string(), 1),
                ("refused_undeclared".to_string(), 1)
            ]
        );
    }

    /// Resolves a hostile secret carrying CRLF (command injection attempt).
    struct CrlfSecrets;
    impl TransportSecretSource for CrlfSecrets {
        fn resolve(
            &self,
            _r: ConnectionRef,
            _d: Destination,
        ) -> BoxFut<'_, Result<crate::connector::secrets::ResolvedAuth, SecretError>> {
            Box::pin(async {
                Ok(crate::connector::secrets::ResolvedAuth {
                    secret: SecretString::new("a\r\nPRIVMSG #c :x".into()),
                    header: None,
                })
            })
        }
    }

    /// A secret that could inject structure is refused (typed, counted) and
    /// nothing is sent.
    #[tokio::test]
    async fn crlf_secret_is_refused_and_counted() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(l, vec![(false, vec![])], tx));
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        let mut t = WsTransport::new(cfg(addr), CrlfSecrets)
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        assert!(t.send_handshake(hs()).await.is_err());
        let fam = reg
            .gather()
            .into_iter()
            .find(|f| f.name() == "svc_ingest_transport_frame_rules_total")
            .unwrap();
        assert!(fam.get_metric().iter().any(|m| m
            .get_label()
            .iter()
            .any(|l| l.value() == "refused_secret_charset")
            && m.get_counter().value() as u64 == 1));
    }

    #[test]
    fn slot_prefix_config_is_validated_at_construction() {
        let addr: std::net::SocketAddr = "127.0.0.1:1".parse().unwrap();
        let n = || CountingSecrets(Arc::new(AtomicUsize::new(0)));
        for bad in [
            HashMap::from([("nope".to_string(), vec!["x".to_string()])]),
            HashMap::from([("h1".to_string(), vec![])]),
            HashMap::from([("h1".to_string(), vec![String::new()])]),
        ] {
            let mut c = cfg(addr);
            c.slot_prefixes = bad;
            assert!(WsTransport::new(c, n()).is_err());
        }
        assert!(WsTransport::new(cfg(addr), n()).is_ok());
    }

    #[tokio::test]
    async fn connect_handshake_and_frames() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(l, vec![(true, vec!["f1", "f2"])], tx));
        let n = Arc::new(AtomicUsize::new(0));
        let mut t = WsTransport::new(cfg(addr), CountingSecrets(n.clone())).unwrap();
        let ctx = t.connect().await.unwrap();
        assert!(ctx.source_id.starts_with("ws-") && !ctx.source_id.contains("channel"));
        t.send_handshake(hs()).await.unwrap();
        assert_eq!(
            rx.recv().await.unwrap(),
            r#"{"op":"auth","token":"tok123"}"#
        );
        assert_eq!(t.next_frame().await.unwrap().unwrap(), b"f1");
        assert_eq!(t.next_frame().await.unwrap().unwrap(), b"f2");
        assert_eq!(t.next_frame().await.unwrap(), None);
        assert_eq!(n.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn secret_resolved_per_call_never_cached() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(l, vec![(true, vec![]), (true, vec![])], tx));
        let n = Arc::new(AtomicUsize::new(0));
        let mut t = WsTransport::new(cfg(addr), CountingSecrets(n.clone())).unwrap();
        for i in 1..=2 {
            t.connect().await.unwrap();
            t.send_handshake(hs()).await.unwrap();
            rx.recv().await.unwrap();
            assert_eq!(n.load(Ordering::SeqCst), i);
        }
    }

    #[tokio::test]
    async fn handshake_failures_are_closed() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(
            l,
            vec![(false, vec!["leak"]), (false, vec!["leak"])],
            tx,
        ));
        let n = Arc::new(AtomicUsize::new(0));
        let mut t = WsTransport::new(cfg(addr), CountingSecrets(n.clone())).unwrap();
        let mut bad = hs();
        bad.secret_refs[0].1 = "unknown".into();
        let mut noslot = hs();
        noslot.secret_refs[0].0 = "{{ABSENT}}".into();
        let mut errs = Vec::new();
        for h in [bad, noslot] {
            t.connect().await.unwrap();
            errs.push(t.send_handshake(h).await.unwrap_err());
            // regression: failed handshake must NOT leave a readable stream.
            assert!(matches!(t.next_frame().await, Err(TE::Read(_))));
        }
        assert_eq!(errs[0], errs[1], "uniform error: no slot/handle oracle");
        assert_eq!(
            n.load(Ordering::SeqCst),
            0,
            "no secret resolved on shape failure"
        );
    }

    #[tokio::test]
    async fn reconnect_replay_not_double_processed() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve(
            l,
            vec![(false, vec!["A"]), (false, vec!["A", "B"])],
            tx,
        ));
        let mut c = cfg(addr);
        c.dedup_window = 8;
        c.resume_token = Some("sess-1".into());
        let mut t = WsTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        let c1 = t.connect().await.unwrap();
        assert_eq!(t.next_frame().await.unwrap().unwrap(), b"A");
        assert_eq!(t.next_frame().await.unwrap(), None);
        let c2 = t.connect().await.unwrap();
        assert_eq!(
            c1.connection_id, c2.connection_id,
            "resume keeps the session"
        );
        assert_eq!(c2.session_token.as_deref(), Some("sess-1"));
        assert_eq!(t.next_frame().await.unwrap().unwrap(), b"B");
    }

    #[tokio::test]
    async fn upstream_401_is_fatal() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move {
            let (mut s, _) = l.accept().await.unwrap();
            let mut b = [0u8; 1024];
            let _ = s.read(&mut b).await;
            s.write_all(b"HTTP/1.1 401 Unauthorized\r\ncontent-length: 0\r\n\r\n")
                .await
                .unwrap();
        });
        let mut t =
            WsTransport::new(cfg(addr), CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        assert!(t.connect().await.unwrap_err().is_fatal());
    }

    #[tokio::test]
    async fn oversize_frame_is_a_read_error() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let big: &'static str = Box::leak("x".repeat(4096).into_boxed_str());
        tokio::spawn(serve(l, vec![(false, vec![big])], tx));
        let mut t =
            WsTransport::new(cfg(addr), CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        t.connect().await.unwrap();
        assert!(matches!(t.next_frame().await, Err(TE::Read(_))));
    }

    #[test]
    fn insecure_or_credentialed_urls_refused() {
        let s = || CountingSecrets(Arc::new(AtomicUsize::new(0)));
        let a: std::net::SocketAddr = "127.0.0.1:1".parse().unwrap();
        let mut c = cfg(a);
        c.allow_loopback_insecure = false;
        assert!(WsTransport::new(c, s()).is_err());
        let mut c = cfg(a);
        c.url = "wss://u:p@host.example/".into();
        assert!(WsTransport::new(c, s()).is_err());
    }

    #[tokio::test]
    async fn ssrf_targets_refused_before_connect() {
        for u in [
            "wss://169.254.169.254/latest",
            "wss://10.0.0.5/x",
            "wss://192.168.1.1/x",
            "wss://100.64.0.1/x",
            "wss://[::ffff:10.0.0.5]/x",
            "wss://localhost/x",
            "wss://[2002:0808:0808::1]/x",
            "wss://[2001:2::1]/x",
            "wss://[100::1]/x",
            "wss://[64:ff9b:1::1]/x",
            "wss://[3fff::1]/x",
            "wss://[fec0::1]/x",
            "wss://192.88.99.1/x",
        ] {
            let mut c = cfg("127.0.0.1:1".parse().unwrap());
            c.url = u.into();
            c.allow_loopback_insecure = false;
            c.egress_allow = vec![];
            let mut t =
                WsTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
            assert!(matches!(t.connect().await, Err(TE::Connect(_))), "{u}");
        }
    }

    #[tokio::test]
    async fn control_frame_flood_cannot_hold_connection_open() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        tokio::spawn(async move {
            let (tcp, _) = l.accept().await.unwrap();
            let mut ws = accept_async(tcp).await.unwrap();
            loop {
                if ws.send(Message::Ping(vec![1].into())).await.is_err() {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        });
        let mut c = cfg(addr);
        c.idle_timeout = Duration::from_millis(100);
        c.max_lifetime = Duration::from_millis(300);
        let mut t = WsTransport::new(c, CountingSecrets(Arc::new(AtomicUsize::new(0)))).unwrap();
        t.connect().await.unwrap();
        let start = Instant::now();
        assert!(matches!(t.next_frame().await, Err(TE::Read(_))));
        assert!(start.elapsed() < Duration::from_secs(2));
    }

    fn irc_cfg(addr: std::net::SocketAddr) -> WsConfig {
        let mut c = cfg(addr);
        c.auto_replies = vec![AutoReplyRule {
            prefix: "PING :".into(),
            reply_prefix: "PONG :".into(),
            reply_suffix: "\r\n".into(),
            max_token_len: 64,
            forward_to_guest: false,
        }];
        c.self_echo = Some(SelfEchoFilter {
            self_id: "WaddleBot".into(),
            sender_open: ":".into(),
            sender_close: "!".into(),
            command: "PRIVMSG".into(),
        });
        c
    }

    /// Server: sends `frames`, then reports every client message it gets
    /// until the client closes (or 300ms of silence).
    async fn serve_collect(
        l: TcpListener,
        frames: Vec<&'static str>,
        got: tokio::sync::mpsc::UnboundedSender<String>,
    ) {
        let (tcp, _) = l.accept().await.unwrap();
        let mut ws = accept_async(tcp).await.unwrap();
        for f in frames {
            ws.send(Message::text(f)).await.unwrap();
        }
        while let Ok(Some(Ok(m))) =
            tokio::time::timeout(Duration::from_millis(300), ws.next()).await
        {
            if let Message::Text(t) = m {
                got.send(t.to_string()).unwrap();
            }
        }
    }

    fn secrets() -> CountingSecrets {
        CountingSecrets(Arc::new(AtomicUsize::new(0)))
    }

    fn counter(reg: &prometheus::Registry, outcome: &str) -> u64 {
        reg.gather()
            .iter()
            .flat_map(|f| f.get_metric().iter())
            .filter(|m| m.get_label().iter().any(|l| l.value() == outcome))
            .map(|m| m.get_counter().value() as u64)
            .sum()
    }

    /// Real-path: the server PINGs, the transport answers PONG with the
    /// echoed token, and the connector-facing stream never sees the PING.
    #[tokio::test]
    async fn server_ping_is_ponged_and_hidden_from_the_connector() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve_collect(
            l,
            vec!["PING :tmi.twitch.tv\r\n", "real-frame"],
            tx,
        ));
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        let mut t = WsTransport::new(irc_cfg(addr), secrets())
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        assert_eq!(t.next_frame().await.unwrap().unwrap(), b"real-frame");
        // the reply is flushed at the top of the next read
        let _ = tokio::time::timeout(Duration::from_millis(100), t.next_frame()).await;
        assert_eq!(rx.recv().await.unwrap(), "PONG :tmi.twitch.tv\r\n");
        assert_eq!(counter(&reg, "replied"), 1);
    }

    #[tokio::test]
    async fn forward_to_guest_config_lets_the_connector_see_the_ping() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve_collect(l, vec!["PING :abc\r\n"], tx));
        let mut c = irc_cfg(addr);
        c.auto_replies[0].forward_to_guest = true;
        let mut t = WsTransport::new(c, secrets()).unwrap();
        t.connect().await.unwrap();
        assert_eq!(t.next_frame().await.unwrap().unwrap(), b"PING :abc\r\n");
        let _ = tokio::time::timeout(Duration::from_millis(100), t.next_frame()).await;
        assert_eq!(rx.recv().await.unwrap(), "PONG :abc\r\n");
    }

    /// regression: p6b-crlf -- a hostile token is a loud read error and
    /// NOTHING is written back upstream.
    #[tokio::test]
    async fn hostile_ping_token_is_a_read_error_and_never_reflected() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve_collect(l, vec!["PING :a\rJOIN #evil\r\n"], tx));
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        let mut t = WsTransport::new(irc_cfg(addr), secrets())
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        assert!(matches!(t.next_frame().await, Err(TE::Read(_))));
        t.close("x").await;
        assert!(rx.recv().await.is_none(), "no frame was written back");
        assert_eq!(counter(&reg, "rejected"), 1);
    }

    #[tokio::test]
    async fn own_echo_is_dropped_and_other_users_pass() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve_collect(
            l,
            vec![
                ":waddlebot!waddlebot@waddlebot.tmi.twitch.tv PRIVMSG #c :me\r\n",
                ":alice!alice@alice.tmi.twitch.tv PRIVMSG #c :hello\r\n",
            ],
            tx,
        ));
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        let mut t = WsTransport::new(irc_cfg(addr), secrets())
            .unwrap()
            .with_metrics(m);
        t.connect().await.unwrap();
        assert_eq!(
            t.next_frame().await.unwrap().unwrap(),
            b":alice!alice@alice.tmi.twitch.tv PRIVMSG #c :hello\r\n"
        );
        assert_eq!(counter(&reg, "dropped"), 1);
    }

    #[tokio::test]
    async fn pending_reply_is_dropped_on_reconnect_and_failed_flush_is_loud() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve_collect(l, vec![], tx));
        let reg = prometheus::Registry::new();
        let m = FrameRuleMetrics::register(&reg).unwrap();
        let mut t = WsTransport::new(irc_cfg(addr), secrets())
            .unwrap()
            .with_metrics(m);
        t.pending.push_back(b"PONG :stale\r\n".to_vec());
        t.connect().await.unwrap();
        assert!(
            t.pending.is_empty(),
            "stale reply must not cross connections"
        );
        t.close("x").await;
        t.pending.push_back(b"PONG :x\r\n".to_vec());
        assert!(matches!(t.next_frame().await, Err(TE::Read(_))));
        assert_eq!(counter(&reg, "reply_failed"), 1);
    }

    #[tokio::test]
    async fn heartbeat_is_sent_when_configured_and_loud_otherwise() {
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(serve_collect(l, vec![], tx));
        let mut c = cfg(addr);
        assert_eq!(
            WsTransport::new(c.clone(), secrets())
                .unwrap()
                .heartbeat_interval(),
            None
        );
        c.heartbeat_interval = Some(Duration::from_secs(30));
        let mut t = WsTransport::new(c, secrets()).unwrap();
        assert_eq!(t.heartbeat_interval(), Some(Duration::from_secs(30)));
        t.connect().await.unwrap();
        t.send_heartbeat(b"PING :keepalive\r\n".to_vec())
            .await
            .unwrap();
        assert_eq!(rx.recv().await.unwrap(), "PING :keepalive\r\n");
        assert!(t.send_heartbeat(vec![]).await.is_err());
        assert!(t.send_heartbeat(vec![b'x'; 2048]).await.is_err());
        // the platform-default heartbeat stays fail-loud (never a silent no-op)
        assert!(t.send_default_heartbeat().await.is_err());
        // no interval configured -> a heartbeat write is refused
        let mut off = WsTransport::new(cfg(addr), secrets()).unwrap();
        assert!(off.send_heartbeat(b"x".to_vec()).await.is_err());
    }

    #[test]
    fn invalid_frame_rules_refuse_construction() {
        let a: std::net::SocketAddr = "127.0.0.1:1".parse().unwrap();
        let mut c = irc_cfg(a);
        c.auto_replies[0].reply_suffix = "\r\nJOIN #x".into();
        assert!(matches!(
            WsTransport::new(c, secrets()),
            Err(TE::Connect(_))
        ));
    }
}

//! Generic, platform-agnostic WebSocket [`Transport`]. URL, handshake slots,
//! secret handles, resume token and limits all come from [`WsConfig`] (the
//! overlay's config); nothing here names a platform. The connector-built
//! handshake is sent on every (re)connect with secret slots substituted per
//! call via the injected [`WebhookSecretSource`] (`connection_resolver` in
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
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

use super::egress_guard::resolve_pinned;
use super::transport::{Transport, TransportError};
use super::transport_common::{opaque_id, substitute_slots, validate_endpoint};
use super::types::{ConnectionCtx, Handshake};
use super::webhook::WebhookSecretSource;

/// Largest accepted `dedup_window`.
pub const MAX_DEDUP_WINDOW: usize = 65_536;

type Stream = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;

/// Overlay-supplied configuration for one WebSocket source.
#[derive(Debug, Clone)]
pub struct WsConfig {
    /// Platform slug (must have a pinned connector).
    pub platform: String,
    /// `wss://` endpoint from the overlay config.
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
}

/// WebSocket transport over tokio-tungstenite.
pub struct WsTransport<S: WebhookSecretSource> {
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
}

impl<S: WebhookSecretSource> WsTransport<S> {
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
        })
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
        let payload = substitute_slots(
            &self.secrets,
            &self.cfg.handles,
            &handshake.bytes,
            &handshake.secret_refs,
        )
        .await?;
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

impl<S: WebhookSecretSource + 'static> Transport for WsTransport<S> {
    fn platform(&self) -> &str {
        &self.cfg.platform
    }

    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError> {
        self.stream = None;
        let wsc = WebSocketConfig::default()
            .max_message_size(Some(self.cfg.max_frame_bytes))
            .max_frame_size(Some(self.cfg.max_frame_bytes));
        // Resolve + validate + PIN the address; the client never re-resolves.
        let pinned = resolve_pinned(&self.url, &self.cfg.egress_allow)
            .await
            .map_err(TransportError::Connect)?;
        let req = self
            .url
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
            session_token: self.cfg.resume_token.clone(),
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
                None | Some(Ok(Message::Close(_))) => return Ok(None),
                Some(Err(WsError::ConnectionClosed | WsError::AlreadyClosed)) => return Ok(None),
                Some(Err(e)) => return Err(TransportError::Read(e.to_string())),
                Some(Ok(Message::Text(t))) => t.as_bytes().to_vec(),
                Some(Ok(Message::Binary(b))) => b.to_vec(),
                // Control frames are answered inside tungstenite and do not
                // reset the idle clock (flood resistance).
                Some(Ok(_)) => continue,
            };
            self.last_data = Instant::now();
            if self.is_replay(&bytes) {
                continue;
            }
            return Ok(Some(bytes));
        }
    }

    async fn close(&mut self, _reason: &str) {
        if let Some(mut s) = self.stream.take() {
            let _ = s.close(None).await;
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
    impl WebhookSecretSource for CountingSecrets {
        fn resolve(&self, _r: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Box::pin(async { Ok(SecretString::new("tok123".into())) })
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
}

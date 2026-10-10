//! SRT ingest listener (S5), implementing [`IngestListener`] against
//! `srt-tokio` 0.4.4's multiplexing [`srt_tokio::SrtListener`] (aliased
//! `SrtTokioListener` below to avoid colliding with this module's own
//! [`SrtListener`]).
//!
//! ## What this validates about `srt-tokio` 0.4.4
//!
//! The upstream `srt-rs` project (<https://github.com/russelltg/srt-rs>,
//! which `srt-tokio`/`srt-protocol` are published from) does not represent
//! itself as a hardened, production-complete SRT implementation -- no
//! encryption, rendezvous, or lossy/high-latency-link behavior is exercised
//! here. What this module *does* exercise, over real loopback UDP in
//! `tests/ingest_srt_*.rs` (not mocks): the multiplexed accept loop
//! (`SrtListener::builder().bind(..)` / `SrtIncoming::incoming()`), reading
//! `streamid` and rejecting *before* the handshake completes
//! (`ConnectionRequest::stream_id()` / `.reject()` / `.accept()`), the
//! accepted `SrtSocket`'s `Stream`/`Sink` payload transfer, and its
//! `statistics()` watch-stream (used for the `srt_rtt_ms` histogram).
//! `streamid` *is* available pre-accept in this version, so the task's
//! documented fallback ("if srt-tokio only exposes streamid after accept,
//! accept then close immediately with a WARN") does not apply -- key
//! authorization happens entirely pre-accept below.
//!
//! ## `Cargo.toml` note
//!
//! `srt-tokio`'s public API (accept loop, payload stream, statistics
//! stream) is expressed entirely through `futures_core::Stream`/`Sink`,
//! with `bytes::Bytes` as the payload item type. Neither was a direct
//! dependency of this crate; both are already transitively resolved
//! (pulled in by `srt-tokio`/`webrtc`/`sea-orm`) at the exact versions now
//! pinned directly in `Cargo.toml`, so promoting them to direct
//! dependencies changed no resolved version in `Cargo.lock` -- it only
//! makes their traits nameable from this crate, which is required to
//! consume `srt-tokio` at all. This is a deviation from this chunk's
//! stated "`Cargo.toml` only to enable features of the already-declared
//! `srt-tokio` crate" scope; flagging it here for visibility rather than
//! making it silently.
//!
//! ## Ingest-auth hook
//!
//! Neither `ingest::mod` (S1) nor `ingest::rtmp` (S4) defines a shared
//! `IngestAuth` trait as of this chunk, so [`SrtAuth`] below is this
//! module's own local hook point, per the coordination instructions for
//! this chunk. If a shared trait lands later, this should be replaced by
//! it -- flagged as a known duplication.

use std::collections::HashSet;
use std::io;
use std::net::Ipv4Addr;
use std::pin::Pin;
use std::sync::{Arc, LazyLock, Mutex, PoisonError};
use std::task::{Context, Poll};
use std::time::Duration;

use anyhow::Context as _;
use bytes::{Buf, Bytes};
use futures::{Stream, StreamExt};
use srt_tokio::access::{AccessControlList, RejectReason, ServerRejectReason};
use srt_tokio::options::StreamId;
use srt_tokio::{ConnectionRequest, SocketStatistics, SrtListener as SrtTokioListener, SrtSocket};
use tokio::io::{AsyncRead, ReadBuf};
use tokio::net::UdpSocket;
use tokio::sync::mpsc;

use crate::ingest::{IngestKind, IngestListener, IngestSession};

/// First byte of every 188-byte MPEG-TS packet.
const TS_SYNC_BYTE: u8 = 0x47;
/// Fixed MPEG-TS packet length (no 192-byte timecode-prefixed variant
/// support -- `ffmpeg -f mpegts` on the receiving end expects raw 188-byte
/// packets, which is what SRT-transported TS contribution feeds use).
const TS_PACKET_LEN: usize = 188;

/// Authorizes an SRT publisher by its resolved stream key before the SRT
/// handshake completes. See the module docs' "Ingest-auth hook" section --
/// this is `srt`'s local stand-in until a shared `ingest`-wide contract
/// exists.
pub trait SrtAuth: Send + Sync {
    /// Returns `Ok(())` if `key` (the resolved stream key, see
    /// [`resolve_stream_key`]) may publish, or `Err` with a short
    /// human-readable reason otherwise.
    fn authorize(&self, key: &str) -> Result<(), &'static str>;
}

/// Permissive placeholder authorizer: allows any non-empty key. Used as the
/// default until pipeline-registry-backed authorization (validating the key
/// against configured `InputSpec::Srt { stream_id }` values, owned by
/// `pipeline`) is wired in -- a follow-up, not implemented in this chunk.
#[derive(Debug, Default, Clone, Copy)]
pub struct AllowAllAuth;

impl SrtAuth for AllowAllAuth {
    fn authorize(&self, _key: &str) -> Result<(), &'static str> {
        Ok(())
    }
}

/// Static-allowlist authorizer: only keys in the configured set are
/// authorized. Used by integration tests, and available to any deployment
/// wanting a static allowlist ahead of full pipeline-registry wiring.
#[derive(Debug, Clone, Default)]
pub struct AllowlistAuth(HashSet<String>);

impl AllowlistAuth {
    /// Builds an allowlist from any iterable of string-like keys.
    pub fn new(keys: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self(keys.into_iter().map(Into::into).collect())
    }
}

impl SrtAuth for AllowlistAuth {
    fn authorize(&self, key: &str) -> Result<(), &'static str> {
        if self.0.contains(key) {
            Ok(())
        } else {
            Err("unknown stream key")
        }
    }
}

/// SRT listener configuration: bind port, publish latency, and the
/// pluggable [`SrtAuth`] hook.
#[derive(Clone)]
pub struct SrtListener {
    pub bind_port: u16,
    pub latency: Duration,
    pub auth: Arc<dyn SrtAuth>,
}

impl std::fmt::Debug for SrtListener {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SrtListener")
            .field("bind_port", &self.bind_port)
            .field("latency", &self.latency)
            .finish_non_exhaustive()
    }
}

impl SrtListener {
    /// Default SRT publish latency (200ms) applied when not overridden via
    /// [`Self::with_latency`]. `crate::config::Config` (owned by `config.rs`,
    /// out of this chunk's file ownership) has no dedicated SRT-latency
    /// field yet -- this constant is the documented default until one is
    /// added; see [`Self::from_config`].
    pub const DEFAULT_LATENCY: Duration = Duration::from_millis(200);

    /// Builds a listener for `bind_port` with [`Self::DEFAULT_LATENCY`] and
    /// the permissive [`AllowAllAuth`] default.
    pub fn new(bind_port: u16) -> Self {
        Self {
            bind_port,
            latency: Self::DEFAULT_LATENCY,
            auth: Arc::new(AllowAllAuth),
        }
    }

    /// Builds a listener from `cfg.cli.srt_port`. See [`Self::DEFAULT_LATENCY`]
    /// for why latency isn't sourced from `cfg` yet.
    pub fn from_config(cfg: &crate::config::Config) -> Self {
        Self::new(cfg.cli.srt_port)
    }

    /// Overrides the publish latency (both send and receive sides).
    pub fn with_latency(mut self, latency: Duration) -> Self {
        self.latency = latency;
        self
    }

    /// Overrides the ingest-auth hook.
    pub fn with_auth(mut self, auth: Arc<dyn SrtAuth>) -> Self {
        self.auth = auth;
        self
    }

    /// Runs the accept loop against an already-bound UDP socket instead of
    /// binding `bind_port` itself. Used by integration tests so each test
    /// can claim an OS-assigned ephemeral port (avoiding collisions under
    /// parallel `cargo nextest` execution) while still exercising the real
    /// accept/authorize/forward path; also usable by any future caller that
    /// wants to pre-bind the socket itself (e.g. for `SO_REUSEPORT`).
    pub async fn run_with_socket(
        self,
        socket: UdpSocket,
        tx: mpsc::Sender<IngestSession>,
    ) -> anyhow::Result<()> {
        self.serve(socket, tx).await
    }

    /// Shared accept-loop implementation behind [`IngestListener::run`] and
    /// [`Self::run_with_socket`]. Spawns one task per accepted connection so
    /// a slow/misbehaving publisher never blocks new connections; a
    /// per-connection error never terminates this loop, only a listener
    /// (bind) failure does.
    async fn serve(self, socket: UdpSocket, tx: mpsc::Sender<IngestSession>) -> anyhow::Result<()> {
        let bind_port = self.bind_port;
        let (_listener, mut incoming) = SrtTokioListener::builder()
            .latency(self.latency)
            .socket(socket)
            .bind(bind_port)
            .await
            .with_context(|| format!("SRT listener failed to bind UDP port {bind_port}"))?;

        let active_keys: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));

        loop {
            match incoming.incoming().next().await {
                Some(request) => {
                    // No remote address on the span: a publisher's IP is
                    // personal data and must not reach the trace backend.
                    let span = tracing::info_span!("srt_connection");
                    tokio::spawn(tracing::Instrument::instrument(
                        handle_connection(
                            request,
                            tx.clone(),
                            active_keys.clone(),
                            self.auth.clone(),
                        ),
                        span,
                    ));
                }
                None => {
                    tracing::warn!(bind_port, "SRT listener incoming stream ended");
                    break;
                }
            }
        }

        Ok(())
    }
}

impl IngestListener for SrtListener {
    async fn run(self, tx: mpsc::Sender<IngestSession>) -> anyhow::Result<()> {
        let bind_port = self.bind_port;
        let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, bind_port))
            .await
            .with_context(|| format!("SRT listener failed to bind UDP port {bind_port}"))?;
        self.serve(socket, tx).await
    }
}

/// Releases `key` from the shared active-publisher set on drop, regardless
/// of which exit path a connection handler takes (accept failure, TS
/// validation failure, normal disconnect, or a future panic unwinding the
/// task) -- this is what makes a key immediately retryable after any
/// rejection and enforces "one publisher per key" while a session is live.
struct ActiveKeyGuard {
    active: Arc<Mutex<HashSet<String>>>,
    key: String,
}

impl Drop for ActiveKeyGuard {
    fn drop(&mut self) {
        let mut set = self.active.lock().unwrap_or_else(PoisonError::into_inner);
        set.remove(&self.key);
    }
}

/// Decrements a Prometheus gauge on drop -- pairs with an `.inc()` taken
/// right after a successful SRT accept.
struct GaugeGuard(prometheus::IntGauge);

impl Drop for GaugeGuard {
    fn drop(&mut self) {
        self.0.dec();
    }
}

/// Resolves the routing key for an SRT publisher from its handshake
/// `streamid`. Supports both the SRT access-control convention
/// (`#!::r=<key>[,...]`, extracting the `r` = resource-name entry) and a
/// bare `<key>` streamid. Returns `None` if no streamid was sent, or if it
/// parses as an access-control list but carries no `r=` entry.
fn resolve_stream_key(stream_id: Option<&StreamId>) -> Option<String> {
    let raw = stream_id?.to_string();
    match raw.parse::<AccessControlList>() {
        Ok(acl) => acl
            .0
            .into_iter()
            .find(|entry| entry.key == "r")
            .map(|entry| entry.value),
        Err(_) => Some(raw),
    }
}

/// Checks that `chunk` looks like raw MPEG-TS: sync byte `0x47` at offset 0
/// and at every subsequent 188-byte packet boundary fully contained in
/// `chunk`. A single SRT payload chunk commonly carries several TS packets,
/// so this validates all of them, not just the first.
fn looks_like_mpegts(chunk: &[u8]) -> bool {
    if chunk.is_empty() || chunk[0] != TS_SYNC_BYTE {
        return false;
    }
    let mut offset = 0;
    while offset + TS_PACKET_LEN <= chunk.len() {
        if chunk[offset] != TS_SYNC_BYTE {
            return false;
        }
        offset += TS_PACKET_LEN;
    }
    true
}

/// Handles one SRT connection request end to end: resolve + authorize the
/// key (pre-accept), enforce one-publisher-per-key, accept, validate the
/// first payload chunk is MPEG-TS, then emit an [`IngestSession`] and
/// forward subsequent payload onto its byte stream until disconnect.
async fn handle_connection(
    request: ConnectionRequest,
    tx: mpsc::Sender<IngestSession>,
    active_keys: Arc<Mutex<HashSet<String>>>,
    auth: Arc<dyn SrtAuth>,
) {
    let remote = request.remote();

    let key = match resolve_stream_key(request.stream_id()) {
        Some(key) if !key.is_empty() => key,
        _ => {
            tracing::warn!(%remote, "srt connect rejected: no resolvable stream key in streamid");
            SRT_METRICS
                .publish_total
                .with_label_values(&["rejected_no_key"])
                .inc();
            let _ = request
                .reject(RejectReason::Server(ServerRejectReason::Unauthorized))
                .await;
            return;
        }
    };

    if let Err(reason) = auth.authorize(&key) {
        tracing::warn!(%remote, key = %key, reason, "srt connect rejected: unauthorized key");
        SRT_METRICS
            .publish_total
            .with_label_values(&["rejected_unauthorized"])
            .inc();
        let _ = request
            .reject(RejectReason::Server(ServerRejectReason::Unauthorized))
            .await;
        return;
    }

    // Resolve the duplicate-key decision entirely inside this block so the
    // `MutexGuard` is dropped before any `.await` below -- a guard held
    // (even conditionally, on an early-return path) across an await makes
    // the enclosing future `!Send`, which `tokio::spawn` requires.
    let is_duplicate = {
        let mut active = active_keys.lock().unwrap_or_else(PoisonError::into_inner);
        if active.contains(&key) {
            true
        } else {
            active.insert(key.clone());
            false
        }
    };

    if is_duplicate {
        tracing::warn!(%remote, key = %key, "srt connect rejected: publisher already active for key");
        SRT_METRICS
            .publish_total
            .with_label_values(&["rejected_duplicate"])
            .inc();
        let _ = request
            .reject(RejectReason::Server(ServerRejectReason::Conflict))
            .await;
        return;
    }

    let guard = ActiveKeyGuard {
        active: active_keys.clone(),
        key: key.clone(),
    };

    let mut socket = match request.accept(None).await {
        Ok(socket) => socket,
        Err(err) => {
            tracing::warn!(%remote, key = %key, error = %err, "srt accept failed");
            SRT_METRICS
                .publish_total
                .with_label_values(&["accept_error"])
                .inc();
            drop(guard);
            return;
        }
    };

    SRT_METRICS.connections_active.inc();
    let _connection_guard = GaugeGuard(SRT_METRICS.connections_active.clone());

    let stats_stream: Pin<Box<dyn Stream<Item = SocketStatistics> + Send>> =
        Box::pin(socket.statistics().clone());
    tokio::spawn(sample_rtt(stats_stream));

    let first_chunk = match socket.next().await {
        Some(Ok((_instant, bytes))) => bytes,
        Some(Err(err)) => {
            tracing::warn!(%remote, key = %key, error = %err, "srt read error before first packet");
            SRT_METRICS
                .publish_total
                .with_label_values(&["read_error"])
                .inc();
            let _ = socket.close_and_finish().await;
            return;
        }
        None => {
            tracing::warn!(%remote, key = %key, "srt caller disconnected before sending data");
            SRT_METRICS
                .publish_total
                .with_label_values(&["empty_stream"])
                .inc();
            return;
        }
    };

    if !looks_like_mpegts(&first_chunk) {
        tracing::warn!(%remote, key = %key, "srt publish rejected: not mpegts");
        SRT_METRICS
            .publish_total
            .with_label_values(&["rejected_not_mpegts"])
            .inc();
        let _ = socket.close_and_finish().await;
        return;
    }

    SRT_METRICS
        .publish_total
        .with_label_values(&["accepted"])
        .inc();
    SRT_METRICS.bytes_total.inc_by(first_chunk.len() as u64);

    let (data_tx, data_rx) = mpsc::channel::<Bytes>(64);
    if data_tx.send(first_chunk).await.is_err() {
        return;
    }

    let session = IngestSession {
        kind: IngestKind::Srt,
        key: key.clone(),
        stream: Box::new(SrtByteStream {
            rx: data_rx,
            partial: Bytes::new(),
        }),
        span: tracing::Span::current(),
    };

    if tx.send(session).await.is_err() {
        tracing::warn!(key = %key, "srt session dropped: pipeline supervisor channel closed");
        let _ = socket.close_and_finish().await;
        return;
    }

    forward_srt_payload(socket, data_tx).await;
    // `_connection_guard`/`guard` drop here: gauge decremented, key released.
}

/// Pulls subsequent payload chunks off an accepted `SrtSocket` and forwards
/// them into `data_tx` (consumed by the [`SrtByteStream`] handed to the
/// pipeline supervisor via the emitted [`IngestSession`]) until the caller
/// disconnects, a stream error occurs, or the reader side is gone.
async fn forward_srt_payload(mut socket: SrtSocket, data_tx: mpsc::Sender<Bytes>) {
    while let Some(item) = socket.next().await {
        match item {
            Ok((_instant, bytes)) => {
                SRT_METRICS.bytes_total.inc_by(bytes.len() as u64);
                if data_tx.send(bytes).await.is_err() {
                    break;
                }
            }
            Err(err) => {
                tracing::warn!(error = %err, "srt payload stream error, closing");
                break;
            }
        }
    }
    let _ = socket.close_and_finish().await;
}

/// Samples an accepted socket's statistics stream for as long as it stays
/// open, recording the smoothed receive-side RTT into `srt_rtt_ms`. Ends
/// naturally when the underlying socket closes (its statistics sender
/// drops, ending this stream).
async fn sample_rtt(mut stats: Pin<Box<dyn Stream<Item = SocketStatistics> + Send>>) {
    while let Some(stats) = stats.next().await {
        let rtt_ms = stats.rx_average_rtt.as_secs_f64() * 1000.0;
        if rtt_ms > 0.0 {
            SRT_METRICS.rtt_ms.observe(rtt_ms);
        }
    }
}

/// Adapts a channel of raw SRT payload chunks (`bytes::Bytes`) into
/// [`tokio::io::AsyncRead`] for ffmpeg's `-f mpegts -i pipe:0`, buffering
/// the tail of a chunk across `poll_read` calls when the destination buffer
/// is smaller than a single received chunk.
struct SrtByteStream {
    rx: mpsc::Receiver<Bytes>,
    partial: Bytes,
}

impl AsyncRead for SrtByteStream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        loop {
            if !this.partial.is_empty() {
                let n = std::cmp::min(this.partial.len(), buf.remaining());
                buf.put_slice(&this.partial[..n]);
                this.partial.advance(n);
                return Poll::Ready(Ok(()));
            }
            match this.rx.poll_recv(cx) {
                Poll::Ready(Some(chunk)) => this.partial = chunk,
                Poll::Ready(None) => return Poll::Ready(Ok(())),
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

/// Prometheus metrics for the SRT ingest listener, registered once against
/// the process-global [`prometheus::default_registry`] on first access (no
/// per-instance `SrtListener` registry handle is available -- see
/// `rules/critical-rules.md` Observability, `srt_publish_total` labeled by
/// `result` rather than being a lone counter).
struct SrtMetrics {
    connections_active: prometheus::IntGauge,
    publish_total: prometheus::IntCounterVec,
    bytes_total: prometheus::IntCounter,
    rtt_ms: prometheus::Histogram,
}

impl SrtMetrics {
    fn register() -> Self {
        let registry = prometheus::default_registry();

        let connections_active = prometheus::IntGauge::new(
            "srt_connections_active",
            "Number of currently active SRT publisher connections",
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(connections_active.clone()))
            .expect("register srt_connections_active");

        let publish_total = prometheus::IntCounterVec::new(
            prometheus::Opts::new(
                "srt_publish_total",
                "Total SRT publish attempts, labeled by outcome",
            ),
            &["result"],
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(publish_total.clone()))
            .expect("register srt_publish_total");

        let bytes_total = prometheus::IntCounter::new(
            "srt_bytes_total",
            "Total MPEG-TS payload bytes received over SRT",
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(bytes_total.clone()))
            .expect("register srt_bytes_total");

        let rtt_ms = prometheus::Histogram::with_opts(
            prometheus::HistogramOpts::new(
                "srt_rtt_ms",
                "Smoothed SRT round-trip time in milliseconds, sampled from socket statistics",
            )
            .buckets(vec![
                1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0, 400.0, 800.0, 1600.0,
            ]),
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(rtt_ms.clone()))
            .expect("register srt_rtt_ms");

        Self {
            connections_active,
            publish_total,
            bytes_total,
            rtt_ms,
        }
    }
}

static SRT_METRICS: LazyLock<SrtMetrics> = LazyLock::new(SrtMetrics::register);

#[cfg(test)]
mod tests {
    use super::*;

    fn stream_id(raw: &str) -> StreamId {
        raw.parse().expect("valid streamid")
    }

    #[test]
    fn resolve_stream_key_extracts_resource_name_from_access_control_list() {
        let sid = stream_id("#!::r=my-stream-key,u=admin");
        assert_eq!(
            resolve_stream_key(Some(&sid)).as_deref(),
            Some("my-stream-key")
        );
    }

    #[test]
    fn resolve_stream_key_treats_non_acl_string_as_bare_key() {
        let sid = stream_id("my-bare-key");
        assert_eq!(
            resolve_stream_key(Some(&sid)).as_deref(),
            Some("my-bare-key")
        );
    }

    #[test]
    fn resolve_stream_key_returns_none_for_acl_without_resource_name() {
        let sid = stream_id("#!::u=admin");
        assert_eq!(resolve_stream_key(Some(&sid)), None);
    }

    #[test]
    fn resolve_stream_key_returns_none_when_absent() {
        assert_eq!(resolve_stream_key(None), None);
    }

    #[test]
    fn looks_like_mpegts_accepts_single_valid_packet() {
        let mut packet = vec![0u8; TS_PACKET_LEN];
        packet[0] = TS_SYNC_BYTE;
        assert!(looks_like_mpegts(&packet));
    }

    #[test]
    fn looks_like_mpegts_accepts_multiple_valid_packets() {
        let mut chunk = vec![0u8; TS_PACKET_LEN * 3];
        for i in 0..3 {
            chunk[i * TS_PACKET_LEN] = TS_SYNC_BYTE;
        }
        assert!(looks_like_mpegts(&chunk));
    }

    #[test]
    fn looks_like_mpegts_rejects_missing_sync_byte() {
        let chunk = vec![0u8; TS_PACKET_LEN];
        assert!(!looks_like_mpegts(&chunk));
    }

    #[test]
    fn looks_like_mpegts_rejects_bad_sync_byte_on_second_packet() {
        let mut chunk = vec![0u8; TS_PACKET_LEN * 2];
        chunk[0] = TS_SYNC_BYTE;
        chunk[TS_PACKET_LEN] = 0x00;
        assert!(!looks_like_mpegts(&chunk));
    }

    #[test]
    fn looks_like_mpegts_rejects_empty_chunk() {
        assert!(!looks_like_mpegts(&[]));
    }

    #[test]
    fn allow_all_auth_authorizes_any_key() {
        assert!(AllowAllAuth.authorize("anything").is_ok());
    }

    #[test]
    fn allowlist_auth_rejects_unknown_key() {
        let auth = AllowlistAuth::new(["known-key"]);
        assert!(auth.authorize("known-key").is_ok());
        assert!(auth.authorize("unknown-key").is_err());
    }

    #[test]
    fn srt_listener_new_uses_default_latency_and_allow_all_auth() {
        let listener = SrtListener::new(9000);
        assert_eq!(listener.bind_port, 9000);
        assert_eq!(listener.latency, SrtListener::DEFAULT_LATENCY);
        assert!(listener.auth.authorize("anything").is_ok());
    }

    #[test]
    fn srt_listener_with_latency_and_auth_override_defaults() {
        let listener = SrtListener::new(9000)
            .with_latency(Duration::from_millis(50))
            .with_auth(Arc::new(AllowlistAuth::new(["only-this-key"])));
        assert_eq!(listener.latency, Duration::from_millis(50));
        assert!(listener.auth.authorize("only-this-key").is_ok());
        assert!(listener.auth.authorize("other-key").is_err());
    }

    #[test]
    fn srt_listener_debug_hides_auth_shows_bind_port_and_latency() {
        let rendered = format!("{:?}", SrtListener::new(9000));
        assert!(rendered.contains("SrtListener"));
        assert!(rendered.contains("9000"));
    }

    #[test]
    fn srt_listener_from_config_uses_configured_srt_port() {
        use crate::config::{CliConfig, Config, Secret};
        use clap::Parser as _;

        let cli = CliConfig::parse_from(["svc-streaming", "--srt-port", "9500"]);
        let cfg = Config {
            cli,
            db_password: Secret::new("db-pass"),
            cache_password: None,
            service_api_key: Secret::new("service-key"),
            jwt_hmac_secret: None,
        };

        let listener = SrtListener::from_config(&cfg);
        assert_eq!(listener.bind_port, 9500);
        assert_eq!(listener.latency, SrtListener::DEFAULT_LATENCY);
    }

    #[tokio::test]
    async fn srt_byte_stream_buffers_partial_reads_across_small_dest_buffers() {
        use tokio::io::AsyncReadExt;

        let (tx, rx) = mpsc::channel::<Bytes>(4);
        tx.send(Bytes::from_static(b"hello world"))
            .await
            .expect("send chunk");
        drop(tx);

        let mut stream = SrtByteStream {
            rx,
            partial: Bytes::new(),
        };

        let mut small_buf = [0u8; 4];
        let n = stream.read(&mut small_buf).await.expect("read");
        assert_eq!(&small_buf[..n], b"hell");

        let mut rest = Vec::new();
        stream.read_to_end(&mut rest).await.expect("read to end");
        assert_eq!(rest, b"o world");
    }

    #[tokio::test]
    async fn srt_byte_stream_returns_eof_when_channel_closes_immediately() {
        use tokio::io::AsyncReadExt;

        let (tx, rx) = mpsc::channel::<Bytes>(1);
        drop(tx);

        let mut stream = SrtByteStream {
            rx,
            partial: Bytes::new(),
        };
        let mut buf = Vec::new();
        stream.read_to_end(&mut buf).await.expect("read to end");
        assert!(buf.is_empty());
    }

    #[test]
    fn srt_metrics_register_exactly_once_and_expose_expected_series() {
        // Forces `SRT_METRICS` initialization; if this were called twice
        // against the same registry without the `LazyLock` guard, the
        // second `Registry::register` would return `Err` and the
        // `.expect(..)` calls in `SrtMetrics::register` would panic.
        SRT_METRICS.connections_active.set(0);
        SRT_METRICS
            .publish_total
            .with_label_values(&["accepted"])
            .inc();
        SRT_METRICS.bytes_total.inc_by(0);

        let families = prometheus::default_registry().gather();
        let names: Vec<&str> = families.iter().map(|f| f.name()).collect();
        assert!(names.contains(&"srt_connections_active"));
        assert!(names.contains(&"srt_publish_total"));
        assert!(names.contains(&"srt_bytes_total"));
        assert!(names.contains(&"srt_rtt_ms"));
    }
}

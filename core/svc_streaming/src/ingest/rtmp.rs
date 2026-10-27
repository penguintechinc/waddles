//! RTMP push ingest listener (S4). Implements [`IngestListener`] on top of
//! `rml_rtmp`'s high-level `ServerSession`: one Tokio task per accepted TCP
//! connection drives the handshake, then the connect/publish handshake, and
//! finally remuxes incoming RTMP audio/video/metadata messages directly
//! into an FLV byte stream (`"FLV"` header + tags) that a downstream
//! `ffmpeg -f flv -i pipe:0` consumer reads via the emitted
//! [`IngestSession::stream`].
//!
//! FLV assembly: RTMP `AudioData`/`VideoData` message payloads use the
//! *exact same* on-wire format as an FLV audio/video tag body (this is why
//! RTMP-to-FLV remuxing is just re-wrapping, not transcoding) -- each
//! payload is wrapped in an 11-byte FLV tag header
//! (type/size/timestamp/stream-id) plus the trailing 4-byte
//! `PreviousTagSize`, with no bytes rewritten. `onMetaData` is re-encoded
//! as an AMF0 script-data tag (type 18) via `rml_amf0` (re-exported as
//! `rml_rtmp::rml_amf0`), since `ServerSessionEvent::StreamMetadataChanged`
//! hands back a parsed [`StreamMetadata`] rather than raw AMF0 bytes.
//!
//! Ingest-auth: [`IngestAuth`]/[`AuthDecision`] are declared *in this file*
//! rather than `crate::ingest` (mod.rs) -- this chunk's ownership boundary
//! is `src/ingest/rtmp.rs` only, and mod.rs had no prior declaration for
//! this chunk to reuse. The trait uses a hand-written boxed-future method
//! (not the `async-trait` macro, not `IngestListener`'s RPITIT style) so
//! `Arc<dyn IngestAuth>` stays object-safe using only the already-declared
//! `rml_rtmp`/`tokio` dependencies. S2's HTTP-backed implementation (and
//! any pipeline-supervisor code matching on `AuthDecision`) should import
//! it from `crate::ingest::rtmp::{IngestAuth, AuthDecision}`.

use std::collections::{HashSet, VecDeque};
use std::future::Future;
use std::net::{IpAddr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, Mutex as StdMutex};
use std::task::{Context, Poll};
use std::time::Duration;

use anyhow::Context as _;
use rml_rtmp::handshake::{Handshake, HandshakeProcessResult, PeerType};
use rml_rtmp::rml_amf0::Amf0Value;
use rml_rtmp::sessions::{
    ServerSession, ServerSessionConfig, ServerSessionEvent, ServerSessionResult, StreamMetadata,
};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt, ReadBuf};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tracing::Instrument;

use crate::ingest::{IngestKind, IngestListener, IngestSession};
use crate::redact::fingerprint;

/// FLV/RTMP tag type ids (shared vocabulary between the two formats).
const FLV_TAG_AUDIO: u8 = 8;
const FLV_TAG_VIDEO: u8 = 9;
const FLV_TAG_SCRIPT: u8 = 18;

/// How long a `send` onto the per-session FLV byte channel is allowed to
/// block before the connection is dropped as stalled.
const BACKPRESSURE_TIMEOUT: Duration = Duration::from_secs(5);
/// Bounded channel depth for the FLV byte stream handed to the pipeline --
/// bursts beyond this before the consumer catches up start counting toward
/// [`BACKPRESSURE_TIMEOUT`].
const FLV_CHANNEL_CAPACITY: usize = 256;
/// RTMP server-side keep-alive ping cadence.
const PING_INTERVAL: Duration = Duration::from_secs(30);
/// Read buffer size for the post-handshake RTMP chunk stream.
const READ_BUF_SIZE: usize = 8192;

/// Successful ingest-auth decision returned by [`IngestAuth::authorize`]:
/// which pipeline the stream key/id maps to. Pipeline routing itself still
/// keys off the ingest `key` carried on [`IngestSession`] -- these
/// identifiers are for authorization + tracing/log context, not routing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthDecision {
    pub community_id: String,
    pub config_id: String,
}

/// Ingest-auth hook invoked on every RTMP publish request, injected into
/// [`RtmpListener::bind`] at construction. S2 owns the HTTP-backed
/// production implementation; this module never talks to the control plane
/// directly. See the module docs for why this trait lives here instead of
/// `crate::ingest`.
pub trait IngestAuth: Send + Sync {
    /// Authorizes a publish attempt for `key` on the given ingest `kind`.
    /// `Err` (including an unknown/unrecognized key) results in a clean
    /// RTMP-level rejection, never a dropped connection with no reason.
    fn authorize<'a>(
        &'a self,
        kind: IngestKind,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = anyhow::Result<AuthDecision>> + Send + 'a>>;
}

/// Prometheus metrics owned by this module, registered once per
/// [`RtmpListener::bind`] call.
#[derive(Clone)]
struct RtmpMetrics {
    connections_active: prometheus::IntGauge,
    publish_total: prometheus::IntCounterVec,
    bytes_total: prometheus::IntCounter,
}

impl RtmpMetrics {
    fn register(registry: &prometheus::Registry) -> anyhow::Result<Self> {
        let connections_active = prometheus::IntGauge::new(
            "rtmp_connections_active",
            "Number of currently open RTMP ingest TCP connections",
        )?;
        registry.register(Box::new(connections_active.clone()))?;

        let publish_total = prometheus::IntCounterVec::new(
            prometheus::Opts::new(
                "rtmp_publish_total",
                "Total RTMP publish requests handled, labeled by result",
            ),
            &["result"],
        )?;
        registry.register(Box::new(publish_total.clone()))?;

        let bytes_total = prometheus::IntCounter::new(
            "rtmp_bytes_total",
            "Total bytes of RTMP audio/video/metadata payload forwarded as FLV tags",
        )?;
        registry.register(Box::new(bytes_total.clone()))?;

        Ok(Self {
            connections_active,
            publish_total,
            bytes_total,
        })
    }
}

/// RTMP push ingest listener (`rml_rtmp`-backed [`IngestListener`]).
///
/// Binds eagerly in [`RtmpListener::bind`] rather than inside
/// [`IngestListener::run`] -- `run` takes `self` by value specifically so
/// an implementation "can hold owned listener state (bound socket,
/// config)" per that trait's docs -- so callers (and tests using an
/// OS-assigned ephemeral port) can read back the real bound address before
/// the accept loop starts.
pub struct RtmpListener {
    listener: TcpListener,
    auth: Arc<dyn IngestAuth>,
    metrics: RtmpMetrics,
}

impl std::fmt::Debug for RtmpListener {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RtmpListener")
            .field("local_addr", &self.listener.local_addr().ok())
            .finish_non_exhaustive()
    }
}

impl RtmpListener {
    /// Binds the RTMP TCP listener on `bind_addr:bind_port` and registers
    /// this module's Prometheus metrics against `registry`. `auth` is
    /// invoked on every publish request (see [`IngestAuth`]).
    pub async fn bind(
        bind_addr: IpAddr,
        bind_port: u16,
        auth: Arc<dyn IngestAuth>,
        registry: &prometheus::Registry,
    ) -> anyhow::Result<Self> {
        let listener = TcpListener::bind((bind_addr, bind_port))
            .await
            .with_context(|| format!("failed to bind rtmp listener on {bind_addr}:{bind_port}"))?;
        let metrics =
            RtmpMetrics::register(registry).context("failed to register rtmp ingest metrics")?;
        Ok(Self {
            listener,
            auth,
            metrics,
        })
    }

    /// The actually-bound local address. Useful in tests/callers that bind
    /// port `0` for an OS-assigned ephemeral port.
    pub fn local_addr(&self) -> std::io::Result<SocketAddr> {
        self.listener.local_addr()
    }
}

impl IngestListener for RtmpListener {
    async fn run(self, tx: mpsc::Sender<IngestSession>) -> anyhow::Result<()> {
        let local_addr = self.listener.local_addr().ok();
        tracing::info!(?local_addr, "rtmp listener accepting connections");

        let active_keys: Arc<StdMutex<HashSet<String>>> = Arc::new(StdMutex::new(HashSet::new()));

        loop {
            tokio::select! {
                accept_result = self.listener.accept() => {
                    match accept_result {
                        Ok((socket, peer_addr)) => {
                            let auth = self.auth.clone();
                            let active_keys = active_keys.clone();
                            let session_tx = tx.clone();
                            let metrics = self.metrics.clone();
                            // No peer address on the span: a publisher's IP is
                            // personal data and must not reach the trace backend
                            // (it stays in the local, sanitized log lines).
                            let span = tracing::info_span!("rtmp_connection");
                            tokio::spawn(
                                handle_connection(socket, peer_addr, auth, active_keys, session_tx, metrics)
                                    .instrument(span),
                            );
                        }
                        Err(err) => {
                            // A per-accept error is not fatal to the listener -- log and keep
                            // serving, per `IngestListener::run`'s contract.
                            tracing::warn!(error = %err, "rtmp accept error");
                        }
                    }
                }
                _ = tx.closed() => {
                    tracing::info!("rtmp listener shutting down: ingest channel closed");
                    return Ok(());
                }
            }
        }
    }
}

/// Per-connection state threaded through handshake/event processing.
struct ConnCtx {
    peer_addr: SocketAddr,
    auth: Arc<dyn IngestAuth>,
    active_keys: Arc<StdMutex<HashSet<String>>>,
    session_tx: mpsc::Sender<IngestSession>,
    metrics: RtmpMetrics,
    published: Option<PublishedStream>,
    should_close: bool,
}

/// The currently-active publish on this connection, if any (RTMP
/// connections publish at most one stream key at a time in this
/// implementation).
struct PublishedStream {
    key: String,
    flv_tx: mpsc::Sender<Vec<u8>>,
}

async fn handle_connection(
    mut socket: TcpStream,
    peer_addr: SocketAddr,
    auth: Arc<dyn IngestAuth>,
    active_keys: Arc<StdMutex<HashSet<String>>>,
    session_tx: mpsc::Sender<IngestSession>,
    metrics: RtmpMetrics,
) {
    metrics.connections_active.inc();

    let mut ctx = ConnCtx {
        peer_addr,
        auth,
        active_keys,
        session_tx,
        metrics: metrics.clone(),
        published: None,
        should_close: false,
    };

    if let Err(err) = run_connection(&mut socket, &mut ctx).await {
        tracing::warn!(error = %err, "rtmp connection ended with error");
    }

    // Disconnect cleanup: release the claimed stream key (if any) and drop
    // the FLV channel sender, which closes the `IngestSession::stream` the
    // pipeline supervisor is reading from (EOF -> pipeline stops, S3).
    if let Some(published) = ctx.published.take() {
        release_key(&ctx.active_keys, &published.key);
        tracing::info!(key_hash = %fingerprint(&published.key), "rtmp connection closed, stream ended");
    }

    metrics.connections_active.dec();
}

async fn run_connection(socket: &mut TcpStream, ctx: &mut ConnCtx) -> anyhow::Result<()> {
    let leftover = perform_handshake(socket).await?;

    let (mut session, initial_results) = ServerSession::new(ServerSessionConfig::new())?;
    if let Signal::Close = drain_results(socket, &mut session, initial_results, ctx).await? {
        return Ok(());
    }

    if !leftover.is_empty() {
        let results = session.handle_input(&leftover)?;
        if let Signal::Close = drain_results(socket, &mut session, results, ctx).await? {
            return Ok(());
        }
    }

    let mut read_buf = vec![0u8; READ_BUF_SIZE];
    let mut ping_interval = tokio::time::interval(PING_INTERVAL);
    ping_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    ping_interval.tick().await; // first tick fires immediately; consume it

    loop {
        tokio::select! {
            read_result = socket.read(&mut read_buf) => {
                let n = read_result?;
                if n == 0 {
                    return Ok(());
                }
                let results = session.handle_input(&read_buf[..n])?;
                if let Signal::Close = drain_results(socket, &mut session, results, ctx).await? {
                    return Ok(());
                }
            }
            _ = ping_interval.tick() => {
                let (packet, _epoch) = session.send_ping_request()?;
                socket.write_all(&packet.bytes).await?;
            }
        }
    }
}

/// Performs the RTMP handshake as the server side, writing responses as
/// they're generated. Returns any already-received post-handshake bytes
/// (`remaining_bytes`) that must be fed into the freshly-created
/// [`ServerSession`] before reading more from the socket.
async fn perform_handshake(socket: &mut TcpStream) -> anyhow::Result<Vec<u8>> {
    let mut handshake = Handshake::new(PeerType::Server);
    let mut buf = [0u8; 4096];
    loop {
        let n = socket.read(&mut buf).await?;
        if n == 0 {
            anyhow::bail!("peer closed connection during rtmp handshake");
        }
        match handshake.process_bytes(&buf[..n])? {
            HandshakeProcessResult::InProgress { response_bytes } => {
                if !response_bytes.is_empty() {
                    socket.write_all(&response_bytes).await?;
                }
            }
            HandshakeProcessResult::Completed {
                response_bytes,
                remaining_bytes,
            } => {
                if !response_bytes.is_empty() {
                    socket.write_all(&response_bytes).await?;
                }
                return Ok(remaining_bytes);
            }
        }
    }
}

enum Signal {
    Continue,
    Close,
}

/// Drains a batch of [`ServerSessionResult`]s: writes outbound packets to
/// the socket and reacts to raised events, which may themselves enqueue
/// further results (e.g. `accept_request`'s response packets).
async fn drain_results(
    socket: &mut TcpStream,
    session: &mut ServerSession,
    results: Vec<ServerSessionResult>,
    ctx: &mut ConnCtx,
) -> anyhow::Result<Signal> {
    let mut queue: VecDeque<ServerSessionResult> = results.into();
    while let Some(result) = queue.pop_front() {
        match result {
            ServerSessionResult::OutboundResponse(packet) => {
                socket.write_all(&packet.bytes).await?;
            }
            ServerSessionResult::UnhandleableMessageReceived(_) => {
                tracing::debug!(peer = %ctx.peer_addr, "rtmp message could not be handled, ignoring");
            }
            ServerSessionResult::RaisedEvent(event) => {
                let more = handle_event(session, event, ctx).await?;
                for result in more {
                    queue.push_back(result);
                }
                if ctx.should_close {
                    return Ok(Signal::Close);
                }
            }
        }
    }
    Ok(Signal::Continue)
}

async fn handle_event(
    session: &mut ServerSession,
    event: ServerSessionEvent,
    ctx: &mut ConnCtx,
) -> anyhow::Result<Vec<ServerSessionResult>> {
    match event {
        ServerSessionEvent::ConnectionRequested {
            request_id,
            app_name,
        } => {
            tracing::debug!(peer = %ctx.peer_addr, app = %loggable_app(&app_name), "rtmp connect requested");
            Ok(session.accept_request(request_id)?)
        }
        ServerSessionEvent::PublishStreamRequested {
            request_id,
            app_name,
            stream_key,
            ..
        } => handle_publish_requested(session, request_id, app_name, stream_key, ctx).await,
        ServerSessionEvent::PublishStreamFinished { stream_key, .. } => {
            handle_publish_finished(stream_key, ctx);
            Ok(Vec::new())
        }
        ServerSessionEvent::StreamMetadataChanged { metadata, .. } => {
            forward_metadata(&metadata, ctx).await;
            Ok(Vec::new())
        }
        ServerSessionEvent::AudioDataReceived {
            data, timestamp, ..
        } => {
            forward_media(FLV_TAG_AUDIO, &data[..], timestamp.value, ctx).await;
            Ok(Vec::new())
        }
        ServerSessionEvent::VideoDataReceived {
            data, timestamp, ..
        } => {
            forward_media(FLV_TAG_VIDEO, &data[..], timestamp.value, ctx).await;
            Ok(Vec::new())
        }
        ServerSessionEvent::PingResponseReceived { .. } => {
            tracing::trace!(peer = %ctx.peer_addr, "rtmp ping response received");
            Ok(Vec::new())
        }
        // ClientChunkSizeChanged, ReleaseStreamRequested (no accept/reject flow
        // exists for it), UnhandleableAmf0Command, PlayStreamRequested/Finished,
        // AcknowledgementReceived: informational only, nothing to do for a
        // publish-only ingest listener.
        _ => Ok(Vec::new()),
    }
}

async fn handle_publish_requested(
    session: &mut ServerSession,
    request_id: u32,
    app_name: String,
    stream_key: String,
    ctx: &mut ConnCtx,
) -> anyhow::Result<Vec<ServerSessionResult>> {
    let key_hash = fingerprint(&stream_key);

    if !claim_key(&ctx.active_keys, &stream_key) {
        ctx.metrics
            .publish_total
            .with_label_values(&["already_publishing"])
            .inc();
        tracing::warn!(peer = %ctx.peer_addr, key_hash = %key_hash, "rejecting rtmp publish: stream key already active");
        return Ok(session.reject_request(
            request_id,
            "NetStream.Publish.BadName",
            "stream key already publishing",
        )?);
    }

    let decision = match ctx.auth.authorize(IngestKind::Rtmp, &stream_key).await {
        Ok(decision) => decision,
        Err(err) => {
            release_key(&ctx.active_keys, &stream_key);
            ctx.metrics
                .publish_total
                .with_label_values(&["rejected"])
                .inc();
            tracing::warn!(peer = %ctx.peer_addr, key_hash = %key_hash, error = %err, "rejecting rtmp publish: not authorized");
            return Ok(session.reject_request(
                request_id,
                "NetStream.Publish.BadName",
                "stream key not authorized",
            )?);
        }
    };

    let (flv_tx, flv_rx) = mpsc::channel::<Vec<u8>>(FLV_CHANNEL_CAPACITY);
    // The channel was just created with spare capacity, so this can never
    // actually block/fail on backpressure.
    let _ = flv_tx.try_send(flv_header());

    let ingest_session = IngestSession {
        kind: IngestKind::Rtmp,
        key: stream_key.clone(),
        stream: Box::new(FlvByteStream::new(flv_rx)),
        span: tracing::Span::current(),
    };

    if ctx.session_tx.send(ingest_session).await.is_err() {
        release_key(&ctx.active_keys, &stream_key);
        ctx.metrics
            .publish_total
            .with_label_values(&["rejected"])
            .inc();
        tracing::warn!(peer = %ctx.peer_addr, key_hash = %key_hash, "rejecting rtmp publish: pipeline supervisor unavailable");
        return Ok(session.reject_request(
            request_id,
            "NetStream.Publish.BadName",
            "ingest pipeline unavailable",
        )?);
    }

    ctx.published = Some(PublishedStream {
        key: stream_key, // last use -- move rather than clone
        flv_tx,
    });
    ctx.metrics
        .publish_total
        .with_label_values(&["accepted"])
        .inc();
    tracing::info!(
        peer = %ctx.peer_addr,
        app = %loggable_app(&app_name),
        key_hash = %key_hash,
        community_id = %decision.community_id,
        config_id = %decision.config_id,
        "rtmp publish accepted"
    );
    Ok(session.accept_request(request_id)?)
}

fn handle_publish_finished(stream_key: String, ctx: &mut ConnCtx) {
    let matches_active = matches!(&ctx.published, Some(p) if p.key == stream_key);
    if matches_active {
        release_key(&ctx.active_keys, &stream_key);
        tracing::info!(peer = %ctx.peer_addr, key_hash = %fingerprint(&stream_key), "rtmp publish finished");
        // Dropping `published` (and its `flv_tx`) closes the FLV stream the
        // pipeline supervisor is reading -- see `IngestSession::stream`.
        ctx.published = None;
    }
}

async fn forward_media(tag_type: u8, data: &[u8], timestamp_ms: u32, ctx: &mut ConnCtx) {
    let Some((flv_tx, key_hash)) = active_flv_sender(ctx) else {
        return;
    };
    let tag = flv_tag(tag_type, timestamp_ms, data);
    send_tag(tag, flv_tx, &key_hash, ctx).await;
}

async fn forward_metadata(metadata: &StreamMetadata, ctx: &mut ConnCtx) {
    let Some((flv_tx, key_hash)) = active_flv_sender(ctx) else {
        return;
    };
    let payload = match encode_metadata(metadata) {
        Ok(payload) => payload,
        Err(err) => {
            tracing::warn!(peer = %ctx.peer_addr, key_hash = %key_hash, error = %err, "failed to encode rtmp onMetaData, skipping tag");
            return;
        }
    };
    let tag = flv_tag(FLV_TAG_SCRIPT, 0, &payload);
    send_tag(tag, flv_tx, &key_hash, ctx).await;
}

fn active_flv_sender(ctx: &ConnCtx) -> Option<(mpsc::Sender<Vec<u8>>, String)> {
    ctx.published
        .as_ref()
        .map(|p| (p.flv_tx.clone(), fingerprint(&p.key)))
}

/// Sends `tag` onto the FLV channel, honoring [`BACKPRESSURE_TIMEOUT`]. A
/// closed receiver (pipeline consumer gone) clears the active publish
/// without treating it as an error; a stall past the timeout marks the
/// connection for closure so the caller can unwind cleanly.
async fn send_tag(tag: Vec<u8>, flv_tx: mpsc::Sender<Vec<u8>>, key_hash: &str, ctx: &mut ConnCtx) {
    let len = tag.len() as u64;
    match tokio::time::timeout(BACKPRESSURE_TIMEOUT, flv_tx.send(tag)).await {
        Ok(Ok(())) => {
            ctx.metrics.bytes_total.inc_by(len);
        }
        Ok(Err(_)) => {
            // Receiver dropped: the pipeline stopped reading this stream.
            ctx.published = None;
        }
        Err(_) => {
            tracing::warn!(peer = %ctx.peer_addr, key_hash = %key_hash, "rtmp downstream consumer stalled past {BACKPRESSURE_TIMEOUT:?}, closing connection");
            ctx.should_close = true;
        }
    }
}

fn claim_key(active_keys: &StdMutex<HashSet<String>>, key: &str) -> bool {
    let mut keys = active_keys.lock().expect("active_keys mutex poisoned");
    keys.insert(key.to_string())
}

fn release_key(active_keys: &StdMutex<HashSet<String>>, key: &str) {
    let mut keys = active_keys.lock().expect("active_keys mutex poisoned");
    keys.remove(key);
}

/// The RTMP `app` name with any `?query` dropped. Clients that authenticate
/// through the app URL (`rtmp://host/live?token=...`) put a credential in
/// the query, so only the bare app name is safe to log.
fn loggable_app(app_name: &str) -> &str {
    app_name.split_once('?').map_or(app_name, |(app, _)| app)
}

fn encode_metadata(metadata: &StreamMetadata) -> anyhow::Result<Vec<u8>> {
    let mut properties = std::collections::HashMap::new();
    if let Some(v) = metadata.video_width {
        properties.insert("width".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.video_height {
        properties.insert("height".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.video_codec_id {
        properties.insert("videocodecid".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.video_frame_rate {
        properties.insert("framerate".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.video_bitrate_kbps {
        properties.insert("videodatarate".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.audio_codec_id {
        properties.insert("audiocodecid".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.audio_bitrate_kbps {
        properties.insert("audiodatarate".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.audio_sample_rate {
        properties.insert("audiosamplerate".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.audio_channels {
        properties.insert("audiochannels".to_string(), Amf0Value::Number(v as f64));
    }
    if let Some(v) = metadata.audio_is_stereo {
        properties.insert("stereo".to_string(), Amf0Value::Boolean(v));
    }
    if let Some(ref v) = metadata.encoder {
        properties.insert("encoder".to_string(), Amf0Value::Utf8String(v.clone()));
    }

    let values = vec![
        Amf0Value::Utf8String("onMetaData".to_string()),
        Amf0Value::Object(properties),
    ];
    rml_rtmp::rml_amf0::serialize(&values)
        .map_err(|err| anyhow::anyhow!("amf0 onMetaData encode failed: {err}"))
}

/// Builds the 13-byte FLV file header (`"FLV"` + version 1 + audio/video
/// present flags + 9-byte data offset) followed by the mandatory
/// `PreviousTagSize0` (always `0`) that precedes the first tag.
fn flv_header() -> Vec<u8> {
    let mut buf = Vec::with_capacity(13);
    buf.extend_from_slice(b"FLV");
    buf.push(1); // version
    buf.push(0b0000_0101); // audio (bit 2) + video (bit 0) present
    buf.extend_from_slice(&9u32.to_be_bytes()); // DataOffset = 9 (header size)
    buf.extend_from_slice(&0u32.to_be_bytes()); // PreviousTagSize0
    buf
}

/// Builds a single FLV tag (11-byte header + `data` + trailing
/// `PreviousTagSize`) for the given tag type/timestamp.
fn flv_tag(tag_type: u8, timestamp_ms: u32, data: &[u8]) -> Vec<u8> {
    let data_size = data.len() as u32;
    let mut buf = Vec::with_capacity(11 + data.len() + 4);
    buf.push(tag_type);
    buf.push((data_size >> 16) as u8);
    buf.push((data_size >> 8) as u8);
    buf.push(data_size as u8);
    buf.push((timestamp_ms >> 16) as u8);
    buf.push((timestamp_ms >> 8) as u8);
    buf.push(timestamp_ms as u8);
    buf.push((timestamp_ms >> 24) as u8); // TimestampExtended
    buf.extend_from_slice(&[0, 0, 0]); // StreamID, always 0
    buf.extend_from_slice(data);
    buf.extend_from_slice(&(11 + data_size).to_be_bytes()); // PreviousTagSize
    buf
}

/// [`AsyncRead`] adapter over a `Vec<u8>`-chunk `mpsc::Receiver`: the byte
/// stream ffmpeg reads via `-f flv -i pipe:0`. Closing the sender (the
/// connection task dropping [`PublishedStream::flv_tx`]) surfaces as a
/// clean EOF (`Ok(())` with no bytes written), matching
/// [`IngestListener`]'s "disconnect closes the stream" contract.
struct FlvByteStream {
    rx: mpsc::Receiver<Vec<u8>>,
    buf: Vec<u8>,
    pos: usize,
}

impl FlvByteStream {
    fn new(rx: mpsc::Receiver<Vec<u8>>) -> Self {
        Self {
            rx,
            buf: Vec::new(),
            pos: 0,
        }
    }
}

impl AsyncRead for FlvByteStream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        // All fields are `Unpin` (`mpsc::Receiver`, `Vec<u8>`, `usize`), so
        // it's sound to project via `get_mut` rather than pin-project.
        let this = self.get_mut();
        loop {
            if this.pos < this.buf.len() {
                let n = (this.buf.len() - this.pos).min(buf.remaining());
                let end = this.pos + n;
                buf.put_slice(&this.buf[this.pos..end]);
                this.pos = end;
                return Poll::Ready(Ok(()));
            }
            match this.rx.poll_recv(cx) {
                Poll::Ready(Some(chunk)) => {
                    this.buf = chunk;
                    this.pos = 0;
                }
                Poll::Ready(None) => return Poll::Ready(Ok(())), // EOF
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct AllowAll;

    impl IngestAuth for AllowAll {
        fn authorize<'a>(
            &'a self,
            _kind: IngestKind,
            _key: &'a str,
        ) -> Pin<Box<dyn Future<Output = anyhow::Result<AuthDecision>> + Send + 'a>> {
            Box::pin(async {
                Ok(AuthDecision {
                    community_id: "community-1".to_string(),
                    config_id: "config-1".to_string(),
                })
            })
        }
    }

    #[tokio::test]
    async fn allow_all_authorize_returns_a_fixed_decision() {
        let decision = AllowAll
            .authorize(IngestKind::Rtmp, "any-key")
            .await
            .expect("AllowAll always authorizes");
        assert_eq!(decision.community_id, "community-1");
        assert_eq!(decision.config_id, "config-1");
    }

    fn test_ctx(published: Option<PublishedStream>) -> ConnCtx {
        let registry = prometheus::Registry::new();
        ConnCtx {
            peer_addr: "127.0.0.1:0".parse().expect("valid socket addr"),
            auth: Arc::new(AllowAll),
            active_keys: Arc::new(StdMutex::new(HashSet::new())),
            session_tx: mpsc::channel(1).0,
            metrics: RtmpMetrics::register(&registry).expect("register test metrics"),
            published,
            should_close: false,
        }
    }

    #[test]
    fn flv_header_has_magic_version_and_flags() {
        let header = flv_header();
        assert_eq!(&header[0..3], b"FLV");
        assert_eq!(header[3], 1, "version must be 1");
        assert_eq!(header[4], 0b0000_0101, "audio+video present flags");
        assert_eq!(&header[5..9], &9u32.to_be_bytes(), "data offset must be 9");
        assert_eq!(
            &header[9..13],
            &0u32.to_be_bytes(),
            "PreviousTagSize0 must be 0"
        );
        assert_eq!(header.len(), 13);
    }

    #[test]
    fn flv_tag_encodes_type_size_timestamp_and_trailer() {
        let data = [1u8, 2, 3, 4, 5];
        let tag = flv_tag(FLV_TAG_VIDEO, 0x0102_0304, &data);

        assert_eq!(tag[0], FLV_TAG_VIDEO);
        assert_eq!(&tag[1..4], &[0, 0, 5], "3-byte big-endian data size");
        assert_eq!(&tag[4..7], &[0x02, 0x03, 0x04], "3-byte timestamp");
        assert_eq!(tag[7], 0x01, "timestamp extended byte");
        assert_eq!(&tag[8..11], &[0, 0, 0], "stream id always 0");
        assert_eq!(&tag[11..16], &data);
        let prev_size = u32::from_be_bytes(tag[16..20].try_into().unwrap());
        assert_eq!(prev_size, 11 + data.len() as u32);
        assert_eq!(tag.len(), 20);
    }

    #[test]
    fn flv_tag_audio_type_matches_rtmp_audio_message_type_id() {
        assert_eq!(FLV_TAG_AUDIO, 8);
        assert_eq!(FLV_TAG_VIDEO, 9);
        assert_eq!(FLV_TAG_SCRIPT, 18);
    }

    #[test]
    fn loggable_app_drops_the_query_but_keeps_the_app_name() {
        assert_eq!(loggable_app("live"), "live");
        assert_eq!(loggable_app("live?token=SECRET"), "live");
        assert_eq!(loggable_app("live/sub?a=1&b=2"), "live/sub");
        assert_eq!(loggable_app("?token=SECRET"), "");
    }

    #[test]
    fn encode_metadata_round_trips_through_amf0() {
        // Set every `StreamMetadata` field so `encode_metadata` exercises
        // all of its `if let Some(..)` branches, not just a subset.
        let mut metadata = StreamMetadata::new();
        metadata.video_width = Some(1920);
        metadata.video_height = Some(1080);
        metadata.video_codec_id = Some(7);
        metadata.video_frame_rate = Some(60.0);
        metadata.video_bitrate_kbps = Some(6000);
        metadata.audio_codec_id = Some(10);
        metadata.audio_bitrate_kbps = Some(160);
        metadata.audio_sample_rate = Some(48000);
        metadata.audio_channels = Some(2);
        metadata.audio_is_stereo = Some(true);
        metadata.encoder = Some("obs".to_string());

        let encoded = encode_metadata(&metadata).expect("encode succeeds");
        let mut cursor = std::io::Cursor::new(encoded);
        let values = rml_rtmp::rml_amf0::deserialize(&mut cursor).expect("decode succeeds");

        assert_eq!(values.len(), 2);
        assert_eq!(values[0], Amf0Value::Utf8String("onMetaData".to_string()));
        let Amf0Value::Object(props) = &values[1] else {
            panic!("expected metadata object");
        };
        assert_eq!(props.get("width"), Some(&Amf0Value::Number(1920.0)));
        assert_eq!(props.get("height"), Some(&Amf0Value::Number(1080.0)));
        assert_eq!(props.get("videocodecid"), Some(&Amf0Value::Number(7.0)));
        assert_eq!(props.get("framerate"), Some(&Amf0Value::Number(60.0)));
        assert_eq!(props.get("videodatarate"), Some(&Amf0Value::Number(6000.0)));
        assert_eq!(props.get("audiocodecid"), Some(&Amf0Value::Number(10.0)));
        assert_eq!(props.get("audiodatarate"), Some(&Amf0Value::Number(160.0)));
        assert_eq!(
            props.get("audiosamplerate"),
            Some(&Amf0Value::Number(48000.0))
        );
        assert_eq!(props.get("audiochannels"), Some(&Amf0Value::Number(2.0)));
        assert_eq!(props.get("stereo"), Some(&Amf0Value::Boolean(true)));
        assert_eq!(
            props.get("encoder"),
            Some(&Amf0Value::Utf8String("obs".to_string()))
        );
    }

    #[tokio::test]
    async fn flv_byte_stream_reads_queued_chunks_then_eof_on_close() {
        let (tx, rx) = mpsc::channel::<Vec<u8>>(4);
        let mut stream = FlvByteStream::new(rx);

        tx.send(vec![1, 2, 3]).await.expect("send chunk 1");
        tx.send(vec![4, 5]).await.expect("send chunk 2");
        drop(tx);

        let mut collected = Vec::new();
        stream
            .read_to_end(&mut collected)
            .await
            .expect("read to end");
        assert_eq!(collected, vec![1, 2, 3, 4, 5]);
    }

    #[tokio::test]
    async fn flv_byte_stream_pending_until_first_chunk_arrives() {
        let (tx, rx) = mpsc::channel::<Vec<u8>>(1);
        let mut stream = FlvByteStream::new(rx);
        let mut buf = [0u8; 8];

        let read_task = tokio::spawn(async move {
            let n = stream.read(&mut buf).await.expect("read succeeds");
            (n, buf)
        });

        tokio::task::yield_now().await;
        tx.send(vec![9, 9, 9]).await.expect("send chunk");

        let (n, buf) = read_task.await.expect("read task completes");
        assert_eq!(n, 3);
        assert_eq!(&buf[..3], &[9, 9, 9]);
    }

    #[test]
    fn claim_key_rejects_duplicate_and_release_key_frees_it() {
        let active_keys = Arc::new(StdMutex::new(HashSet::new()));
        assert!(claim_key(&active_keys, "sk_1"), "first claim succeeds");
        assert!(
            !claim_key(&active_keys, "sk_1"),
            "second claim of the same key fails"
        );
        release_key(&active_keys, "sk_1");
        assert!(
            claim_key(&active_keys, "sk_1"),
            "claim succeeds again after release"
        );
    }

    #[tokio::test]
    async fn forward_media_with_no_active_publish_is_a_no_op() {
        let mut ctx = test_ctx(None);
        forward_media(FLV_TAG_AUDIO, &[1, 2, 3], 0, &mut ctx).await;
        assert_eq!(ctx.metrics.bytes_total.get(), 0);
        assert!(!ctx.should_close);
    }

    #[tokio::test]
    async fn forward_media_sends_tag_and_increments_bytes_total() {
        let (flv_tx, mut flv_rx) = mpsc::channel::<Vec<u8>>(4);
        let mut ctx = test_ctx(Some(PublishedStream {
            key: "sk_1".to_string(),
            flv_tx,
        }));

        forward_media(FLV_TAG_VIDEO, &[7, 7, 7], 42, &mut ctx).await;

        let tag = flv_rx.recv().await.expect("tag forwarded");
        assert_eq!(tag[0], FLV_TAG_VIDEO);
        assert!(ctx.metrics.bytes_total.get() > 0);
        assert!(!ctx.should_close);
    }

    #[tokio::test]
    async fn forward_media_clears_published_when_receiver_dropped() {
        let (flv_tx, flv_rx) = mpsc::channel::<Vec<u8>>(4);
        drop(flv_rx);
        let mut ctx = test_ctx(Some(PublishedStream {
            key: "sk_1".to_string(),
            flv_tx,
        }));

        forward_media(FLV_TAG_AUDIO, &[1], 0, &mut ctx).await;

        assert!(ctx.published.is_none());
        assert!(!ctx.should_close);
    }

    #[tokio::test(start_paused = true)]
    async fn send_tag_closes_connection_when_consumer_stalls_past_backpressure_timeout() {
        let (flv_tx, mut flv_rx) = mpsc::channel::<Vec<u8>>(1);
        // Fill the single slot so the next send has to wait on capacity.
        flv_tx.try_send(vec![0u8; 4]).expect("initial send fits");

        let mut ctx = test_ctx(Some(PublishedStream {
            key: "sk_1".to_string(),
            flv_tx,
        }));

        let handle = tokio::spawn(async move {
            forward_media(FLV_TAG_AUDIO, &[1, 2, 3], 0, &mut ctx).await;
            ctx
        });

        // Let the spawned task run up to the point it registers the timeout
        // timer against the (virtual) clock.
        tokio::task::yield_now().await;
        tokio::time::advance(BACKPRESSURE_TIMEOUT + Duration::from_millis(100)).await;

        let ctx = handle.await.expect("task completes");
        assert!(
            ctx.should_close,
            "stalled consumer must mark connection for close"
        );

        // Keep the receiver alive for the duration of the test so the
        // channel isn't closed for an unrelated reason.
        flv_rx.close();
    }

    #[tokio::test]
    async fn handle_publish_finished_only_clears_matching_key() {
        let (flv_tx, _flv_rx) = mpsc::channel::<Vec<u8>>(4);
        let mut ctx = test_ctx(Some(PublishedStream {
            key: "sk_1".to_string(),
            flv_tx,
        }));
        claim_key(&ctx.active_keys, "sk_1");

        handle_publish_finished("sk_other".to_string(), &mut ctx);
        assert!(
            ctx.published.is_some(),
            "non-matching key must not clear published"
        );

        handle_publish_finished("sk_1".to_string(), &mut ctx);
        assert!(ctx.published.is_none());
        assert!(
            !ctx.active_keys.lock().unwrap().contains("sk_1"),
            "key must be released"
        );
    }

    #[tokio::test]
    async fn bind_reports_local_addr_and_debug_impl() {
        let registry = prometheus::Registry::new();
        let listener = RtmpListener::bind(
            "127.0.0.1".parse().unwrap(),
            0,
            Arc::new(AllowAll),
            &registry,
        )
        .await
        .expect("bind succeeds on an ephemeral port");

        let addr = listener.local_addr().expect("local_addr succeeds");
        assert_eq!(addr.ip().to_string(), "127.0.0.1");
        assert_ne!(addr.port(), 0, "OS must assign a concrete port");

        let rendered = format!("{listener:?}");
        assert!(rendered.contains("RtmpListener"));
    }

    #[test]
    fn register_metrics_exposes_all_three_series() {
        let registry = prometheus::Registry::new();
        let metrics = RtmpMetrics::register(&registry).expect("register succeeds");

        // `IntGauge`/`IntCounter` always report a series; `IntCounterVec`
        // only materializes a child (and thus a non-empty `MetricFamily`)
        // once a label combination has actually been used -- prometheus's
        // `Registry::gather` prunes empty families, so exercise each label
        // once before asserting presence.
        metrics.connections_active.inc();
        metrics.publish_total.with_label_values(&["accepted"]).inc();
        metrics.bytes_total.inc_by(1);

        let families = registry.gather();
        let names: Vec<_> = families.iter().map(|f| f.name()).collect();
        assert!(names.contains(&"rtmp_connections_active"));
        assert!(names.contains(&"rtmp_publish_total"));
        assert!(names.contains(&"rtmp_bytes_total"));
    }
}

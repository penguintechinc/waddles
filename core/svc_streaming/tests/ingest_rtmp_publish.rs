//! Integration tests for `ingest::rtmp`. Drives a real RTMP client
//! handshake (`rml_rtmp::sessions::ClientSession`) over a loopback TCP
//! connection against a real `RtmpListener`, exercising the public
//! `IngestListener`/`IngestAuth` contract end-to-end rather than any
//! private implementation detail.

mod log_capture;

use std::collections::{HashSet, VecDeque};
use std::future::Future;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use rml_rtmp::handshake::{Handshake, HandshakeProcessResult, PeerType};
use rml_rtmp::rml_amf0::Amf0Value;
use rml_rtmp::sessions::{
    ClientSession, ClientSessionConfig, ClientSessionEvent, ClientSessionResult,
    PublishRequestType, StreamMetadata,
};
use rml_rtmp::time::RtmpTimestamp;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc;

use log_capture::LogCapture;
use svc_streaming::ingest::rtmp::{AuthDecision, IngestAuth, RtmpListener};
use svc_streaming::ingest::{IngestKind, IngestListener, IngestSession};
use svc_streaming::redact::fingerprint;
use svc_streaming::telemetry::render_metrics;

/// Generous upper bound for each network round trip in these tests -- a
/// hung listener/handshake fails loudly instead of blocking the suite.
const TEST_TIMEOUT: Duration = Duration::from_secs(10);

/// Test [`IngestAuth`] that authorizes only keys present in `allowed`,
/// standing in for S2's HTTP-backed production implementation.
struct AllowlistAuth {
    allowed: HashSet<String>,
}

impl IngestAuth for AllowlistAuth {
    fn authorize<'a>(
        &'a self,
        _kind: IngestKind,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = anyhow::Result<AuthDecision>> + Send + 'a>> {
        let allowed = self.allowed.contains(key);
        Box::pin(async move {
            if allowed {
                Ok(AuthDecision {
                    community_id: "community-1".to_string(),
                    config_id: "config-1".to_string(),
                })
            } else {
                Err(anyhow::anyhow!("stream key not recognized"))
            }
        })
    }
}

/// Binds an [`RtmpListener`] on an OS-assigned loopback port, spawns its
/// accept loop, and returns everything a test needs: the bound address,
/// the [`IngestSession`] receiver, the Prometheus registry it registered
/// into (for metrics assertions), and the background task handle.
async fn start_listener(
    allowed_keys: &[&str],
) -> (
    SocketAddr,
    mpsc::Receiver<IngestSession>,
    prometheus::Registry,
    tokio::task::JoinHandle<anyhow::Result<()>>,
) {
    let auth = Arc::new(AllowlistAuth {
        allowed: allowed_keys.iter().map(|s| s.to_string()).collect(),
    });
    let registry = prometheus::Registry::new();
    let listener = RtmpListener::bind(IpAddr::V4(Ipv4Addr::LOCALHOST), 0, auth, &registry)
        .await
        .expect("rtmp listener binds on an ephemeral port");
    let addr = listener.local_addr().expect("local_addr succeeds");
    let (tx, rx) = mpsc::channel::<IngestSession>(4);
    let handle = tokio::spawn(listener.run(tx));
    (addr, rx, registry, handle)
}

/// Drives the RTMP handshake as the client side over `stream`, returning
/// any post-handshake bytes the server already sent (must be fed into the
/// [`ClientSession`] before reading more from the socket).
async fn client_handshake(stream: &mut TcpStream) -> Vec<u8> {
    let mut handshake = Handshake::new(PeerType::Client);
    let p0_p1 = handshake
        .generate_outbound_p0_and_p1()
        .expect("generate c0/c1");
    stream.write_all(&p0_p1).await.expect("write c0/c1");

    let mut buf = [0u8; 4096];
    loop {
        let n = stream.read(&mut buf).await.expect("read handshake bytes");
        assert!(n > 0, "server closed connection during handshake");
        match handshake
            .process_bytes(&buf[..n])
            .expect("process handshake bytes")
        {
            HandshakeProcessResult::InProgress { response_bytes } => {
                if !response_bytes.is_empty() {
                    stream
                        .write_all(&response_bytes)
                        .await
                        .expect("write handshake response");
                }
            }
            HandshakeProcessResult::Completed {
                response_bytes,
                remaining_bytes,
            } => {
                if !response_bytes.is_empty() {
                    stream
                        .write_all(&response_bytes)
                        .await
                        .expect("write handshake response");
                }
                return remaining_bytes;
            }
        }
    }
}

async fn write_client_result(stream: &mut TcpStream, result: ClientSessionResult) {
    if let ClientSessionResult::OutboundResponse(packet) = result {
        stream
            .write_all(&packet.bytes)
            .await
            .expect("write outbound rtmp packet");
    }
}

/// Drives an RTMP client through handshake -> connect -> publish-request
/// against `stream`. On acceptance, returns the live [`ClientSession`] so
/// the caller can keep publishing audio/video; on rejection, returns the
/// server's rejection description.
async fn connect_and_publish(
    stream: &mut TcpStream,
    app_name: &str,
    stream_key: &str,
) -> Result<ClientSession, String> {
    let leftover = client_handshake(stream).await;
    let (mut session, _) =
        ClientSession::new(ClientSessionConfig::new()).expect("new client session");

    let mut pending: VecDeque<ClientSessionResult> = VecDeque::new();
    if !leftover.is_empty() {
        pending.extend(
            session
                .handle_input(&leftover)
                .expect("handle leftover bytes"),
        );
    }

    let connect_result = session
        .request_connection(app_name.to_string())
        .expect("request connection");
    write_client_result(stream, connect_result).await;

    let mut requested_publish = false;
    let mut buf = [0u8; 4096];
    loop {
        while let Some(result) = pending.pop_front() {
            match result {
                ClientSessionResult::OutboundResponse(packet) => {
                    stream
                        .write_all(&packet.bytes)
                        .await
                        .expect("write outbound rtmp packet");
                }
                ClientSessionResult::RaisedEvent(ClientSessionEvent::ConnectionRequestAccepted) => {
                    if !requested_publish {
                        requested_publish = true;
                        let publish_result = session
                            .request_publishing(stream_key.to_string(), PublishRequestType::Live)
                            .expect("request publishing");
                        pending.push_back(publish_result);
                    }
                }
                ClientSessionResult::RaisedEvent(
                    ClientSessionEvent::ConnectionRequestRejected { description },
                ) => {
                    return Err(format!("connection rejected: {description}"));
                }
                ClientSessionResult::RaisedEvent(ClientSessionEvent::PublishRequestAccepted) => {
                    return Ok(session);
                }
                ClientSessionResult::RaisedEvent(ClientSessionEvent::UnhandleableAmf0Command {
                    ..
                }) => {
                    // `ServerSessionConfig::new()` defaults
                    // `send_on_bw_done_message_on_start` to `true`, so the
                    // server sends an `onBWDone` command right after
                    // accepting the connection; `ClientSession` has no
                    // built-in handling for it and surfaces it here. Not
                    // relevant to the publish flow under test.
                }
                ClientSessionResult::RaisedEvent(
                    ClientSessionEvent::UnknownTransactionResultReceived {
                        additional_values, ..
                    },
                ) => {
                    // The RTMP `publish` command always carries
                    // `transaction_id 0` per spec, which `request_publishing`
                    // doesn't register as a tracked transaction (it only
                    // tracks the preceding `createStream` call) -- so the
                    // server's `reject_request` error response for a
                    // rejected publish surfaces here instead of as a
                    // dedicated "publish rejected" event. Treat it as that
                    // rejection signal.
                    let description = additional_values
                        .iter()
                        .find_map(|value| match value {
                            Amf0Value::Object(props) => props
                                .get("description")
                                .and_then(|v| v.clone().get_string()),
                            _ => None,
                        })
                        .unwrap_or_else(|| "publish rejected".to_string());
                    return Err(description);
                }
                ClientSessionResult::RaisedEvent(other) => {
                    panic!("unexpected client event during connect/publish: {other:?}");
                }
                ClientSessionResult::UnhandleableMessageReceived(_) => {}
            }
        }

        let n = stream.read(&mut buf).await.expect("read server bytes");
        if n == 0 {
            return Err("server closed the connection".to_string());
        }
        pending.extend(
            session
                .handle_input(&buf[..n])
                .expect("handle server bytes"),
        );
    }
}

#[tokio::test]
async fn successful_publish_emits_session_with_valid_flv_header() {
    let (addr, mut rx, _registry, _listener_handle) = start_listener(&["sk_allowed"]).await;
    let mut stream = TcpStream::connect(addr)
        .await
        .expect("connect to rtmp listener");

    tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", "sk_allowed"),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");

    let mut session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("ingest session received in time")
        .expect("ingest channel not closed");

    assert_eq!(session.kind, IngestKind::Rtmp);
    assert_eq!(session.key, "sk_allowed");

    let mut header = [0u8; 13];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut header))
        .await
        .expect("read flv header in time")
        .expect("read flv header bytes");

    assert_eq!(&header[0..3], b"FLV", "flv magic");
    assert_eq!(header[3], 1, "flv version must be 1");
    assert_eq!(
        &header[5..9],
        &9u32.to_be_bytes(),
        "flv data offset must be 9"
    );
    assert_eq!(
        &header[9..13],
        &0u32.to_be_bytes(),
        "PreviousTagSize0 must be 0"
    );
}

#[tokio::test]
async fn unknown_key_is_rejected_cleanly() {
    let (addr, _rx, _registry, _listener_handle) = start_listener(&["sk_allowed"]).await;
    let mut stream = TcpStream::connect(addr)
        .await
        .expect("connect to rtmp listener");

    let result = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", "sk_totally_unknown"),
    )
    .await
    .expect("publish flow completes in time");

    assert!(
        result.is_err(),
        "an unrecognized stream key must be rejected"
    );
}

#[tokio::test]
async fn duplicate_publish_of_an_active_key_is_rejected() {
    let (addr, mut rx, _registry, _listener_handle) = start_listener(&["sk_dup"]).await;

    let mut first = TcpStream::connect(addr)
        .await
        .expect("connect first client");
    tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut first, "live", "sk_dup"),
    )
    .await
    .expect("first publish flow completes in time")
    .expect("first publish accepted");

    let _first_session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("first ingest session received in time")
        .expect("ingest channel not closed");

    let mut second = TcpStream::connect(addr)
        .await
        .expect("connect second client");
    let result = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut second, "live", "sk_dup"),
    )
    .await
    .expect("second publish flow completes in time");

    assert!(
        result.is_err(),
        "a second publish of an already-active key must be rejected"
    );
}

#[tokio::test]
async fn disconnect_closes_the_ingest_stream() {
    let (addr, mut rx, _registry, _listener_handle) = start_listener(&["sk_disconnect"]).await;
    let mut stream = TcpStream::connect(addr)
        .await
        .expect("connect to rtmp listener");

    tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", "sk_disconnect"),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");

    let mut session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("ingest session received in time")
        .expect("ingest channel not closed");

    let mut header = [0u8; 13];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut header))
        .await
        .expect("read flv header in time")
        .expect("read flv header bytes");

    drop(stream); // simulate the publisher disconnecting

    let mut trailing = Vec::new();
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_to_end(&mut trailing))
        .await
        .expect("stream reaches EOF in time")
        .expect("read to end succeeds");
    assert!(
        trailing.is_empty(),
        "no further FLV bytes should follow a client disconnect"
    );
}

#[tokio::test]
async fn publish_and_media_increment_prometheus_metrics() {
    let (addr, mut rx, registry, _listener_handle) = start_listener(&["sk_metrics"]).await;
    let mut stream = TcpStream::connect(addr)
        .await
        .expect("connect to rtmp listener");

    let mut client_session = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", "sk_metrics"),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");

    let mut session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("ingest session received in time")
        .expect("ingest channel not closed");

    // Drain the FLV header, then publish one video frame and read its tag
    // header back out -- this deterministically synchronizes on the frame
    // having been processed server-side (no arbitrary sleep needed).
    let mut header = [0u8; 13];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut header))
        .await
        .expect("read flv header in time")
        .expect("read flv header bytes");

    let video_result = client_session
        .publish_video_data(vec![1, 2, 3, 4].into(), RtmpTimestamp::new(0), false)
        .expect("publish video data");
    write_client_result(&mut stream, video_result).await;

    let mut tag_header = [0u8; 11];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut tag_header))
        .await
        .expect("read flv tag header in time")
        .expect("read flv tag header bytes");
    assert_eq!(
        tag_header[0], 9,
        "video data must produce an FLV video tag (type 9)"
    );

    let rendered = render_metrics(&registry).expect("render prometheus metrics");
    assert!(
        rendered.contains("rtmp_connections_active 1"),
        "rendered metrics: {rendered}"
    );
    assert!(
        rendered.contains("rtmp_publish_total{result=\"accepted\"} 1"),
        "rendered metrics: {rendered}"
    );
    assert!(
        rendered.contains("rtmp_bytes_total"),
        "rendered metrics: {rendered}"
    );
}

#[tokio::test]
async fn stop_publishing_finishes_the_publish_and_closes_the_ingest_stream() {
    let (addr, mut rx, _registry, _listener_handle) = start_listener(&["sk_stop"]).await;
    let mut stream = TcpStream::connect(addr)
        .await
        .expect("connect to rtmp listener");

    let mut client_session = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", "sk_stop"),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");

    let mut session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("ingest session received in time")
        .expect("ingest channel not closed");

    let mut header = [0u8; 13];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut header))
        .await
        .expect("read flv header in time")
        .expect("read flv header bytes");

    // Tell the server we're done publishing (RTMP `deleteStream`) without
    // dropping the TCP connection -- this must raise
    // `ServerSessionEvent::PublishStreamFinished` server-side and close the
    // FLV stream even though the client is still connected.
    for result in client_session.stop_publishing().expect("stop publishing") {
        write_client_result(&mut stream, result).await;
    }

    let mut trailing = Vec::new();
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_to_end(&mut trailing))
        .await
        .expect("stream reaches EOF in time")
        .expect("read to end succeeds");
    assert!(
        trailing.is_empty(),
        "no further FLV bytes should follow PublishStreamFinished"
    );
}

#[tokio::test]
async fn publish_metadata_forwards_onmetadata_as_flv_script_tag() {
    let (addr, mut rx, _registry, _listener_handle) = start_listener(&["sk_meta"]).await;
    let mut stream = TcpStream::connect(addr)
        .await
        .expect("connect to rtmp listener");

    let mut client_session = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", "sk_meta"),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");

    let mut session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("ingest session received in time")
        .expect("ingest channel not closed");

    let mut header = [0u8; 13];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut header))
        .await
        .expect("read flv header in time")
        .expect("read flv header bytes");

    let mut metadata = StreamMetadata::new();
    metadata.video_width = Some(1920);
    metadata.video_height = Some(1080);
    metadata.encoder = Some("test-encoder".to_string());
    let metadata_result = client_session
        .publish_metadata(&metadata)
        .expect("publish metadata");
    write_client_result(&mut stream, metadata_result).await;

    let mut tag_header = [0u8; 11];
    tokio::time::timeout(TEST_TIMEOUT, session.stream.read_exact(&mut tag_header))
        .await
        .expect("read flv tag header in time")
        .expect("read flv tag header bytes");
    assert_eq!(
        tag_header[0], 18,
        "onMetaData must produce an FLV script-data tag (type 18)"
    );
}

/// Regression: the RTMP stream key -- and any credential a client smuggles
/// into the `app` name (`live?token=...`, a common auth scheme) -- never
/// reaches a log line at any level. The logs carry the non-secret
/// `key_hash` correlation id instead.
#[tokio::test]
async fn rtmp_ingest_logs_never_contain_the_raw_stream_key_or_app_query() {
    const ALLOWED_KEY: &str = "sk_live_RTMP_ALLOWED_SECRET_3a7f";
    const UNKNOWN_KEY: &str = "sk_live_RTMP_UNKNOWN_SECRET_b812";
    const APP_QUERY_SECRET: &str = "APP_QUERY_TOKEN_SECRET_91cd";
    let app = format!("live?token={APP_QUERY_SECRET}");

    let capture = LogCapture::install();
    let (addr, mut rx, _registry, _listener_handle) = start_listener(&[ALLOWED_KEY]).await;

    // Unauthorized key.
    let mut rejected = TcpStream::connect(addr).await.expect("connect");
    let result = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut rejected, &app, UNKNOWN_KEY),
    )
    .await
    .expect("publish flow completes in time");
    assert!(result.is_err(), "unknown key must be rejected");

    // Authorized key.
    let mut first = TcpStream::connect(addr).await.expect("connect");
    let _client = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut first, &app, ALLOWED_KEY),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");
    let _session = tokio::time::timeout(TEST_TIMEOUT, rx.recv())
        .await
        .expect("ingest session received in time")
        .expect("ingest channel not closed");

    // Duplicate publisher for the active key.
    let mut duplicate = TcpStream::connect(addr).await.expect("connect");
    let result = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut duplicate, &app, ALLOWED_KEY),
    )
    .await
    .expect("publish flow completes in time");
    assert!(result.is_err(), "duplicate publisher must be rejected");

    // Publisher goes away.
    drop(first);
    capture
        .wait_for(
            &format!(
                "rtmp connection closed, stream ended key_hash={}",
                fingerprint(ALLOWED_KEY)
            ),
            TEST_TIMEOUT,
        )
        .await;

    capture.assert_no_secret_leak(
        &[ALLOWED_KEY, UNKNOWN_KEY, APP_QUERY_SECRET],
        &[
            "rtmp connect requested",
            "rejecting rtmp publish: not authorized",
            "rejecting rtmp publish: stream key already active",
            "rtmp publish accepted",
            &format!("key_hash={}", fingerprint(UNKNOWN_KEY)),
            &format!("key_hash={}", fingerprint(ALLOWED_KEY)),
        ],
    );
}

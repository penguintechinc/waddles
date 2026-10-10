//! Regression: ingest credentials (RTMP stream keys, SRT stream ids, WHIP
//! tokens) and the secret URLs ffmpeg echoes back must never reach a log
//! line -- at *any* level, `debug`/`trace` included (`rules/critical-rules.md`
//! Token & Secret Hygiene; Observability: sanitization applies at every
//! level).
//!
//! Each test installs (once per process) a global TRACE-level `tracing`
//! subscriber that captures the fully rendered output (message, fields, and
//! enclosing span fields), drives the **real** code path over loopback (SRT
//! over UDP, WHIP over a negotiated `PeerConnection`, a fake-ffmpeg
//! subprocess), and asserts two things:
//!
//! 1. the raw secret appears nowhere in the capture, and
//! 2. the log lines we expect *were* captured, carrying the non-secret
//!    `key_hash` correlation id (`svc_streaming::redact::fingerprint`) --
//!    a positive control, so "no secret found" can never mean "nothing was
//!    captured" (zero lines examined is a failure, not a pass).
//!
//! The capture is shared by the tests of this binary (see `log_capture`), so
//! every test uses secrets unique to it.

mod log_capture;
mod rtc_common;

use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use futures::SinkExt;
use srt_tokio::SrtSocket;
use tokio::sync::mpsc;
use uuid::Uuid;

use log_capture::LogCapture;
use rtc_common::*;
use svc_streaming::ingest::srt::{AllowlistAuth, SrtListener};
use svc_streaming::ingest::whip::{self, WhipState};
use svc_streaming::ingest::{IngestKind, IngestSession};
use svc_streaming::pipeline::{InputSpec, Paths};
use svc_streaming::redact::fingerprint;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

/// Binds an OS-assigned loopback UDP port for an SRT listener.
async fn ephemeral_udp() -> (tokio::net::UdpSocket, u16) {
    let socket = tokio::net::UdpSocket::bind("127.0.0.1:0")
        .await
        .expect("bind ephemeral udp socket");
    let port = socket.local_addr().expect("local addr").port();
    (socket, port)
}

fn mpegts_packet() -> Bytes {
    let mut packet = vec![0u8; 188];
    packet[0] = 0x47;
    Bytes::from(packet)
}

#[tokio::test]
async fn srt_ingest_logs_never_contain_the_raw_stream_key() {
    const KNOWN_KEY: &str = "sk_live_SRT_KEY_SECRET_5c1e";
    const UNKNOWN_KEY: &str = "sk_live_SRT_UNKNOWN_SECRET_88ab";
    const NON_TS_KEY: &str = "sk_live_SRT_NONTS_SECRET_0f42";
    const GONE_KEY: &str = "sk_live_SRT_GONE_SECRET_71d9";
    const DROPPED_KEY: &str = "sk_live_SRT_DROPPED_SECRET_a3c6";

    let capture = LogCapture::install();
    let allowlist = || {
        Arc::new(AllowlistAuth::new([
            KNOWN_KEY,
            NON_TS_KEY,
            GONE_KEY,
            DROPPED_KEY,
        ]))
    };

    // --- Listener A: unauthorized key, duplicate publisher, non-TS payload,
    // --- and a caller that disconnects before sending anything.
    let (socket, port) = ephemeral_udp().await;
    let (tx, mut rx) = mpsc::channel(4);
    let listener = SrtListener::new(port).with_auth(allowlist());
    let _server = tokio::spawn(listener.run_with_socket(socket, tx));

    let unauthorized = SrtSocket::builder()
        .call(format!("127.0.0.1:{port}"), Some(UNKNOWN_KEY))
        .await;
    assert!(unauthorized.is_err(), "unknown key must be rejected");

    let mut first = SrtSocket::builder()
        .call(format!("127.0.0.1:{port}"), Some(KNOWN_KEY))
        .await
        .expect("first publisher accepted");
    first
        .send((std::time::Instant::now(), mpegts_packet()))
        .await
        .expect("send ts packet");
    let _session = tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .expect("session emitted")
        .expect("channel open");
    let duplicate = SrtSocket::builder()
        .call(format!("127.0.0.1:{port}"), Some(KNOWN_KEY))
        .await;
    assert!(duplicate.is_err(), "duplicate publisher must be rejected");
    first.close().await.expect("close first");

    let mut not_ts = SrtSocket::builder()
        .call(format!("127.0.0.1:{port}"), Some(NON_TS_KEY))
        .await
        .expect("non-ts publisher accepted pre-payload");
    not_ts
        .send((std::time::Instant::now(), Bytes::from(vec![0u8; 188])))
        .await
        .expect("send non-ts payload");
    not_ts.close().await.expect("close non-ts caller");

    let mut gone = SrtSocket::builder()
        .call(format!("127.0.0.1:{port}"), Some(GONE_KEY))
        .await
        .expect("silent publisher accepted");
    gone.close().await.expect("close silent caller");

    // --- Listener B: the pipeline-supervisor side of the channel is gone.
    let (socket_b, port_b) = ephemeral_udp().await;
    let (tx_b, rx_b) = mpsc::channel(4);
    drop(rx_b);
    let listener_b = SrtListener::new(port_b).with_auth(allowlist());
    let _server_b = tokio::spawn(listener_b.run_with_socket(socket_b, tx_b));
    let mut dropped = SrtSocket::builder()
        .call(format!("127.0.0.1:{port_b}"), Some(DROPPED_KEY))
        .await
        .expect("publisher accepted");
    dropped
        .send((std::time::Instant::now(), mpegts_packet()))
        .await
        .expect("send ts packet");
    tokio::time::timeout(Duration::from_secs(5), dropped.close())
        .await
        .expect("close completes")
        .expect("close caller");

    let wait = Duration::from_secs(5);
    capture
        .wait_for("srt publish rejected: not mpegts", wait)
        .await;
    capture
        .wait_for("srt caller disconnected before sending data", wait)
        .await;
    capture
        .wait_for(
            "srt session dropped: pipeline supervisor channel closed",
            wait,
        )
        .await;

    capture.assert_no_secret_leak(
        &[KNOWN_KEY, UNKNOWN_KEY, NON_TS_KEY, GONE_KEY, DROPPED_KEY],
        &[
            "srt connect rejected: unauthorized key",
            "srt connect rejected: publisher already active for key",
            &format!("key_hash={}", fingerprint(UNKNOWN_KEY)),
            &format!("key_hash={}", fingerprint(KNOWN_KEY)),
            &format!("key_hash={}", fingerprint(NON_TS_KEY)),
            &format!("key_hash={}", fingerprint(GONE_KEY)),
            &format!("key_hash={}", fingerprint(DROPPED_KEY)),
        ],
    );
}

#[tokio::test]
async fn whip_ingest_logs_never_contain_the_raw_token() {
    const TOKEN: &str = "sk_live_WHIP_TOKEN_SECRET_9d41";

    let capture = LogCapture::install();

    let whip_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41850, 41859))).unwrap());
    let publisher_factory =
        PeerConnectionFactory::new(loopback_rtc_config((41860, 41869))).unwrap();
    let registry = prometheus::Registry::new();
    let metrics = RtcMetrics::register(&registry).unwrap();
    // The supervisor side of the hand-off channel is gone: a successful
    // publish takes the "ingest channel full or no receiver" debug path,
    // which used to print the token.
    let (ingest_tx, ingest_rx) = mpsc::channel(4);
    drop(ingest_rx);
    let stream_data_dir =
        std::env::temp_dir().join(format!("svc-streaming-log-redaction-{}", Uuid::new_v4()));
    let state = Arc::new(WhipState::new(
        whip_factory,
        Arc::new(AllowAllAuthorizer),
        ingest_tx,
        stream_data_dir,
        loopback_ip(),
        metrics,
    ));
    let router = whip::router(state);

    let (track, _ssrc) = opus_publisher_track();
    let handler = TestPeerHandler::new();
    let publisher = publisher_factory
        .build(Arc::clone(&handler))
        .await
        .expect("publisher peer builds");
    publisher
        .add_track(track as Arc<dyn webrtc::media_stream::track_local::TrackLocal>)
        .await
        .expect("add publisher track");
    let offer = offer_sdp(&publisher, &handler.gather_complete).await;

    let response = oneshot(router, post_sdp(&format!("/whip/{TOKEN}"), offer)).await;
    assert_status(&response, axum::http::StatusCode::CREATED);

    capture
        .wait_for("ingest channel full or no receiver", Duration::from_secs(5))
        .await;
    capture.assert_no_secret_leak(&[TOKEN], &[&format!("key_hash={}", fingerprint(TOKEN))]);
}

#[cfg(unix)]
#[tokio::test]
async fn ffmpeg_error_lines_never_leak_relay_urls_into_logs_or_pipeline_status() {
    use std::os::unix::fs::PermissionsExt;
    use svc_streaming::pipeline::{
        AudioCodec, FfmpegSupervisor, ObjectStoreRef, OutputSpec, PipelineEngine, PipelineSpec,
        SupervisorConfig, TranscodeProfile, VideoCodec,
    };
    use svc_streaming::store::DefaultSecretResolver;

    const RELAY_KEY: &str = "sk_live_RELAY_KEY_SECRET_c07b";
    const SRT_PASSPHRASE: &str = "PASSPHRASE_SECRET_4e2d";

    let capture = LogCapture::install();

    // A fake ffmpeg that fails the way the real one does -- echoing the
    // resolved secret push URLs it was given -- then keeps running.
    let script = std::env::temp_dir().join(format!(
        "svc-streaming-log-redaction-ffmpeg-{}-{}.sh",
        std::process::id(),
        Uuid::new_v4()
    ));
    let body = format!(
        "#!/bin/sh\n\
         echo \"[error] Error opening output 'rtmp://live.example.com/app/{RELAY_KEY}': Connection refused\" >&2\n\
         echo \"[error] srt://relay.example.com:9000?streamid={RELAY_KEY}&passphrase={SRT_PASSPHRASE}: I/O error\" >&2\n\
         while read -r line; do [ \"$line\" = \"q\" ] && exit 0; done\n"
    );
    std::fs::write(&script, body).expect("write fake ffmpeg");
    let mut perms = std::fs::metadata(&script).unwrap().permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&script, perms).unwrap();

    let supervisor = FfmpegSupervisor::new(
        script.clone(),
        std::env::temp_dir(),
        41870,
        Arc::new(DefaultSecretResolver),
        SupervisorConfig {
            backoff_initial: Duration::from_millis(20),
            backoff_max: Duration::from_millis(100),
            max_restarts: 3,
            stall_timeout: Duration::from_secs(30),
            stop_grace_timeout: Duration::from_millis(200),
            term_grace_timeout: Duration::from_millis(200),
        },
    );
    let id = Uuid::new_v4();
    supervisor
        .start(PipelineSpec {
            id,
            tenant: "tenant-1".into(),
            community_id: "community-1".into(),
            inputs: vec![InputSpec::Rtmp {
                stream_key: "sk_input".into(),
            }],
            profiles: vec![TranscodeProfile {
                name: "copy".into(),
                video: VideoCodec::Copy,
                audio: AudioCodec::Copy,
                resolution: None,
                fps: None,
            }],
            outputs: vec![OutputSpec::Record {
                profile: "copy".into(),
                target: ObjectStoreRef {
                    store: "local".into(),
                    prefix: "t".into(),
                },
            }],
        })
        .await
        .expect("start succeeds");

    capture
        .wait_for("ffmpeg reported an error", Duration::from_secs(5))
        .await;
    // Both stderr lines have been through the monitor once this one lands.
    capture
        .wait_for("srt://relay.example.com:9000", Duration::from_secs(5))
        .await;

    let progress = supervisor.progress(id).await.expect("registered");
    let last_error = progress.last_error.expect("an ffmpeg error was recorded");
    println!("log-redaction: pipeline last_error = {last_error:?}");
    for secret in [RELAY_KEY, SRT_PASSPHRASE] {
        assert!(
            !last_error.contains(secret),
            "raw secret {secret:?} leaked into the pipeline status: {last_error}"
        );
    }

    supervisor.stop(id).await.expect("stop succeeds");
    let _ = std::fs::remove_file(&script);

    capture.assert_no_secret_leak(
        &[RELAY_KEY, SRT_PASSPHRASE],
        &["ffmpeg reported an error", "rtmp://live.example.com/"],
    );
}

#[test]
fn debug_renderings_of_ingest_credentials_never_contain_the_raw_value() {
    const KEY: &str = "sk_live_DEBUG_KEY_SECRET_6b30";
    const TOKEN: &str = "sk_live_DEBUG_TOKEN_SECRET_2e91";
    const STREAM_ID: &str = "sk_live_DEBUG_STREAMID_SECRET_f548";
    const PULL_URL: &str =
        "rtmp://puller:PULL_PASSWORD_SECRET@origin.example.com/app/PULL_KEY_SECRET";
    const RESOLVED_URL: &str = "rtmp://relay.example.com/app/RESOLVED_RELAY_SECRET";

    let session = IngestSession {
        kind: IngestKind::Rtmp,
        key: KEY.to_string(),
        stream: Box::new(tokio::io::empty()),
        span: tracing::Span::none(),
    };
    let inputs = vec![
        InputSpec::Rtmp {
            stream_key: KEY.into(),
        },
        InputSpec::Srt {
            stream_id: STREAM_ID.into(),
        },
        InputSpec::Whip {
            token: TOKEN.into(),
        },
        InputSpec::Pull {
            url: PULL_URL.into(),
        },
    ];
    let mut paths = Paths::default();
    paths
        .resolved_secrets
        .insert("env:RELAY_URL".into(), RESOLVED_URL.into());
    paths.whip_sdp_paths.insert(
        0,
        std::path::PathBuf::from(format!("/data/stream/whip-{TOKEN}.sdp")),
    );

    let renderings = [
        format!("{session:?}"),
        format!("{session:#?}"),
        format!("{inputs:?}"),
        format!("{inputs:#?}"),
        format!("{paths:?}"),
        format!("{paths:#?}"),
    ];
    println!(
        "log-redaction: examined {} Debug rendering(s)",
        renderings.len()
    );
    assert!(renderings.iter().all(|r| !r.is_empty()));
    for rendering in &renderings {
        for secret in [
            KEY,
            TOKEN,
            STREAM_ID,
            "PULL_PASSWORD_SECRET",
            "PULL_KEY_SECRET",
            "RESOLVED_RELAY_SECRET",
        ] {
            assert!(
                !rendering.contains(secret),
                "raw secret {secret:?} leaked via Debug:\n{rendering}"
            );
        }
    }
    // Positive control: the correlation id is what's shown instead.
    assert!(
        renderings[0].contains(&fingerprint(KEY)),
        "{}",
        renderings[0]
    );
}

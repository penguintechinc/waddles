//! S12 integration test (deliverable 5): a real RTMP publish, through a
//! real `RtmpListener`, through `orchestrator::Orchestrator`, into a real
//! `FfmpegSupervisor` spawning a **fake** `ffmpeg` (no real ffmpeg is
//! installed on this host -- see the S12 task instructions; real-media
//! proof happens in-cluster later), asserting:
//! 1. the supervisor actually spawned ffmpeg with an argv containing the
//!    HLS output path `pipeline::ffmpeg`/`egress::hls::output` compute, and
//! 2. `HlsSink`/the orchestrator's `PipelineRegistry` lists the pipeline as
//!    running for the community.
//!
//! Uses the same fake-`sh`-ffmpeg technique as
//! `tests/pipeline_supervisor.rs` (a POSIX shell script standing in for the
//! real binary), extended to capture its own invocation argv to a file so
//! this test can assert on it. RTMP client handshake/publish driving is
//! duplicated from `tests/ingest_rtmp_publish.rs` (self-contained --
//! `tests/*.rs` files don't share code without a `tests/common`-style
//! helper module, and this is the only other file that needs it).
//!
//! # Telemetry gate (`rules/testing.md` Telemetry Validation)
//!
//! The same run also proves the stream latency histograms really emit: the
//! process-global meter provider and `tracing` subscriber are pointed at the
//! in-memory sink in `tests/otel_common` *before* anything is constructed,
//! and after the publish -> pipeline -> HLS path has run the test asserts
//! each ingest->egress histogram, stage histogram, counter, and span was
//! received (>= 1 data point, counts printed). A histogram that silently
//! stops emitting fails this test.

#![cfg(unix)]

mod otel_common;

use std::net::{IpAddr, Ipv4Addr};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body as AxumBody;
use axum::http::header::CONTENT_TYPE;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use rml_rtmp::handshake::{Handshake, HandshakeProcessResult, PeerType};
use rml_rtmp::rml_amf0::Amf0Value;
use rml_rtmp::sessions::{
    ClientSession, ClientSessionConfig, ClientSessionEvent, ClientSessionResult, PublishRequestType,
};
use rml_rtmp::time::RtmpTimestamp;
use sea_orm::ConnectionTrait;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tower::ServiceExt as _;
use uuid::Uuid;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::egress::hls::{hls_router, HlsRouterState, HlsSink, RunningPipelines};
use svc_streaming::egress::relay::RelaySink;
use svc_streaming::egress::whep::WhepState;
use svc_streaming::ingest::rtmp::RtmpListener;
use svc_streaming::ingest::whip::WhipState;
use svc_streaming::ingest::IngestListener as _;
use svc_streaming::orchestrator::{DbIngestAuth, Orchestrator, PipelineRegistry};
use svc_streaming::pipeline::{FfmpegSupervisor, SupervisorConfig};
use svc_streaming::rtc::ingest_auth::InternalIngestAuthClient;
use svc_streaming::rtc::{PeerConnectionFactory, RtcConfig, RtcMetrics};
use svc_streaming::store::DefaultSecretResolver;

use otel_common::span_attrs;

const TEST_TIMEOUT: Duration = Duration::from_secs(10);
const STREAM_KEY: &str = "sk_e2e_fake_ffmpeg";
const COMMUNITY_ID: i32 = 42;
const CONFIG_ID: i32 = 1;

// --- Fake ffmpeg: captures its own argv, then behaves like a healthy,
// responsive encoder (progress on stderr, clean exit on stdin "q").

fn fake_ffmpeg(argv_capture: &Path) -> PathBuf {
    let script_path = std::env::temp_dir().join(format!(
        "svc-streaming-e2e-fake-ffmpeg-{}-{}.sh",
        std::process::id(),
        Uuid::new_v4()
    ));
    let body = format!(
        r#"#!/bin/sh
printf '%s\n' "$*" > "{capture}"
( while true; do echo 'frame=  10 fps= 30 q=-1.0 size=  128kB time=00:00:01.00 bitrate=1000.0kbits/s speed=1.00x' >&2; sleep 0.05; done ) &
BGPID=$!
while read -r line; do
  if [ "$line" = "q" ]; then
    kill "$BGPID" 2>/dev/null
    exit 0
  fi
done
kill "$BGPID" 2>/dev/null
exit 0
"#,
        capture = argv_capture.display()
    );
    std::fs::write(&script_path, body).expect("write fake ffmpeg script");
    let mut perms = std::fs::metadata(&script_path).unwrap().permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&script_path, perms).unwrap();
    script_path
}

async fn wait_until<F, Fut>(timeout: Duration, mut check: F)
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = bool>,
{
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if check().await {
            return;
        }
        if tokio::time::Instant::now() >= deadline {
            panic!("condition not met within {timeout:?}");
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

// --- Minimal RTMP client (handshake + connect + publish), duplicated from
// tests/ingest_rtmp_publish.rs -- see this file's module doc.

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

async fn connect_and_publish(
    stream: &mut TcpStream,
    app_name: &str,
    stream_key: &str,
) -> Result<ClientSession, String> {
    let leftover = client_handshake(stream).await;
    let (mut session, _) =
        ClientSession::new(ClientSessionConfig::new()).expect("new client session");

    let mut pending: std::collections::VecDeque<ClientSessionResult> =
        std::collections::VecDeque::new();
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
                }) => {}
                ClientSessionResult::RaisedEvent(
                    ClientSessionEvent::UnknownTransactionResultReceived {
                        additional_values, ..
                    },
                ) => {
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

/// Builds a minimal `Arc<WhipState>` -- unused by this RTMP-only scenario,
/// but `Orchestrator::new`'s signature requires one (WHIP hand-off shares
/// the orchestrator with RTMP/SRT; see `orchestrator` module docs).
fn unused_whip_state(config: &Config) -> Arc<WhipState> {
    let rtc_config = RtcConfig::from_config(config).expect("valid webrtc_udp_range");
    let factory = Arc::new(PeerConnectionFactory::new(rtc_config).expect("factory builds"));
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).expect("registers once");
    let authorizer =
        Arc::new(InternalIngestAuthClient::new(config).expect("loopback client builds"));
    let (tx, _rx) = mpsc::channel(1);
    Arc::new(WhipState::new(
        factory,
        authorizer,
        tx,
        config.cli.stream_data_dir.clone(),
        config.cli.bind_addr,
        metrics,
    ))
}

#[tokio::test]
async fn rtmp_publish_starts_a_transcoded_pipeline_and_hls_lists_it() {
    // Must precede every `FfmpegSupervisor`/`HlsSink`/`Orchestrator`
    // construction: they bind their OTel instruments to whichever global
    // meter provider is installed at that moment.
    let otel = otel_common::OtelSink::install();

    // SAFETY: single test in this file/process, before any await point.
    unsafe { std::env::set_var("DB_TYPE", "sqlite") };

    let stream_data_dir =
        std::env::temp_dir().join(format!("svc-streaming-e2e-data-{}", Uuid::new_v4()));
    let db_path = std::env::temp_dir().join(format!(
        "svc-streaming-e2e-db-{}-{}.sqlite",
        std::process::id(),
        Uuid::new_v4()
    ));
    let argv_capture =
        std::env::temp_dir().join(format!("svc-streaming-e2e-argv-{}.txt", Uuid::new_v4()));
    let fake_ffmpeg_path = fake_ffmpeg(&argv_capture);

    let cli = CliConfig::try_parse_from([
        "svc-streaming",
        "--db-name",
        &db_path.to_string_lossy(),
        "--stream-data-dir",
        &stream_data_dir.to_string_lossy(),
        "--ffmpeg-path",
        &fake_ffmpeg_path.to_string_lossy(),
        "--webrtc-udp-range",
        "45500-45510",
    ])
    .expect("valid cli assembly");
    let config = Config {
        cli,
        db_password: Secret::new("unused"),
        cache_password: None,
        service_api_key: Secret::new("unused"),
        jwt_hmac_secret: None,
    };

    let db = svc_streaming::db::get_or_connect(&config)
        .await
        .expect("lazy sqlite connection establishes");
    db.execute_unprepared(&format!(
        r#"
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE);
        CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL);
        CREATE TABLE streaming_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'rtmp',
            enabled INTEGER NOT NULL DEFAULT 1,
            record_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_enabled INTEGER NOT NULL DEFAULT 0,
            transcode_bitrate_kbps INTEGER NOT NULL DEFAULT 4000
        );
        CREATE TABLE streaming_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            forward_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO tenants (id, slug) VALUES (1, 'tenant-1');
        INSERT INTO communities (id, tenant_id) VALUES ({COMMUNITY_ID}, 1);
        INSERT INTO streaming_configs (id, community_id, source_url, enabled, record_enabled, transcode_enabled)
            VALUES ({CONFIG_ID}, {COMMUNITY_ID}, '{STREAM_KEY}', 1, 0, 0);
        "#
    ))
    .await
    .expect("seed schema");

    let prom_registry = prometheus::Registry::new();
    let supervisor = Arc::new(FfmpegSupervisor::new(
        fake_ffmpeg_path,
        stream_data_dir.clone(),
        45500,
        Arc::new(DefaultSecretResolver),
        SupervisorConfig::default(),
    ));
    // 50 ms poll so the HLS poller notices the fixture segment quickly
    // instead of waiting out the production 2 s default.
    let hls = Arc::new(HlsSink::with_intervals(
        stream_data_dir.clone(),
        &prom_registry,
        Duration::from_millis(50),
        svc_streaming::egress::hls::DEFAULT_CLEANUP_DELAY,
    ));
    let registry = Arc::new(PipelineRegistry::new());
    let relay = Arc::new(RelaySink::new());
    let whip_state = unused_whip_state(&config);
    let _whep_state = WhepState::new(
        Arc::new(PeerConnectionFactory::new(RtcConfig::from_config(&config).unwrap()).unwrap()),
        RtcMetrics::register(&prometheus::Registry::new()).unwrap(),
        svc_streaming::egress::whep::DEFAULT_MAX_VIEWERS,
    );

    let orchestrator = Arc::new(Orchestrator::new(
        config.clone(),
        supervisor.clone(),
        hls.clone(),
        relay,
        None,
        registry.clone(),
        whip_state,
    ));
    let (ingest_tx, ingest_rx) = mpsc::channel(4);
    tokio::spawn(Arc::clone(&orchestrator).run(ingest_rx));

    let db_auth = Arc::new(DbIngestAuth::new(config.clone()));
    let rtmp_listener =
        RtmpListener::bind(IpAddr::V4(Ipv4Addr::LOCALHOST), 0, db_auth, &prom_registry)
            .await
            .expect("rtmp listener binds on an ephemeral port");
    let rtmp_addr = rtmp_listener.local_addr().expect("local_addr");
    tokio::spawn(rtmp_listener.run(ingest_tx));

    // Drive a real RTMP publish against the listener.
    let mut stream = TcpStream::connect(rtmp_addr)
        .await
        .expect("connect to rtmp listener");
    let mut client_session = tokio::time::timeout(
        TEST_TIMEOUT,
        connect_and_publish(&mut stream, "live", STREAM_KEY),
    )
    .await
    .expect("publish flow completes in time")
    .expect("publish accepted");

    for i in 0..3u32 {
        let video_result = client_session
            .publish_video_data(
                vec![0, 0, 0, i as u8].into(),
                RtmpTimestamp::new(i * 40),
                false,
            )
            .expect("publish video data");
        write_client_result(&mut stream, video_result).await;
    }

    // The pipeline id is deterministic from `streaming_configs.id`
    // (`api::common::pipeline_id_for_config`, `pub(crate)` -- not visible
    // from an integration test, so recomputed here from its documented
    // formula: `Uuid::from_u128(config_id as u128)`).
    let pipeline_id = Uuid::from_u128(CONFIG_ID as u128);

    // 1. ffmpeg was actually spawned with an HLS output argument pointing
    // at this pipeline's HLS directory.
    wait_until(TEST_TIMEOUT, || async {
        tokio::fs::metadata(&argv_capture).await.is_ok()
    })
    .await;
    let argv_line = tokio::fs::read_to_string(&argv_capture)
        .await
        .expect("read captured argv");
    let expected_hls_dir = stream_data_dir
        .join("hls")
        .join(pipeline_id.to_string())
        .join("default");
    assert!(
        argv_line.contains("hls"),
        "expected the ffmpeg argv to request an hls muxer, got: {argv_line}"
    );
    assert!(
        argv_line.contains(&expected_hls_dir.to_string_lossy().to_string()),
        "expected the argv to reference {expected_hls_dir:?}, got: {argv_line}"
    );

    // 2. HlsSink/the orchestrator's registry list this pipeline as running
    // for its community.
    wait_until(TEST_TIMEOUT, || async {
        !registry.list(&COMMUNITY_ID.to_string()).is_empty()
    })
    .await;
    let listed = registry.list(&COMMUNITY_ID.to_string());
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].id, pipeline_id);

    // 3. The public HLS HTTP surface -- built from the *same* registry Arc
    // the orchestrator writes into, exactly like `run_with_shutdown` wires
    // `AppState::hls_router_state` -- lists the pipeline too. This is the
    // end-to-end proof that the router's `RunningPipelines` adapter is
    // actually connected (S13: nothing in this suite previously drove a
    // real HTTP request against the HLS router alongside a real
    // orchestrator/registry; `tests/http_router_assembly.rs` only exercises
    // the default `EmptyRunningPipelines` wiring).
    let hls_router_state = HlsRouterState::new(
        stream_data_dir.clone(),
        registry.clone() as Arc<dyn RunningPipelines>,
    );
    let hls_app = hls_router(hls_router_state);

    let response = hls_app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/live/{COMMUNITY_ID}"))
                .body(AxumBody::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    let pipelines = parsed["pipelines"].as_array().expect("pipelines array");
    assert_eq!(pipelines.len(), 1);
    let entry = &pipelines[0];
    assert_eq!(entry["id"], pipeline_id.to_string());
    assert_eq!(entry["profile"], "default");
    let expected_url = format!("/live/{COMMUNITY_ID}/{pipeline_id}/default/index.m3u8");
    assert_eq!(entry["url"], expected_url);
    assert!(
        entry["started_at"].as_str().is_some(),
        "started_at must be a serialized timestamp, got: {entry}"
    );

    // 4. Write a fixture playlist + segment into the pipeline's real HLS
    // output dir (the same dir `HlsSink::start` created and the disk layout
    // `pipeline::ffmpeg::build_argv`'s HLS argv now targets, see
    // `expected_hls_dir` above) and confirm the file-serving route reads
    // them back with the right content type.
    tokio::fs::write(
        expected_hls_dir.join("master.m3u8"),
        b"#EXTM3U\n#EXT-X-STREAM-INF\nindex.m3u8\n",
    )
    .await
    .expect("write fixture master playlist");
    tokio::fs::write(expected_hls_dir.join("segment_00001.m4s"), vec![0u8; 64])
        .await
        .expect("write fixture segment");
    // The media playlist is written *after* the segment, exactly as ffmpeg's
    // hls muxer does -- the HLS sink treats "listed in the playlist" as
    // "segment complete", which is what unlocks the segment-duration /
    // size / time-to-first-egress histograms.
    tokio::fs::write(
        expected_hls_dir.join("index.m3u8"),
        "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:4\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4.000000,\nsegment_00001.m4s\n",
    )
    .await
    .expect("write fixture media playlist");

    // The HLS poller (50 ms interval here) publishes the first segment's
    // egress timings; wait for it *before* tearing the pipeline down (stop
    // aborts the poller).
    otel_common::wait_for(
        "stream_time_to_first_egress_seconds{protocol=rtmp,egress=hls} to emit",
        TEST_TIMEOUT,
        || {
            otel.histogram(
                "stream_time_to_first_egress_seconds",
                &[("protocol", "rtmp"), ("egress", "hls")],
            )
            .0 >= 1
        },
    )
    .await;

    let playlist_response = hls_app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "/live/{COMMUNITY_ID}/{pipeline_id}/default/master.m3u8"
                ))
                .body(AxumBody::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(playlist_response.status(), StatusCode::OK);
    assert_eq!(
        playlist_response.headers().get(CONTENT_TYPE).unwrap(),
        "application/vnd.apple.mpegurl"
    );

    let segment_response = hls_app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!(
                    "/live/{COMMUNITY_ID}/{pipeline_id}/default/segment_00001.m4s"
                ))
                .body(AxumBody::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(segment_response.status(), StatusCode::OK);
    assert_eq!(
        segment_response.headers().get(CONTENT_TYPE).unwrap(),
        "video/iso.segment"
    );

    // Cleanup: stop the pipeline (kills the fake ffmpeg, tears down HLS),
    // then the seeded temp files.
    orchestrator.stop_pipeline(pipeline_id).await;

    // 5. After teardown the registry -- and therefore the HTTP listing --
    // is empty again (the file-serving route is unaffected: `HlsSink::stop`
    // only removes the on-disk directory after `DEFAULT_CLEANUP_DELAY`, see
    // that constant's doc comment, so this only asserts the listing side).
    let response_after_stop = hls_app
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/live/{COMMUNITY_ID}"))
                .body(AxumBody::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response_after_stop.status(), StatusCode::OK);
    let body_after_stop = response_after_stop
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes();
    let parsed_after_stop: serde_json::Value = serde_json::from_slice(&body_after_stop).unwrap();
    assert_eq!(
        parsed_after_stop["pipelines"].as_array().unwrap().len(),
        0,
        "pipeline must be delisted immediately once stop_pipeline returns"
    );
    // 6. Telemetry gate. The publisher disconnects, which ends the ingest
    // pump (FLV stream EOF) and with it the session: the live-session
    // gauge returns to 0 and the session-lifetime histogram gets its point.
    assert_eq!(
        otel.counter("stream_active_sessions", &[("protocol", "rtmp")]),
        1,
        "exactly one pumped RTMP session is live while the publisher is connected"
    );
    drop(client_session);
    drop(stream);
    otel_common::wait_for(
        "the ingest session to end after the publisher disconnects",
        TEST_TIMEOUT,
        || otel.counter("stream_active_sessions", &[("protocol", "rtmp")]) == 0,
    )
    .await;

    otel.assert_histograms_emitted(&[
        // ingest -> egress latency
        ("stream_ingest_handoff_seconds", &[("protocol", "rtmp")]),
        (
            "stream_time_to_first_egress_seconds",
            &[("protocol", "rtmp"), ("egress", "hls")],
        ),
        // segment duration + size
        ("stream_segment_duration_seconds", &[("variant", "std")]),
        ("stream_segment_size_bytes", &[("variant", "std")]),
        // every pipeline setup/teardown stage the orchestrator + supervisor ran
        ("stream_stage_duration_seconds", &[("stage", "db_connect")]),
        (
            "stream_stage_duration_seconds",
            &[("stage", "config_lookup")],
        ),
        (
            "stream_stage_duration_seconds",
            &[("stage", "tenant_resolve")],
        ),
        ("stream_stage_duration_seconds", &[("stage", "spec_build")]),
        (
            "stream_stage_duration_seconds",
            &[("stage", "egress_start")],
        ),
        (
            "stream_stage_duration_seconds",
            &[("stage", "engine_start")],
        ),
        (
            "stream_stage_duration_seconds",
            &[("stage", "ffmpeg_spawn")],
        ),
        (
            "stream_stage_duration_seconds",
            &[("stage", "ffmpeg_first_progress")],
        ),
        ("stream_stage_duration_seconds", &[("stage", "ffmpeg_stop")]),
        ("stream_stage_duration_seconds", &[("stage", "teardown")]),
        // session lifetime
        ("stream_session_duration_seconds", &[("protocol", "rtmp")]),
        // the pre-existing supervisor histograms still emit alongside
        ("pipeline_start_ms", &[]),
    ]);

    // Value checks: the fixture segment is 64 bytes / 4.000000 s, and the
    // time-to-first-egress is a small positive wall-clock interval.
    assert_eq!(
        otel.histogram("stream_segment_duration_seconds", &[("variant", "std")]),
        (1, 4.0)
    );
    assert_eq!(
        otel.histogram("stream_segment_size_bytes", &[("variant", "std")]),
        (1, 64.0)
    );
    let (first_egress_count, first_egress_sum) = otel.histogram(
        "stream_time_to_first_egress_seconds",
        &[("protocol", "rtmp"), ("egress", "hls")],
    );
    assert_eq!(first_egress_count, 1, "recorded exactly once per pipeline");
    assert!(
        (0.0..TEST_TIMEOUT.as_secs_f64() * 3.0).contains(&first_egress_sum),
        "time-to-first-egress must be a small non-negative interval, got {first_egress_sum}s"
    );
    assert!(
        otel.counter("stream_ingest_bytes_total", &[("protocol", "rtmp")]) > 0,
        "the pump forwarded the FLV header + published chunks to ffmpeg stdin"
    );
    for outcome in ["started", "ended"] {
        assert_eq!(
            otel.counter(
                "stream_sessions_total",
                &[("protocol", "rtmp"), ("outcome", outcome)]
            ),
            1,
            "sessions_total{{outcome={outcome}}}"
        );
    }
    assert_eq!(
        otel.counter("stream_session_failures_total", &[]),
        0,
        "a healthy publish must not count any session failure"
    );

    // Spans: one trace covers listener connection -> orchestrator session
    // -> setup stages / ingest pump, linked across the mpsc channel and the
    // `tokio::spawn` (neither carries tracing context by itself).
    let spans = otel.spans();
    let by_name = |name: &str| {
        spans.iter().find(|s| s.name == name).unwrap_or_else(|| {
            panic!("no `{name}` span exported; got {:?}", {
                let mut names: Vec<_> = spans.iter().map(|s| s.name.to_string()).collect();
                names.sort();
                names.dedup();
                names
            })
        })
    };
    let connection = by_name("rtmp_connection");
    let session_span = by_name("orchestrator.handle_session");
    assert_eq!(
        session_span.parent_span_id,
        connection.span_context.span_id(),
        "the session span must be parented to the listener's connection span"
    );
    assert_eq!(
        session_span.span_context.trace_id(),
        connection.span_context.trace_id(),
        "one trace across the ingest -> orchestrator hop"
    );
    for stage in [
        "pipeline.config_lookup",
        "pipeline.egress_start",
        "pipeline.engine_start",
        "ingest.pump",
    ] {
        let span = by_name(stage);
        assert_eq!(
            span.parent_span_id,
            session_span.span_context.span_id(),
            "{stage} must be a child of the session span"
        );
    }
    let pump_attrs = span_attrs(by_name("ingest.pump"));
    assert!(
        pump_attrs.iter().any(|a| a == "protocol=rtmp"),
        "pump attrs: {pump_attrs:?}"
    );
    let pumped: i64 = pump_attrs
        .iter()
        .find_map(|a| a.strip_prefix("bytes_total=")?.parse().ok())
        .expect("ingest.pump records bytes_total on exit");
    assert!(pumped > 0, "bytes_total={pumped}");
    println!(
        "telemetry: {} span(s) exported; session trace {}",
        spans.len(),
        session_span.span_context.trace_id()
    );

    // PII / secret hygiene: the stream key must not appear in any span
    // attribute, span name, or metric attribute value.
    for span in &spans {
        assert!(
            !span.name.contains(STREAM_KEY)
                && span_attrs(span).iter().all(|a| !a.contains(STREAM_KEY)),
            "stream key leaked into span `{}`: {:?}",
            span.name,
            span_attrs(span)
        );
    }
    for metric in [
        "stream_ingest_handoff_seconds",
        "stream_ingest_bytes_total",
        "stream_time_to_first_egress_seconds",
        "stream_segment_duration_seconds",
        "stream_stage_duration_seconds",
        "stream_session_duration_seconds",
        "stream_sessions_total",
        "stream_active_sessions",
        "pipeline_start_ms",
    ] {
        for point in otel.points(metric) {
            assert!(
                point.attrs.iter().all(|(_, v)| !v.contains(STREAM_KEY)),
                "stream key leaked into metric {metric} attributes: {:?}",
                point.attrs
            );
        }
    }

    tokio::time::sleep(Duration::from_millis(100)).await;
    tokio::fs::remove_dir_all(&stream_data_dir).await.ok();
    tokio::fs::remove_file(&db_path).await.ok();
    tokio::fs::remove_file(&argv_capture).await.ok();
    unsafe { std::env::remove_var("DB_TYPE") };
}

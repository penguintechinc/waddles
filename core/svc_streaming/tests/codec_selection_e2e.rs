//! Codec-selection end to end: `streaming_configs`/`streaming_targets` rows
//! (with the new codec/protocol columns) -> `Orchestrator::handle_session`
//! -> `spec_builder` -> `FfmpegSupervisor` -> a **fake** `ffmpeg` that
//! captures its argv. Proves H.265/AV1 selection is *reachable* from the
//! database all the way to the command line, that an unmuxable choice and
//! an over-cap fan-out are refused loudly (no ffmpeg ever spawned), and
//! that the GPU/CPU encoder selection reaches the argv.
//!
//! The sessions are handed to the orchestrator directly (the RTMP wire
//! handshake is covered by `e2e_rtmp_to_hls_fake.rs`); each scenario uses
//! its own config row, stream key and fake ffmpeg. One test function owns
//! the process-global sqlite connection (`db::get_or_connect` is a
//! `OnceCell`), so scenarios run sequentially against one database.

#![cfg(unix)]

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use sea_orm::ConnectionTrait;
use tokio::sync::mpsc;
use uuid::Uuid;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::egress::hls::HlsSink;
use svc_streaming::egress::relay::RelaySink;
use svc_streaming::ingest::whip::WhipState;
use svc_streaming::ingest::{IngestKind, IngestSession};
use svc_streaming::orchestrator::{Orchestrator, PipelineRegistry};
use svc_streaming::pipeline::{
    EncoderBackend, EncoderSelection, FfmpegSupervisor, PipelineEngine, PipelineError,
    SupervisorConfig, VideoFamily,
};
use svc_streaming::rtc::ingest_auth::InternalIngestAuthClient;
use svc_streaming::rtc::{PeerConnectionFactory, RtcConfig, RtcMetrics};
use svc_streaming::store::DefaultSecretResolver;

const TEST_TIMEOUT: Duration = Duration::from_secs(10);

/// A fake ffmpeg that records its argv, then behaves like a healthy
/// encoder: progress on stderr and a clean exit on stdin `q`.
fn fake_ffmpeg(argv_capture: &Path) -> PathBuf {
    let script_path = std::env::temp_dir().join(format!(
        "svc-streaming-codec-e2e-ffmpeg-{}.sh",
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
    std::fs::write(&script_path, body).expect("write fake ffmpeg");
    let mut perms = std::fs::metadata(&script_path).unwrap().permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&script_path, perms).unwrap();
    script_path
}

async fn wait_for_file(path: &Path) -> String {
    let deadline = tokio::time::Instant::now() + TEST_TIMEOUT;
    loop {
        if let Ok(text) = tokio::fs::read_to_string(path).await {
            if !text.is_empty() {
                return text;
            }
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "{path:?} never appeared"
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn whip_state(config: &Config) -> Arc<WhipState> {
    let rtc_config = RtcConfig::from_config(config).expect("valid webrtc_udp_range");
    let factory = Arc::new(PeerConnectionFactory::new(rtc_config).expect("factory builds"));
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).expect("registers once");
    let authorizer = Arc::new(InternalIngestAuthClient::new(config).expect("client builds"));
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

/// One scenario's orchestrator wired to its own fake ffmpeg, plus the
/// handles the assertions need.
struct Harness {
    orchestrator: Arc<Orchestrator>,
    supervisor: Arc<FfmpegSupervisor>,
    ingest_tx: mpsc::Sender<IngestSession>,
    argv_capture: PathBuf,
}

fn harness(config: &Config, data_dir: &Path, encoders: EncoderSelection) -> Harness {
    let argv_capture = std::env::temp_dir().join(format!(
        "svc-streaming-codec-e2e-argv-{}.txt",
        Uuid::new_v4()
    ));
    let supervisor = Arc::new(
        FfmpegSupervisor::new(
            fake_ffmpeg(&argv_capture),
            data_dir.to_path_buf(),
            45600,
            Arc::new(DefaultSecretResolver),
            SupervisorConfig::default(),
        )
        .with_encoders(encoders),
    );
    let prom = prometheus::Registry::new();
    let orchestrator = Arc::new(Orchestrator::new(
        config.clone(),
        supervisor.clone(),
        Arc::new(HlsSink::new(data_dir.to_path_buf(), &prom)),
        Arc::new(RelaySink::new()),
        None,
        Arc::new(PipelineRegistry::new()),
        whip_state(config),
    ));
    let (ingest_tx, ingest_rx) = mpsc::channel(4);
    tokio::spawn(Arc::clone(&orchestrator).run(ingest_rx));
    Harness {
        orchestrator,
        supervisor,
        ingest_tx,
        argv_capture,
    }
}

impl Harness {
    /// Publishes `key`; the returned writer keeps the (never-ending) ingest
    /// stream open -- dropping it is the publisher disconnecting.
    async fn publish(&self, key: &str) -> tokio::io::DuplexStream {
        let (reader, writer) = tokio::io::duplex(1024);
        self.ingest_tx
            .send(IngestSession {
                kind: IngestKind::Rtmp,
                key: key.to_string(),
                stream: Box::new(reader),
            })
            .await
            .expect("orchestrator accepts the session");
        writer
    }
}

fn argv_has(argv: &str, needle: &str) -> bool {
    argv.split_whitespace().any(|a| a == needle)
}

fn secret_json(var: &str) -> String {
    serde_json::to_string(&svc_streaming::store::SecretRef::Env { var: var.into() })
        .unwrap()
        .replace('\'', "''")
}

#[tokio::test]
async fn codec_selection_reaches_the_ffmpeg_command_line_and_bad_choices_never_spawn() {
    // SAFETY: the only test in this process, set before any await point.
    unsafe {
        std::env::set_var("DB_TYPE", "sqlite");
        std::env::set_var("CODEC_E2E_RTMP_URL", "rtmps://live.example.com/app/rtmpkey");
        std::env::set_var(
            "CODEC_E2E_SRT_URL",
            "srt://srt.example.com:9000?streamid=srtkey",
        );
    }

    let data_dir = std::env::temp_dir().join(format!("svc-streaming-codec-e2e-{}", Uuid::new_v4()));
    let db_path = std::env::temp_dir().join(format!(
        "svc-streaming-codec-e2e-db-{}-{}.sqlite",
        std::process::id(),
        Uuid::new_v4()
    ));
    let cli = CliConfig::try_parse_from([
        "svc-streaming",
        "--db-name",
        &db_path.to_string_lossy(),
        "--stream-data-dir",
        &data_dir.to_string_lossy(),
        "--webrtc-udp-range",
        "45600-45620",
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
        .expect("sqlite connection");
    let rtmp = secret_json("CODEC_E2E_RTMP_URL");
    let srt = secret_json("CODEC_E2E_SRT_URL");
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
            transcode_bitrate_kbps INTEGER NOT NULL DEFAULT 4000,
            video_codec TEXT NOT NULL DEFAULT 'h264',
            audio_codec TEXT NOT NULL DEFAULT 'copy'
        );
        CREATE TABLE streaming_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            forward_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            protocol TEXT NOT NULL DEFAULT 'rtmp',
            video_codec TEXT,
            audio_codec TEXT
        );
        INSERT INTO tenants (id, slug) VALUES (1, 'tenant-1');
        INSERT INTO communities (id, tenant_id) VALUES (101, 1), (102, 1), (103, 1), (104, 1);

        -- 1: H.265 + AAC. HLS and the SRT target share the H.265 encode; the
        --    RTMP target (rtmps://) cannot carry HEVC so it rides an H.264 rung.
        INSERT INTO streaming_configs (id, community_id, source_url, transcode_enabled, video_codec, audio_codec)
            VALUES (1, 101, 'key_h265', 1, 'h265', 'aac');
        INSERT INTO streaming_targets (config_id, platform, forward_url, protocol) VALUES (1, 'custom', '{rtmp}', 'rtmp');
        INSERT INTO streaming_targets (config_id, platform, forward_url, protocol) VALUES (1, 'custom', '{srt}', 'srt');

        -- 2: AV1 HLS only, no push targets.
        INSERT INTO streaming_configs (id, community_id, source_url, transcode_enabled, video_codec)
            VALUES (2, 102, 'key_av1', 1, 'av1');

        -- 3: AV1 + an SRT target that inherits it: MPEG-TS cannot carry AV1.
        INSERT INTO streaming_configs (id, community_id, source_url, transcode_enabled, video_codec)
            VALUES (3, 103, 'key_av1_srt', 1, 'av1');
        INSERT INTO streaming_targets (config_id, platform, forward_url, protocol) VALUES (3, 'custom', '{srt}', 'srt');

        -- 4: five destinations -- over the Free-tier cap of three.
        INSERT INTO streaming_configs (id, community_id, source_url) VALUES (4, 104, 'key_fanout');
        INSERT INTO streaming_targets (config_id, platform, forward_url) VALUES (4, 'custom', '{rtmp}');
        INSERT INTO streaming_targets (config_id, platform, forward_url) VALUES (4, 'custom', '{rtmp}');
        INSERT INTO streaming_targets (config_id, platform, forward_url, protocol) VALUES (4, 'custom', '{srt}', 'srt');
        INSERT INTO streaming_targets (config_id, platform, forward_url, protocol) VALUES (4, 'custom', '{srt}', 'srt');
        INSERT INTO streaming_targets (config_id, platform, forward_url) VALUES (4, 'custom', '{rtmp}');
        "#
    ))
    .await
    .expect("seed database");

    // Every fake-ffmpeg script is written before the first process spawns:
    // writing an executable and exec'ing it while another thread forks
    // fails with ETXTBSY.
    let cpu = harness(&config, &data_dir, EncoderSelection::software());
    let gpu = harness(
        &config,
        &data_dir,
        EncoderSelection::software()
            .with(VideoFamily::H264, EncoderBackend::Nvenc)
            .with(VideoFamily::H265, EncoderBackend::Nvenc),
    );
    let av1 = harness(&config, &data_dir, EncoderSelection::software());
    let bad = harness(&config, &data_dir, EncoderSelection::software());
    let fanout = harness(&config, &data_dir, EncoderSelection::software());

    // --- 1. H.265 + SRT + an H.264 RTMP rung, on the CPU encoders --------
    let _publisher = cpu.publish("key_h265").await;
    let argv = wait_for_file(&cpu.argv_capture).await;
    assert!(
        argv_has(&argv, "libx265"),
        "H.265 CPU encode missing: {argv}"
    );
    assert!(
        argv_has(&argv, "hvc1"),
        "HLS players need the hvc1 tag: {argv}"
    );
    assert!(
        argv_has(&argv, "libx264"),
        "RTMP rung must stay H.264: {argv}"
    );
    assert!(argv.contains("-filter_complex [0:v]split=2"), "{argv}");
    assert!(
        argv.contains("[f=hls:hls_time=4"),
        "full HLS tee slave: {argv}"
    );
    assert!(
        argv.contains("[f=mpegts:onfail=ignore]srt://srt.example.com:9000?streamid=srtkey"),
        "SRT target wired from the DB: {argv}"
    );
    assert!(
        argv.contains("[f=flv:onfail=ignore]rtmps://live.example.com/app/rtmpkey|[f=null]-"),
        "rtmps:// accepted and the lone push isolated: {argv}"
    );
    assert!(!argv.contains("nvenc") && !argv.contains("vaapi"), "{argv}");
    cpu.orchestrator.stop_pipeline(Uuid::from_u128(1)).await;

    // --- 1b. same rows, but the host proved NVENC for H.264/H.265 --------
    let _publisher = gpu.publish("key_h265").await;
    let argv = wait_for_file(&gpu.argv_capture).await;
    assert!(argv_has(&argv, "hevc_nvenc"), "GPU H.265 missing: {argv}");
    assert!(
        argv_has(&argv, "h264_nvenc"),
        "GPU H.264 rung missing: {argv}"
    );
    assert!(
        !argv_has(&argv, "libx265") && !argv_has(&argv, "libx264"),
        "{argv}"
    );
    gpu.orchestrator.stop_pipeline(Uuid::from_u128(1)).await;

    // --- 2. AV1 HLS -------------------------------------------------------
    let _publisher = av1.publish("key_av1").await;
    let argv = wait_for_file(&av1.argv_capture).await;
    assert!(argv_has(&argv, "libsvtav1"), "AV1 HLS missing: {argv}");
    assert!(argv_has(&argv, "hls"), "{argv}");
    av1.orchestrator.stop_pipeline(Uuid::from_u128(2)).await;

    // --- 3. AV1 inherited by an SRT target: refused, ffmpeg never spawns --
    let _publisher = bad.publish("key_av1_srt").await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(
        !bad.argv_capture.exists(),
        "an unmuxable codec choice must never reach ffmpeg"
    );
    assert!(matches!(
        bad.supervisor.status(Uuid::from_u128(3)).await,
        Err(PipelineError::NotFound(_))
    ));

    // --- 4. Over the destination cap: refused in the argv path ------------
    let _publisher = fanout.publish("key_fanout").await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(
        !fanout.argv_capture.exists(),
        "five destinations on the Free tier must not spawn ffmpeg"
    );
    assert!(matches!(
        fanout.supervisor.status(Uuid::from_u128(4)).await,
        Err(PipelineError::NotFound(_))
    ));

    std::fs::remove_dir_all(&data_dir).ok();
    std::fs::remove_file(&db_path).ok();
}

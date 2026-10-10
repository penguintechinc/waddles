//! Fail-loud telemetry for the ingest -> pipeline path: an accepted ingest
//! session that cannot become a running pipeline must surface as a counted,
//! reasoned failure (`stream_session_failures_total{protocol,reason}`) and
//! must never be recorded as a started session -- not a silent drop that
//! only a log line betrays.
//!
//! Drives the real [`Orchestrator`] by pushing [`IngestSession`]s straight
//! onto its channel (the listeners are not involved), one session per
//! failure reason, then reads the in-memory OTel sink (`tests/otel_common`)
//! and asserts each reason's count (denominator reported, not just "no
//! errors"). Phase 1 runs with an unusable database URL (`no_database`);
//! phase 2 switches to a seeded sqlite DB for the remaining reasons.

#![cfg(unix)]

mod otel_common;

use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use sea_orm::ConnectionTrait;
use tokio::sync::mpsc;
use uuid::Uuid;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::egress::hls::{HlsSink, DEFAULT_CLEANUP_DELAY};
use svc_streaming::egress::relay::RelaySink;
use svc_streaming::ingest::whip::WhipState;
use svc_streaming::ingest::{IngestKind, IngestSession};
use svc_streaming::orchestrator::{Orchestrator, PipelineRegistry};
use svc_streaming::pipeline::{FfmpegSupervisor, SupervisorConfig};
use svc_streaming::rtc::ingest_auth::InternalIngestAuthClient;
use svc_streaming::rtc::{PeerConnectionFactory, RtcConfig, RtcMetrics};
use svc_streaming::store::DefaultSecretResolver;

const TEST_TIMEOUT: Duration = Duration::from_secs(10);

/// A session whose byte stream is already at EOF -- none of the failure
/// paths ever reads it.
fn session(kind: IngestKind, key: &str) -> IngestSession {
    IngestSession {
        kind,
        key: key.to_string(),
        stream: Box::new(tokio::io::empty()),
        span: tracing::Span::none(),
    }
}

fn whip_state(config: &Config) -> Arc<WhipState> {
    let factory = Arc::new(
        PeerConnectionFactory::new(RtcConfig::from_config(config).expect("udp range"))
            .expect("factory builds"),
    );
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

fn protocol_of(kind: IngestKind) -> &'static str {
    match kind {
        IngestKind::Rtmp => "rtmp",
        IngestKind::Srt => "srt",
        IngestKind::Whip => "whip",
    }
}

#[tokio::test]
async fn every_session_failure_reason_is_counted_and_none_is_recorded_as_started() {
    let otel = otel_common::OtelSink::install();

    // SAFETY: the only test in this file/process, before any await point
    // that could race another thread's env access.
    unsafe {
        std::env::remove_var("DB_TYPE");
        std::env::remove_var("SVC_STREAMING_FAILURE_TEST_UNSET_VAR");
    }
    let stream_data_dir = std::env::temp_dir().join(format!(
        "svc-streaming-failure-telemetry-{}",
        Uuid::new_v4()
    ));
    let db_path = std::env::temp_dir().join(format!(
        "svc-streaming-failure-telemetry-{}-{}.sqlite",
        std::process::id(),
        Uuid::new_v4()
    ));
    // `--db-host` holds a space: the postgres URL fails to parse, a
    // deterministic, instant `DbErr` (no network, no connect-retry wait).
    let cli = CliConfig::try_parse_from([
        "svc-streaming",
        "--db-host",
        "not a host",
        "--db-name",
        &db_path.to_string_lossy(),
        "--stream-data-dir",
        &stream_data_dir.to_string_lossy(),
        "--ffmpeg-path",
        "/nonexistent/ffmpeg-for-failure-telemetry",
        "--webrtc-udp-range",
        "45520-45530",
    ])
    .expect("valid cli assembly");
    let config = Config {
        cli,
        db_password: Secret::new("unused"),
        cache_password: None,
        service_api_key: Secret::new("unused"),
        jwt_hmac_secret: None,
    };

    let prom = prometheus::Registry::new();
    let supervisor = Arc::new(FfmpegSupervisor::new(
        "/nonexistent/ffmpeg-for-failure-telemetry".into(),
        stream_data_dir.clone(),
        45520,
        Arc::new(DefaultSecretResolver),
        SupervisorConfig::default(),
    ));
    let hls = Arc::new(HlsSink::with_intervals(
        stream_data_dir.clone(),
        &prom,
        Duration::from_millis(50),
        DEFAULT_CLEANUP_DELAY,
    ));
    let orchestrator = Arc::new(Orchestrator::new(
        config.clone(),
        supervisor,
        hls,
        Arc::new(RelaySink::new()),
        None,
        Arc::new(PipelineRegistry::new()),
        whip_state(&config),
    ));
    let (tx, rx) = mpsc::channel(8);
    tokio::spawn(Arc::clone(&orchestrator).run(rx));

    // --- Phase 1: no usable database -> `no_database`.
    tx.send(session(IngestKind::Rtmp, "sk_no_db"))
        .await
        .expect("orchestrator is running");
    otel_common::wait_for(
        "the no_database failure to be counted",
        TEST_TIMEOUT,
        || {
            otel.counter(
                "stream_session_failures_total",
                &[("protocol", "rtmp"), ("reason", "no_database")],
            ) == 1
        },
    )
    .await;

    // --- Phase 2: switch to a seeded sqlite DB (the connection factory
    // re-reads `DB_TYPE` and retries after a failed connect).
    unsafe { std::env::set_var("DB_TYPE", "sqlite") };
    let db = svc_streaming::db::get_or_connect(&config)
        .await
        .expect("lazy sqlite connection establishes");
    db.execute_unprepared(
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
        -- Communities 42-44 exist; 99 deliberately has no `communities` row.
        INSERT INTO communities (id, tenant_id) VALUES (42, 1), (43, 1), (44, 1);
        -- tenant_unresolved: config points at a community that is not there.
        INSERT INTO streaming_configs (id, community_id, source_url) VALUES (1, 99, 'sk_orphan_community');
        -- spec_build_failed: a target whose forward_url is not a secret_ref.
        INSERT INTO streaming_configs (id, community_id, source_url) VALUES (2, 42, 'sk_bad_target');
        INSERT INTO streaming_targets (config_id, platform, forward_url) VALUES (2, 'twitch', 'not-a-secret-ref');
        -- whip_sdp_missing: a healthy config, but no WHIP bridge was registered.
        INSERT INTO streaming_configs (id, community_id, source_url) VALUES (3, 43, 'sk_whip_no_bridge');
        -- engine_start_failed: a well-formed secret_ref whose env var is unset,
        -- so the supervisor cannot resolve the relay target's URL.
        INSERT INTO streaming_configs (id, community_id, source_url) VALUES (4, 44, 'sk_unresolvable_secret');
        INSERT INTO streaming_targets (config_id, platform, forward_url)
            VALUES (4, 'twitch', '{"source":"env","var":"SVC_STREAMING_FAILURE_TEST_UNSET_VAR"}');
        "#,
    )
    .await
    .expect("seed schema");

    let cases = [
        (IngestKind::Rtmp, "sk_unknown_key", "config_not_found"),
        (IngestKind::Srt, "sk_orphan_community", "tenant_unresolved"),
        (IngestKind::Rtmp, "sk_bad_target", "spec_build_failed"),
        (IngestKind::Whip, "sk_whip_no_bridge", "whip_sdp_missing"),
        (
            IngestKind::Rtmp,
            "sk_unresolvable_secret",
            "engine_start_failed",
        ),
    ];
    for (kind, key, _) in &cases {
        tx.send(session(*kind, key))
            .await
            .expect("orchestrator is running");
    }

    // +1: the phase-1 `no_database` failure is already counted.
    let expected_total = cases.len() as i64 + 1;
    otel_common::wait_for("every session failure to be counted", TEST_TIMEOUT, || {
        otel.counter("stream_session_failures_total", &[]) >= expected_total
    })
    .await;

    let total = otel.counter("stream_session_failures_total", &[]);
    println!("telemetry: stream_session_failures_total = {total} across {expected_total} sessions");
    assert_eq!(total, expected_total, "exactly one failure per bad session");
    for (kind, _, reason) in &cases {
        assert_eq!(
            otel.counter(
                "stream_session_failures_total",
                &[("protocol", protocol_of(*kind)), ("reason", reason)]
            ),
            1,
            "{}/{reason}",
            protocol_of(*kind)
        );
    }

    // Fail loud, not silent: none of them may look like a started session.
    assert_eq!(
        otel.counter("stream_sessions_total", &[("outcome", "started")]),
        0,
        "a failed session must never be counted as started"
    );
    assert_eq!(otel.counter("stream_active_sessions", &[]), 0);

    // The stages that *did* run before each failure were still timed.
    otel.assert_histograms_emitted(&[
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
        ("stream_stage_duration_seconds", &[("stage", "teardown")]),
    ]);

    // No routing key (a credential) in any failure counter attribute.
    for point in otel.points("stream_session_failures_total") {
        assert!(
            point.attrs.iter().all(|(_, v)| !v.starts_with("sk_")),
            "routing key leaked into attributes: {:?}",
            point.attrs
        );
    }

    tokio::fs::remove_dir_all(&stream_data_dir).await.ok();
    tokio::fs::remove_file(&db_path).await.ok();
    unsafe { std::env::remove_var("DB_TYPE") };
}

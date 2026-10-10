//! Integration coverage for `RelaySink` secret resolution through the S1
//! `SecretResolver` mechanism (env + file) and the redaction guarantee on
//! every accessor that touches a resolved relay target URL -- see
//! `svc_streaming::egress::relay::target` module docs.

use tokio::sync::Mutex;

use svc_streaming::egress::relay::RelaySink;
use svc_streaming::egress::{OutputSink, SinkError};
use svc_streaming::pipeline::model::OutputSpec;
use svc_streaming::store::SecretRef;
use uuid::Uuid;

// std::env is process-global; serialize env-mutating tests in this binary
// the same way src/config.rs / src/store/secrets.rs do. `tokio::sync::Mutex`
// (not `std::sync::Mutex`) -- its guard is `Send` and safe to hold across
// an `.await`, which this file's tests need to do (S12 clippy fix:
// `await_holding_lock` -- see `src/db/mod.rs`'s own test module for the
// identical pattern).
static ENV_LOCK: Mutex<()> = Mutex::const_new(());

#[tokio::test]
async fn resolves_an_rtmp_target_from_an_env_secret() {
    let _guard = ENV_LOCK.lock().await;
    // SAFETY: serialized by ENV_LOCK.
    unsafe {
        std::env::set_var(
            "SVC_STREAMING_RELAY_TEST_ENV",
            "rtmp://ingest.example.com/app/sk_env_secret",
        );
    }

    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    let spec = OutputSpec::RtmpPush {
        url_secret_ref: SecretRef::Env {
            var: "SVC_STREAMING_RELAY_TEST_ENV".to_string(),
        },
        profile: None,
    };
    sink.start(pipeline_id, spec)
        .await
        .expect("resolves from env");

    let args = sink
        .ffmpeg_output_args(pipeline_id)
        .await
        .expect("exactly one target");
    assert_eq!(
        args,
        vec![
            "-f".to_string(),
            "flv".to_string(),
            "rtmp://ingest.example.com/app/sk_env_secret".to_string(),
        ]
    );

    // SAFETY: serialized by ENV_LOCK.
    unsafe { std::env::remove_var("SVC_STREAMING_RELAY_TEST_ENV") };
}

#[tokio::test]
async fn resolves_an_srt_target_from_a_mounted_secret_file() {
    let dir = std::env::temp_dir();
    let path = dir.join(format!(
        "svc-streaming-relay-secret-test-{}-{}",
        std::process::id(),
        "srt_file"
    ));
    std::fs::write(
        &path,
        "srt://ingest.example.com:9000?streamid=sk_file_secret\n",
    )
    .unwrap();

    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    let spec = OutputSpec::SrtPush {
        url_secret_ref: SecretRef::File {
            path: path.to_string_lossy().to_string(),
        },
        profile: None,
    };
    sink.start(pipeline_id, spec)
        .await
        .expect("resolves from file");

    let args = sink
        .ffmpeg_output_args(pipeline_id)
        .await
        .expect("exactly one target");
    assert_eq!(args[0], "-f");
    assert_eq!(args[1], "mpegts");
    assert_eq!(
        args[2],
        "srt://ingest.example.com:9000?streamid=sk_file_secret"
    );

    std::fs::remove_file(&path).ok();
}

#[tokio::test]
async fn missing_env_secret_surfaces_as_a_sink_error() {
    let _guard = ENV_LOCK.lock().await;
    // SAFETY: serialized by ENV_LOCK.
    unsafe { std::env::remove_var("SVC_STREAMING_RELAY_TEST_MISSING") };

    let sink: RelaySink = RelaySink::new();
    let spec = OutputSpec::RtmpPush {
        url_secret_ref: SecretRef::Env {
            var: "SVC_STREAMING_RELAY_TEST_MISSING".to_string(),
        },
        profile: None,
    };
    let err = sink.start(Uuid::new_v4(), spec).await.unwrap_err();
    assert!(matches!(err, SinkError::Other(_)));
}

#[tokio::test]
async fn missing_secret_file_surfaces_as_a_sink_error() {
    let sink: RelaySink = RelaySink::new();
    let spec = OutputSpec::SrtPush {
        url_secret_ref: SecretRef::File {
            path: "/nonexistent/path/for/svc-streaming-relay-tests".to_string(),
        },
        profile: None,
    };
    let err = sink.start(Uuid::new_v4(), spec).await.unwrap_err();
    assert!(matches!(err, SinkError::Other(_)));
}

#[tokio::test]
async fn a_file_resolved_secret_never_appears_in_tee_slave_debug_or_display() {
    let dir = std::env::temp_dir();
    let path = dir.join(format!(
        "svc-streaming-relay-secret-test-{}-{}",
        std::process::id(),
        "redact_check"
    ));
    std::fs::write(
        &path,
        "rtmp://ingest.example.com/app/sk_should_never_appear\n",
    )
    .unwrap();

    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(
        pipeline_id,
        OutputSpec::RtmpPush {
            url_secret_ref: SecretRef::File {
                path: path.to_string_lossy().to_string(),
            },
            profile: None,
        },
    )
    .await
    .expect("resolves from file");

    let slaves = sink.tee_slaves(pipeline_id).await;
    let debugged = format!("{:?}", slaves[0]);
    let displayed = format!("{}", slaves[0]);
    assert!(!debugged.contains("sk_should_never_appear"));
    assert!(!displayed.contains("sk_should_never_appear"));
    assert!(debugged.contains("****"));
    assert!(displayed.contains("****"));

    std::fs::remove_file(&path).ok();
}

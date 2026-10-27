//! Integration coverage for `RelaySink::observe_stderr_line`: classifying
//! ffmpeg `-f tee` stderr into a bounded `FailureReasonKind`, mapping it
//! back to the right target via the `Output #<N>` index, and recording
//! `relay_target_failures_total{reason}` / `relay_targets_active`.

use svc_streaming::egress::relay::{
    register_relay_metrics, FailureReasonKind, RelaySink, TargetHealth,
};
use svc_streaming::egress::OutputSink;
use svc_streaming::pipeline::model::OutputSpec;
use svc_streaming::store::SecretRef;
use uuid::Uuid;

struct EnvVarGuard(&'static str);

impl EnvVarGuard {
    fn set(name: &'static str, value: &str) -> Self {
        // SAFETY: each test in this file uses a distinct env var name.
        unsafe { std::env::set_var(name, value) };
        Self(name)
    }
}

impl Drop for EnvVarGuard {
    fn drop(&mut self) {
        // SAFETY: see `set`.
        unsafe { std::env::remove_var(self.0) };
    }
}

fn rtmp_spec(var: &str) -> OutputSpec {
    OutputSpec::RtmpPush {
        url_secret_ref: SecretRef::Env {
            var: var.to_string(),
        },
        profile: None,
    }
}

fn srt_spec(var: &str) -> OutputSpec {
    OutputSpec::SrtPush {
        url_secret_ref: SecretRef::Env {
            var: var.to_string(),
        },
        profile: None,
    }
}

#[tokio::test]
async fn connection_refused_marks_the_indexed_target_failing() {
    let guard_a = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_HEALTH_A",
        "rtmp://a.example.com/app/k1",
    );
    let guard_b = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_HEALTH_B",
        "srt://b.example.com:9000?streamid=k2",
    );

    let registry = prometheus::Registry::new();
    let metrics = register_relay_metrics(&registry).unwrap();
    let sink: RelaySink = RelaySink::new().with_metrics(metrics.clone());
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_HEALTH_A"))
        .await
        .unwrap();
    sink.start(pipeline_id, srt_spec("SVC_STREAMING_RELAY_HEALTH_B"))
        .await
        .unwrap();

    sink.observe_stderr_line(
        pipeline_id,
        "[tee @ 0x1] Output #1, mpegts, to 'srt://b.example.com:9000?streamid=k2': Connection refused",
    )
    .await;

    let health = sink.target_health(pipeline_id).await;
    assert_eq!(health[0], TargetHealth::Active);
    assert_eq!(
        health[1],
        TargetHealth::Failing(FailureReasonKind::ConnectionRefused)
    );

    let failure_count = metrics
        .relay_target_failures_total
        .with_label_values(&["destination refused connection"])
        .get();
    assert_eq!(failure_count, 1);

    drop(guard_a);
    drop(guard_b);
}

#[tokio::test]
async fn dns_lookup_failure_is_classified_correctly() {
    let guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_HEALTH_DNS",
        "rtmp://bad.invalid/app/k1",
    );
    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_HEALTH_DNS"))
        .await
        .unwrap();

    sink.observe_stderr_line(
        pipeline_id,
        "[tcp @ 0x1] Output #0, flv, to 'rtmp://bad.invalid/app/k1': Name or service not known",
    )
    .await;

    let health = sink.target_health(pipeline_id).await;
    assert_eq!(
        health[0],
        TargetHealth::Failing(FailureReasonKind::DnsLookupFailed)
    );
    drop(guard);
}

#[tokio::test]
async fn stream_key_rejection_is_classified_correctly() {
    let _guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_HEALTH_KEY",
        "rtmp://ingest.example.com/app/bad_key",
    );
    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_HEALTH_KEY"))
        .await
        .unwrap();

    sink.observe_stderr_line(pipeline_id, "Output #0, flv, to 'rtmp://ingest.example.com/app/bad_key': Server error: NetStream.Publish.BadName")
        .await;

    let health = sink.target_health(pipeline_id).await;
    assert_eq!(
        health[0],
        TargetHealth::Failing(FailureReasonKind::StreamKeyRejected)
    );
}

#[tokio::test]
async fn progress_lines_leave_target_health_active() {
    let guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_HEALTH_PROGRESS",
        "rtmp://a.example.com/app/k1",
    );
    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(
        pipeline_id,
        rtmp_spec("SVC_STREAMING_RELAY_HEALTH_PROGRESS"),
    )
    .await
    .unwrap();

    sink.observe_stderr_line(
        pipeline_id,
        "frame=  120 fps= 30 q=-1.0 size=  512kB time=00:00:04.00 bitrate=1024.0kbits/s",
    )
    .await;

    assert_eq!(
        sink.target_health(pipeline_id).await,
        vec![TargetHealth::Active]
    );
    drop(guard);
}

#[tokio::test]
async fn observe_stderr_line_on_an_untracked_pipeline_is_a_no_op() {
    let sink: RelaySink = RelaySink::new();
    sink.observe_stderr_line(Uuid::new_v4(), "Connection refused")
        .await;
    assert!(sink.target_health(Uuid::new_v4()).await.is_empty());
}

#[tokio::test]
async fn record_bytes_increments_the_target_labeled_counter() {
    let guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_HEALTH_BYTES",
        "rtmp://a.example.com/app/k1",
    );
    let registry = prometheus::Registry::new();
    let metrics = register_relay_metrics(&registry).unwrap();
    let sink: RelaySink = RelaySink::new().with_metrics(metrics.clone());
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_HEALTH_BYTES"))
        .await
        .unwrap();

    sink.record_bytes(pipeline_id, 0, 4096).await;

    let value = metrics
        .relay_bytes_total
        .with_label_values(&["rtmp://a.example.com/app/****"])
        .get();
    assert_eq!(value, 4096);
    drop(guard);
}

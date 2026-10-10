//! Integration coverage for the ffmpeg output-argument fragments
//! `RelaySink` produces: single-target `-f <mux> <url>` via
//! `ffmpeg_output_args`, and multi-target `-f tee` slave grouping via
//! `tee_slaves` -- spec `docs/plans/2026-09-11-svc-streaming-pipeline-
//! matrix.md` §4.

use svc_streaming::egress::relay::{RelayError, RelaySink};
use svc_streaming::egress::OutputSink;
use svc_streaming::pipeline::model::OutputSpec;
use svc_streaming::store::SecretRef;
use uuid::Uuid;

fn rtmp_spec(url: &str) -> OutputSpec {
    // Env-var indirection matches how a real PipelineSpec never carries a
    // raw secret; the value below is set just-in-time per test.
    OutputSpec::RtmpPush {
        url_secret_ref: SecretRef::Env {
            var: url.to_string(),
        },
        profile: None,
    }
}

fn srt_spec(url: &str) -> OutputSpec {
    OutputSpec::SrtPush {
        url_secret_ref: SecretRef::Env {
            var: url.to_string(),
        },
        profile: None,
    }
}

struct EnvVarGuard(&'static str);

impl EnvVarGuard {
    fn set(name: &'static str, value: &str) -> Self {
        // SAFETY: each test in this file uses a distinct env var name, so
        // there is no cross-test mutation even without a shared lock.
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

#[tokio::test]
async fn single_rtmp_target_returns_flv_output_args() {
    let _guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_FRAG_RTMP",
        "rtmp://ingest.example.com/app/sk_abc",
    );
    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_FRAG_RTMP"))
        .await
        .unwrap();

    let args = sink.ffmpeg_output_args(pipeline_id).await.unwrap();
    assert_eq!(
        args,
        vec![
            "-f".to_string(),
            "flv".to_string(),
            "rtmp://ingest.example.com/app/sk_abc".to_string(),
        ]
    );
}

#[tokio::test]
async fn single_srt_target_returns_mpegts_output_args() {
    let _guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_FRAG_SRT",
        "srt://ingest.example.com:9000?streamid=sk_abc&latency=120",
    );
    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, srt_spec("SVC_STREAMING_RELAY_FRAG_SRT"))
        .await
        .unwrap();

    let args = sink.ffmpeg_output_args(pipeline_id).await.unwrap();
    assert_eq!(args[0], "-f");
    assert_eq!(args[1], "mpegts");
    assert_eq!(
        args[2],
        "srt://ingest.example.com:9000?streamid=sk_abc&latency=120"
    );
}

#[tokio::test]
async fn no_active_targets_errors_instead_of_returning_empty_args() {
    let sink: RelaySink = RelaySink::new();
    let err = sink.ffmpeg_output_args(Uuid::new_v4()).await.unwrap_err();
    assert!(matches!(err, RelayError::NoActiveTargets(_)));
}

#[tokio::test]
async fn two_targets_sharing_a_pipeline_group_into_ordered_tee_slaves() {
    let guard_a = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_FRAG_TEE_A",
        "rtmp://a.example.com/app/k1",
    );
    let guard_b = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_FRAG_TEE_B",
        "srt://b.example.com:9000?streamid=k2",
    );

    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_FRAG_TEE_A"))
        .await
        .unwrap();
    sink.start(pipeline_id, srt_spec("SVC_STREAMING_RELAY_FRAG_TEE_B"))
        .await
        .unwrap();

    // A pipeline with >1 relay target must use tee_slaves(), not the
    // single-target ffmpeg_output_args() path.
    let err = sink.ffmpeg_output_args(pipeline_id).await.unwrap_err();
    assert!(matches!(err, RelayError::MultipleTargets(_, 2)));

    let slaves = sink.tee_slaves(pipeline_id).await;
    assert_eq!(slaves.len(), 2);
    assert_eq!(slaves[0].format, "flv");
    assert_eq!(slaves[1].format, "mpegts");
    assert_eq!(
        slaves[0].tee_fragment_unredacted(),
        "[f=flv:onfail=ignore]rtmp://a.example.com/app/k1"
    );
    assert_eq!(
        slaves[1].tee_fragment_unredacted(),
        "[f=mpegts:onfail=ignore]srt://b.example.com:9000?streamid=k2"
    );

    drop(guard_a);
    drop(guard_b);
}

#[tokio::test]
async fn tee_slaves_is_empty_for_an_untracked_pipeline() {
    let sink: RelaySink = RelaySink::new();
    assert!(sink.tee_slaves(Uuid::new_v4()).await.is_empty());
}

#[tokio::test]
async fn stop_clears_active_targets_so_fragments_and_slaves_reset() {
    let guard = EnvVarGuard::set(
        "SVC_STREAMING_RELAY_FRAG_STOP",
        "rtmp://a.example.com/app/k1",
    );
    let sink: RelaySink = RelaySink::new();
    let pipeline_id = Uuid::new_v4();
    sink.start(pipeline_id, rtmp_spec("SVC_STREAMING_RELAY_FRAG_STOP"))
        .await
        .unwrap();
    assert_eq!(sink.tee_slaves(pipeline_id).await.len(), 1);

    sink.stop(pipeline_id).await.unwrap();

    assert!(sink.tee_slaves(pipeline_id).await.is_empty());
    let err = sink.ffmpeg_output_args(pipeline_id).await.unwrap_err();
    assert!(matches!(err, RelayError::NoActiveTargets(_)));

    drop(guard);
}

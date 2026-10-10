//! `FfmpegSupervisor` lifecycle integration tests against a fake `ffmpeg`
//! binary (a tiny POSIX shell script standing in for the real thing) --
//! start/status/stop, restart-on-crash with backoff, stall detection, and
//! graceful-stop timeout escalation (stdin EOF -> SIGTERM -> SIGKILL), per
//! `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §6. The
//! teardown-specific regression suite (process-group sweep, no `kill`
//! binary, runtime never wedged) lives in `tests/pipeline_teardown*.rs`.
//!
//! Unix-only: the fake binaries are POSIX `sh` scripts and the stop-timeout
//! tests rely on `TERM`/`KILL` signal semantics -- consistent with this
//! service's Linux-container-only production target (see
//! `pipeline::process_group`).

#![cfg(unix)]

use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use svc_streaming::pipeline::{
    AudioCodec, FfmpegSupervisor, InputSpec, ObjectStoreRef, OutputSpec, PipelineEngine,
    PipelineSpec, PipelineState, SupervisorConfig, TranscodeProfile, VideoCodec,
};
use svc_streaming::store::DefaultSecretResolver;
use uuid::Uuid;

/// A single well-formed `FfmpegProgress`-parseable line (see
/// `ffmpeg_sidecar::log_parser::try_parse_progress`) -- printed to stderr,
/// no `ffmpeg`-preamble lines required first.
const PROGRESS_LINE: &str =
    "frame=  100 fps= 30 q=-1.0 size=    1024kB time=00:00:03.33 bitrate=2500.0kbits/s speed=1.00x";

/// Writes an executable `sh` script to a fresh, namespaced temp path (pid +
/// uuid, per `general.md` Parallel Agent Isolation) and returns its path.
fn fake_ffmpeg(name: &str, body: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "svc-streaming-fake-ffmpeg-{name}-{}-{}.sh",
        std::process::id(),
        Uuid::new_v4()
    ));
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write fake ffmpeg script");
    let mut perms = std::fs::metadata(&path).unwrap().permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms).unwrap();
    path
}

/// Millisecond-scale timings so the lifecycle suite runs fast -- spec §6's
/// production defaults (1s-30s backoff, 10s stall, 3s+3s stop grace) are
/// covered by `SupervisorConfig::default()`'s own unit assertions, not
/// re-tested at these timescales here.
fn fast_config() -> SupervisorConfig {
    SupervisorConfig {
        backoff_initial: Duration::from_millis(20),
        backoff_max: Duration::from_millis(100),
        max_restarts: 3,
        stall_timeout: Duration::from_millis(150),
        stop_grace_timeout: Duration::from_millis(80),
        term_grace_timeout: Duration::from_millis(80),
        reap_timeout: Duration::from_millis(500),
    }
}

fn record_only_spec(id: Uuid) -> PipelineSpec {
    PipelineSpec {
        id,
        tenant: "tenant-1".into(),
        community_id: "community-1".into(),
        inputs: vec![InputSpec::Rtmp {
            stream_key: "sk1".into(),
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
    }
}

fn supervisor(ffmpeg_path: PathBuf, config: SupervisorConfig) -> FfmpegSupervisor {
    FfmpegSupervisor::new(
        ffmpeg_path,
        std::env::temp_dir(),
        41000,
        Arc::new(DefaultSecretResolver),
        config,
    )
}

/// Polls `check` until it returns `true` or `timeout` elapses (panicking on
/// timeout) -- used instead of a fixed sleep since exact process-scheduling
/// timing is inherently non-deterministic.
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
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

/// Emits progress every 50ms on stderr in the background; the foreground
/// blocks on stdin, exiting cleanly the moment stdin reaches EOF (the
/// supervisor's graceful-stop signal -- stdin is ffmpeg's `pipe:0` media
/// input, so closing it is the only quit that cannot collide with the
/// data) -- the common "healthy, responsive" fake ffmpeg used by the
/// happy-path test. A `q` line also still exits it, for older callers.
fn responsive_script() -> String {
    format!(
        r#"( while true; do echo '{PROGRESS_LINE}' >&2; sleep 0.05; done ) &
BGPID=$!
while read -r line; do
  if [ "$line" = "q" ]; then
    kill "$BGPID" 2>/dev/null
    exit 0
  fi
done
kill "$BGPID" 2>/dev/null
exit 0"#
    )
}

#[tokio::test]
async fn start_status_stop_happy_path() {
    let script = fake_ffmpeg("happy", &responsive_script());
    let sup = supervisor(script, fast_config());
    let id = Uuid::new_v4();

    let handle = sup
        .start(record_only_spec(id))
        .await
        .expect("start succeeds");
    assert_eq!(handle.id, id);

    // `state` flips to `Running` as soon as ffmpeg spawns, before the
    // first `Progress` event is parsed -- wait for an actual parsed frame
    // count instead of just the state, so this isn't a timing race.
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id).await.map(|p| p.frame > 0).unwrap_or(false)
    })
    .await;

    let progress = sup.progress(id).await.expect("registered");
    assert_eq!(progress.state, PipelineState::Running);
    assert!(progress.frame > 0, "expected a parsed progress frame count");

    sup.stop(id).await.expect("stop succeeds");
    assert!(
        sup.status(id).await.is_err(),
        "pipeline should be deregistered after stop()"
    );
}

/// A binary that exits immediately (simulating an ffmpeg crash) every
/// attempt -- the supervisor must restart with exponential backoff, then
/// mark the pipeline `Failed` once `max_restarts` is exceeded.
#[tokio::test]
async fn restart_on_crash_with_backoff_then_failed() {
    let script = fake_ffmpeg("crash", "exit 1");
    let config = fast_config();
    let max_restarts = config.max_restarts;
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();

    sup.start(record_only_spec(id))
        .await
        .expect("start succeeds");

    wait_until(Duration::from_secs(5), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Failed)
            .unwrap_or(false)
    })
    .await;

    let progress = sup
        .progress(id)
        .await
        .expect("still registered while Failed");
    assert_eq!(progress.state, PipelineState::Failed);
    assert_eq!(progress.restarts, max_restarts + 1);
    assert!(progress.last_error.is_some());

    // The monitor task has already exited on its own (Failed breaks the
    // retry loop), but the registry entry persists until an explicit
    // stop() -- clean it up rather than leaking it for the rest of the
    // test process.
    sup.stop(id)
        .await
        .expect("stop of an already-Failed pipeline is a clean deregister");
}

/// A binary that reports exactly one progress event, then hangs forever
/// without producing more output or exiting -- the supervisor's stall
/// detector (no `Progress` event for `stall_timeout`) must force a
/// restart.
#[tokio::test]
async fn stall_detection_forces_restart() {
    let script = fake_ffmpeg("stall", &format!("echo '{PROGRESS_LINE}' >&2\nsleep 100"));
    let sup = supervisor(script, fast_config());
    let id = Uuid::new_v4();

    sup.start(record_only_spec(id))
        .await
        .expect("start succeeds");

    // First progress event -> Running, then no further advance for
    // stall_timeout -> at least one stall-triggered restart recorded.
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id)
            .await
            .map(|p| p.restarts >= 1)
            .unwrap_or(false)
    })
    .await;

    // The fake binary stalls every attempt, so left alone the monitor task
    // keeps restarting it indefinitely (until max_restarts) in the
    // background -- stop it now rather than leaking that loop past this
    // test's return.
    sup.stop(id).await.expect("stop succeeds");
}

/// A binary that ignores both stdin EOF and SIGTERM -- `stop()` must still
/// complete (via SIGKILL) within its bounded overall timeout instead of
/// hanging forever.
#[tokio::test]
async fn stop_escalates_through_sigterm_to_sigkill_within_timeout() {
    let script = fake_ffmpeg(
        "stubborn",
        &format!("trap '' TERM\necho '{PROGRESS_LINE}' >&2\nwhile true; do sleep 1; done"),
    );
    let config = fast_config();
    let overall_bound = config.stop_deadline();
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();

    sup.start(record_only_spec(id))
        .await
        .expect("start succeeds");
    wait_until(Duration::from_secs(3), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Running)
            .unwrap_or(false)
    })
    .await;

    let start = tokio::time::Instant::now();
    tokio::time::timeout(overall_bound, sup.stop(id))
        .await
        .expect("stop() must not hang past its own bounded timeout")
        .expect("stop() succeeds once SIGKILL lands");
    assert!(
        start.elapsed() < overall_bound,
        "stop() should escalate to SIGKILL well within {overall_bound:?}, took {:?}",
        start.elapsed()
    );
    assert!(sup.status(id).await.is_err());
}

fn discord_voice_spec(id: Uuid) -> PipelineSpec {
    PipelineSpec {
        id,
        tenant: "tenant-1".into(),
        community_id: "community-1".into(),
        inputs: vec![InputSpec::Rtmp {
            stream_key: "sk1".into(),
        }],
        profiles: vec![TranscodeProfile {
            name: "copy".into(),
            video: VideoCodec::Copy,
            audio: AudioCodec::Copy,
            resolution: None,
            fps: None,
        }],
        outputs: vec![OutputSpec::DiscordVoice {
            guild_id: "guild-1".into(),
            channel_id: "channel-1".into(),
        }],
    }
}

/// Exercises `stdin_writer()` and `take_stdout()` against a real running
/// pipeline: writes ingest-listener-style bytes into ffmpeg's stdin, reads
/// the PCM sink back off stdout, and confirms a second `take_stdout()`
/// call is rejected (single-owner contract).
#[tokio::test]
async fn stdin_writer_and_take_stdout_work_against_a_running_pipeline() {
    let script = fake_ffmpeg(
        "stdio",
        &format!(
            r#"echo '{PROGRESS_LINE}' >&2
read -r line
printf '%s' "got:$line"
"#
        ),
    );
    let sup = supervisor(script, fast_config());
    let id = Uuid::new_v4();

    sup.start(discord_voice_spec(id))
        .await
        .expect("start succeeds");
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id).await.map(|p| p.frame > 0).unwrap_or(false)
    })
    .await;

    let stdout = sup.take_stdout(id).await.expect("PCM sink available");
    assert!(
        sup.take_stdout(id).await.is_err(),
        "stdout must be a single-owner handle"
    );

    let stdin = sup.stdin_writer(id).await.expect("stdin available");
    stdin.write(b"hello\n").expect("write succeeds");

    let mut buf = Vec::new();
    tokio::task::spawn_blocking(move || {
        use std::io::Read;
        let mut stdout = stdout;
        let _ = stdout.read_to_end(&mut buf);
        buf
    })
    .await
    .expect("blocking read task did not panic");

    sup.stop(id).await.expect("stop succeeds");
}

/// An `ffmpeg_path` that doesn't exist at all -- `spawn()` itself fails
/// every attempt, exercising the spawn-error backoff/Failed path
/// (distinct from a process that spawns then exits).
#[tokio::test]
async fn spawn_failure_backs_off_then_fails() {
    let config = fast_config();
    let max_restarts = config.max_restarts;
    let sup = supervisor(
        PathBuf::from("/nonexistent/svc-streaming-fake-ffmpeg-binary"),
        config,
    );
    let id = Uuid::new_v4();

    sup.start(record_only_spec(id))
        .await
        .expect("start succeeds even though spawning fails asynchronously");

    wait_until(Duration::from_secs(5), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Failed)
            .unwrap_or(false)
    })
    .await;

    let progress = sup
        .progress(id)
        .await
        .expect("still registered while Failed");
    assert_eq!(progress.restarts, max_restarts + 1);
    assert!(progress
        .last_error
        .as_deref()
        .unwrap_or("")
        .contains("spawn failed"));

    sup.stop(id)
        .await
        .expect("stop of an already-Failed pipeline deregisters cleanly");
}

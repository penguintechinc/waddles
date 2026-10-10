//! Regression suite for the ffmpeg teardown hang found in the 2026-10-10
//! alpha RTMP->HLS live test: on publisher disconnect the supervisor never
//! delivered its stop signals (it shelled out to a `kill` binary the runtime
//! image does not have, ignoring the result), wrote `q\n` into ffmpeg's
//! `pipe:0` *media* input where it could never be interpreted as a command,
//! then blocked a tokio worker forever joining the stderr thread -- `/health`
//! timed out and liveness killed the pod.
//!
//! Every test here drives the real `FfmpegSupervisor` teardown path against
//! fake `ffmpeg` shell scripts that leave **real helper processes** in the
//! process group, and checks the outcome against `/proc` (the kernel's view),
//! never against supervisor bookkeeping. The no-`kill`-binary variant lives in
//! `tests/pipeline_teardown_no_kill_binary.rs` because it must mutate `PATH`
//! for its whole process.

#![cfg(unix)]

mod teardown_common;

use std::time::Duration;

use svc_streaming::pipeline::{PipelineEngine, PipelineState, SupervisorConfig};
use teardown_common::{
    assert_gone, assert_reaped, fake_ffmpeg, fast_config, find_in_path, read_pids,
    record_only_spec, scratch_dir, stubborn_script, supervisor, wait_until, PROGRESS_LINE,
};
use uuid::Uuid;

/// Graceful quit is EOF on stdin, and nothing else is ever written to the
/// media pipe. The fake reads stdin to EOF into a capture file: it must
/// contain exactly the media bytes the ingest side wrote -- no `q\n` -- and
/// the fake must exit on its own (no SIGTERM needed) well within a generous
/// grace period.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stop_closes_stdin_for_eof_and_never_writes_a_quit_command_into_the_media_pipe() {
    let dir = scratch_dir("eof");
    let cat = find_in_path("cat");
    let script = fake_ffmpeg(
        &dir,
        "eof",
        &format!(
            r#"trap 'echo TERM >> "{d}/log"; exit 143' TERM
echo '{PROGRESS_LINE}' >&2
{cat} > "{d}/capture"
echo EOF >> "{d}/log"
exit 0"#,
            d = dir.display(),
            cat = cat.display(),
        ),
    );
    let config = SupervisorConfig {
        stop_grace_timeout: Duration::from_secs(5),
        ..fast_config()
    };
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id).await.map(|p| p.frame > 0).unwrap_or(false)
    })
    .await;

    let stdin = sup.stdin_writer(id).await.expect("stdin handle");
    tokio::task::spawn_blocking(move || stdin.write(b"MEDIA-BYTES"))
        .await
        .expect("join")
        .expect("media write");

    let started = tokio::time::Instant::now();
    sup.stop(id).await.expect("stop");
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "EOF must end ffmpeg promptly, took {:?}",
        started.elapsed()
    );

    assert_eq!(
        std::fs::read(dir.join("capture")).expect("capture written"),
        b"MEDIA-BYTES",
        "the media pipe must carry only media -- no `q\\n` quit command"
    );
    let log = std::fs::read_to_string(dir.join("log")).unwrap_or_default();
    assert!(log.contains("EOF"), "fake never saw stdin EOF: {log:?}");
    assert!(
        !log.contains("TERM"),
        "a cooperative ffmpeg must exit on EOF without SIGTERM: {log:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

/// A helper that outlives the leader keeps stderr open and (before the fix)
/// kept the supervisor waiting for a stream-end that never came. The leader
/// here exits on EOF but leaks a `sleep 300` into the group; teardown must
/// sweep it, and must reap the leader (no zombie).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stop_sweeps_a_helper_leaked_into_the_process_group_and_reaps_the_leader() {
    let dir = scratch_dir("sweep");
    let sleep = find_in_path("sleep");
    let cat = find_in_path("cat");
    let script = fake_ffmpeg(
        &dir,
        "sweep",
        &format!(
            r#"echo $$ >> "{d}/leader.pids"
{sleep} 300 &
echo $! >> "{d}/child.pids"
echo '{PROGRESS_LINE}' >&2
{cat} > /dev/null
exit 0"#,
            d = dir.display(),
            sleep = sleep.display(),
            cat = cat.display(),
        ),
    );
    let sup = supervisor(script, fast_config());
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id).await.map(|p| p.frame > 0).unwrap_or(false)
    })
    .await;
    let leader = read_pids(&dir.join("leader.pids"))[0];
    let helper = read_pids(&dir.join("child.pids"))[0];

    tokio::time::timeout(Duration::from_secs(5), sup.stop(id))
        .await
        .expect("stop must not hang on a leaked helper holding stderr open")
        .expect("stop");

    assert_gone("leaked helper", helper).await;
    assert_reaped("group leader", leader).await;
    std::fs::remove_dir_all(&dir).ok();
}

/// The core regression: a group that ignores both stdin EOF and SIGTERM must
/// still be killed and reaped inside the supervisor's own stop deadline, the
/// helper with it, and the pipeline must be deregistered.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stop_escalates_to_sigkill_and_kills_and_reaps_the_whole_group() {
    let dir = scratch_dir("stubborn");
    let sleep = find_in_path("sleep");
    let script = fake_ffmpeg(&dir, "stubborn", &stubborn_script(&dir, &sleep));
    let config = fast_config();
    let deadline = config.stop_deadline();
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Running)
            .unwrap_or(false)
            && !read_pids(&dir.join("child.pids")).is_empty()
    })
    .await;
    let leader = read_pids(&dir.join("leader.pids"))[0];
    let helper = read_pids(&dir.join("child.pids"))[0];

    let started = tokio::time::Instant::now();
    tokio::time::timeout(deadline, sup.stop(id))
        .await
        .expect("stop must finish within its own bounded deadline")
        .expect("stop");
    assert!(started.elapsed() < deadline);

    assert_gone("helper", helper).await;
    assert_reaped("leader", leader).await;
    assert!(
        sup.status(id).await.is_err(),
        "pipeline must be deregistered after stop()"
    );
    std::fs::remove_dir_all(&dir).ok();
}

/// A stall-triggered restart is a teardown too: the previous attempt's group
/// must be killed and reaped before the next attempt spawns, or every stall
/// would leak an ffmpeg.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stall_restart_kills_and_reaps_the_previous_attempts_group() {
    let dir = scratch_dir("stall");
    let sleep = find_in_path("sleep");
    let script = fake_ffmpeg(
        &dir,
        "stall",
        &format!(
            r#"echo $$ >> "{d}/leader.pids"
{sleep} 300 &
echo $! >> "{d}/child.pids"
echo '{PROGRESS_LINE}' >&2
exec {sleep} 300"#,
            d = dir.display(),
            sleep = sleep.display(),
        ),
    );
    let sup = supervisor(script, fast_config());
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");

    wait_until(Duration::from_secs(5), || async {
        read_pids(&dir.join("leader.pids")).len() >= 2
    })
    .await;
    let first_leader = read_pids(&dir.join("leader.pids"))[0];
    let first_helper = read_pids(&dir.join("child.pids"))[0];
    assert_gone("first attempt's helper", first_helper).await;
    assert_reaped("first attempt's leader", first_leader).await;

    sup.stop(id).await.expect("stop");
    for pid in read_pids(&dir.join("leader.pids")) {
        assert_reaped("leader", pid).await;
    }
    for pid in read_pids(&dir.join("child.pids")) {
        assert_gone("helper", pid).await;
    }
    std::fs::remove_dir_all(&dir).ok();
}

/// `stop()` while a pipeline sleeps out a long restart backoff must return
/// promptly instead of waiting the backoff out (and blowing its deadline).
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stop_interrupts_a_long_restart_backoff() {
    let dir = scratch_dir("backoff");
    let script = fake_ffmpeg(&dir, "crash", "exit 1");
    let config = SupervisorConfig {
        backoff_initial: Duration::from_secs(30),
        backoff_max: Duration::from_secs(30),
        ..fast_config()
    };
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(5), || async {
        sup.progress(id)
            .await
            .map(|p| p.restarts >= 1)
            .unwrap_or(false)
    })
    .await;

    let started = tokio::time::Instant::now();
    tokio::time::timeout(Duration::from_secs(3), sup.stop(id))
        .await
        .expect("stop must interrupt the 30s backoff")
        .expect("stop");
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "took {:?}",
        started.elapsed()
    );
    std::fs::remove_dir_all(&dir).ok();
}

/// The argv the supervisor actually hands ffmpeg must carry `-nostdin` as a
/// global option (ahead of the `-i` input) and must NOT end in the stray `-n`
/// ffmpeg-sidecar's `spawn()` appends after the output path when no
/// overwrite/stdin option is present. (The sidecar itself prepends
/// `-loglevel level+info`, so `-nostdin` is not literally argv[0].)
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn spawned_argv_has_nostdin_first_and_no_stray_trailing_n() {
    let dir = scratch_dir("argv");
    let cat = find_in_path("cat");
    let script = fake_ffmpeg(
        &dir,
        "argv",
        &format!(
            r#"for a in "$@"; do echo "$a" >> "{d}/argv"; done
echo '{PROGRESS_LINE}' >&2
{cat} > /dev/null"#,
            d = dir.display(),
            cat = cat.display(),
        ),
    );
    let sup = supervisor(script, fast_config());
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id).await.map(|p| p.frame > 0).unwrap_or(false)
    })
    .await;
    sup.stop(id).await.expect("stop");

    let argv: Vec<String> = std::fs::read_to_string(dir.join("argv"))
        .expect("argv captured")
        .lines()
        .map(str::to_owned)
        .collect();
    let nostdin = argv.iter().position(|a| a == "-nostdin");
    let input = argv.iter().position(|a| a == "-i");
    assert!(
        matches!((nostdin, input), (Some(n), Some(i)) if n < i),
        "-nostdin must precede -i as a global option, argv: {argv:?}"
    );
    assert!(
        !argv.iter().any(|a| a == "-n"),
        "stray -n in argv: {argv:?}"
    );
    assert!(
        argv.last().is_some_and(|last| last.ends_with(".ts")),
        "the recording output path must be the final argv element: {argv:?}"
    );
    std::fs::remove_dir_all(&dir).ok();
}

/// An ingest write parked on a stuck ffmpeg (its stdin pipe full) holds the
/// stdin mutex. Teardown must not wait for that lock on an async worker: it
/// skips the EOF, escalates through the signals, and the kill unblocks the
/// writer with `EPIPE` instead of leaving it parked forever.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stop_does_not_hang_when_an_ingest_write_is_parked_on_a_stuck_ffmpeg() {
    let dir = scratch_dir("busy-stdin");
    let sleep = find_in_path("sleep");
    // The stubborn fake never reads stdin, so a large write fills the pipe.
    let script = fake_ffmpeg(&dir, "stubborn", &stubborn_script(&dir, &sleep));
    let config = fast_config();
    let deadline = config.stop_deadline();
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Running)
            .unwrap_or(false)
            && !read_pids(&dir.join("child.pids")).is_empty()
    })
    .await;
    let leader = read_pids(&dir.join("leader.pids"))[0];
    let helper = read_pids(&dir.join("child.pids"))[0];

    let stdin = sup.stdin_writer(id).await.expect("stdin handle");
    let writer = tokio::task::spawn_blocking(move || stdin.write(&vec![7u8; 8 * 1024 * 1024]));
    tokio::time::sleep(Duration::from_millis(300)).await;
    assert!(
        !writer.is_finished(),
        "precondition: the write must be parked on the full pipe"
    );

    tokio::time::timeout(deadline, sup.stop(id))
        .await
        .expect("stop must not wait on the parked writer's lock")
        .expect("stop");

    let write_result = tokio::time::timeout(Duration::from_secs(5), writer)
        .await
        .expect("the kill must unblock the parked writer")
        .expect("writer task did not panic");
    assert!(
        write_result.is_err(),
        "a write into a killed ffmpeg must fail, not succeed"
    );
    assert_gone("helper", helper).await;
    assert_reaped("leader", leader).await;
    std::fs::remove_dir_all(&dir).ok();
}

/// ffmpeg closing its stderr while still alive ends the event stream early.
/// The old code read that as "the process ended" and then blocked in
/// `wait()`; teardown must keep tracking the actual process and escalate.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stop_keeps_escalating_after_ffmpeg_closes_its_stderr_but_stays_alive() {
    let dir = scratch_dir("stderr-closed");
    let sleep = find_in_path("sleep");
    let cat = find_in_path("cat");
    let script = fake_ffmpeg(
        &dir,
        "stderr-closed",
        &format!(
            r#"trap '' TERM
echo $$ >> "{d}/leader.pids"
echo '{PROGRESS_LINE}' >&2
{cat} > /dev/null
exec 2>&-
while true; do {sleep} 1; done"#,
            d = dir.display(),
            sleep = sleep.display(),
            cat = cat.display(),
        ),
    );
    let config = fast_config();
    let deadline = config.stop_deadline();
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.progress(id).await.map(|p| p.frame > 0).unwrap_or(false)
            && !read_pids(&dir.join("leader.pids")).is_empty()
    })
    .await;
    let leader = read_pids(&dir.join("leader.pids"))[0];

    tokio::time::timeout(deadline, sup.stop(id))
        .await
        .expect("stop must finish within its deadline")
        .expect("stop");
    assert_reaped("leader", leader).await;
    std::fs::remove_dir_all(&dir).ok();
}

/// Even with every grace and reap bound at zero (so the stop deadline is
/// already expired when `stop()` starts waiting), `stop()` returns promptly,
/// the pipeline is deregistered, and the child is still killed and collected
/// -- by the monitor task finishing detached, or by the detached reaper it
/// hands an un-reapable child to. A failed teardown is loud and bounded,
/// never a hang and never a leaked zombie.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expired_stop_deadline_still_deregisters_and_the_child_is_still_collected() {
    let dir = scratch_dir("deadline");
    let sleep = find_in_path("sleep");
    let script = fake_ffmpeg(&dir, "stubborn", &stubborn_script(&dir, &sleep));
    let config = SupervisorConfig {
        stop_grace_timeout: Duration::ZERO,
        term_grace_timeout: Duration::ZERO,
        reap_timeout: Duration::ZERO,
        ..fast_config()
    };
    assert_eq!(config.stop_deadline(), Duration::ZERO);
    let sup = supervisor(script, config);
    let id = Uuid::new_v4();
    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(3), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Running)
            .unwrap_or(false)
            && !read_pids(&dir.join("child.pids")).is_empty()
    })
    .await;
    let leader = read_pids(&dir.join("leader.pids"))[0];
    let helper = read_pids(&dir.join("child.pids"))[0];

    tokio::time::timeout(Duration::from_secs(2), sup.stop(id))
        .await
        .expect("stop must return even when its deadline has already expired")
        .expect("stop");
    assert!(
        sup.status(id).await.is_err(),
        "pipeline must be deregistered even if teardown overran"
    );

    assert_gone("helper", helper).await;
    assert_reaped("leader", leader).await;
    std::fs::remove_dir_all(&dir).ok();
}

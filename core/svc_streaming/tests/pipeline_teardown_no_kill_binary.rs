//! The production failure, reproduced faithfully: the runtime image
//! (`debian:bookworm-slim` + ffmpeg) ships **no** `kill` binary, so the old
//! supervisor's `Command::new("kill")` shell-out failed silently, SIGTERM and
//! SIGKILL were never sent, the ffmpeg child survived, and a tokio worker
//! parked forever joining its stderr thread -- `/health` stopped answering and
//! liveness killed the container (~10s after the RTMP publisher disconnected).
//!
//! This test empties `PATH` for its whole process so no `kill` is resolvable
//! (asserted as a precondition), runs the scenario on a **single-worker**
//! tokio runtime so one blocked worker is a fully wedged service, and checks:
//!
//! * the whole process group (leader + leaked helper) is dead and the leader
//!   is reaped, per `/proc`;
//! * `stop()` finished inside the supervisor's own bounded deadline;
//! * a heartbeat task sharing that one worker was never starved (the service
//!   stayed responsive -- the `/health` analogue);
//! * a second pipeline can still start and stop afterwards.
//!
//! It runs on a watchdog thread: on the old code the worker blocks forever and
//! timers never fire, so the only way to turn "wedged" into a test *failure*
//! rather than a CI hang is a wall-clock bound outside the runtime. It lives
//! alone in this file because `PATH` is process-global state.

#![cfg(unix)]

mod teardown_common;

use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use svc_streaming::pipeline::{PipelineEngine, PipelineState};
use teardown_common::{
    assert_gone, assert_reaped, fake_ffmpeg, fast_config, find_in_path, read_pids,
    record_only_spec, scratch_dir, stubborn_script, supervisor, wait_until, PROGRESS_LINE,
};
use uuid::Uuid;

/// Everything the scenario observed, handed back to the asserting thread.
struct Observed {
    leader: u32,
    helper: u32,
    stop_elapsed: Duration,
    stop_deadline: Duration,
    max_heartbeat_gap: Duration,
    stopped_deregistered: bool,
    second_pipeline_ok: bool,
}

async fn scenario(
    dir: std::path::PathBuf,
    sleep: std::path::PathBuf,
    cat: std::path::PathBuf,
) -> Observed {
    let stubborn = fake_ffmpeg(&dir, "stubborn", &stubborn_script(&dir, &sleep));
    let config = fast_config();
    let stop_deadline = config.stop_deadline();
    let sup = Arc::new(supervisor(stubborn, config));
    let id = Uuid::new_v4();

    // Heartbeat on the same (only) worker as the monitor task.
    let max_gap_ms = Arc::new(AtomicU64::new(0));
    let beat = {
        let max_gap_ms = max_gap_ms.clone();
        tokio::spawn(async move {
            let mut last = Instant::now();
            loop {
                tokio::time::sleep(Duration::from_millis(10)).await;
                let gap = last.elapsed().as_millis() as u64;
                max_gap_ms.fetch_max(gap, Ordering::SeqCst);
                last = Instant::now();
            }
        })
    };

    sup.start(record_only_spec(id)).await.expect("start");
    wait_until(Duration::from_secs(5), || async {
        sup.status(id)
            .await
            .map(|s| s.state == PipelineState::Running)
            .unwrap_or(false)
            && !read_pids(&dir.join("child.pids")).is_empty()
    })
    .await;
    let leader = read_pids(&dir.join("leader.pids"))[0];
    let helper = read_pids(&dir.join("child.pids"))[0];

    // The publisher disconnects -> the orchestrator calls stop().
    let started = Instant::now();
    sup.stop(id).await.expect("stop");
    let stop_elapsed = started.elapsed();
    let stopped_deregistered = sup.status(id).await.is_err();

    // The service must still be able to run pipelines afterwards.
    let second = fake_ffmpeg(
        &dir,
        "second",
        &format!(
            "echo '{PROGRESS_LINE}' >&2\n{} > /dev/null\nexit 0",
            cat.display()
        ),
    );
    let sup2 = supervisor(second, fast_config());
    let id2 = Uuid::new_v4();
    sup2.start(record_only_spec(id2)).await.expect("start #2");
    wait_until(Duration::from_secs(3), || async {
        sup2.progress(id2)
            .await
            .map(|p| p.frame > 0)
            .unwrap_or(false)
    })
    .await;
    sup2.stop(id2).await.expect("stop #2");
    let second_pipeline_ok = sup2.status(id2).await.is_err();

    beat.abort();
    Observed {
        leader,
        helper,
        stop_elapsed,
        stop_deadline,
        max_heartbeat_gap: Duration::from_millis(max_gap_ms.load(Ordering::SeqCst)),
        stopped_deregistered,
        second_pipeline_ok,
    }
}

#[test]
fn publisher_disconnect_teardown_kills_the_group_without_a_kill_binary_and_never_wedges() {
    let dir = scratch_dir("nokill");
    let sleep = find_in_path("sleep");
    let cat = find_in_path("cat");

    // Mirror the runtime image: nothing resolvable by name. SAFETY: this is
    // the only test in this binary, and it runs before any other thread is
    // spawned.
    let empty_path = dir.join("empty-path");
    std::fs::create_dir_all(&empty_path).expect("empty PATH dir");
    unsafe { std::env::set_var("PATH", &empty_path) };
    assert!(
        Command::new("kill").arg("-0").arg("1").status().is_err(),
        "precondition: no `kill` binary may be resolvable on PATH"
    );

    let (tx, rx) = std::sync::mpsc::channel();
    let scenario_dir = dir.clone();
    std::thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .expect("runtime");
        let observed = runtime.block_on(scenario(scenario_dir, sleep, cat));
        runtime.shutdown_timeout(Duration::from_secs(2));
        let _ = tx.send(observed);
    });
    let observed = rx
        .recv_timeout(Duration::from_secs(60))
        .expect("teardown wedged the runtime: the scenario never finished (old-code behaviour)");

    // Verify against the kernel, on a throwaway runtime for the polling helpers.
    let verifier = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("verifier runtime");
    verifier.block_on(async {
        assert_gone("leaked helper", observed.helper).await;
        assert_reaped("group leader", observed.leader).await;
    });

    assert!(
        observed.stop_elapsed < observed.stop_deadline,
        "stop() took {:?}, deadline {:?}",
        observed.stop_elapsed,
        observed.stop_deadline
    );
    assert!(observed.stopped_deregistered, "pipeline left registered");
    assert!(
        observed.max_heartbeat_gap < Duration::from_secs(2),
        "the only runtime worker was starved for {:?} -- teardown blocked it",
        observed.max_heartbeat_gap
    );
    assert!(
        observed.second_pipeline_ok,
        "the service could not run a pipeline after the teardown"
    );
    std::fs::remove_dir_all(&dir).ok();
}

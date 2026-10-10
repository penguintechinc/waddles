//! Shared helpers for the ffmpeg-supervisor teardown regression suites
//! (`tests/pipeline_teardown.rs`, `tests/pipeline_teardown_no_kill_binary.rs`):
//! fake-`ffmpeg` shell scripts that leave real helper processes behind, a
//! millisecond-scale supervisor config, and `/proc`-based liveness checks so
//! "the process group is actually gone" is verified against the kernel, not
//! against the supervisor's own bookkeeping.
//!
//! Unix/Linux-only (POSIX `sh` fakes, `/proc`), matching the service's
//! Linux-container-only production target.

#![cfg(unix)]
#![allow(dead_code)] // each test binary uses a different subset

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use svc_streaming::pipeline::{
    AudioCodec, FfmpegSupervisor, InputSpec, ObjectStoreRef, OutputSpec, PipelineSpec,
    SupervisorConfig, TranscodeProfile, VideoCodec,
};
use svc_streaming::store::DefaultSecretResolver;
use uuid::Uuid;

/// A well-formed ffmpeg stats line the supervisor's progress parser accepts.
pub const PROGRESS_LINE: &str =
    "frame=  100 fps= 30 q=-1.0 size=    1024kB time=00:00:03.33 bitrate=2500.0kbits/s speed=1.00x";

/// Resolves `name` against the current `PATH` to an absolute path, so fake
/// scripts keep working after a test empties `PATH`.
pub fn find_in_path(name: &str) -> PathBuf {
    let path = std::env::var_os("PATH").expect("PATH is set");
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
        .unwrap_or_else(|| panic!("`{name}` not found on PATH"))
}

/// A fresh, namespaced scratch directory for one test (pid + uuid).
pub fn scratch_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "svc-streaming-teardown-{label}-{}-{}",
        std::process::id(),
        Uuid::new_v4()
    ));
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// Writes an executable `sh` script into `dir` and returns its path.
pub fn fake_ffmpeg(dir: &Path, name: &str, body: &str) -> PathBuf {
    let path = dir.join(format!("fake-ffmpeg-{name}.sh"));
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write fake ffmpeg");
    let mut perms = std::fs::metadata(&path).expect("stat").permissions();
    perms.set_mode(0o755);
    std::fs::set_permissions(&path, perms).expect("chmod");
    path
}

/// Millisecond-scale lifecycle timings.
pub fn fast_config() -> SupervisorConfig {
    SupervisorConfig {
        backoff_initial: Duration::from_millis(20),
        backoff_max: Duration::from_millis(100),
        max_restarts: 3,
        stall_timeout: Duration::from_millis(150),
        stop_grace_timeout: Duration::from_millis(80),
        term_grace_timeout: Duration::from_millis(80),
        reap_timeout: Duration::from_secs(2),
    }
}

pub fn record_only_spec(id: Uuid) -> PipelineSpec {
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

pub fn supervisor(ffmpeg_path: PathBuf, config: SupervisorConfig) -> FfmpegSupervisor {
    FfmpegSupervisor::new(
        ffmpeg_path,
        std::env::temp_dir(),
        41000,
        Arc::new(DefaultSecretResolver),
        config,
    )
}

/// Polls `check` until true or `timeout` elapses (panicking on timeout).
pub async fn wait_until<F, Fut>(timeout: Duration, mut check: F)
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

/// Reads every PID (one per line) a fake script appended to `path`.
pub fn read_pids(path: &Path) -> Vec<u32> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| line.trim().parse().ok())
        .collect()
}

/// True once `pid` is not running: gone from `/proc`, or only a zombie
/// awaiting an init that may never reap (containers).
pub fn is_gone(pid: u32) -> bool {
    match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        Err(_) => true,
        Ok(stat) => stat
            .rsplit(')')
            .next()
            .map(|rest| rest.trim_start().starts_with('Z'))
            .unwrap_or(true),
    }
}

/// True once `pid` has been fully reaped: no `/proc` entry at all (a zombie
/// still has one). Only meaningful for processes that are our children.
pub fn is_reaped(pid: u32) -> bool {
    !Path::new(&format!("/proc/{pid}")).exists()
}

/// Waits (bounded) for `pid` to stop running, panicking with context.
pub async fn assert_gone(what: &str, pid: u32) {
    for _ in 0..300 {
        if is_gone(pid) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("{what} (pid {pid}) is still running after teardown");
}

/// Waits (bounded) for `pid` to disappear from `/proc` entirely.
pub async fn assert_reaped(what: &str, pid: u32) {
    for _ in 0..300 {
        if is_reaped(pid) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("{what} (pid {pid}) was killed but never reaped (zombie)");
}

/// Script body for a fake ffmpeg that ignores SIGTERM and stdin EOF, leaks a
/// SIGTERM-ignoring helper into the same process group, records both PIDs
/// (`{dir}/leader.pids`, `{dir}/child.pids`), and keeps emitting progress --
/// i.e. only an unconditional group SIGKILL ends it.
pub fn stubborn_script(dir: &Path, sleep: &Path) -> String {
    let sleep = sleep.display();
    let dir = dir.display();
    format!(
        r#"trap '' TERM
echo $$ >> "{dir}/leader.pids"
{sleep} 300 &
echo $! >> "{dir}/child.pids"
while true; do echo '{PROGRESS_LINE}' >&2; {sleep} 0.05; done"#
    )
}

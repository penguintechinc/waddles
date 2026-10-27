//! The on-disk liveness probe: this process has no HTTP surface
//! (`crate::run_healthcheck`'s own doc comment), so Kubernetes' liveness/
//! readiness probes exec this same binary instead of hitting a port. The
//! long-running session loop (`crate::heartbeat::run_monitor`) touches a
//! small timestamp file whenever the host-API connection is confirmed
//! healthy; `--healthcheck=session` (a brand new, short-lived process
//! invocation with no shared memory to the long-running one) reads it back
//! and exits non-zero if it is missing or stale.
//!
//! Two files, not one: the probe file itself (`path`) records "last known
//! healthy", and a sibling start marker (`path` + `.started`) records
//! "when did the long-running process begin trying to connect" -- written
//! once by `crate::run` before the dial loop starts. Without the second
//! file, a brand new pod that hasn't completed its first handshake yet
//! would have no way to distinguish "still starting up" from "genuinely
//! stuck", since the probe file itself doesn't exist until the first
//! successful connection.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use crate::error::ExecutorError;

/// `EXECUTOR_GRACE_SECONDS` default: both the max age `--healthcheck=
/// session` accepts for the probe file, and the startup-grace window
/// before a still-missing probe file is treated as a failure.
pub const DEFAULT_GRACE_SECS: u64 = 60;

/// `EXECUTOR_PROBE_FILE` default -- matches `CliConfig::for_healthcheck`
/// and `CliConfig`'s own `#[arg(default_value = ...)]`.
pub const DEFAULT_PROBE_FILE_PATH: &str = "/tmp/executor-live";

/// Atomically writes `now` (seconds since `UNIX_EPOCH`) to `path`: write to
/// a sibling `.tmp` file, `fsync`, then `rename` over the target. A reader
/// (`check_session`, running as a wholly separate process) can therefore
/// never observe a partially written timestamp -- `rename` within the same
/// filesystem is atomic.
pub fn touch(path: &Path) -> Result<(), ExecutorError> {
    write_timestamp(path, now_secs())
}

/// Records "the long-running process started trying to connect now" --
/// called once by `crate::run` before its dial loop begins. Never updated
/// again for the life of the process.
pub fn record_start(path: &Path) -> Result<(), ExecutorError> {
    write_timestamp(&start_marker_path(path), now_secs())
}

fn start_marker_path(path: &Path) -> PathBuf {
    let mut os = path.as_os_str().to_owned();
    os.push(".started");
    PathBuf::from(os)
}

fn write_timestamp(path: &Path, secs: u64) -> Result<(), ExecutorError> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp_path = PathBuf::from(tmp);
    {
        let mut f = std::fs::File::create(&tmp_path).map_err(ExecutorError::Io)?;
        write!(f, "{secs}").map_err(ExecutorError::Io)?;
        f.sync_all().map_err(ExecutorError::Io)?;
    }
    std::fs::rename(&tmp_path, path).map_err(ExecutorError::Io)
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn read_timestamp(path: &Path) -> std::io::Result<u64> {
    let contents = std::fs::read_to_string(path)?;
    contents.trim().parse::<u64>().map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            format!("{path:?} does not contain a valid u64 timestamp: {contents:?}"),
        )
    })
}

/// `--healthcheck=session`'s check: passes if the probe file exists and is
/// no older than `max_age`. If it does not exist at all, falls back to the
/// start marker -- missing within `grace` of the recorded start time is
/// tolerated (still connecting for the first time), but a session that
/// was once healthy and has since gone stale is NEVER given a free pass by
/// the startup grace period, matching the spec's "exits non-zero if the
/// file is older than EXECUTOR_GRACE_SECONDS ... or missing after a
/// startup grace period" exactly.
pub fn check_session(path: &Path, max_age: Duration, grace: Duration) -> Result<(), String> {
    match read_timestamp(path) {
        Ok(ts) => {
            let age = Duration::from_secs(now_secs().saturating_sub(ts));
            if age > max_age {
                Err(format!(
                    "probe file {path:?} is {}s old, exceeds max age {}s",
                    age.as_secs(),
                    max_age.as_secs()
                ))
            } else {
                Ok(())
            }
        }
        Err(probe_err) if probe_err.kind() == std::io::ErrorKind::NotFound => {
            match read_timestamp(&start_marker_path(path)) {
                Ok(start_ts) => {
                    let since_start = Duration::from_secs(now_secs().saturating_sub(start_ts));
                    if since_start > grace {
                        Err(format!(
                            "no live host-api session {}s after startup, exceeds startup grace {}s",
                            since_start.as_secs(),
                            grace.as_secs()
                        ))
                    } else {
                        Ok(())
                    }
                }
                Err(_) => Err(format!(
                    "neither probe file {path:?} nor its start marker exist -- \
                     the session loop has not run yet"
                )),
            }
        }
        Err(e) => Err(format!("probe file {path:?} unreadable: {e}")),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn temp_probe_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "bundle-executor-test-probe-{}-{label}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ))
    }

    fn cleanup(path: &Path) {
        let _ = std::fs::remove_file(path);
        let _ = std::fs::remove_file(start_marker_path(path));
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        let _ = std::fs::remove_file(PathBuf::from(tmp));
    }

    #[test]
    fn touch_then_check_session_passes_within_max_age() {
        let path = temp_probe_path("fresh");
        touch(&path).expect("touch");
        assert!(check_session(&path, Duration::from_secs(60), Duration::from_secs(60)).is_ok());
        cleanup(&path);
    }

    #[test]
    fn a_stale_probe_file_fails_regardless_of_grace() {
        let path = temp_probe_path("stale");
        write_timestamp(&path, now_secs().saturating_sub(120)).expect("write stale timestamp");
        let result = check_session(&path, Duration::from_secs(60), Duration::from_secs(600));
        assert!(
            result.is_err(),
            "a stale probe file must never pass, grace or not"
        );
        cleanup(&path);
    }

    #[test]
    fn a_missing_probe_file_within_startup_grace_passes() {
        let path = temp_probe_path("starting");
        record_start(&path).expect("record start");
        assert!(check_session(&path, Duration::from_secs(60), Duration::from_secs(60)).is_ok());
        cleanup(&path);
    }

    #[test]
    fn a_missing_probe_file_past_startup_grace_fails() {
        let path = temp_probe_path("stuck-starting");
        write_timestamp(&start_marker_path(&path), now_secs().saturating_sub(120))
            .expect("write old start marker");
        let result = check_session(&path, Duration::from_secs(60), Duration::from_secs(60));
        assert!(
            result.is_err(),
            "missing probe file past the startup grace window must fail"
        );
        cleanup(&path);
    }

    #[test]
    fn neither_file_existing_fails() {
        let path = temp_probe_path("never-started");
        let result = check_session(&path, Duration::from_secs(60), Duration::from_secs(60));
        assert!(result.is_err());
        cleanup(&path);
    }

    #[test]
    fn touch_is_atomic_rename_leaves_no_tmp_file_behind() {
        let path = temp_probe_path("atomic");
        touch(&path).expect("touch");
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        assert!(
            !PathBuf::from(tmp).exists(),
            "the .tmp staging file must be renamed away, not left behind"
        );
        cleanup(&path);
    }
}

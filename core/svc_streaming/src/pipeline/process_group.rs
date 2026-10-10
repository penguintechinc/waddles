//! Shell-free process-group teardown primitives for supervised `ffmpeg`
//! children.
//!
//! The supervisor used to deliver SIGTERM/SIGKILL by shelling out to a
//! `kill` binary and ignoring the result. The `debian:bookworm-slim`
//! runtime image ships no `/bin/kill`, so no signal was ever sent, the
//! child survived teardown, and the monitor task blocked forever on its
//! pipes (alpha RTMP->HLS test, 2026-10-10). This module replaces that with
//! direct syscalls (`killpg`, `waitid(WNOWAIT)`, `wait4` via
//! [`Child::try_wait`]) whose results are always surfaced to the caller.
//!
//! **Ordering contract.** A child's PID (and therefore its process group
//! ID) stays reserved by the kernel until the leader is *reaped*. Every
//! function here that touches a PID is therefore safe to call only before
//! [`reap`] has returned `Some` for that child; the supervisor sweeps the
//! group with [`signal_group`] first and reaps last, and never touches a
//! `Child` again once reaped, so a recycled PID can never be signalled.
//! [`has_exited`] observes exit *without* reaping precisely so the sweep
//! can still happen after the leader has died.

use std::io;
use std::process::{Child, ExitStatus};
use std::time::Duration;

#[cfg(unix)]
use nix::errno::Errno;
#[cfg(unix)]
use nix::sys::signal::{killpg, Signal};
#[cfg(unix)]
use nix::sys::wait::{waitid, Id, WaitPidFlag, WaitStatus};
#[cfg(unix)]
use nix::unistd::{getpgid, getpgrp, Pid};

/// The signals the teardown ladder delivers to a child's process group.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum GroupSignal {
    /// Polite termination request -- ffmpeg finalizes its muxers and exits.
    Term,
    /// Unconditional kill -- cannot be caught, blocked, or ignored.
    Kill,
}

impl GroupSignal {
    /// Human-readable signal name for logs and metric labels.
    pub(crate) fn name(self) -> &'static str {
        match self {
            GroupSignal::Term => "SIGTERM",
            GroupSignal::Kill => "SIGKILL",
        }
    }
}

/// What a successful [`signal_group`] call observed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Delivery {
    /// The kernel accepted the signal for at least one group member.
    Delivered,
    /// No such process group (already fully gone) -- not a failure.
    AlreadyGone,
}

/// Why a group signal could not be delivered. Always surfaced by the
/// caller (logged + counted) -- the old shell-out ignored this entirely.
#[derive(Debug, thiserror::Error)]
pub(crate) enum SignalError {
    /// The child's PID is not a safe `killpg` target (0 would signal this
    /// service's own group; 1 is init; values above `i32::MAX` wrap
    /// negative and mean "other groups"). Refused rather than risked.
    #[error("refusing to signal the process group of pid {pid}: not a safe group id")]
    UnsafeGroup { pid: u32 },
    /// The child is not the leader of its own process group, so signalling
    /// `-pid` would hit some other group (typically this service's own).
    /// The supervisor spawns every child with `process_group(0)`; seeing
    /// this means that invariant broke.
    #[error("pid {pid} is not its own process-group leader (pgid {pgid})")]
    NotGroupLeader { pid: u32, pgid: i32 },
    /// The `killpg`/`getpgid` syscall itself failed (e.g. `EPERM`).
    #[error("{op}(pgid={pgid}) failed: {source}")]
    Os {
        op: &'static str,
        pgid: i32,
        #[source]
        source: io::Error,
    },
}

/// Validates `pid` as a `killpg` target and returns it as a [`Pid`].
/// Rejects the values whose `killpg` semantics are catastrophic: `0` (the
/// caller's own group), `1` (init), anything that does not fit `i32`, and
/// this service's own process group.
#[cfg(unix)]
fn safe_group_id(pid: u32) -> Result<Pid, SignalError> {
    let raw = i32::try_from(pid).map_err(|_| SignalError::UnsafeGroup { pid })?;
    let candidate = Pid::from_raw(raw);
    if raw <= 1 || candidate == getpgrp() {
        return Err(SignalError::UnsafeGroup { pid });
    }
    Ok(candidate)
}

/// Sends `signal` to the **entire process group** led by `child` (not just
/// the direct PID), so helper processes ffmpeg forks cannot survive
/// teardown holding the media pipes open.
///
/// Must be called before the child is reaped (see the module ordering
/// contract). A zombie leader that has exited but is not yet reaped is a
/// valid target, which is what makes the post-exit sweep possible.
#[cfg(unix)]
pub(crate) fn signal_group(
    child: &mut Child,
    signal: GroupSignal,
) -> Result<Delivery, SignalError> {
    let pid = child.id();
    let pgid = safe_group_id(pid)?;
    match getpgid(Some(pgid)) {
        Ok(actual) if actual == pgid => {}
        Ok(actual) => {
            return Err(SignalError::NotGroupLeader {
                pid,
                pgid: actual.as_raw(),
            })
        }
        Err(Errno::ESRCH) => return Ok(Delivery::AlreadyGone),
        Err(errno) => return Err(os_error("getpgid", pgid, errno)),
    }
    let nix_signal = match signal {
        GroupSignal::Term => Signal::SIGTERM,
        GroupSignal::Kill => Signal::SIGKILL,
    };
    match killpg(pgid, nix_signal) {
        Ok(()) => Ok(Delivery::Delivered),
        Err(Errno::ESRCH) => Ok(Delivery::AlreadyGone),
        Err(errno) => Err(os_error("killpg", pgid, errno)),
    }
}

/// Wraps a failed signalling syscall with the operation and group it hit.
#[cfg(unix)]
fn os_error(op: &'static str, pgid: Pid, errno: Errno) -> SignalError {
    SignalError::Os {
        op,
        pgid: pgid.as_raw(),
        source: errno.into(),
    }
}

/// Non-unix fallback: no portable process groups, so kill just the tracked
/// child. Production targets are Linux containers; this only keeps the
/// crate compiling elsewhere.
#[cfg(not(unix))]
pub(crate) fn signal_group(
    child: &mut Child,
    _signal: GroupSignal,
) -> Result<Delivery, SignalError> {
    child
        .kill()
        .map(|()| Delivery::Delivered)
        .map_err(|source| SignalError::Os {
            op: "kill",
            pgid: 0,
            source,
        })
}

/// Reports whether `child` has exited **without reaping it**
/// (`waitid(WEXITED|WNOHANG|WNOWAIT)`), leaving the zombie in place so the
/// PID stays reserved and the group can still be swept before [`reap`].
/// `ECHILD` (already reaped elsewhere) counts as exited.
#[cfg(unix)]
pub(crate) fn has_exited(child: &mut Child) -> io::Result<bool> {
    let raw = i32::try_from(child.id())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "child pid exceeds i32"))?;
    let flags = WaitPidFlag::WEXITED | WaitPidFlag::WNOHANG | WaitPidFlag::WNOWAIT;
    match waitid(Id::Pid(Pid::from_raw(raw)), flags) {
        Ok(WaitStatus::StillAlive) => Ok(false),
        Ok(_) => Ok(true),
        Err(Errno::ECHILD) => Ok(true),
        // Interrupted by a signal: report "not yet", the caller polls again.
        Err(Errno::EINTR) => Ok(false),
        Err(errno) => Err(errno.into()),
    }
}

/// Non-unix fallback: `try_wait` reaps, which is acceptable where there is
/// no group sweep to preserve.
#[cfg(not(unix))]
pub(crate) fn has_exited(child: &mut Child) -> io::Result<bool> {
    child.try_wait().map(|status| status.is_some())
}

/// Reaps `child` (collects its exit status so no zombie remains) by polling
/// the non-blocking [`Child::try_wait`], bounded by `timeout`. Never blocks
/// an async worker thread -- the old `Child::wait()` did, forever, when the
/// child outlived teardown. Returns `Ok(None)` if the child is still
/// unreaped when the deadline passes; the caller must treat that as a
/// teardown failure, not success.
pub(crate) async fn reap(child: &mut Child, timeout: Duration) -> io::Result<Option<ExitStatus>> {
    let deadline = tokio::time::Instant::now() + timeout;
    let mut delay = Duration::from_millis(2);
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(Some(status));
        }
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            return Ok(None);
        }
        tokio::time::sleep(delay.min(remaining)).await;
        delay = (delay * 2).min(Duration::from_millis(50));
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::os::unix::process::CommandExt;
    use std::process::{Command, Stdio};

    /// Spawns `sh -c script` as the leader of a fresh process group, the
    /// same way the supervisor spawns ffmpeg.
    fn spawn_leader(script: &str) -> Child {
        let mut cmd = Command::new("sh");
        cmd.arg("-c")
            .arg(script)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0);
        cmd.spawn().expect("spawn sh")
    }

    /// Reads the first stdout line of `child` as a PID (the grandchild the
    /// script backgrounds and echoes).
    fn read_grandchild_pid(child: &mut Child) -> u32 {
        let stdout = child.stdout.take().expect("piped stdout");
        let mut line = String::new();
        BufReader::new(stdout)
            .read_line(&mut line)
            .expect("read grandchild pid");
        line.trim().parse().expect("grandchild pid is numeric")
    }

    /// True once `pid` no longer exists or is only a zombie awaiting its
    /// (possibly never-reaping, in a container) init -- either way it is
    /// not running.
    fn is_gone(pid: u32) -> bool {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Err(_) => true,
            Ok(stat) => stat
                .rsplit(')')
                .next()
                .map(|rest| rest.trim_start().starts_with('Z'))
                .unwrap_or(true),
        }
    }

    async fn wait_gone(pid: u32) {
        for _ in 0..200 {
            if is_gone(pid) {
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("pid {pid} still running 2s after the group was signalled");
    }

    #[tokio::test]
    async fn sigterm_reaches_leader_and_grandchild() {
        let mut child = spawn_leader("sleep 300 & echo $!; wait");
        let grandchild = read_grandchild_pid(&mut child);
        assert!(!is_gone(grandchild), "grandchild must be running first");

        let delivery = signal_group(&mut child, GroupSignal::Term).expect("signal delivered");
        assert_eq!(delivery, Delivery::Delivered);

        let status = reap(&mut child, Duration::from_secs(3))
            .await
            .expect("try_wait ok")
            .expect("leader reaped after SIGTERM");
        assert!(!status.success());
        wait_gone(grandchild).await;
    }

    #[tokio::test]
    async fn sigkill_is_required_when_the_group_ignores_sigterm() {
        // `trap '' TERM` is inherited as SIG_IGN by the backgrounded
        // `sleep`, so the whole group shrugs off SIGTERM.
        let mut child =
            spawn_leader("trap '' TERM; sleep 300 & echo $!; while true; do sleep 1; done");
        let grandchild = read_grandchild_pid(&mut child);

        signal_group(&mut child, GroupSignal::Term).expect("TERM delivered");
        tokio::time::sleep(Duration::from_millis(300)).await;
        assert!(
            !has_exited(&mut child).expect("waitid ok"),
            "a TERM-ignoring leader must survive SIGTERM"
        );
        assert!(!is_gone(grandchild));

        signal_group(&mut child, GroupSignal::Kill).expect("KILL delivered");
        let status = reap(&mut child, Duration::from_secs(3))
            .await
            .expect("try_wait ok")
            .expect("leader reaped after SIGKILL");
        assert!(!status.success());
        wait_gone(grandchild).await;
    }

    #[tokio::test]
    async fn has_exited_observes_exit_without_reaping() {
        let mut child = spawn_leader("exit 7");
        let mut exited = false;
        for _ in 0..300 {
            if has_exited(&mut child).expect("waitid ok") {
                exited = true;
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(exited, "child should have exited within 3s");
        // Still reapable -> has_exited did not consume the status.
        let status = reap(&mut child, Duration::from_secs(1))
            .await
            .expect("try_wait ok")
            .expect("zombie still reapable");
        assert_eq!(status.code(), Some(7));
        // Once reaped, waitid reports ECHILD, which counts as exited.
        assert!(has_exited(&mut child).expect("ECHILD maps to exited"));
    }

    #[tokio::test]
    async fn group_can_be_swept_after_the_leader_exits_but_before_reap() {
        // Leader exits immediately but leaves a grandchild behind in the
        // group -- the orphan case the post-exit sweep exists for.
        let mut child = spawn_leader("sleep 300 & echo $!; exit 0");
        let grandchild = read_grandchild_pid(&mut child);
        for _ in 0..300 {
            if has_exited(&mut child).expect("waitid ok") {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(has_exited(&mut child).expect("waitid ok"));
        assert!(!is_gone(grandchild), "orphaned grandchild still running");

        let delivery = signal_group(&mut child, GroupSignal::Kill).expect("sweep delivered");
        assert_eq!(delivery, Delivery::Delivered);
        wait_gone(grandchild).await;
        reap(&mut child, Duration::from_secs(1))
            .await
            .expect("try_wait ok")
            .expect("leader reaped");
    }

    #[tokio::test]
    async fn reap_times_out_on_a_live_child_instead_of_blocking() {
        let mut child = spawn_leader("sleep 300");
        let started = tokio::time::Instant::now();
        let outcome = reap(&mut child, Duration::from_millis(120))
            .await
            .expect("try_wait ok");
        assert!(outcome.is_none(), "live child must not report as reaped");
        assert!(started.elapsed() < Duration::from_secs(2));

        signal_group(&mut child, GroupSignal::Kill).expect("KILL delivered");
        reap(&mut child, Duration::from_secs(3))
            .await
            .expect("try_wait ok")
            .expect("reaped after KILL");
    }

    #[tokio::test]
    async fn a_child_outside_its_own_group_is_refused_not_signalled() {
        // No `process_group(0)`: the child shares THIS test process's
        // group, so `killpg(child_pid)` would be wrong and signalling the
        // real group would kill the test runner. Must refuse.
        let mut cmd = Command::new("sleep");
        cmd.arg("300").stdin(Stdio::null());
        let mut child = cmd.spawn().expect("spawn sleep");

        let err = signal_group(&mut child, GroupSignal::Kill).expect_err("must refuse");
        assert!(
            matches!(err, SignalError::NotGroupLeader { .. }),
            "unexpected error: {err}"
        );
        assert!(
            !has_exited(&mut child).expect("waitid ok"),
            "the refused child must not have been signalled"
        );
        child.kill().expect("direct kill");
        child.wait().expect("reap");
    }

    #[test]
    fn unsafe_group_ids_are_rejected() {
        let own = u32::try_from(getpgrp().as_raw()).expect("own pgrp positive");
        for pid in [0u32, 1, u32::MAX, i32::MAX as u32 + 1, own] {
            assert!(
                matches!(safe_group_id(pid), Err(SignalError::UnsafeGroup { .. })),
                "pid {pid} must be refused"
            );
        }
        // An arbitrary other positive id passes validation (it is only
        // *validated* here, never signalled).
        let other = if own == 4_000_000 {
            4_000_001
        } else {
            4_000_000
        };
        assert!(safe_group_id(other).is_ok());
    }

    #[tokio::test]
    async fn signalling_a_fully_gone_group_reports_already_gone_not_an_error() {
        // Reaped first, then signalled -- deliberately violating the
        // ordering contract (safe here: the PID cannot be recycled within
        // this test) to pin the ESRCH mapping.
        let mut child = spawn_leader("exit 0");
        let status = reap(&mut child, Duration::from_secs(3))
            .await
            .expect("try_wait ok")
            .expect("leader reaped");
        assert!(status.success());
        let delivery = signal_group(&mut child, GroupSignal::Kill).expect("ESRCH is not an error");
        assert_eq!(delivery, Delivery::AlreadyGone);
    }

    #[test]
    fn syscall_failures_are_wrapped_with_the_operation_and_group() {
        let err = os_error("killpg", Pid::from_raw(1234), Errno::EPERM);
        match &err {
            SignalError::Os { op, pgid, source } => {
                assert_eq!(*op, "killpg");
                assert_eq!(*pgid, 1234);
                assert_eq!(source.raw_os_error(), Some(Errno::EPERM as i32));
            }
            other => panic!("unexpected variant: {other:?}"),
        }
        assert!(err.to_string().contains("killpg(pgid=1234)"));
    }

    #[test]
    fn signal_names_and_error_text_are_stable() {
        assert_eq!(GroupSignal::Term.name(), "SIGTERM");
        assert_eq!(GroupSignal::Kill.name(), "SIGKILL");
        let err = SignalError::Os {
            op: "killpg",
            pgid: 42,
            source: io::Error::from_raw_os_error(1),
        };
        let text = err.to_string();
        assert!(text.contains("killpg(pgid=42)"), "got: {text}");
        let err = SignalError::NotGroupLeader { pid: 5, pgid: 3 };
        assert!(err.to_string().contains("pgid 3"));
    }
}

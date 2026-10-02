//! Exercises the compiled `bundle-executor` binary's `--healthcheck`
//! subcommand as a real subprocess (`rules/general.md`: "native Rust
//! healthcheck subcommand -- never curl") -- the one path in `src/main.rs`
//! that is meaningfully testable without a live stage connection.

#[test]
fn healthcheck_subcommand_exits_zero() {
    let bin = env!("CARGO_BIN_EXE_bundle-executor");
    let status = std::process::Command::new(bin)
        .arg("--healthcheck")
        .status()
        .unwrap_or_else(|e| panic!("failed to spawn {bin}: {e}"));
    assert!(status.success(), "healthcheck exited with {status:?}");
}

fn temp_probe_path(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!(
        "bundle-executor-test-main-healthcheck-{}-{label}",
        std::process::id()
    ))
}

/// **`--healthcheck=session` exit codes, exercised as a real subprocess**
/// (regression: executor stuck on terminated svc pod after rollout, alpha
/// 2026-10-02) -- a fresh probe file (as `crate::heartbeat::run_monitor`
/// writes on every healthy tick) must exit `0`.
#[test]
fn session_healthcheck_exits_zero_for_a_fresh_probe_file() {
    let probe_file = temp_probe_path("fresh");
    std::fs::write(
        &probe_file,
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
            .to_string(),
    )
    .unwrap_or_else(|e| panic!("write fresh probe file: {e}"));

    let bin = env!("CARGO_BIN_EXE_bundle-executor");
    let status = std::process::Command::new(bin)
        .args(["--healthcheck=session", "--max-age", "60"])
        .env("EXECUTOR_PROBE_FILE", &probe_file)
        .status()
        .unwrap_or_else(|e| panic!("failed to spawn {bin}: {e}"));
    assert!(
        status.success(),
        "fresh probe file must pass, got {status:?}"
    );

    let _ = std::fs::remove_file(&probe_file);
}

/// A stale probe file (older than `--max-age`) must exit non-zero -- this
/// is the exact signal that makes Kubernetes restart a pod stuck on a
/// half-open host-API connection instead of leaving it marked `Ready`
/// forever.
#[test]
fn session_healthcheck_exits_nonzero_for_a_stale_probe_file() {
    let probe_file = temp_probe_path("stale");
    let stale_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .saturating_sub(120);
    std::fs::write(&probe_file, stale_ts.to_string())
        .unwrap_or_else(|e| panic!("write stale probe file: {e}"));

    let bin = env!("CARGO_BIN_EXE_bundle-executor");
    let status = std::process::Command::new(bin)
        .args(["--healthcheck=session", "--max-age", "60"])
        .env("EXECUTOR_PROBE_FILE", &probe_file)
        .status()
        .unwrap_or_else(|e| panic!("failed to spawn {bin}: {e}"));
    assert!(
        !status.success(),
        "a stale probe file must fail the healthcheck, got {status:?}"
    );

    let _ = std::fs::remove_file(&probe_file);
}

/// Neither the probe file nor its start marker exists at all (a path
/// that's never been touched by the long-running process) -- must fail,
/// never silently pass.
#[test]
fn session_healthcheck_exits_nonzero_when_nothing_has_ever_run() {
    let probe_file = temp_probe_path("never-started");
    let _ = std::fs::remove_file(&probe_file);

    let bin = env!("CARGO_BIN_EXE_bundle-executor");
    let status = std::process::Command::new(bin)
        .args(["--healthcheck=session", "--max-age", "60"])
        .env("EXECUTOR_PROBE_FILE", &probe_file)
        .status()
        .unwrap_or_else(|e| panic!("failed to spawn {bin}: {e}"));
    assert!(
        !status.success(),
        "a probe file that was never written must fail, got {status:?}"
    );
}

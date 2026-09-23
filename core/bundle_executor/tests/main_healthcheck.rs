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

//! Smoke tests for the CLI surface -- later modules' own tests cover the
//! real logic; this only proves the binary starts, parses args, and
//! exits with the documented code for a phase not yet wired.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::process::Command;

#[test]
fn help_exits_zero() {
    let output = Command::new(env!("CARGO_BIN_EXE_bundle-compiler"))
        .arg("--help")
        .output()
        .expect("bundle-compiler binary should be runnable");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("build"));
    assert!(stdout.contains("publish"));
}

#[test]
fn publish_without_wiring_exits_78() {
    let output = Command::new(env!("CARGO_BIN_EXE_bundle-compiler"))
        .args([
            "publish",
            "--component",
            "/tmp/c.wasm",
            "--manifest",
            "/tmp/m.yaml",
            "--language",
            "python",
            "--artifact-kind",
            "source",
        ])
        .output()
        .expect("bundle-compiler binary should be runnable");
    assert_eq!(output.status.code(), Some(78));
}

#[test]
fn build_with_missing_paths_exits_nonzero() {
    // Unlike `publish` (fully unwired -- Task 18), `build` IS wired
    // (Task 7): a nonexistent manifest surfaces as an I/O error, exit
    // code 74, not the Task-2-era placeholder 78.
    let output = Command::new(env!("CARGO_BIN_EXE_bundle-compiler"))
        .args([
            "build",
            "--bundle",
            "/tmp/nonexistent-x",
            "--manifest",
            "/tmp/nonexistent-y",
            "--out",
            "/tmp/nonexistent-z",
        ])
        .output()
        .expect("bundle-compiler binary should be runnable");
    assert_eq!(output.status.code(), Some(74));
}

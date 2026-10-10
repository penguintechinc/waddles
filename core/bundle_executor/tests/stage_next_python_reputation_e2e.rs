//! Issue #726 Python proof -- the seam `componentize-py-eager-wizen` says
//! mocked tests can never see. A Python bundle
//! (`tests/fixtures/reputation-py-bundle/src`) is **freshly compiled on every
//! run** with the exact production recipe (`componentize-py -w stage-next`,
//! pinned `python:3.13-slim-bookworm` image, `waddle_sdk._component_entry`),
//! loaded by the REAL `Executor`, and calls `waddle_sdk.reputation` ->
//! `wit_world.imports.reputation` -> the executor's `reputation::Host` ->
//! the wire. If the SDK's eager wizening of `wit_world.imports.reputation`
//! ever regressed, this build/run would fail with `AttributeError` at the
//! first call -- exactly the silent-degrade failure mode of the flags saga,
//! here caught loudly.
//!
//! Requires `docker` on PATH (same requirement and recipe as
//! `flag_on_command_e2e.rs`); a missing docker or a failed build FAILS the
//! test outright, never skips (`rules/critical-rules.md` Verification
//! Integrity). Only the stage half of the connection is simulated.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use common::{run_many, Scenario, StageAnswer, TARGET_USER};

const APP_ID: &str = "waddles.test.reputation-py";

/// Builds the Python test bundle for `world stage-next` inside the same
/// pinned image/componentize-py version production's builder uses.
fn build_fresh_wasm() -> Vec<u8> {
    const PYTHON_IMAGE: &str =
        "python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26";
    const COMPONENTIZE_PY_VERSION: &str = "0.25.1";

    let repo_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("core/bundle_executor has a parent (core/)")
        .parent()
        .expect("core/ has a parent (repo root)")
        .to_path_buf();
    let out_dir = std::env::temp_dir().join(format!(
        "stage-next-py-reputation-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&out_dir).expect("create temp wasm output dir");

    let build_script = format!(
        "set -eu; export PATH=/tmp/.local/bin:$PATH; \
         pip install --no-cache-dir --quiet componentize-py=={COMPONENTIZE_PY_VERSION}; \
         cd /repo; \
         PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 componentize-py \
            -d wit/waddle-bundle -w stage-next \
            componentize -p sdk/waddle-sdk/src \
            -p core/bundle_executor/tests/fixtures/reputation-py-bundle/src \
            waddle_sdk._component_entry \
            -o /out/reputation-py.wasm"
    );
    let status = std::process::Command::new("docker")
        .args([
            "run",
            "--rm",
            "--user",
            &format!("{}:{}", host_uid(), host_gid()),
            "-e",
            "HOME=/tmp",
            "-v",
            &format!("{}:/repo:ro", repo_root.display()),
            "-v",
            &format!("{}:/out", out_dir.display()),
            PYTHON_IMAGE,
            "sh",
            "-c",
            &build_script,
        ])
        .status()
        .unwrap_or_else(|e| {
            panic!(
                "docker must be on PATH to build the real stage-next Python bundle -- no \
                 mock/stub substitute is permitted at this boundary (error: {e})"
            )
        });
    assert!(
        status.success(),
        "fresh `componentize-py -w stage-next componentize` of the reputation Python bundle \
         FAILED (exit {:?})",
        status.code()
    );
    let bytes = std::fs::read(out_dir.join("reputation-py.wasm"))
        .expect("componentize-py reported success but the wasm is unreadable");
    assert!(!bytes.is_empty(), "freshly built wasm is empty");
    let _ = std::fs::remove_dir_all(&out_dir);
    bytes
}

/// Current uid/gid for the bind-mounted `docker run` (root-owned output in a
/// worktree must never be left behind -- agent-container-user convention).
/// `std` has no portable getuid; shell out once.
fn id_of(flag: &str) -> String {
    let out = std::process::Command::new("id")
        .arg(flag)
        .output()
        .expect("`id` is available");
    String::from_utf8(out.stdout)
        .expect("utf8")
        .trim()
        .to_string()
}
fn host_uid() -> String {
    id_of("-u")
}
fn host_gid() -> String {
    id_of("-g")
}

fn scenario(event_type: &'static str, payload: serde_json::Value, answer: StageAnswer) -> Scenario {
    Scenario {
        event_type,
        payload_json: payload.to_string(),
        answer,
    }
}

/// ONE fresh build + ONE component compile (CPython-on-WASI is expensive),
/// then every scenario as its own invoke on that connection.
#[tokio::test(flavor = "multi_thread")]
async fn python_bundle_calls_reputation_through_the_real_wizened_import_and_fails_loud() {
    let adjust_args = serde_json::json!({"user": TARGET_USER, "delta": 4, "reason": "game.win"});
    let denial_args = serde_json::json!({"user": TARGET_USER, "delta": 1, "reason": "r"});
    let denials = [
        ("not_granted", "denied:not_granted"),
        ("delta_out_of_bounds", "denied:delta_out_of_bounds"),
        ("quota_exceeded", "denied:quota_exceeded"),
        ("user_not_in_scope", "not-a-member"),
        ("daily_cap_exceeded", "daily-cap-exceeded"),
        ("not_implemented", "unavailable"),
    ];

    let mut scenarios = vec![
        scenario(
            "rep-adjust",
            adjust_args.clone(),
            StageAnswer::Ok(serde_json::json!({ "balance": 4 })),
        ),
        scenario(
            "rep-get",
            serde_json::json!({"user": TARGET_USER}),
            StageAnswer::Ok(serde_json::json!({ "balance": 11 })),
        ),
    ];
    for (code, _) in denials {
        scenarios.push(scenario(
            "rep-adjust",
            denial_args.clone(),
            StageAnswer::Err(code, "stage said no"),
        ));
    }

    // CPython needs real linear-memory headroom.
    let wasm = tokio::task::spawn_blocking(build_fresh_wasm)
        .await
        .expect("build task");
    let observed = run_many(&wasm, APP_ID, 256, scenarios).await;

    // adjust: op + exact args crossed the wire, new balance returned.
    assert_eq!(observed[0].0, "reputation.adjust");
    assert_eq!(observed[0].1, adjust_args);
    assert_eq!(observed[0].2, "ok:4");
    // get
    assert_eq!(observed[1].0, "reputation.get");
    assert_eq!(observed[1].1, serde_json::json!({ "user": TARGET_USER }));
    assert_eq!(observed[1].2, "ok:11");
    // Fail-loud through the whole Python stack: every stage denial surfaces as
    // a typed SDK exception, never a swallowed default.
    for (i, (code, expected)) in denials.iter().enumerate() {
        assert_eq!(observed[2 + i].2, *expected, "stage code {code}");
    }
}

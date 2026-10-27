//! Issue #714 Python proof -- the seam `componentize-py-eager-wizen` says
//! mocked tests can never see. A Python bundle
//! (`tests/fixtures/economy-py-bundle/src`) is **freshly compiled on every
//! run** with the exact production recipe (`componentize-py -w stage-next`,
//! pinned `python:3.13-slim-bookworm` image, `waddle_sdk._component_entry`),
//! loaded by the REAL `Executor`, and calls `waddle_sdk.economy` ->
//! `wit_world.imports.economy` -> the executor's `economy::Host` -> the wire.
//! If the SDK's eager wizening of `wit_world.imports.economy` ever regressed,
//! this build/run would fail with `AttributeError` at the first call --
//! exactly the silent-degrade failure mode of the flags saga, here caught
//! loudly.
//!
//! Requires `docker` on PATH (same requirement and recipe as
//! `stage_next_python_reputation_e2e.rs`); a missing docker or a failed build
//! FAILS the test outright, never skips (`rules/critical-rules.md`
//! Verification Integrity). Only the stage half of the connection is
//! simulated.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use common::{run_many, Scenario, StageAnswer, TARGET_USER};

const APP_ID: &str = "waddles.test.economy-py";
const OTHER_USER: &str = "66666666-7777-4888-8999-aaaaaaaaaaaa";

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
        "stage-next-py-economy-{}-{}",
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
            -p core/bundle_executor/tests/fixtures/economy-py-bundle/src \
            waddle_sdk._component_entry \
            -o /out/economy-py.wasm"
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
        "fresh `componentize-py -w stage-next componentize` of the economy Python bundle \
         FAILED (exit {:?})",
        status.code()
    );
    let bytes = std::fs::read(out_dir.join("economy-py.wasm"))
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
async fn python_bundle_calls_economy_through_the_real_wizened_import_and_fails_loud() {
    let wager_args = serde_json::json!({"user": TARGET_USER, "stake": 10, "payout": 25});
    let transfer_args = serde_json::json!({"from": TARGET_USER, "to": OTHER_USER, "amount": 30});
    let user_args = serde_json::json!({"user": TARGET_USER});

    // (event, payload, stage code, number-or-message the stage carries, expected tag)
    let refusals: [(
        &'static str,
        serde_json::Value,
        &'static str,
        &'static str,
        &str,
    ); 8] = [
        (
            "eco-wager",
            wager_args.clone(),
            "not_granted",
            "no",
            "denied:not_granted",
        ),
        (
            "eco-wager",
            wager_args.clone(),
            "amount_out_of_bounds",
            "no",
            "denied:amount_out_of_bounds",
        ),
        (
            "eco-wager",
            wager_args.clone(),
            "quota_exceeded",
            "no",
            "denied:quota_exceeded",
        ),
        (
            "eco-wager",
            wager_args.clone(),
            "user_not_in_scope",
            "no",
            "not-a-member",
        ),
        (
            "eco-wager",
            wager_args.clone(),
            "insufficient_funds",
            "7",
            "insufficient-funds:7",
        ),
        (
            "eco-wager",
            wager_args.clone(),
            "over_cap",
            "50",
            "over-cap:50",
        ),
        (
            "eco-transfer",
            transfer_args.clone(),
            "not_implemented",
            "off",
            "unavailable",
        ),
        (
            "eco-balance",
            user_args.clone(),
            "invalid_args",
            "bad",
            "invalid",
        ),
    ];

    let mut scenarios = vec![
        scenario(
            "eco-balance",
            user_args.clone(),
            StageAnswer::Ok(serde_json::json!({ "balance": 420 })),
        ),
        scenario(
            "eco-wager",
            wager_args.clone(),
            StageAnswer::Ok(serde_json::json!({ "balance": 115 })),
        ),
        scenario(
            "eco-transfer",
            transfer_args.clone(),
            StageAnswer::Ok(serde_json::json!({})),
        ),
        scenario(
            "eco-max-bet",
            user_args.clone(),
            StageAnswer::Ok(serde_json::json!({ "max_bet": 50 })),
        ),
        scenario(
            "eco-board",
            serde_json::json!({"limit": 2}),
            StageAnswer::Ok(serde_json::json!({ "entries": [
                { "user": TARGET_USER, "balance": 300 },
                { "user": OTHER_USER, "balance": 50 },
            ]})),
        ),
    ];
    for (event, payload, code, message, _) in &refusals {
        scenarios.push(scenario(
            event,
            payload.clone(),
            StageAnswer::Err(code, message),
        ));
    }

    // CPython needs real linear-memory headroom.
    let wasm = tokio::task::spawn_blocking(build_fresh_wasm)
        .await
        .expect("build task");
    let observed = run_many(&wasm, APP_ID, 256, scenarios).await;

    // Each op: the wire op + exact args crossed, the typed result came back.
    assert_eq!(observed[0].0, "economy.balance");
    assert_eq!(observed[0].1, user_args);
    assert_eq!(observed[0].2, "ok:420");

    assert_eq!(observed[1].0, "economy.wager");
    assert_eq!(observed[1].1, wager_args);
    assert_eq!(observed[1].2, "ok:115");

    assert_eq!(observed[2].0, "economy.transfer");
    assert_eq!(observed[2].1, transfer_args);
    assert_eq!(observed[2].2, "ok");

    assert_eq!(observed[3].0, "economy.max_bet");
    assert_eq!(observed[3].2, "ok:50");

    assert_eq!(observed[4].0, "economy.leaderboard");
    assert_eq!(observed[4].1, serde_json::json!({ "limit": 2 }));
    assert_eq!(
        observed[4].2,
        format!("ok:{TARGET_USER}=300,{OTHER_USER}=50")
    );

    // Fail-loud through the whole Python stack: every stage refusal surfaces
    // as a typed SDK exception (numbers intact), never a swallowed default.
    for (i, (_, _, code, _, expected)) in refusals.iter().enumerate() {
        assert_eq!(observed[5 + i].2, *expected, "stage code {code}");
    }
}

//! `ai.generate` Python proof -- the seam `componentize-py-eager-wizen` says
//! mocked tests can never see. A Python bundle
//! (`tests/fixtures/ai-py-bundle/src`) is **freshly compiled on every run** with
//! the exact production recipe (`componentize-py -w stage-next`, pinned
//! `python:3.13-slim-bookworm` image, `waddle_sdk._component_entry`), loaded by
//! the REAL `Executor`, and calls `waddle_sdk.ai` -> `wit_world.imports.ai` ->
//! the executor's `ai::Host` -> the wire. If the SDK's eager wizening of
//! `wit_world.imports.ai` ever regressed, this build/run would fail with
//! `AttributeError` at the first call -- exactly the silent-degrade failure
//! mode of the flags saga, here caught loudly.
//!
//! Requires `docker` on PATH (same requirement and recipe as
//! `stage_next_python_economy_e2e.rs`); a missing docker or a failed build
//! FAILS the test outright, never skips (`rules/critical-rules.md`
//! Verification Integrity). Only the stage half of the connection is simulated.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use common::{run_many, Scenario, StageAnswer};

const APP_ID: &str = "waddles.test.ai-py";

/// Builds the Python test bundle for `world stage-next` inside the same pinned
/// image/componentize-py version production's builder uses.
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
        "stage-next-py-ai-{}-{}",
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
            -p core/bundle_executor/tests/fixtures/ai-py-bundle/src \
            waddle_sdk._component_entry \
            -o /out/ai-py.wasm"
    );
    let status = std::process::Command::new("docker")
        .args([
            "run",
            "--rm",
            "--user",
            &format!("{}:{}", id_of("-u"), id_of("-g")),
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
        "fresh `componentize-py -w stage-next componentize` of the ai Python bundle FAILED \
         (exit {:?})",
        status.code()
    );
    let bytes = std::fs::read(out_dir.join("ai-py.wasm"))
        .expect("componentize-py reported success but the wasm is unreadable");
    assert!(!bytes.is_empty(), "freshly built wasm is empty");
    let _ = std::fs::remove_dir_all(&out_dir);
    bytes
}

/// `id <flag>` -- the current uid/gid for the bind-mounted `docker run` (a
/// root-owned output in a worktree must never be left behind).
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

fn scenario(payload: serde_json::Value, answer: StageAnswer) -> Scenario {
    Scenario {
        event_type: "ai-generate",
        payload_json: payload.to_string(),
        answer,
    }
}

/// ONE fresh build + ONE component compile (CPython-on-WASI is expensive), then
/// every scenario as its own invoke on that connection.
#[tokio::test(flavor = "multi_thread")]
async fn python_bundle_calls_ai_through_the_real_wizened_import_and_fails_loud() {
    let plain = serde_json::json!({"prompt": "say hello"});
    let full = serde_json::json!({
        "prompt": "say hello", "max_tokens": 64, "temperature_x100": 25, "json": true
    });

    // (stage code, message the stage carries, expected tag)
    let refusals: [(&'static str, &'static str, &str); 8] = [
        ("not_granted", "no", "denied:not_granted"),
        ("rate_limited", "no", "denied:rate_limited"),
        (
            "contains_pii",
            "prompt contains email",
            "denied:contains_pii",
        ),
        ("too_many_tokens", "512", "too-many-tokens:512"),
        ("content_filtered", "none", "content-filtered"),
        ("unavailable", "model timed out", "unavailable"),
        ("invalid_args", "bad", "invalid"),
        ("backend", "boom", "backend"),
    ];

    let mut scenarios = vec![
        scenario(
            plain.clone(),
            StageAnswer::Ok(serde_json::json!({ "text": "Hello, friend!" })),
        ),
        scenario(
            full.clone(),
            StageAnswer::Ok(serde_json::json!({ "text": "{\"a\":1}" })),
        ),
    ];
    for (code, message, _) in &refusals {
        scenarios.push(scenario(plain.clone(), StageAnswer::Err(code, message)));
    }

    // CPython needs real linear-memory headroom.
    let wasm = tokio::task::spawn_blocking(build_fresh_wasm)
        .await
        .expect("build task");
    let observed = run_many(&wasm, APP_ID, 256, scenarios).await;

    // The wire op + exact args crossed, the typed result came back.
    assert_eq!(observed[0].0, "ai.generate");
    assert_eq!(
        observed[0].1,
        serde_json::json!({
            "prompt": "say hello", "max_tokens": null, "temperature": null, "json": false
        })
    );
    assert_eq!(observed[0].2, "ok:Hello, friend!");

    assert_eq!(observed[1].0, "ai.generate");
    assert_eq!(
        observed[1].1,
        serde_json::json!({
            "prompt": "say hello", "max_tokens": 64, "temperature": 0.25, "json": true
        })
    );
    assert_eq!(observed[1].2, "ok:{\"a\":1}");

    // Fail-loud through the whole Python stack: every stage refusal surfaces as
    // a typed SDK exception (numbers intact), never a swallowed empty completion.
    for (i, (code, _, expected)) in refusals.iter().enumerate() {
        assert_eq!(observed[2 + i].2, *expected, "stage code {code}");
    }
}

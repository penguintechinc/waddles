//! Python proof for the bundle `identity` capability -- the seam
//! `componentize-py-eager-wizen` says mocked tests can never see. A Python
//! reference points-game bundle (`tests/fixtures/identity-py-bundle/src`) is
//! **freshly compiled on every run** with the exact production recipe
//! (`componentize-py -w stage-next`, pinned `python:3.13-slim-bookworm` image,
//! `waddle_sdk._component_entry`), loaded by the REAL `Executor`, and calls
//! `waddle_sdk.identity` -> `wit_world.imports.identity` -> the executor's
//! `identity::Host` -> the wire. If the SDK's eager wizening of
//! `wit_world.imports.identity` ever regressed, this build/run would fail with
//! `AttributeError` at the first call -- exactly the silent-degrade failure mode
//! of the flags saga, here caught loudly.
//!
//! The bundle is the REFERENCE shape of a `!steal @user` game: it reads the
//! mention token out of the message text, resolves the actor and the mention to
//! community uuids through the host, then moves currency between exactly those
//! two uuids -- and stops, surfacing a clear error, on any refusal.
//!
//! Requires `docker` on PATH (same requirement and recipe as
//! `stage_next_python_economy_e2e.rs`); a missing docker or a failed build
//! FAILS the test outright, never skips (`rules/critical-rules.md`
//! Verification Integrity). Only the stage half of the connection is
//! simulated.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use common::{run_many_detailed, Scenario, StageAnswer};

const APP_ID: &str = "waddles.test.identity-py";
const ALICE: &str = "aaaaaaaa-1111-4222-8333-444444444444";
const BOB: &str = "bbbbbbbb-1111-4222-8333-444444444444";
/// The opaque pseudonym token the bundle was shown in `{user:<token>}`.
const TOKEN: &str = "cccccccc-5555-4666-8777-888888888888";

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
        "stage-next-py-identity-{}-{}",
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
            -p core/bundle_executor/tests/fixtures/identity-py-bundle/src \
            waddle_sdk._component_entry \
            -o /out/identity-py.wasm"
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
        "fresh `componentize-py -w stage-next componentize` of the identity Python bundle \
         FAILED (exit {:?})",
        status.code()
    );
    let bytes = std::fs::read(out_dir.join("identity-py.wasm"))
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
async fn python_bundle_resolves_identities_through_the_real_wizened_import_and_fails_loud() {
    let steal_payload = serde_json::json!({
        "text": format!("!steal {{user:{TOKEN}}} 30"),
        "amount": 30,
    });
    let token_payload = serde_json::json!({ "token": TOKEN });

    // (event, payload, stage code, message, expected tag)
    let refusals: [(
        &'static str,
        serde_json::Value,
        &'static str,
        &'static str,
        &str,
    ); 9] = [
        (
            "id-actor",
            serde_json::json!({}),
            "not_linked",
            "x",
            "not-linked",
        ),
        (
            "id-actor",
            serde_json::json!({}),
            "not_a_member",
            "x",
            "not-a-member",
        ),
        (
            "id-mention",
            token_payload.clone(),
            "not_found",
            "x",
            "not-found",
        ),
        (
            "id-mention",
            token_payload.clone(),
            "ambiguous",
            "x",
            "ambiguous",
        ),
        (
            "id-actor",
            serde_json::json!({}),
            "not_granted",
            "no",
            "denied:not_granted",
        ),
        (
            "id-mention",
            token_payload.clone(),
            "rate_limited",
            "no",
            "denied:rate_limited",
        ),
        (
            "id-actor",
            serde_json::json!({}),
            "not_implemented",
            "off",
            "unavailable",
        ),
        (
            "id-mention",
            token_payload.clone(),
            "invalid_args",
            "bad",
            "invalid",
        ),
        (
            "id-actor",
            serde_json::json!({}),
            "backend",
            "boom",
            "backend",
        ),
    ];

    let mut scenarios = vec![
        scenario(
            "id-actor",
            serde_json::json!({}),
            StageAnswer::Ok(serde_json::json!({ "user": ALICE })),
        ),
        scenario(
            "id-mention",
            token_payload.clone(),
            StageAnswer::Ok(serde_json::json!({ "user": BOB })),
        ),
        // The full reference flow: actor, mention, then a transfer between
        // exactly the two resolved uuids.
        scenario(
            "id-steal",
            steal_payload.clone(),
            StageAnswer::PerOp(vec![
                (
                    "identity.resolve_actor",
                    Ok(serde_json::json!({ "user": ALICE })),
                ),
                (
                    "identity.resolve_mention",
                    Ok(serde_json::json!({ "user": BOB })),
                ),
                ("economy.transfer", Ok(serde_json::json!({}))),
            ]),
        ),
        // An unlinked actor stops the flow before anything else is attempted.
        scenario(
            "id-steal",
            steal_payload.clone(),
            StageAnswer::PerOp(vec![(
                "identity.resolve_actor",
                Err(("not_linked", "identity is not linked")),
            )]),
        ),
        // An unlinked TARGET: actor resolved, mention refused, no transfer.
        scenario(
            "id-steal",
            steal_payload.clone(),
            StageAnswer::PerOp(vec![
                (
                    "identity.resolve_actor",
                    Ok(serde_json::json!({ "user": ALICE })),
                ),
                (
                    "identity.resolve_mention",
                    Err(("not_linked", "identity is not linked")),
                ),
            ]),
        ),
        // A stage bug that leaked a platform id where the uuid belongs.
        scenario(
            "id-actor",
            serde_json::json!({}),
            StageAnswer::Ok(serde_json::json!({ "user": "1001" })),
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
    let observed = run_many_detailed(&wasm, APP_ID, 256, scenarios).await;

    // actor: no argument crossed; the typed result came back.
    assert_eq!(
        observed[0].0,
        vec![("identity.resolve_actor".to_string(), serde_json::json!({}))]
    );
    assert_eq!(observed[0].1, format!("ok:{ALICE}"));

    // mention: only the opaque token crossed.
    assert_eq!(
        observed[1].0,
        vec![(
            "identity.resolve_mention".to_string(),
            serde_json::json!({ "token": TOKEN })
        )]
    );
    assert_eq!(observed[1].1, format!("ok:{BOB}"));

    // The reference flow: the transfer args are EXACTLY the resolved uuids, and
    // the mention token was extracted from the message text by the SDK helper.
    assert_eq!(observed[2].1, "steal:ok");
    assert_eq!(
        observed[2].0,
        vec![
            ("identity.resolve_actor".to_string(), serde_json::json!({})),
            (
                "identity.resolve_mention".to_string(),
                serde_json::json!({ "token": TOKEN })
            ),
            (
                "economy.transfer".to_string(),
                serde_json::json!({ "from": ALICE, "to": BOB, "amount": 30 })
            ),
        ]
    );

    // Unlinked actor / target: a clear error, and no currency call.
    assert_eq!(observed[3].1, "actor:not-linked");
    assert_eq!(observed[3].0.len(), 1);
    assert_eq!(observed[4].1, "target:not-linked");
    assert_eq!(observed[4].0.len(), 2);
    assert!(observed[4].0.iter().all(|(op, _)| op != "economy.transfer"));

    // A leaked non-uuid never reaches the bundle as an identity.
    assert_eq!(observed[5].1, "backend");

    // Fail-loud through the whole Python stack: every stage refusal surfaces
    // as a typed SDK exception, never a swallowed default.
    for (i, (_, _, code, _, expected)) in refusals.iter().enumerate() {
        assert_eq!(observed[6 + i].1, *expected, "stage code {code}");
    }
}

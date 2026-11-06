//! `media` proof: a REAL compiled `world stage-next` component
//! (`tests/fixtures/media_fixture.wasm`) loads into the executor's production
//! `Linker`, CALLS each `media` host import, and every call crosses the real wire
//! bridge as `capability = db`, `op = "media.*"` with the exact args shape
//! `core/svc_process::capabilities::handle_media` decodes (that handler is
//! exercised with the same JSON in `core/svc_process/tests/media_e2e.rs`). Only
//! the stage half is simulated here. The executor does no network I/O, no tier
//! check and no prompt inspection -- this proves it is a pure, typed bridge.

#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use bundle_executor::engine::{build_engine, build_linker_for};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use common::{run_many, test_config, Scenario, StageAnswer};

const FIXTURE_WASM: &[u8] = include_bytes!("fixtures/media_fixture.wasm");
const APP_ID: &str = "waddles.test.media-fixture";

/// One load (one wasm compile) and one invoke per `(event, payload, answer)`.
async fn run_all(
    scenarios: Vec<(&'static str, &str, StageAnswer)>,
) -> Vec<(String, serde_json::Value, String)> {
    let scenarios = scenarios
        .into_iter()
        .map(|(event_type, payload, answer)| Scenario {
            event_type,
            payload_json: payload.to_string(),
            answer,
        })
        .collect();
    run_many(FIXTURE_WASM, APP_ID, 64, scenarios).await
}

#[tokio::test]
async fn every_op_crosses_the_bridge_with_only_guest_words_and_decodes_its_result() {
    use serde_json::json;
    let ok = StageAnswer::Ok;
    let out = run_all(vec![
        (
            "media-image",
            r#"{"prompt":"a cat"}"#,
            ok(json!({"handle":"media://1/2/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"})),
        ),
        (
            "media-submit-video",
            r#"{"prompt":"clip"}"#,
            ok(json!({"job_id":"j1"})),
        ),
        (
            "media-submit-music",
            r#"{"prompt":"tune"}"#,
            ok(json!({"job_id":"j2"})),
        ),
        (
            "media-status",
            r#"{"job_id":"j1"}"#,
            ok(json!({"state":"pending"})),
        ),
        (
            "media-status",
            r#"{"job_id":"j1"}"#,
            ok(json!({"state":"done","handle":"media://1/2/x.mp4"})),
        ),
        (
            "media-status",
            r#"{"job_id":"j1"}"#,
            ok(json!({"state":"failed","code":"provider_refused","message":"safety"})),
        ),
        (
            "media-url",
            r#"{"handle":"media://1/2/x.mp4"}"#,
            ok(json!({"url":"https://s/x"})),
        ),
    ])
    .await;
    let want: [(&str, serde_json::Value, &str); 7] = [
        (
            "media.generate_image",
            json!({"prompt":"a cat"}),
            "handle:media://1/2/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
        ),
        ("media.submit_video", json!({"prompt":"clip"}), "job:j1"),
        ("media.submit_music", json!({"prompt":"tune"}), "job:j2"),
        ("media.job_status", json!({"job_id":"j1"}), "pending"),
        (
            "media.job_status",
            json!({"job_id":"j1"}),
            "done:media://1/2/x.mp4",
        ),
        (
            "media.job_status",
            json!({"job_id":"j1"}),
            "failed:provider-refused:safety",
        ),
        (
            "media.object_url",
            json!({"handle":"media://1/2/x.mp4"}),
            "url:https://s/x",
        ),
    ];
    assert_eq!(out.len(), want.len());
    for ((op, args, result), (wop, wargs, wres)) in out.iter().zip(want.iter()) {
        assert_eq!((op.as_str(), args, result.as_str()), (*wop, wargs, *wres));
    }
}

#[tokio::test]
async fn stage_refusals_map_to_their_typed_variants() {
    let cases: [(&'static str, &'static str, String); 16] = [
        (
            "tier_required",
            "professional",
            "tier-required:professional".into(),
        ),
        (
            "tier_required",
            "license_unknown",
            "tier-required:license_unknown".into(),
        ),
        ("byok_missing", "gemini", "byok-missing:gemini".into()),
        (
            "provider_refused",
            "safety",
            "provider-refused:safety".into(),
        ),
        ("quota", "minute", "quota:minute".into()),
        ("timeout", "", "timeout".into()),
        ("not_found", "", "not-found".into()),
        ("invalid_args", "bad", "invalid:bad".into()),
        ("not_implemented", "wiring", "unavailable:wiring".into()),
        (
            "feature_disabled",
            "flag off",
            "unavailable:flag off".into(),
        ),
        ("backend", "boom", "backend:boom".into()),
        ("not_granted", "refused", "denied:not_granted".into()),
        ("rate_limited", "refused", "denied:rate_limited".into()),
        ("quota_exceeded", "refused", "denied:quota_exceeded".into()),
        (
            "instance_denied",
            "refused",
            "denied:instance_denied".into(),
        ),
        ("contains_pii", "refused", "denied:contains_pii".into()),
    ];
    let out = run_all(
        cases
            .iter()
            .map(|(c, m, _)| ("media-image", r#"{"prompt":"x"}"#, StageAnswer::Err(c, m)))
            .collect(),
    )
    .await;
    for ((_, _, got), (code, _, want)) in out.iter().zip(cases.iter()) {
        assert_eq!(got, want, "stage code {code}");
    }
}

#[tokio::test]
async fn a_malformed_success_payload_is_a_loud_backend_error() {
    let bad: Vec<(&'static str, &str, serde_json::Value)> = vec![
        (
            "media-image",
            r#"{"prompt":"x"}"#,
            serde_json::json!({"unexpected": true}),
        ),
        (
            "media-submit-video",
            r#"{"prompt":"x"}"#,
            serde_json::json!({"job_id": 7}),
        ),
        (
            "media-status",
            r#"{"job_id":"j"}"#,
            serde_json::json!({"state":"done"}),
        ),
        (
            "media-status",
            r#"{"job_id":"j"}"#,
            serde_json::json!({"state":"sideways"}),
        ),
        ("media-url", r#"{"handle":"h"}"#, serde_json::json!({})),
    ];
    let out = run_all(
        bad.iter()
            .map(|(e, p, v)| (*e, *p, StageAnswer::Ok(v.clone())))
            .collect(),
    )
    .await;
    for ((_, _, got), (event, _, _)) in out.iter().zip(bad.iter()) {
        assert!(
            got.starts_with("backend:malformed host-result"),
            "{event}: {got}"
        );
    }
}

#[tokio::test]
async fn stage_world_linker_refuses_a_component_importing_media() {
    let engine = build_engine(&test_config()).expect("engine");
    let component = wasmtime::component::Component::new(&engine, FIXTURE_WASM).expect("component");
    let stage = VerifiedManifest::new(APP_ID, "sha256:aa", WitWorld::Stage, Default::default());
    assert!(build_linker_for(&engine, &stage)
        .expect("stage linker")
        .instantiate_pre(&component)
        .is_err());
    let next = VerifiedManifest::new(APP_ID, "sha256:bb", WitWorld::StageNext, Default::default());
    assert!(build_linker_for(&engine, &next)
        .expect("stage-next linker")
        .instantiate_pre(&component)
        .is_ok());
}

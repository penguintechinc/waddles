//! Real-path proof for the YouTube overlay (P6b T3): the REAL compiled
//! `bundles/rust/youtube` component loaded through the production
//! `ConnectorHost::load` (connector-tools world, digest-checked, fuel/epoch/
//! memory bounded), driving `receiver` + `tool-provider` exports.
//!
//! Needs the built wasm, so it is `#[ignore]`d in a plain `cargo test`; run it
//! via `scripts/test-overlay-youtube.sh` (builds the component, sets
//! `YOUTUBE_OVERLAY_WASM`, runs `--ignored`). A missing env var FAILS (never
//! skips) once the test is selected.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::{
    ConnectionCtx, ConnectorCallError, ConnectorHost, ConnectorLimits, FrameError, RawFrame,
};
use bundle_executor::engine::{build_engine, spawn_epoch_ticker, LinkerCache};
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use sha2::{Digest, Sha256};

const APP: &str = "waddles.core.service.youtube";

fn wasm() -> Vec<u8> {
    let p = std::env::var("YOUTUBE_OVERLAY_WASM")
        .expect("YOUTUBE_OVERLAY_WASM must point at the built youtube.wasm");
    std::fs::read(p).expect("read youtube wasm")
}

async fn host() -> (ConnectorHost, tokio::task::JoinHandle<()>) {
    use clap::Parser;
    let cfg = CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-ingest:8301",
    ])
    .unwrap();
    let engine = build_engine(&cfg).unwrap();
    let ticker = spawn_epoch_ticker(&engine);
    let linkers = LinkerCache::new(engine.clone());
    let bytes = wasm();
    let digest = format!("sha256:{:x}", Sha256::digest(&bytes));
    let manifest = VerifiedManifest::new(APP, digest, WitWorld::ConnectorTools, HashSet::new());
    let host = ConnectorHost::load(
        &engine,
        &linkers,
        &manifest,
        bytes,
        ConnectorLimits {
            fuel: 50_000_000,
            deadline_ms: 2_000,
            memory_limit_mb: 64,
            pool_size: 4,
            pool_wait: Duration::from_millis(200),
            max_tool_args_bytes: 900_000,
            max_tool_result_bytes: 900_000,
        },
        None,
    )
    .await
    .unwrap();
    (host, ticker)
}

fn ctx() -> ConnectionCtx {
    ConnectionCtx {
        platform: "youtube".into(),
        source_id: "poll-abc".into(),
        connection_id: "conn-1".into(),
        session_token: None,
    }
}

fn frame(bytes: &[u8]) -> RawFrame {
    RawFrame {
        ctx: ctx(),
        bytes: bytes.to_vec(),
        received_at: "2026-11-05T00:00:00.000Z".into(),
    }
}

const LIST: &str = r#"{"pollingIntervalMillis":5000,"nextPageToken":"T","items":[{"id":"msg-1",
"snippet":{"displayMessage":"  hello chat  ","publishedAt":"2026-09-11T00:00:00Z","liveChatId":"chat123"},
"authorDetails":{"channelId":"UCviewer1","displayName":"Alice","isChatModerator":false,
"isChatOwner":false,"isChatSponsor":false}}]}"#;

#[tokio::test]
#[ignore = "needs built wasm; run scripts/test-overlay-youtube.sh"]
async fn real_component_normalizes_a_poll_response() {
    let (host, _t) = host().await;
    let hs = host.on_connect(ctx()).await.unwrap();
    assert!(hs.value.bytes.is_empty() && hs.value.secret_refs.is_empty());

    let ev = host.on_frame(frame(LIST.as_bytes())).await.unwrap().value;
    assert_eq!(ev.len(), 1);
    assert_eq!(ev[0].platform, "youtube");
    assert_eq!(ev[0].event_type, "message");
    assert_eq!(ev[0].actor.as_deref(), Some("UCviewer1"));
    assert_eq!(ev[0].occurred_at, "2026-09-11T00:00:00Z");
    let p: serde_json::Value = serde_json::from_str(&ev[0].payload_json).unwrap();
    assert_eq!(p["text"], "hello chat");
    assert_eq!(p["live_chat_id"], "chat123");
    assert_eq!(p["message_id"], "msg-1");
    assert_eq!(p["is_mod"], false);

    assert_eq!(host.on_heartbeat_due(ctx()).await.unwrap().value, None);
}

#[tokio::test]
#[ignore = "needs built wasm; run scripts/test-overlay-youtube.sh"]
async fn real_component_corpus_raw_frame_and_malformed() {
    let (host, _t) = host().await;
    let corpus: serde_json::Value = serde_json::from_slice(
        &std::fs::read(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/data_plane_parity/corpus/youtube/live_chat_message.json"
        ))
        .unwrap(),
    )
    .unwrap();
    let ev = host
        .on_frame(frame(&serde_json::to_vec(&corpus["frame"]).unwrap()))
        .await
        .unwrap()
        .value;
    let p: serde_json::Value = serde_json::from_str(&ev[0].payload_json).unwrap();
    assert_eq!(p, corpus["golden"]["payload"]);
    assert_eq!(ev[0].actor.as_deref(), corpus["golden"]["actor"].as_str());

    let err = host.on_frame(frame(b"not json")).await.unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Guest(FrameError::Malformed(_))),
        "{err:?}"
    );
}

#[tokio::test]
#[ignore = "needs built wasm; run scripts/test-overlay-youtube.sh"]
async fn real_component_tools_poll_plan_classify_and_send_template() {
    let (host, _t) = host().await;
    let call = |name: &str, v: serde_json::Value| {
        host.call_tool(name.into(), serde_json::to_vec(&v).unwrap())
    };
    let plan: serde_json::Value = serde_json::from_slice(
        &call(
            "poll_plan",
            serde_json::json!({"body": {"nextPageToken": "N", "pollingIntervalMillis": 100}}),
        )
        .await
        .unwrap()
        .value,
    )
    .unwrap();
    assert_eq!(
        plan,
        serde_json::json!({"next_page_token": "N", "interval_ms": 2000})
    );

    let v: serde_json::Value = serde_json::from_slice(
        &call(
            "classify_error",
            serde_json::json!({"status": 403, "body": {"error": {"errors": [{"reason": "quotaExceeded"}]}}}),
        )
        .await
        .unwrap()
        .value,
    )
    .unwrap();
    assert_eq!(v["action"], "backoff");

    let t: serde_json::Value = serde_json::from_slice(
        &call(
            "build_send_chat_request",
            serde_json::json!({"target_source_id": "chat9", "payload": {"text": "hi"}}),
        )
        .await
        .unwrap()
        .value,
    )
    .unwrap();
    assert_eq!(t["secret_refs"][0][1], "youtube.oauth");
    assert!(!t.to_string().contains("ya29"), "no literal token");

    let err = call("nope", serde_json::json!({})).await.unwrap_err();
    assert!(matches!(err, ConnectorCallError::Tool(_)), "{err:?}");
}

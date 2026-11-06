//! Real-path proof for the YouTube connector (P6b T3): the REAL compiled
//! `bundles/rust/youtube` component loaded through the production
//! `ConnectorHost::load` (connector-tools world, digest-checked, fuel/epoch/
//! memory bounded), driving `receiver` + `tool-provider` exports.
//!
//! Needs the built wasm, so it is `#[ignore]`d in a plain `cargo test`; run it
//! via `scripts/test-connector-youtube.sh` (builds the component, sets
//! `YOUTUBE_CONNECTOR_WASM`, runs `--ignored`). A missing env var FAILS (never
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
    let p = std::env::var("YOUTUBE_CONNECTOR_WASM")
        .expect("YOUTUBE_CONNECTOR_WASM must point at the built youtube.wasm");
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
    let perms: HashSet<String> = HashSet::from(["connector.pii.read".to_string()]);
    let manifest = VerifiedManifest::new(APP, digest, WitWorld::ConnectorTools, perms);
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

/// Handshake/heartbeat are identity-free; frame normalization (which needs the identity
/// bridge) is covered by the shared harness (`make test-connector-parity PLATFORM=youtube`).
/// Without a bridge, `on-frame` for an actor MUST fail loud, never emit a raw handle.
#[tokio::test]
#[ignore = "needs built wasm; run scripts/test-connector-youtube.sh"]
async fn real_component_connect_heartbeat_and_no_identity_fails_loud() {
    let (host, _t) = host().await;
    let hs = host.on_connect(ctx()).await.unwrap();
    assert!(hs.value.bytes.is_empty() && hs.value.secret_refs.is_empty());
    assert_eq!(host.on_heartbeat_due(ctx()).await.unwrap().value, None);

    let err = host
        .on_frame(frame(
            br#"{"text":"hi","live_chat_id":"c","author_id":"UCx"}"#,
        ))
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Guest(FrameError::Backend(_))),
        "{err:?}"
    );
    let err = host.on_frame(frame(b"not json")).await.unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Guest(FrameError::Malformed(_))),
        "{err:?}"
    );
}

#[tokio::test]
#[ignore = "needs built wasm; run scripts/test-connector-youtube.sh"]
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
    assert_eq!(t["secret_refs"][1][1], "youtube.live_chat_id");
    assert!(!t.to_string().contains("ya29"), "no literal token");

    let err = call("nope", serde_json::json!({})).await.unwrap_err();
    assert!(matches!(err, ConnectorCallError::Tool(_)), "{err:?}");
}

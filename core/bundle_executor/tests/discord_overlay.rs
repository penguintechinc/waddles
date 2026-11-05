//! Real-path proof of the Discord platform overlay (P6b T2): the REAL compiled
//! `bundles/rust/discord` component, loaded through the production
//! `build_engine` / `LinkerCache` / `ConnectorHost::load` path on the
//! `connector-tools` world, driven through every receiver export plus the tool
//! provider. Nothing here is mocked; the only absent piece is the stage bridge
//! (no host `http`), which the overlay must survive by failing closed.
//!
//! `fixtures/discord_overlay.wasm` is a build output of `bundles/rust/discord`
//! (`cargo component build --release --target wasm32-wasip2 --locked`); rebuild
//! it with `fixtures/README.md` "discord_overlay.wasm" whenever that crate changes.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::{
    ConnectionCtx, ConnectorCallError, ConnectorHost, ConnectorLimits, FrameError, RawFrame,
    ToolError,
};
use bundle_executor::engine::{build_engine, spawn_epoch_ticker, LinkerCache};
use bundle_executor::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use sha2::{Digest, Sha256};

const WASM: &[u8] = include_bytes!("fixtures/discord_overlay.wasm");
const APP: &str = "waddles.core.service.discord";

fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
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
    let limits = ConnectorLimits {
        fuel: 50_000_000,
        deadline_ms: 2_000,
        memory_limit_mb: 64,
        pool_size: 4,
        pool_wait: Duration::from_millis(200),
        max_tool_args_bytes: 900_000,
        max_tool_result_bytes: 900_000,
    };
    let m = VerifiedManifest::new(
        APP,
        digest_of(WASM),
        WitWorld::ConnectorTools,
        HashSet::from([PERM_CONNECTOR_PII_READ.to_string()]),
    );
    let host = ConnectorHost::load(&engine, &linkers, &m, WASM.to_vec(), limits, None)
        .await
        .expect("the real discord overlay must load on the production linker");
    (host, ticker)
}

fn ctx(token: Option<&str>) -> ConnectionCtx {
    ConnectionCtx {
        platform: "discord".to_string(),
        source_id: "src-1".to_string(),
        connection_id: "conn-1".to_string(),
        session_token: token.map(str::to_string),
    }
}

fn frame(token: Option<&str>, v: serde_json::Value) -> RawFrame {
    RawFrame {
        ctx: ctx(token),
        bytes: serde_json::to_vec(&v).unwrap(),
        received_at: "2026-11-05T00:00:00.000Z".to_string(),
    }
}

#[tokio::test]
async fn on_connect_identifies_with_a_slot_and_an_opaque_handle_never_a_token() {
    let (host, _t) = host().await;
    let hs = host.on_connect(ctx(None)).await.unwrap().value;
    let text = String::from_utf8(hs.bytes).unwrap();
    assert!(
        text.contains(r#""token":"{{DISCORD_BOT_TOKEN}}""#),
        "{text}"
    );
    assert!(text.contains(r#""op":2"#));
    assert_eq!(
        hs.secret_refs,
        vec![(
            "{{DISCORD_BOT_TOKEN}}".to_string(),
            "discord-bot".to_string()
        )]
    );
    // RESUME when the host supplies a session.
    let hs = host
        .on_connect(ctx(Some(r#"{"session_id":"s1","seq":9}"#)))
        .await
        .unwrap()
        .value;
    let v: serde_json::Value = serde_json::from_slice(&hs.bytes).unwrap();
    assert_eq!(
        (v["op"].as_u64(), v["d"]["seq"].as_u64()),
        (Some(6), Some(9))
    );
    // A bad token fails loud as a typed guest error, not a trap.
    let err = host.on_connect(ctx(Some("sess-1"))).await.unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Guest(FrameError::Malformed(_))),
        "{err:?}"
    );
}

#[tokio::test]
async fn message_create_without_an_identity_bridge_fails_loud_never_a_raw_handle() {
    // Pinned connectors have no identity/host-call bridge in this rig (the full
    // success path with a scripted identity bridge is the shared parity harness,
    // `make test-connector-parity PLATFORM=discord`). The overlay must refuse to
    // emit the raw handle: the frame fails with a typed guest error, not a trap.
    let (host, _t) = host().await;
    for guild in [None, Some("111")] {
        let mut d = serde_json::json!({
            "author":{"bot":false,"id":"444","username":"someuser"},
            "channel_id":"222","content":"hello chat","id":"333","type":0});
        if let Some(g) = guild {
            d["guild_id"] = g.into();
        }
        let err = host
            .on_frame(frame(
                Some(r#"{"self_user_id":"999"}"#),
                serde_json::json!({"op":0,"s":1,"t":"MESSAGE_CREATE","d":d}),
            ))
            .await
            .expect_err("no identity bridge: the frame must fail, never emit a raw handle");
        assert!(
            matches!(err, ConnectorCallError::Guest(FrameError::Backend(_))),
            "{err:?}"
        );
    }
    // Self-authored is dropped BEFORE any identity/REST work.
    let none = host
        .on_frame(frame(
            Some(r#"{"self_user_id":"444"}"#),
            serde_json::json!({"op":0,"s":2,"t":"MESSAGE_CREATE","d":{
                "author":{"id":"444","username":"x"},"channel_id":"222"}}),
        ))
        .await
        .unwrap()
        .value;
    assert!(none.is_empty());
}

#[tokio::test]
async fn interaction_ping_lifecycle_and_garbage_through_the_real_engine() {
    let (host, _t) = host().await;
    let cmd = host
        .on_frame(frame(
            None,
            serde_json::json!({"id":"777","type":2,"token":"SECRET","guild_id":"1","channel_id":"2",
                "member":{"permissions":"8"},"data":{"name":"ping"}}),
        ))
        .await
        .unwrap()
        .value;
    assert_eq!(cmd[0].event_type, "interaction.command");
    assert_eq!(
        cmd[0].actor, None,
        "no user object -> no actor, no identity lookup"
    );
    assert!(!cmd[0].payload_json.contains("SECRET"));
    let ping = host
        .on_frame(frame(None, serde_json::json!({"id":"1","type":1})))
        .await
        .unwrap()
        .value;
    assert!(ping.is_empty());
    let hello = host
        .on_frame(frame(
            None,
            serde_json::json!({"op":10,"d":{"heartbeat_interval":41250}}),
        ))
        .await
        .unwrap()
        .value;
    assert!(hello.is_empty());
    let err = host
        .on_frame(RawFrame {
            ctx: ctx(None),
            bytes: b"nope".to_vec(),
            received_at: "t".into(),
        })
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Guest(FrameError::Malformed(_))),
        "{err:?}"
    );
}

#[tokio::test]
async fn heartbeat_body_tracks_the_host_sequence_and_tools_classify() {
    let (host, _t) = host().await;
    assert_eq!(host.on_heartbeat_due(ctx(None)).await.unwrap().value, None);
    let hb = host
        .on_heartbeat_due(ctx(Some(r#"{"seq":7}"#)))
        .await
        .unwrap()
        .value
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&hb).unwrap()["d"],
        7
    );
    host.on_disconnect(ctx(None), "closed".into())
        .await
        .unwrap();

    let reply = host
        .call_tool(
            "classify_frame".into(),
            serde_json::to_vec(&serde_json::json!({"frame":{"op":9,"d":true}})).unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&reply.value).unwrap(),
        serde_json::json!({"action":"invalid_session","resumable":true})
    );
    let err = host
        .call_tool("nope".into(), b"{}".to_vec())
        .await
        .unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::Tool(ToolError::InvalidArgs(_))),
        "{err:?}"
    );
}

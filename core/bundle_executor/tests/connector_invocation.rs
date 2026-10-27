//! Real-path proof that the executor can INVOKE connector-world exports
//! (issue #840 "B0"; spec `docs/superpowers/specs/2026-09-28-connector-bundles.md`
//! S2.3 pinning, S2.6 trap isolation, S3.5 fuel/deadline/memory bounds).
//!
//! Two real, compiled `waddle:connector@1.0.0` components -- never a mock:
//! - `tests/fixtures/echo_connector.wasm` -- the Echo reference connector.
//! - `tests/fixtures/faulty_connector.wasm` -- fault injection (trap, spin,
//!   memory hog, event flood) to prove the host CONTAINS a bad connector.
//!
//! Every test goes through the production `build_engine` / `LinkerCache` /
//! `ConnectorHost::load` path and a real epoch ticker.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;
use std::time::Duration;

use bundle_executor::config::CliConfig;
use bundle_executor::connector::{
    ConnectionCtx, ConnectorCallError, ConnectorHost, ConnectorLimits, FrameError, RawFrame,
};
use bundle_executor::engine::{build_engine, spawn_epoch_ticker, LinkerCache};
use bundle_executor::error::ExecutorError;
use bundle_executor::manifest::{VerifiedManifest, WitWorld};
use sha2::{Digest, Sha256};
use wasmtime::Engine;

const ECHO_WASM: &[u8] = include_bytes!("fixtures/echo_connector.wasm");
const FAULTY_WASM: &[u8] = include_bytes!("fixtures/faulty_connector.wasm");

fn test_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .unwrap()
}

fn digest_of(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("sha256:{:x}", hasher.finalize())
}

fn limits() -> ConnectorLimits {
    ConnectorLimits {
        fuel: 50_000_000,
        deadline_ms: 2_000,
        memory_limit_mb: 64,
        pool_size: 4,
        pool_wait: Duration::from_millis(200),
    }
}

fn manifest(app_id: &str, bytes: &[u8], world: WitWorld) -> VerifiedManifest {
    VerifiedManifest::new(app_id, digest_of(bytes), world, HashSet::new())
}

fn ctx() -> ConnectionCtx {
    ConnectionCtx {
        platform: "echo".to_string(),
        source_id: "src-1".to_string(),
        connection_id: "conn-1".to_string(),
        session_token: None,
    }
}

fn frame(bytes: &[u8]) -> RawFrame {
    RawFrame {
        ctx: ctx(),
        bytes: bytes.to_vec(),
        received_at: "2026-10-12T00:00:00.000Z".to_string(),
    }
}

struct Rig {
    engine: Engine,
    linkers: LinkerCache,
    _ticker: tokio::task::JoinHandle<()>,
}

fn rig() -> Rig {
    let engine = build_engine(&test_config()).unwrap();
    let ticker = spawn_epoch_ticker(&engine);
    Rig {
        linkers: LinkerCache::new(engine.clone()),
        engine,
        _ticker: ticker,
    }
}

async fn load(rig: &Rig, app_id: &str, bytes: &[u8], limits: ConnectorLimits) -> ConnectorHost {
    ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest(app_id, bytes, WitWorld::Connector),
        bytes.to_vec(),
        limits,
        None,
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn echo_on_connect_returns_its_handshake_bytes() {
    let rig = rig();
    let host = load(&rig, "waddles.core.example.echo", ECHO_WASM, limits()).await;
    let reply = host.on_connect(ctx()).await.unwrap();
    assert_eq!(reply.value.bytes, b"echo-hello");
    assert!(reply.value.secret_refs.is_empty());
    assert!(
        reply.fuel_used > 0,
        "fuel metering must be live on connector calls"
    );
}

#[tokio::test]
async fn echo_on_frame_normalizes_like_the_legacy_ingest_handler() {
    let rig = rig();
    let host = load(&rig, "waddles.core.example.echo", ECHO_WASM, limits()).await;
    let reply = host
        .on_frame(frame(
            br#"{"source":"echo","text":"  hello world  ","actor":"alice"}"#,
        ))
        .await
        .unwrap();
    assert_eq!(reply.value.len(), 1);
    let ev = &reply.value[0];
    assert_eq!(ev.platform, "echo");
    assert_eq!(ev.event_type, "message");
    assert_eq!(ev.actor.as_deref(), Some("alice"));
    assert_eq!(ev.payload_json, r#"{"text":"hello world"}"#);
    // No `occurred_at` on the raw event -> the host-stamped arrival time.
    assert_eq!(ev.occurred_at, "2026-10-12T00:00:00.000Z");
    assert!(reply.fuel_used > 0);
}

#[tokio::test]
async fn echo_defaults_actor_and_keeps_an_explicit_event_type_and_timestamp() {
    let rig = rig();
    let host = load(&rig, "waddles.core.example.echo", ECHO_WASM, limits()).await;
    let reply = host
        .on_frame(frame(
            br#"{"source":"echo","text":"x","event_type":"chat.message","occurred_at":"2026-01-02T03:04:05.006Z"}"#,
        ))
        .await
        .unwrap();
    let ev = &reply.value[0];
    assert_eq!(ev.event_type, "chat.message");
    assert_eq!(ev.actor.as_deref(), Some("unknown"));
    assert_eq!(ev.occurred_at, "2026-01-02T03:04:05.006Z");
}

#[tokio::test]
async fn echo_rejects_a_malformed_frame_with_a_typed_guest_error_not_a_trap() {
    let rig = rig();
    let host = load(&rig, "waddles.core.example.echo", ECHO_WASM, limits()).await;
    for bad in [
        br#"{"text":"no source"}"#.as_slice(),
        br#"{"source":"echo"}"#.as_slice(),
        br#"{"source":"","text":"x"}"#.as_slice(),
        b"not json at all".as_slice(),
    ] {
        let err = host.on_frame(frame(bad)).await.unwrap_err();
        assert!(
            matches!(err, ConnectorCallError::Guest(FrameError::Malformed(_))),
            "expected a guest malformed error, got {err:?}"
        );
        assert!(!err.is_sandbox_trip());
    }
}

#[tokio::test]
async fn echo_heartbeat_and_disconnect_are_callable() {
    let rig = rig();
    let host = load(&rig, "waddles.core.example.echo", ECHO_WASM, limits()).await;
    assert_eq!(host.on_heartbeat_due(ctx()).await.unwrap().value, None);
    host.on_disconnect(ctx(), "test".to_string()).await.unwrap();
}

#[tokio::test]
async fn load_refuses_a_non_connector_world_manifest() {
    let rig = rig();
    let err = ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest("waddles.core.example.echo", ECHO_WASM, WitWorld::Stage),
        ECHO_WASM.to_vec(),
        limits(),
        None,
    )
    .await
    .err()
    .expect("a stage-world manifest must not load as a connector");
    assert!(matches!(err, ExecutorError::ConnectorLoad(_)), "{err:?}");
}

#[tokio::test]
async fn load_refuses_a_non_core_namespace_even_with_valid_bytes() {
    let rig = rig();
    let err = ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest("vendor.acme.echo", ECHO_WASM, WitWorld::Connector),
        ECHO_WASM.to_vec(),
        limits(),
        None,
    )
    .await
    .err()
    .expect("a vendor connector must not load");
    assert!(matches!(err, ExecutorError::ConnectorLoad(ref m) if m.contains("core-namespaced")));
}

#[tokio::test]
async fn load_refuses_bytes_that_do_not_match_the_manifest_digest() {
    let rig = rig();
    let err = ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest("waddles.core.example.echo", ECHO_WASM, WitWorld::Connector),
        FAULTY_WASM.to_vec(),
        limits(),
        None,
    )
    .await
    .err()
    .expect("tampered bytes must not load");
    assert!(
        matches!(err, ExecutorError::DigestMismatch { .. }),
        "{err:?}"
    );
}

#[tokio::test]
async fn load_refuses_a_stage_component_claiming_the_connector_world() {
    let rig = rig();
    let stage_bytes = include_bytes!("fixtures/hostile_fixture.wasm");
    let err = ConnectorHost::load(
        &rig.engine,
        &rig.linkers,
        &manifest(
            "waddles.core.example.echo",
            stage_bytes,
            WitWorld::Connector,
        ),
        stage_bytes.to_vec(),
        limits(),
        None,
    )
    .await
    .err()
    .expect("a stage component must not load as a connector");
    assert!(matches!(err, ExecutorError::ConnectorLoad(_)), "{err:?}");
}

#[tokio::test]
async fn a_guest_trap_is_contained_and_the_host_keeps_serving() {
    let rig = rig();
    let host = load(&rig, "waddles.core.test.faulty", FAULTY_WASM, limits()).await;
    let err = host.on_frame(frame(b"trap")).await.unwrap_err();
    assert!(matches!(err, ConnectorCallError::Trap(_)), "{err:?}");
    assert!(err.is_sandbox_trip());
    // The trapped Store was destroyed; a later, unrelated call is unaffected.
    let ok = host.on_frame(frame(b"ok")).await.unwrap();
    assert_eq!(ok.value.len(), 1);
    assert_eq!(
        host.available_slots(),
        limits().pool_size,
        "the trapped call's slot must be released"
    );
}

#[tokio::test]
async fn an_unbounded_loop_is_stopped_by_fuel_before_the_deadline() {
    let rig = rig();
    let mut l = limits();
    l.fuel = 2_000_000;
    l.deadline_ms = 30_000; // far away: only fuel can stop this call
    let host = load(&rig, "waddles.core.test.faulty", FAULTY_WASM, l).await;
    let started = std::time::Instant::now();
    let err = host.on_frame(frame(b"spin")).await.unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::FuelExhausted(_)),
        "{err:?}"
    );
    assert!(
        started.elapsed() < Duration::from_secs(10),
        "fuel must stop a spin promptly, took {:?}",
        started.elapsed()
    );
}

#[tokio::test]
async fn an_unbounded_loop_is_stopped_by_the_epoch_deadline_when_fuel_is_plentiful() {
    let rig = rig();
    let mut l = limits();
    l.fuel = u64::MAX;
    l.deadline_ms = 150;
    let host = load(&rig, "waddles.core.test.faulty", FAULTY_WASM, l).await;
    let err = host.on_frame(frame(b"spin")).await.unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::DeadlineExceeded(_)),
        "{err:?}"
    );
}

#[tokio::test]
async fn a_memory_hog_is_stopped_by_the_per_instance_cap() {
    let rig = rig();
    let mut l = limits();
    l.memory_limit_mb = 16;
    let host = load(&rig, "waddles.core.test.faulty", FAULTY_WASM, l).await;
    let err = host.on_frame(frame(b"grow")).await.unwrap_err();
    assert!(matches!(err, ConnectorCallError::MemoryLimit(_)), "{err:?}");
}

#[tokio::test]
async fn a_full_pool_is_typed_back_pressure_not_an_unbounded_queue() {
    let rig = rig();
    let mut l = limits();
    l.pool_size = 1;
    l.pool_wait = Duration::from_millis(50);
    l.fuel = u64::MAX;
    l.deadline_ms = 1_500;
    let host = load(&rig, "waddles.core.test.faulty", FAULTY_WASM, l).await;

    // One slow call occupies the only slot...
    let busy = {
        let host = host.clone();
        tokio::spawn(async move { host.on_frame(frame(b"spin")).await })
    };
    tokio::time::sleep(Duration::from_millis(200)).await;
    // ...so a second call is refused, loudly, after pool_wait.
    let err = host.on_frame(frame(b"ok")).await.unwrap_err();
    assert!(
        matches!(err, ConnectorCallError::PoolExhausted { .. }),
        "{err:?}"
    );
    assert!(!err.is_sandbox_trip(), "back-pressure is not a guest fault");

    let busy_result = busy.await.unwrap();
    assert!(matches!(
        busy_result,
        Err(ConnectorCallError::DeadlineExceeded(_))
    ));
    // Slot freed again once the slow call ended.
    assert_eq!(host.available_slots(), 1);
    host.on_frame(frame(b"ok")).await.unwrap();
}

#[tokio::test]
async fn concurrent_calls_proceed_in_parallel_across_the_pool() {
    let rig = rig();
    let host = load(&rig, "waddles.core.example.echo", ECHO_WASM, limits()).await;
    let mut tasks = Vec::new();
    for i in 0..16 {
        let host = host.clone();
        tasks.push(tokio::spawn(async move {
            let body = format!(r#"{{"source":"echo","text":"msg-{i}"}}"#);
            host.on_frame(frame(body.as_bytes())).await
        }));
    }
    let mut seen = 0;
    for (i, t) in tasks.into_iter().enumerate() {
        let reply = t.await.unwrap().unwrap();
        assert_eq!(
            reply.value[0].payload_json,
            format!(r#"{{"text":"msg-{i}"}}"#),
            "each call must see only its own frame (no cross-call state)"
        );
        seen += 1;
    }
    assert_eq!(seen, 16);
}

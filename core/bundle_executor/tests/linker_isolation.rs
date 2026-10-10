//! Per-component `Linker` isolation tests (spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` S3.2.1, task
//! requirement: "linking is cached per (digest, permission-set)" +
//! instantiation-level proof of the `identity.lookup` gate).
//!
//! Two real components exercise this, both compiled ahead of time (never a
//! hand-rolled wasm byte stub, matching this crate's own "never fake a
//! host call" discipline):
//! - `tests/fixtures/connector_fixture.wasm` -- a genuine `connector@1.0.0`
//!   component (`tests/fixtures/connector-fixture-src`) whose `on-connect`
//!   unconditionally calls `identity.lookup`, so its compiled import set
//!   genuinely requires `identity.lookup` to be linked.
//! - `tests/fixtures/hostile_fixture.wasm` -- the existing `stage@1.0.0`
//!   component (`context`/`kv`/`db`/`relay`/... imports), reused here to
//!   prove a `stage`-world component cannot link against a `connector`-world
//!   `Linker` at all.

// Integration-test-only opt-out, same rationale as
// `tests/host_bridge_integration.rs`'s own header comment: the crate-wide
// `[lints.clippy] unwrap_used/expect_used = "deny"` still reaches `tests/`
// targets, and assertions here read naturally as `expect`.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashSet;

use bundle_executor::config::CliConfig;
use bundle_executor::engine::{build_engine, LinkerCache};
use bundle_executor::host::ExecState;
use bundle_executor::manifest::{VerifiedManifest, WitWorld, PERM_CONNECTOR_PII_READ};
use wasmtime::component::Component;
use wasmtime::Store;

const CONNECTOR_FIXTURE_WASM: &[u8] = include_bytes!("fixtures/connector_fixture.wasm");
const STAGE_FIXTURE_WASM: &[u8] = include_bytes!("fixtures/hostile_fixture.wasm");

fn test_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .unwrap()
}

fn core_connector_manifest(digest: &str, granted: bool) -> VerifiedManifest {
    let mut perms = HashSet::new();
    if granted {
        perms.insert(PERM_CONNECTOR_PII_READ.to_string());
    }
    VerifiedManifest::new(
        "waddles.core.connector.discord",
        digest,
        WitWorld::Connector,
        perms,
    )
}

fn vendor_connector_manifest(digest: &str) -> VerifiedManifest {
    // A vendor component should never actually be signed under a
    // `connector`-world manifest granting `connector.pii.read` (spec
    // S3.2.1 gate 1) -- this manifest models the defense-in-depth case
    // gate 2 must independently refuse even if gate 1 were somehow
    // bypassed.
    let mut perms = HashSet::new();
    perms.insert(PERM_CONNECTOR_PII_READ.to_string());
    VerifiedManifest::new(
        "vendor.acme.connector.discord",
        digest,
        WitWorld::Connector,
        perms,
    )
}

fn stage_manifest(digest: &str) -> VerifiedManifest {
    VerifiedManifest::new(
        "waddles.core.some-app",
        digest,
        WitWorld::Stage,
        HashSet::new(),
    )
}

/// Core connector, `connector.pii.read` granted: `identity.lookup` links,
/// and the component (which calls it unconditionally) instantiates.
#[tokio::test]
async fn core_connector_with_grants_links_and_instantiates() {
    let engine = build_engine(&test_config()).expect("engine builds");
    let cache = LinkerCache::new(engine.clone());
    let manifest = core_connector_manifest("sha256:core-granted", true);
    let linker = cache.get_or_build(&manifest).expect("linker builds");

    let component = Component::new(&engine, CONNECTOR_FIXTURE_WASM).expect("component compiles");
    let mut store = Store::new(&engine, ExecState::new(None, "test-app".to_string(), 1));
    store.set_epoch_deadline(1_000_000);
    // build_engine enables fuel accounting unconditionally: arm a generous
    // budget so these linking tests don't trip the out-of-fuel trap.
    store
        .set_fuel(10_000_000)
        .expect("fuel accounting is enabled");
    let result = linker.instantiate_async(&mut store, &component).await;
    assert!(
        result.is_ok(),
        "core connector with connector.pii.read granted must instantiate: {result:?}"
    );
}

/// Core connector, `connector.pii.read` NOT granted: `identity.lookup`
/// never gets registered, so the same component (which imports it
/// unconditionally) fails at instantiation -- not merely "would return
/// denied at call time" (spec S3.2.1's explicit distinction).
#[tokio::test]
async fn core_connector_without_grant_fails_to_instantiate() {
    let engine = build_engine(&test_config()).expect("engine builds");
    let cache = LinkerCache::new(engine.clone());
    let manifest = core_connector_manifest("sha256:core-ungranted", false);
    let linker = cache.get_or_build(&manifest).expect("linker builds");

    let component = Component::new(&engine, CONNECTOR_FIXTURE_WASM).expect("component compiles");
    let mut store = Store::new(&engine, ExecState::new(None, "test-app".to_string(), 1));
    store.set_epoch_deadline(1_000_000);
    // build_engine enables fuel accounting unconditionally: arm a generous
    // budget so these linking tests don't trip the out-of-fuel trap.
    store
        .set_fuel(10_000_000)
        .expect("fuel accounting is enabled");
    let result = linker.instantiate_async(&mut store, &component).await;
    assert!(
        result.is_err(),
        "identity.lookup must not be linked without connector.pii.read"
    );
}

/// A non-core (vendor) namespace never links `identity.lookup` even if its
/// manifest somehow carries the grant (spec S3.2.1 "why both, not just
/// one" -- gate 2 does not trust namespace alone).
#[tokio::test]
async fn vendor_component_importing_identity_lookup_fails_to_instantiate() {
    let engine = build_engine(&test_config()).expect("engine builds");
    let cache = LinkerCache::new(engine.clone());
    let manifest = vendor_connector_manifest("sha256:vendor");
    let linker = cache.get_or_build(&manifest).expect("linker builds");

    let component = Component::new(&engine, CONNECTOR_FIXTURE_WASM).expect("component compiles");
    let mut store = Store::new(&engine, ExecState::new(None, "test-app".to_string(), 1));
    store.set_epoch_deadline(1_000_000);
    // build_engine enables fuel accounting unconditionally: arm a generous
    // budget so these linking tests don't trip the out-of-fuel trap.
    store
        .set_fuel(10_000_000)
        .expect("fuel accounting is enabled");
    let result = linker.instantiate_async(&mut store, &component).await;
    assert!(
        result.is_err(),
        "a vendor-namespaced component must never link identity.lookup"
    );
}

/// A `stage`-world component (`context`/`kv`/`db`/`relay`/... imports)
/// cannot instantiate against a `connector`-world `Linker` -- the two
/// worlds' import sets are disjoint by construction (spec S3.2.1 gate 2:
/// "a `stage`/`stage-v1_1` component ... never has the `connector` world's
/// interfaces registered under any circumstance", and symmetrically here).
#[tokio::test]
async fn stage_component_cannot_link_against_a_connector_linker() {
    let engine = build_engine(&test_config()).expect("engine builds");
    let cache = LinkerCache::new(engine.clone());
    let manifest = core_connector_manifest("sha256:stage-vs-connector", true);
    let linker = cache.get_or_build(&manifest).expect("linker builds");

    let component = Component::new(&engine, STAGE_FIXTURE_WASM).expect("component compiles");
    let mut store = Store::new(&engine, ExecState::new(None, "test-app".to_string(), 1));
    store.set_epoch_deadline(1_000_000);
    // build_engine enables fuel accounting unconditionally: arm a generous
    // budget so these linking tests don't trip the out-of-fuel trap.
    store
        .set_fuel(10_000_000)
        .expect("fuel accounting is enabled");
    let result = linker.instantiate_async(&mut store, &component).await;
    assert!(
        result.is_err(),
        "a stage-world component must not link against a connector-world Linker"
    );
}

/// `LinkerCache` returns the SAME `Linker` (by `Arc` identity) for two
/// manifests sharing a `(digest, permission-set)` key, and a DIFFERENT one
/// once either changes (task requirement: "linking is cached per (digest,
/// permission-set)").
#[test]
fn linker_cache_reuses_by_digest_and_permission_set() {
    let engine = build_engine(&test_config()).expect("engine builds");
    let cache = LinkerCache::new(engine);

    let m1 = core_connector_manifest("sha256:same", true);
    let m2 = core_connector_manifest("sha256:same", true);
    let a = cache.get_or_build(&m1).expect("builds");
    let b = cache.get_or_build(&m2).expect("builds");
    assert!(
        std::sync::Arc::ptr_eq(&a, &b),
        "identical (digest, permission-set) must reuse the same Linker"
    );

    let m3 = core_connector_manifest("sha256:same", false);
    let c = cache.get_or_build(&m3).expect("builds");
    assert!(
        !std::sync::Arc::ptr_eq(&a, &c),
        "a different permission-set on the same digest must build a fresh Linker"
    );

    let m4 = core_connector_manifest("sha256:different", true);
    let d = cache.get_or_build(&m4).expect("builds");
    assert!(
        !std::sync::Arc::ptr_eq(&a, &d),
        "a different digest must build a fresh Linker"
    );
}

/// A `stage`-world manifest builds a `Linker` a `stage` component can
/// actually instantiate against -- the existing full-capability path
/// (`Stage::add_to_linker`) is unaffected by this task's connector-world
/// addition.
#[tokio::test]
async fn stage_manifest_still_links_the_stage_world() {
    let engine = build_engine(&test_config()).expect("engine builds");
    let cache = LinkerCache::new(engine.clone());
    let manifest = stage_manifest("sha256:stage-ok");
    let linker = cache.get_or_build(&manifest).expect("linker builds");

    let component = Component::new(&engine, STAGE_FIXTURE_WASM).expect("component compiles");
    let mut store = Store::new(&engine, ExecState::new(None, "test-app".to_string(), 1));
    store.set_epoch_deadline(1_000_000);
    // build_engine enables fuel accounting unconditionally: arm a generous
    // budget so these linking tests don't trip the out-of-fuel trap.
    store
        .set_fuel(10_000_000)
        .expect("fuel accounting is enabled");
    let result = linker.instantiate_async(&mut store, &component).await;
    assert!(
        result.is_ok(),
        "a stage component must still link the stage world: {result:?}"
    );
}

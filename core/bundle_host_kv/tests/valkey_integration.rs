//! Integration test proving `crate::backend`'s Lua scripts (not just the
//! in-memory fake) enforce isolation and quotas against a real Valkey
//! server. `crate::lib`'s unit tests already exercise every orchestration
//! path against `backend::fake::FakeBackend`; this file exists because
//! that fake is a second, hand-written implementation of the same
//! admit/quota/TTL semantics, and only a real server proves the actual
//! `EVAL` scripts agree with it.
//!
//! Requires Docker (via `testcontainers`) -- CI (`rust-bundle-host-kv.yml`)
//! runs on `ubuntu-latest`, which ships Docker; local runs need it too.

use bundle_host_kv::{KvHost, KvScope, MAX_KEYS_PER_APP, MAX_VALUE_BYTES};
use redis::aio::MultiplexedConnection;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage};

/// Exact, immutable Valkey image version -- not `testcontainers-modules`'s
/// prebuilt "redis" module, which pins a `testcontainers` line whose
/// `astral-tokio-tar` transitive dependency carries four unpatched RustSec
/// advisories (see `Cargo.toml`'s dependency comment). `testcontainers`'
/// own `GenericImage::new(name, tag)` API pulls by `name:tag`, not
/// `name@sha256:digest`, so this pins the most specific equivalent it
/// supports: an exact upstream version tag (confirmed to resolve to
/// `sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507`
/// at the time this was pinned), never a floating `8`/`8-alpine`/`latest`.
const VALKEY_IMAGE: &str = "valkey/valkey";
const VALKEY_TAG: &str = "8.1.10-alpine";

/// Starts one Valkey container and returns it alongside its connection
/// URL, so a test can open as many independent connections as it needs
/// (e.g. one for `KvHost`, a second to seed state directly for a quota
/// test) against the same server.
async fn start() -> (ContainerAsync<GenericImage>, String) {
    let container = GenericImage::new(VALKEY_IMAGE, VALKEY_TAG)
        .with_exposed_port(ContainerPort::Tcp(6379))
        .with_wait_for(WaitFor::message_on_stdout("Ready to accept connections"))
        .start()
        .await
        .expect("valkey test container starts");
    let host = container.get_host().await.expect("container host");
    let port = container
        .get_host_port_ipv4(6379)
        .await
        .expect("container port");
    (container, format!("redis://{host}:{port}/"))
}

async fn connect(url: &str) -> MultiplexedConnection {
    redis::Client::open(url)
        .expect("client opens")
        .get_multiplexed_async_connection()
        .await
        .expect("connection established")
}

fn scope(tenant: &str, community: Option<&str>, app_id: &str) -> KvScope {
    KvScope::new(tenant, community.map(str::to_string), app_id)
}

#[tokio::test]
async fn set_then_get_round_trips_through_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    host.set(&scope, 1, "greeting", b"hello valkey", 0)
        .await
        .unwrap();
    let got = host.get(&scope, 2, "greeting").await.unwrap();
    assert_eq!(got, Some(b"hello valkey".to_vec()));
}

#[tokio::test]
async fn ttl_actually_expires_the_key_in_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let scope = scope("acme", None, "waddles.bot.a");

    host.set(&scope, 1, "ephemeral", b"gone-soon", 1)
        .await
        .unwrap();
    assert_eq!(
        host.get(&scope, 2, "ephemeral").await.unwrap(),
        Some(b"gone-soon".to_vec())
    );

    tokio::time::sleep(std::time::Duration::from_millis(1_500)).await;
    assert_eq!(host.get(&scope, 3, "ephemeral").await.unwrap(), None);
}

#[tokio::test]
async fn delete_is_reflected_immediately_in_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    host.set(&scope, 1, "k", b"v", 0).await.unwrap();
    host.delete(&scope, 2, "k").await.unwrap();
    assert_eq!(host.get(&scope, 3, "k").await.unwrap(), None);
}

#[tokio::test]
async fn increment_is_atomic_and_persists_across_calls_in_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    assert_eq!(host.increment(&scope, 1, "hits", 5, 0).await.unwrap(), 5);
    assert_eq!(host.increment(&scope, 2, "hits", -2, 0).await.unwrap(), 3);
    assert_eq!(host.increment(&scope, 3, "hits", 10, 0).await.unwrap(), 13);
}

/// Cross-app isolation, proven against real Valkey (not just the fake):
/// two apps in the same tenant/community writing the same guest key name
/// must never observe each other's value.
#[tokio::test]
async fn cross_app_isolation_holds_against_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let app_a = scope("acme", Some("main"), "waddles.bot.a");
    let app_b = scope("acme", Some("main"), "waddles.bot.b");

    host.set(&app_a, 1, "secret", b"a-only", 0).await.unwrap();
    assert_eq!(host.get(&app_b, 1, "secret").await.unwrap(), None);

    host.set(&app_b, 1, "secret", b"b-only", 0).await.unwrap();
    assert_eq!(
        host.get(&app_a, 2, "secret").await.unwrap(),
        Some(b"a-only".to_vec()),
        "app a's value must be unaffected by app b writing the same key name"
    );
}

/// Cross-tenant isolation, proven against real Valkey: identical
/// `(community, app_id)` in two different tenants are fully separate
/// namespaces.
#[tokio::test]
async fn cross_tenant_isolation_holds_against_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let acme = scope("acme", Some("main"), "waddles.bot.a");
    let globex = scope("globex", Some("main"), "waddles.bot.a");

    host.set(&acme, 1, "secret", b"acme-value", 0)
        .await
        .unwrap();
    assert_eq!(host.get(&globex, 1, "secret").await.unwrap(), None);
}

/// Quota enforcement against real Valkey: proves the Lua script's atomic
/// admit-or-reject logic, not just the fake's plain-Rust reimplementation.
/// A second, independent connection to the same container seeds the app's
/// counter directly at its ceiling -- equivalent to
/// `backend::fake::FakeBackend::seed_count`, without a slow loop writing
/// `MAX_KEYS_PER_APP` real keys first.
#[tokio::test]
async fn key_count_quota_is_enforced_by_the_real_lua_script() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    let mut seed_conn = connect(&url).await;
    redis::AsyncCommands::set::<_, _, ()>(&mut seed_conn, scope.count_key(), MAX_KEYS_PER_APP)
        .await
        .unwrap();

    let host = KvHost::new(connect(&url).await);
    let err = host
        .set(&scope, 1, "one-too-many", b"x", 0)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "quota_exceeded");
}

/// Overwriting an already-live key must stay exempt from the key-count
/// quota against the real backend too (unit-tested against the fake in
/// `crate::tests::overwriting_an_existing_key_is_exempt_from_the_key_count_quota`).
#[tokio::test]
async fn overwriting_an_existing_key_is_exempt_from_quota_against_real_valkey() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host = KvHost::new(connect(&url).await);

    host.set(&scope, 1, "existing", b"v1", 0).await.unwrap();

    let mut seed_conn = connect(&url).await;
    redis::AsyncCommands::set::<_, _, ()>(&mut seed_conn, scope.count_key(), MAX_KEYS_PER_APP)
        .await
        .unwrap();

    // Overwriting the pre-existing key succeeds despite the quota being full.
    host.set(&scope, 2, "existing", b"v2", 0).await.unwrap();
    assert_eq!(
        host.get(&scope, 3, "existing").await.unwrap(),
        Some(b"v2".to_vec())
    );

    // A genuinely new key is still rejected.
    let err = host.set(&scope, 4, "brand-new", b"x", 0).await.unwrap_err();
    assert_eq!(err.code(), "quota_exceeded");
}

#[tokio::test]
async fn value_size_quota_is_enforced_before_any_backend_round_trip() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await);
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let oversized = vec![0u8; MAX_VALUE_BYTES + 1];

    let err = host.set(&scope, 1, "k", &oversized, 0).await.unwrap_err();
    assert_eq!(err.wire_code(), "too_large");
}

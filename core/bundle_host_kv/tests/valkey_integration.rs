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

use std::sync::Arc;
use std::time::Duration;

use bundle_host_kv::backend::LIVE_INDEX_SENTINEL;
use bundle_host_kv::{
    CapabilitySnapshot, KvBackend, KvHost, KvScope, QuotaOutcome, ReconcileOutcome,
    MAX_KEYS_PER_APP, MAX_VALUE_BYTES,
};
use redis::aio::MultiplexedConnection;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage};
use tokio::task::JoinSet;

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

/// A [`CapabilitySnapshot`] granting `storage.kv` to every `app_id` listed
/// -- every test below exercises something other than the gate itself, so
/// they all grant up front (`crate::authorize`'s own unit tests, and
/// `crate::tests::kv_call_is_denied_when_the_app_never_declared_storage_kv`,
/// cover the denial path).
fn granting(app_ids: &[&str]) -> Arc<CapabilitySnapshot> {
    let snapshot = CapabilitySnapshot::new();
    for app_id in app_ids {
        snapshot.update(*app_id, ["storage.kv".to_string()]);
    }
    Arc::new(snapshot)
}

/// Initialises `scope`'s live index (sentinel included) and tops it up with
/// `fillers` permanent members -- the real-Valkey equivalent of the fake's
/// `seed_count`, without writing that many real keys first. Seeds through a
/// second connection, so the host under test never sees the seeding.
async fn seed_live_index(conn: &mut MultiplexedConnection, scope: &KvScope, fillers: u64) {
    let mut cmd = redis::cmd("ZADD");
    cmd.arg(scope.live_key())
        .arg("+inf")
        .arg(LIVE_INDEX_SENTINEL);
    for i in 0..fillers {
        cmd.arg("+inf").arg(format!("filler.{i}"));
    }
    cmd.query_async::<()>(conn).await.unwrap();
}

/// Valkey's own clock in epoch milliseconds -- the clock that expires keys
/// and scores the live index, so it is the right "now" for assertions.
async fn valkey_now_ms(conn: &mut MultiplexedConnection) -> u64 {
    let (secs, micros): (u64, u64) = redis::cmd("TIME").query_async(conn).await.unwrap();
    secs * 1_000 + micros / 1_000
}

/// How many keys the quota currently sees as live: index members not yet
/// lapsed at Valkey's clock, sentinel excluded. Evaluated independently of
/// any write, so it also proves lapsed keys stop counting *without* a
/// trimming write having run.
async fn live_keys_now(conn: &mut MultiplexedConnection, scope: &KvScope) -> i64 {
    let now = valkey_now_ms(conn).await;
    let counted: i64 = redis::cmd("ZCOUNT")
        .arg(scope.live_key())
        .arg(now)
        .arg("+inf")
        .query_async(conn)
        .await
        .unwrap();
    counted - 1 // the sentinel's `+inf` score always falls in the range
}

/// A member's raw index score: `None` = not in the index, `Some(inf)` = no
/// TTL, otherwise the absolute expiry in epoch milliseconds.
async fn index_score(
    conn: &mut MultiplexedConnection,
    scope: &KvScope,
    guest_key: &str,
) -> Option<f64> {
    let score: Option<String> = redis::cmd("ZSCORE")
        .arg(scope.live_key())
        .arg(scope.data_key(guest_key))
        .query_async(conn)
        .await
        .unwrap();
    score.map(|s| s.parse::<f64>().unwrap())
}

type SharedHost = Arc<KvHost<MultiplexedConnection>>;

/// Writes `count` distinct keys (`{prefix}{i}`) from concurrent tasks and
/// returns `(admitted, quota_rejected)`; any other error fails the test.
/// Every write gets its own `call_id` (starting at `first_call_id`) so the
/// per-invocation op rate limit never interferes with the quota under test.
async fn set_concurrently(
    host: &SharedHost,
    scope: &KvScope,
    prefix: &str,
    count: u64,
    ttl_seconds: u32,
    first_call_id: u64,
) -> (u64, u64) {
    let mut admitted = 0;
    let mut rejected = 0;
    let mut tasks = JoinSet::new();
    let mut tally = |result: Result<Result<(), bundle_host_kv::KvError>, _>| match result
        .expect("task joined")
    {
        Ok(()) => admitted += 1,
        Err(e) => {
            assert_eq!(e.code(), "quota_exceeded", "unexpected error: {e}");
            rejected += 1;
        }
    };
    for i in 0..count {
        let (host, scope, key) = (Arc::clone(host), scope.clone(), format!("{prefix}{i}"));
        tasks.spawn(async move {
            host.set(&scope, first_call_id + i, &key, b"v", ttl_seconds)
                .await
        });
        // Bound in-flight work so one test does not open 10k tasks at once.
        while tasks.len() >= 256 {
            tally(tasks.join_next().await.expect("a task is in flight"));
        }
    }
    while let Some(result) = tasks.join_next().await {
        tally(result);
    }
    (admitted, rejected)
}

#[tokio::test]
async fn set_then_get_round_trips_through_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
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
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
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
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    host.set(&scope, 1, "k", b"v", 0).await.unwrap();
    host.delete(&scope, 2, "k").await.unwrap();
    assert_eq!(host.get(&scope, 3, "k").await.unwrap(), None);
}

#[tokio::test]
async fn increment_is_atomic_and_persists_across_calls_in_real_valkey() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
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
    let host = KvHost::new(
        connect(&url).await,
        granting(&["waddles.bot.a", "waddles.bot.b"]),
    );
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
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
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
/// live index directly at its ceiling -- equivalent to
/// `backend::fake::FakeBackend::seed_count`, without a slow loop writing
/// `MAX_KEYS_PER_APP` real keys first.
#[tokio::test]
async fn key_count_quota_is_enforced_by_the_real_lua_script() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    let mut seed_conn = connect(&url).await;
    seed_live_index(&mut seed_conn, &scope, MAX_KEYS_PER_APP).await;

    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
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
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));

    host.set(&scope, 1, "existing", b"v1", 0).await.unwrap();

    // The existing key plus `MAX - 1` fillers puts the index exactly at its
    // ceiling.
    let mut seed_conn = connect(&url).await;
    seed_live_index(&mut seed_conn, &scope, MAX_KEYS_PER_APP - 1).await;

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
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let oversized = vec![0u8; MAX_VALUE_BYTES + 1];

    let err = host.set(&scope, 1, "k", &oversized, 0).await.unwrap_err();
    assert_eq!(err.wire_code(), "too_large");
}

/// `authorize()`'s "undeclared means denied" default, against a real
/// Valkey backend (not just the fake) -- an app with an empty
/// `CapabilitySnapshot` is refused before any Valkey round trip at all.
#[tokio::test]
async fn a_kv_call_is_denied_against_real_valkey_when_storage_kv_was_never_declared() {
    let (_container, url) = start().await;
    let host = KvHost::new(connect(&url).await, granting(&[]));
    let scope = scope("acme", Some("main"), "waddles.bot.a");

    let err = host.set(&scope, 1, "k", b"v", 0).await.unwrap_err();
    assert_eq!(err.code(), "not_granted");
}

/// A fresh Valkey container's default `maxmemory-policy` (`noeviction`) is
/// compliant -- proves `crate::policy::check_maxmemory_policy` runs a real
/// `CONFIG GET` round trip correctly, not just against a mock.
#[tokio::test]
async fn maxmemory_policy_check_reports_compliant_against_a_fresh_valkey_container() {
    let (_container, url) = start().await;
    let mut conn = connect(&url).await;
    let check = bundle_host_kv::policy::check_maxmemory_policy(&mut conn).await;
    assert!(
        matches!(check, bundle_host_kv::policy::PolicyCheck::Compliant(_)),
        "expected a fresh Valkey container's default policy to be compliant, got {check:?}"
    );
}

/// The self-heal reconciliation path (`crate::backend::KvBackend::
/// reconcile_count_if_missing`), against a real Valkey `EVAL`, not just
/// the fake: two live data keys, the live index deleted outright (the same
/// observable state an `allkeys-*` eviction leaves -- `crate::policy`'s
/// doc), a third write still succeeds and the index is rebuilt to the true
/// live set via the real Lua `SCAN` loop.
#[tokio::test]
async fn a_deleted_live_index_is_rebuilt_via_scan_against_real_valkey() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));

    host.set(&scope, 1, "existing-1", b"v1", 0).await.unwrap();
    host.set(&scope, 2, "existing-2", b"v2", 0).await.unwrap();

    // Simulate the exact observable effect of an `allkeys-*` eviction of
    // the live index alone -- the data keys above survive untouched.
    let mut admin_conn = connect(&url).await;
    redis::AsyncCommands::del::<_, ()>(&mut admin_conn, scope.live_key())
        .await
        .unwrap();

    host.set(&scope, 3, "existing-3", b"v3", 0).await.unwrap();
    assert_eq!(
        host.get(&scope, 4, "existing-3").await.unwrap(),
        Some(b"v3".to_vec()),
        "the write must still succeed once the index self-heals"
    );

    // The rebuilt index must reflect the true live set (2 pre-existing
    // keys, plus the 1 just admitted = 3), not 0 or 1.
    assert_eq!(live_keys_now(&mut admin_conn, &scope).await, 3);
    for key in ["existing-1", "existing-2", "existing-3"] {
        assert_eq!(
            index_score(&mut admin_conn, &scope, key).await,
            Some(f64::INFINITY),
            "{key} was written without a TTL"
        );
    }
}

/// Regression for the quota-leak bug: TTL keys never freed their slot, so a
/// bundle using cooldown keys died at the ceiling permanently. Fills the
/// app to exactly `MAX_KEYS_PER_APP` live TTL keys through the real host,
/// proves the ceiling holds while they are live, then proves that once they
/// lapse -- with nothing ever calling `delete` -- the app can write again.
/// Against the old `INCR`/`DECR` counter the final `set` was rejected.
#[tokio::test]
async fn ttl_keys_free_their_quota_slot_once_they_lapse_in_real_valkey() {
    // Generous relative to how long 10k local writes take, so every key is
    // still live when the ceiling check runs even on a slow CI runner.
    const TTL_SECONDS: u32 = 15;

    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host: SharedHost = Arc::new(KvHost::new(
        connect(&url).await,
        granting(&["waddles.bot.a"]),
    ));
    let mut probe = connect(&url).await;

    let (admitted, rejected) =
        set_concurrently(&host, &scope, "cooldown.", MAX_KEYS_PER_APP, TTL_SECONDS, 0).await;
    assert_eq!((admitted, rejected), (MAX_KEYS_PER_APP, 0));
    assert_eq!(
        live_keys_now(&mut probe, &scope).await,
        MAX_KEYS_PER_APP as i64
    );

    // While every key is live the ceiling holds.
    let err = host
        .set(&scope, 1_000_000, "over-the-ceiling", b"x", 0)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "quota_exceeded");

    tokio::time::sleep(Duration::from_secs(u64::from(TTL_SECONDS) + 1)).await;

    // Every key lapsed on Valkey's own clock; the quota sees zero live keys
    // before any write has trimmed anything...
    assert_eq!(live_keys_now(&mut probe, &scope).await, 0);
    // ...so a write is admitted, and a whole new generation fits.
    host.set(&scope, 1_000_001, "after-expiry", b"x", 0)
        .await
        .unwrap();
    assert_eq!(
        host.get(&scope, 1_000_002, "cooldown.0").await.unwrap(),
        None
    );
    assert_eq!(live_keys_now(&mut probe, &scope).await, 1);
    let (admitted, rejected) = set_concurrently(
        &host,
        &scope,
        "gen2.",
        MAX_KEYS_PER_APP - 1,
        TTL_SECONDS,
        2_000_000,
    )
    .await;
    assert_eq!((admitted, rejected), (MAX_KEYS_PER_APP - 1, 0));
}

/// Concurrency: many writers racing for the last few slots must be admitted
/// exactly up to the ceiling, never past it -- with TTL and permanent keys
/// mixed -- and freed slots (explicit `delete`) are handed out exactly once.
#[tokio::test]
async fn concurrent_new_keys_never_exceed_the_live_quota_in_real_valkey() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host: SharedHost = Arc::new(KvHost::new(
        connect(&url).await,
        granting(&["waddles.bot.a"]),
    ));
    let mut probe = connect(&url).await;
    let headroom = 40;
    seed_live_index(&mut probe, &scope, MAX_KEYS_PER_APP - headroom).await;

    // Two racing batches over the same headroom, one permanent, one TTL.
    let (permanent, ttl) = tokio::join!(
        set_concurrently(&host, &scope, "perm.", 150, 0, 0),
        set_concurrently(&host, &scope, "ttl.", 150, 300, 1_000),
    );
    assert_eq!(
        permanent.0 + ttl.0,
        headroom,
        "admitted exactly the headroom"
    );
    assert_eq!(permanent.1 + ttl.1, 300 - headroom);
    assert_eq!(
        live_keys_now(&mut probe, &scope).await,
        MAX_KEYS_PER_APP as i64
    );

    // Free exactly 5 slots; of 20 racers exactly 5 get them.
    let mut freed = 0;
    for i in 0..150 {
        if freed == 5 {
            break;
        }
        let key = format!("perm.{i}");
        if index_score(&mut probe, &scope, &key).await.is_some() {
            host.delete(&scope, 5_000 + i, &key).await.unwrap();
            freed += 1;
        }
    }
    assert_eq!(freed, 5);
    let (admitted, rejected) = set_concurrently(&host, &scope, "late.", 20, 0, 10_000).await;
    assert_eq!((admitted, rejected), (5, 15));
}

/// The live index stays exact across set / expire / overwrite / delete /
/// increment against real Valkey -- scores follow TTL changes in both
/// directions, a delete of something already gone changes nothing, and a
/// failed `INCRBY` leaves no phantom member.
#[tokio::test]
async fn live_index_stays_exact_across_set_expire_overwrite_delete_in_real_valkey() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
    let mut probe = connect(&url).await;

    host.set(&scope, 1, "short", b"v", 2).await.unwrap();
    host.set(&scope, 2, "perm", b"v", 0).await.unwrap();
    host.set(&scope, 3, "long", b"v", 100).await.unwrap();
    assert_eq!(live_keys_now(&mut probe, &scope).await, 3);
    assert_eq!(
        index_score(&mut probe, &scope, "perm").await,
        Some(f64::INFINITY)
    );
    let long_score = index_score(&mut probe, &scope, "long").await.unwrap();
    let now = valkey_now_ms(&mut probe).await as f64;
    assert!(
        long_score > now + 90_000.0 && long_score <= now + 100_000.0,
        "a 100s TTL scores ~now+100s, got {long_score} vs now {now}"
    );

    tokio::time::sleep(Duration::from_millis(2_500)).await;
    assert_eq!(
        live_keys_now(&mut probe, &scope).await,
        2,
        "the 2s key stopped counting on its own, with no write to trim it"
    );

    // Re-creating the lapsed key counts exactly once and trims its corpse.
    host.set(&scope, 4, "short", b"v2", 100).await.unwrap();
    assert_eq!(live_keys_now(&mut probe, &scope).await, 3);

    // Delete, double-delete, and delete-of-nothing never underflow.
    host.delete(&scope, 5, "perm").await.unwrap();
    host.delete(&scope, 6, "perm").await.unwrap();
    host.delete(&scope, 7, "never-existed").await.unwrap();
    assert_eq!(live_keys_now(&mut probe, &scope).await, 2);
    assert_eq!(index_score(&mut probe, &scope, "perm").await, None);

    // Overwrite with ttl=0 clears the expiry in the index too.
    host.set(&scope, 8, "long", b"v3", 0).await.unwrap();
    assert_eq!(
        index_score(&mut probe, &scope, "long").await,
        Some(f64::INFINITY)
    );

    // increment: ttl>0 scores finitely, a later ttl=0 PERSISTs the key AND
    // moves its score to +inf.
    host.increment(&scope, 9, "hits", 1, 100).await.unwrap();
    assert!(index_score(&mut probe, &scope, "hits")
        .await
        .unwrap()
        .is_finite());
    host.increment(&scope, 10, "hits", 1, 0).await.unwrap();
    assert_eq!(
        index_score(&mut probe, &scope, "hits").await,
        Some(f64::INFINITY)
    );
    let pttl: i64 = redis::cmd("PTTL")
        .arg(scope.data_key("hits"))
        .query_async(&mut probe)
        .await
        .unwrap();
    assert_eq!(pttl, -1, "PERSIST cleared the TTL");

    // A failing INCRBY (non-integer value) must not leave a phantom member.
    redis::AsyncCommands::set::<_, _, ()>(&mut probe, scope.data_key("planted-text"), "abc")
        .await
        .unwrap();
    let err = host
        .increment(&scope, 11, "planted-text", 1, 30)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "backend");
    assert_eq!(index_score(&mut probe, &scope, "planted-text").await, None);
}

/// Deleting the last key (or every key lapsing) leaves the index in place
/// via its sentinel, so the next write is not mistaken for "index evicted"
/// and does not run a keyspace `SCAN`. Proven with a key planted behind the
/// index's back: a spurious reconciliation would index it.
#[tokio::test]
async fn an_emptied_index_is_not_rebuilt_by_a_keyspace_scan_in_real_valkey() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
    let mut probe = connect(&url).await;

    host.set(&scope, 1, "cooldown", b"v", 100).await.unwrap(); // builds the index
    redis::AsyncCommands::set::<_, _, ()>(&mut probe, scope.data_key("planted"), "v")
        .await
        .unwrap();

    host.delete(&scope, 2, "cooldown").await.unwrap(); // the index drains
    let exists: i64 = redis::cmd("EXISTS")
        .arg(scope.live_key())
        .query_async(&mut probe)
        .await
        .unwrap();
    assert_eq!(exists, 1, "the sentinel keeps the drained index alive");

    host.set(&scope, 3, "next", b"v", 100).await.unwrap();
    assert_eq!(
        index_score(&mut probe, &scope, "planted").await,
        None,
        "no SCAN reconciliation ran, so the planted key was never indexed"
    );
    assert_eq!(live_keys_now(&mut probe, &scope).await, 1);
}

/// Upgrade path. A deployment coming from the old `INCR`/`DECR` counter has
/// a counter pinned at the ceiling by phantom (lapsed) TTL keys, a few real
/// data keys, and no live index. The first write must succeed (the new code
/// enforces against the index it rebuilds, so the phantoms cannot block it),
/// rebuild the index from the real keys with their remaining TTLs, and leave
/// the legacy counter in place -- pods still running the old code are
/// enforcing against it (deleting it was a mixed-version fail-open) -- while
/// writing the new key through so that counter stays an upper bound.
#[tokio::test]
async fn first_write_after_upgrade_is_not_blocked_by_phantoms_and_keeps_the_legacy_counter() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
    let mut probe = connect(&url).await;

    redis::AsyncCommands::set::<_, _, ()>(&mut probe, scope.legacy_count_key(), MAX_KEYS_PER_APP)
        .await
        .unwrap();
    redis::AsyncCommands::set::<_, _, ()>(&mut probe, scope.data_key("perm"), "v")
        .await
        .unwrap();
    redis::cmd("SET")
        .arg(scope.data_key("ttl"))
        .arg("v")
        .arg("PX")
        .arg(60_000)
        .query_async::<()>(&mut probe)
        .await
        .unwrap();
    let before = valkey_now_ms(&mut probe).await as f64;

    host.set(&scope, 1, "first-after-upgrade", b"x", 0)
        .await
        .expect("phantom counts must not block the first write after upgrade");

    assert_eq!(
        legacy_counter(&mut probe, &scope).await,
        Some(MAX_KEYS_PER_APP as i64 + 1),
        "the legacy counter survives the upgrade and includes the new key"
    );
    assert_eq!(live_keys_now(&mut probe, &scope).await, 3);
    assert_eq!(
        index_score(&mut probe, &scope, "perm").await,
        Some(f64::INFINITY)
    );
    let ttl_score = index_score(&mut probe, &scope, "ttl").await.unwrap();
    assert!(
        ttl_score > before && ttl_score <= before + 61_000.0,
        "the rebuilt member keeps its remaining ~60s TTL, got {ttl_score}"
    );
}

// ---------------------------------------------------------------------------
// Mixed-version rollout: pre-index ("legacy") pods and this code, one Valkey.
// ---------------------------------------------------------------------------

/// The legacy counter's raw value, `None` if the key does not exist.
async fn legacy_counter(conn: &mut MultiplexedConnection, scope: &KvScope) -> Option<i64> {
    let raw: Option<String> = redis::cmd("GET")
        .arg(scope.legacy_count_key())
        .query_async(conn)
        .await
        .unwrap();
    raw.map(|v| v.parse().expect("legacy counter is an integer"))
}

/// Plants a raw value at the legacy counter key (what a pre-index pod, or
/// debris from one, leaves behind).
async fn plant_counter(conn: &mut MultiplexedConnection, scope: &KvScope, value: &str) {
    redis::AsyncCommands::set::<_, _, ()>(conn, scope.legacy_count_key(), value)
        .await
        .unwrap();
}

/// Ground truth for the quota: data keys that actually exist right now,
/// independent of the live index and of the legacy counter.
async fn true_live_keys(conn: &mut MultiplexedConnection, scope: &KvScope) -> i64 {
    let keys: Vec<String> = redis::cmd("KEYS")
        .arg(scope.data_scan_pattern())
        .query_async(conn)
        .await
        .unwrap();
    let mut live = 0;
    for key in keys {
        let pttl: i64 = redis::cmd("PTTL")
            .arg(&key)
            .query_async(conn)
            .await
            .unwrap();
        if pttl != -2 {
            live += 1;
        }
    }
    live
}

/// Asserts the coexistence invariant: whenever the legacy counter exists it
/// is never below the true live-key count (it may only over-count lapsed TTL
/// keys), so a pre-index pod can never be more permissive than the truth.
async fn assert_legacy_counter_never_undercounts(
    conn: &mut MultiplexedConnection,
    scope: &KvScope,
    ctx: &str,
) {
    if let Some(counter) = legacy_counter(conn, scope).await {
        let truth = true_live_keys(conn, scope).await;
        assert!(
            counter >= truth,
            "{ctx}: legacy counter {counter} fell below the true live-key count {truth}"
        );
    }
}

/// Ceiling both versions enforce in the mixed-version tests -- small enough
/// to reach, passed explicitly (both versions take `max_keys` per call).
const MIXED_MAX: u64 = 30;

/// A pod running the PRE-INDEX code, reproduced from its exact Lua (commit
/// `881ac8dd`, `core/bundle_host_kv/src/backend.rs` before the live-index
/// fix) so the mixed-version tests exercise the real legacy scripts, not a
/// paraphrase. The legacy host made two round trips per write -- reconcile,
/// then the quota script -- which [`LegacyPod::reconcile`] and
/// [`LegacyPod::set_script`] keep separate so tests can interleave a new pod
/// between them (the reported race).
mod legacy_pod {
    use super::*;

    const SET_WITH_QUOTA: &str = r#"
local existed = redis.call('EXISTS', KEYS[1])
if existed == 0 then
  local count = tonumber(redis.call('GET', KEYS[2]) or '0')
  if count >= tonumber(ARGV[3]) then
    return 1
  end
  redis.call('INCR', KEYS[2])
end
if tonumber(ARGV[2]) > 0 then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
else
  redis.call('SET', KEYS[1], ARGV[1])
end
return 0
"#;

    const DELETE: &str = r#"
local deleted = redis.call('DEL', KEYS[1])
if deleted == 1 and redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('DECR', KEYS[2])
end
return deleted
"#;

    const RECONCILE: &str = r#"
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {0, 0}
end
local lock_ok = redis.call('SET', KEYS[2], '1', 'NX', 'PX', ARGV[2])
if not lock_ok then
  return {2, 0}
end
local cursor = '0'
local count = 0
local limit = tonumber(ARGV[3])
repeat
  local res = redis.call('SCAN', cursor, 'MATCH', ARGV[1], 'COUNT', 1000)
  cursor = res[1]
  count = count + #res[2]
  if count >= limit then
    count = limit
    break
  end
until cursor == '0'
redis.call('SET', KEYS[1], count)
redis.call('DEL', KEYS[2])
return {1, count}
"#;

    #[derive(Clone)]
    pub struct LegacyPod {
        conn: MultiplexedConnection,
    }

    impl LegacyPod {
        pub fn new(conn: MultiplexedConnection) -> Self {
            Self { conn }
        }

        /// The legacy `ensure_count_reconciled` round trip: rebuilds a missing
        /// counter from a `SCAN`, a no-op while it exists.
        pub async fn reconcile(&self, scope: &KvScope) {
            let mut conn = self.conn.clone();
            let _: (i64, u64) = redis::Script::new(RECONCILE)
                .key(scope.legacy_count_key())
                .key(scope.reconcile_lock_key())
                .arg(scope.data_scan_pattern())
                .arg(5_000u64)
                .arg(MIXED_MAX * 2)
                .invoke_async(&mut conn)
                .await
                .unwrap();
        }

        /// The legacy quota script alone; `true` if the write was admitted.
        pub async fn set_script(&self, scope: &KvScope, key: &str, ttl: u32) -> bool {
            let mut conn = self.conn.clone();
            let rejected: i64 = redis::Script::new(SET_WITH_QUOTA)
                .key(scope.data_key(key))
                .key(scope.legacy_count_key())
                .arg(b"v".as_slice())
                .arg(ttl)
                .arg(MIXED_MAX)
                .invoke_async(&mut conn)
                .await
                .unwrap();
            rejected == 0
        }

        /// A full legacy write: reconcile, then the quota script.
        pub async fn set(&self, scope: &KvScope, key: &str, ttl: u32) -> bool {
            self.reconcile(scope).await;
            self.set_script(scope, key, ttl).await
        }

        pub async fn delete(&self, scope: &KvScope, key: &str) {
            let mut conn = self.conn.clone();
            let _: i64 = redis::Script::new(DELETE)
                .key(scope.data_key(key))
                .key(scope.legacy_count_key())
                .invoke_async(&mut conn)
                .await
                .unwrap();
        }
    }
}
use legacy_pod::LegacyPod;

/// A write from a pod running THIS code, through the real `KvBackend`
/// scripts at the mixed-version ceiling (reconcile-if-missing, then the quota
/// script -- the two calls `KvHost::set` makes). `true` if admitted.
async fn new_pod_set(conn: &MultiplexedConnection, scope: &KvScope, key: &str, ttl: u32) -> bool {
    match conn
        .reconcile_count_if_missing(
            &scope.live_key(),
            &scope.data_scan_pattern(),
            &scope.reconcile_lock_key(),
            5_000,
            MIXED_MAX * 2,
        )
        .await
        .unwrap()
    {
        ReconcileOutcome::AlreadyPresent | ReconcileOutcome::Reconciled(_) => {}
        ReconcileOutcome::Locked => return false, // fail closed, like `KvHost`
    }
    matches!(
        conn.set_with_quota(
            &scope.data_key(key),
            &scope.live_key(),
            &scope.legacy_count_key(),
            b"v",
            ttl,
            MIXED_MAX,
        )
        .await
        .unwrap(),
        QuotaOutcome::Admitted(())
    )
}

async fn new_pod_delete(conn: &MultiplexedConnection, scope: &KvScope, key: &str) {
    conn.delete(
        &scope.data_key(key),
        &scope.live_key(),
        &scope.legacy_count_key(),
    )
    .await
    .unwrap();
}

/// The reported bypass, replayed against the real legacy and new Lua: a
/// legacy pod passes its reconcile check (counter exists), a new pod then
/// does its first write/reconcile and fills the app, and the legacy pod's
/// quota script runs. When the new code deleted the counter in that gap the
/// legacy script read `0`, recreated it from there, and admitted writes past
/// the ceiling for the rest of the rollout.
#[tokio::test]
async fn mixed_rollout_a_legacy_pod_cannot_recreate_the_counter_from_zero_mid_request() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let legacy = LegacyPod::new(connect(&url).await);
    let new_pod = connect(&url).await;
    let mut probe = connect(&url).await;

    for i in 0..4 {
        assert!(legacy.set(&scope, &format!("pre.{i}"), 0).await);
    }
    assert_eq!(legacy_counter(&mut probe, &scope).await, Some(4));

    legacy.reconcile(&scope).await; // legacy pod: counter present, proceeds
    for i in 0..(MIXED_MAX - 4) {
        // the new pod's first write builds `:live` (reconcile) and fills the
        // app to the ceiling inside the gap
        assert!(new_pod_set(&new_pod, &scope, &format!("new.{i}"), 0).await);
    }
    assert_eq!(true_live_keys(&mut probe, &scope).await, MIXED_MAX as i64);
    assert!(
        legacy_counter(&mut probe, &scope).await.is_some(),
        "the new pod must never delete the legacy counter"
    );

    assert!(
        !legacy.set_script(&scope, "one-too-many", 0).await,
        "the legacy pod must see the new pod's keys and reject"
    );
    assert_eq!(true_live_keys(&mut probe, &scope).await, MIXED_MAX as i64);
    assert_legacy_counter_never_undercounts(&mut probe, &scope, "after the gap write").await;
}

/// Both directions of visibility the legacy counter needs: keys a new pod
/// creates are counted by it (a legacy pod rejects at the ceiling), and keys
/// a new pod deletes free legacy slots exactly once (double-delete and
/// delete-of-nothing never double-`DECR`).
#[tokio::test]
async fn mixed_rollout_legacy_pod_tracks_new_pod_creates_and_deletes() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let legacy = LegacyPod::new(connect(&url).await);
    let new_pod = connect(&url).await;
    let mut probe = connect(&url).await;

    assert!(legacy.set(&scope, "pre", 0).await);
    for i in 0..(MIXED_MAX - 1) {
        assert!(new_pod_set(&new_pod, &scope, &format!("new.{i}"), 0).await);
    }
    assert!(
        !legacy.set(&scope, "blocked", 0).await,
        "app is full: the legacy pod rejects"
    );

    new_pod_delete(&new_pod, &scope, "new.0").await;
    new_pod_delete(&new_pod, &scope, "new.0").await;
    new_pod_delete(&new_pod, &scope, "never-existed").await;
    assert!(
        legacy.set(&scope, "fits-now", 0).await,
        "the freed slot is usable"
    );
    assert!(!legacy.set(&scope, "blocked-again", 0).await);
    assert_legacy_counter_never_undercounts(&mut probe, &scope, "after deletes").await;
}

/// A new pod never fabricates the counter (a guessed value is exactly the
/// under-count to avoid); a legacy pod that arrives later rebuilds it from a
/// keyspace `SCAN`, which already includes the new pod's keys.
#[tokio::test]
async fn mixed_rollout_an_absent_legacy_counter_is_never_created_by_a_new_pod() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let legacy = LegacyPod::new(connect(&url).await);
    let new_pod = connect(&url).await;
    let mut probe = connect(&url).await;

    for i in 0..3 {
        assert!(new_pod_set(&new_pod, &scope, &format!("k.{i}"), 0).await);
    }
    new_pod_delete(&new_pod, &scope, "k.0").await;
    assert_eq!(legacy_counter(&mut probe, &scope).await, None);

    assert!(legacy.set(&scope, "legacy.0", 0).await);
    assert_eq!(legacy_counter(&mut probe, &scope).await, Some(3));
}

/// Concurrent legacy and new writers/deleters (permanent and TTL keys,
/// overlapping key pool, repeated rounds with TTL lapse between them) on one
/// app: after every round the legacy counter is still at or above the true
/// live-key count -- over-count only, never a bypass -- and it never
/// disappears mid-rollout.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn mixed_rollout_concurrent_legacy_and_new_pods_never_undercount() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let legacy = LegacyPod::new(connect(&url).await);
    let new_pod = connect(&url).await;
    let mut probe = connect(&url).await;

    // Pre-rollout state: a legacy deployment with a populated counter.
    for i in 0..6 {
        let ttl = if i % 2 == 0 { 0 } else { 2 };
        assert!(legacy.set(&scope, &format!("pre.{i}"), ttl).await);
    }
    assert!(legacy_counter(&mut probe, &scope).await.is_some());

    for round in 0..8u32 {
        let mut tasks = JoinSet::new();
        for i in 0..24u32 {
            let key = format!("k.{}", (i * 7 + round * 13) % 40);
            let ttl = if (i + round) % 3 == 0 { 2 } else { 0 };
            let (legacy, new_pod, scope) = (legacy.clone(), new_pod.clone(), scope.clone());
            match i % 4 {
                0 | 1 => {
                    tasks.spawn(async move {
                        legacy.set(&scope, &key, ttl).await;
                    });
                }
                2 => {
                    tasks.spawn(async move {
                        new_pod_set(&new_pod, &scope, &key, ttl).await;
                    });
                }
                _ => {
                    tasks.spawn(async move {
                        if (i + round) % 2 == 0 {
                            new_pod_delete(&new_pod, &scope, &key).await;
                        } else {
                            legacy.delete(&scope, &key).await;
                        }
                    });
                }
            }
        }
        while let Some(joined) = tasks.join_next().await {
            joined.expect("worker task joined");
        }
        assert!(
            legacy_counter(&mut probe, &scope).await.is_some(),
            "round {round}: the legacy counter disappeared mid-rollout"
        );
        assert_legacy_counter_never_undercounts(&mut probe, &scope, &format!("round {round}"))
            .await;
        if round % 2 == 1 {
            // Let the TTL keys lapse: the counter keeps counting them
            // (stricter), the truth drops -- the safe direction.
            tokio::time::sleep(Duration::from_millis(2_200)).await;
            assert_legacy_counter_never_undercounts(
                &mut probe,
                &scope,
                &format!("round {round} after lapse"),
            )
            .await;
        }
    }
}

/// The write-through is exact for every transition, never creates or breaks
/// anything, and can never fail a write: overwrites do not `INCR`, a delete
/// of nothing does not `DECR`, a failing `INCRBY` leaves the counter alone,
/// a garbage/foreign-typed counter is ignored (the write still succeeds), and
/// a counter at zero is never taken negative.
#[tokio::test]
async fn legacy_counter_write_through_is_exact_and_can_never_fail_a_write() {
    let (_container, url) = start().await;
    let scope = scope("acme", Some("main"), "waddles.bot.a");
    let host = KvHost::new(connect(&url).await, granting(&["waddles.bot.a"]));
    let mut probe = connect(&url).await;

    plant_counter(&mut probe, &scope, "5").await;
    host.set(&scope, 1, "a", b"v", 0).await.unwrap();
    assert_eq!(legacy_counter(&mut probe, &scope).await, Some(6), "new key");
    host.set(&scope, 2, "a", b"v2", 0).await.unwrap();
    assert_eq!(
        legacy_counter(&mut probe, &scope).await,
        Some(6),
        "overwrite"
    );
    host.increment(&scope, 3, "hits", 1, 0).await.unwrap();
    assert_eq!(
        legacy_counter(&mut probe, &scope).await,
        Some(7),
        "new counter key"
    );
    host.increment(&scope, 4, "hits", 1, 0).await.unwrap();
    assert_eq!(
        legacy_counter(&mut probe, &scope).await,
        Some(7),
        "existing counter key"
    );
    host.delete(&scope, 5, "a").await.unwrap();
    assert_eq!(legacy_counter(&mut probe, &scope).await, Some(6), "delete");
    host.delete(&scope, 6, "a").await.unwrap();
    host.delete(&scope, 7, "never-existed").await.unwrap();
    assert_eq!(
        legacy_counter(&mut probe, &scope).await,
        Some(6),
        "no double DECR"
    );

    // A failing INCRBY (non-integer value) leaves the legacy counter alone.
    redis::AsyncCommands::set::<_, _, ()>(&mut probe, scope.data_key("text"), "abc")
        .await
        .unwrap();
    assert_eq!(
        host.increment(&scope, 8, "text", 1, 30)
            .await
            .unwrap_err()
            .code(),
        "backend"
    );
    assert_eq!(legacy_counter(&mut probe, &scope).await, Some(6));

    // Never taken negative: a zero counter with a live key stays at zero.
    plant_counter(&mut probe, &scope, "0").await;
    host.delete(&scope, 9, "hits").await.unwrap();
    assert_eq!(legacy_counter(&mut probe, &scope).await, Some(0));

    // Debris can never fail a write: a non-numeric counter, and one of the
    // wrong type entirely, are ignored by the write-through.
    plant_counter(&mut probe, &scope, "not-a-number").await;
    host.set(&scope, 10, "b", b"v", 0).await.unwrap();
    host.delete(&scope, 11, "b").await.unwrap();
    redis::cmd("DEL")
        .arg(scope.legacy_count_key())
        .query_async::<()>(&mut probe)
        .await
        .unwrap();
    redis::cmd("RPUSH")
        .arg(scope.legacy_count_key())
        .arg("x")
        .query_async::<()>(&mut probe)
        .await
        .unwrap();
    host.set(&scope, 12, "c", b"v", 0).await.unwrap();
    host.increment(&scope, 13, "d", 1, 0).await.unwrap();
    host.delete(&scope, 14, "c").await.unwrap();

    // An absent counter is never created.
    redis::cmd("DEL")
        .arg(scope.legacy_count_key())
        .query_async::<()>(&mut probe)
        .await
        .unwrap();
    host.set(&scope, 15, "e", b"v", 0).await.unwrap();
    host.delete(&scope, 16, "e").await.unwrap();
    assert_eq!(legacy_counter(&mut probe, &scope).await, None);
}

//! End-to-end proof of per-tenant at-rest sealing of `kv` values against a
//! REAL Valkey server (no fake backend): a sealed write is not plaintext in
//! Valkey, the round trip works, a ciphertext copied across tenant / key
//! fails to open, a missing key refuses (and writes nothing), rotation reads
//! old and new versions, legacy plaintext follows the migration policy, and
//! the operator kill-switch writes plaintext on purpose.
//!
//! Requires Docker (`core/test_support`), like `valkey_integration.rs`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_host_crypto::{Dek, DekProvider, DekUnavailableError, TenantCrypto, MAGIC};
use bundle_host_kv::{
    BoxFuture, CapabilitySnapshot, KvError, KvHost, KvScope, Sealing, SealingGate,
};
use redis::aio::MultiplexedConnection;
use zeroize::Zeroizing;

#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::start_container;

const VALKEY_IMAGE: &str = "valkey/valkey:8.1.10-alpine";
const APP: &str = "app.sealed";

type DekTable = HashMap<String, Vec<(u32, [u8; 32])>>;

/// Per-tenant versioned DEKs; the highest version is active. `up` simulates
/// a broker outage.
#[derive(Clone)]
struct Keys {
    deks: Arc<Mutex<DekTable>>,
    up: Arc<AtomicBool>,
}

impl Keys {
    fn new() -> Self {
        Self {
            deks: Arc::new(Mutex::new(HashMap::new())),
            up: Arc::new(AtomicBool::new(true)),
        }
    }

    fn add(&self, tenant: &str, version: u32, seed: u8) {
        if let Ok(mut g) = self.deks.lock() {
            g.entry(tenant.to_string())
                .or_default()
                .push((version, [seed; 32]));
        }
    }

    fn unavailable(tenant: &str, v: Option<u32>) -> DekUnavailableError {
        DekUnavailableError {
            tenant_id: tenant.to_string(),
            dek_version: v,
            reason: "test: unavailable".to_string(),
        }
    }
}

impl DekProvider for Keys {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        if !self.up.load(Ordering::SeqCst) {
            return Err(Self::unavailable(tenant_id, None));
        }
        let g = self
            .deks
            .lock()
            .map_err(|_| Self::unavailable(tenant_id, None))?;
        g.get(tenant_id)
            .and_then(|v| v.iter().max_by_key(|(ver, _)| *ver))
            .map(|(ver, k)| (Zeroizing::new(*k), *ver))
            .ok_or_else(|| Self::unavailable(tenant_id, None))
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        if !self.up.load(Ordering::SeqCst) {
            return Err(Self::unavailable(tenant_id, Some(version)));
        }
        let g = self
            .deks
            .lock()
            .map_err(|_| Self::unavailable(tenant_id, Some(version)))?;
        g.get(tenant_id)
            .and_then(|v| v.iter().find(|(ver, _)| *ver == version))
            .map(|(_, k)| Zeroizing::new(*k))
            .ok_or_else(|| Self::unavailable(tenant_id, Some(version)))
    }
}

struct Gate(Arc<AtomicBool>);

impl SealingGate for Gate {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        let on = self.0.load(Ordering::SeqCst);
        Box::pin(async move { on })
    }
}

async fn connect(url: &str) -> MultiplexedConnection {
    redis::Client::open(url)
        .unwrap_or_else(|e| panic!("client: {e}"))
        .get_multiplexed_async_connection()
        .await
        .unwrap_or_else(|e| panic!("connect: {e}"))
}

fn scope(tenant_slug: &str, tenant_id: i32, community: &str, community_id: i32) -> KvScope {
    KvScope::new(tenant_slug, Some(community.to_string()), APP).with_ids(tenant_id, community_id)
}

fn host(
    conn: MultiplexedConnection,
    keys: &Keys,
    seal_on: &Arc<AtomicBool>,
) -> KvHost<MultiplexedConnection> {
    let caps = CapabilitySnapshot::new();
    caps.update(APP, ["storage.kv".to_string()]);
    let sealing = Sealing::new(
        TenantCrypto::new(keys.clone()),
        Arc::new(Gate(seal_on.clone())),
    );
    KvHost::new(conn, Arc::new(caps)).with_sealing(sealing)
}

async fn raw_get(conn: &mut MultiplexedConnection, key: &str) -> Option<Vec<u8>> {
    redis::cmd("GET")
        .arg(key)
        .query_async(conn)
        .await
        .unwrap_or_else(|e| panic!("raw GET: {e}"))
}

async fn raw_set(conn: &mut MultiplexedConnection, key: &str, value: &[u8]) {
    redis::cmd("SET")
        .arg(key)
        .arg(value)
        .query_async::<()>(conn)
        .await
        .unwrap_or_else(|e| panic!("raw SET: {e}"));
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

#[tokio::test]
async fn sealed_kv_end_to_end_against_real_valkey() {
    let container = start_container(
        VALKEY_IMAGE,
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let url = format!("redis://{}:{}/", container.host(), container.port());
    let mut raw = connect(&url).await;

    let keys = Keys::new();
    keys.add("1", 1, 0x11);
    keys.add("2", 1, 0x22);
    let seal_on = Arc::new(AtomicBool::new(true));
    let h = host(connect(&url).await, &keys, &seal_on);
    let acme = scope("acme", 1, "main", 10);
    let globex = scope("globex", 2, "main", 20);

    // 1. Sealed write is NOT plaintext in Valkey; the round trip works.
    h.set(&acme, 1, "doc", b"top-secret-token", 0)
        .await
        .unwrap_or_else(|e| panic!("set: {e}"));
    let stored = raw_get(&mut raw, &acme.data_key("doc"))
        .await
        .unwrap_or_default();
    assert!(
        stored.starts_with(&MAGIC),
        "stored value must be a sealed envelope"
    );
    assert!(
        !contains(&stored, b"top-secret-token"),
        "plaintext must not appear in Valkey"
    );
    let got = h
        .get(&acme, 2, "doc")
        .await
        .unwrap_or_else(|e| panic!("get: {e}"));
    assert_eq!(got.as_deref(), Some(&b"top-secret-token"[..]));

    // 2. Cross-tenant: copying the raw ciphertext into another tenant's
    // namespace (or another key of the same scope) fails to open.
    h.set(&globex, 3, "own", b"globex-value", 0)
        .await
        .unwrap_or_else(|e| panic!("set globex: {e}"));
    raw_set(&mut raw, &globex.data_key("doc"), &stored).await;
    let swapped = h.get(&globex, 4, "doc").await;
    assert!(
        swapped.is_err(),
        "cross-tenant ciphertext must not open: {swapped:?}"
    );
    raw_set(&mut raw, &acme.data_key("other"), &stored).await;
    let moved = h.get(&acme, 5, "other").await;
    assert!(
        matches!(moved, Err(KvError::Integrity(_))),
        "ciphertext moved to another key must not open: {moved:?}"
    );
    let own = h
        .get(&globex, 6, "own")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(own.as_deref(), Some(&b"globex-value"[..]));

    // 3. Missing key: write refused, nothing stored; sealed read refused.
    keys.up.store(false, Ordering::SeqCst);
    let refused = h.set(&acme, 7, "nokey", b"must-not-land", 0).await;
    assert!(
        matches!(refused, Err(KvError::KeyUnavailable(_))),
        "{refused:?}"
    );
    assert!(raw_get(&mut raw, &acme.data_key("nokey")).await.is_none());
    let read_refused = h.get(&acme, 8, "doc").await;
    assert!(
        matches!(read_refused, Err(KvError::KeyUnavailable(_))),
        "{read_refused:?}"
    );
    keys.up.store(true, Ordering::SeqCst);

    // 4. Rotation: a new version seals new writes; old ciphertext stays readable.
    keys.add("1", 2, 0x33);
    h.set(&acme, 9, "fresh", b"v2-value", 0)
        .await
        .unwrap_or_else(|e| panic!("set v2: {e}"));
    let fresh = raw_get(&mut raw, &acme.data_key("fresh"))
        .await
        .unwrap_or_default();
    assert_eq!(
        &fresh[5..9],
        &2u32.to_be_bytes(),
        "header carries dek_version 2"
    );
    assert_eq!(&stored[5..9], &1u32.to_be_bytes());
    let old = h
        .get(&acme, 10, "doc")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let new = h
        .get(&acme, 11, "fresh")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(old.as_deref(), Some(&b"top-secret-token"[..]));
    assert_eq!(new.as_deref(), Some(&b"v2-value"[..]));

    // 5. Legacy plaintext: readable in a never-sealed scope (migration
    // window); refused once the scope has a sealed write (marker persisted
    // in Valkey), including plaintext planted straight into Valkey.
    let legacy = scope("legacyco", 1, "fresh-community", 30);
    raw_set(&mut raw, &legacy.data_key("old"), b"legacy-plain").await;
    let legacy_read = h
        .get(&legacy, 12, "old")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(legacy_read.as_deref(), Some(&b"legacy-plain"[..]));
    h.set(&legacy, 13, "new", b"sealed-now", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert!(
        raw_get(&mut raw, &legacy.sealed_marker_key())
            .await
            .is_some(),
        "sealed-since marker must be persisted in Valkey"
    );
    raw_set(&mut raw, &legacy.data_key("planted"), b"attacker-plaintext").await;
    let planted = h.get(&legacy, 14, "planted").await;
    assert!(matches!(planted, Err(KvError::Integrity(_))), "{planted:?}");

    // 6. Counters are sealed too (CAS increment), readable via get.
    h.increment(&acme, 15, "hits", 7, 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let hits_raw = raw_get(&mut raw, &acme.data_key("hits"))
        .await
        .unwrap_or_default();
    assert!(
        hits_raw.starts_with(&MAGIC),
        "counter must be sealed at rest"
    );
    assert_ne!(hits_raw, b"7");
    let hits = h
        .get(&acme, 16, "hits")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(hits.as_deref(), Some(&b"7"[..]));

    // 7. Operator kill-switch ON: plaintext write on purpose; envelopes
    // still open.
    seal_on.store(false, Ordering::SeqCst);
    h.set(&acme, 17, "rollback", b"plain-by-operator", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let rolled = raw_get(&mut raw, &acme.data_key("rollback"))
        .await
        .unwrap_or_default();
    assert_eq!(rolled, b"plain-by-operator");
    let still = h
        .get(&acme, 18, "doc")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(still.as_deref(), Some(&b"top-secret-token"[..]));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_sealed_increments_against_real_valkey_lose_no_updates() {
    const TASKS: usize = 16;
    const PER_TASK: usize = 25;
    let container = start_container(
        VALKEY_IMAGE,
        6379,
        &[],
        &["valkey-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let url = format!("redis://{}:{}/", container.host(), container.port());
    let mut raw = connect(&url).await;
    let keys = Keys::new();
    keys.add("1", 1, 0x11);
    let seal_on = Arc::new(AtomicBool::new(true));
    // Two hosts over separate connections: two "pods" contending on one key.
    let hosts = [
        Arc::new(host(connect(&url).await, &keys, &seal_on)),
        Arc::new(host(connect(&url).await, &keys, &seal_on)),
    ];
    let s = scope("acme", 1, "main", 10);

    let mut joins = Vec::new();
    for t in 0..TASKS {
        let h = hosts[t % 2].clone();
        let s = s.clone();
        joins.push(tokio::spawn(async move {
            for i in 0..PER_TASK {
                let call_id = (t * PER_TASK + i) as u64;
                h.increment(&s, call_id, "shared", 1, 0)
                    .await
                    .unwrap_or_else(|e| panic!("increment: {e}"));
            }
        }));
    }
    for j in joins {
        j.await.unwrap_or_else(|e| panic!("join: {e}"));
    }

    let total = (TASKS * PER_TASK).to_string();
    let got = hosts[0]
        .get(&s, 1_000_000, "shared")
        .await
        .unwrap_or_else(|e| panic!("get: {e}"));
    assert_eq!(got.as_deref(), Some(total.as_bytes()), "no lost updates");
    let stored = raw_get(&mut raw, &s.data_key("shared"))
        .await
        .unwrap_or_default();
    assert!(stored.starts_with(&MAGIC), "counter sealed at rest");
    assert!(!contains(&stored, total.as_bytes()));
}

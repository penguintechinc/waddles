//! Unit tests for `kv` value sealing against the in-memory fake backend.
//! The real-Valkey proof (raw `GET` shows ciphertext) is
//! `tests/sealed_valkey.rs`.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use bundle_host_crypto::{Dek, DekProvider, DekUnavailableError, TenantCrypto};
use zeroize::Zeroizing;

use crate::backend::fake::FakeBackend;
use crate::{BoxFuture, CapabilitySnapshot, KvError, KvHost, KvScope, Sealing, SealingGate};

/// Test provider: a fixed DEK per tenant id, switchable to "broker down".
pub(crate) struct Provider {
    pub(crate) up: Arc<AtomicBool>,
}

impl DekProvider for Provider {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        self.get_dek_version(tenant_id, 1).await.map(|dek| (dek, 1))
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        if !self.up.load(Ordering::SeqCst) || version != 1 {
            return Err(DekUnavailableError {
                tenant_id: tenant_id.to_string(),
                dek_version: Some(version),
                reason: "test: unavailable".to_string(),
            });
        }
        let byte = tenant_id.bytes().fold(7u8, |a, b| a.wrapping_add(b));
        Ok(Zeroizing::new([byte; 32]))
    }
}

pub(crate) struct Gate(pub(crate) Arc<AtomicBool>);

impl SealingGate for Gate {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        let on = self.0.load(Ordering::SeqCst);
        Box::pin(async move { on })
    }
}

struct Rig {
    host: KvHost<FakeBackend>,
    broker_up: Arc<AtomicBool>,
    seal_on: Arc<AtomicBool>,
}

fn rig(backend: FakeBackend) -> Rig {
    let caps = CapabilitySnapshot::new();
    caps.update("app.a", [crate::authorize::KV_PERMISSION_ID.to_string()]);
    let broker_up = Arc::new(AtomicBool::new(true));
    let seal_on = Arc::new(AtomicBool::new(true));
    let sealing = Sealing::new(
        TenantCrypto::new(Provider {
            up: broker_up.clone(),
        }),
        Arc::new(Gate(seal_on.clone())),
    );
    Rig {
        host: KvHost::new(backend, Arc::new(caps)).with_sealing(sealing),
        broker_up,
        seal_on,
    }
}

pub(crate) fn scope(tenant: i32, community: i32) -> KvScope {
    KvScope::new("slug", Some("name".to_string()), "app.a").with_ids(tenant, community)
}

#[tokio::test]
async fn sealed_round_trip() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.host
        .set(&s, 1, "k", b"hello", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let got = r
        .host
        .get(&s, 2, "k")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(got.as_deref(), Some(&b"hello"[..]));
}

#[tokio::test]
async fn missing_key_refuses_write_and_nothing_is_stored() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.broker_up.store(false, Ordering::SeqCst);
    let err = r.host.set(&s, 1, "k", b"hello", 0).await;
    assert!(matches!(err, Err(KvError::KeyUnavailable(_))), "{err:?}");
    // Broker back up: the refused write left nothing behind.
    r.broker_up.store(true, Ordering::SeqCst);
    let got = r
        .host
        .get(&s, 2, "k")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(got, None);
}

#[tokio::test]
async fn missing_key_refuses_read_of_sealed_value() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.host
        .set(&s, 1, "k", b"hello", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    r.broker_up.store(false, Ordering::SeqCst);
    let err = r.host.get(&s, 2, "k").await;
    assert!(matches!(err, Err(KvError::KeyUnavailable(_))), "{err:?}");
}

#[tokio::test]
async fn scope_without_numeric_ids_refuses() {
    let r = rig(FakeBackend::new());
    let s = KvScope::new("slug", None, "app.a");
    let err = r.host.set(&s, 1, "k", b"v", 0).await;
    assert!(matches!(err, Err(KvError::KeyUnavailable(_))), "{err:?}");
}

#[tokio::test]
async fn legacy_plaintext_reads_through() {
    let backend = FakeBackend::new();
    let s = scope(1, 2);
    backend.seed_unindexed(&s.data_key("old"), b"legacy-value", None);
    let r = rig(backend);
    let got = r
        .host
        .get(&s, 1, "old")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(got.as_deref(), Some(&b"legacy-value"[..]));
}

#[tokio::test]
async fn sealed_value_does_not_open_for_other_tenant_or_key() {
    let r = rig(FakeBackend::new());
    let s1 = scope(1, 2);
    let s2 = scope(2, 2);
    r.host
        .set(&s1, 1, "k", b"secret", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let sealing = r
        .host
        .sealing
        .clone()
        .unwrap_or_else(|| panic!("sealing attached"));
    let stored = crate::KvBackend::get(&r.host.backend, &s1.data_key("k"))
        .await
        .unwrap_or_else(|e| panic!("{e}"))
        .unwrap_or_default();
    assert!(!stored.windows(6).any(|w| w == b"secret"));
    let cross_tenant = sealing
        .open_for_read(&r.host.backend, &s2, "k", stored.clone())
        .await;
    assert!(cross_tenant.is_err(), "cross-tenant open must fail");
    let other_key = sealing
        .open_for_read(&r.host.backend, &s1, "other-key", stored)
        .await;
    assert!(
        matches!(other_key, Err(KvError::Integrity(_))),
        "{other_key:?}"
    );
}

#[tokio::test]
async fn kill_switch_writes_plaintext_but_reads_still_open_envelopes() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.host
        .set(&s, 1, "sealed", b"one", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    r.seal_on.store(false, Ordering::SeqCst);
    r.host
        .set(&s, 2, "plain", b"two", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let raw = crate::KvBackend::get(&r.host.backend, &s.data_key("plain"))
        .await
        .unwrap_or_default();
    assert_eq!(raw.as_deref(), Some(&b"two"[..]));
    let got = r
        .host
        .get(&s, 3, "sealed")
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(got.as_deref(), Some(&b"one"[..]));
}

#[tokio::test]
async fn sealed_increment_is_not_plaintext_at_rest_and_accumulates() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    assert_eq!(r.host.increment(&s, 1, "n", 41, 0).await.unwrap_or(-1), 41);
    assert_eq!(r.host.increment(&s, 2, "n", 1, 0).await.unwrap_or(-1), 42);
    let raw = crate::KvBackend::get(&r.host.backend, &s.data_key("n"))
        .await
        .unwrap_or_default()
        .unwrap_or_default();
    assert!(
        bundle_host_crypto::is_envelope(&raw),
        "stored counter must be sealed"
    );
    assert!(
        !raw.windows(2).any(|w| w == b"42"),
        "no plaintext digits at rest"
    );
    let got = r.host.get(&s, 3, "n").await.unwrap_or_default();
    assert_eq!(got.as_deref(), Some(&b"42"[..]));
}

#[tokio::test]
async fn sealed_increment_refuses_when_the_key_is_unavailable_and_writes_nothing() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.broker_up.store(false, Ordering::SeqCst);
    let err = r.host.increment(&s, 1, "n", 1, 0).await;
    assert!(matches!(err, Err(KvError::KeyUnavailable(_))), "{err:?}");
    r.broker_up.store(true, Ordering::SeqCst);
    assert_eq!(r.host.get(&s, 2, "n").await.unwrap_or(Some(vec![1])), None);
}

#[tokio::test]
async fn sealed_increment_rejects_non_integers_and_overflow() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.host
        .set(&s, 1, "text", b"not-a-number", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let err = r.host.increment(&s, 2, "text", 1, 0).await;
    assert!(matches!(err, Err(KvError::Backend(_))), "{err:?}");
    r.host
        .increment(&s, 3, "big", i64::MAX, 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let err = r.host.increment(&s, 4, "big", 1, 0).await;
    assert!(matches!(err, Err(KvError::Backend(_))), "{err:?}");
}

#[tokio::test]
async fn kill_switch_increment_stores_a_plaintext_integer_and_still_reads_sealed() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.host.increment(&s, 1, "n", 5, 0).await.unwrap_or_default();
    r.seal_on.store(false, Ordering::SeqCst);
    assert_eq!(r.host.increment(&s, 2, "n", 2, 0).await.unwrap_or(-1), 7);
    let raw = crate::KvBackend::get(&r.host.backend, &s.data_key("n"))
        .await
        .unwrap_or_default()
        .unwrap_or_default();
    assert_eq!(raw, b"7");
}

// Concurrent increments must not lose updates: the compare-and-set conflict
// path re-reads and retries until every delta lands exactly once.
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_sealed_increments_lose_no_updates() {
    const TASKS: usize = 8;
    const PER_TASK: usize = 20;
    let r = Arc::new(rig(FakeBackend::new()));
    let s = scope(1, 2);
    let mut joins = Vec::new();
    for t in 0..TASKS {
        let r = r.clone();
        let s = s.clone();
        joins.push(tokio::spawn(async move {
            for i in 0..PER_TASK {
                let call_id = (t * PER_TASK + i) as u64;
                r.host
                    .increment(&s, call_id, "shared", 1, 0)
                    .await
                    .unwrap_or_else(|e| panic!("increment: {e}"));
            }
        }));
    }
    for j in joins {
        j.await.unwrap_or_else(|e| panic!("join: {e}"));
    }
    let got = r.host.get(&s, 9_999, "shared").await.unwrap_or_default();
    assert_eq!(
        got.as_deref(),
        Some((TASKS * PER_TASK).to_string().as_bytes())
    );
}

#[tokio::test]
async fn sealed_versioned_cas_round_trips_plaintext_and_stores_ciphertext() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    let created = r
        .host
        .cas(&s, 1, "k", 0, b"one", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let crate::KvCasResult::Swapped(v1) = created else {
        panic!("create-if-absent must swap, got {created:?}");
    };
    let raw = crate::KvBackend::get(&r.host.backend, &s.data_key("k"))
        .await
        .unwrap_or_default()
        .unwrap_or_default();
    assert!(
        bundle_host_crypto::is_envelope(&raw) && !raw.windows(3).any(|w| w == b"one"),
        "cas must never store plaintext under sealing"
    );
    let (plain, ver) = r
        .host
        .get_versioned(&s, 2, "k")
        .await
        .unwrap_or_else(|e| panic!("{e}"))
        .unwrap_or_else(|| panic!("value present"));
    assert_eq!(plain, b"one");
    assert_eq!(ver, v1);
    let next = r
        .host
        .cas(&s, 3, "k", ver, b"two", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert!(matches!(next, crate::KvCasResult::Swapped(_)));
}

#[tokio::test]
async fn sealed_cas_refuses_and_writes_nothing_when_the_key_is_unavailable() {
    let r = rig(FakeBackend::new());
    let s = scope(1, 2);
    r.broker_up.store(false, Ordering::SeqCst);
    assert!(r.host.cas(&s, 1, "k", 0, b"one", 0).await.is_err());
    assert!(crate::KvBackend::get(&r.host.backend, &s.data_key("k"))
        .await
        .unwrap_or_default()
        .is_none());
}

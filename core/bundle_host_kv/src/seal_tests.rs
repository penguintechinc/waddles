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
struct Provider {
    up: Arc<AtomicBool>,
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

struct Gate(Arc<AtomicBool>);

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

fn scope(tenant: i32, community: i32) -> KvScope {
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
    let cross_tenant = sealing.open_for_read(&s2, "k", stored.clone()).await;
    assert!(cross_tenant.is_err(), "cross-tenant open must fail");
    let other_key = sealing.open_for_read(&s1, "other-key", stored).await;
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

//! Tests for the legacy-plaintext read policy: operator
//! `legacy_plaintext_reads_allowed`, the persisted per-scope sealed-since
//! marker, the kill-switch override, and the WARN log shape.

use std::fmt::Write as _;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use bundle_host_crypto::TenantCrypto;
use tracing::field::{Field, Visit};
use tracing::{span, Event, Level, Metadata, Subscriber};

use crate::backend::fake::FakeBackend;
use crate::seal_tests::{scope, Gate, Provider};
use crate::{CapabilitySnapshot, KvBackend, KvError, KvHost, KvScope, Sealing};

struct Rig {
    host: KvHost<FakeBackend>,
    seal_on: Arc<AtomicBool>,
}

fn rig(backend: FakeBackend, legacy_allowed: bool) -> Rig {
    let caps = CapabilitySnapshot::new();
    caps.update("app.a", [crate::authorize::KV_PERMISSION_ID.to_string()]);
    let seal_on = Arc::new(AtomicBool::new(true));
    let sealing = Sealing::new(
        TenantCrypto::new(Provider {
            up: Arc::new(AtomicBool::new(true)),
        }),
        Arc::new(Gate(seal_on.clone())),
    )
    .with_legacy_plaintext_reads_allowed(legacy_allowed);
    Rig {
        host: KvHost::new(backend, Arc::new(caps)).with_sealing(sealing),
        seal_on,
    }
}

fn plant(r: &Rig, key: &str, value: &[u8]) {
    r.host
        .backend
        .seed_unindexed(&scope(1, 2).data_key(key), value, None);
}

// (a) operator policy

#[tokio::test]
async fn legacy_policy_off_refuses_plaintext_as_integrity() {
    let r = rig(FakeBackend::new(), false);
    plant(&r, "doc", b"{\"legacy\":true}");
    let err = r.host.get(&scope(1, 2), 1, "doc").await;
    assert!(matches!(err, Err(KvError::Integrity(_))), "{err:?}");
}

#[tokio::test]
async fn legacy_policy_on_accepts_plaintext_in_an_unmarked_scope() {
    let r = rig(FakeBackend::new(), true);
    plant(&r, "doc", b"{\"legacy\":true}");
    let got = r.host.get(&scope(1, 2), 1, "doc").await;
    assert_eq!(
        got.ok().flatten().as_deref(),
        Some(&b"{\"legacy\":true}"[..])
    );
}

#[tokio::test]
async fn sealed_counters_are_readable_with_policy_off_but_legacy_integers_are_not() {
    let r = rig(FakeBackend::new(), false);
    let s = scope(1, 2);
    r.host
        .increment(&s, 1, "hits", 5, 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let got = r.host.get(&s, 2, "hits").await;
    assert_eq!(got.ok().flatten().as_deref(), Some(&b"5"[..]));
    // A pre-sealing plaintext integer is a legacy value: refused once the
    // operator turns legacy reads OFF.
    plant(&r, "old-counter", b"9");
    let err = r.host.get(&s, 3, "old-counter").await;
    assert!(matches!(err, Err(KvError::Integrity(_))), "{err:?}");
}

#[tokio::test]
async fn legacy_plaintext_counter_is_migrated_to_sealed_by_the_next_increment() {
    let r = rig(FakeBackend::new(), true);
    let s = scope(1, 2);
    plant(&r, "old-counter", b"9");
    let v = r
        .host
        .increment(&s, 1, "old-counter", 1, 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert_eq!(v, 10);
    let raw = crate::KvBackend::get(&r.host.backend, &s.data_key("old-counter"))
        .await
        .unwrap_or_default()
        .unwrap_or_default();
    assert!(
        bundle_host_crypto::is_envelope(&raw),
        "counter must now be sealed"
    );
    let got = r.host.get(&s, 2, "old-counter").await;
    assert_eq!(got.ok().flatten().as_deref(), Some(&b"10"[..]));
}

// (b) sealed-since marker

#[tokio::test]
async fn first_sealed_write_persists_the_marker_and_blocks_planted_plaintext() {
    let r = rig(FakeBackend::new(), true);
    let s = scope(1, 2);
    assert!(r
        .host
        .backend
        .get(&s.sealed_marker_key())
        .await
        .unwrap_or_default()
        .is_none());
    r.host
        .set(&s, 1, "real", b"sealed-value", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    assert!(
        r.host
            .backend
            .get(&s.sealed_marker_key())
            .await
            .unwrap_or_default()
            .is_some(),
        "marker must be persisted in the backend, not only in memory"
    );
    // Someone with raw Valkey access plants plaintext in the same scope.
    plant(&r, "planted", b"attacker-controlled");
    let err = r.host.get(&s, 2, "planted").await;
    assert!(matches!(err, Err(KvError::Integrity(_))), "{err:?}");
}

#[tokio::test]
async fn marker_survives_restart_because_it_lives_in_the_backend() {
    // A fresh process (empty in-memory cache) over a backend that already
    // carries the marker must still refuse plaintext, policy ON.
    let backend = FakeBackend::new();
    backend.seed_unindexed(&scope(1, 2).sealed_marker_key(), b"1", None);
    let r = rig(backend, true);
    plant(&r, "doc", b"plain");
    let err = r.host.get(&scope(1, 2), 1, "doc").await;
    assert!(matches!(err, Err(KvError::Integrity(_))), "{err:?}");
}

#[tokio::test]
async fn marker_is_per_scope() {
    let r = rig(FakeBackend::new(), true);
    r.host
        .set(&scope(1, 2), 1, "k", b"v", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    // A different community namespace (today's keys are name-scoped) under
    // the same tenant has no marker yet, so its legacy value is accepted.
    let other = KvScope::new("slug", Some("other-community".to_string()), "app.a").with_ids(1, 3);
    r.host
        .backend
        .seed_unindexed(&other.data_key("old"), b"legacy", None);
    let got = r.host.get(&other, 2, "old").await;
    assert_eq!(got.ok().flatten().as_deref(), Some(&b"legacy"[..]));
}

#[tokio::test]
async fn kill_switch_on_accepts_plaintext_despite_marker_and_policy() {
    let r = rig(FakeBackend::new(), false);
    let s = scope(1, 2);
    r.host
        .set(&s, 1, "sealed", b"v", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    r.seal_on.store(false, Ordering::SeqCst);
    r.host
        .set(&s, 2, "rollback", b"plain-by-operator", 0)
        .await
        .unwrap_or_else(|e| panic!("{e}"));
    let got = r.host.get(&s, 3, "rollback").await;
    assert_eq!(
        got.ok().flatten().as_deref(),
        Some(&b"plain-by-operator"[..])
    );
}

// (c) WARN on every legacy read, no key or value

#[derive(Clone, Default)]
struct Captured(Arc<Mutex<Vec<(Level, String)>>>);

struct Fields(String);

impl Visit for Fields {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        let _ = write!(self.0, "{}={:?} ", field.name(), value);
    }
}

impl Subscriber for Captured {
    fn enabled(&self, _: &Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &span::Attributes<'_>) -> span::Id {
        span::Id::from_u64(1)
    }
    fn record(&self, _: &span::Id, _: &span::Record<'_>) {}
    fn record_follows_from(&self, _: &span::Id, _: &span::Id) {}
    fn event(&self, event: &Event<'_>) {
        let mut f = Fields(String::new());
        event.record(&mut f);
        if let Ok(mut g) = self.0.lock() {
            g.push((*event.metadata().level(), f.0));
        }
    }
    fn enter(&self, _: &span::Id) {}
    fn exit(&self, _: &span::Id) {}
}

#[tokio::test]
async fn every_legacy_read_warns_with_app_and_key_hash_but_never_key_or_value() {
    let captured = Captured::default();
    let _guard = tracing::subscriber::set_default(captured.clone());
    let r = rig(FakeBackend::new(), true);
    plant(&r, "legacykey1", b"legacy-secret-value");
    for call in 1..=2 {
        let got = r.host.get(&scope(1, 2), call, "legacykey1").await;
        assert!(got.is_ok());
    }
    let events = captured.0.lock().map(|g| g.clone()).unwrap_or_default();
    let warns: Vec<&String> = events
        .iter()
        .filter(|(l, m)| *l == Level::WARN && m.contains("LEGACY PLAINTEXT"))
        .map(|(_, m)| m)
        .collect();
    assert_eq!(warns.len(), 2, "one WARN per legacy read: {events:?}");
    let hash = crate::key_hash("legacykey1");
    for w in &warns {
        assert!(w.contains("app_id=app.a"), "{w}");
        assert!(w.contains(&hash), "{w}");
    }
    for (_, m) in &events {
        assert!(!m.contains("legacykey1"), "raw key leaked: {m}");
        assert!(!m.contains("legacy-secret-value"), "value leaked: {m}");
    }
}

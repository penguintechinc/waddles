//! Per-tenant sealing of object bodies: round trip, ciphertext-at-rest, and the
//! cross-tenant / cross-community / cross-key isolation proofs (a body copied to another
//! scope must NOT open), fail-closed key trouble, legacy-plaintext policy and the
//! kill-switch. Real `StorageHost` + real AES-GCM envelope over `MemoryBackend`; only the
//! DEK provider (per-tenant keys, switchable to "broker down") is a test double.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};

use bundle_host_crypto::{Dek, DekProvider, DekUnavailableError, TenantCrypto, MAGIC};
use bundle_host_storage::key::object_key;
use bundle_host_storage::seal::BoxFuture;
use bundle_host_storage::{
    AuditEvent, AuditSink, GrantSet, MemoryBackend, MemoryPlainLedger, ObjectBackend,
    ObjectSealing, SealGate, StorageError, StorageHost, StorageScope,
};
use bytes::Bytes;
use zeroize::Zeroizing;

const APP: &str = "waddles.harness.media";
const PLAIN: &[u8] = b"top secret clip metadata";

struct Keys {
    up: Arc<AtomicBool>,
    active: Arc<AtomicU32>,
}

impl DekProvider for Keys {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        let v = self.active.load(Ordering::SeqCst);
        self.get_dek_version(tenant_id, v).await.map(|d| (d, v))
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        if !self.up.load(Ordering::SeqCst)
            || version == 0
            || version > self.active.load(Ordering::SeqCst)
        {
            return Err(DekUnavailableError {
                tenant_id: tenant_id.to_string(),
                dek_version: Some(version),
                reason: "test: unavailable".into(),
            });
        }
        // A distinct DEK per tenant id.
        let byte = tenant_id
            .bytes()
            .fold(11u8, |a, b| a.wrapping_mul(31).wrapping_add(b))
            .wrapping_add((version as u8).wrapping_mul(53));
        Ok(Zeroizing::new([byte; 32]))
    }
}

struct Gate(Arc<AtomicBool>);
impl SealGate for Gate {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        let on = self.0.load(Ordering::SeqCst);
        Box::pin(async move { on })
    }
}

#[derive(Default)]
struct Sink(Mutex<Vec<AuditEvent>>);
#[async_trait::async_trait]
impl AuditSink for Sink {
    async fn record(&self, e: AuditEvent) -> Result<(), StorageError> {
        self.0.lock().expect("audit").push(e);
        Ok(())
    }
}

struct Rig {
    host: StorageHost,
    backend: Arc<MemoryBackend>,
    broker_up: Arc<AtomicBool>,
    seal_on: Arc<AtomicBool>,
    active: Arc<AtomicU32>,
}

fn rig(legacy_cutoff: Option<u64>) -> Rig {
    let backend = Arc::new(MemoryBackend::new());
    let broker_up = Arc::new(AtomicBool::new(true));
    let seal_on = Arc::new(AtomicBool::new(true));
    let active = Arc::new(AtomicU32::new(1));
    let sealing = ObjectSealing::new(TenantCrypto::new(Keys {
        up: broker_up.clone(),
        active: active.clone(),
    }))
    .with_plain_ledger(Arc::new(MemoryPlainLedger::default()))
    .with_gate(Arc::new(Gate(seal_on.clone())));
    let sealing = match legacy_cutoff {
        Some(cutoff) => sealing.with_legacy_plaintext_until(cutoff),
        None => sealing,
    };
    Rig {
        host: StorageHost::with_rate_limit(backend.clone(), Arc::new(Sink::default()), 1000)
            .with_sealing(sealing),
        backend,
        broker_up,
        seal_on,
        active,
    }
}

fn scope(tenant: u64, community: u64) -> StorageScope {
    StorageScope {
        app_id: APP.into(),
        tenant,
        community,
        grants: GrantSet::from_permission_ids(std::iter::empty::<&str>()).expect("grants"),
        enabled: true,
        request_id: "req-1".into(),
    }
}

async fn put(r: &Rig, s: &StorageScope, key: &str) {
    r.host
        .put(s, None, key, Bytes::from_static(PLAIN), "text/plain", &[])
        .await
        .expect("put");
}

/// Raw stored copy (what the bucket holds).
async fn raw(r: &Rig, s: &StorageScope, key: &str) -> bundle_host_storage::StoredObject {
    r.backend
        .get(&object_key(APP, s.tenant, s.community, key))
        .await
        .expect("backend get")
        .expect("exists")
}

/// Copies the raw sealed object (body + tags) from one scope/key to another, as a party with
/// bucket-level write access could -- the host's tag checks pass, only the crypto can refuse.
async fn transplant(r: &Rig, from: (&StorageScope, &str), to: (&StorageScope, &str)) {
    let src = raw(r, from.0, from.1).await;
    let tags: Vec<String> = src
        .meta
        .tags
        .iter()
        .filter(|t| !t.starts_with("waddles-tenant=") && !t.starts_with("waddles-community="))
        .cloned()
        .chain([
            format!("waddles-tenant={}", to.0.tenant),
            format!("waddles-community={}", to.0.community),
        ])
        .collect();
    r.backend
        .put(
            &object_key(APP, to.0.tenant, to.0.community, to.1),
            src.body,
            &src.meta.content_type,
            &tags,
        )
        .await
        .expect("transplant");
}

#[tokio::test]
async fn body_is_ciphertext_at_rest_and_round_trips_with_plaintext_sizes() {
    let r = rig(None);
    let s = scope(1, 7);
    put(&r, &s, "clip.txt").await;
    let stored = raw(&r, &s, "clip.txt").await;
    assert_eq!(&stored.body[..4], &MAGIC, "stored body is an envelope");
    assert!(
        !stored.body.windows(PLAIN.len()).any(|w| w == PLAIN),
        "plaintext must not appear in the bucket"
    );
    let (info, body) = r.host.get(&s, None, "clip.txt").await.expect("get");
    assert_eq!(&body[..], PLAIN);
    assert_eq!(info.size, PLAIN.len() as u64);
    assert_eq!(
        r.host.head(&s, None, "clip.txt").await.expect("head").size,
        PLAIN.len() as u64
    );
    let page = r.host.list(&s, None, "", None, 10).await.expect("list");
    assert_eq!(page.objects[0].size, PLAIN.len() as u64);
}

#[tokio::test]
async fn another_tenants_copy_does_not_open() {
    let r = rig(None);
    let a = scope(1, 7);
    let b = scope(2, 7);
    put(&r, &a, "doc").await;
    // B cannot reach A's object by key (separate prefix).
    assert_eq!(
        r.host.get(&b, None, "doc").await.unwrap_err(),
        StorageError::NotFound
    );
    // Even with the raw sealed bytes planted under B's own prefix, B's DEK refuses them.
    transplant(&r, (&a, "doc"), (&b, "doc")).await;
    let err = r.host.get(&b, None, "doc").await.unwrap_err();
    assert_eq!(
        err,
        StorageError::Backend("object integrity check failed".into())
    );
    // A is unaffected.
    assert_eq!(
        &r.host.get(&a, None, "doc").await.expect("get").1[..],
        PLAIN
    );
}

#[tokio::test]
async fn cross_community_and_cross_key_swaps_fail() {
    let r = rig(None);
    let c7 = scope(1, 7);
    let c8 = scope(1, 8);
    put(&r, &c7, "a").await;
    put(&r, &c7, "b").await;
    transplant(&r, (&c7, "a"), (&c8, "a")).await;
    assert!(
        r.host.get(&c8, None, "a").await.is_err(),
        "same tenant, other community"
    );
    transplant(&r, (&c7, "a"), (&c7, "b")).await;
    assert!(
        r.host.get(&c7, None, "b").await.is_err(),
        "same scope, other key"
    );
}

#[tokio::test]
async fn key_trouble_fails_closed_on_put_and_get() {
    let r = rig(None);
    let s = scope(1, 7);
    put(&r, &s, "keep").await;
    r.broker_up.store(false, Ordering::SeqCst);
    let err = r
        .host
        .put(
            &s,
            None,
            "new",
            Bytes::from_static(PLAIN),
            "text/plain",
            &[],
        )
        .await
        .unwrap_err();
    assert_eq!(
        err,
        StorageError::Backend("tenant data key unavailable".into())
    );
    assert!(
        r.backend
            .get(&object_key(APP, 1, 7, "new"))
            .await
            .expect("backend")
            .is_none(),
        "nothing written without a key"
    );
    assert_eq!(
        r.host.get(&s, None, "keep").await.unwrap_err(),
        StorageError::Backend("tenant data key unavailable".into())
    );
}

const CUTOFF: u64 = 2_000_000_000;

fn legacy_tags() -> Vec<String> {
    vec!["waddles-tenant=1".into(), "waddles-community=7".into()]
}

fn seed_legacy(r: &Rig, key: &str, modified: u64, tags: &[String]) {
    r.backend
        .put_with_modified(
            &object_key(APP, 1, 7, key),
            Bytes::from_static(PLAIN),
            "text/plain",
            tags,
            modified,
        )
        .expect("seed");
}

#[tokio::test]
async fn legacy_plaintext_is_refused_by_default() {
    let r = rig(None);
    seed_legacy(&r, "old", 1, &legacy_tags());
    assert!(r.host.get(&scope(1, 7), None, "old").await.is_err());
}

#[tokio::test]
async fn legacy_plaintext_served_only_inside_the_opt_in_window() {
    let r = rig(Some(CUTOFF));
    let s = scope(1, 7);
    seed_legacy(&r, "older", CUTOFF - 1, &legacy_tags());
    seed_legacy(&r, "newer", CUTOFF, &legacy_tags());
    assert_eq!(
        &r.host.get(&s, None, "older").await.expect("in window").1[..],
        PLAIN
    );
    assert!(
        r.host.get(&s, None, "newer").await.is_err(),
        "at/after the cutoff: refused"
    );
}

#[tokio::test]
async fn stripped_seal_is_refused_even_with_the_window_open() {
    // An attacker with bucket write replaces a sealed object with plaintext and strips the
    // tags. The server-assigned last-modified is NOW, past any cutoff recorded earlier, so the
    // replacement is not "legacy" and is refused -- with or without an opt-in window.
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_secs();
    for cutoff in [None, Some(now - 3600)] {
        let r = rig(cutoff);
        let s = scope(1, 7);
        put(&r, &s, "victim").await;
        r.backend
            .put(
                &object_key(APP, 1, 7, "victim"),
                Bytes::from_static(b"attacker chosen plaintext"),
                "text/plain",
                &legacy_tags(),
            )
            .await
            .expect("attacker overwrite");
        assert!(
            r.host.get(&s, None, "victim").await.is_err(),
            "cutoff {cutoff:?}"
        );
    }
}

#[tokio::test]
async fn tagged_sealed_object_with_plaintext_body_is_an_integrity_failure() {
    let r = rig(None);
    let s = scope(1, 7);
    r.backend
        .put(
            &object_key(APP, 1, 7, "forged"),
            Bytes::from_static(PLAIN),
            "text/plain",
            &[
                "waddles-tenant=1".into(),
                "waddles-community=7".into(),
                "waddles-sealed=1".into(),
            ],
        )
        .await
        .expect("seed");
    assert!(r.host.get(&s, None, "forged").await.is_err());
}

#[tokio::test]
async fn kill_switch_plaintext_is_mac_authenticated_and_forgery_is_refused() {
    let r = rig(None);
    let s = scope(1, 7);
    put(&r, &s, "sealed").await;
    r.seal_on.store(false, Ordering::SeqCst);
    put(&r, &s, "plain").await;
    let stored = raw(&r, &s, "plain").await;
    assert_eq!(&stored.body[..], PLAIN);
    assert!(stored
        .meta
        .tags
        .iter()
        .any(|t| t.starts_with("waddles-plain=")));
    // Sealed objects still open, and the host-written plaintext reads back (MAC verifies).
    assert_eq!(
        &r.host.get(&s, None, "sealed").await.expect("get").1[..],
        PLAIN
    );
    assert_eq!(
        &r.host.get(&s, None, "plain").await.expect("get").1[..],
        PLAIN
    );

    // Tampered body under the original MAC tag: refused.
    r.backend
        .put(
            &object_key(APP, 1, 7, "plain"),
            Bytes::from_static(b"tampered"),
            "text/plain",
            &stored.meta.tags,
        )
        .await
        .expect("tamper");
    assert!(r.host.get(&s, None, "plain").await.is_err());

    // Forged MAC tag (attacker lacks the tenant key): refused.
    let mut tags: Vec<String> = legacy_tags();
    tags.push(format!("waddles-plain={}", "ab".repeat(32)));
    r.backend
        .put(
            &object_key(APP, 1, 7, "plain"),
            Bytes::from_static(b"forged"),
            "text/plain",
            &tags,
        )
        .await
        .expect("forge");
    assert!(r.host.get(&s, None, "plain").await.is_err());

    // A MAC minted for another tenant's scope does not transplant.
    let other = scope(2, 7);
    r.seal_on.store(false, Ordering::SeqCst);
    put(&r, &other, "plain").await;
    let theirs = raw(&r, &other, "plain").await;
    let mut moved = legacy_tags();
    moved.extend(
        theirs
            .meta
            .tags
            .iter()
            .filter(|t| t.starts_with("waddles-plain="))
            .cloned(),
    );
    r.backend
        .put(
            &object_key(APP, 1, 7, "plain"),
            theirs.body,
            "text/plain",
            &moved,
        )
        .await
        .expect("transplant");
    assert!(r.host.get(&s, None, "plain").await.is_err());
}

#[tokio::test]
async fn sealed_objects_have_no_presigned_url_and_unsealed_host_refuses_them() {
    let r = rig(None);
    let s = scope(1, 7);
    put(&r, &s, "x").await;
    let err = r.host.presign_get(&s, None, "x", 60).await.unwrap_err();
    assert!(matches!(err, StorageError::Invalid(m) if m.contains("tenant-sealed")));

    // A host built WITHOUT sealing must not hand back ciphertext as if it were the object.
    let bare = StorageHost::with_rate_limit(r.backend.clone(), Arc::new(Sink::default()), 1000);
    assert!(matches!(
        bare.get(&s, None, "x").await.unwrap_err(),
        StorageError::Backend(m) if m.contains("sealing is not configured")
    ));
}

fn plain_tag_of(o: &bundle_host_storage::StoredObject) -> String {
    o.meta
        .tags
        .iter()
        .find(|t| t.starts_with("waddles-plain="))
        .cloned()
        .expect("plain tag")
}

fn with_tag(o: &bundle_host_storage::StoredObject, tag: String) -> Vec<String> {
    o.meta
        .tags
        .iter()
        .filter(|t| !t.starts_with("waddles-plain="))
        .cloned()
        .chain([tag])
        .collect()
}

#[tokio::test]
async fn rotation_keeps_recent_plain_tags_verifiable_and_rewrite_moves_to_the_new_version() {
    let r = rig(None);
    let s = scope(1, 7);
    r.seal_on.store(false, Ordering::SeqCst);
    put(&r, &s, "p").await;
    assert!(plain_tag_of(&raw(&r, &s, "p").await).starts_with("waddles-plain=v1:1:"));
    // One rotation: still inside the window.
    r.active.store(2, Ordering::SeqCst);
    assert_eq!(
        &r.host
            .get(&s, None, "p")
            .await
            .expect("v1 tag after rotation")
            .1[..],
        PLAIN
    );
    // A rewrite re-tags under v2 with the next counter.
    put(&r, &s, "p").await;
    assert!(plain_tag_of(&raw(&r, &s, "p").await).starts_with("waddles-plain=v2:2:"));
    // Two rotations past the tag's version: outside the window, refused until rewritten.
    r.active.store(4, Ordering::SeqCst);
    assert!(r.host.get(&s, None, "p").await.is_err());
}

#[tokio::test]
async fn replayed_older_plain_body_and_tag_is_refused() {
    let r = rig(None);
    let s = scope(1, 7);
    r.seal_on.store(false, Ordering::SeqCst);
    r.host
        .put(
            &s,
            None,
            "k",
            Bytes::from_static(b"old value"),
            "text/plain",
            &[],
        )
        .await
        .expect("put old");
    let old = raw(&r, &s, "k").await;
    r.host
        .put(
            &s,
            None,
            "k",
            Bytes::from_static(b"new value"),
            "text/plain",
            &[],
        )
        .await
        .expect("put new");
    assert_eq!(
        &r.host.get(&s, None, "k").await.expect("current").1[..],
        b"new value"
    );
    // Attacker restores the earlier (valid-at-the-time) body + tags wholesale.
    r.backend
        .put(
            &object_key(APP, 1, 7, "k"),
            old.body,
            "text/plain",
            &old.meta.tags,
        )
        .await
        .expect("rollback");
    assert!(r.host.get(&s, None, "k").await.is_err(), "rollback refused");
}

#[tokio::test]
async fn plain_tag_version_mismatch_is_refused() {
    let r = rig(None);
    let s = scope(1, 7);
    r.seal_on.store(false, Ordering::SeqCst);
    put(&r, &s, "v").await;
    r.active.store(2, Ordering::SeqCst);
    let o = raw(&r, &s, "v").await;
    // Claim the MAC (made under v1) was made under v2.
    let forged = plain_tag_of(&o).replacen("=v1:", "=v2:", 1);
    r.backend
        .put(
            &object_key(APP, 1, 7, "v"),
            o.body.clone(),
            "text/plain",
            &with_tag(&o, forged),
        )
        .await
        .expect("forge");
    assert!(r.host.get(&s, None, "v").await.is_err());
    // Version beyond the active one is refused outright.
    let beyond = plain_tag_of(&o).replacen("=v1:", "=v9:", 1);
    r.backend
        .put(
            &object_key(APP, 1, 7, "v"),
            o.body.clone(),
            "text/plain",
            &with_tag(&o, beyond),
        )
        .await
        .expect("forge");
    assert!(r.host.get(&s, None, "v").await.is_err());
}

#[tokio::test]
async fn plaintext_writes_without_a_ledger_are_refused() {
    let backend = Arc::new(MemoryBackend::new());
    let up = Arc::new(AtomicBool::new(true));
    let host = StorageHost::with_rate_limit(backend, Arc::new(Sink::default()), 1000).with_sealing(
        ObjectSealing::new(TenantCrypto::new(Keys {
            up,
            active: Arc::new(AtomicU32::new(1)),
        }))
        .with_gate(Arc::new(Gate(Arc::new(AtomicBool::new(false))))),
    );
    let err = host
        .put(
            &scope(1, 7),
            None,
            "x",
            Bytes::from_static(PLAIN),
            "text/plain",
            &[],
        )
        .await
        .unwrap_err();
    assert!(matches!(err, StorageError::Backend(m) if m.contains("rollback ledger")));
}

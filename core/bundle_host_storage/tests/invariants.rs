//! Behavioural tests for the storage host over `MemoryBackend`. The tag-filter
//! invariant is asserted PER OP so deleting the filter from any one op fails a test.

use std::sync::{Arc, Mutex};

use bundle_host_storage::{
    AuditEvent, AuditOutcome, AuditSink, GrantSet, MemoryBackend, ObjectBackend, StorageError,
    StorageHost, StorageScope,
};
use bytes::Bytes;

#[derive(Default)]
struct Rec(Mutex<Vec<AuditEvent>>);
#[async_trait::async_trait]
impl AuditSink for Rec {
    async fn record(&self, e: AuditEvent) -> Result<(), StorageError> {
        self.0.lock().expect("audit lock").push(e);
        Ok(())
    }
}

/// Sink that always fails -- proves audit-first / fail-closed behaviour.
struct FailingSink;
#[async_trait::async_trait]
impl AuditSink for FailingSink {
    async fn record(&self, _e: AuditEvent) -> Result<(), StorageError> {
        Err(StorageError::Backend("audit down".into()))
    }
}

const OWNER: &str = "waddles.harness.media";
const CONSUMER: &str = "waddles.app.clips";
const OTHER: &str = "waddles.app.other";

fn scope(app: &str, tenant: u64, community: u64, grants: &[&str]) -> StorageScope {
    StorageScope {
        app_id: app.into(),
        tenant,
        community,
        grants: GrantSet::from_permission_ids(grants.iter().copied()).expect("grants"),
        enabled: true,
        request_id: "req-1".into(),
    }
}

fn rig() -> (StorageHost, Arc<MemoryBackend>, Arc<Rec>) {
    let b = Arc::new(MemoryBackend::new());
    let r = Arc::new(Rec::default());
    (
        StorageHost::with_rate_limit(b.clone(), r.clone(), 1000),
        b,
        r,
    )
}

fn owner_scope() -> StorageScope {
    scope(OWNER, 1, 7, &[])
}
fn consumer_scope() -> StorageScope {
    scope(
        CONSUMER,
        1,
        7,
        &[
            "storage.read:waddles.harness.media",
            "storage.list:waddles.harness.media",
            "storage.delete:waddles.harness.media",
            "storage.write:waddles.harness.media",
        ],
    )
}

async fn seed(h: &StorageHost, key: &str, tags: &[&str]) {
    let tags: Vec<String> = tags.iter().map(|t| t.to_string()).collect();
    h.put(
        &owner_scope(),
        None,
        key,
        Bytes::from_static(b"data"),
        "video/mp4",
        &tags,
    )
    .await
    .expect("seed");
}

#[tokio::test]
async fn own_prefix_allowed_all_ops() {
    let (h, _, _) = rig();
    let s = owner_scope();
    h.put(
        &s,
        None,
        "a/b.mp4",
        Bytes::from_static(b"x"),
        "video/mp4",
        &[],
    )
    .await
    .unwrap();
    assert_eq!(
        h.get(&s, None, "a/b.mp4").await.unwrap().1,
        Bytes::from_static(b"x")
    );
    assert_eq!(
        h.list(&s, None, "", None, 10).await.unwrap().objects.len(),
        1
    );
    assert!(h.presign_get(&s, None, "a/b.mp4", 60).await.is_ok());
    h.delete(&s, None, "a/b.mp4").await.unwrap();
    assert_eq!(
        h.get(&s, None, "a/b.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
}

#[tokio::test]
async fn other_prefix_denied_without_approval_and_audited() {
    let (h, _, rec) = rig();
    seed(&h, "k.mp4", &[&format!("app-bundle:{CONSUMER}")]).await;
    let s = scope(CONSUMER, 1, 7, &[]);
    for r in [
        h.get(&s, Some(OWNER), "k.mp4").await.map(|_| ()),
        h.head(&s, Some(OWNER), "k.mp4").await.map(|_| ()),
        h.list(&s, Some(OWNER), "", None, 10).await.map(|_| ()),
        h.delete(&s, Some(OWNER), "k.mp4").await,
        h.presign_get(&s, Some(OWNER), "k.mp4", 30)
            .await
            .map(|_| ()),
        h.put(&s, Some(OWNER), "n.mp4", Bytes::new(), "video/mp4", &[])
            .await
            .map(|_| ()),
    ] {
        assert!(matches!(r, Err(StorageError::NotGranted(_))), "{r:?}");
    }
    h.flush_denials().await;
    let ev = rec.0.lock().unwrap();
    assert!(ev.len() >= 6 && ev.iter().all(|e| e.outcome == AuditOutcome::Denied));
}

#[tokio::test]
async fn approved_grant_untagged_object_hidden_every_op() {
    let (h, b, _) = rig();
    seed(&h, "k.mp4", &[]).await;
    let s = consumer_scope();
    assert_eq!(
        h.get(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    assert_eq!(
        h.head(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    assert!(h
        .list(&s, Some(OWNER), "", None, 10)
        .await
        .unwrap()
        .objects
        .is_empty());
    assert_eq!(
        h.presign_get(&s, Some(OWNER), "k.mp4", 30)
            .await
            .unwrap_err(),
        StorageError::NotFound
    );
    assert_eq!(
        h.delete(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    // delete must not have removed it
    assert!(b
        .head(&format!("{OWNER}/1/7/k.mp4"))
        .await
        .unwrap()
        .is_some());
    // overwrite of an untagged object is refused
    let r = h
        .put(
            &s,
            Some(OWNER),
            "k.mp4",
            Bytes::from_static(b"z"),
            "video/mp4",
            &[],
        )
        .await;
    assert!(matches!(r, Err(StorageError::NotGranted(_))));
}

#[tokio::test]
async fn tagged_for_other_bundle_hidden_every_op() {
    let (h, _, _) = rig();
    seed(&h, "k.mp4", &[&format!("app-bundle:{OTHER}")]).await;
    let s = consumer_scope();
    assert_eq!(
        h.get(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    assert_eq!(
        h.head(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    assert!(h
        .list(&s, Some(OWNER), "", None, 10)
        .await
        .unwrap()
        .objects
        .is_empty());
    assert_eq!(
        h.presign_get(&s, Some(OWNER), "k.mp4", 30)
            .await
            .unwrap_err(),
        StorageError::NotFound
    );
    assert_eq!(
        h.delete(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
}

#[tokio::test]
async fn tagged_for_self_visible_every_op() {
    let (h, _, rec) = rig();
    seed(&h, "k.mp4", &[&format!("app-bundle:{CONSUMER}")]).await;
    seed(&h, "hidden.mp4", &[]).await;
    let s = consumer_scope();
    assert_eq!(
        h.get(&s, Some(OWNER), "k.mp4").await.unwrap().1,
        Bytes::from_static(b"data")
    );
    assert_eq!(h.head(&s, Some(OWNER), "k.mp4").await.unwrap().size, 4);
    let page = h.list(&s, Some(OWNER), "", None, 10).await.unwrap();
    assert_eq!(
        page.objects
            .iter()
            .map(|o| o.key.as_str())
            .collect::<Vec<_>>(),
        vec!["k.mp4"]
    );
    assert!(h.presign_get(&s, Some(OWNER), "k.mp4", 30).await.is_ok());
    h.delete(&s, Some(OWNER), "k.mp4").await.unwrap();
    assert!(rec
        .0
        .lock()
        .unwrap()
        .iter()
        .any(|e| e.outcome == AuditOutcome::Allowed && e.owner == OWNER));
}

#[tokio::test]
async fn cross_writer_object_is_auto_tagged_and_free_tags_refused() {
    let (h, b, _) = rig();
    let s = consumer_scope();
    h.put(
        &s,
        Some(OWNER),
        "up.png",
        Bytes::from_static(b"p"),
        "image/png",
        &[],
    )
    .await
    .unwrap();
    let m = b
        .head(&format!("{OWNER}/1/7/up.png"))
        .await
        .unwrap()
        .unwrap();
    assert!(
        m.tags.contains(&format!("app-bundle:{CONSUMER}"))
            && !m
                .tags
                .iter()
                .any(|t| t.starts_with("app-bundle:") && !t.ends_with(CONSUMER))
    );
    let r = h
        .put(
            &s,
            Some(OWNER),
            "x.png",
            Bytes::new(),
            "image/png",
            &["app-bundle:evil".into()],
        )
        .await;
    assert!(matches!(r, Err(StorageError::Invalid(_))));
}

#[tokio::test]
async fn cross_tenant_and_community_isolated() {
    let (h, _, _) = rig();
    seed(&h, "k.mp4", &[&format!("app-bundle:{CONSUMER}")]).await;
    for (t, c) in [(2u64, 7u64), (1, 8), (1, 0)] {
        let s = scope(
            CONSUMER,
            t,
            c,
            &[
                "storage.read:waddles.harness.media",
                "storage.list:waddles.harness.media",
            ],
        );
        assert_eq!(
            h.get(&s, Some(OWNER), "k.mp4").await.unwrap_err(),
            StorageError::NotFound,
            "{t}/{c}"
        );
        assert!(h
            .list(&s, Some(OWNER), "", None, 10)
            .await
            .unwrap()
            .objects
            .is_empty());
    }
    // owner of another scope can't see it either
    assert_eq!(
        h.get(&scope(OWNER, 2, 7, &[]), None, "k.mp4")
            .await
            .unwrap_err(),
        StorageError::NotFound
    );
}

#[tokio::test]
async fn traversal_and_encoded_separators_refused_before_backend() {
    let (h, b, _) = rig();
    let s = consumer_scope();
    for k in [
        "../x",
        "a/../../b",
        "/abs",
        "a%2f..%2fb",
        "%2e%2e/x",
        "a\\b",
        "a//b",
        "",
    ] {
        assert!(
            matches!(
                h.get(&s, Some(OWNER), k).await,
                Err(StorageError::Invalid(_))
            ),
            "{k:?}"
        );
        assert!(matches!(
            h.put(&owner_scope(), None, k, Bytes::new(), "image/png", &[])
                .await,
            Err(StorageError::Invalid(_))
        ));
    }
    assert!(matches!(
        h.list(&s, Some(OWNER), "../", None, 5).await,
        Err(StorageError::Invalid(_))
    ));
    assert!(matches!(
        h.get(&s, Some("../etc"), "k").await,
        Err(StorageError::Invalid(_))
    ));
    assert!(b.list("", None, 10).await.unwrap().is_empty());
}

#[tokio::test]
async fn limits_content_type_size_quota_rate() {
    let (h, _, _) = rig();
    let s = owner_scope();
    let big = Bytes::from(vec![0u8; 25 * 1024 * 1024 + 1]);
    assert!(matches!(
        h.put(&s, None, "big", big, "video/mp4", &[]).await,
        Err(StorageError::TooLarge(_))
    ));
    assert!(matches!(
        h.put(&s, None, "x", Bytes::new(), "text/html", &[]).await,
        Err(StorageError::Invalid(_))
    ));
    // quota: 21 x 25MiB > 512MiB
    let chunk = Bytes::from(vec![0u8; 25 * 1024 * 1024]);
    let mut err = None;
    for i in 0..21 {
        if let Err(e) = h
            .put(&s, None, &format!("o{i}"), chunk.clone(), "video/mp4", &[])
            .await
        {
            err = Some(e);
            break;
        }
    }
    assert_eq!(err, Some(StorageError::QuotaExceeded));
    // rate limit
    let b = Arc::new(MemoryBackend::new());
    let lim = StorageHost::with_rate_limit(b, Arc::new(Rec::default()), 2);
    let s = owner_scope();
    lim.put(&s, None, "a", Bytes::new(), "image/png", &[])
        .await
        .unwrap();
    lim.put(&s, None, "b", Bytes::new(), "image/png", &[])
        .await
        .unwrap();
    assert_eq!(
        lim.put(&s, None, "c", Bytes::new(), "image/png", &[])
            .await
            .unwrap_err(),
        StorageError::RateLimited
    );
}

#[tokio::test]
async fn presign_scope_and_ttl_clamp() {
    let (h, _, _) = rig();
    seed(&h, "k.mp4", &[&format!("app-bundle:{CONSUMER}")]).await;
    let url = h
        .presign_get(&consumer_scope(), Some(OWNER), "k.mp4", 99_999)
        .await
        .unwrap();
    assert!(
        url.contains(&format!("{OWNER}/1/7/k.mp4")) && url.ends_with("ttl=300"),
        "{url}"
    );
    assert!(matches!(
        h.presign_get(&consumer_scope(), Some(OWNER), "k.mp4", 0)
            .await,
        Err(StorageError::Invalid(_))
    ));
}

#[tokio::test]
async fn flag_off_is_not_implemented() {
    let (h, _, _) = rig();
    let mut s = owner_scope();
    s.enabled = false;
    assert_eq!(
        h.get(&s, None, "k").await.unwrap_err(),
        StorageError::NotImplemented
    );
    assert_eq!(
        h.list(&s, None, "", None, 1).await.unwrap_err(),
        StorageError::NotImplemented
    );
    assert_eq!(
        h.tag_add(&s, None, "k", CONSUMER).await.unwrap_err(),
        StorageError::NotImplemented
    );
}

#[tokio::test]
async fn owner_can_tag_untag_audited_non_owner_cannot() {
    let (h, _, rec) = rig();
    seed(&h, "k.mp4", &[]).await;
    let c = consumer_scope();
    assert_eq!(
        h.get(&c, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    h.tag_add(&owner_scope(), None, "k.mp4", CONSUMER)
        .await
        .unwrap();
    assert!(h.get(&c, Some(OWNER), "k.mp4").await.is_ok());
    h.tag_remove(&owner_scope(), None, "k.mp4", CONSUMER)
        .await
        .unwrap();
    assert_eq!(
        h.get(&c, Some(OWNER), "k.mp4").await.unwrap_err(),
        StorageError::NotFound
    );
    // non-owner (even with every grant) cannot tag, explicitly or via owner arg
    assert!(matches!(
        h.tag_add(&c, Some(OWNER), "k.mp4", CONSUMER).await,
        Err(StorageError::NotGranted(_))
    ));
    assert!(matches!(
        h.tag_remove(&c, Some(OWNER), "k.mp4", OTHER).await,
        Err(StorageError::NotGranted(_))
    ));
    assert_eq!(
        h.tag_add(&c, None, "k.mp4", CONSUMER).await.unwrap_err(),
        StorageError::NotFound
    ); // own (empty) prefix
    h.flush_denials().await;
    let ev = rec.0.lock().unwrap();
    let ops: Vec<(&str, AuditOutcome)> = ev
        .iter()
        .filter(|e| e.op.starts_with("tag_"))
        .map(|e| (e.op, e.outcome))
        .collect();
    assert!(ops.contains(&("tag_add", AuditOutcome::Allowed)));
    assert!(ops.contains(&("tag_remove", AuditOutcome::Allowed)));
    assert_eq!(
        ops.iter()
            .filter(|(_, o)| *o == AuditOutcome::Denied)
            .count(),
        2
    );
}

#[tokio::test]
async fn list_pagination_and_tag_filter_interleaved() {
    let (h, _, _) = rig();
    for i in 0..6 {
        let tags: Vec<String> = if i % 2 == 0 {
            vec![format!("app-bundle:{CONSUMER}")]
        } else {
            vec![]
        };
        h.put(
            &owner_scope(),
            None,
            &format!("d/f{i}"),
            Bytes::new(),
            "image/png",
            &tags,
        )
        .await
        .unwrap();
    }
    let s = consumer_scope();
    let p1 = h.list(&s, Some(OWNER), "d", None, 2).await.unwrap();
    assert_eq!(
        p1.objects.iter().map(|o| o.key.clone()).collect::<Vec<_>>(),
        vec!["d/f0", "d/f2"]
    );
    let p2 = h
        .list(&s, Some(OWNER), "d", p1.next_cursor.as_deref(), 2)
        .await
        .unwrap();
    assert_eq!(
        p2.objects.iter().map(|o| o.key.clone()).collect::<Vec<_>>(),
        vec!["d/f4"]
    );
    assert!(p2.next_cursor.is_none());
}

#[tokio::test]
async fn system_tags_written_request_id_recorded_and_guest_cannot_set() {
    let (h, b, _) = rig();
    seed(&h, "k.mp4", &[]).await;
    let m = b
        .head(&format!("{OWNER}/1/7/k.mp4"))
        .await
        .unwrap()
        .unwrap();
    for t in [
        "waddles-tenant=1",
        "waddles-community=7",
        "waddles-request-id=req-1",
    ] {
        assert!(m.tags.iter().any(|x| x == t), "{t} in {:?}", m.tags);
    }
    for bad in [
        "waddles-tenant=2",
        "waddles-community=1",
        "waddles-request-id=x",
        "waddles-tenant:2",
    ] {
        let r = h
            .put(
                &owner_scope(),
                None,
                "g.mp4",
                Bytes::new(),
                "video/mp4",
                &[bad.into()],
            )
            .await;
        assert!(matches!(r, Err(StorageError::Invalid(_))), "{bad}");
    }
    // cross-prefix writer cannot inject either
    let r = h
        .put(
            &consumer_scope(),
            Some(OWNER),
            "g.mp4",
            Bytes::new(),
            "video/mp4",
            &["waddles-tenant=1".into()],
        )
        .await;
    assert!(matches!(r, Err(StorageError::Invalid(_))));
    // tag ops preserve system tags
    h.tag_add(&owner_scope(), None, "k.mp4", CONSUMER)
        .await
        .unwrap();
    let m = b
        .head(&format!("{OWNER}/1/7/k.mp4"))
        .await
        .unwrap()
        .unwrap();
    assert!(
        m.tags.contains(&"waddles-tenant=1".to_string())
            && m.tags.contains(&"waddles-request-id=req-1".to_string())
    );
    let mut bad = owner_scope();
    bad.request_id = "bad id!".into();
    assert!(h
        .put(&bad, None, "z.mp4", Bytes::new(), "video/mp4", &[])
        .await
        .is_err());
}

#[tokio::test]
async fn system_tag_mismatch_hidden_even_with_matching_app_bundle_tag_and_correct_prefix() {
    let (h, b, _) = rig();
    // Simulate a path mistake: object at tenant 1 / community 7 path carrying app-bundle tag for the
    // consumer but system tags of another community / tenant, or missing system tags entirely.
    let key = format!("{OWNER}/1/7");
    for (name, tags) in [
        (
            "wrongcomm",
            vec![
                format!("app-bundle:{CONSUMER}"),
                "waddles-tenant=1".into(),
                "waddles-community=8".into(),
            ],
        ),
        (
            "wrongten",
            vec![
                format!("app-bundle:{CONSUMER}"),
                "waddles-tenant=2".into(),
                "waddles-community=7".into(),
            ],
        ),
        ("notags", vec![format!("app-bundle:{CONSUMER}")]),
    ] {
        b.put(
            &format!("{key}/{name}"),
            Bytes::from_static(b"x"),
            "video/mp4",
            &tags,
        )
        .await
        .unwrap();
        let c = consumer_scope();
        assert_eq!(
            h.get(&c, Some(OWNER), name).await.unwrap_err(),
            StorageError::NotFound,
            "{name}"
        );
        assert_eq!(
            h.head(&c, Some(OWNER), name).await.unwrap_err(),
            StorageError::NotFound
        );
        assert_eq!(
            h.presign_get(&c, Some(OWNER), name, 10).await.unwrap_err(),
            StorageError::NotFound
        );
        assert_eq!(
            h.delete(&c, Some(OWNER), name).await.unwrap_err(),
            StorageError::NotFound
        );
        // owner (defense in depth) is also blocked
        assert_eq!(
            h.get(&owner_scope(), None, name).await.unwrap_err(),
            StorageError::NotFound,
            "owner {name}"
        );
        assert_eq!(
            h.head(&owner_scope(), None, name).await.unwrap_err(),
            StorageError::NotFound
        );
        assert_eq!(
            h.delete(&owner_scope(), None, name).await.unwrap_err(),
            StorageError::NotFound
        );
    }
    assert!(h
        .list(&consumer_scope(), Some(OWNER), "", None, 10)
        .await
        .unwrap()
        .objects
        .is_empty());
    assert!(h
        .list(&owner_scope(), None, "", None, 10)
        .await
        .unwrap()
        .objects
        .is_empty());
}

#[tokio::test]
async fn failed_audit_fails_allowed_cross_prefix_and_tag_ops_without_effect() {
    let b = Arc::new(MemoryBackend::new());
    // Seed through a working host, then drive the same backend through a failing-audit host.
    let ok = StorageHost::with_rate_limit(b.clone(), Arc::new(Rec::default()), 1000);
    seed(&ok, "k.mp4", &[&format!("app-bundle:{CONSUMER}")]).await;
    seed(&ok, "own.mp4", &[]).await;
    let h = StorageHost::with_rate_limit(b.clone(), Arc::new(FailingSink), 1000);
    let c = consumer_scope();
    // cross-prefix ALLOWED paths fail when the audit cannot be written
    assert!(matches!(
        h.get(&c, Some(OWNER), "k.mp4").await,
        Err(StorageError::Backend(_))
    ));
    assert!(matches!(
        h.head(&c, Some(OWNER), "k.mp4").await,
        Err(StorageError::Backend(_))
    ));
    assert!(matches!(
        h.list(&c, Some(OWNER), "", None, 5).await,
        Err(StorageError::Backend(_))
    ));
    assert!(matches!(
        h.presign_get(&c, Some(OWNER), "k.mp4", 5).await,
        Err(StorageError::Backend(_))
    ));
    // mutations are audit-FIRST: no effect when the audit fails
    assert!(matches!(
        h.delete(&c, Some(OWNER), "k.mp4").await,
        Err(StorageError::Backend(_))
    ));
    assert!(b
        .head(&format!("{OWNER}/1/7/k.mp4"))
        .await
        .unwrap()
        .is_some());
    let r = h
        .put(&c, Some(OWNER), "new.png", Bytes::new(), "image/png", &[])
        .await;
    assert!(matches!(r, Err(StorageError::Backend(_))));
    assert!(b
        .head(&format!("{OWNER}/1/7/new.png"))
        .await
        .unwrap()
        .is_none());
    let before = b
        .head(&format!("{OWNER}/1/7/own.mp4"))
        .await
        .unwrap()
        .unwrap()
        .tags;
    let r = h.tag_add(&owner_scope(), None, "own.mp4", CONSUMER).await;
    assert!(matches!(r, Err(StorageError::Backend(_))));
    assert_eq!(
        b.head(&format!("{OWNER}/1/7/own.mp4"))
            .await
            .unwrap()
            .unwrap()
            .tags,
        before
    );
    // own-prefix ops need no audit and still work; denials stay denials (not audit errors)
    assert!(h.get(&owner_scope(), None, "own.mp4").await.is_ok());
    let nogrant = scope(CONSUMER, 1, 7, &[]);
    assert!(matches!(
        h.get(&nogrant, Some(OWNER), "k.mp4").await,
        Err(StorageError::NotGranted(_))
    ));
}

#[tokio::test]
async fn audit_events_carry_request_id_and_cover_tag_ops_and_list() {
    let (h, _, rec) = rig();
    seed(&h, "k.mp4", &[&format!("app-bundle:{CONSUMER}")]).await;
    h.tag_add(&owner_scope(), None, "k.mp4", OTHER)
        .await
        .unwrap();
    h.list(&consumer_scope(), Some(OWNER), "", None, 5)
        .await
        .unwrap();
    h.flush_denials().await;
    let ev = rec.0.lock().unwrap();
    assert!(ev.iter().all(|e| e.request_id == "req-1"));
    assert!(ev
        .iter()
        .any(|e| e.op == "tag_add" && e.reason == format!("consumer={OTHER}")));
    assert!(ev
        .iter()
        .any(|e| e.op == "list" && e.outcome == AuditOutcome::Allowed));
}

/// Sink whose `record` never completes: models a saturated/stuck audit DB.
struct StuckSink;
#[async_trait::async_trait]
impl AuditSink for StuckSink {
    async fn record(&self, _e: AuditEvent) -> Result<(), StorageError> {
        std::future::pending::<()>().await;
        Ok(())
    }
}

#[tokio::test]
async fn denied_call_spam_is_rate_limited_before_any_audit_work() {
    let rec = Arc::new(Rec::default());
    let h = StorageHost::with_rate_limit(Arc::new(MemoryBackend::new()), rec.clone(), 3);
    let s = scope(CONSUMER, 1, 7, &[]); // no grants: every cross-prefix call is a denial
    let mut denied = 0;
    let mut limited = 0;
    for _ in 0..20 {
        match h.get(&s, Some(OWNER), "k.mp4").await {
            Err(StorageError::NotGranted(_)) => denied += 1,
            Err(StorageError::RateLimited) => limited += 1,
            other => panic!("{other:?}"),
        }
    }
    assert_eq!((denied, limited), (3, 17));
    // list + tag paths too
    assert!(matches!(
        h.list(&s, Some(OWNER), "", None, 5).await,
        Err(StorageError::RateLimited)
    ));
    assert!(matches!(
        h.tag_add(&s, Some(OWNER), "k.mp4", OTHER).await,
        Err(StorageError::RateLimited)
    ));
    h.flush_denials().await;
    assert_eq!(
        rec.0.lock().unwrap().len(),
        3,
        "only budgeted denials are audited"
    );
}

#[tokio::test]
async fn list_and_tag_denials_are_rate_limited_first_too() {
    let h =
        StorageHost::with_rate_limit(Arc::new(MemoryBackend::new()), Arc::new(Rec::default()), 1);
    let s = scope(CONSUMER, 1, 7, &[]);
    assert!(matches!(
        h.list(&s, Some(OWNER), "", None, 5).await,
        Err(StorageError::NotGranted(_))
    ));
    assert!(matches!(
        h.list(&s, Some(OWNER), "", None, 5).await,
        Err(StorageError::RateLimited)
    ));
    let h2 =
        StorageHost::with_rate_limit(Arc::new(MemoryBackend::new()), Arc::new(Rec::default()), 1);
    assert!(matches!(
        h2.tag_add(&s, Some(OWNER), "k", OTHER).await,
        Err(StorageError::NotGranted(_))
    ));
    assert!(matches!(
        h2.tag_add(&s, Some(OWNER), "k", OTHER).await,
        Err(StorageError::RateLimited)
    ));
}

#[tokio::test]
async fn stuck_audit_sink_cannot_block_or_unbound_denials() {
    let h = StorageHost::with_rate_limit(
        Arc::new(MemoryBackend::new()),
        Arc::new(StuckSink),
        1_000_000,
    );
    let s = scope(CONSUMER, 1, 7, &[]);
    // Far more denials than the queue holds: every call still returns promptly
    // (records are dropped + counted, never awaited).
    let run = async {
        for _ in 0..2000 {
            assert!(matches!(
                h.get(&s, Some(OWNER), "k.mp4").await,
                Err(StorageError::NotGranted(_))
            ));
        }
    };
    tokio::time::timeout(std::time::Duration::from_secs(10), run)
        .await
        .expect("denials must never wait on the audit sink");
}

#[tokio::test]
async fn tag_ops_validate_the_audited_owner_string() {
    let (h, _, rec) = rig();
    seed(&h, "k.mp4", &[]).await;
    let c = consumer_scope();
    for bad in ["../x", "A B", "a\0b", "x/y", ""] {
        let r = h.tag_add(&c, Some(bad), "k.mp4", CONSUMER).await;
        assert!(
            matches!(r, Err(StorageError::Invalid(_))),
            "{bad:?} -> {r:?}"
        );
    }
    h.flush_denials().await;
    assert!(
        rec.0.lock().unwrap().is_empty(),
        "invalid owners never reach the ledger"
    );
}

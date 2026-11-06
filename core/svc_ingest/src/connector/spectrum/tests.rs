//! Unit tests for the Spectrum transport against a local fake RSI API.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::VecDeque;
use std::sync::Mutex;

use connection_resolver::{ConnectionRef, SecretString};
use serde_json::json;
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

use super::*;
use crate::connector::poll::{PollAuth, PollConfig, PollRequest};
use crate::connector::secrets::{Destination, ResolvedAuth};
use crate::connector::webhook::SecretError;

const REF: ConnectionRef = ConnectionRef {
    community_id: 7,
    connection_id: 12,
};

struct Tok;
impl TransportSecretSource for Tok {
    fn resolve(
        &self,
        _r: ConnectionRef,
        _d: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
        Box::pin(async {
            Ok(ResolvedAuth {
                secret: SecretString::new("rsi-tok".into()),
                header: None,
            })
        })
    }
}

/// Answers from a script (last answer repeats).
struct Script(Mutex<VecDeque<(u16, serde_json::Value)>>);
impl Respond for Script {
    fn respond(&self, _: &Request) -> ResponseTemplate {
        let mut q = self.0.lock().unwrap();
        let (code, body) = if q.len() > 1 {
            q.pop_front().unwrap()
        } else {
            q.front().cloned().unwrap()
        };
        ResponseTemplate::new(code).set_body_json(body)
    }
}

async fn server(script: Vec<(u16, serde_json::Value)>) -> MockServer {
    let s = MockServer::start().await;
    Mock::given(wiremock::matchers::any())
        .respond_with(Script(Mutex::new(script.into())))
        .mount(&s)
        .await;
    s
}

fn env_ok(data: serde_json::Value) -> (u16, serde_json::Value) {
    (200, json!({"success": 1, "code": "OK", "data": data}))
}

fn msgs(ids: &[&str]) -> (u16, serde_json::Value) {
    let items: Vec<_> = ids
        .iter()
        .map(|i| json!({"id": i, "plaintext": format!("t{i}"), "member": {"id": "m1"}, "time_created": 100}))
        .collect();
    env_ok(json!({"messages": items}))
}

fn poll_cfg(s: &MockServer, org: bool) -> PollConfig {
    PollConfig {
        platform: "spectrum".into(),
        url: format!("{}/api/spectrum/x", s.uri()),
        source_key: "spectrum:test".into(),
        handles: std::collections::HashMap::from([("h".to_owned(), REF)]),
        auth: Some(PollAuth {
            header: "X-Rsi-Token".into(),
            prefix: String::new(),
            handle: "h".into(),
        }),
        cursor_param: None,
        cursor_pointer: None,
        initial_cursor: None,
        interval: Duration::from_millis(2),
        request_timeout: Duration::from_secs(2),
        max_body_bytes: 1 << 20,
        max_drop_attempts: 3,
        egress_allow: vec!["127.0.0.1".parse().unwrap()],
        allow_loopback_insecure: true,
        lifecycle: Default::default(),
        request: PollRequest {
            json_body: Some(serde_json::to_vec(&json!({"community_id": "c", "page": 1})).unwrap()),
            extra_auth: vec![PollAuth {
                header: "Cookie".into(),
                prefix: "Rsi-Token=".into(),
                handle: "h".into(),
            }],
            page_field: org.then(|| "page".to_owned()),
        },
    }
}

fn spec(kind: SpectrumKind) -> SpectrumSpec {
    SpectrumSpec {
        kind,
        key: "k".into(),
        page_delay: Duration::from_millis(1),
        max_pages: 4,
        guard_ratio: 0.5,
        guard_min_size: 10,
        seen_cap: 3,
        ttl: Duration::from_secs(60),
        max_drop_attempts: 2,
    }
}

fn transport(
    s: &MockServer,
    kind: SpectrumKind,
    store: &Arc<MemorySpectrumStore>,
) -> SpectrumTransport<Tok> {
    let inner = PollTransport::new(poll_cfg(s, kind.is_org()), Tok).unwrap();
    let st: Arc<dyn SpectrumStore> = store.clone();
    SpectrumTransport::new(inner, st, spec(kind))
}

async fn frame(t: &mut SpectrumTransport<Tok>) -> serde_json::Value {
    let b = tokio::time::timeout(Duration::from_secs(5), t.next_frame())
        .await
        .expect("frame in time")
        .unwrap()
        .expect("a frame");
    serde_json::from_slice(&b).unwrap()
}

async fn no_frame(t: &mut SpectrumTransport<Tok>) {
    let r = tokio::time::timeout(Duration::from_millis(150), t.next_frame()).await;
    assert!(r.is_err(), "no frame expected, got {r:?}");
}

fn ids_of(f: &serde_json::Value) -> Vec<String> {
    f["data"]["messages"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["id"].as_str().unwrap().to_owned())
        .collect()
}

#[tokio::test]
async fn memory_store_semantics() {
    let s = MemorySpectrumStore::new();
    let ttl = Duration::from_secs(1);
    assert!(!s.is_primed("k".into()).await.unwrap());
    s.remember("k".into(), vec!["a".into(), "b".into(), "a".into()], 2, ttl)
        .await
        .unwrap();
    assert!(s.is_primed("k".into()).await.unwrap());
    s.remember("k".into(), vec!["c".into()], 2, ttl)
        .await
        .unwrap();
    assert_eq!(
        s.unseen(
            "k".into(),
            vec!["a".into(), "b".into(), "c".into(), "d".into()]
        )
        .await
        .unwrap(),
        ["a", "d"],
        "cap 2 evicted the oldest (a)"
    );
    assert_eq!(s.load_snapshot("k".into()).await.unwrap(), None);
    s.save_snapshot("k".into(), "{}".into(), ttl).await.unwrap();
    assert_eq!(
        s.load_snapshot("k".into()).await.unwrap().as_deref(),
        Some("{}")
    );
    s.set_failing(true);
    assert_eq!(
        s.is_primed("k".into()).await.unwrap_err(),
        StoreError("unavailable")
    );
    s.set_failing(false);
    assert!(s.is_primed("k".into()).await.is_ok());
}

#[tokio::test]
async fn kind_labels() {
    for (k, n, org) in [
        (SpectrumKind::Lobby, "lobby", false),
        (SpectrumKind::Forum, "forum", false),
        (SpectrumKind::Roster, "roster", true),
        (SpectrumKind::Events, "events", true),
    ] {
        assert_eq!((k.as_str(), k.is_org()), (n, org));
    }
}

#[tokio::test]
async fn first_poll_primes_then_only_new_ids_flow_and_are_remembered_on_publish() {
    let s = server(vec![
        msgs(&["m1", "m2"]),
        msgs(&["m1", "m2", "m3"]),
        msgs(&["m1", "m2", "m3"]),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Lobby, &store);
    t.connect().await.unwrap();
    let f = frame(&mut t).await;
    assert_eq!(
        ids_of(&f),
        ["m3"],
        "history (m1,m2) primed away; only m3 forwarded"
    );
    assert!(store.is_primed("k".into()).await.unwrap());
    // not yet published -> not remembered
    assert_eq!(
        store.unseen("k".into(), vec!["m3".into()]).await.unwrap(),
        ["m3"]
    );
    t.on_frame_outcome(FrameOutcome::Published).await;
    assert!(store
        .unseen("k".into(), vec!["m3".into()])
        .await
        .unwrap()
        .is_empty());
    no_frame(&mut t).await;
}

#[tokio::test]
async fn a_dropped_window_is_re_yielded_then_poison_skipped() {
    let s = server(vec![msgs(&["a"]), msgs(&["a", "b"])]).await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Lobby, &store);
    t.connect().await.unwrap();
    assert_eq!(ids_of(&frame(&mut t).await), ["b"]);
    t.on_frame_outcome(FrameOutcome::Dropped(FrameDropReason::GuestError))
        .await;
    assert_eq!(
        ids_of(&frame(&mut t).await),
        ["b"],
        "dropped -> same id offered again"
    );
    t.on_frame_outcome(FrameOutcome::Dropped(FrameDropReason::GuestError))
        .await;
    // max_drop_attempts = 2: the poison window's ids are now remembered
    assert!(store
        .unseen("k".into(), vec!["b".into()])
        .await
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn forum_uses_the_threads_list_and_drops_idless_entries() {
    let s = server(vec![
        env_ok(json!({"threads": [{"id": 1, "subject": "s"}]})),
        env_ok(json!({"threads": [{"id": 1, "subject": "s"}, {"id": 2, "subject": "n"}, {"subject": "noid"}]})),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Forum, &store);
    t.connect().await.unwrap();
    let f = frame(&mut t).await;
    let th = f["data"]["threads"].as_array().unwrap();
    assert_eq!(th.len(), 1);
    assert_eq!(th[0]["id"], 2);
}

#[tokio::test]
async fn failure_envelopes_are_terminal_and_auth_codes_are_named() {
    for (body, want) in [
        (
            json!({"success": 0, "code": "ErrApiLoginRequired"}),
            "rejected the session",
        ),
        (json!({"success": 0, "code": "Other"}), "reported failure"),
        (json!({"success": 1}), "no data object"),
        (json!([1]), "not an object"),
    ] {
        let s = server(vec![(200, body)]).await;
        let store = Arc::new(MemorySpectrumStore::new());
        let mut t = transport(&s, SpectrumKind::Lobby, &store);
        t.connect().await.unwrap();
        let e = t.next_frame().await.unwrap_err();
        assert!(e.is_fatal() && e.to_string().contains(want), "{e}");
    }
    let s = server(vec![env_ok(json!({"other": []}))]).await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Lobby, &store);
    assert!(t
        .next_frame()
        .await
        .unwrap_err()
        .to_string()
        .contains("list missing"));
}

#[tokio::test]
async fn non_json_body_is_terminal() {
    let s = MockServer::start().await;
    Mock::given(wiremock::matchers::any())
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>"))
        .mount(&s)
        .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Lobby, &store);
    assert!(t.next_frame().await.unwrap_err().is_fatal());
}

#[tokio::test]
async fn store_outage_is_a_retryable_error_never_read_as_empty() {
    let s = server(vec![msgs(&["a"])]).await;
    let store = Arc::new(MemorySpectrumStore::new());
    store.set_failing(true);
    let mut t = transport(&s, SpectrumKind::Lobby, &store);
    t.connect().await.unwrap();
    let e = t.next_frame().await.unwrap_err();
    assert!(
        !e.is_fatal() && matches!(e, TransportError::Read(_)),
        "{e:?}"
    );
}

fn member(id: &str, roles: &[&str]) -> serde_json::Value {
    json!({"id": id, "roles": roles})
}

fn roster_page(members: Vec<serde_json::Value>, total: Option<u64>) -> (u16, serde_json::Value) {
    let mut d = json!({"members": members});
    if let Some(t) = total {
        d["total"] = json!(t);
    }
    env_ok(d)
}

#[tokio::test]
async fn roster_pages_with_host_written_page_numbers_primes_then_diffs() {
    let s = server(vec![
        // cycle 1 (prime): two pages, total 3
        roster_page(vec![member("1", &["a"]), member("2", &[])], Some(3)),
        roster_page(vec![member("3", &[])], Some(3)),
        // cycle 2: member 3 left, 4 joined, 1 changed roles
        roster_page(vec![member("1", &["b"]), member("2", &[])], Some(3)),
        roster_page(vec![member("4", &["x"])], Some(3)),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    let f1 = frame(&mut t).await;
    assert_eq!(f1["kind"], "roster");
    assert_eq!(f1["change"], "roles_changed");
    assert_eq!(f1["member_id"], "1");
    assert_eq!(f1["roles_added"], json!(["b"]));
    assert!(f1.get("display_name").is_none());
    let mut got = vec![(
        f1["change"].as_str().unwrap().to_owned(),
        f1["member_id"].as_str().unwrap().to_owned(),
    )];
    for _ in 0..2 {
        t.on_frame_outcome(FrameOutcome::Published).await;
        let f = frame(&mut t).await;
        got.push((
            f["change"].as_str().unwrap().to_owned(),
            f["member_id"].as_str().unwrap().to_owned(),
        ));
    }
    t.on_frame_outcome(FrameOutcome::Published).await;
    got.sort();
    assert_eq!(
        got,
        [
            ("joined".into(), "4".into()),
            ("left".into(), "3".into()),
            ("roles_changed".into(), "1".into())
        ]
    );
    // snapshot advanced to the new roster after all three were published
    let snap: serde_json::Value =
        serde_json::from_str(&store.load_snapshot("k".into()).await.unwrap().unwrap()).unwrap();
    assert!(snap["members"].get("4").is_some() && snap["members"].get("3").is_none());
    // the host wrote page numbers 1,2,1,2 into the body
    let pages: Vec<u64> = s
        .received_requests()
        .await
        .unwrap()
        .iter()
        .take(4)
        .map(|r| {
            serde_json::from_slice::<serde_json::Value>(&r.body).unwrap()["page"]
                .as_u64()
                .unwrap()
        })
        .collect();
    assert_eq!(pages, [1, 2, 1, 2]);
}

#[tokio::test]
async fn a_dropped_change_holds_the_snapshot_so_it_re_emits() {
    let s = server(vec![
        roster_page(vec![member("1", &[])], Some(1)),
        roster_page(vec![member("1", &[]), member("2", &[])], Some(2)),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    let f = frame(&mut t).await;
    assert_eq!(f["change"], "joined");
    t.on_frame_outcome(FrameOutcome::Dropped(FrameDropReason::PublishIncomplete))
        .await;
    let snap: serde_json::Value =
        serde_json::from_str(&store.load_snapshot("k".into()).await.unwrap().unwrap()).unwrap();
    assert!(
        snap["members"].get("2").is_none(),
        "held: snapshot still the primed one"
    );
    assert_eq!(
        frame(&mut t).await["member_id"],
        "2",
        "re-emitted next cycle"
    );
}

#[tokio::test]
async fn roster_anomaly_is_a_retryable_error_and_does_not_advance() {
    let many: Vec<_> = (0..12).map(|i| member(&i.to_string(), &[])).collect();
    let s = server(vec![
        roster_page(many, None),
        roster_page(vec![member("0", &[])], None),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    let e = t.next_frame().await.unwrap_err();
    assert!(!e.is_fatal() && e.to_string().contains("anomaly"), "{e}");
    let snap: serde_json::Value =
        serde_json::from_str(&store.load_snapshot("k".into()).await.unwrap().unwrap()).unwrap();
    assert_eq!(snap["members"].as_object().unwrap().len(), 12);
}

#[tokio::test]
async fn partial_snapshots_are_refused() {
    // page cap (4) reached with data still arriving
    let pages: Vec<_> = (0..6)
        .map(|i| roster_page(vec![member(&format!("p{i}"), &[])], None))
        .collect();
    let s = server(pages).await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    let e = t.next_frame().await.unwrap_err();
    assert!(
        e.is_fatal() && e.to_string().contains("partial snapshot"),
        "{e}"
    );
    // none parseable
    let s = server(vec![roster_page(vec![json!({"displayname": "x"})], None)]).await;
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    assert!(t
        .next_frame()
        .await
        .unwrap_err()
        .to_string()
        .contains("none parseable"));
    // missing list
    let s = server(vec![env_ok(json!({"nope": []}))]).await;
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    assert!(t
        .next_frame()
        .await
        .unwrap_err()
        .to_string()
        .contains("list missing"));
}

#[tokio::test]
async fn unparseable_entries_are_counted_and_duplicates_collapse() {
    // page 2 adds nothing new -> walk ends; an unparseable entry is dropped.
    let s = server(vec![
        roster_page(vec![member("1", &[]), json!({"x": 1})], None),
        roster_page(vec![member("1", &[])], None),
        roster_page(vec![member("1", &[]), member("2", &[])], None),
        roster_page(vec![member("1", &[])], None),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    assert_eq!(frame(&mut t).await["change"], "joined");
}

#[tokio::test]
async fn events_kind_diffs_and_empty_first_page_primes_an_empty_snapshot() {
    let ev = |id: &str, t: &str| json!({"id": id, "title": t, "time_start": 4_000_000_000u64});
    let s = server(vec![
        env_ok(json!({"events": [ev("e1", "A")]})),
        env_ok(json!({"events": []})),
        env_ok(json!({"events": [ev("e1", "A"), ev("e2", "B")]})),
        env_ok(json!({"events": []})),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Events, &store);
    t.connect().await.unwrap();
    let f = frame(&mut t).await;
    assert_eq!(f["kind"], "events");
    assert_eq!(f["change"], "created");
    assert_eq!(f["event_id"], "e2");
}

#[tokio::test]
async fn unreadable_stored_snapshot_reprimes_without_emitting() {
    let s = server(vec![
        roster_page(vec![member("1", &[])], Some(1)),
        roster_page(vec![member("1", &[]), member("2", &[])], Some(2)),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    store
        .save_snapshot("k".into(), "not json".into(), Duration::from_secs(1))
        .await
        .unwrap();
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    assert_eq!(
        frame(&mut t).await["change"],
        "joined",
        "first cycle primed silently; second diffed"
    );
}

#[tokio::test]
async fn page_unavailable_abandons_the_cycle() {
    let s = server(vec![
        (204, json!(null)),
        roster_page(vec![member("1", &[])], Some(1)),
        roster_page(vec![member("1", &[]), member("2", &[])], Some(2)),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    assert_eq!(frame(&mut t).await["member_id"], "2");
}

#[tokio::test]
async fn reconnect_discards_in_memory_delivery_state() {
    let s = server(vec![
        roster_page(vec![member("1", &[])], Some(1)),
        roster_page(vec![member("1", &[]), member("2", &[])], Some(2)),
    ])
    .await;
    let store = Arc::new(MemorySpectrumStore::new());
    let mut t = transport(&s, SpectrumKind::Roster, &store);
    t.connect().await.unwrap();
    let _ = frame(&mut t).await;
    t.close("test").await;
    t.connect().await.unwrap();
    assert!(t
        .send_handshake(Handshake {
            bytes: vec![],
            secret_refs: vec![]
        })
        .await
        .is_ok());
    assert_eq!(t.platform(), "spectrum");
}

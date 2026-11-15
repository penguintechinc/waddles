//! Host tests: an in-memory `db-v2` fake (filters, ordering, unique keys, atomic batches with
//! rollback and guards) drives the real tool table end to end.
use super::*;
use std::cmp::Ordering;
use std::sync::Mutex;
use waddle_sdk::storage_v2::{Page, Query as Q};

const ALICE: &str = "11111111-1111-4111-8111-111111111111";
const BOB: &str = "22222222-2222-4222-8222-222222222222";
const NCS: &str = "https://ncs.io/track-one";
const OTHER: &str = "https://example.com/song";

#[derive(Clone)]
struct FRow {
    row: Row,
}

/// In-memory stand-in for one community's tables.
#[derive(Default)]
struct FakeDb {
    state: Mutex<(Vec<FRow>, u64)>,
}

/// Total order over comparable values.
fn cmp(a: &Db, b: &Db) -> Option<Ordering> {
    match (a, b) {
        (Db::Int(x), Db::Int(y)) => Some(x.cmp(y)),
        (Db::Text(x), Db::Text(y)) => Some(x.cmp(y)),
        _ => None,
    }
}

/// Evaluates one condition against a row.
fn matches(r: &Row, c: &Cond) -> bool {
    let v = |col: &str| r.get(col).cloned().unwrap_or(Db::Null);
    match c {
        Cond::Eq(col, x) => v(col) == *x,
        Cond::Ne(col, x) => v(col) != *x,
        Cond::Lt(col, x) => cmp(&v(col), x) == Some(Ordering::Less),
        Cond::Le(col, x) => matches!(cmp(&v(col), x), Some(Ordering::Less | Ordering::Equal)),
        Cond::Gt(col, x) => cmp(&v(col), x) == Some(Ordering::Greater),
        Cond::Ge(col, x) => matches!(cmp(&v(col), x), Some(Ordering::Greater | Ordering::Equal)),
        Cond::In(col, xs) => xs.contains(&v(col)),
        Cond::Prefix(col, p) => matches!(v(col), Db::Text(t) if t.starts_with(p.as_str())),
        Cond::IsNull(col) => v(col) == Db::Null,
        Cond::IsNotNull(col) => v(col) != Db::Null,
    }
}

const UNIQUE: [(&str, &str); 3] = [
    ("settings", "skey"),
    ("artist_plays", "artist_key"),
    ("song_plays", "song_key"),
];

impl FakeDb {
    /// Applies one op to `rows`, returning its result or a conflict.
    fn apply(rows: &mut Vec<FRow>, ctr: &mut u64, op: &TxOp) -> Result<TxResult, DbV2Error> {
        let conflict = |m: &str| DbV2Error::Conflict(m.into());
        match op {
            TxOp::Insert { table, columns } => {
                for (t, u) in UNIQUE {
                    if t == table {
                        let key = columns.iter().find(|(c, _)| c == u).map(|(_, v)| v.clone());
                        if rows
                            .iter()
                            .any(|r| r.row.table == *table && r.row.get(u) == key.as_ref())
                        {
                            return Err(conflict("unique"));
                        }
                    }
                }
                *ctr += 1;
                let row = Row {
                    table: table.clone(),
                    row_id: format!("row{ctr}"),
                    version: 1,
                    created_at: String::new(),
                    updated_at: String::new(),
                    columns: columns.clone(),
                };
                rows.push(FRow { row: row.clone() });
                Ok(TxResult::Written(row))
            }
            TxOp::Update {
                table,
                row_id,
                expected_version,
                columns,
            } => {
                let r = rows
                    .iter_mut()
                    .find(|r| r.row.table == *table && r.row.row_id == *row_id)
                    .ok_or(DbV2Error::NotFound)?;
                if r.row.version != *expected_version {
                    return Err(conflict("version"));
                }
                for (c, v) in columns {
                    match r.row.columns.iter_mut().find(|(k, _)| k == c) {
                        Some(slot) => slot.1 = v.clone(),
                        None => r.row.columns.push((c.clone(), v.clone())),
                    }
                }
                r.row.version += 1;
                Ok(TxResult::Written(r.row.clone()))
            }
            TxOp::Delete {
                table,
                row_id,
                expected_version,
            } => {
                let i = rows
                    .iter()
                    .position(|r| r.row.table == *table && r.row.row_id == *row_id)
                    .ok_or(DbV2Error::NotFound)?;
                if rows[i].row.version != *expected_version {
                    return Err(conflict("version"));
                }
                rows.remove(i);
                Ok(TxResult::Done)
            }
            TxOp::GuardVersion {
                table,
                row_id,
                expected_version,
            } => {
                match rows
                    .iter()
                    .find(|r| r.row.table == *table && r.row.row_id == *row_id)
                {
                    Some(r) if r.row.version == *expected_version => Ok(TxResult::Done),
                    _ => Err(conflict("guard")),
                }
            }
            TxOp::GuardCount {
                table,
                filter,
                min,
                max,
            } => {
                let n = rows
                    .iter()
                    .filter(|r| r.row.table == *table && filter.iter().all(|c| matches(&r.row, c)))
                    .count() as u64;
                if n < *min || n > *max {
                    Err(conflict("count"))
                } else {
                    Ok(TxResult::Done)
                }
            }
        }
    }
}

impl DbV2Transport for FakeDb {
    fn insert(&self, t: &str, c: Vec<(String, Db)>) -> Result<Row, DbV2Error> {
        match self
            .transact(&[TxOp::Insert {
                table: t.into(),
                columns: c,
            }])?
            .remove(0)
        {
            TxResult::Written(r) => Ok(r),
            _ => unreachable!(),
        }
    }
    fn get(&self, t: &str, id: &str) -> Result<Row, DbV2Error> {
        let s = self.state.lock().unwrap();
        s.0.iter()
            .find(|r| r.row.table == t && r.row.row_id == id)
            .map(|r| r.row.clone())
            .ok_or(DbV2Error::NotFound)
    }
    fn query(&self, q: &Q) -> Result<Page, DbV2Error> {
        let s = self.state.lock().unwrap();
        let mut rows: Vec<Row> =
            s.0.iter()
                .filter(|r| r.row.table == q.table && q.filter.iter().all(|c| matches(&r.row, c)))
                .map(|r| r.row.clone())
                .collect();
        if let Some((col, desc)) = &q.order {
            rows.sort_by(|a, b| {
                let o = cmp(
                    a.get(col).unwrap_or(&Db::Null),
                    b.get(col).unwrap_or(&Db::Null),
                )
                .unwrap_or(Ordering::Equal);
                if *desc { o.reverse() } else { o }
            });
        }
        let start: usize = q
            .cursor
            .as_deref()
            .and_then(|c| c.parse().ok())
            .unwrap_or(0);
        let end = (start + q.limit as usize).min(rows.len());
        let next = (end < rows.len()).then(|| end.to_string());
        Ok(Page {
            rows: rows[start..end].to_vec(),
            next_cursor: next,
        })
    }
    fn count(&self, t: &str, f: &[Cond]) -> Result<u64, DbV2Error> {
        let s = self.state.lock().unwrap();
        Ok(s.0
            .iter()
            .filter(|r| r.row.table == t && f.iter().all(|c| matches(&r.row, c)))
            .count() as u64)
    }
    fn update(&self, t: &str, id: &str, v: u64, c: Vec<(String, Db)>) -> Result<Row, DbV2Error> {
        match self
            .transact(&[TxOp::Update {
                table: t.into(),
                row_id: id.into(),
                expected_version: v,
                columns: c,
            }])?
            .remove(0)
        {
            TxResult::Written(r) => Ok(r),
            _ => unreachable!(),
        }
    }
    fn delete(&self, t: &str, id: &str, v: u64) -> Result<(), DbV2Error> {
        self.transact(&[TxOp::Delete {
            table: t.into(),
            row_id: id.into(),
            expected_version: v,
        }])
        .map(|_| ())
    }
    fn transact(&self, ops: &[TxOp]) -> Result<Vec<TxResult>, DbV2Error> {
        let mut s = self.state.lock().unwrap();
        let mut rows = s.0.clone();
        let mut ctr = s.1;
        let mut out = Vec::new();
        for op in ops {
            out.push(Self::apply(&mut rows, &mut ctr, op)?);
        }
        *s = (rows, ctr);
        Ok(out)
    }
}

/// Settable clock.
struct TestClock(Mutex<u64>);
impl Clock for TestClock {
    fn now_millis(&self) -> u64 {
        let mut t = self.0.lock().unwrap();
        *t += 1;
        *t
    }
}

/// Fixed platform context.
struct TestPlatform(i32, Option<String>);
impl Platform for TestPlatform {
    fn community_id(&self) -> i32 {
        self.0
    }
    fn web_base_url(&self) -> Option<String> {
        self.1.clone()
    }
}

/// One community's registry plus a handle to its clock.
struct Rig {
    reg: ToolRegistry,
    clock: Arc<TestClock>,
}

/// Builds a rig for community `cid` with an optional web base URL.
fn rig_with(cid: i32, base: Option<&str>) -> Rig {
    let clock = Arc::new(TestClock(Mutex::new(1_800_000_000_000)));
    let svc = Service::new(
        Arc::new(FakeDb::default()),
        clock.clone(),
        Arc::new(TestPlatform(cid, base.map(str::to_string))),
    );
    Rig {
        reg: build_registry(Arc::new(svc)),
        clock,
    }
}

fn rig() -> Rig {
    rig_with(7, Some("https://app.example.org/"))
}

/// A context for `actor` with `scopes` and a fresh idempotency key.
fn ctx(actor: Option<&str>, scopes: &[&str]) -> ToolContext {
    ToolContext {
        idempotency_key: Some("k1".into()),
        actor: actor.map(str::to_string),
        actor_scopes: scopes.iter().map(|s| s.to_string()).collect(),
    }
}

fn member(a: &str) -> ToolContext {
    ctx(Some(a), &[SCOPE_READ, SCOPE_WRITE])
}

fn admin() -> ToolContext {
    ctx(
        Some(BOB),
        &[
            SCOPE_READ,
            SCOPE_WRITE,
            SCOPE_MANAGE,
            SCOPE_DJ_MANAGE,
            SCOPE_SETTINGS_WRITE,
            SCOPE_STATS_READ,
        ],
    )
}

/// Test-only: encodes the host-injected idempotency key and `__ctx` into the args the way
/// the host does, then dispatches through the real registry.
trait CallWith {
    fn call_with(&self, c: &ToolContext, name: &str, args: &[u8]) -> Result<Vec<u8>, ToolError>;
}

impl CallWith for ToolRegistry {
    fn call_with(&self, c: &ToolContext, name: &str, args: &[u8]) -> Result<Vec<u8>, ToolError> {
        let mut v: Value = serde_json::from_slice(args).unwrap();
        if let Some(o) = v.as_object_mut() {
            if let Some(k) = &c.idempotency_key {
                o.insert(waddle_sdk::tool::IDEMPOTENCY_ARG.into(), json!(k));
            }
            o.insert(
                waddle_sdk::tool::CTX_ARG.into(),
                json!({"actor": c.actor, "scopes": c.actor_scopes}),
            );
        }
        self.call(name, &serde_json::to_vec(&v).unwrap())
    }
}

impl Rig {
    fn call(&self, c: &ToolContext, name: &str, args: Value) -> Result<Value, ToolError> {
        self.reg
            .call_with(c, name, &serde_json::to_vec(&args).unwrap())
            .map(|b| serde_json::from_slice(&b).unwrap())
    }
    fn ok(&self, c: &ToolContext, name: &str, args: Value) -> Value {
        self.call(c, name, args)
            .unwrap_or_else(|e| panic!("{name}: {e:?}"))
    }
    fn err(&self, c: &ToolContext, name: &str, args: Value) -> String {
        match self.call(c, name, args) {
            Err(ToolError::InvalidArgs(m)) => m,
            Err(e) => format!("{e:?}"),
            Ok(v) => panic!("expected error, got {v}"),
        }
    }
    fn add(&self, who: &str, title: &str) -> Value {
        self.ok(
            &member(who),
            "requests.add",
            json!({"title": title, "artist": "A", "duration_ms": 1000}),
        )
    }
}

#[test]
fn rfc3339_roundtrips_and_parses_offsets() {
    for ms in [0u64, 1_800_000_123_456, 951_782_400_000] {
        assert_eq!(parse_rfc3339(&rfc3339(ms)), Some(ms));
    }
    assert_eq!(parse_rfc3339("1970-01-01T00:00:01Z"), Some(1000));
    assert_eq!(parse_rfc3339("1970-01-01T01:00:00.5+01:00"), Some(500));
    assert_eq!(parse_rfc3339("1970-01-01T00:00:00.123456Z"), Some(123));
    assert_eq!(parse_rfc3339("garbage"), None);
    assert_eq!(parse_rfc3339("1970-13-01T00:00:00Z"), None);
}

#[test]
fn sha256_matches_known_vector() {
    let d = sha256(b"abc");
    let hex: String = d.iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(
        hex,
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    );
    assert_eq!(song_key("A", "B"), song_key("a", "b"));
    assert_ne!(song_key("a", "b"), song_key("b", "a"));
}

#[test]
fn url_validation_is_https_only_and_strict() {
    for ok in [
        "",
        "https://example.com/x?y=1",
        "HTTPS://Example.com:8443/x",
    ] {
        assert!(validate_url(ok).is_ok(), "{ok}");
    }
    for bad in [
        "http://example.com/x",
        "javascript:alert(1)",
        "ftp://example.com",
        "https://",
        "https://nodot/x",
        "https://user@example.com/x",
        "https://ncs.io@evil.com/x",
        "https://exa mple.com",
        "https://example.com/\u{e9}",
        "https://example.com:abc/x",
        "https://example.com\\x",
    ] {
        assert!(validate_url(bad).is_err(), "{bad}");
    }
    assert!(validate_url(&format!("https://example.com/{}", "a".repeat(500))).is_err());
}

#[test]
fn provider_is_derived_from_host_only() {
    assert_eq!(provider_for("https://ncs.io/x"), "ncs");
    assert_eq!(provider_for("https://www.ncs.io/x"), "ncs");
    assert_eq!(provider_for("https://ncs.lnk.to/x"), "ncs");
    assert_eq!(
        provider_for("https://soundcloud.com/nocopyrightsounds/x"),
        "ncs"
    );
    assert_eq!(provider_for("https://soundcloud.com/someone/x"), "other");
    assert_eq!(provider_for("https://pixabay.com/music/x"), "rfm");
    assert_eq!(provider_for("https://pixabay.com/photos/x"), "other");
    assert_eq!(provider_for("https://ncs.io.evil.com/x"), "other");
    assert_eq!(provider_for("https://evil.com/ncs.io"), "other");
    assert_eq!(provider_for("https://www.youtube.com/watch?v=1"), "other");
    assert_eq!(provider_for(""), "other");
    assert_eq!(provider_for("http://ncs.io/x"), "other");
}

#[test]
fn add_list_and_never_exposes_requester() {
    let r = rig();
    let out = r.add(ALICE, "Song One");
    assert_eq!(out["position"], 1);
    assert_eq!(out["item"]["kind"], "viewer");
    let list = r.ok(&member(ALICE), "requests.list", json!({}));
    assert_eq!(list["total_queued"], 1);
    assert!(list["now_playing"].is_null());
    let text = list.to_string() + &out.to_string();
    assert!(!text.contains(ALICE) && !text.contains("requester"));
}

#[test]
fn add_requires_actor_key_and_rejects_unknown_fields() {
    let r = rig();
    let a = json!({"title": "T"});
    assert!(matches!(
        r.call(&ctx(None, &[]), "requests.add", a.clone()),
        Err(ToolError::Forbidden)
    ));
    let mut nokey = member(ALICE);
    nokey.idempotency_key = None;
    assert_eq!(r.err(&nokey, "requests.add", a), "idempotency key required");
    for extra in ["requester", "actor", "user", "provider"] {
        let mut args = json!({"title": "T"});
        args[extra] = json!(ALICE);
        assert_eq!(r.err(&member(ALICE), "requests.add", args), "unknown field");
    }
    assert_eq!(
        r.err(&member(ALICE), "requests.add", json!({"title": "  "})),
        "invalid title"
    );
    assert_eq!(
        r.err(
            &member(ALICE),
            "requests.add",
            json!({"title": "T", "source_url": "http://x.com/a"})
        ),
        "invalid_url"
    );
    assert_eq!(
        r.err(
            &member(ALICE),
            "requests.add",
            json!({"title": "T", "kind": "imported"})
        ),
        "invalid kind"
    );
}

#[test]
fn per_user_queue_and_duplicate_caps() {
    let r = rig();
    for i in 0..3 {
        r.add(ALICE, &format!("S{i}"));
    }
    assert_eq!(
        r.err(&member(ALICE), "requests.add", json!({"title": "S9"})),
        "per_user_limit"
    );
    assert_eq!(
        r.err(
            &member(BOB),
            "requests.add",
            json!({"title": "s1", "artist": "a"})
        ),
        "duplicate"
    );
    r.ok(&admin(), "config.set", json!({"max_queue": 3}));
    assert_eq!(
        r.err(&member(BOB), "requests.add", json!({"title": "Other"})),
        "queue_full"
    );
}

#[test]
fn dj_kind_needs_manage_and_stores_no_requester() {
    let r = rig();
    assert!(matches!(
        r.call(
            &member(ALICE),
            "requests.add",
            json!({"title": "D", "kind": "dj"})
        ),
        Err(ToolError::Forbidden)
    ));
    let out = r.ok(
        &admin(),
        "requests.add",
        json!({"title": "D", "kind": "dj"}),
    );
    assert_eq!(out["item"]["kind"], "dj");
    // DJ rows never count against any user's cap and cannot be cancelled by a viewer.
    let id = out["item"]["id"].clone();
    assert_eq!(
        r.ok(&member(BOB), "requests.cancel_own", json!({"id": id}))["removed"],
        false
    );
}

#[test]
fn cancel_own_only_removes_the_actors_rows() {
    let r = rig();
    let a = r.add(ALICE, "Mine")["item"]["id"].clone();
    assert_eq!(
        r.ok(&member(BOB), "requests.cancel_own", json!({"id": a}))["removed"],
        false
    );
    assert_eq!(
        r.ok(&member(ALICE), "requests.cancel_own", json!({"id": a}))["removed"],
        true
    );
    assert_eq!(
        r.ok(&member(ALICE), "requests.cancel_own", json!({"id": a}))["removed"],
        false
    );
}

#[test]
fn remove_needs_manage() {
    let r = rig();
    let id = r.add(ALICE, "X")["item"]["id"].clone();
    assert!(matches!(
        r.call(&member(BOB), "requests.remove", json!({"id": id})),
        Err(ToolError::Forbidden)
    ));
    assert_eq!(
        r.ok(&admin(), "requests.remove", json!({"id": id}))["removed"],
        true
    );
}

#[test]
fn advance_writes_history_and_promotes_next() {
    let r = rig();
    r.add(ALICE, "First");
    r.add(BOB, "Second");
    assert!(matches!(
        r.call(&member(ALICE), "requests.advance", json!({})),
        Err(ToolError::Forbidden)
    ));
    let s1 = r.ok(&admin(), "requests.advance", json!({}));
    assert!(s1["finished"].is_null());
    assert_eq!(s1["now_playing"]["title"], "First");
    let s2 = r.ok(&admin(), "requests.advance", json!({}));
    assert_eq!(s2["finished"]["title"], "First");
    assert_eq!(s2["now_playing"]["title"], "Second");
    let h = r.ok(&member(ALICE), "history.list", json!({}));
    assert_eq!(h["items"].as_array().unwrap().len(), 1);
    assert_eq!(h["items"][0]["title"], "First");
    assert!(!h.to_string().contains(ALICE) && !h.to_string().contains("requester"));
    let s3 = r.ok(&admin(), "requests.advance", json!({}));
    assert!(s3["now_playing"].is_null());
    let s4 = r.ok(&admin(), "requests.advance", json!({}));
    assert!(s4["finished"].is_null() && s4["now_playing"].is_null());
    assert_eq!(
        r.ok(&member(ALICE), "history.list", json!({"limit": 1}))["items"][0]["title"],
        "Second"
    );
}

#[test]
fn advance_due_only_when_elapsed_and_needs_no_privilege() {
    let r = rig();
    r.add(ALICE, "One");
    r.add(BOB, "Two");
    let anyone = member(ALICE);
    // Nothing playing + queue non-empty: starts the first song.
    assert_eq!(
        r.ok(&anyone, "requests.advance_due", json!({}))["now_playing"]["title"],
        "One"
    );
    // Duration is 1000 ms and the test clock ticks 1 ms per read: not elapsed yet.
    let again = r.ok(&anyone, "requests.advance_due", json!({}));
    assert!(again["finished"].is_null());
    assert_eq!(again["now_playing"]["title"], "One");
    *r.clock.0.lock().unwrap() += 5_000;
    let due = r.ok(&anyone, "requests.advance_due", json!({}));
    assert_eq!(due["finished"]["title"], "One");
    assert_eq!(due["now_playing"]["title"], "Two");
    *r.clock.0.lock().unwrap() += 5_000;
    let last = r.ok(&anyone, "requests.advance_due", json!({}));
    assert_eq!(last["finished"]["title"], "Two");
    assert!(last["now_playing"].is_null());
    let idle = r.ok(&anyone, "requests.advance_due", json!({}));
    assert!(idle["finished"].is_null() && idle["now_playing"].is_null());
}

#[test]
fn import_is_one_shot_validated_and_requires_empty_community() {
    let r = rig();
    let items: Vec<Value> = (0..20).map(|i| json!({"title": format!("T{i}"), "artist": "A", "source_url": "", "duration_ms": 100})).collect();
    assert_eq!(
        r.ok(&member(ALICE), "requests.import", json!({"items": items}))["imported"],
        20
    );
    let list = r.ok(&member(ALICE), "requests.list", json!({"limit": 100}));
    assert_eq!(list["total_queued"], 20);
    assert_eq!(list["queue"][0]["title"], "T0");
    assert_eq!(list["queue"][0]["kind"], "imported");
    assert_eq!(list["queue"][19]["title"], "T19");
    assert_eq!(
        r.err(
            &member(ALICE),
            "requests.import",
            json!({"items": [{"title": "Z"}]})
        ),
        "already_imported"
    );

    let r2 = rig();
    r2.add(ALICE, "Existing");
    assert_eq!(
        r2.err(
            &member(ALICE),
            "requests.import",
            json!({"items": [{"title": "Z"}]})
        ),
        "not_empty"
    );

    let r3 = rig();
    let too_many: Vec<Value> = (0..101)
        .map(|i| json!({"title": format!("T{i}")}))
        .collect();
    assert!(
        r3.err(
            &member(ALICE),
            "requests.import",
            json!({"items": too_many})
        )
        .contains("at most 100")
    );
    assert_eq!(
        r3.err(
            &member(ALICE),
            "requests.import",
            json!({"items": [{"title": "Z", "requester": ALICE}]})
        ),
        "unknown field"
    );
    assert_eq!(
        r3.err(
            &member(ALICE),
            "requests.import",
            json!({"items": [{"title": "Z", "source_url": "http://x.io/a"}]})
        ),
        "invalid_url"
    );
}

#[test]
fn config_and_dj_toggle_are_scoped_and_bounded() {
    let r = rig();
    let c = r.ok(&member(ALICE), "config.get", json!({}));
    assert_eq!(
        (c["max_per_user"].clone(), c["max_queue"].clone()),
        (json!(3), json!(100))
    );
    assert_eq!(c["ai_dj_on"], false);
    assert_eq!(c["dmca_safe"], false);
    assert!(matches!(
        r.call(&member(ALICE), "config.set", json!({"max_queue": 5})),
        Err(ToolError::Forbidden)
    ));
    assert_eq!(
        r.err(&admin(), "config.set", json!({"max_queue": 0})),
        "invalid max_queue"
    );
    assert_eq!(
        r.err(&admin(), "config.set", json!({"max_per_user": 51})),
        "invalid max_per_user"
    );
    assert_eq!(
        r.ok(&admin(), "config.set", json!({"max_per_user": 5}))["max_per_user"],
        5
    );
    assert!(matches!(
        r.call(&member(ALICE), "dj.set", json!({"enabled": true})),
        Err(ToolError::Forbidden)
    ));
    assert_eq!(
        r.ok(&admin(), "dj.set", json!({"enabled": true}))["enabled"],
        true
    );
    assert_eq!(r.ok(&member(ALICE), "dj.get", json!({}))["enabled"], true);
    assert_eq!(
        r.ok(&member(ALICE), "requests.list", json!({}))["ai_dj_on"],
        true
    );
    assert_eq!(
        r.err(&admin(), "dj.set", json!({"enabled": "yes"})),
        "enabled must be a boolean"
    );
}

#[test]
fn page_link_uses_base_url_and_numeric_community() {
    let r = rig();
    assert_eq!(
        r.ok(&member(ALICE), "page.link", json!({}))["url"],
        "https://app.example.org/community/7/music"
    );
    let none = rig_with(7, None);
    assert_eq!(
        none.err(&member(ALICE), "page.link", json!({})),
        "link_not_configured"
    );
    let bad = rig_with(7, Some("ftp://x"));
    assert_eq!(
        bad.err(&member(ALICE), "page.link", json!({})),
        "link_not_configured"
    );
    let noscope = rig_with(0, Some("https://a.example"));
    assert_eq!(
        noscope.err(&member(ALICE), "page.link", json!({})),
        "link_not_configured"
    );
    let dev = rig_with(3, Some("http://localhost:8080"));
    assert_eq!(
        dev.ok(&member(ALICE), "page.link", json!({}))["url"],
        "http://localhost:8080/community/3/music"
    );
}

#[test]
fn dmca_safe_mode_refuses_non_safe_including_dj_and_forged_provider() {
    let r = rig();
    assert_eq!(
        r.ok(&admin(), "settings.set", json!({"dmca_safe": true}))["purged"],
        0
    );
    let msg = r.err(
        &member(ALICE),
        "requests.add",
        json!({"title": "X", "source_url": OTHER}),
    );
    assert!(msg.starts_with("not_dmca_safe:") && msg.contains("royalty-free"));
    assert_eq!(outcome_code(&msg), "not_dmca_safe");
    assert!(
        r.err(&member(ALICE), "requests.add", json!({"title": "X"}))
            .starts_with("not_dmca_safe")
    );
    assert!(
        r.err(
            &admin(),
            "requests.add",
            json!({"title": "D", "kind": "dj", "source_url": OTHER})
        )
        .starts_with("not_dmca_safe")
    );
    // A forged provider argument or tag-in-title cannot make a non-safe source safe.
    assert_eq!(
        r.err(
            &member(ALICE),
            "requests.add",
            json!({"title": "X", "source_url": OTHER, "provider": "ncs"})
        ),
        "unknown field"
    );
    assert!(
        r.err(
            &member(ALICE),
            "requests.add",
            json!({"title": "[NCS] X", "artist": "ncs", "source_url": OTHER})
        )
        .starts_with("not_dmca_safe")
    );
    let ok = r.ok(
        &member(ALICE),
        "requests.add",
        json!({"title": "Safe", "source_url": NCS}),
    );
    assert_eq!(ok["item"]["dmca_safe_item"], true);
    assert_eq!(
        r.ok(&member(ALICE), "requests.list", json!({}))["dmca_safe"],
        true
    );
    assert!(
        r.err(
            &member(BOB),
            "requests.import",
            json!({"items": [{"title": "Q", "source_url": OTHER}]})
        )
        .starts_with("not_dmca_safe")
    );
}

#[test]
fn enabling_dmca_safe_purges_queued_non_safe_but_not_now_playing() {
    let r = rig();
    r.ok(
        &member(ALICE),
        "requests.add",
        json!({"title": "Playing", "source_url": OTHER}),
    );
    r.ok(&admin(), "requests.advance", json!({}));
    r.ok(
        &member(ALICE),
        "requests.add",
        json!({"title": "Bad1", "source_url": "https://example.com/b1"}),
    );
    r.ok(&member(ALICE), "requests.add", json!({"title": "Bad2"}));
    r.ok(
        &member(BOB),
        "requests.add",
        json!({"title": "Good", "source_url": "https://ncs.lnk.to/g"}),
    );
    assert!(matches!(
        r.call(&member(ALICE), "settings.set", json!({"dmca_safe": true})),
        Err(ToolError::Forbidden)
    ));
    let out = r.ok(&admin(), "settings.set", json!({"dmca_safe": true}));
    assert_eq!(out["purged"], 2);
    let list = r.ok(&member(ALICE), "requests.list", json!({}));
    assert_eq!(list["total_queued"], 1);
    assert_eq!(list["queue"][0]["title"], "Good");
    assert_eq!(list["now_playing"]["title"], "Playing");
    assert_eq!(list["now_playing"]["dmca_safe_item"], false);
    // Disabling never purges and never re-adds.
    assert_eq!(
        r.ok(&admin(), "settings.set", json!({"dmca_safe": false}))["purged"],
        0
    );
    assert_eq!(
        r.err(&admin(), "settings.set", json!({"dmca_safe": 1})),
        "dmca_safe must be a boolean"
    );
    assert_eq!(
        r.err(&admin(), "settings.set", json!({"dmca_safe": true, "x": 1})),
        "unknown field"
    );
}

#[test]
fn history_exposes_dmca_safe_flag_per_item() {
    let r = rig();
    r.ok(
        &member(ALICE),
        "requests.add",
        json!({"title": "S", "source_url": NCS}),
    );
    r.ok(
        &member(BOB),
        "requests.add",
        json!({"title": "O", "source_url": OTHER}),
    );
    for _ in 0..3 {
        r.ok(&admin(), "requests.advance", json!({}));
    }
    let h = r.ok(&member(ALICE), "history.list", json!({"limit": 10}));
    let items = h["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(items[0]["title"], "O");
    assert_eq!(items[0]["dmca_safe_item"], false);
    assert_eq!(items[1]["dmca_safe_item"], true);
}

#[test]
fn play_counts_increment_atomically_and_are_community_scoped() {
    let a = rig_with(1, None);
    let b = rig_with(2, None);
    let stats = admin();
    for round in 0..3 {
        for rg in [&a, &b] {
            if rg
                .reg
                .call_with(
                    &member(ALICE),
                    "requests.add",
                    br#"{"title":"Hit","artist":"Band"}"#,
                )
                .is_err()
            {
                panic!("add failed");
            }
            rg.ok(&admin(), "requests.advance", json!({}));
            rg.ok(&admin(), "requests.advance", json!({}));
            let _ = round;
        }
    }
    // Community B plays an extra distinct song once.
    b.ok(
        &member(ALICE),
        "requests.add",
        json!({"title": "Solo", "artist": "Other"}),
    );
    b.ok(&admin(), "requests.advance", json!({}));
    b.ok(&admin(), "requests.advance", json!({}));
    let top_a = a.ok(&stats, "music.stats.top", json!({"kind": "artist"}));
    assert_eq!(top_a["items"].as_array().unwrap().len(), 1);
    assert_eq!(top_a["items"][0]["artist"], "Band");
    assert_eq!(top_a["items"][0]["plays"], 3);
    let songs_a = a.ok(
        &stats,
        "music.stats.top",
        json!({"kind": "song", "limit": 5}),
    );
    assert_eq!(songs_a["items"][0]["title"], "Hit");
    assert_eq!(songs_a["items"][0]["plays"], 3);
    let top_b = b.ok(&stats, "music.stats.top", json!({"kind": "artist"}));
    assert_eq!(top_b["items"].as_array().unwrap().len(), 2);
    assert_eq!(top_b["items"][0]["plays"], 3);
    assert_eq!(top_b["items"][1]["plays"], 1);
    let text = top_b.to_string();
    assert!(!text.contains(ALICE) && !text.contains("requester") && !text.contains("song_key"));
    assert_eq!(
        a.err(&stats, "music.stats.top", json!({"kind": "genre"})),
        "kind must be artist or song"
    );
    assert_eq!(
        a.err(
            &stats,
            "music.stats.top",
            json!({"kind": "song", "limit": 26})
        ),
        "invalid limit"
    );
}

#[test]
fn failed_advance_batch_rolls_back_history_and_counts() {
    // A stale version aborts the whole batch: nothing is half-written.
    let db = Arc::new(FakeDb::default());
    let svc = Service::new(
        db.clone(),
        Arc::new(TestClock(Mutex::new(1_800_000_000_000))),
        Arc::new(TestPlatform(1, None)),
    );
    let reg = build_registry(Arc::new(svc));
    reg.call_with(
        &member(ALICE),
        "requests.add",
        br#"{"title":"T","artist":"A"}"#,
    )
    .unwrap();
    let before = db.state.lock().unwrap().0.len();
    let bad = db.transact(&[
        TxOp::Insert {
            table: "history".into(),
            columns: vec![("title".into(), "T".into())],
        },
        TxOp::Delete {
            table: "requests".into(),
            row_id: "nope".into(),
            expected_version: 1,
        },
    ]);
    assert!(bad.is_err());
    assert_eq!(db.state.lock().unwrap().0.len(), before);
}

#[test]
fn unknown_tool_and_non_object_args_fail_loud() {
    let r = rig();
    assert!(r.reg.call_with(&member(ALICE), "nope", b"{}").is_err());
    assert_eq!(
        r.err(&member(ALICE), "requests.list", json!([1])),
        "args must be an object"
    );
    assert_eq!(
        r.err(&member(ALICE), "requests.list", json!({"limit": 0})),
        "invalid limit"
    );
    assert_eq!(
        r.err(&member(ALICE), "requests.list", json!({"limit": 101})),
        "invalid limit"
    );
    assert_eq!(tool_log_label("requests.add"), "requests.add");
    assert_eq!(tool_log_label("attacker\nname"), "unknown");
    assert_eq!(outcome_code("something with ALICE pii"), "invalid_args");
}

#[test]
fn every_exported_tool_is_registered() {
    let r = rig();
    for t in TOOLS {
        let res = r.reg.call_with(&admin(), t, b"{}");
        if let Err(ToolError::InvalidArgs(m)) = &res {
            assert_ne!(m, "unknown tool", "{t}");
        }
    }
}

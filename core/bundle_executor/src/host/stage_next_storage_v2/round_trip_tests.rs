//! Real host-call round trips for the `kv-v2` and `db-v2` imports: each call crosses the actual
//! frame codec to a fake stage that answers once, so the encoded args and the decoded result
//! are both pinned -- never a fabricated return value.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use super::*;
use crate::host::imports::tests::{one_shot_bridge_capturing, state_with};
use penguin_bundle_host::wire::HostResultError;

fn row_json(id: &str, version: u64) -> Value {
    json!({
        "table": "t", "row_id": id, "version": version,
        "created_at": "c", "updated_at": "u", "columns": {"a": 1}
    })
}

fn cv(c: &str, v: db_v2::Value) -> db_v2::ColumnValue {
    db_v2::ColumnValue {
        column: c.to_string(),
        value: v,
    }
}

#[tokio::test]
async fn kv_get_versioned_decodes_present_absent_and_malformed() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"value": [1, 2], "version": 4})));
    let mut st = state_with(bridge);
    let got = kv_v2::Host::get_versioned(&mut st, "k".into())
        .await
        .unwrap()
        .expect("present");
    assert_eq!((got.value, got.version), (vec![1, 2], 4));
    assert_eq!(args.await.unwrap(), json!({"key": "k"}));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"value": null})));
    let mut st = state_with(bridge);
    assert!(kv_v2::Host::get_versioned(&mut st, "k".into())
        .await
        .unwrap()
        .is_none());

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    assert!(kv_v2::Host::get_versioned(&mut st, "k".into())
        .await
        .unwrap()
        .is_none());

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"value": "not-bytes", "version": 1})));
    let mut st = state_with(bridge);
    let e = kv_v2::Host::get_versioned(&mut st, "k".into())
        .await
        .unwrap_err();
    assert!(matches!(e, kv_v2::Error::Backend(m) if m.contains("value")));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"value": [1]})));
    let mut st = state_with(bridge);
    let e = kv_v2::Host::get_versioned(&mut st, "k".into())
        .await
        .unwrap_err();
    assert!(matches!(e, kv_v2::Error::Backend(m) if m.contains("version")));
}

#[tokio::test]
async fn kv_cas_reports_swapped_conflict_and_malformed() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"swapped": true, "version": 5})));
    let mut st = state_with(bridge);
    let out = kv_v2::Host::cas(&mut st, "k".into(), 4, vec![9], 60)
        .await
        .unwrap();
    assert!(matches!(out, kv_v2::CasResult::Swapped(5)));
    assert_eq!(
        args.await.unwrap(),
        json!({"key": "k", "expected_version": 4, "value": [9], "ttl_seconds": 60})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"swapped": false, "version": 7})));
    let mut st = state_with(bridge);
    let out = kv_v2::Host::cas(&mut st, "k".into(), 4, vec![9], 0)
        .await
        .unwrap();
    assert!(matches!(out, kv_v2::CasResult::Conflict(7)));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"version": 7})));
    let mut st = state_with(bridge);
    let e = kv_v2::Host::cas(&mut st, "k".into(), 4, vec![9], 0)
        .await
        .unwrap_err();
    assert!(matches!(e, kv_v2::Error::Backend(m) if m.contains("swapped")));
}

#[tokio::test]
async fn kv_delete_if_and_list_keys_round_trip() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"deleted": true})));
    let mut st = state_with(bridge);
    let out = kv_v2::Host::delete_if(&mut st, "k".into(), 3)
        .await
        .unwrap();
    assert!(matches!(out, kv_v2::DeleteResult::Deleted));
    assert_eq!(
        args.await.unwrap(),
        json!({"key": "k", "expected_version": 3})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"deleted": false, "version": 8})));
    let mut st = state_with(bridge);
    let out = kv_v2::Host::delete_if(&mut st, "k".into(), 3)
        .await
        .unwrap();
    assert!(matches!(out, kv_v2::DeleteResult::Conflict(8)));

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    assert!(kv_v2::Host::delete_if(&mut st, "k".into(), 3)
        .await
        .is_err());

    let (bridge, args) =
        one_shot_bridge_capturing(Ok(json!({"keys": ["p.a", "p.b"], "next_cursor": "c2"})));
    let mut st = state_with(bridge);
    let page = kv_v2::Host::list_keys(&mut st, "p.".into(), Some("c1".into()), 10)
        .await
        .unwrap();
    assert_eq!(page.keys, vec!["p.a".to_string(), "p.b".to_string()]);
    assert_eq!(page.next_cursor.as_deref(), Some("c2"));
    assert_eq!(
        args.await.unwrap(),
        json!({"prefix": "p.", "cursor": "c1", "limit": 10})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"keys": [], "next_cursor": null})));
    let mut st = state_with(bridge);
    let page = kv_v2::Host::list_keys(&mut st, "p.".into(), None, 10)
        .await
        .unwrap();
    assert!(page.keys.is_empty() && page.next_cursor.is_none());

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"keys": [], "next_cursor": 5})));
    let mut st = state_with(bridge);
    assert!(kv_v2::Host::list_keys(&mut st, "p.".into(), None, 10)
        .await
        .is_err());

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"keys": "nope"})));
    let mut st = state_with(bridge);
    assert!(kv_v2::Host::list_keys(&mut st, "p.".into(), None, 10)
        .await
        .is_err());
}

#[tokio::test]
async fn kv_refusals_map_onto_the_wit_variants() {
    let refusal = HostResultError {
        code: "invalid_key".to_string(),
        message: "bad key".to_string(),
    };
    let (bridge, _a) = one_shot_bridge_capturing(Err(refusal));
    let mut st = state_with(bridge);
    let e = kv_v2::Host::get_versioned(&mut st, "k".into())
        .await
        .unwrap_err();
    assert!(matches!(e, kv_v2::Error::Invalid(m) if m == "bad key"));
}

#[tokio::test]
async fn db_insert_get_update_delete_round_trip() {
    let (bridge, args) = one_shot_bridge_capturing(Ok(row_json("r1", 1)));
    let mut st = state_with(bridge);
    let row = db_v2::Host::insert(
        &mut st,
        "t".into(),
        vec![cv("a", db_v2::Value::IntValue(1))],
    )
    .await
    .unwrap();
    assert_eq!((row.row_id.as_str(), row.version), ("r1", 1));
    assert_eq!(
        args.await.unwrap(),
        json!({"table": "t", "columns": {"a": 1}})
    );

    let (bridge, args) = one_shot_bridge_capturing(Ok(row_json("r1", 1)));
    let mut st = state_with(bridge);
    let row = db_v2::Host::get(&mut st, "t".into(), "r1".into())
        .await
        .unwrap();
    assert_eq!(row.columns.len(), 1);
    assert_eq!(args.await.unwrap(), json!({"table": "t", "row_id": "r1"}));

    let (bridge, args) = one_shot_bridge_capturing(Ok(row_json("r1", 2)));
    let mut st = state_with(bridge);
    let row = db_v2::Host::update(
        &mut st,
        "t".into(),
        "r1".into(),
        1,
        vec![cv("a", db_v2::Value::TextValue("x".into()))],
    )
    .await
    .unwrap();
    assert_eq!(row.version, 2);
    assert_eq!(
        args.await.unwrap(),
        json!({"table": "t", "row_id": "r1", "expected_version": 1, "columns": {"a": "x"}})
    );

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    db_v2::Host::delete(&mut st, "t".into(), "r1".into(), 2)
        .await
        .unwrap();
    assert_eq!(
        args.await.unwrap(),
        json!({"table": "t", "row_id": "r1", "expected_version": 2})
    );

    let conflict = HostResultError {
        code: "conflict".to_string(),
        message: "stale".to_string(),
    };
    let (bridge, _a) = one_shot_bridge_capturing(Err(conflict));
    let mut st = state_with(bridge);
    let e = db_v2::Host::delete(&mut st, "t".into(), "r1".into(), 1)
        .await
        .unwrap_err();
    assert!(matches!(e, db_v2::Error::Conflict(_)));

    // A value the wire cannot carry fails loud BEFORE any host call.
    let mut st = ExecState::new(None, "waddles.test.app".to_string(), 1);
    let e = db_v2::Host::insert(
        &mut st,
        "t".into(),
        vec![cv("a", db_v2::Value::FloatValue(1.5))],
    )
    .await
    .unwrap_err();
    assert!(matches!(e, db_v2::Error::InvalidValue(_)));
}

#[tokio::test]
async fn db_query_and_count_round_trip() {
    let page = json!({"rows": [row_json("r1", 1), row_json("r2", 3)], "next_cursor": "c9"});
    let (bridge, args) = one_shot_bridge_capturing(Ok(page));
    let mut st = state_with(bridge);
    let out = db_v2::Host::query(
        &mut st,
        db_v2::QueryRequest {
            table: "t".into(),
            filter: vec![db_v2::Condition::Eq(cv("a", db_v2::Value::IntValue(1)))],
            order: Some(db_v2::Order {
                column: "version".into(),
                descending: true,
            }),
            limit: 25,
            cursor: None,
        },
    )
    .await
    .unwrap();
    assert_eq!(out.rows.len(), 2);
    assert_eq!(out.next_cursor.as_deref(), Some("c9"));
    assert_eq!(
        args.await.unwrap(),
        json!({
            "table": "t",
            "filter": [{"op": "eq", "column": "a", "value": 1}],
            "order": {"column": "version", "descending": true},
            "limit": 25, "cursor": null
        })
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"rows": []})));
    let mut st = state_with(bridge);
    let out = db_v2::Host::query(
        &mut st,
        db_v2::QueryRequest {
            table: "t".into(),
            filter: vec![],
            order: None,
            limit: 1,
            cursor: Some("c".into()),
        },
    )
    .await
    .unwrap();
    assert!(out.rows.is_empty() && out.next_cursor.is_none());

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({"rows": [], "next_cursor": 1})));
    let mut st = state_with(bridge);
    let e = db_v2::Host::query(
        &mut st,
        db_v2::QueryRequest {
            table: "t".into(),
            filter: vec![],
            order: None,
            limit: 1,
            cursor: None,
        },
    )
    .await
    .unwrap_err();
    assert!(matches!(e, db_v2::Error::Backend(_)));

    let (bridge, args) = one_shot_bridge_capturing(Ok(json!({"count": 12})));
    let mut st = state_with(bridge);
    let n = db_v2::Host::count(
        &mut st,
        "t".into(),
        vec![db_v2::Condition::IsNotNull("a".into())],
    )
    .await
    .unwrap();
    assert_eq!(n, 12);
    assert_eq!(
        args.await.unwrap(),
        json!({"table": "t", "filter": [{"op": "is_not_null", "column": "a"}]})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    assert!(db_v2::Host::count(&mut st, "t".into(), vec![])
        .await
        .is_err());
}

#[tokio::test]
async fn db_transact_decodes_written_and_done_results() {
    let (bridge, args) =
        one_shot_bridge_capturing(Ok(json!({"results": [row_json("r1", 1), null]})));
    let mut st = state_with(bridge);
    let out = db_v2::Host::transact(
        &mut st,
        vec![
            db_v2::TxOp::Insert(db_v2::InsertOp {
                table: "t".into(),
                columns: vec![cv("a", db_v2::Value::IntValue(1))],
            }),
            db_v2::TxOp::GuardVersion(db_v2::RowVersionOp {
                table: "t".into(),
                row_id: "r0".into(),
                expected_version: 2,
            }),
        ],
    )
    .await
    .unwrap();
    assert_eq!(out.len(), 2);
    assert!(matches!(&out[0], db_v2::TxResult::Written(r) if r.row_id == "r1"));
    assert!(matches!(out[1], db_v2::TxResult::Done));
    assert_eq!(
        args.await.unwrap(),
        json!({"ops": [
            {"op": "insert", "table": "t", "columns": {"a": 1}},
            {"op": "guard_version", "table": "t", "row_id": "r0", "expected_version": 2}
        ]})
    );

    let (bridge, _a) = one_shot_bridge_capturing(Ok(json!({})));
    let mut st = state_with(bridge);
    let e = db_v2::Host::transact(&mut st, vec![]).await.unwrap_err();
    assert!(matches!(e, db_v2::Error::Backend(_)));
}

#[test]
fn every_tx_op_and_condition_kind_encodes() {
    let upd = db_v2::TxOp::Update(db_v2::UpdateOp {
        table: "t".into(),
        row_id: "r".into(),
        expected_version: 1,
        columns: vec![cv("a", db_v2::Value::BoolValue(true))],
    });
    assert_eq!(
        tx_op_json(&upd).unwrap(),
        json!({"op": "update", "table": "t", "row_id": "r", "expected_version": 1,
               "columns": {"a": true}})
    );
    for (cond, op) in [
        (db_v2::Condition::Ne(cv("a", db_v2::Value::NullValue)), "ne"),
        (
            db_v2::Condition::Lt(cv("a", db_v2::Value::IntValue(1))),
            "lt",
        ),
        (
            db_v2::Condition::Le(cv("a", db_v2::Value::IntValue(1))),
            "le",
        ),
        (
            db_v2::Condition::Gt(cv("a", db_v2::Value::IntValue(1))),
            "gt",
        ),
    ] {
        assert_eq!(condition_json(&cond).unwrap()["op"], op);
    }
    assert!(
        matches!(json_value(&json!(1.5)), db_v2::Value::NullValue),
        "a non-integer number is not representable and decodes as null"
    );
}

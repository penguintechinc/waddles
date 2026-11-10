//! Docker-free unit tests for [`super`] over scripted `MockDatabase` results
//! (`tests/postgres_v2_integration.rs` covers the real-server paths).

#![allow(clippy::unwrap_used, clippy::expect_used)]
use super::super::ast::{CmpOp, Order};
use super::super::testkit::*;
use super::*;
use sea_orm::DbErr;

fn backend(db: sea_orm::MockDatabase) -> (PostgresV2Backend, DatabaseConnection) {
    let c = conn(db);
    (PostgresV2Backend::new(c.clone()), c)
}

fn sql_log(c: DatabaseConnection) -> Vec<Statement> {
    c.into_transaction_log()
        .into_iter()
        .flat_map(|t| t.statements().to_vec())
        .collect()
}

fn ins(title: &str) -> TxOp {
    TxOp::Insert {
        table: "events".into(),
        columns: vec![
            ("title".into(), DbValue::Text(title.into())),
            (
                "start_at".into(),
                DbValue::Text("2026-03-01T00:00:00Z".into()),
            ),
        ],
    }
}

fn no_rows() -> Vec<MockRow> {
    Vec::new()
}

#[test]
fn sqlstates_map_to_the_v2_error_surface() {
    let code = |c: &'static str| map_db_err(pg_exec_err(c)).code();
    assert_eq!(code("23502"), "invalid_value");
    assert_eq!(code("23505"), "conflict");
    assert_eq!(code("40001"), "conflict");
    assert_eq!(code("40P01"), "conflict");
    assert_eq!(code("57014"), "timeout");
    assert_eq!(code("22003"), "invalid_value");
    assert_eq!(code("XX000"), "backend");
    assert_eq!(map_db_err(pg_query_err("23505")).code(), "conflict");
    assert_eq!(map_db_err(DbErr::Custom("x".into())).code(), "backend");
    let non_db = DbErr::Exec(RuntimeErr::SqlxError(
        sea_orm::sqlx::Error::PoolTimedOut.into(),
    ));
    assert_eq!(map_db_err(non_db).code(), "backend");
}

#[test]
fn helpers_reject_malformed_ids_and_versions() {
    assert!(parse_row_id("nope").is_err());
    assert!(parse_row_id(ROW_ID).is_ok());
    assert_eq!(version_i64(5).unwrap(), 5);
    assert_eq!(version_i64(u64::MAX).unwrap_err().code(), "invalid_value");
}

#[test]
fn annotate_names_the_op_only_in_a_batch() {
    let e = DbV2Error::InvalidTable("t".into());
    assert_eq!(annotate(false, 2, e.clone()), e);
    assert_eq!(
        annotate(true, 2, e),
        DbV2Error::InvalidTable("op 2: t".into())
    );
    for (input, code) in [
        (DbV2Error::InvalidColumn("c".into()), "invalid_column"),
        (DbV2Error::InvalidValue("v".into()), "invalid_value"),
        (DbV2Error::InvalidQuery("q".into()), "invalid_query"),
        (DbV2Error::Conflict("x".into()), "conflict"),
        (DbV2Error::QuotaExceeded("x".into()), "quota_exceeded"),
        (DbV2Error::NotFound, "conflict"),
        (DbV2Error::Timeout, "timeout"),
        (DbV2Error::Backend("b".into()), "backend"),
    ] {
        let out = annotate(true, 1, input);
        assert_eq!(out.code(), code);
        if code != "timeout" && code != "backend" {
            assert!(out.message().starts_with("op 1: "), "{out:?}");
        }
    }
}

#[test]
fn plan_op_validates_before_any_round_trip() {
    let s = schema();
    let code = |op: &TxOp| plan_op(&s, op).err().map(|e| e.code());
    assert_eq!(code(&ins("a")), None);
    let bad_table = TxOp::Delete {
        table: "ghost".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
    };
    assert_eq!(code(&bad_table), Some("invalid_table"));
    let empty_update = TxOp::Update {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
        columns: vec![],
    };
    assert_eq!(code(&empty_update), Some("invalid_value"));
    let bad_col = TxOp::Insert {
        table: "events".into(),
        columns: vec![("ghost".into(), DbValue::Int(1))],
    };
    assert_eq!(code(&bad_col), Some("invalid_column"));
    let ok_update = TxOp::Update {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
        columns: vec![("title".into(), DbValue::Text("x".into()))],
    };
    assert_eq!(code(&ok_update), None);
    let bad_id = TxOp::GuardVersion {
        table: "events".into(),
        row_id: "x".into(),
        expected_version: 1,
    };
    assert_eq!(code(&bad_id), Some("invalid_value"));
    let gv = TxOp::GuardVersion {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
    };
    assert_eq!(code(&gv), None);
    let min_gt_max = TxOp::GuardCount {
        table: "events".into(),
        filter: vec![],
        min: 3,
        max: 1,
    };
    assert_eq!(code(&min_gt_max), Some("invalid_query"));
    let unindexed = TxOp::GuardCount {
        table: "events".into(),
        filter: vec![Condition::IsNull {
            column: "score".into(),
        }],
        min: 0,
        max: 1,
    };
    assert_eq!(code(&unindexed), Some("invalid_query"));
}

#[tokio::test]
async fn get_returns_the_scoped_row_and_binds_scope_from_the_invocation() {
    let (b, c) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([vec![event_row(
                ROW_ID,
                4,
                "Raid",
                "2026-03-01T00:00:00.000000Z",
            )]]),
    );
    let row = b.get(&scope(), &events_def(), ROW_ID).await.unwrap();
    assert_eq!(row.table, "events");
    assert_eq!(row.row_id, ROW_ID);
    assert_eq!(row.version, 4);
    assert_eq!(
        row.columns[0],
        ("title".into(), DbValue::Text("Raid".into()))
    );
    assert_eq!(row.columns[2], ("score".into(), DbValue::Null));
    let log = sql_log(c);
    let select = log
        .iter()
        .find(|s| s.sql.starts_with("SELECT \"row_id\""))
        .unwrap();
    assert!(select.sql.contains("tenant_id = $2 AND community_id = $3"));
    let vals = select.values.as_ref().unwrap().0.clone();
    assert_eq!(vals[1], int(7));
    assert_eq!(vals[2], int(3));
}

#[tokio::test]
async fn get_failure_modes() {
    let (b, _) = backend(mock_db());
    assert_eq!(
        b.get(&scope(), &events_def(), "nope")
            .await
            .unwrap_err()
            .code(),
        "invalid_value"
    );
    let (b, _) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([no_rows()]),
    );
    assert_eq!(
        b.get(&scope(), &events_def(), ROW_ID).await.unwrap_err(),
        DbV2Error::NotFound
    );
    let (b, _) = backend(mock_db().append_exec_errors([DbErr::Custom("boom".into())]));
    assert_eq!(
        b.get(&scope(), &events_def(), ROW_ID)
            .await
            .unwrap_err()
            .code(),
        "backend"
    );
    let (b, _) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_errors([pg_query_err("57014")]),
    );
    assert_eq!(
        b.get(&scope(), &events_def(), ROW_ID).await.unwrap_err(),
        DbV2Error::Timeout
    );
    let (b, _) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([vec![cells(vec![("row_id", uuid(ROW_ID))])]]),
    );
    assert_eq!(
        b.get(&scope(), &events_def(), ROW_ID)
            .await
            .unwrap_err()
            .code(),
        "backend"
    );
}

fn q(order: Option<Order>, limit: u32) -> QueryRequest {
    QueryRequest {
        table: "events".into(),
        filter: vec![],
        order,
        limit,
        cursor: None,
    }
}

#[tokio::test]
async fn query_pages_and_mints_a_cursor_per_order_column() {
    let ids = [ROW_ID, OTHER_ROW_ID, "33333333-3333-4333-8333-333333333333"];
    for col in [
        "row_id",
        "version",
        "created_at",
        "updated_at",
        "start_at",
        "title",
    ] {
        let order = (col != "row_id").then(|| Order {
            column: col.into(),
            descending: false,
        });
        let rows: Vec<MockRow> = ids
            .iter()
            .enumerate()
            .map(|(i, id)| event_row(id, i as i32 + 1, "T", "2026-03-01T00:00:00.000000Z"))
            .collect();
        let (b, _) = backend(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([rows]),
        );
        // platform columns are not index-led, so pin the query to the pk range
        let mut req = q(order, 2);
        req.filter = vec![Condition::Cmp {
            column: "row_id".into(),
            op: CmpOp::Eq,
            value: DbValue::Text(ROW_ID.into()),
        }];
        let page = b.query(&scope(), &events_def(), &req).await.unwrap();
        assert_eq!(page.rows.len(), 2, "{col}");
        assert!(page.next_cursor.is_some(), "{col}");
    }
}

#[tokio::test]
async fn query_last_page_has_no_cursor_and_bad_requests_cost_no_round_trip() {
    let (b, _) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([vec![event_row(
                ROW_ID,
                1,
                "T",
                "2026-03-01T00:00:00.000000Z",
            )]]),
    );
    let page = b.query(&scope(), &events_def(), &q(None, 5)).await.unwrap();
    assert_eq!(page.rows.len(), 1);
    assert!(page.next_cursor.is_none());

    let (b, c) = backend(mock_db());
    let err = b
        .query(&scope(), &events_def(), &q(None, 0))
        .await
        .unwrap_err();
    assert_eq!(err.code(), "invalid_query");
    assert!(sql_log(c).is_empty());

    let (b, _) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_errors([DbErr::Custom("x".into())]),
    );
    assert_eq!(
        b.query(&scope(), &events_def(), &q(None, 5))
            .await
            .unwrap_err()
            .code(),
        "backend"
    );
    let (b, _) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([vec![cells(vec![("row_id", text("not-a-uuid"))])]]),
    );
    assert!(b.query(&scope(), &events_def(), &q(None, 5)).await.is_err());
}

#[tokio::test]
async fn count_runs_a_scoped_indexed_count() {
    let filter = vec![Condition::Cmp {
        column: "start_at".into(),
        op: CmpOp::Ge,
        value: DbValue::Text("2026-01-01T00:00:00Z".into()),
    }];
    let (b, c) = backend(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([count_row(5)]),
    );
    assert_eq!(b.count(&scope(), &events_def(), &filter).await.unwrap(), 5);
    let log = sql_log(c);
    let s = log.iter().find(|s| s.sql.contains("COUNT(*)")).unwrap();
    assert!(s.sql.contains("tenant_id = $1 AND community_id = $2"));

    // unindexed filters are refused before touching the database
    let unindexed = vec![Condition::IsNull {
        column: "score".into(),
    }];
    let (b, _) = backend(mock_db());
    assert_eq!(
        b.count(&scope(), &events_def(), &unindexed)
            .await
            .unwrap_err()
            .code(),
        "invalid_query"
    );
    let cases = vec![
        (
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([no_rows()]),
            "backend",
        ),
        (
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([count_row(-1)]),
            "backend",
        ),
        (
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_errors([pg_query_err("57014")]),
            "timeout",
        ),
    ];
    for (db, code) in cases {
        let (b, _) = backend(db);
        assert_eq!(
            b.count(&scope(), &events_def(), &filter)
                .await
                .unwrap_err()
                .code(),
            code
        );
    }
}

#[tokio::test]
async fn transact_rejects_empty_and_oversized_batches() {
    let (b, _) = backend(mock_db());
    assert_eq!(
        b.transact(&scope(), &schema(), &[])
            .await
            .unwrap_err()
            .code(),
        "invalid_query"
    );
    let too_many: Vec<TxOp> = (0..=MAX_TX_OPS).map(|_| ins("x")).collect();
    assert_eq!(
        b.transact(&scope(), &schema(), &too_many)
            .await
            .unwrap_err()
            .code(),
        "invalid_query"
    );
    let batch = [
        ins("a"),
        TxOp::GuardVersion {
            table: "ghost".into(),
            row_id: ROW_ID.into(),
            expected_version: 1,
        },
    ];
    let err = b.transact(&scope(), &schema(), &batch).await.unwrap_err();
    assert_eq!(
        err,
        DbV2Error::InvalidTable("op 1: \"ghost\" is not a table this bundle declared".into())
    );
}

#[tokio::test]
async fn transact_commits_a_mixed_batch_and_accounts_usage() {
    let ops = [
        ins("new"),
        TxOp::Update {
            table: "events".into(),
            row_id: ROW_ID.into(),
            expected_version: 2,
            columns: vec![("title".into(), DbValue::Text("edit".into()))],
        },
        TxOp::GuardVersion {
            table: "events".into(),
            row_id: ROW_ID.into(),
            expected_version: 3,
        },
        TxOp::GuardCount {
            table: "events".into(),
            filter: vec![],
            min: 0,
            max: 10,
        },
        TxOp::Delete {
            table: "events".into(),
            row_id: OTHER_ROW_ID.into(),
            expected_version: 1,
        },
    ];
    let (b, c) = backend(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([
                vec![event_row_sz(ROW_ID, 1, "new", 100)],
                // update: lock row (version 2, size 100), then RETURNING (size 120)
                vec![cells(vec![("version", int(2)), ("_sz", big(100))])],
                vec![event_row_sz(ROW_ID, 3, "edit", 120)],
                // guard-version
                vec![cells(vec![("version", int(3))])],
                // guard-count (filter-less)
                count_row(4),
                // delete RETURNING size
                vec![cells(vec![("_sz", big(50))])],
                // usage upsert
                usage_row(),
            ]),
    );
    let out = b.transact(&scope(), &schema(), &ops).await.unwrap();
    assert_eq!(out.len(), 5);
    assert!(matches!(out[0], TxOutcome::Row(_)));
    assert!(matches!(out[1], TxOutcome::Row(_)));
    assert_eq!(out[2], TxOutcome::Done);
    assert_eq!(out[4], TxOutcome::Done);
    let log = sql_log(c);
    let usage = log
        .iter()
        .find(|s| s.sql.contains("bundle_data.usage"))
        .unwrap();
    let v = usage.values.as_ref().unwrap().0.clone();
    // rows: +1 -1 = 0; bytes: +100 +20 -50 = 70
    assert_eq!(v[3], big(0));
    assert_eq!(v[4], big(70));
    assert_eq!(log.last().unwrap().sql, "COMMIT");
}

#[tokio::test]
async fn transact_op_failures_roll_back_with_the_right_code() {
    let upd = |v| TxOp::Update {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: v,
        columns: vec![("title".into(), DbValue::Text("e".into()))],
    };
    let del = TxOp::Delete {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
    };
    let gv = TxOp::GuardVersion {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
    };
    let gc = TxOp::GuardCount {
        table: "events".into(),
        filter: vec![],
        min: 1,
        max: 2,
    };
    let run = || mock_db().append_exec_results(exec_ok(3));
    let cases: Vec<(&str, TxOp, sea_orm::MockDatabase, &str)> = vec![
        (
            "insert returns no row",
            ins("a"),
            run().append_query_results([no_rows()]),
            "backend",
        ),
        (
            "insert unique violation",
            ins("a"),
            run().append_query_errors([pg_query_err("23505")]),
            "conflict",
        ),
        (
            "update missing row",
            upd(1),
            run().append_query_results([no_rows()]),
            "not_found",
        ),
        (
            "update version mismatch",
            upd(1),
            run().append_query_results([vec![cells(vec![("version", int(9)), ("_sz", big(10))])]]),
            "conflict",
        ),
        (
            "update lock row lacks size",
            upd(1),
            run().append_query_results([vec![cells(vec![("version", int(1))])]]),
            "backend",
        ),
        (
            "update returns no row",
            upd(1),
            run().append_query_results([
                vec![cells(vec![("version", int(1)), ("_sz", big(10))])],
                no_rows(),
            ]),
            "backend",
        ),
        (
            "delete missing row",
            del.clone(),
            run().append_query_results([no_rows(), no_rows()]),
            "not_found",
        ),
        (
            "delete version mismatch",
            del.clone(),
            run().append_query_results([no_rows(), vec![cells(vec![("present", int(1))])]]),
            "conflict",
        ),
        (
            "delete returning lacks size",
            del.clone(),
            run().append_query_results([vec![cells(vec![("other", int(1))])]]),
            "backend",
        ),
        (
            "guard-version missing",
            gv.clone(),
            run().append_query_results([no_rows()]),
            "not_found",
        ),
        (
            "guard-version mismatch",
            gv.clone(),
            run().append_query_results([vec![cells(vec![("version", int(5))])]]),
            "conflict",
        ),
        (
            "guard-count below min",
            gc.clone(),
            run().append_query_results([count_row(0)]),
            "conflict",
        ),
        (
            "guard-count above max",
            gc.clone(),
            run().append_query_results([count_row(3)]),
            "conflict",
        ),
        (
            "guard-count no row",
            gc.clone(),
            run().append_query_results([no_rows()]),
            "backend",
        ),
        (
            "guard-count negative",
            gc.clone(),
            run().append_query_results([count_row(-2)]),
            "backend",
        ),
        (
            "scope prologue fails",
            gc.clone(),
            mock_db().append_exec_errors([DbErr::Custom("x".into())]),
            "backend",
        ),
        (
            "advisory lock fails",
            gc,
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_exec_errors([DbErr::Custom("x".into())]),
            "backend",
        ),
    ];
    for (name, op, db, code) in cases {
        let (b, _) = backend(db);
        let err = b.transact(&scope(), &schema(), &[op]).await.unwrap_err();
        assert_eq!(err.code(), code, "{name}: {err:?}");
    }
    // an out-of-range delete expected_version never reaches a statement
    let big_v = TxOp::Delete {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: u64::MAX,
    };
    let (b, _) = backend(run());
    assert_eq!(
        b.transact(&scope(), &schema(), &[big_v])
            .await
            .unwrap_err()
            .code(),
        "invalid_value"
    );
}

#[tokio::test]
async fn quota_is_enforced_before_and_by_the_conditional_upsert() {
    let insert_row = || vec![event_row_sz(ROW_ID, 1, "a", 10)];
    // over the cap outright: refused without issuing the upsert
    let c = conn(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([insert_row()]),
    );
    let b = PostgresV2Backend::new(c).with_quota_for_test(0, 1_000);
    assert_eq!(
        b.transact(&scope(), &schema(), &[ins("a")])
            .await
            .unwrap_err()
            .code(),
        "quota_exceeded"
    );
    // under the cap but the upsert affects no row (concurrent usage)
    let c = conn(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([insert_row(), no_rows()]),
    );
    let b = PostgresV2Backend::new(c);
    let err = b
        .transact(&scope(), &schema(), &[ins("a")])
        .await
        .unwrap_err();
    assert_eq!(err.code(), "quota_exceeded");
    // the upsert itself erroring maps by sqlstate
    let c = conn(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([insert_row()])
            .append_query_errors([pg_query_err("57014")]),
    );
    let b = PostgresV2Backend::new(c);
    assert_eq!(
        b.transact(&scope(), &schema(), &[ins("a")])
            .await
            .unwrap_err(),
        DbV2Error::Timeout
    );
    // a batch with no net usage change skips the upsert and commits
    let c = conn(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([vec![cells(vec![("version", int(1))])]]),
    );
    let b = PostgresV2Backend::new(c);
    let gv = TxOp::GuardVersion {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
    };
    assert_eq!(
        b.transact(&scope(), &schema(), &[gv]).await.unwrap(),
        vec![TxOutcome::Done]
    );
}

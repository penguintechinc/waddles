//! Docker-free tests for [`DbV2Host`]: authorization, schema resolution,
//! deadline and error-surface wiring over a scripted `MockDatabase`.

#![allow(clippy::unwrap_used, clippy::expect_used)]
use super::testkit::*;
use super::*;

fn host(db: sea_orm::MockDatabase) -> DbV2Host {
    DbV2Host::new(PostgresV2Backend::new(conn(db)))
}

fn cols(title: &str) -> Vec<(String, DbValue)> {
    vec![
        ("title".into(), DbValue::Text(title.into())),
        (
            "start_at".into(),
            DbValue::Text("2026-03-01T00:00:00Z".into()),
        ),
    ]
}

fn no_rows() -> Vec<MockRow> {
    Vec::new()
}

#[tokio::test(start_paused = true)]
async fn the_call_deadline_fires_as_a_timeout() {
    let r: Result<(), DbV2Error> = with_deadline(std::future::pending()).await;
    assert_eq!(r.unwrap_err(), DbV2Error::Timeout);
    let ok = with_deadline(async { Ok::<_, DbV2Error>(3) })
        .await
        .unwrap();
    assert_eq!(ok, 3);
}

#[tokio::test]
async fn an_ungranted_app_is_denied_and_an_unprovisioned_one_is_invalid_table() {
    let h = host(mock_db());
    let reg = registry();
    let none = CapabilitySnapshot::new();
    let err = h
        .insert(&scope(), &reg, &none, "events", cols("a"))
        .await
        .unwrap_err();
    assert_eq!(err.code(), "denied");

    let empty = SchemaRegistryV2::new();
    let err = h
        .get(&scope(), &empty, &granted_caps(), "events", ROW_ID)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "invalid_table");
}

#[tokio::test]
async fn undeclared_tables_are_refused_on_every_read_path() {
    let h = host(mock_db());
    let (reg, caps) = (registry(), granted_caps());
    let err = h
        .get(&scope(), &reg, &caps, "ghost", ROW_ID)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "invalid_table");
    let req = QueryRequest {
        table: "ghost".into(),
        filter: vec![],
        order: None,
        limit: 1,
        cursor: None,
    };
    assert_eq!(
        h.query(&scope(), &reg, &caps, &req)
            .await
            .unwrap_err()
            .code(),
        "invalid_table"
    );
    assert_eq!(
        h.count(&scope(), &reg, &caps, "ghost", &[])
            .await
            .unwrap_err()
            .code(),
        "invalid_table"
    );
}

#[tokio::test]
async fn insert_update_delete_get_query_count_round_trip() {
    let (reg, caps) = (registry(), granted_caps());
    let usage_ins = || vec![event_row_sz(ROW_ID, 1, "a", 10)];

    let h = host(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([usage_ins(), usage_row()]),
    );
    let row = h
        .insert(&scope(), &reg, &caps, "events", cols("a"))
        .await
        .unwrap();
    assert_eq!(row.version, 1);

    let h = host(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([
                vec![cells(vec![("version", int(1)), ("_sz", big(10))])],
                vec![event_row_sz(ROW_ID, 2, "b", 12)],
                usage_row(),
            ]),
    );
    let row = h
        .update(
            &scope(),
            &reg,
            &caps,
            "events",
            ROW_ID,
            1,
            vec![("title".into(), DbValue::Text("b".into()))],
        )
        .await
        .unwrap();
    assert_eq!(row.version, 2);

    let h = host(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([vec![cells(vec![("_sz", big(10))])], usage_row()]),
    );
    h.delete(&scope(), &reg, &caps, "events", ROW_ID, 2)
        .await
        .unwrap();

    let h = host(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([vec![event_row(
                ROW_ID,
                2,
                "b",
                "2026-03-01T00:00:00.000000Z",
            )]]),
    );
    assert_eq!(
        h.get(&scope(), &reg, &caps, "events", ROW_ID)
            .await
            .unwrap()
            .version,
        2
    );

    let h = host(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([vec![event_row(
                ROW_ID,
                2,
                "b",
                "2026-03-01T00:00:00.000000Z",
            )]]),
    );
    let req = QueryRequest {
        table: "events".into(),
        filter: vec![],
        order: None,
        limit: 10,
        cursor: None,
    };
    let page = h.query(&scope(), &reg, &caps, &req).await.unwrap();
    assert_eq!(page.rows.len(), 1);

    let h = host(
        mock_db()
            .append_exec_results(exec_ok(2))
            .append_query_results([count_row(0)]),
    );
    assert_eq!(
        h.count(&scope(), &reg, &caps, "events", &[]).await.unwrap(),
        0
    );
}

#[tokio::test]
async fn a_write_that_returns_no_row_is_a_backend_error_and_failures_are_classified() {
    let (reg, caps) = (registry(), granted_caps());
    // a single-op batch whose only outcome is `Done` (guard) cannot be a row
    let h = host(mock_db());
    let guard = TxOp::GuardVersion {
        table: "events".into(),
        row_id: ROW_ID.into(),
        expected_version: 1,
    };
    let h2 = host(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([vec![cells(vec![("version", int(1))])]]),
    );
    let out = h2
        .transact(&scope(), &reg, &caps, std::slice::from_ref(&guard))
        .await
        .unwrap();
    assert_eq!(out, vec![TxOutcome::Done]);
    drop(h);

    // quota rejection, backend failure and timeout all surface their codes
    let quota = DbV2Host::new(
        PostgresV2Backend::new(conn(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([vec![event_row_sz(ROW_ID, 1, "a", 10)]]),
        ))
        .with_quota_for_test(0, 100),
    );
    assert_eq!(
        quota
            .insert(&scope(), &reg, &caps, "events", cols("a"))
            .await
            .unwrap_err()
            .code(),
        "quota_exceeded"
    );
    let backend_down = host(mock_db().append_exec_errors([sea_orm::DbErr::Custom("down".into())]));
    assert_eq!(
        backend_down
            .transact(&scope(), &reg, &caps, std::slice::from_ref(&guard))
            .await
            .unwrap_err()
            .code(),
        "backend"
    );
    let timed_out = host(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_errors([pg_query_err("57014")]),
    );
    assert_eq!(
        timed_out
            .transact(&scope(), &reg, &caps, &[guard])
            .await
            .unwrap_err(),
        DbV2Error::Timeout
    );

    // an insert whose RETURNING row is missing is surfaced, not swallowed
    let none = host(
        mock_db()
            .append_exec_results(exec_ok(3))
            .append_query_results([no_rows()]),
    );
    assert_eq!(
        none.insert(&scope(), &reg, &caps, "events", cols("a"))
            .await
            .unwrap_err()
            .code(),
        "backend"
    );
}

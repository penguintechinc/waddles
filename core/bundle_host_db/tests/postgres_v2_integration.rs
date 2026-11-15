//! `db` v2 against a REAL Postgres container: cross-community/tenant/bundle
//! isolation, CAS (optimistic version) conflicts, filtered + indexed + paginated
//! queries, atomic transaction rollback, unique indexes, and quota exhaustion.
//!
//! The table DDL restates what `hub_api/services/bundle_data_ddl.py`
//! (`generate_v2_table_ddl`) emits (same platform columns, `FORCE ROW LEVEL
//! SECURITY`, tenant+community policy, `(tenant_id, community_id, ...)`
//! indexes) and the `bundle_data` schema restates alembic `0087_bundle_data_v2`
//! -- keep them in lockstep, exactly as `postgres_integration.rs` does for v1.

use std::time::Duration;

use bundle_host_db::v2::{
    load_registry, BundleSchemaV2, CmpOp, Condition, DbV2Error, DbV2Host, IndexColumn, IndexDef,
    Order, PostgresV2Backend, QueryRequest, SchemaRegistryV2, TableDefV2, TxOp, TxOutcome,
};
use bundle_host_db::{
    AppSchema, CapabilitySnapshot, ColumnDef, ColumnType, DbScope, DbValue, TableSchema,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
#[allow(dead_code)]
#[path = "../../test_support/tests/docker_container.rs"]
mod docker_container;
use docker_container::{start_container, DockerContainer};

const POSTGRES_IMAGE: &str = "postgres:17.6-bookworm";
const SUPERUSER_PASSWORD: &str = "postgres_test_superuser_pw";
const RUNTIME_PASSWORD: &str = "waddles_bundle_runtime_test_pw";
const RUNTIME_ROLE: &str = "waddles_bundle_runtime";
const APP: &str = "waddles.cal.test";
const OTHER_APP: &str = "waddles.other.test";

async fn start() -> (DockerContainer, String) {
    let container = start_container(
        POSTGRES_IMAGE,
        5432,
        &[
            ("POSTGRES_PASSWORD", SUPERUSER_PASSWORD),
            ("POSTGRES_DB", "waddles_test"),
        ],
        &["pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
        Duration::from_secs(240),
    )
    .await;
    let url = format!(
        "postgres://postgres:{SUPERUSER_PASSWORD}@{}:{}/waddles_test",
        container.host(),
        container.port()
    );
    (container, url)
}

fn runtime_url(superuser_url: &str) -> String {
    superuser_url.replacen(
        &format!("postgres:{SUPERUSER_PASSWORD}"),
        &format!("{RUNTIME_ROLE}:{RUNTIME_PASSWORD}"),
        1,
    )
}

fn table_ddl(table: &str, declared: &[&str], indexes: &[&str]) -> Vec<String> {
    let q = format!("\"app_community\".\"{table}\"");
    let mut v = vec![format!(
        "CREATE TABLE {q} (row_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), \
         tenant_id integer NOT NULL, community_id integer NOT NULL, \
         version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(), \
         updated_at timestamptz NOT NULL DEFAULT now(), {})",
        declared.join(", ")
    )];
    v.extend(indexes.iter().map(|i| i.replace("{q}", &q)));
    v.push(format!("ALTER TABLE {q} ENABLE ROW LEVEL SECURITY"));
    v.push(format!("ALTER TABLE {q} FORCE ROW LEVEL SECURITY"));
    v.push(format!(
        "CREATE POLICY \"{table}_tenant_isolation\" ON {q} USING \
         (tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer \
         AND community_id = NULLIF(current_setting('waddles.community_id', true), '')::integer)"
    ));
    v.push(format!(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON {q} TO \"{RUNTIME_ROLE}\""
    ));
    v
}

async fn setup(conn: &DatabaseConnection) {
    let mut stmts: Vec<String> = vec![
        "CREATE SCHEMA IF NOT EXISTS app_core".into(),
        "CREATE SCHEMA IF NOT EXISTS app_community".into(),
        "CREATE SCHEMA IF NOT EXISTS bundle_data".into(),
        format!(
            "CREATE ROLE {RUNTIME_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{RUNTIME_PASSWORD}'"
        ),
        format!("GRANT USAGE ON SCHEMA app_core, app_community, bundle_data TO {RUNTIME_ROLE}"),
        format!("REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}"),
        "CREATE TABLE bundle_data.table_registry (app_id text NOT NULL, logical_name text NOT NULL, \
         schema_kind text NOT NULL, physical_table text NOT NULL, columns jsonb NOT NULL, \
         indexes jsonb NOT NULL DEFAULT '[]'::jsonb, PRIMARY KEY (app_id, logical_name))"
            .into(),
        format!("GRANT SELECT ON bundle_data.table_registry TO {RUNTIME_ROLE}"),
        "CREATE TABLE bundle_data.usage (app_id text NOT NULL, tenant_id integer NOT NULL, \
         community_id integer NOT NULL, row_count bigint NOT NULL DEFAULT 0 CHECK (row_count >= 0), \
         byte_count bigint NOT NULL DEFAULT 0 CHECK (byte_count >= 0), \
         PRIMARY KEY (app_id, tenant_id, community_id))"
            .into(),
        "ALTER TABLE bundle_data.usage ENABLE ROW LEVEL SECURITY".into(),
        "ALTER TABLE bundle_data.usage FORCE ROW LEVEL SECURITY".into(),
        format!(
            "CREATE POLICY usage_scope ON bundle_data.usage TO {RUNTIME_ROLE} USING (\
             tenant_id = NULLIF(current_setting('waddles.tenant_id', true), '')::integer \
             AND community_id = NULLIF(current_setting('waddles.community_id', true), '')::integer)"
        ),
        format!("GRANT SELECT, INSERT, UPDATE ON bundle_data.usage TO {RUNTIME_ROLE}"),
    ];
    stmts.extend(table_ddl(
        "calapp__events",
        &[
            r#""title" varchar(200) NOT NULL"#,
            r#""start_at" timestamptz NOT NULL"#,
            r#""kind" integer"#,
            r#""note" varchar(8000)"#,
        ],
        &[
            "CREATE INDEX ON {q} (tenant_id, community_id, \"start_at\")",
            "CREATE INDEX ON {q} (tenant_id, community_id, \"title\" text_pattern_ops)",
        ],
    ));
    stmts.extend(table_ddl(
        "calapp__subs",
        &[r#""slug" varchar(64) NOT NULL"#],
        &["CREATE UNIQUE INDEX ON {q} (tenant_id, community_id, \"slug\")"],
    ));
    stmts.extend(table_ddl(
        "otherapp__events",
        &[
            r#""title" varchar(200) NOT NULL"#,
            r#""start_at" timestamptz NOT NULL"#,
            r#""kind" integer"#,
            r#""note" varchar(8000)"#,
        ],
        &["CREATE INDEX ON {q} (tenant_id, community_id, \"start_at\")"],
    ));
    for s in stmts {
        conn.execute_unprepared(&s)
            .await
            .unwrap_or_else(|e| panic!("setup failed ({s:?}): {e}"));
    }
}

fn col(name: &str, t: ColumnType, nullable: bool) -> ColumnDef {
    ColumnDef {
        name: name.into(),
        sql_type: t,
        nullable,
        is_user_ref: false,
    }
}

fn idx(col: &str, prefix_ops: bool, unique: bool) -> IndexDef {
    IndexDef {
        unique,
        columns: vec![IndexColumn {
            name: col.into(),
            descending: false,
            prefix_ops,
        }],
    }
}

fn cal_schema(app: &str, prefix: &str) -> BundleSchemaV2 {
    let events = TableSchema::validated(
        AppSchema::Community,
        format!("{prefix}__events"),
        vec![
            col("title", ColumnType::Text, false),
            col("start_at", ColumnType::Timestamptz, false),
            col("kind", ColumnType::Int4, true),
            col("note", ColumnType::Text, true),
        ],
    )
    .unwrap();
    let mut defs = vec![TableDefV2::validated(
        "events",
        events,
        vec![idx("start_at", false, false), idx("title", true, false)],
    )
    .unwrap()];
    if prefix == "calapp" {
        let subs = TableSchema::validated(
            AppSchema::Community,
            "calapp__subs",
            vec![col("slug", ColumnType::Text, false)],
        )
        .unwrap();
        defs.push(TableDefV2::validated("subs", subs, vec![idx("slug", false, true)]).unwrap());
    }
    BundleSchemaV2::validated(app, defs).unwrap()
}

fn caps() -> CapabilitySnapshot {
    let c = CapabilitySnapshot::new();
    for app in [APP, OTHER_APP] {
        c.update(app, ["storage.tables".to_string()]);
    }
    c
}

fn text(s: &str) -> DbValue {
    DbValue::Text(s.into())
}

fn ev(title: &str, start: &str) -> Vec<(String, DbValue)> {
    vec![
        ("title".into(), text(title)),
        ("start_at".into(), text(start)),
    ]
}

fn day(i: usize) -> String {
    format!("2026-03-{:02}T10:00:00Z", i + 1)
}

async fn scalar(conn: &DatabaseConnection, sql: &str) -> i64 {
    conn.query_one_raw(Statement::from_string(
        conn.get_database_backend(),
        sql.to_string(),
    ))
    .await
    .unwrap()
    .unwrap()
    .try_get::<i64>("", "n")
    .unwrap()
}

fn q(table: &str, filter: Vec<Condition>, order: Option<Order>, limit: u32) -> QueryRequest {
    QueryRequest {
        table: table.into(),
        filter,
        order,
        limit,
        cursor: None,
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn db_v2_against_real_postgres() {
    let (_c, url) = start().await;
    let admin = Database::connect(&url).await.unwrap();
    setup(&admin).await;

    let registry = SchemaRegistryV2::new();
    registry.update(cal_schema(APP, "calapp"));
    registry.update(cal_schema(OTHER_APP, "otherapp"));
    let caps = caps();
    let runtime = Database::connect(runtime_url(&url)).await.unwrap();
    let host = DbV2Host::new(PostgresV2Backend::new(runtime));

    let a7 = DbScope::new(101, 7, APP);
    let a8 = DbScope::new(101, 8, APP);
    let t2 = DbScope::new(102, 7, APP);
    let other = DbScope::new(101, 7, OTHER_APP);

    // --- 1. multiple tables per bundle; injection-shaped values are inert ---
    let evil = "x'); DROP TABLE app_community.calapp__events; --";
    let e1 = host
        .insert(&a7, &registry, &caps, "events", ev(evil, &day(0)))
        .await
        .unwrap();
    assert_eq!(e1.version, 1);
    host.insert(
        &a7,
        &registry,
        &caps,
        "subs",
        vec![("slug".into(), text("daily"))],
    )
    .await
    .unwrap();
    assert_eq!(
        scalar(
            &admin,
            "SELECT COUNT(*) AS n FROM app_community.calapp__events"
        )
        .await,
        1
    );

    // --- 2. isolation: community, tenant, bundle, and guest-named tables ---
    for (scope, why) in [(&a8, "other community"), (&t2, "other tenant")] {
        let got = host
            .get(scope, &registry, &caps, "events", &e1.row_id)
            .await;
        assert_eq!(got.unwrap_err(), DbV2Error::NotFound, "{why}: get");
        let page = host
            .query(scope, &registry, &caps, &q("events", vec![], None, 50))
            .await
            .unwrap();
        assert!(page.rows.is_empty(), "{why}: query must see nothing");
        let upd = host
            .update(
                scope,
                &registry,
                &caps,
                "events",
                &e1.row_id,
                1,
                vec![("title".into(), text("hijack"))],
            )
            .await;
        assert_eq!(upd.unwrap_err(), DbV2Error::NotFound, "{why}: update");
        let del = host
            .delete(scope, &registry, &caps, "events", &e1.row_id, 1)
            .await;
        assert_eq!(del.unwrap_err(), DbV2Error::NotFound, "{why}: delete");
        assert_eq!(
            host.count(scope, &registry, &caps, "events", &[])
                .await
                .unwrap(),
            0,
            "{why}: count"
        );
    }
    // another bundle: its own table, never ours
    let page = host
        .query(&other, &registry, &caps, &q("events", vec![], None, 50))
        .await
        .unwrap();
    assert!(
        page.rows.is_empty(),
        "other bundle sees none of this bundle's rows"
    );
    assert!(matches!(
        host.get(&other, &registry, &caps, "subs", &e1.row_id).await,
        Err(DbV2Error::InvalidTable(_))
    ));
    // a guest naming a physical / foreign table is refused
    for name in [
        "calapp__events",
        "app_community.calapp__events",
        "otherapp__events",
        "../x",
    ] {
        assert!(
            matches!(
                host.insert(&a7, &registry, &caps, name, ev("t", &day(0)))
                    .await,
                Err(DbV2Error::InvalidTable(_))
            ),
            "{name}"
        );
    }
    // undeclared capability -> denied
    let nocaps = CapabilitySnapshot::new();
    assert!(matches!(
        host.get(&a7, &registry, &nocaps, "events", &e1.row_id)
            .await,
        Err(DbV2Error::Denied(_))
    ));
    // the data really is still there, unharmed
    assert_eq!(
        host.get(&a7, &registry, &caps, "events", &e1.row_id)
            .await
            .unwrap()
            .columns[0]
            .1,
        text(evil)
    );

    // --- 3. CAS: stale version conflicts, current version bumps ---
    let u = host
        .update(
            &a7,
            &registry,
            &caps,
            "events",
            &e1.row_id,
            1,
            vec![("kind".into(), DbValue::Int(3))],
        )
        .await
        .unwrap();
    assert_eq!(u.version, 2);
    let stale = host
        .update(
            &a7,
            &registry,
            &caps,
            "events",
            &e1.row_id,
            1,
            vec![("kind".into(), DbValue::Int(4))],
        )
        .await;
    assert!(matches!(stale, Err(DbV2Error::Conflict(_))), "{stale:?}");
    let stale_del = host
        .delete(&a7, &registry, &caps, "events", &e1.row_id, 1)
        .await;
    assert!(matches!(stale_del, Err(DbV2Error::Conflict(_))));
    // concurrent racers on the same version: exactly one wins
    let barrier_results = futures_join((0..8).map(|i| {
        host.update(
            &a7,
            &registry,
            &caps,
            "events",
            &e1.row_id,
            2,
            vec![("kind".into(), DbValue::Int(100 + i))],
        )
    }))
    .await;
    let wins = barrier_results.iter().filter(|r| r.is_ok()).count();
    assert_eq!(
        wins, 1,
        "exactly one CAS racer may win: {barrier_results:?}"
    );
    assert!(barrier_results
        .iter()
        .filter_map(|r| r.as_ref().err())
        .all(|e| matches!(e, DbV2Error::Conflict(_))));
    host.delete(&a7, &registry, &caps, "events", &e1.row_id, 3)
        .await
        .unwrap();

    // --- 4. filtered, indexed, ordered, paginated queries (60 events > the old ~40 cap) ---
    let ops: Vec<TxOp> = Vec::new();
    let _ = ops;
    for chunk in (0..60usize).collect::<Vec<_>>().chunks(10) {
        let batch: Vec<TxOp> = chunk
            .iter()
            .map(|i| TxOp::Insert {
                table: "events".into(),
                columns: {
                    let mut c = ev(
                        &format!("evt_{i:02}"),
                        &format!("2026-04-{:02}T{:02}:00:00Z", i % 28 + 1, i % 24),
                    );
                    c.push(("kind".into(), DbValue::Int((i % 3) as i64)));
                    c
                },
            })
            .collect();
        host.transact(&a7, &registry, &caps, &batch).await.unwrap();
    }
    assert_eq!(
        host.count(&a7, &registry, &caps, "events", &[])
            .await
            .unwrap(),
        60
    );

    // range + eq, ordered, walked page by page with a cursor
    let mut req = q(
        "events",
        vec![
            Condition::Cmp {
                column: "start_at".into(),
                op: CmpOp::Ge,
                value: text("2026-04-05T00:00:00Z"),
            },
            Condition::Cmp {
                column: "start_at".into(),
                op: CmpOp::Lt,
                value: text("2026-04-20T00:00:00Z"),
            },
            Condition::Cmp {
                column: "kind".into(),
                op: CmpOp::Ne,
                value: DbValue::Int(2),
            },
        ],
        Some(Order {
            column: "start_at".into(),
            descending: false,
        }),
        7,
    );
    let mut seen: Vec<(String, String)> = Vec::new();
    let mut pages = 0;
    loop {
        let page = host.query(&a7, &registry, &caps, &req).await.unwrap();
        pages += 1;
        for r in &page.rows {
            let start = match &r.columns[1].1 {
                DbValue::Text(s) => s.clone(),
                other => panic!("{other:?}"),
            };
            assert!(
                start.as_str() >= "2026-04-05" && start.as_str() < "2026-04-20",
                "{start}"
            );
            seen.push((start, r.row_id.clone()));
        }
        match page.next_cursor {
            Some(c) => req.cursor = Some(c),
            None => break,
        }
        assert!(pages < 50);
    }
    let expected = host
        .count(
            &a7,
            &registry,
            &caps,
            "events",
            &[
                Condition::Cmp {
                    column: "start_at".into(),
                    op: CmpOp::Ge,
                    value: text("2026-04-05T00:00:00Z"),
                },
                Condition::Cmp {
                    column: "start_at".into(),
                    op: CmpOp::Lt,
                    value: text("2026-04-20T00:00:00Z"),
                },
            ],
        )
        .await
        .unwrap();
    assert!(
        seen.len() > 7 && pages > 1,
        "multiple pages exercised ({pages})"
    );
    assert!(seen.len() as u64 <= expected);
    let mut sorted = seen.clone();
    sorted.sort();
    assert_eq!(
        seen, sorted,
        "pages are globally ordered (start_at, row_id)"
    );
    let mut ids: Vec<_> = seen.iter().map(|s| &s.1).collect();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), seen.len(), "no row repeated across pages");

    // descending walk covers the same set
    let mut dreq = q(
        "events",
        vec![],
        Some(Order {
            column: "start_at".into(),
            descending: true,
        }),
        25,
    );
    let mut total = 0;
    loop {
        let page = host.query(&a7, &registry, &caps, &dreq).await.unwrap();
        total += page.rows.len();
        match page.next_cursor {
            Some(c) => dreq.cursor = Some(c),
            None => break,
        }
    }
    assert_eq!(total, 60);

    // IN, prefix (with a literal underscore), and the row_id default order
    let in_page = host
        .query(
            &a7,
            &registry,
            &caps,
            &q(
                "events",
                vec![Condition::In {
                    column: "start_at".into(),
                    values: vec![text("2026-04-02T01:00:00Z"), text("2026-04-03T02:00:00Z")],
                }],
                None,
                50,
            ),
        )
        .await
        .unwrap();
    assert_eq!(in_page.rows.len(), 2);
    let pre = host
        .query(
            &a7,
            &registry,
            &caps,
            &q(
                "events",
                vec![Condition::Prefix {
                    column: "title".into(),
                    prefix: "evt_1".into(),
                }],
                None,
                50,
            ),
        )
        .await
        .unwrap();
    assert_eq!(pre.rows.len(), 10, "evt_10..evt_19");
    let wildcard = host
        .query(
            &a7,
            &registry,
            &caps,
            &q(
                "events",
                vec![Condition::Prefix {
                    column: "title".into(),
                    prefix: "evt%".into(),
                }],
                None,
                50,
            ),
        )
        .await
        .unwrap();
    assert!(wildcard.rows.is_empty(), "% must be literal in a prefix");
    let underscore = host
        .query(
            &a7,
            &registry,
            &caps,
            &q(
                "events",
                vec![Condition::Prefix {
                    column: "title".into(),
                    prefix: "evt_".into(),
                }],
                None,
                50,
            ),
        )
        .await
        .unwrap();
    assert_eq!(
        underscore.rows.len(),
        50,
        "limit clamps the page; underscore literal matches all 60"
    );
    assert!(underscore.next_cursor.is_some());

    // unindexed / unbounded shapes are refused with an explicit error
    let unindexed = host
        .query(
            &a7,
            &registry,
            &caps,
            &q(
                "events",
                vec![Condition::Cmp {
                    column: "kind".into(),
                    op: CmpOp::Eq,
                    value: DbValue::Int(1),
                }],
                Some(Order {
                    column: "created_at".into(),
                    descending: false,
                }),
                10,
            ),
        )
        .await;
    assert!(
        matches!(unindexed, Err(DbV2Error::InvalidQuery(_))),
        "{unindexed:?}"
    );
    let forged = QueryRequest {
        cursor: Some("deadbeef".into()),
        ..q("events", vec![], None, 5)
    };
    assert!(matches!(
        host.query(&a7, &registry, &caps, &forged).await,
        Err(DbV2Error::InvalidQuery(_))
    ));
    // a cursor minted in one community cannot read another's data (scope is re-injected)
    let mut c1 = q("events", vec![], None, 5);
    let p = host.query(&a7, &registry, &caps, &c1).await.unwrap();
    c1.cursor = p.next_cursor;
    assert!(host
        .query(&a8, &registry, &caps, &c1)
        .await
        .unwrap()
        .rows
        .is_empty());

    // --- 5. transactions: atomic commit, full rollback on any failing op ---
    let before = host
        .count(&a7, &registry, &caps, "events", &[])
        .await
        .unwrap();
    let ok = host
        .transact(
            &a7,
            &registry,
            &caps,
            &[
                TxOp::GuardCount {
                    table: "events".into(),
                    filter: vec![Condition::Prefix {
                        column: "title".into(),
                        prefix: "tx_".into(),
                    }],
                    min: 0,
                    max: 0,
                },
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("tx_a", &day(1)),
                },
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("tx_b", &day(2)),
                },
                TxOp::Insert {
                    table: "subs".into(),
                    columns: vec![("slug".into(), text("tx"))],
                },
            ],
        )
        .await
        .unwrap();
    assert!(matches!(ok[0], TxOutcome::Done) && matches!(ok[1], TxOutcome::Row(_)));
    assert_eq!(
        host.count(&a7, &registry, &caps, "events", &[])
            .await
            .unwrap(),
        before + 2
    );
    // guard-count now fails -> nothing from this batch lands
    let guarded = host
        .transact(
            &a7,
            &registry,
            &caps,
            &[
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("tx_c", &day(3)),
                },
                TxOp::GuardCount {
                    table: "events".into(),
                    filter: vec![Condition::Prefix {
                        column: "title".into(),
                        prefix: "tx_".into(),
                    }],
                    min: 0,
                    max: 0,
                },
            ],
        )
        .await;
    assert!(
        matches!(&guarded, Err(DbV2Error::Conflict(m)) if m.starts_with("op 1")),
        "{guarded:?}"
    );
    // a later op violating the unique index rolls back the earlier insert
    let dup = host
        .transact(
            &a7,
            &registry,
            &caps,
            &[
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("tx_d", &day(4)),
                },
                TxOp::Insert {
                    table: "subs".into(),
                    columns: vec![("slug".into(), text("tx"))],
                },
            ],
        )
        .await;
    assert!(
        matches!(&dup, Err(DbV2Error::Conflict(m)) if m.contains("unique")),
        "{dup:?}"
    );
    // a missing NOT NULL column rolls back too
    let missing = host
        .transact(
            &a7,
            &registry,
            &caps,
            &[
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("tx_e", &day(5)),
                },
                TxOp::Insert {
                    table: "events".into(),
                    columns: vec![("kind".into(), DbValue::Int(1))],
                },
            ],
        )
        .await;
    assert!(
        matches!(missing, Err(DbV2Error::InvalidValue(_))),
        "{missing:?}"
    );
    assert_eq!(
        host.count(&a7, &registry, &caps, "events", &[])
            .await
            .unwrap(),
        before + 2,
        "failed batches left no rows behind"
    );
    // guard-version protects a read-modify-write across two tables
    let tx_a = host
        .query(
            &a7,
            &registry,
            &caps,
            &q(
                "events",
                vec![Condition::Prefix {
                    column: "title".into(),
                    prefix: "tx_a".into(),
                }],
                None,
                5,
            ),
        )
        .await
        .unwrap()
        .rows
        .remove(0);
    let stale_guard = host
        .transact(
            &a7,
            &registry,
            &caps,
            &[
                TxOp::GuardVersion {
                    table: "events".into(),
                    row_id: tx_a.row_id.clone(),
                    expected_version: 99,
                },
                TxOp::Insert {
                    table: "subs".into(),
                    columns: vec![("slug".into(), text("never"))],
                },
            ],
        )
        .await;
    assert!(matches!(stale_guard, Err(DbV2Error::Conflict(_))));
    assert_eq!(
        host.count(&a7, &registry, &caps, "subs", &[])
            .await
            .unwrap(),
        2,
        "daily + tx only"
    );
    // bounds: empty and oversized batches are refused
    assert!(host.transact(&a7, &registry, &caps, &[]).await.is_err());
    let big: Vec<TxOp> = (0..17)
        .map(|i| TxOp::Insert {
            table: "events".into(),
            columns: ev(&format!("z{i}"), &day(0)),
        })
        .collect();
    assert!(matches!(
        host.transact(&a7, &registry, &caps, &big).await,
        Err(DbV2Error::InvalidQuery(_))
    ));

    // --- 6. registry loads from the real bundle_data.table_registry ---
    admin
        .execute_unprepared(
            "INSERT INTO bundle_data.table_registry (app_id, logical_name, schema_kind, physical_table, columns, indexes) VALUES \
             ('waddles.cal.test','events','community','calapp__events', \
              '[{\"name\":\"title\",\"type\":\"text\"},{\"name\":\"start_at\",\"type\":\"timestamptz\"}]', \
              '[{\"columns\":[{\"name\":\"start_at\"}],\"unique\":false}]')",
        )
        .await
        .unwrap();
    let loaded = SchemaRegistryV2::new();
    let rt2 = Database::connect(runtime_url(&url)).await.unwrap();
    assert_eq!(load_registry(&rt2, &loaded).await.unwrap(), 1);
    assert!(loaded.get(APP).unwrap().table("events").is_some());
}

/// Polls every future to completion concurrently (no extra dependency).
async fn futures_join<T>(
    futs: impl IntoIterator<Item = impl std::future::Future<Output = T>>,
) -> Vec<T> {
    let handles: Vec<_> = futs.into_iter().map(|f| Box::pin(f)).collect();
    let mut out = Vec::new();
    // join_all via a simple poll_fn fan-in
    let mut pending: Vec<_> = handles.into_iter().map(Some).collect();
    let mut results: Vec<Option<T>> = (0..pending.len()).map(|_| None).collect();
    std::future::poll_fn(|cx| {
        let mut done = true;
        for (i, slot) in pending.iter_mut().enumerate() {
            if let Some(f) = slot {
                match f.as_mut().poll(cx) {
                    std::task::Poll::Ready(v) => {
                        results[i] = Some(v);
                        *slot = None;
                    }
                    std::task::Poll::Pending => done = false,
                }
            }
        }
        if done {
            std::task::Poll::Ready(())
        } else {
            std::task::Poll::Pending
        }
    })
    .await;
    for r in results {
        out.push(r.expect("future completed"));
    }
    out
}

#[tokio::test(flavor = "multi_thread")]
async fn db_v2_quotas_are_enforced_per_bundle_per_community_with_explicit_errors() {
    let (_c, url) = start().await;
    let admin = Database::connect(&url).await.unwrap();
    setup(&admin).await;
    let registry = SchemaRegistryV2::new();
    registry.update(cal_schema(APP, "calapp"));
    let caps = caps();
    let runtime = Database::connect(runtime_url(&url)).await.unwrap();
    // 3 rows / 4 KiB per (bundle, community)
    let host = DbV2Host::new(PostgresV2Backend::new(runtime).with_quota_for_test(3, 4096));
    let c7 = DbScope::new(201, 7, APP);
    let c8 = DbScope::new(201, 8, APP);

    // rows cap is shared across the bundle's tables
    let r1 = host
        .insert(&c7, &registry, &caps, "events", ev("a", &day(0)))
        .await
        .unwrap();
    host.insert(&c7, &registry, &caps, "events", ev("b", &day(1)))
        .await
        .unwrap();
    host.insert(
        &c7,
        &registry,
        &caps,
        "subs",
        vec![("slug".into(), text("s"))],
    )
    .await
    .unwrap();
    let over = host
        .insert(&c7, &registry, &caps, "events", ev("c", &day(2)))
        .await;
    assert!(
        matches!(&over, Err(DbV2Error::QuotaExceeded(m)) if m.contains("3 rows")),
        "{over:?}"
    );
    assert_eq!(scalar(&admin, "SELECT COUNT(*) AS n FROM app_community.calapp__events WHERE tenant_id = 201 AND community_id = 7").await, 2, "rejected write rolled back");
    assert_eq!(scalar(&admin, "SELECT row_count AS n FROM bundle_data.usage WHERE tenant_id = 201 AND community_id = 7").await, 3);

    // another community is unaffected by this one's exhaustion
    host.insert(&c8, &registry, &caps, "events", ev("a", &day(0)))
        .await
        .unwrap();

    // delete frees a slot
    host.delete(&c7, &registry, &caps, "events", &r1.row_id, 1)
        .await
        .unwrap();
    assert_eq!(scalar(&admin, "SELECT row_count AS n FROM bundle_data.usage WHERE tenant_id = 201 AND community_id = 7").await, 2);
    host.insert(&c7, &registry, &caps, "events", ev("c", &day(2)))
        .await
        .unwrap();

    // an atomic batch that would exceed the cap lands nothing
    host.delete(
        &c7,
        &registry,
        &caps,
        "subs",
        &host
            .query(&c7, &registry, &caps, &q("subs", vec![], None, 5))
            .await
            .unwrap()
            .rows[0]
            .row_id,
        1,
    )
    .await
    .unwrap();
    let batch = host
        .transact(
            &c7,
            &registry,
            &caps,
            &[
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("d", &day(3)),
                },
                TxOp::Insert {
                    table: "events".into(),
                    columns: ev("e", &day(4)),
                },
            ],
        )
        .await;
    assert!(
        matches!(batch, Err(DbV2Error::QuotaExceeded(_))),
        "{batch:?}"
    );
    assert_eq!(scalar(&admin, "SELECT row_count AS n FROM bundle_data.usage WHERE tenant_id = 201 AND community_id = 7").await, 2);

    // bytes cap: a large row cannot be stored once the byte budget is spent
    let big = "x".repeat(3000);
    let mut cols = ev("big", &day(5));
    cols.push(("note".into(), text(&big)));
    host.insert(&c8, &registry, &caps, "events", cols.clone())
        .await
        .unwrap();
    let bytes_over = host.insert(&c8, &registry, &caps, "events", cols).await;
    assert!(
        matches!(&bytes_over, Err(DbV2Error::QuotaExceeded(m)) if m.contains("4096 bytes")),
        "{bytes_over:?}"
    );
    // growing a row via update is byte-accounted too
    let small = host
        .insert(&c8, &registry, &caps, "events", ev("s", &day(6)))
        .await
        .unwrap();
    let grow = host
        .update(
            &c8,
            &registry,
            &caps,
            "events",
            &small.row_id,
            1,
            vec![("note".into(), text(&big))],
        )
        .await;
    assert!(matches!(grow, Err(DbV2Error::QuotaExceeded(_))), "{grow:?}");
    let _ = DbScope::new(0, 0, APP);
}

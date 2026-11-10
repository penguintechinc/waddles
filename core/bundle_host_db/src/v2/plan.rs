//! Turns the structured AST into parameterized SQL. **No guest string is ever
//! placed in SQL text**: identifiers come only from the declared schema (after
//! being matched by name), values are bound as typed parameters with an explicit
//! cast chosen by the *declared* column type, and the cursor is decoded into
//! typed values that are validated and bound exactly like any other operand.

use sea_orm::Value;

use super::ast::{CmpOp, Condition, DbV2Error, Order, QueryRequest};
use super::schema::TableDefV2;
use crate::backend::DbValue;
use crate::limits::{
    MAX_CURSOR_BYTES, MAX_FILTER_CONDITIONS, MAX_IN_LIST, MAX_PREFIX_BYTES, MAX_QUERY_LIMIT,
    MAX_TEXT_BYTES,
};
use crate::schema::{ColumnDef, ColumnType};
use crate::scope::quote_ident;
use crate::typed;

/// Platform columns a guest may reference in a filter / order (never
/// `tenant_id`/`community_id`: scope is host-injected, not guest-addressable).
const FILTERABLE_PLATFORM: &[&str] = &["row_id", "version", "created_at", "updated_at"];

/// Resolves `name` to a column definition: a declared column, or a synthetic
/// definition for a filterable platform column. Everything else is refused.
pub(crate) fn resolve_column(def: &TableDefV2, name: &str) -> Result<ColumnDef, DbV2Error> {
    if let Some(c) = def.table.column(name) {
        return Ok(c.clone());
    }
    let synthetic = |sql_type| ColumnDef {
        name: name.to_string(),
        sql_type,
        nullable: false,
        is_user_ref: false,
    };
    match name {
        "row_id" => Ok(synthetic(ColumnType::Uuid)),
        "version" => Ok(synthetic(ColumnType::Int4)),
        "created_at" | "updated_at" => Ok(synthetic(ColumnType::Timestamptz)),
        _ => Err(DbV2Error::InvalidColumn(format!(
            "{name:?} is not a column of table {:?} (platform columns allowed: {})",
            def.logical,
            FILTERABLE_PLATFORM.join(", ")
        ))),
    }
}

fn is_platform(name: &str) -> bool {
    FILTERABLE_PLATFORM.contains(&name)
}

fn qcol(def: &TableDefV2, name: &str) -> String {
    format!("{}.{}", quote_ident(&def.table.table), quote_ident(name))
}

/// Type-checks one operand against its column (never against its own shape).
fn validate_operand(col: &ColumnDef, v: &DbValue) -> Result<(), DbV2Error> {
    let bad = |why: &str| DbV2Error::InvalidValue(format!("{:?}: {why}", col.name));
    match (typed::effective_type(col), v) {
        (_, DbValue::Null) => Err(bad("NULL cannot be compared; use is-null / is-not-null")),
        (ColumnType::Uuid, DbValue::Text(s)) => uuid::Uuid::parse_str(s)
            .map(|_| ())
            .map_err(|_| bad("not a valid UUID")),
        (ColumnType::Int4, DbValue::Int(i)) => i32::try_from(*i)
            .map(|_| ())
            .map_err(|_| bad("exceeds int4 range")),
        (ColumnType::Int8, DbValue::Int(_)) | (ColumnType::Bool, DbValue::Bool(_)) => Ok(()),
        (ColumnType::Text, DbValue::Text(s)) => {
            if s.len() > MAX_TEXT_BYTES {
                Err(bad("text operand too long"))
            } else {
                Ok(())
            }
        }
        (ColumnType::Timestamptz, DbValue::Text(s)) => typed::validate_timestamptz(s).map_err(bad),
        (ColumnType::Numeric { precision, scale }, DbValue::Text(s)) => {
            typed::validate_numeric_text(s, precision, scale).map_err(bad)
        }
        (ColumnType::Numeric { precision, scale }, DbValue::Int(i)) => {
            typed::validate_numeric_int(*i, precision, scale).map_err(bad)
        }
        (ColumnType::Jsonb, _) => Err(bad("jsonb columns cannot be filtered on")),
        _ => Err(bad("operand does not match the column's declared type")),
    }
}

fn orderable(col: &ColumnDef) -> bool {
    !col.is_user_ref
        && !matches!(
            col.sql_type,
            ColumnType::Uuid | ColumnType::Bool | ColumnType::Jsonb
        )
}

fn placeholder(col: &ColumnDef, n: usize) -> String {
    format!("${n}{}", typed::param_cast(col))
}

/// Escapes `\`, `%`, `_` so a prefix is matched literally by `LIKE ... ESCAPE '\'`.
pub(crate) fn escape_like(prefix: &str) -> String {
    let mut out = String::with_capacity(prefix.len() + 1);
    for ch in prefix.chars() {
        if matches!(ch, '\\' | '%' | '_') {
            out.push('\\');
        }
        out.push(ch);
    }
    out
}

/// Plans a filter into SQL fragments joined by `AND` (empty filter -> `TRUE`).
/// Placeholders start at `$next_param`. Enforces every complexity limit.
pub(crate) fn plan_filter(
    def: &TableDefV2,
    filter: &[Condition],
    mut next_param: usize,
) -> Result<(String, Vec<Value>), DbV2Error> {
    if filter.len() > MAX_FILTER_CONDITIONS {
        return Err(DbV2Error::InvalidQuery(format!(
            "{} conditions exceeds the cap of {MAX_FILTER_CONDITIONS}",
            filter.len()
        )));
    }
    let mut parts = Vec::with_capacity(filter.len());
    let mut values = Vec::new();
    for cond in filter {
        let col = resolve_column(def, cond.column())?;
        let q = qcol(def, &col.name);
        match cond {
            Condition::Cmp { op, value, .. } => {
                let ty = typed::effective_type(&col);
                if op.is_ordering()
                    && matches!(ty, ColumnType::Uuid | ColumnType::Bool | ColumnType::Jsonb)
                {
                    return Err(DbV2Error::InvalidQuery(format!(
                        "{:?}: ordering comparison is not supported on this column type",
                        col.name
                    )));
                }
                validate_operand(&col, value)?;
                parts.push(format!(
                    "{q} {} {}",
                    op.sql(),
                    placeholder(&col, next_param)
                ));
                values.push(typed::bind_value(&col, value)?);
                next_param += 1;
            }
            Condition::In { values: vs, .. } => {
                if vs.is_empty() || vs.len() > MAX_IN_LIST {
                    return Err(DbV2Error::InvalidQuery(format!(
                        "an IN list must hold 1..={MAX_IN_LIST} values, got {}",
                        vs.len()
                    )));
                }
                let mut phs = Vec::with_capacity(vs.len());
                for v in vs {
                    validate_operand(&col, v)?;
                    phs.push(placeholder(&col, next_param));
                    values.push(typed::bind_value(&col, v)?);
                    next_param += 1;
                }
                parts.push(format!("{q} IN ({})", phs.join(", ")));
            }
            Condition::Prefix { prefix, .. } => {
                if !matches!(col.sql_type, ColumnType::Text) || col.is_user_ref {
                    return Err(DbV2Error::InvalidQuery(format!(
                        "{:?}: prefix applies to text columns only",
                        col.name
                    )));
                }
                if prefix.is_empty() || prefix.len() > MAX_PREFIX_BYTES {
                    return Err(DbV2Error::InvalidQuery(format!(
                        "a prefix must be 1..={MAX_PREFIX_BYTES} bytes"
                    )));
                }
                parts.push(format!("{q} LIKE ${next_param} ESCAPE '\\'"));
                values.push(Value::String(Some(format!("{}%", escape_like(prefix)))));
                next_param += 1;
            }
            Condition::IsNull { .. } => parts.push(format!("{q} IS NULL")),
            Condition::IsNotNull { .. } => parts.push(format!("{q} IS NOT NULL")),
        }
    }
    let sql = if parts.is_empty() {
        "TRUE".to_string()
    } else {
        parts.join(" AND ")
    };
    Ok((sql, values))
}

/// Query-complexity gate: a read must be supported by a declared index (or the
/// primary key). Admissible when some condition constrains an index-leading
/// column (`<>`/`IS NULL`/non-pattern-ops prefix don't count) or the order
/// column is the primary key / an index-leading column.
pub(crate) fn check_indexed(
    def: &TableDefV2,
    filter: &[Condition],
    order_column: &str,
) -> Result<(), DbV2Error> {
    let leads = |c: &str| c == "row_id" || def.leads_an_index(c);
    let supported = filter.iter().any(|c| match c {
        Condition::Cmp { column, op, .. } => *op != CmpOp::Ne && leads(column),
        Condition::In { column, .. } => leads(column),
        Condition::Prefix { column, .. } => def.leads_a_prefix_index(column),
        Condition::IsNull { .. } | Condition::IsNotNull { .. } => false,
    });
    if supported || leads(order_column) {
        return Ok(());
    }
    Err(DbV2Error::InvalidQuery(
        "query is not supported by a declared index: constrain or order by the leading column \
         of an index (declare one in the manifest), or by row_id"
            .to_string(),
    ))
}

/// Resolved ordering for a query.
#[derive(Debug)]
pub(crate) struct OrderPlan {
    pub column: ColumnDef,
    pub descending: bool,
}

pub(crate) fn plan_order(def: &TableDefV2, order: &Option<Order>) -> Result<OrderPlan, DbV2Error> {
    let (name, descending) = match order {
        None => ("row_id", false),
        Some(o) => (o.column.as_str(), o.descending),
    };
    let column = resolve_column(def, name)?;
    if !orderable(&column) && name != "row_id" {
        return Err(DbV2Error::InvalidQuery(format!(
            "{name:?} cannot be used to order (uuid/bool/jsonb/user_ref columns are not orderable)"
        )));
    }
    if !is_platform(name) && column.nullable {
        return Err(DbV2Error::InvalidQuery(format!(
            "order column {name:?} must be NOT NULL so cursor pagination is exact"
        )));
    }
    Ok(OrderPlan { column, descending })
}

fn value_to_json(v: &DbValue) -> Option<serde_json::Value> {
    Some(match v {
        DbValue::Text(s) => serde_json::Value::String(s.clone()),
        DbValue::Int(i) => serde_json::Value::from(*i),
        DbValue::Bool(b) => serde_json::Value::Bool(*b),
        _ => return None,
    })
}

fn json_to_value(v: &serde_json::Value) -> Option<DbValue> {
    match v {
        serde_json::Value::String(s) => Some(DbValue::Text(s.clone())),
        serde_json::Value::Bool(b) => Some(DbValue::Bool(*b)),
        serde_json::Value::Number(n) => n.as_i64().map(DbValue::Int),
        _ => None,
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    bytes.iter().fold(String::new(), |mut s, b| {
        let _ = write!(s, "{b:02x}");
        s
    })
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

/// Encodes the keyset position (last row's order value + row id) as an opaque
/// cursor bound to the order column and direction it was issued for.
pub(crate) fn encode_cursor(
    order: &OrderPlan,
    order_value: Option<&DbValue>,
    row_id: &str,
) -> String {
    let doc = serde_json::json!({
        "c": order.column.name,
        "d": order.descending,
        "v": order_value.and_then(value_to_json),
        "r": row_id,
    });
    hex(doc.to_string().as_bytes())
}

/// A decoded, validated keyset position.
pub(crate) struct CursorPos {
    pub value: Option<DbValue>,
    pub row_id: uuid::Uuid,
}

/// Decodes a cursor and checks it belongs to this order column + direction.
/// Every decoded value is re-validated like any operand, so a forged cursor can
/// only reposition within the caller's own scoped rows.
pub(crate) fn decode_cursor(order: &OrderPlan, cursor: &str) -> Result<CursorPos, DbV2Error> {
    let bad = || DbV2Error::InvalidQuery("malformed or foreign pagination cursor".to_string());
    if cursor.len() > MAX_CURSOR_BYTES {
        return Err(bad());
    }
    let raw = unhex(cursor).ok_or_else(bad)?;
    let doc: serde_json::Value = serde_json::from_slice(&raw).map_err(|_| bad())?;
    if doc.get("c").and_then(|v| v.as_str()) != Some(order.column.name.as_str())
        || doc.get("d").and_then(|v| v.as_bool()) != Some(order.descending)
    {
        return Err(bad());
    }
    let row_id = doc
        .get("r")
        .and_then(|v| v.as_str())
        .and_then(|s| uuid::Uuid::parse_str(s).ok())
        .ok_or_else(bad)?;
    let value = if order.column.name == "row_id" {
        None
    } else {
        let v = doc.get("v").and_then(json_to_value).ok_or_else(bad)?;
        validate_operand(&order.column, &v)?;
        Some(v)
    };
    Ok(CursorPos { value, row_id })
}

/// Everything `query` needs to execute and to mint the next cursor.
#[derive(Debug)]
pub(crate) struct QueryPlan {
    pub sql: String,
    pub values: Vec<Value>,
    pub order: OrderPlan,
    pub limit: u32,
}

/// Plans a full query: scope predicate ($1,$2), filter, keyset, order, limit+1.
pub(crate) fn plan_query(
    def: &TableDefV2,
    req: &QueryRequest,
    select_list: &str,
    tenant_id: i32,
    community_id: i32,
) -> Result<QueryPlan, DbV2Error> {
    if req.limit == 0 || req.limit > MAX_QUERY_LIMIT {
        return Err(DbV2Error::InvalidQuery(format!(
            "limit must be 1..={MAX_QUERY_LIMIT}"
        )));
    }
    let order = plan_order(def, &req.order)?;
    check_indexed(def, &req.filter, &order.column.name)?;

    let mut values = vec![Value::Int(Some(tenant_id)), Value::Int(Some(community_id))];
    let (filter_sql, filter_values) = plan_filter(def, &req.filter, 3)?;
    values.extend(filter_values);
    let mut where_sql = format!(
        "{}.tenant_id = $1 AND {}.community_id = $2 AND {filter_sql}",
        quote_ident(&def.table.table),
        quote_ident(&def.table.table)
    );

    let dir = if order.descending { "DESC" } else { "ASC" };
    let cmp = if order.descending { "<" } else { ">" };
    if let Some(cursor) = &req.cursor {
        let pos = decode_cursor(&order, cursor)?;
        let rid_param = values.len() + 1;
        match pos.value {
            None => {
                where_sql.push_str(&format!(
                    " AND {} {cmp} ${rid_param}::uuid",
                    qcol(def, "row_id")
                ));
                values.push(Value::Uuid(Some(pos.row_id)));
            }
            Some(v) => {
                let v_param = values.len() + 1;
                let rid_param = v_param + 1;
                where_sql.push_str(&format!(
                    " AND ({}, {}) {cmp} ({}, ${rid_param}::uuid)",
                    qcol(def, &order.column.name),
                    qcol(def, "row_id"),
                    placeholder(&order.column, v_param)
                ));
                values.push(typed::bind_value(&order.column, &v)?);
                values.push(Value::Uuid(Some(pos.row_id)));
            }
        }
    }

    let order_sql = if order.column.name == "row_id" {
        format!("{} {dir}", qcol(def, "row_id"))
    } else {
        format!(
            "{} {dir}, {} {dir}",
            qcol(def, &order.column.name),
            qcol(def, "row_id")
        )
    };
    let sql = format!(
        "SELECT {select_list} FROM {} WHERE {where_sql} ORDER BY {order_sql} LIMIT {}",
        def.table.qualified_name(),
        i64::from(req.limit) + 1
    );
    Ok(QueryPlan {
        sql,
        values,
        order,
        limit: req.limit,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::TableSchema;
    use crate::scope::AppSchema;
    use crate::v2::schema::{IndexColumn, IndexDef};

    fn def() -> TableDefV2 {
        let cols = vec![
            ColumnDef {
                name: "title".into(),
                sql_type: ColumnType::Text,
                nullable: false,
                is_user_ref: false,
            },
            ColumnDef {
                name: "start_at".into(),
                sql_type: ColumnType::Timestamptz,
                nullable: false,
                is_user_ref: false,
            },
            ColumnDef {
                name: "kind".into(),
                sql_type: ColumnType::Int4,
                nullable: true,
                is_user_ref: false,
            },
            ColumnDef {
                name: "meta".into(),
                sql_type: ColumnType::Jsonb,
                nullable: true,
                is_user_ref: false,
            },
            ColumnDef {
                name: "owner_ref".into(),
                sql_type: ColumnType::Uuid,
                nullable: true,
                is_user_ref: true,
            },
        ];
        let table = TableSchema::validated(AppSchema::Community, "app__events", cols).unwrap();
        let ix = |c: &str, prefix_ops| IndexDef {
            unique: false,
            columns: vec![IndexColumn {
                name: c.into(),
                descending: false,
                prefix_ops,
            }],
        };
        TableDefV2::validated(
            "events",
            table,
            vec![ix("start_at", false), ix("title", true)],
        )
        .unwrap()
    }

    fn req(filter: Vec<Condition>) -> QueryRequest {
        QueryRequest {
            table: "events".into(),
            filter,
            order: Some(Order {
                column: "start_at".into(),
                descending: false,
            }),
            limit: 10,
            cursor: None,
        }
    }

    fn ts(s: &str) -> DbValue {
        DbValue::Text(s.into())
    }

    #[test]
    fn range_filter_builds_parameterized_sql_with_no_guest_text() {
        let r = req(vec![
            Condition::Cmp {
                column: "start_at".into(),
                op: CmpOp::Ge,
                value: ts("2026-01-01T00:00:00Z"),
            },
            Condition::Cmp {
                column: "start_at".into(),
                op: CmpOp::Lt,
                value: ts("2026-02-01T00:00:00Z"),
            },
        ]);
        let p = plan_query(&def(), &r, "x", 7, 9).unwrap();
        assert!(
            p.sql
                .contains(r#""app__events"."start_at" >= $3::timestamptz"#),
            "{}",
            p.sql
        );
        assert!(p.sql.contains("< $4::timestamptz"));
        assert!(p.sql.ends_with("LIMIT 11"), "{}", p.sql);
        assert!(!p.sql.contains("2026"), "a value leaked into SQL text");
        assert_eq!(p.values.len(), 4);
    }

    #[test]
    fn injection_shaped_values_never_reach_sql_text() {
        let evil = "x'; DROP TABLE users; --";
        let r = req(vec![Condition::Prefix {
            column: "title".into(),
            prefix: evil.into(),
        }]);
        let p = plan_query(&def(), &r, "x", 1, 1).unwrap();
        assert!(!p.sql.contains("DROP"), "{}", p.sql);
        assert!(p.sql.contains("LIKE $3 ESCAPE"));
    }

    #[test]
    fn injection_shaped_identifiers_are_refused() {
        for bad in [
            "title\"; DROP TABLE x; --",
            "tenant_id",
            "community_id",
            "nope",
            "title OR 1=1",
        ] {
            let r = req(vec![Condition::IsNull { column: bad.into() }]);
            assert!(
                matches!(
                    plan_query(&def(), &r, "x", 1, 1),
                    Err(DbV2Error::InvalidColumn(_))
                ),
                "{bad}"
            );
        }
        let mut r = req(vec![]);
        r.order = Some(Order {
            column: "tenant_id; --".into(),
            descending: false,
        });
        assert!(plan_query(&def(), &r, "x", 1, 1).is_err());
    }

    #[test]
    fn like_wildcards_in_a_prefix_are_escaped() {
        assert_eq!(escape_like(r"50%_off\"), r"50\%\_off\\");
        let r = req(vec![Condition::Prefix {
            column: "title".into(),
            prefix: "a%b".into(),
        }]);
        let p = plan_query(&def(), &r, "x", 1, 1).unwrap();
        assert_eq!(p.values[2], Value::String(Some(r"a\%b%".into())));
    }

    #[test]
    fn complexity_limits_are_enforced() {
        let many: Vec<_> = (0..=MAX_FILTER_CONDITIONS)
            .map(|_| Condition::IsNotNull {
                column: "title".into(),
            })
            .collect();
        assert!(plan_query(&def(), &req(many), "x", 1, 1).is_err());
        let big_in = Condition::In {
            column: "kind".into(),
            values: (0..=MAX_IN_LIST as i64).map(DbValue::Int).collect(),
        };
        assert!(plan_query(&def(), &req(vec![big_in]), "x", 1, 1).is_err());
        let empty_in = Condition::In {
            column: "kind".into(),
            values: vec![],
        };
        assert!(plan_query(&def(), &req(vec![empty_in]), "x", 1, 1).is_err());
        let mut r = req(vec![]);
        r.limit = MAX_QUERY_LIMIT + 1;
        assert!(plan_query(&def(), &r, "x", 1, 1).is_err());
        r.limit = 0;
        assert!(plan_query(&def(), &r, "x", 1, 1).is_err());
    }

    #[test]
    fn unindexed_queries_are_rejected_but_indexed_or_pk_ones_pass() {
        // filter on non-indexed `kind`, order by non-indexed created_at -> rejected
        let mut r = req(vec![Condition::Cmp {
            column: "kind".into(),
            op: CmpOp::Eq,
            value: DbValue::Int(1),
        }]);
        r.order = Some(Order {
            column: "created_at".into(),
            descending: false,
        });
        let err = plan_query(&def(), &r, "x", 1, 1).unwrap_err();
        assert!(matches!(err, DbV2Error::InvalidQuery(_)), "{err:?}");
        // same filter, order by indexed start_at -> ok
        r.order = Some(Order {
            column: "start_at".into(),
            descending: true,
        });
        assert!(plan_query(&def(), &r, "x", 1, 1).is_ok());
        // empty filter, default row_id order -> ok (PK)
        let mut r2 = req(vec![]);
        r2.order = None;
        assert!(plan_query(&def(), &r2, "x", 1, 1).is_ok());
        // `<>` on an indexed column does not count as index support
        let mut r3 = req(vec![Condition::Cmp {
            column: "start_at".into(),
            op: CmpOp::Ne,
            value: ts("2026-01-01T00:00:00Z"),
        }]);
        r3.order = Some(Order {
            column: "created_at".into(),
            descending: false,
        });
        assert!(plan_query(&def(), &r3, "x", 1, 1).is_err());
        // prefix needs a pattern-ops index: title has one, kind is not text
        let ok = req(vec![Condition::Prefix {
            column: "title".into(),
            prefix: "a".into(),
        }]);
        assert!(check_indexed(&def(), &ok.filter, "created_at").is_ok());
    }

    #[test]
    fn type_rules_for_operands_and_ops() {
        let d = def();
        let bad = |c: Condition| plan_filter(&d, &[c], 3).is_err();
        assert!(bad(Condition::Cmp {
            column: "kind".into(),
            op: CmpOp::Eq,
            value: DbValue::Text("1".into())
        }));
        assert!(bad(Condition::Cmp {
            column: "kind".into(),
            op: CmpOp::Eq,
            value: DbValue::Int(i64::MAX)
        }));
        assert!(bad(Condition::Cmp {
            column: "meta".into(),
            op: CmpOp::Eq,
            value: DbValue::Text("{}".into())
        }));
        assert!(bad(Condition::Cmp {
            column: "owner_ref".into(),
            op: CmpOp::Lt,
            value: DbValue::Text(uuid::Uuid::nil().to_string())
        }));
        assert!(bad(Condition::Cmp {
            column: "start_at".into(),
            op: CmpOp::Eq,
            value: DbValue::Text("yesterday".into())
        }));
        assert!(bad(Condition::Cmp {
            column: "kind".into(),
            op: CmpOp::Eq,
            value: DbValue::Null
        }));
        assert!(bad(Condition::Prefix {
            column: "kind".into(),
            prefix: "a".into()
        }));
        assert!(bad(Condition::Prefix {
            column: "title".into(),
            prefix: String::new()
        }));
        assert!(!bad(Condition::Cmp {
            column: "owner_ref".into(),
            op: CmpOp::Eq,
            value: DbValue::Text(uuid::Uuid::nil().to_string())
        }));
    }

    #[test]
    fn nullable_or_unorderable_order_columns_are_rejected() {
        let d = def();
        for col in ["kind", "meta", "owner_ref"] {
            let o = Some(Order {
                column: col.into(),
                descending: false,
            });
            assert!(plan_order(&d, &o).is_err(), "{col}");
        }
        assert!(plan_order(&d, &None).is_ok());
    }

    #[test]
    fn cursor_round_trips_and_is_bound_to_its_order_column() {
        let d = def();
        let order = plan_order(
            &d,
            &Some(Order {
                column: "start_at".into(),
                descending: false,
            }),
        )
        .unwrap();
        let rid = uuid::Uuid::new_v4().to_string();
        let c = encode_cursor(&order, Some(&ts("2026-01-01T00:00:00.000000Z")), &rid);
        let pos = decode_cursor(&order, &c).unwrap();
        assert_eq!(pos.row_id.to_string(), rid);
        assert_eq!(pos.value, Some(ts("2026-01-01T00:00:00.000000Z")));
        let desc = plan_order(
            &d,
            &Some(Order {
                column: "start_at".into(),
                descending: true,
            }),
        )
        .unwrap();
        assert!(decode_cursor(&desc, &c).is_err(), "direction mismatch");
        let other = plan_order(&d, &None).unwrap();
        assert!(decode_cursor(&other, &c).is_err(), "column mismatch");
        assert!(decode_cursor(&order, "zz").is_err());
        assert!(decode_cursor(&order, &hex(b"{\"c\":1}")).is_err());
        assert!(decode_cursor(&order, &"a".repeat(MAX_CURSOR_BYTES + 2)).is_err());
        // forged value of the wrong type is refused
        let forged = hex(
            serde_json::json!({"c":"start_at","d":false,"v":"not-a-date","r":rid})
                .to_string()
                .as_bytes(),
        );
        assert!(decode_cursor(&order, &forged).is_err());
    }

    #[test]
    fn keyset_predicate_uses_row_comparison_in_the_order_direction() {
        let d = def();
        let order = plan_order(
            &d,
            &Some(Order {
                column: "start_at".into(),
                descending: true,
            }),
        )
        .unwrap();
        let c = encode_cursor(
            &order,
            Some(&ts("2026-01-01T00:00:00.000000Z")),
            &uuid::Uuid::new_v4().to_string(),
        );
        let mut r = req(vec![]);
        r.order = Some(Order {
            column: "start_at".into(),
            descending: true,
        });
        r.cursor = Some(c);
        let p = plan_query(&d, &r, "x", 1, 1).unwrap();
        assert!(
            p.sql.contains(r#"("app__events"."start_at", "app__events"."row_id") < ($3::timestamptz, $4::uuid)"#),
            "{}",
            p.sql
        );
        assert!(p
            .sql
            .contains(r#"ORDER BY "app__events"."start_at" DESC, "app__events"."row_id" DESC"#));
    }
}

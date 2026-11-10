//! The `db` v2 host-call wire contract: JSON `{op, args}` in, JSON out, shared
//! by both stages so each needs one call to serve it (see `handle`). The
//! executor encodes the WIT `db-v2` types into exactly these shapes
//! (`core/bundle_executor/src/host/stage_next_storage_v2.rs`); the contract
//! fixtures in this module's tests pin both ends.
//!
//! Ops ride `capability = db` with the `db2.` prefix: `db2.insert`, `db2.get`,
//! `db2.update`, `db2.delete`, `db2.query`, `db2.count`, `db2.transact`.
//! Values are JSON `null`/bool/integer/string -- never floats or bytes.

use serde_json::{json, Map, Value};

use super::ast::{CmpOp, Condition, DbV2Error, Order, QueryRequest, RowV2, TxOp, TxOutcome};
use super::{DbV2Host, SchemaRegistryV2};
use crate::authorize::CapabilitySnapshot;
use crate::backend::DbValue;
use crate::scope::DbScope;

/// A refusal in wire form: stable `code` + human `message` (the stage turns
/// this into its `HostResultError`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireError {
    pub code: &'static str,
    pub message: String,
}

impl From<DbV2Error> for WireError {
    fn from(e: DbV2Error) -> Self {
        WireError {
            code: e.code(),
            message: e.message(),
        }
    }
}

fn bad(msg: impl Into<String>) -> WireError {
    WireError {
        code: "invalid_args",
        message: msg.into(),
    }
}

/// True for every op this module serves.
pub fn is_v2_op(op: &str) -> bool {
    op.starts_with("db2.")
}

fn str_arg<'a>(args: &'a Value, key: &str) -> Result<&'a str, WireError> {
    args.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| bad(format!("{key} must be a string")))
}

fn u64_arg(args: &Value, key: &str) -> Result<u64, WireError> {
    args.get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| bad(format!("{key} must be an unsigned integer")))
}

fn json_to_value(v: &Value) -> Result<DbValue, WireError> {
    match v {
        Value::Null => Ok(DbValue::Null),
        Value::Bool(b) => Ok(DbValue::Bool(*b)),
        Value::String(s) => Ok(DbValue::Text(s.clone())),
        Value::Number(n) => n.as_i64().map(DbValue::Int).ok_or_else(|| {
            bad("only integer numbers are accepted (use a decimal string for numeric columns)")
        }),
        _ => Err(bad("a value must be null, bool, integer or string")),
    }
}

fn value_to_json(v: &DbValue) -> Value {
    match v {
        DbValue::Null => Value::Null,
        DbValue::Bool(b) => json!(b),
        DbValue::Int(i) => json!(i),
        DbValue::Float(f) => json!(f),
        DbValue::Text(s) => json!(s),
        DbValue::Bytes(_) => Value::Null,
    }
}

/// Parses a `{column: value}` object into ordered pairs.
pub fn parse_columns(v: Option<&Value>) -> Result<Vec<(String, DbValue)>, WireError> {
    let obj = v
        .and_then(Value::as_object)
        .ok_or_else(|| bad("columns must be a JSON object"))?;
    obj.iter()
        .map(|(k, v)| Ok((k.clone(), json_to_value(v)?)))
        .collect()
}

/// Parses one filter condition object.
pub fn parse_condition(v: &Value) -> Result<Condition, WireError> {
    let op = str_arg(v, "op")?;
    let column = str_arg(v, "column")?.to_string();
    let cmp = |op| -> Result<Condition, WireError> {
        Ok(Condition::Cmp {
            column: column.clone(),
            op,
            value: json_to_value(v.get("value").ok_or_else(|| bad("value is required"))?)?,
        })
    };
    match op {
        "eq" => cmp(CmpOp::Eq),
        "ne" => cmp(CmpOp::Ne),
        "lt" => cmp(CmpOp::Lt),
        "le" => cmp(CmpOp::Le),
        "gt" => cmp(CmpOp::Gt),
        "ge" => cmp(CmpOp::Ge),
        "in" => {
            let values = v
                .get("values")
                .and_then(Value::as_array)
                .ok_or_else(|| bad("values must be an array"))?
                .iter()
                .map(json_to_value)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(Condition::In { column, values })
        }
        "prefix" => Ok(Condition::Prefix {
            column,
            prefix: str_arg(v, "value")?.to_string(),
        }),
        "is_null" => Ok(Condition::IsNull { column }),
        "is_not_null" => Ok(Condition::IsNotNull { column }),
        other => Err(bad(format!("unknown condition op {other:?}"))),
    }
}

/// Parses a `filter` array (absent = empty).
pub fn parse_filter(v: Option<&Value>) -> Result<Vec<Condition>, WireError> {
    match v {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => items.iter().map(parse_condition).collect(),
        Some(_) => Err(bad("filter must be an array")),
    }
}

/// Parses `db2.query` args.
pub fn parse_query(args: &Value) -> Result<QueryRequest, WireError> {
    let order = match args.get("order") {
        None | Some(Value::Null) => None,
        Some(o) => Some(Order {
            column: str_arg(o, "column")?.to_string(),
            descending: o
                .get("descending")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        }),
    };
    let cursor = match args.get("cursor") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => return Err(bad("cursor must be a string")),
    };
    Ok(QueryRequest {
        table: str_arg(args, "table")?.to_string(),
        filter: parse_filter(args.get("filter"))?,
        order,
        limit: u32::try_from(u64_arg(args, "limit")?).map_err(|_| bad("limit out of range"))?,
        cursor,
    })
}

/// Parses one `db2.transact` op object (`{"op":"insert", ...}`).
pub fn parse_tx_op(v: &Value) -> Result<TxOp, WireError> {
    let table = str_arg(v, "table")?.to_string();
    Ok(match str_arg(v, "op")? {
        "insert" => TxOp::Insert {
            table,
            columns: parse_columns(v.get("columns"))?,
        },
        "update" => TxOp::Update {
            table,
            row_id: str_arg(v, "row_id")?.to_string(),
            expected_version: u64_arg(v, "expected_version")?,
            columns: parse_columns(v.get("columns"))?,
        },
        "delete" => TxOp::Delete {
            table,
            row_id: str_arg(v, "row_id")?.to_string(),
            expected_version: u64_arg(v, "expected_version")?,
        },
        "guard_version" => TxOp::GuardVersion {
            table,
            row_id: str_arg(v, "row_id")?.to_string(),
            expected_version: u64_arg(v, "expected_version")?,
        },
        "guard_count" => TxOp::GuardCount {
            table,
            filter: parse_filter(v.get("filter"))?,
            min: u64_arg(v, "min")?,
            max: u64_arg(v, "max")?,
        },
        other => return Err(bad(format!("unknown transact op {other:?}"))),
    })
}

/// Renders a row to its wire shape.
pub fn row_json(row: &RowV2) -> Value {
    let cols: Map<String, Value> = row
        .columns
        .iter()
        .map(|(k, v)| (k.clone(), value_to_json(v)))
        .collect();
    json!({
        "table": row.table,
        "row_id": row.row_id,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "columns": cols,
    })
}

/// Serves one `db2.*` host-call. The caller has already passed the stage's
/// capability gate; this authorizes again at the crate seam, resolves the
/// declared schema, and runs the op host-scoped to `scope`.
pub async fn handle(
    host: &DbV2Host,
    scope: &DbScope,
    registry: &SchemaRegistryV2,
    caps: &CapabilitySnapshot,
    op: &str,
    args: &Value,
) -> Result<Value, WireError> {
    match op {
        "db2.insert" => {
            let row = host
                .insert(
                    scope,
                    registry,
                    caps,
                    str_arg(args, "table")?,
                    parse_columns(args.get("columns"))?,
                )
                .await?;
            Ok(row_json(&row))
        }
        "db2.get" => {
            let row = host
                .get(
                    scope,
                    registry,
                    caps,
                    str_arg(args, "table")?,
                    str_arg(args, "row_id")?,
                )
                .await?;
            Ok(row_json(&row))
        }
        "db2.update" => {
            let row = host
                .update(
                    scope,
                    registry,
                    caps,
                    str_arg(args, "table")?,
                    str_arg(args, "row_id")?,
                    u64_arg(args, "expected_version")?,
                    parse_columns(args.get("columns"))?,
                )
                .await?;
            Ok(row_json(&row))
        }
        "db2.delete" => {
            host.delete(
                scope,
                registry,
                caps,
                str_arg(args, "table")?,
                str_arg(args, "row_id")?,
                u64_arg(args, "expected_version")?,
            )
            .await?;
            Ok(json!({}))
        }
        "db2.query" => {
            let page = host
                .query(scope, registry, caps, &parse_query(args)?)
                .await?;
            Ok(json!({
                "rows": page.rows.iter().map(row_json).collect::<Vec<_>>(),
                "next_cursor": page.next_cursor,
            }))
        }
        "db2.count" => {
            let n = host
                .count(
                    scope,
                    registry,
                    caps,
                    str_arg(args, "table")?,
                    &parse_filter(args.get("filter"))?,
                )
                .await?;
            Ok(json!({ "count": n }))
        }
        "db2.transact" => {
            let ops = args
                .get("ops")
                .and_then(Value::as_array)
                .ok_or_else(|| bad("ops must be an array"))?
                .iter()
                .map(parse_tx_op)
                .collect::<Result<Vec<_>, _>>()?;
            let results = host.transact(scope, registry, caps, &ops).await?;
            Ok(json!({
                "results": results.iter().map(|r| match r {
                    TxOutcome::Row(row) => row_json(row),
                    TxOutcome::Done => Value::Null,
                }).collect::<Vec<_>>(),
            }))
        }
        other => Err(WireError {
            code: "unknown_op",
            message: format!("db op {other:?} not supported"),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_contract_fixture_parses() {
        let q = parse_query(&json!({
            "table": "events",
            "filter": [
                {"op": "ge", "column": "start_at", "value": "2026-01-01T00:00:00Z"},
                {"op": "in", "column": "kind", "values": [1, 2]},
                {"op": "prefix", "column": "title", "value": "ab"},
                {"op": "is_null", "column": "note"}
            ],
            "order": {"column": "start_at", "descending": true},
            "limit": 25,
            "cursor": null
        }))
        .unwrap();
        assert_eq!(q.table, "events");
        assert_eq!(q.filter.len(), 4);
        assert_eq!(q.limit, 25);
        assert!(q.order.unwrap().descending);
        assert!(q.cursor.is_none());
    }

    #[test]
    fn transact_contract_fixture_parses_every_op() {
        let ops: Vec<TxOp> = [
            json!({"op":"insert","table":"t","columns":{"a":1}}),
            json!({"op":"update","table":"t","row_id":"r","expected_version":3,"columns":{"a":null}}),
            json!({"op":"delete","table":"t","row_id":"r","expected_version":3}),
            json!({"op":"guard_version","table":"t","row_id":"r","expected_version":3}),
            json!({"op":"guard_count","table":"t","filter":[],"min":0,"max":4}),
        ]
        .iter()
        .map(|v| parse_tx_op(v).unwrap())
        .collect();
        assert_eq!(ops.len(), 5);
    }

    #[test]
    fn malformed_args_are_invalid_args_not_panics() {
        for v in [
            json!({"op":"eq","column":"c","value":1.5}),
            json!({"op":"eq","column":"c"}),
            json!({"op":"wat","column":"c"}),
            json!({"op":"in","column":"c","values":"x"}),
            json!({"op":"eq","value":1}),
            json!({"op":"eq","column":"c","value":[1]}),
        ] {
            let e = parse_condition(&v).unwrap_err();
            assert_eq!(e.code, "invalid_args", "{v}");
        }
        assert!(
            parse_query(&json!({"table":"t"})).is_err(),
            "limit required"
        );
        assert!(parse_query(&json!({"table":"t","limit":1,"cursor":5})).is_err());
        assert!(parse_tx_op(&json!({"op":"nope","table":"t"})).is_err());
        assert!(parse_columns(Some(&json!([1]))).is_err());
        assert!(is_v2_op("db2.query") && !is_v2_op("query"));
    }

    #[test]
    fn row_json_shape_is_stable() {
        let row = RowV2 {
            table: "t".into(),
            row_id: "id".into(),
            version: 2,
            created_at: "c".into(),
            updated_at: "u".into(),
            columns: vec![("a".into(), DbValue::Int(1)), ("b".into(), DbValue::Null)],
        };
        assert_eq!(
            row_json(&row),
            json!({"table":"t","row_id":"id","version":2,"created_at":"c","updated_at":"u","columns":{"a":1,"b":null}})
        );
    }
}

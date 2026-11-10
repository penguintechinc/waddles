//! `kv-v2` and `db-v2`: the additive storage imports (`wit/waddle-bundle/
//! storage-v2.wit`, design `docs/bundle-storage-v2.md`) for worlds `stage-next`,
//! `stage-v2` and `stage-tools`.
//!
//! Every call rides the standard [`imports::call`](crate::host::imports::call)
//! round trip. `kv-v2` ops are carried as `capability = kv`, op `get_versioned`/
//! `cas`/`delete_if`/`list`; `db-v2` ops as `capability = db`, op
//! `db2.<insert|get|update|delete|query|count|transact>`. Nothing here decides
//! authority: this layer encodes the call, decodes the answer and maps refusals
//! onto the WIT error variants. Tenant/community scope is ALWAYS host-injected
//! by the stage and never an argument or a result. The JSON shapes are the
//! contract `bundle_host_db::v2::wire` / the stages' `handle_kv` serve; the
//! contract fixtures in this file's tests pin the executor half.

use penguin_bundle_host::wire::CapabilityKind;
use serde_json::{json, Map, Value};

use crate::engine::stage_next_world::waddle::bundle::{db_v2, kv_v2};
use crate::error::ExecutorError;
use crate::host::imports::call;
use crate::host::ExecState;

// ---------------------------------------------------------------- kv-v2 ----

fn kv_error_from(err: ExecutorError) -> kv_v2::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => match code.as_str() {
            "too_large" => kv_v2::Error::TooLarge(message.parse().unwrap_or(u64::MAX)),
            "quota_exceeded" | "rate_limited" => kv_v2::Error::QuotaExceeded(message.clone()),
            "invalid_key" | "invalid_args" => kv_v2::Error::Invalid(message.clone()),
            _ => kv_v2::Error::Backend(format!("{code}: {message}")),
        },
        other => kv_v2::Error::Backend(other.to_string()),
    }
}

fn malformed(what: &str) -> kv_v2::Error {
    kv_v2::Error::Backend(format!("malformed host-result: {what}"))
}

fn u64_field(v: &Value, key: &str) -> Result<u64, kv_v2::Error> {
    v.get(key)
        .and_then(Value::as_u64)
        .ok_or_else(|| malformed(key))
}

impl kv_v2::Host for ExecState {
    async fn get_versioned(
        &mut self,
        key: String,
    ) -> Result<Option<kv_v2::Versioned>, kv_v2::Error> {
        let v = call(
            self,
            CapabilityKind::Kv,
            "get_versioned",
            json!({ "key": key }),
        )
        .await
        .map_err(kv_error_from)?;
        match v.get("value") {
            None | Some(Value::Null) => Ok(None),
            Some(bytes) => Ok(Some(kv_v2::Versioned {
                value: serde_json::from_value(bytes.clone()).map_err(|_| malformed("value"))?,
                version: u64_field(&v, "version")?,
            })),
        }
    }

    async fn cas(
        &mut self,
        key: String,
        expected_version: u64,
        value: Vec<u8>,
        ttl_seconds: u32,
    ) -> Result<kv_v2::CasResult, kv_v2::Error> {
        let args = json!({
            "key": key, "expected_version": expected_version,
            "value": value, "ttl_seconds": ttl_seconds,
        });
        let v = call(self, CapabilityKind::Kv, "cas", args)
            .await
            .map_err(kv_error_from)?;
        let version = u64_field(&v, "version")?;
        match v.get("swapped").and_then(Value::as_bool) {
            Some(true) => Ok(kv_v2::CasResult::Swapped(version)),
            Some(false) => Ok(kv_v2::CasResult::Conflict(version)),
            None => Err(malformed("swapped")),
        }
    }

    async fn delete_if(
        &mut self,
        key: String,
        expected_version: u64,
    ) -> Result<kv_v2::DeleteResult, kv_v2::Error> {
        let args = json!({ "key": key, "expected_version": expected_version });
        let v = call(self, CapabilityKind::Kv, "delete_if", args)
            .await
            .map_err(kv_error_from)?;
        match v.get("deleted").and_then(Value::as_bool) {
            Some(true) => Ok(kv_v2::DeleteResult::Deleted),
            Some(false) => Ok(kv_v2::DeleteResult::Conflict(u64_field(&v, "version")?)),
            None => Err(malformed("deleted")),
        }
    }

    async fn list_keys(
        &mut self,
        prefix: String,
        cursor: Option<String>,
        limit: u32,
    ) -> Result<kv_v2::KeyPage, kv_v2::Error> {
        let args = json!({ "prefix": prefix, "cursor": cursor, "limit": limit });
        let v = call(self, CapabilityKind::Kv, "list", args)
            .await
            .map_err(kv_error_from)?;
        Ok(kv_v2::KeyPage {
            keys: serde_json::from_value(v.get("keys").cloned().unwrap_or(Value::Null))
                .map_err(|_| malformed("keys"))?,
            next_cursor: match v.get("next_cursor") {
                None | Some(Value::Null) => None,
                Some(Value::String(s)) => Some(s.clone()),
                Some(_) => return Err(malformed("next_cursor")),
            },
        })
    }
}

// ---------------------------------------------------------------- db-v2 ----

fn db_error_from(err: ExecutorError) -> db_v2::Error {
    match &err {
        ExecutorError::HostCallDenied { code, message, .. } => {
            let m = message.clone();
            match code.as_str() {
                "invalid_table" => db_v2::Error::InvalidTable(m),
                "invalid_column" => db_v2::Error::InvalidColumn(m),
                "invalid_value" | "invalid_args" => db_v2::Error::InvalidValue(m),
                "invalid_query" => db_v2::Error::InvalidQuery(m),
                "not_found" => db_v2::Error::NotFound,
                "conflict" => db_v2::Error::Conflict(m),
                "quota_exceeded" => db_v2::Error::QuotaExceeded(m),
                "timeout" => db_v2::Error::Timeout,
                "backend" => db_v2::Error::Backend(m),
                // every gate / policy refusal (not_granted, feature_disabled, ...) is a denial
                other => db_v2::Error::Denied(format!("{other}: {m}")),
            }
        }
        other => db_v2::Error::Backend(other.to_string()),
    }
}

fn value_json(v: &db_v2::Value) -> Result<Value, db_v2::Error> {
    match v {
        db_v2::Value::NullValue => Ok(Value::Null),
        db_v2::Value::BoolValue(b) => Ok(json!(b)),
        db_v2::Value::IntValue(i) => Ok(json!(i)),
        db_v2::Value::TextValue(s) => Ok(json!(s)),
        db_v2::Value::FloatValue(_) | db_v2::Value::BytesValue(_) => {
            Err(db_v2::Error::InvalidValue(
                "db v2 accepts null/bool/int/text values only (use a decimal string for numeric)"
                    .to_string(),
            ))
        }
    }
}

fn json_value(v: &Value) -> db_v2::Value {
    match v {
        Value::Bool(b) => db_v2::Value::BoolValue(*b),
        Value::String(s) => db_v2::Value::TextValue(s.clone()),
        Value::Number(n) => n
            .as_i64()
            .map(db_v2::Value::IntValue)
            .unwrap_or(db_v2::Value::NullValue),
        _ => db_v2::Value::NullValue,
    }
}

fn columns_json(cols: &[db_v2::ColumnValue]) -> Result<Value, db_v2::Error> {
    let mut m = Map::new();
    for c in cols {
        if m.insert(c.column.clone(), value_json(&c.value)?).is_some() {
            return Err(db_v2::Error::InvalidColumn(format!(
                "column {:?} given twice",
                c.column
            )));
        }
    }
    Ok(Value::Object(m))
}

fn condition_json(c: &db_v2::Condition) -> Result<Value, db_v2::Error> {
    let cmp = |op: &str, cv: &db_v2::ColumnValue| -> Result<Value, db_v2::Error> {
        Ok(json!({"op": op, "column": cv.column, "value": value_json(&cv.value)?}))
    };
    match c {
        db_v2::Condition::Eq(cv) => cmp("eq", cv),
        db_v2::Condition::Ne(cv) => cmp("ne", cv),
        db_v2::Condition::Lt(cv) => cmp("lt", cv),
        db_v2::Condition::Le(cv) => cmp("le", cv),
        db_v2::Condition::Gt(cv) => cmp("gt", cv),
        db_v2::Condition::Ge(cv) => cmp("ge", cv),
        db_v2::Condition::InList(i) => Ok(json!({
            "op": "in", "column": i.column,
            "values": i.values.iter().map(value_json).collect::<Result<Vec<_>, _>>()?,
        })),
        db_v2::Condition::Prefix(cv) => match &cv.value {
            db_v2::Value::TextValue(s) => {
                Ok(json!({"op": "prefix", "column": cv.column, "value": s}))
            }
            _ => Err(db_v2::Error::InvalidValue(
                "a prefix operand must be text".to_string(),
            )),
        },
        db_v2::Condition::IsNull(col) => Ok(json!({"op": "is_null", "column": col})),
        db_v2::Condition::IsNotNull(col) => Ok(json!({"op": "is_not_null", "column": col})),
    }
}

fn filter_json(f: &[db_v2::Condition]) -> Result<Value, db_v2::Error> {
    Ok(Value::Array(
        f.iter()
            .map(condition_json)
            .collect::<Result<Vec<_>, _>>()?,
    ))
}

fn tx_op_json(op: &db_v2::TxOp) -> Result<Value, db_v2::Error> {
    Ok(match op {
        db_v2::TxOp::Insert(o) => {
            json!({"op": "insert", "table": o.table, "columns": columns_json(&o.columns)?})
        }
        db_v2::TxOp::Update(o) => json!({
            "op": "update", "table": o.table, "row_id": o.row_id,
            "expected_version": o.expected_version, "columns": columns_json(&o.columns)?,
        }),
        db_v2::TxOp::Delete(o) => json!({
            "op": "delete", "table": o.table, "row_id": o.row_id,
            "expected_version": o.expected_version,
        }),
        db_v2::TxOp::GuardVersion(o) => json!({
            "op": "guard_version", "table": o.table, "row_id": o.row_id,
            "expected_version": o.expected_version,
        }),
        db_v2::TxOp::GuardCount(g) => json!({
            "op": "guard_count", "table": g.table, "filter": filter_json(&g.filter)?,
            "min": g.min, "max": g.max,
        }),
    })
}

fn bad_result(what: &str) -> db_v2::Error {
    db_v2::Error::Backend(format!("malformed host-result: {what}"))
}

fn row_from(v: &Value) -> Result<db_v2::Row, db_v2::Error> {
    let s = |k: &str| {
        v.get(k)
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| bad_result(k))
    };
    let columns = v
        .get("columns")
        .and_then(Value::as_object)
        .ok_or_else(|| bad_result("columns"))?
        .iter()
        .map(|(k, val)| db_v2::ColumnValue {
            column: k.clone(),
            value: json_value(val),
        })
        .collect();
    Ok(db_v2::Row {
        table: s("table")?,
        row_id: s("row_id")?,
        version: v
            .get("version")
            .and_then(Value::as_u64)
            .ok_or_else(|| bad_result("version"))?,
        created_at: s("created_at")?,
        updated_at: s("updated_at")?,
        columns,
    })
}

async fn db_op(st: &mut ExecState, op: &'static str, args: Value) -> Result<Value, db_v2::Error> {
    call(st, CapabilityKind::Db, op, args)
        .await
        .map_err(db_error_from)
}

impl db_v2::Host for ExecState {
    async fn insert(
        &mut self,
        table: String,
        columns: Vec<db_v2::ColumnValue>,
    ) -> Result<db_v2::Row, db_v2::Error> {
        let args = json!({"table": table, "columns": columns_json(&columns)?});
        row_from(&db_op(self, "db2.insert", args).await?)
    }

    async fn get(&mut self, table: String, row_id: String) -> Result<db_v2::Row, db_v2::Error> {
        row_from(&db_op(self, "db2.get", json!({"table": table, "row_id": row_id})).await?)
    }

    async fn query(
        &mut self,
        request: db_v2::QueryRequest,
    ) -> Result<db_v2::QueryPage, db_v2::Error> {
        let args = json!({
            "table": request.table,
            "filter": filter_json(&request.filter)?,
            "order": request.order.as_ref().map(|o| json!({"column": o.column, "descending": o.descending})),
            "limit": request.limit,
            "cursor": request.cursor,
        });
        let v = db_op(self, "db2.query", args).await?;
        let rows = v
            .get("rows")
            .and_then(Value::as_array)
            .ok_or_else(|| bad_result("rows"))?
            .iter()
            .map(row_from)
            .collect::<Result<Vec<_>, _>>()?;
        let next_cursor = match v.get("next_cursor") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => return Err(bad_result("next_cursor")),
        };
        Ok(db_v2::QueryPage { rows, next_cursor })
    }

    async fn count(
        &mut self,
        table: String,
        filter: Vec<db_v2::Condition>,
    ) -> Result<u64, db_v2::Error> {
        let args = json!({"table": table, "filter": filter_json(&filter)?});
        db_op(self, "db2.count", args)
            .await?
            .get("count")
            .and_then(Value::as_u64)
            .ok_or_else(|| bad_result("count"))
    }

    async fn update(
        &mut self,
        table: String,
        row_id: String,
        expected_version: u64,
        columns: Vec<db_v2::ColumnValue>,
    ) -> Result<db_v2::Row, db_v2::Error> {
        let args = json!({
            "table": table, "row_id": row_id,
            "expected_version": expected_version, "columns": columns_json(&columns)?,
        });
        row_from(&db_op(self, "db2.update", args).await?)
    }

    async fn delete(
        &mut self,
        table: String,
        row_id: String,
        expected_version: u64,
    ) -> Result<(), db_v2::Error> {
        let args = json!({"table": table, "row_id": row_id, "expected_version": expected_version});
        db_op(self, "db2.delete", args).await.map(|_| ())
    }

    async fn transact(
        &mut self,
        ops: Vec<db_v2::TxOp>,
    ) -> Result<Vec<db_v2::TxResult>, db_v2::Error> {
        let args = json!({"ops": ops.iter().map(tx_op_json).collect::<Result<Vec<_>, _>>()?});
        let v = db_op(self, "db2.transact", args).await?;
        v.get("results")
            .and_then(Value::as_array)
            .ok_or_else(|| bad_result("results"))?
            .iter()
            .map(|r| {
                Ok(if r.is_null() {
                    db_v2::TxResult::Done
                } else {
                    db_v2::TxResult::Written(row_from(r)?)
                })
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn cv(c: &str, v: db_v2::Value) -> db_v2::ColumnValue {
        db_v2::ColumnValue {
            column: c.to_string(),
            value: v,
        }
    }

    #[test]
    fn query_filter_encodes_to_the_pinned_wire_shape() {
        let f = vec![
            db_v2::Condition::Ge(cv(
                "start_at",
                db_v2::Value::TextValue("2026-01-01T00:00:00Z".into()),
            )),
            db_v2::Condition::InList(db_v2::InCondition {
                column: "kind".into(),
                values: vec![db_v2::Value::IntValue(1), db_v2::Value::IntValue(2)],
            }),
            db_v2::Condition::Prefix(cv("title", db_v2::Value::TextValue("ab".into()))),
            db_v2::Condition::IsNull("note".into()),
        ];
        assert_eq!(
            filter_json(&f).unwrap(),
            json!([
                {"op": "ge", "column": "start_at", "value": "2026-01-01T00:00:00Z"},
                {"op": "in", "column": "kind", "values": [1, 2]},
                {"op": "prefix", "column": "title", "value": "ab"},
                {"op": "is_null", "column": "note"}
            ])
        );
    }

    #[test]
    fn transact_ops_encode_to_the_pinned_wire_shape() {
        let ops = [
            db_v2::TxOp::Insert(db_v2::InsertOp {
                table: "t".into(),
                columns: vec![cv("a", db_v2::Value::IntValue(1))],
            }),
            db_v2::TxOp::GuardCount(db_v2::GuardCount {
                table: "t".into(),
                filter: vec![],
                min: 0,
                max: 4,
            }),
            db_v2::TxOp::Delete(db_v2::RowVersionOp {
                table: "t".into(),
                row_id: "r".into(),
                expected_version: 3,
            }),
        ];
        let got: Vec<Value> = ops.iter().map(|o| tx_op_json(o).unwrap()).collect();
        assert_eq!(got[0], json!({"op":"insert","table":"t","columns":{"a":1}}));
        assert_eq!(
            got[1],
            json!({"op":"guard_count","table":"t","filter":[],"min":0,"max":4})
        );
        assert_eq!(
            got[2],
            json!({"op":"delete","table":"t","row_id":"r","expected_version":3})
        );
    }

    #[test]
    fn unsupported_values_and_duplicate_columns_fail_loud_before_the_wire() {
        assert!(matches!(
            value_json(&db_v2::Value::FloatValue(1.5)),
            Err(db_v2::Error::InvalidValue(_))
        ));
        assert!(matches!(
            value_json(&db_v2::Value::BytesValue(vec![1])),
            Err(db_v2::Error::InvalidValue(_))
        ));
        let dup = vec![
            cv("a", db_v2::Value::NullValue),
            cv("a", db_v2::Value::NullValue),
        ];
        assert!(matches!(
            columns_json(&dup),
            Err(db_v2::Error::InvalidColumn(_))
        ));
        assert!(matches!(
            condition_json(&db_v2::Condition::Prefix(cv(
                "t",
                db_v2::Value::IntValue(1)
            ))),
            Err(db_v2::Error::InvalidValue(_))
        ));
    }

    #[test]
    fn row_decodes_the_stage_shape() {
        let r = row_from(&json!({
            "table": "t", "row_id": "id", "version": 2,
            "created_at": "c", "updated_at": "u", "columns": {"a": 1, "b": null, "c": "x", "d": true}
        }))
        .unwrap();
        assert_eq!((r.table.as_str(), r.version), ("t", 2));
        assert_eq!(r.columns.len(), 4);
        assert!(row_from(&json!({"table": "t"})).is_err());
    }

    #[test]
    fn error_codes_map_to_the_wit_variants() {
        let denied = |code: &str, m: &str| ExecutorError::HostCallDenied {
            capability: "db",
            op: "db2.test",
            code: code.into(),
            message: m.into(),
        };
        assert!(matches!(
            db_error_from(denied("conflict", "x")),
            db_v2::Error::Conflict(_)
        ));
        assert!(matches!(
            db_error_from(denied("quota_exceeded", "x")),
            db_v2::Error::QuotaExceeded(_)
        ));
        assert!(matches!(
            db_error_from(denied("not_found", "x")),
            db_v2::Error::NotFound
        ));
        assert!(matches!(
            db_error_from(denied("invalid_query", "x")),
            db_v2::Error::InvalidQuery(_)
        ));
        assert!(matches!(
            db_error_from(denied("not_granted", "x")),
            db_v2::Error::Denied(_)
        ));
        assert!(matches!(
            kv_error_from(denied("too_large", "70000")),
            kv_v2::Error::TooLarge(70000)
        ));
        assert!(matches!(
            kv_error_from(denied("rate_limited", "x")),
            kv_v2::Error::QuotaExceeded(_)
        ));
        assert!(matches!(
            kv_error_from(denied("invalid_key", "x")),
            kv_v2::Error::Invalid(_)
        ));
    }
}

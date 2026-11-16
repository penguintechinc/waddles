//! Music requests SERVICE bundle: the single per-community song-request queue.
//! All policy lives in [`logic`] (host-testable behind `DbV2Transport`/`Clock`/`Platform`
//! seams); this file only adapts the generated `db-v2`/`clock`/`context-v2` imports.
#[cfg(target_arch = "wasm32")]
#[allow(warnings)]
mod bindings;
pub mod logic;
#[cfg(target_arch = "wasm32")]
mod component {
    use std::sync::{Arc, OnceLock};

    use super::bindings::exports::waddle::bundle::tool_provider::Guest;
    use super::bindings::waddle::bundle::db::Value as WitValue;
    use super::bindings::waddle::bundle::tool::ToolError as WitError;
    use super::bindings::waddle::bundle::{clock, context_v2, db_v2, log};
    use super::logic::{self, Clock, Platform, Service};
    use waddle_sdk::db::Value;
    use waddle_sdk::storage_v2::{
        Cond, DbV2Error, DbV2Transport, Page, Query, Row, TxOp, TxResult,
    };
    use waddle_sdk::tool::{ToolError, ToolRegistry};

    pub struct Component;

    /// Real `db-v2` host import behind the SDK transport seam.
    struct WasmDb;

    /// SDK value to WIT value.
    fn to_wit_value(v: &Value) -> WitValue {
        match v {
            Value::Null => WitValue::NullValue,
            Value::Bool(b) => WitValue::BoolValue(*b),
            Value::Int(i) => WitValue::IntValue(*i),
            Value::Float(f) => WitValue::FloatValue(*f),
            Value::Text(t) => WitValue::TextValue(t.clone()),
            Value::Bytes(b) => WitValue::BytesValue(b.clone()),
        }
    }

    /// WIT value to SDK value.
    fn from_wit_value(v: WitValue) -> Value {
        match v {
            WitValue::NullValue => Value::Null,
            WitValue::BoolValue(b) => Value::Bool(b),
            WitValue::IntValue(i) => Value::Int(i),
            WitValue::FloatValue(f) => Value::Float(f),
            WitValue::TextValue(t) => Value::Text(t),
            WitValue::BytesValue(b) => Value::Bytes(b),
        }
    }

    /// Column list to WIT.
    fn cols(c: &[(String, Value)]) -> Vec<db_v2::ColumnValue> {
        c.iter()
            .map(|(column, v)| db_v2::ColumnValue {
                column: column.clone(),
                value: to_wit_value(v),
            })
            .collect()
    }

    /// SDK condition to WIT condition.
    fn cond(c: &Cond) -> db_v2::Condition {
        let cv = |column: &str, v: &Value| db_v2::ColumnValue {
            column: column.to_string(),
            value: to_wit_value(v),
        };
        match c {
            Cond::Eq(c, v) => db_v2::Condition::Eq(cv(c, v)),
            Cond::Ne(c, v) => db_v2::Condition::Ne(cv(c, v)),
            Cond::Lt(c, v) => db_v2::Condition::Lt(cv(c, v)),
            Cond::Le(c, v) => db_v2::Condition::Le(cv(c, v)),
            Cond::Gt(c, v) => db_v2::Condition::Gt(cv(c, v)),
            Cond::Ge(c, v) => db_v2::Condition::Ge(cv(c, v)),
            Cond::In(c, vs) => db_v2::Condition::InList(db_v2::InCondition {
                column: c.clone(),
                values: vs.iter().map(to_wit_value).collect(),
            }),
            Cond::Prefix(c, p) => db_v2::Condition::Prefix(cv(c, &Value::Text(p.clone()))),
            Cond::IsNull(c) => db_v2::Condition::IsNull(c.clone()),
            Cond::IsNotNull(c) => db_v2::Condition::IsNotNull(c.clone()),
        }
    }

    /// Filter list to WIT.
    fn conds(f: &[Cond]) -> Vec<db_v2::Condition> {
        f.iter().map(cond).collect()
    }

    /// WIT row to SDK row.
    fn row(r: db_v2::Row) -> Row {
        Row {
            table: r.table,
            row_id: r.row_id,
            version: r.version,
            created_at: r.created_at,
            updated_at: r.updated_at,
            columns: r
                .columns
                .into_iter()
                .map(|c| (c.column, from_wit_value(c.value)))
                .collect(),
        }
    }

    /// WIT error to SDK error.
    fn err(e: db_v2::Error) -> DbV2Error {
        match e {
            db_v2::Error::Denied(m) => DbV2Error::Denied(m),
            db_v2::Error::InvalidTable(m) => DbV2Error::InvalidTable(m),
            db_v2::Error::InvalidColumn(m) => DbV2Error::InvalidColumn(m),
            db_v2::Error::InvalidValue(m) => DbV2Error::InvalidValue(m),
            db_v2::Error::InvalidQuery(m) => DbV2Error::InvalidQuery(m),
            db_v2::Error::NotFound => DbV2Error::NotFound,
            db_v2::Error::Conflict(m) => DbV2Error::Conflict(m),
            db_v2::Error::QuotaExceeded(m) => DbV2Error::QuotaExceeded(m),
            db_v2::Error::Timeout => DbV2Error::Timeout,
            db_v2::Error::Backend(m) => DbV2Error::Backend(m),
        }
    }

    /// SDK transaction op to WIT op.
    fn tx_op(op: &TxOp) -> db_v2::TxOp {
        match op {
            TxOp::Insert { table, columns } => db_v2::TxOp::Insert(db_v2::InsertOp {
                table: table.clone(),
                columns: cols(columns),
            }),
            TxOp::Update {
                table,
                row_id,
                expected_version,
                columns,
            } => db_v2::TxOp::Update(db_v2::UpdateOp {
                table: table.clone(),
                row_id: row_id.clone(),
                expected_version: *expected_version,
                columns: cols(columns),
            }),
            TxOp::Delete {
                table,
                row_id,
                expected_version,
            } => db_v2::TxOp::Delete(db_v2::RowVersionOp {
                table: table.clone(),
                row_id: row_id.clone(),
                expected_version: *expected_version,
            }),
            TxOp::GuardVersion {
                table,
                row_id,
                expected_version,
            } => db_v2::TxOp::GuardVersion(db_v2::RowVersionOp {
                table: table.clone(),
                row_id: row_id.clone(),
                expected_version: *expected_version,
            }),
            TxOp::GuardCount {
                table,
                filter,
                min,
                max,
            } => db_v2::TxOp::GuardCount(db_v2::GuardCount {
                table: table.clone(),
                filter: conds(filter),
                min: *min,
                max: *max,
            }),
        }
    }

    impl DbV2Transport for WasmDb {
        fn insert(&self, table: &str, columns: Vec<(String, Value)>) -> Result<Row, DbV2Error> {
            db_v2::insert(table, &cols(&columns)).map(row).map_err(err)
        }
        fn get(&self, table: &str, row_id: &str) -> Result<Row, DbV2Error> {
            db_v2::get(table, row_id).map(row).map_err(err)
        }
        fn query(&self, q: &Query) -> Result<Page, DbV2Error> {
            let req = db_v2::QueryRequest {
                table: q.table.clone(),
                filter: conds(&q.filter),
                order: q.order.as_ref().map(|(c, d)| db_v2::Order {
                    column: c.clone(),
                    descending: *d,
                }),
                limit: q.limit,
                cursor: q.cursor.clone(),
            };
            db_v2::query(&req)
                .map(|p| Page {
                    rows: p.rows.into_iter().map(row).collect(),
                    next_cursor: p.next_cursor,
                })
                .map_err(err)
        }
        fn count(&self, table: &str, filter: &[Cond]) -> Result<u64, DbV2Error> {
            db_v2::count(table, &conds(filter)).map_err(err)
        }
        fn update(
            &self,
            table: &str,
            row_id: &str,
            expected_version: u64,
            columns: Vec<(String, Value)>,
        ) -> Result<Row, DbV2Error> {
            db_v2::update(table, row_id, expected_version, &cols(&columns))
                .map(row)
                .map_err(err)
        }
        fn delete(
            &self,
            table: &str,
            row_id: &str,
            expected_version: u64,
        ) -> Result<(), DbV2Error> {
            db_v2::delete(table, row_id, expected_version).map_err(err)
        }
        fn transact(&self, ops: &[TxOp]) -> Result<Vec<TxResult>, DbV2Error> {
            let wit: Vec<db_v2::TxOp> = ops.iter().map(tx_op).collect();
            db_v2::transact(&wit)
                .map(|rs| {
                    rs.into_iter()
                        .map(|r| match r {
                            db_v2::TxResult::Written(w) => TxResult::Written(row(w)),
                            db_v2::TxResult::Done => TxResult::Done,
                        })
                        .collect()
                })
                .map_err(err)
        }
    }

    /// Host wall clock.
    struct WasmClock;

    impl Clock for WasmClock {
        /// Milliseconds since the Unix epoch as the stage sees it.
        fn now_millis(&self) -> u64 {
            clock::now_millis()
        }
    }

    /// Authoritative scope and platform URL from `context-v2`.
    struct WasmPlatform;

    impl Platform for WasmPlatform {
        /// The host-verified numeric community id (0 = no scope).
        fn community_id(&self) -> i32 {
            context_v2::get_context().community_id
        }
        /// The platform web base URL the host folds into the context config, if set.
        fn web_base_url(&self) -> Option<String> {
            let ctx = context_v2::get_context();
            serde_json::from_str::<serde_json::Value>(&ctx.config_json)
                .ok()
                .and_then(|v| {
                    v.get("web_base_url")
                        .and_then(|u| u.as_str().map(str::to_owned))
                })
        }
    }

    /// Maps the SDK tool error onto the WIT one.
    fn to_wit(e: ToolError) -> WitError {
        match e {
            ToolError::NotGranted => WitError::NotGranted,
            ToolError::Forbidden => WitError::Forbidden,
            ToolError::Timeout => WitError::Timeout,
            ToolError::DepthExceeded => WitError::DepthExceeded,
            ToolError::PoolExhausted => WitError::PoolExhausted,
            ToolError::TargetUnavailable => WitError::TargetUnavailable,
            ToolError::InvalidArgs(m) => WitError::InvalidArgs(m),
            ToolError::NoTransport => {
                WitError::InvalidArgs("no tool transport installed".to_string())
            }
        }
    }

    /// Emits one PII-free line: tool label and outcome code only (never args or actor).
    fn log_outcome(tool: &str, outcome: &str) {
        let fields = serde_json::json!({ "tool": tool, "outcome": outcome }).to_string();
        log::write(log::Level::Debug, "music-requests tool call", &fields);
    }

    static REGISTRY: OnceLock<ToolRegistry> = OnceLock::new();

    /// Lazily builds the tool table over the real host adapters.
    fn registry() -> &'static ToolRegistry {
        REGISTRY.get_or_init(|| {
            logic::build_registry(Arc::new(Service::new(
                Arc::new(WasmDb),
                Arc::new(WasmClock),
                Arc::new(WasmPlatform),
            )))
        })
    }

    impl Guest for Component {
        /// Routes `name` through the tool table; unknown tools are a typed error.
        fn call(name: String, args: Vec<u8>) -> Result<Vec<u8>, WitError> {
            let result = registry().call(&name, &args);
            log_outcome(
                logic::tool_log_label(&name),
                match &result {
                    Ok(_) => "ok",
                    Err(ToolError::InvalidArgs(m)) => logic::outcome_code(m),
                    Err(ToolError::NotGranted) => "not_granted",
                    Err(ToolError::Forbidden) => "forbidden",
                    Err(_) => "error",
                },
            );
            result.map_err(to_wit)
        }
    }
}

#[cfg(target_arch = "wasm32")]
use component::Component;
#[cfg(target_arch = "wasm32")]
bindings::export!(Component with_types_in bindings);

//! Idiomatic wrapper over the WIT `db` interface -- structured
//! `insert`/`get`/`query`/`update`/`delete` ops against the bundle's own
//! single table, granted only when the bundle manifest's `data.tables` is
//! non-empty; `wit/waddle-bundle/stage.wit` `interface db`. **No raw SQL
//! crosses this boundary, ever** (design doc SS1 round-1 CRITICAL finding)
//! -- the earlier `execute(statement, params)` shape is retired.
//!
//! There is deliberately no query-builder facade here (unlike `waddle-sdk`
//! (Python), which reproduces the full `penguin-dal` API to keep ~16
//! existing bundles byte-for-byte unchanged, D21). Rust bundles carry no
//! such compatibility obligation (spec SS6.5/Q7), so this module stays a
//! thin, typed wrapper over the five structured ops.

#[cfg(target_arch = "wasm32")]
use crate::error::SdkError;

/// One SQL parameter/column value. Mirrors WIT `interface db`'s `variant
/// value`.
#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
}

impl From<bool> for Value {
    fn from(v: bool) -> Self {
        Value::Bool(v)
    }
}

impl From<i64> for Value {
    fn from(v: i64) -> Self {
        Value::Int(v)
    }
}

impl From<f64> for Value {
    fn from(v: f64) -> Self {
        Value::Float(v)
    }
}

impl From<String> for Value {
    fn from(v: String) -> Self {
        Value::Text(v)
    }
}

impl From<&str> for Value {
    fn from(v: &str) -> Self {
        Value::Text(v.to_string())
    }
}

impl From<Vec<u8>> for Value {
    fn from(v: Vec<u8>) -> Self {
        Value::Bytes(v)
    }
}

impl<T> From<Option<T>> for Value
where
    Value: From<T>,
{
    fn from(v: Option<T>) -> Self {
        match v {
            Some(inner) => Value::from(inner),
            None => Value::Null,
        }
    }
}

impl Value {
    pub fn as_text(&self) -> Option<&str> {
        match self {
            Value::Text(s) => Some(s.as_str()),
            _ => None,
        }
    }

    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(v) => Some(*v),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(v) => Some(*v),
            _ => None,
        }
    }

    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }
}

/// One `(column, value)` pair -- WIT has no map type, so `insert`/`update`'s
/// column-values are carried as a list of these. Mirrors WIT `record
/// column-value`.
#[derive(Debug, Clone, PartialEq)]
pub struct ColumnValue {
    pub column: String,
    pub value: Value,
}

impl ColumnValue {
    pub fn new(column: impl Into<String>, value: impl Into<Value>) -> Self {
        ColumnValue {
            column: column.into(),
            value: value.into(),
        }
    }
}

/// One row as returned by the host. Mirrors WIT `record row`: `row_id`/
/// `version` are the platform-owned identity/optimistic-concurrency
/// columns, `columns` echoes back exactly the declared-column values.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub row_id: String,
    pub version: u64,
    pub columns: Vec<ColumnValue>,
}

impl Row {
    /// The value of `column`, if present on this row.
    pub fn get(&self, column: &str) -> Option<&Value> {
        self.columns
            .iter()
            .find(|cv| cv.column == column)
            .map(|cv| &cv.value)
    }
}

/// A declared-column (or fixed platform-column) sort key. Mirrors WIT
/// `record order-column`.
#[derive(Debug, Clone, PartialEq)]
pub struct OrderColumn {
    pub name: String,
    pub descending: bool,
}

/// `query`'s row ordering. Mirrors WIT `variant order-by`. Omitted (`None`)
/// defaults to the host's own stable `row-id` ascending.
#[derive(Debug, Clone, PartialEq)]
pub enum OrderBy {
    Column(OrderColumn),
    Random,
}

/// Inserts one row; returns the platform-assigned `row_id`/`version`
/// alongside the stored columns.
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn insert(column_values: Vec<ColumnValue>) -> Result<Row, SdkError> {
    crate::bindings_glue::db_insert(column_values)
}

/// Fetches one row by its platform `row_id`.
#[cfg(target_arch = "wasm32")]
pub fn get(row_id: &str) -> Result<Row, SdkError> {
    crate::bindings_glue::db_get(row_id)
}

/// Bounded, orderable list of this bundle's own rows. `limit` is clamped
/// host-side regardless of what is requested; `order_by` omitted (`None`)
/// defaults to stable `row_id` ascending.
#[cfg(target_arch = "wasm32")]
pub fn query(limit: u32, offset: u32, order_by: Option<OrderBy>) -> Result<Vec<Row>, SdkError> {
    crate::bindings_glue::db_query(limit, offset, order_by)
}

/// Updates one row, gated on `expected_version` (optimistic concurrency) --
/// a mismatch or missing row returns `conflict`/`not-found`.
#[cfg(target_arch = "wasm32")]
pub fn update(
    row_id: &str,
    expected_version: u64,
    column_values: Vec<ColumnValue>,
) -> Result<Row, SdkError> {
    crate::bindings_glue::db_update(row_id, expected_version, column_values)
}

/// Deletes one row, gated on `expected_version` the same way as [`update`].
#[cfg(target_arch = "wasm32")]
pub fn delete(row_id: &str, expected_version: u64) -> Result<(), SdkError> {
    crate::bindings_glue::db_delete(row_id, expected_version)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn value_from_conversions_cover_every_scalar() {
        assert_eq!(Value::from(true), Value::Bool(true));
        assert_eq!(Value::from(7i64), Value::Int(7));
        assert_eq!(Value::from(1.5f64), Value::Float(1.5));
        assert_eq!(Value::from("hi"), Value::Text("hi".to_string()));
        assert_eq!(Value::from(vec![1u8, 2]), Value::Bytes(vec![1, 2]));
    }

    #[test]
    fn value_from_option_none_is_null() {
        let v: Value = Option::<i64>::None.into();
        assert!(v.is_null());
        let v: Value = Some(5i64).into();
        assert_eq!(v, Value::Int(5));
    }

    #[test]
    fn value_accessors_return_none_for_wrong_variant() {
        let v = Value::Text("x".to_string());
        assert_eq!(v.as_int(), None);
        assert_eq!(v.as_text(), Some("x"));
    }

    fn sample_row() -> Row {
        Row {
            row_id: "00000000-0000-0000-0000-000000000001".to_string(),
            version: 1,
            columns: vec![
                ColumnValue::new("id", 1i64),
                ColumnValue::new("alias", "sr"),
            ],
        }
    }

    #[test]
    fn row_get_looks_up_by_column_name() {
        let row = sample_row();
        assert_eq!(row.get("alias"), Some(&Value::Text("sr".to_string())));
        assert_eq!(row.get("missing"), None);
    }

    #[test]
    fn column_value_new_converts_into_value() {
        let cv = ColumnValue::new("note", Option::<i64>::None);
        assert_eq!(cv.column, "note");
        assert!(cv.value.is_null());
    }

    #[test]
    fn order_by_variants_are_distinct() {
        let by_column = OrderBy::Column(OrderColumn {
            name: "created_at".to_string(),
            descending: true,
        });
        assert_ne!(by_column, OrderBy::Random);
    }
}

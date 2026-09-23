//! Idiomatic wrapper over the WIT `db` interface (parameterized SQL,
//! granted only when the bundle manifest's `data.tables` is non-empty;
//! `wit/waddle-bundle/stage.wit` `interface db`).
//!
//! There is deliberately no query-builder facade here (unlike `waddle-sdk`
//! (Python), which reproduces the full `penguin-dal` API to keep ~16
//! existing bundles byte-for-byte unchanged, D21). Rust bundles carry no
//! such compatibility obligation (spec SS6.5/Q7), so this module stays a
//! thin, typed wrapper over `execute(statement, params)`.

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

/// The result of a successful `execute`. Mirrors WIT `record rows`.
#[derive(Debug, Clone, PartialEq)]
pub struct Rows {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<Value>>,
    pub rows_affected: u64,
}

impl Rows {
    /// The zero-based index of `column`, if present.
    pub fn column_index(&self, column: &str) -> Option<usize> {
        self.columns.iter().position(|c| c == column)
    }

    /// The value at `(row, column)`, or `None` if either is out of range.
    pub fn get(&self, row: usize, column: &str) -> Option<&Value> {
        let idx = self.column_index(column)?;
        self.rows.get(row)?.get(idx)
    }

    pub fn len(&self) -> usize {
        self.rows.len()
    }

    pub fn is_empty(&self) -> bool {
        self.rows.is_empty()
    }
}

/// Executes `statement` (with `$1..$n` placeholders) against `params`
/// under the bundle's own Postgres role, row-level-security scoped to the
/// envelope's tenant/community.
///
/// Only compiles for `wasm32` targets -- see `crate::bindings_glue`'s
/// module doc comment for the resulting host coverage carve-out.
#[cfg(target_arch = "wasm32")]
pub fn execute(statement: &str, params: &[Value]) -> Result<Rows, SdkError> {
    crate::bindings_glue::db_execute(statement, params)
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

    fn sample_rows() -> Rows {
        Rows {
            columns: vec!["id".to_string(), "alias".to_string()],
            rows: vec![
                vec![Value::Int(1), Value::Text("sr".to_string())],
                vec![Value::Int(2), Value::Null],
            ],
            rows_affected: 2,
        }
    }

    #[test]
    fn rows_get_looks_up_by_column_name() {
        let rows = sample_rows();
        assert_eq!(rows.get(0, "alias"), Some(&Value::Text("sr".to_string())));
        assert_eq!(rows.get(1, "alias"), Some(&Value::Null));
        assert_eq!(rows.get(0, "missing"), None);
        assert_eq!(rows.get(5, "id"), None);
    }

    #[test]
    fn rows_len_and_is_empty() {
        let rows = sample_rows();
        assert_eq!(rows.len(), 2);
        assert!(!rows.is_empty());
        assert!(
            Rows {
                columns: vec![],
                rows: vec![],
                rows_affected: 0
            }
            .is_empty()
        );
    }
}

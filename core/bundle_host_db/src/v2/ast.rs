//! `db` v2 structured query AST and result types. This is the ONLY way a guest
//! expresses a query: there is no SQL text anywhere in it, every identifier is
//! a *name* validated against the declared schema, and every value is bound as
//! a typed parameter by [`super::plan`].

use crate::backend::{DbError, DbValue};

/// A comparison operator for [`Condition::Cmp`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CmpOp {
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
}

impl CmpOp {
    pub(crate) fn sql(self) -> &'static str {
        match self {
            CmpOp::Eq => "=",
            CmpOp::Ne => "<>",
            CmpOp::Lt => "<",
            CmpOp::Le => "<=",
            CmpOp::Gt => ">",
            CmpOp::Ge => ">=",
        }
    }

    pub(crate) fn is_ordering(self) -> bool {
        matches!(self, CmpOp::Lt | CmpOp::Le | CmpOp::Gt | CmpOp::Ge)
    }
}

/// One predicate; a filter is a list of these joined by `AND`.
#[derive(Debug, Clone, PartialEq)]
pub enum Condition {
    Cmp {
        column: String,
        op: CmpOp,
        value: DbValue,
    },
    In {
        column: String,
        values: Vec<DbValue>,
    },
    /// `text` column starts with `prefix` (literal; `%`/`_` are escaped).
    Prefix {
        column: String,
        prefix: String,
    },
    IsNull {
        column: String,
    },
    IsNotNull {
        column: String,
    },
}

impl Condition {
    pub(crate) fn column(&self) -> &str {
        match self {
            Condition::Cmp { column, .. }
            | Condition::In { column, .. }
            | Condition::Prefix { column, .. }
            | Condition::IsNull { column }
            | Condition::IsNotNull { column } => column,
        }
    }
}

/// `ORDER BY` an indexed, non-null column (or a platform column).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Order {
    pub column: String,
    pub descending: bool,
}

/// A filtered, ordered, cursor-paginated read of one logical table.
#[derive(Debug, Clone, PartialEq)]
pub struct QueryRequest {
    pub table: String,
    pub filter: Vec<Condition>,
    /// `None` = `row_id` ascending.
    pub order: Option<Order>,
    /// `1..=MAX_QUERY_LIMIT`.
    pub limit: u32,
    /// Opaque cursor from a previous page of the SAME query.
    pub cursor: Option<String>,
}

/// One row as returned to the guest.
#[derive(Debug, Clone, PartialEq)]
pub struct RowV2 {
    pub table: String,
    pub row_id: String,
    pub version: u64,
    /// RFC 3339 UTC, microsecond precision.
    pub created_at: String,
    pub updated_at: String,
    pub columns: Vec<(String, DbValue)>,
}

/// One page of a query.
#[derive(Debug, Clone, PartialEq)]
pub struct QueryPage {
    pub rows: Vec<RowV2>,
    /// `Some` iff more rows follow.
    pub next_cursor: Option<String>,
}

/// One operation inside a `transact` batch.
#[derive(Debug, Clone, PartialEq)]
pub enum TxOp {
    Insert {
        table: String,
        columns: Vec<(String, DbValue)>,
    },
    Update {
        table: String,
        row_id: String,
        expected_version: u64,
        columns: Vec<(String, DbValue)>,
    },
    Delete {
        table: String,
        row_id: String,
        expected_version: u64,
    },
    /// Locks the row (`FOR UPDATE`) and fails the batch unless its version matches.
    GuardVersion {
        table: String,
        row_id: String,
        expected_version: u64,
    },
    /// Fails the batch unless `min <= count(filter) <= max`.
    GuardCount {
        table: String,
        filter: Vec<Condition>,
        min: u64,
        max: u64,
    },
}

/// Per-op result of a committed `transact`.
#[derive(Debug, Clone, PartialEq)]
pub enum TxOutcome {
    Row(RowV2),
    Done,
}

/// The `db` v2 error surface (maps 1:1 onto the WIT `db-v2.error`).
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum DbV2Error {
    #[error("denied: {0}")]
    Denied(String),
    #[error("invalid table: {0}")]
    InvalidTable(String),
    #[error("invalid column: {0}")]
    InvalidColumn(String),
    #[error("invalid value: {0}")]
    InvalidValue(String),
    #[error("invalid query: {0}")]
    InvalidQuery(String),
    #[error("row not found")]
    NotFound,
    #[error("conflict: {0}")]
    Conflict(String),
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    #[error("operation timed out")]
    Timeout,
    #[error("backend error: {0}")]
    Backend(String),
}

impl DbV2Error {
    /// Stable machine code for logs, metrics and the wire.
    pub fn code(&self) -> &'static str {
        match self {
            DbV2Error::Denied(_) => "denied",
            DbV2Error::InvalidTable(_) => "invalid_table",
            DbV2Error::InvalidColumn(_) => "invalid_column",
            DbV2Error::InvalidValue(_) => "invalid_value",
            DbV2Error::InvalidQuery(_) => "invalid_query",
            DbV2Error::NotFound => "not_found",
            DbV2Error::Conflict(_) => "conflict",
            DbV2Error::QuotaExceeded(_) => "quota_exceeded",
            DbV2Error::Timeout => "timeout",
            DbV2Error::Backend(_) => "backend",
        }
    }

    /// Human message carried alongside [`Self::code`] on the wire.
    pub fn message(&self) -> String {
        match self {
            DbV2Error::NotFound => "row not found".to_string(),
            DbV2Error::Timeout => "operation timed out".to_string(),
            DbV2Error::Denied(m)
            | DbV2Error::InvalidTable(m)
            | DbV2Error::InvalidColumn(m)
            | DbV2Error::InvalidValue(m)
            | DbV2Error::InvalidQuery(m)
            | DbV2Error::Conflict(m)
            | DbV2Error::QuotaExceeded(m)
            | DbV2Error::Backend(m) => m.clone(),
        }
    }
}

impl From<DbError> for DbV2Error {
    fn from(e: DbError) -> Self {
        match e {
            DbError::NoTable => DbV2Error::InvalidTable("no such table".into()),
            DbError::InvalidColumn(m) => DbV2Error::InvalidColumn(m),
            DbError::InvalidValue(m) => DbV2Error::InvalidValue(m),
            DbError::NotFound => DbV2Error::NotFound,
            DbError::Conflict => DbV2Error::Conflict("version conflict".into()),
            DbError::QuotaExceeded(m) => DbV2Error::QuotaExceeded(m),
            DbError::Timeout => DbV2Error::Timeout,
            DbError::Backend(m) => DbV2Error::Backend(m),
        }
    }
}

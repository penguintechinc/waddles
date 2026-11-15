//! Ergonomic, host-testable helpers over the additive `kv-v2` / `db-v2` WIT
//! imports (`wit/waddle-bundle/storage-v2.wit`; design `docs/bundle-storage-v2.md`).
//!
//! The `stage-next` / `stage-v2` bindings live in the BUNDLE crate (this crate's
//! `bindings_glue` generates only the frozen `stage` world), so -- exactly like
//! [`crate::tool`] -- the host imports are reached through a transport seam: the
//! bundle implements [`KvV2Transport`] / [`DbV2Transport`] as a thin adapter over
//! its generated `kv_v2` / `db_v2` calls, and everything here (query builder,
//! cursor-following, CAS retry loops, transaction builder) is plain Rust that is
//! unit-tested on the host. There is **no SQL anywhere**: a query is a typed
//! value naming declared tables and columns.
//!
//! ```ignore
//! let page = Query::table("events")
//!     .filter(Cond::ge("start_at", "2026-04-01T00:00:00Z"))
//!     .filter(Cond::lt("start_at", "2026-05-01T00:00:00Z"))
//!     .order_by("start_at", false)
//!     .limit(50);
//! let events = query_all(&transport, page, 20)?; // follows cursors
//! ```

use thiserror::Error;

use crate::db::Value;

/// Safety valve for the cursor-following helpers: never loop unboundedly.
pub const DEFAULT_MAX_PAGES: u32 = 100;

// ---------------------------------------------------------------- kv-v2 ----

/// Mirrors WIT `kv-v2.error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum KvV2Error {
    /// Value or key too large (carries the offending size).
    #[error("too large: {0} bytes")]
    TooLarge(u64),
    /// Live-key or per-invocation (ops / bytes / lists) quota hit.
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    /// Malformed key, prefix, cursor or limit.
    #[error("invalid: {0}")]
    Invalid(String),
    /// Backend / transport failure.
    #[error("backend: {0}")]
    Backend(String),
    /// [`read_modify_write`] lost the CAS race on every attempt.
    #[error("compare-and-swap still conflicting after {0} attempts")]
    Contended(u32),
}

/// Result of a compare-and-swap.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CasResult {
    /// Written; the key's new version.
    Swapped(u64),
    /// Not written; the key's current version (0 = absent).
    Conflict(u64),
}

/// Result of a conditional delete.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DeleteResult {
    Deleted,
    /// Not deleted; the key's current version (0 = absent).
    Conflict(u64),
}

/// One page of keys.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KeyPage {
    pub keys: Vec<String>,
    pub next_cursor: Option<String>,
}

/// The `kv-v2` host import, as the bundle's generated bindings expose it.
pub trait KvV2Transport {
    /// `get-versioned`: value + opaque version token, or `None` when absent.
    fn get_versioned(&self, key: &str) -> Result<Option<(Vec<u8>, u64)>, KvV2Error>;
    /// `cas`: write iff the version equals `expected_version` (0 = create only).
    fn cas(
        &self,
        key: &str,
        expected_version: u64,
        value: &[u8],
        ttl_seconds: u32,
    ) -> Result<CasResult, KvV2Error>;
    /// `delete-if`: delete iff the version equals `expected_version` (>= 1).
    fn delete_if(&self, key: &str, expected_version: u64) -> Result<DeleteResult, KvV2Error>;
    /// `list-keys`: keys under `prefix` after `cursor` (limit 1..=200).
    fn list_keys(
        &self,
        prefix: &str,
        cursor: Option<&str>,
        limit: u32,
    ) -> Result<KeyPage, KvV2Error>;
}

/// Atomic read-modify-write with bounded retries: reads the key with its
/// version, applies `f` (which sees `None` for an absent key), and `cas`es the
/// result back; on a conflict it re-reads and retries up to `attempts` times.
/// Returns the value written. Lost updates are impossible: a concurrent writer
/// forces a retry instead of being overwritten.
pub fn read_modify_write<T: KvV2Transport>(
    t: &T,
    key: &str,
    ttl_seconds: u32,
    attempts: u32,
    mut f: impl FnMut(Option<Vec<u8>>) -> Vec<u8>,
) -> Result<Vec<u8>, KvV2Error> {
    for _ in 0..attempts {
        let (current, version) = match t.get_versioned(key)? {
            Some((v, ver)) => (Some(v), ver),
            None => (None, 0),
        };
        let next = f(current);
        if let CasResult::Swapped(_) = t.cas(key, version, &next, ttl_seconds)? {
            return Ok(next);
        }
    }
    Err(KvV2Error::Contended(attempts))
}

/// Collects every key under `prefix`, following cursors (at most `max_pages`).
pub fn list_all<T: KvV2Transport>(
    t: &T,
    prefix: &str,
    page_size: u32,
    max_pages: u32,
) -> Result<Vec<String>, KvV2Error> {
    let mut out = Vec::new();
    let mut cursor: Option<String> = None;
    for _ in 0..max_pages {
        let page = t.list_keys(prefix, cursor.as_deref(), page_size)?;
        out.extend(page.keys);
        match page.next_cursor {
            Some(c) => cursor = Some(c),
            None => return Ok(out),
        }
    }
    Err(KvV2Error::Invalid(format!(
        "listing under {prefix:?} exceeded {max_pages} pages"
    )))
}

// ---------------------------------------------------------------- db-v2 ----

/// Mirrors WIT `db-v2.error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
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
    /// Version mismatch, failed guard or unique violation -- re-read and retry.
    #[error("conflict: {0}")]
    Conflict(String),
    /// Row/byte quota for this bundle in this community is exhausted.
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    #[error("timed out")]
    Timeout,
    #[error("backend: {0}")]
    Backend(String),
    /// [`update_with_retry`] kept losing the version race.
    #[error("update still conflicting after {0} attempts")]
    Contended(u32),
}

/// One predicate; a query's filter is a list of these joined by `AND`.
#[derive(Debug, Clone, PartialEq)]
pub enum Cond {
    Eq(String, Value),
    Ne(String, Value),
    Lt(String, Value),
    Le(String, Value),
    Gt(String, Value),
    Ge(String, Value),
    In(String, Vec<Value>),
    /// text column starts with the string (literal; `%`/`_` are not wildcards).
    Prefix(String, String),
    IsNull(String),
    IsNotNull(String),
}

impl Cond {
    /// `column = value`.
    pub fn eq(column: &str, v: impl Into<Value>) -> Self {
        Cond::Eq(column.into(), v.into())
    }
    /// `column <> value`.
    pub fn ne(column: &str, v: impl Into<Value>) -> Self {
        Cond::Ne(column.into(), v.into())
    }
    /// `column < value`.
    pub fn lt(column: &str, v: impl Into<Value>) -> Self {
        Cond::Lt(column.into(), v.into())
    }
    /// `column <= value`.
    pub fn le(column: &str, v: impl Into<Value>) -> Self {
        Cond::Le(column.into(), v.into())
    }
    /// `column > value`.
    pub fn gt(column: &str, v: impl Into<Value>) -> Self {
        Cond::Gt(column.into(), v.into())
    }
    /// `column >= value`.
    pub fn ge(column: &str, v: impl Into<Value>) -> Self {
        Cond::Ge(column.into(), v.into())
    }
    /// `column IN (values...)` (1..=64 values).
    pub fn in_list<V: Into<Value>>(column: &str, values: impl IntoIterator<Item = V>) -> Self {
        Cond::In(column.into(), values.into_iter().map(Into::into).collect())
    }
    /// `column` starts with `prefix`.
    pub fn prefix(column: &str, prefix: &str) -> Self {
        Cond::Prefix(column.into(), prefix.into())
    }
}

/// A stored row.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub table: String,
    pub row_id: String,
    /// Optimistic-concurrency token; pass it back to `update` / `delete`.
    pub version: u64,
    pub created_at: String,
    pub updated_at: String,
    pub columns: Vec<(String, Value)>,
}

impl Row {
    /// The value of a declared column, if present.
    pub fn get(&self, column: &str) -> Option<&Value> {
        self.columns
            .iter()
            .find(|(c, _)| c == column)
            .map(|(_, v)| v)
    }
}

/// A filtered, ordered, cursor-paginated read. Build with [`Query::table`].
#[derive(Debug, Clone, PartialEq)]
pub struct Query {
    pub table: String,
    pub filter: Vec<Cond>,
    pub order: Option<(String, bool)>,
    pub limit: u32,
    pub cursor: Option<String>,
}

impl Query {
    /// A query over `table` (limit 50, `row_id` order, no filter).
    pub fn table(table: &str) -> Self {
        Self {
            table: table.into(),
            filter: Vec::new(),
            order: None,
            limit: 50,
            cursor: None,
        }
    }
    /// Adds one `AND`ed condition.
    pub fn filter(mut self, c: Cond) -> Self {
        self.filter.push(c);
        self
    }
    /// Orders by an indexed NOT NULL column (or a platform column).
    pub fn order_by(mut self, column: &str, descending: bool) -> Self {
        self.order = Some((column.into(), descending));
        self
    }
    /// Page size, 1..=200.
    pub fn limit(mut self, limit: u32) -> Self {
        self.limit = limit;
        self
    }
    /// Continues from a previous page's cursor.
    pub fn after(mut self, cursor: Option<String>) -> Self {
        self.cursor = cursor;
        self
    }
}

/// One page of a query.
#[derive(Debug, Clone, PartialEq)]
pub struct Page {
    pub rows: Vec<Row>,
    pub next_cursor: Option<String>,
}

/// One operation in a [`DbV2Transport::transact`] batch (atomic; 1..=16 ops).
#[derive(Debug, Clone, PartialEq)]
pub enum TxOp {
    Insert {
        table: String,
        columns: Vec<(String, Value)>,
    },
    Update {
        table: String,
        row_id: String,
        expected_version: u64,
        columns: Vec<(String, Value)>,
    },
    Delete {
        table: String,
        row_id: String,
        expected_version: u64,
    },
    /// Lock the row and fail the batch unless its version matches.
    GuardVersion {
        table: String,
        row_id: String,
        expected_version: u64,
    },
    /// Fail the batch unless `min <= count(filter) <= max`.
    GuardCount {
        table: String,
        filter: Vec<Cond>,
        min: u64,
        max: u64,
    },
}

/// Per-op result of a committed batch.
#[derive(Debug, Clone, PartialEq)]
pub enum TxResult {
    Written(Row),
    Done,
}

/// The `db-v2` host import, as the bundle's generated bindings expose it.
pub trait DbV2Transport {
    fn insert(&self, table: &str, columns: Vec<(String, Value)>) -> Result<Row, DbV2Error>;
    fn get(&self, table: &str, row_id: &str) -> Result<Row, DbV2Error>;
    fn query(&self, q: &Query) -> Result<Page, DbV2Error>;
    fn count(&self, table: &str, filter: &[Cond]) -> Result<u64, DbV2Error>;
    fn update(
        &self,
        table: &str,
        row_id: &str,
        expected_version: u64,
        columns: Vec<(String, Value)>,
    ) -> Result<Row, DbV2Error>;
    fn delete(&self, table: &str, row_id: &str, expected_version: u64) -> Result<(), DbV2Error>;
    fn transact(&self, ops: &[TxOp]) -> Result<Vec<TxResult>, DbV2Error>;
}

/// Runs `q` and follows cursors until exhausted, collecting every row (at most
/// `max_pages` pages -- an explicit error rather than an unbounded loop).
pub fn query_all<T: DbV2Transport>(t: &T, q: Query, max_pages: u32) -> Result<Vec<Row>, DbV2Error> {
    let mut rows = Vec::new();
    let mut q = q;
    for _ in 0..max_pages {
        let page = t.query(&q)?;
        rows.extend(page.rows);
        match page.next_cursor {
            Some(c) => q.cursor = Some(c),
            None => return Ok(rows),
        }
    }
    Err(DbV2Error::InvalidQuery(format!(
        "query exceeded {max_pages} pages; narrow the filter"
    )))
}

/// Optimistic read-modify-write on one row: reads it, lets `f` decide the new
/// column values (return `None` to leave it unchanged), and `update`s with the
/// row's version; on a version conflict it re-reads and retries up to
/// `attempts` times. Returns the stored row.
pub fn update_with_retry<T: DbV2Transport>(
    t: &T,
    table: &str,
    row_id: &str,
    attempts: u32,
    mut f: impl FnMut(&Row) -> Option<Vec<(String, Value)>>,
) -> Result<Row, DbV2Error> {
    for _ in 0..attempts {
        let row = t.get(table, row_id)?;
        let Some(columns) = f(&row) else {
            return Ok(row);
        };
        match t.update(table, row_id, row.version, columns) {
            Err(DbV2Error::Conflict(_)) => continue,
            other => return other,
        }
    }
    Err(DbV2Error::Contended(attempts))
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    use super::*;

    /// In-memory KV transport with the real version/CAS semantics.
    #[derive(Default)]
    struct MemKv {
        data: RefCell<BTreeMap<String, (Vec<u8>, u64)>>,
        next_version: RefCell<u64>,
        /// Simulates a concurrent writer: bumps the key between our read and CAS.
        interfere: RefCell<u32>,
    }

    impl MemKv {
        fn bump(&self, key: &str) {
            let mut v = self.next_version.borrow_mut();
            *v += 1;
            let ver = *v;
            let mut d = self.data.borrow_mut();
            let cur = match d.get(key) {
                Some((b, _)) => b.clone(),
                None => Vec::new(),
            };
            d.insert(key.to_string(), (cur, ver));
        }
    }

    impl KvV2Transport for MemKv {
        fn get_versioned(&self, key: &str) -> Result<Option<(Vec<u8>, u64)>, KvV2Error> {
            Ok(self.data.borrow().get(key).cloned())
        }
        fn cas(
            &self,
            key: &str,
            expected: u64,
            value: &[u8],
            _ttl: u32,
        ) -> Result<CasResult, KvV2Error> {
            if *self.interfere.borrow() > 0 {
                *self.interfere.borrow_mut() -= 1;
                self.bump(key);
            }
            let cur = self.data.borrow().get(key).map_or(0, |(_, v)| *v);
            if cur != expected {
                return Ok(CasResult::Conflict(cur));
            }
            let mut n = self.next_version.borrow_mut();
            *n += 1;
            let ver = *n;
            drop(n);
            self.data
                .borrow_mut()
                .insert(key.to_string(), (value.to_vec(), ver));
            Ok(CasResult::Swapped(ver))
        }
        fn delete_if(&self, key: &str, expected: u64) -> Result<DeleteResult, KvV2Error> {
            let cur = self.data.borrow().get(key).map_or(0, |(_, v)| *v);
            if cur != expected {
                return Ok(DeleteResult::Conflict(cur));
            }
            self.data.borrow_mut().remove(key);
            Ok(DeleteResult::Deleted)
        }
        fn list_keys(
            &self,
            prefix: &str,
            cursor: Option<&str>,
            limit: u32,
        ) -> Result<KeyPage, KvV2Error> {
            let keys: Vec<String> = self
                .data
                .borrow()
                .keys()
                .filter(|k| k.starts_with(prefix) && cursor.is_none_or(|c| k.as_str() > c))
                .take(limit as usize + 1)
                .cloned()
                .collect();
            let more = keys.len() > limit as usize;
            let keys: Vec<String> = keys.into_iter().take(limit as usize).collect();
            let next_cursor = if more { keys.last().cloned() } else { None };
            Ok(KeyPage { keys, next_cursor })
        }
    }

    #[test]
    fn read_modify_write_creates_then_updates() {
        let kv = MemKv::default();
        let inc = |cur: Option<Vec<u8>>| {
            let n: u32 = cur.map_or(0, |b| String::from_utf8(b).unwrap().parse().unwrap());
            (n + 1).to_string().into_bytes()
        };
        assert_eq!(read_modify_write(&kv, "ctr", 0, 3, inc).unwrap(), b"1");
        assert_eq!(read_modify_write(&kv, "ctr", 0, 3, inc).unwrap(), b"2");
    }

    #[test]
    fn read_modify_write_retries_through_a_concurrent_writer_and_gives_up_loudly() {
        let kv = MemKv::default();
        read_modify_write(&kv, "k", 0, 1, |_| b"a".to_vec()).unwrap();
        // two interfering writers: attempt 1 and 2 lose, attempt 3 wins
        *kv.interfere.borrow_mut() = 2;
        assert!(read_modify_write(&kv, "k", 0, 3, |_| b"b".to_vec()).is_ok());
        // more interference than attempts -> an explicit Contended error
        *kv.interfere.borrow_mut() = 5;
        assert_eq!(
            read_modify_write(&kv, "k", 0, 2, |_| b"c".to_vec()),
            Err(KvV2Error::Contended(2))
        );
    }

    #[test]
    fn list_all_follows_cursors_and_is_bounded() {
        let kv = MemKv::default();
        for i in 0..25 {
            kv.cas(&format!("ev.{i:02}"), 0, b"x", 0).unwrap();
        }
        kv.cas("zz", 0, b"x", 0).unwrap();
        assert_eq!(list_all(&kv, "ev.", 10, 10).unwrap().len(), 25);
        assert!(matches!(
            list_all(&kv, "ev.", 10, 2),
            Err(KvV2Error::Invalid(_))
        ));
    }

    /// In-memory DB transport: one table, versioned rows.
    #[derive(Default)]
    struct MemDb {
        rows: RefCell<Vec<Row>>,
        conflicts_to_inject: RefCell<u32>,
    }

    impl DbV2Transport for MemDb {
        fn insert(&self, table: &str, columns: Vec<(String, Value)>) -> Result<Row, DbV2Error> {
            let mut rows = self.rows.borrow_mut();
            let row = Row {
                table: table.into(),
                row_id: format!("r{}", rows.len()),
                version: 1,
                created_at: "c".into(),
                updated_at: "u".into(),
                columns,
            };
            rows.push(row.clone());
            Ok(row)
        }
        fn get(&self, _t: &str, row_id: &str) -> Result<Row, DbV2Error> {
            self.rows
                .borrow()
                .iter()
                .find(|r| r.row_id == row_id)
                .cloned()
                .ok_or(DbV2Error::NotFound)
        }
        fn query(&self, q: &Query) -> Result<Page, DbV2Error> {
            let start = q
                .cursor
                .as_deref()
                .map_or(0, |c| c.parse::<usize>().unwrap());
            let rows = self.rows.borrow();
            let slice: Vec<Row> = rows
                .iter()
                .skip(start)
                .take(q.limit as usize)
                .cloned()
                .collect();
            let next =
                (start + slice.len() < rows.len()).then(|| (start + slice.len()).to_string());
            Ok(Page {
                rows: slice,
                next_cursor: next,
            })
        }
        fn count(&self, _t: &str, _f: &[Cond]) -> Result<u64, DbV2Error> {
            Ok(self.rows.borrow().len() as u64)
        }
        fn update(
            &self,
            _t: &str,
            row_id: &str,
            expected: u64,
            columns: Vec<(String, Value)>,
        ) -> Result<Row, DbV2Error> {
            if *self.conflicts_to_inject.borrow() > 0 {
                *self.conflicts_to_inject.borrow_mut() -= 1;
                if let Some(r) = self
                    .rows
                    .borrow_mut()
                    .iter_mut()
                    .find(|r| r.row_id == row_id)
                {
                    r.version += 1;
                }
            }
            let mut rows = self.rows.borrow_mut();
            let r = rows
                .iter_mut()
                .find(|r| r.row_id == row_id)
                .ok_or(DbV2Error::NotFound)?;
            if r.version != expected {
                return Err(DbV2Error::Conflict("version mismatch".into()));
            }
            r.version += 1;
            r.columns = columns;
            Ok(r.clone())
        }
        fn delete(&self, _t: &str, _r: &str, _v: u64) -> Result<(), DbV2Error> {
            Ok(())
        }
        fn transact(&self, ops: &[TxOp]) -> Result<Vec<TxResult>, DbV2Error> {
            Ok(ops.iter().map(|_| TxResult::Done).collect())
        }
    }

    #[test]
    fn query_builder_composes_a_typed_query_with_no_sql() {
        let q = Query::table("events")
            .filter(Cond::ge("start_at", "2026-04-01T00:00:00Z"))
            .filter(Cond::in_list("kind", [1i64, 2]))
            .filter(Cond::prefix("title", "wk_"))
            .order_by("start_at", true)
            .limit(25)
            .after(Some("c".into()));
        assert_eq!(q.filter.len(), 3);
        assert_eq!(q.order, Some(("start_at".to_string(), true)));
        assert_eq!((q.limit, q.cursor.as_deref()), (25, Some("c")));
        assert_eq!(
            q.filter[1],
            Cond::In("kind".into(), vec![Value::Int(1), Value::Int(2)])
        );
    }

    #[test]
    fn query_all_follows_cursors_past_the_old_40_row_ceiling() {
        let db = MemDb::default();
        for i in 0..130 {
            db.insert("events", vec![("n".into(), Value::Int(i))])
                .unwrap();
        }
        let rows = query_all(&db, Query::table("events").limit(50), 10).unwrap();
        assert_eq!(rows.len(), 130);
        let err = query_all(&db, Query::table("events").limit(10), 3).unwrap_err();
        assert!(matches!(err, DbV2Error::InvalidQuery(_)));
    }

    #[test]
    fn update_with_retry_survives_conflicts_then_fails_loudly() {
        let db = MemDb::default();
        let row = db.insert("t", vec![("n".into(), Value::Int(0))]).unwrap();
        *db.conflicts_to_inject.borrow_mut() = 2;
        let out = update_with_retry(&db, "t", &row.row_id, 5, |r| {
            let n = match r.get("n") {
                Some(Value::Int(n)) => *n,
                _ => 0,
            };
            Some(vec![("n".into(), Value::Int(n + 1))])
        })
        .unwrap();
        assert_eq!(out.get("n"), Some(&Value::Int(1)));
        *db.conflicts_to_inject.borrow_mut() = 9;
        assert_eq!(
            update_with_retry(&db, "t", &row.row_id, 2, |_| Some(vec![])),
            Err(DbV2Error::Contended(2))
        );
        // `None` from the closure = leave unchanged, no write
        let same = update_with_retry(&db, "t", &row.row_id, 2, |_| None).unwrap();
        assert_eq!(same.row_id, row.row_id);
        // NotFound is not retried
        assert_eq!(
            update_with_retry(&db, "t", "missing", 3, |_| Some(vec![])),
            Err(DbV2Error::NotFound)
        );
    }
}

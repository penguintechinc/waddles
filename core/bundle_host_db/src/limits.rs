//! Fixed operational ceilings for the `db` capability (design doc SS3.1,
//! SS8, SS9) -- one bundle looping `insert`/`query` must hit a WIT error,
//! never degrade Postgres for every other tenant sharing the instance.

use std::time::Duration;

/// Longest `text` column value accepted, in bytes (design doc SS3.1).
pub const MAX_TEXT_BYTES: usize = 8192;

/// Largest `jsonb` column value accepted, in bytes (design doc SS3.1).
pub const MAX_JSONB_BYTES: usize = 16 * 1024;

/// Highest `numeric(p,s)` precision a column may declare -- mirrors
/// `MAX_NUMERIC_PRECISION` in `hub_api/services/bundle_data_schema.py`.
pub const MAX_NUMERIC_PRECISION: u8 = 38;

/// Highest `numeric(p,s)` scale a column may declare -- mirrors
/// `MAX_NUMERIC_SCALE` in `hub_api/services/bundle_data_schema.py`.
pub const MAX_NUMERIC_SCALE: u8 = 12;

/// Longest decimal string accepted for a `numeric` column, in bytes: far
/// above any legal value (sign + 38 digits + point) so harmless zero padding
/// still fits, yet small enough that a bundle cannot make the host scan an
/// arbitrarily long digit string.
pub const MAX_NUMERIC_TEXT_BYTES: usize = 128;

/// Highest row count `query` may ever return in one call, and the default
/// applied when a bundle requests more (design doc SS6.1: "`limit` capped
/// (default 200)").
pub const MAX_QUERY_LIMIT: u32 = 200;

/// Highest number of `db` ops one invocation may perform -- same rationale
/// and value as `bundle_host_kv::limits::MAX_OPS_PER_INVOKE`.
pub const MAX_OPS_PER_INVOKE: u32 = 64;

/// Highest number of live rows one `(tenant, community, app_id)` may hold
/// in its table at once (design doc SS9: "transactional per-(app_id,
/// tenant) row/byte counters"). This landing enforces it via a pre-insert
/// `COUNT(*)` under the same transaction as the insert, not yet the
/// trigger-based counter the full design calls for (see `crate::backend`'s
/// doc) -- correct but not the final-scale mechanism.
pub const MAX_ROWS_PER_APP: i64 = 100_000;

/// Statement-level timeout applied via `SET LOCAL statement_timeout` on
/// every `db` transaction (design doc SS6.2/SS7), independent of the
/// call-level deadline below -- a runaway query is killed by Postgres
/// itself even if the host-side timeout races it.
pub const STATEMENT_TIMEOUT_MS: u64 = 2_000;

/// Overall wall-clock deadline for one `db` host-call, wrapping connection
/// acquisition + the transaction itself (`tokio::time::timeout` in
/// `crate::backend`) -- strictly greater than [`STATEMENT_TIMEOUT_MS`] so
/// a legitimately-slow-but-within-budget statement is never cut off by the
/// outer deadline before Postgres's own timeout would have fired first.
pub const CALL_DEADLINE: Duration = Duration::from_secs(5);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn call_deadline_exceeds_the_statement_timeout() {
        assert!(CALL_DEADLINE > Duration::from_millis(STATEMENT_TIMEOUT_MS));
    }

    #[test]
    fn max_query_limit_matches_the_design_doc_default() {
        assert_eq!(MAX_QUERY_LIMIT, 200);
    }
}

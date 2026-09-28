//! The multi-tenant change-log consumer half of the DB-driven active-bundle
//! loader (dataplane scale design rev 4, §7/§8 step 2: "Multi-tenant
//! watermark polling"). Replaces the single-`(tenant_id, community_id)`
//! SHA-256 [`crate::query::Watermark`] with an exact, xid-safe sequence
//! number published by the PRIMARY (`bundle_active_set_watermark.safe_seq`)
//! -- every replica polls `WHERE seq > last_seen_seq AND seq <= safe_seq
//! ORDER BY seq`, **never consuming beyond `safe_seq`**, and advances
//! `last_seen_seq` to `safe_seq` itself (not to the max row `seq` actually
//! returned) so a quiet tick with zero rows in range still moves the
//! watermark forward -- see [`ChangeLogTracker::advance`]'s own doc.
//!
//! This module is deliberately silent on *what to do* with an affected
//! scope -- [`affected_scopes`] only tells a caller *which* `(tenant_id,
//! community_id)` pairs changed; `core/svc_process`'s and
//! `core/svc_action`'s own `changelog_consumer` modules are what actually
//! re-read ([`crate::query::read_active_set`]) and apply
//! ([`crate::diff::plan`]) each affected scope, fail-closed per entry
//! (skip + metric, never abort the whole tick -- see each service's own
//! module doc).

use std::collections::BTreeSet;

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter, QueryOrder};

use crate::entities::{bundle_active_set_changes, bundle_active_set_watermark};
use crate::query::ActiveSetError;

/// The single watermark row's `id` (dataplane scale design §7: "one row").
const WATERMARK_ROW_ID: i32 = 1;

/// Reads the current `safe_seq` horizon published by the primary. A
/// missing row (the primary-side publisher job hasn't run in this
/// environment yet, or a fresh install) reads as `0` -- the fail-safe
/// default that simply means "nothing is safe to incrementally consume
/// yet", never an error; the periodic full reconcile (each service's own
/// `changelog_consumer` module) keeps the active set correct regardless.
pub async fn read_safe_seq(conn: &DatabaseConnection) -> Result<i64, ActiveSetError> {
    let row = bundle_active_set_watermark::Entity::find_by_id(WATERMARK_ROW_ID)
        .one(conn)
        .await?;
    Ok(row.map(|r| r.safe_seq).unwrap_or(0))
}

/// One `bundle_active_set_changes` row -- the scope it names plus enough
/// metadata for a caller's own logging/metrics; [`affected_scopes`] is the
/// only thing that consumes the `(tenant_id, community_id)` pair, deliberately
/// never branching on `entity`/`op` (every change gets the identical
/// "re-read this scope in full" response, per this module's own doc).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChangeRow {
    pub seq: i64,
    pub tenant_id: i32,
    pub community_id: i32,
    pub entity: String,
    pub entity_id: String,
    pub op: String,
}

impl From<bundle_active_set_changes::Model> for ChangeRow {
    fn from(m: bundle_active_set_changes::Model) -> Self {
        Self {
            seq: m.seq,
            tenant_id: m.tenant_id,
            community_id: m.community_id,
            entity: m.entity,
            entity_id: m.entity_id,
            op: m.op,
        }
    }
}

/// Reads every change row with `since_seq < seq <= safe_seq`, ordered by
/// `seq` -- **never reads past `safe_seq`** (the caller-supplied horizon
/// is a hard upper bound, not a hint), matching the design's own polling
/// contract verbatim (§7).
pub async fn read_changes(
    conn: &DatabaseConnection,
    since_seq: i64,
    safe_seq: i64,
) -> Result<Vec<ChangeRow>, ActiveSetError> {
    if safe_seq <= since_seq {
        return Ok(Vec::new());
    }
    let rows = bundle_active_set_changes::Entity::find()
        .filter(bundle_active_set_changes::Column::Seq.gt(since_seq))
        .filter(bundle_active_set_changes::Column::Seq.lte(safe_seq))
        .order_by_asc(bundle_active_set_changes::Column::Seq)
        .all(conn)
        .await?;
    Ok(rows.into_iter().map(ChangeRow::from).collect())
}

/// Distinct `(tenant_id, community_id)` scopes touched by `changes`,
/// deduplicated and in a stable (sorted) order -- callers iterate this in
/// order so per-scope fail-closed handling (skip + metric on one scope's
/// re-read failure) is deterministic across runs, which matters for tests
/// asserting exact scope-processing order.
pub fn affected_scopes(changes: &[ChangeRow]) -> Vec<(i32, i32)> {
    changes
        .iter()
        .map(|c| (c.tenant_id, c.community_id))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// Tracks one poller's `last_seen_seq` against the change-log's exact
/// `safe_seq` horizon -- pure, no I/O, mirroring [`crate::query::
/// WatermarkTracker`]'s identical "testable without a database" rationale.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChangeLogTracker {
    last_seq: i64,
}

impl ChangeLogTracker {
    /// Starts tracking from `initial_seq` -- callers seed this with the
    /// `safe_seq` observed at (or just before) the initial full active-set
    /// read completes, so the very next tick's incremental poll only sees
    /// changes genuinely newer than what that full read already reflects
    /// (a tiny race window between reading `safe_seq` and finishing the
    /// full read is bounded by the periodic full reconcile, never a
    /// correctness gap).
    pub fn new(initial_seq: i64) -> Self {
        Self {
            last_seq: initial_seq,
        }
    }

    /// The last horizon this tracker has fully advanced past.
    pub fn last_seq(&self) -> i64 {
        self.last_seq
    }

    /// Advances `last_seq` to `safe_seq` -- **not** to the max `seq`
    /// actually returned by [`read_changes`]. A tick with zero rows in
    /// range (nothing changed) must still advance the watermark, exactly
    /// per the design's own polling contract ("advancing `last_seen_seq`
    /// only up to `safe_seq`") -- otherwise a quiet period would leave
    /// `last_seq` stuck arbitrarily far behind `safe_seq` even though
    /// there is genuinely nothing left to apply in that range. Monotonic
    /// (`max`, never regresses) as a defensive floor against a caller
    /// accidentally observing a stale, smaller `safe_seq` out of order.
    pub fn advance(&mut self, safe_seq: i64) {
        self.last_seq = self.last_seq.max(safe_seq);
    }

    /// How far behind the just-read `safe_seq` this tracker's `last_seq`
    /// currently is -- the `waddles_bundle_changelog_lag` gauge's value.
    /// Never negative (a `safe_seq` behind `last_seq` -- a stale read
    /// racing an already-applied advance -- reports `0`, not a misleading
    /// negative lag).
    pub fn lag(&self, safe_seq: i64) -> i64 {
        (safe_seq - self.last_seq).max(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn watermark_row(safe_seq: i64) -> bundle_active_set_watermark::Model {
        bundle_active_set_watermark::Model {
            id: WATERMARK_ROW_ID,
            safe_seq,
            computed_at: Utc::now(),
        }
    }

    fn change_row(seq: i64, tenant_id: i32, community_id: i32) -> bundle_active_set_changes::Model {
        bundle_active_set_changes::Model {
            seq,
            tenant_id,
            community_id,
            entity: "app_active_versions".to_string(),
            entity_id: "waddles.a".to_string(),
            op: "upsert".to_string(),
            changed_at: Utc::now(),
            writer_xid: Some(42),
        }
    }

    #[tokio::test]
    async fn read_safe_seq_returns_zero_when_the_watermark_row_is_missing(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<bundle_active_set_watermark::Model>::new()])
            .into_connection();
        assert_eq!(read_safe_seq(&db).await?, 0);
        Ok(())
    }

    #[tokio::test]
    async fn read_safe_seq_returns_the_published_value() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(1234)]])
            .into_connection();
        assert_eq!(read_safe_seq(&db).await?, 1234);
        Ok(())
    }

    #[tokio::test]
    async fn read_changes_never_queries_when_safe_seq_has_not_advanced(
    ) -> Result<(), ActiveSetError> {
        // No `append_query_results` at all -- a query here would exhaust
        // the mock and error, proving the short-circuit genuinely skips
        // the DB round trip rather than issuing a query that happens to
        // return nothing.
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        assert_eq!(read_changes(&db, 100, 100).await?, Vec::new());
        assert_eq!(read_changes(&db, 100, 50).await?, Vec::new());
        Ok(())
    }

    #[tokio::test]
    async fn read_changes_returns_rows_in_the_requested_range() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![change_row(11, 1, 0), change_row(12, 2, 0)]])
            .into_connection();
        let changes = read_changes(&db, 10, 12).await?;
        assert_eq!(changes.len(), 2);
        assert_eq!(changes[0].seq, 11);
        assert_eq!(changes[1].tenant_id, 2);
        Ok(())
    }

    #[test]
    fn affected_scopes_dedupes_and_sorts() {
        let changes = vec![
            change_row(3, 2, 0),
            change_row(1, 1, 0),
            change_row(2, 1, 0),
            change_row(4, 1, 5),
        ]
        .into_iter()
        .map(ChangeRow::from)
        .collect::<Vec<_>>();
        assert_eq!(
            affected_scopes(&changes),
            vec![(1, 0), (1, 5), (2, 0)],
            "must dedupe (1,0) and sort deterministically"
        );
    }

    #[test]
    fn affected_scopes_is_empty_for_no_changes() {
        assert!(affected_scopes(&[]).is_empty());
    }

    #[test]
    fn tracker_advances_to_safe_seq_even_with_zero_changes_in_range() {
        let mut tracker = ChangeLogTracker::new(100);
        assert_eq!(tracker.last_seq(), 100);
        tracker.advance(150);
        assert_eq!(
            tracker.last_seq(),
            150,
            "a quiet tick (zero changes in range) must still advance to safe_seq"
        );
    }

    #[test]
    fn tracker_never_regresses_on_a_stale_smaller_safe_seq() {
        let mut tracker = ChangeLogTracker::new(200);
        tracker.advance(150);
        assert_eq!(
            tracker.last_seq(),
            200,
            "advance must be monotonic, never regress on a stale read"
        );
    }

    #[test]
    fn lag_reports_the_gap_and_never_negative() {
        let tracker = ChangeLogTracker::new(100);
        assert_eq!(tracker.lag(130), 30);
        assert_eq!(
            tracker.lag(50),
            0,
            "a safe_seq behind last_seq must report zero lag, never negative"
        );
        assert_eq!(tracker.lag(100), 0);
    }
}

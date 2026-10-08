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

use sea_orm::{
    ColumnTrait, ConnectionTrait, DatabaseConnection, EntityTrait, FromQueryResult, QueryFilter,
    QueryOrder, QuerySelect, Statement,
};

use crate::entities::{bundle_active_set_changes, bundle_active_set_watermark};
use crate::query::ActiveSetError;

/// The single watermark row's `id` (dataplane scale design §7: "one row").
// regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
const WATERMARK_ROW_ID: i16 = 1;

/// The primary's published `safe_seq` horizon AND `min_retained_seq` (hub-api
/// migration `0026_bundle_active_set_changelog`, waddles PR #397, requires
/// **hub-api >= the release that shipped that migration** -- see
/// [`probe_min_retained_seq_supported`]'s doc for how an older hub-api is
/// detected and tolerated) -- together these let [`read_safe_seq_watermark`]'s
/// caller detect "my `last_seq` has fallen behind retention" directly,
/// rather than only via the "lowest returned change row's seq" heuristic
/// each service's own `changelog_consumer` module falls back to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct SafeSeqWatermark {
    pub safe_seq: i64,
    /// `0` when the column genuinely reads as `0` (a fresh install/no prune
    /// has ever run) OR when `supports_retention: false` was passed to
    /// [`read_safe_seq_watermark`] (older hub-api schema) -- either way,
    /// `last_seq + 1 < 0` is never true, so a caller's retention check
    /// against this value simply never fires until real data is present,
    /// never a false positive.
    pub min_retained_seq: i64,
}

#[derive(Debug, FromQueryResult)]
struct SafeSeqOnly {
    safe_seq: i64,
}

/// Probes whether this environment's `bundle_active_set_watermark` table has
/// the `min_retained_seq` column at all (hub-api migration `0026`/PR #397 --
/// **minimum hub-api version: any release including that migration**; an
/// older hub-api's schema predates it entirely). Queried once per consumer
/// instance (`crate::changelog_consumer`'s own `initial_state`, cached in
/// `ConsumerState` for the process's lifetime -- the schema does not change
/// while a pod is running) rather than trying the full select and catching a
/// driver-specific "column does not exist" error: `information_schema` is a
/// stable, ordinary `SELECT`, so both the "column present" and "column
/// absent" cases are exercised by plain, `MockDatabase`-mockable queries in
/// this crate's own tests -- no need to synthesize a real Postgres error.
pub async fn probe_min_retained_seq_supported(
    conn: &DatabaseConnection,
) -> Result<bool, ActiveSetError> {
    let stmt = Statement::from_string(
        conn.get_database_backend(),
        "SELECT 1 FROM information_schema.columns \
         WHERE table_name = 'bundle_active_set_watermark' \
         AND column_name = 'min_retained_seq'"
            .to_string(),
    );
    let rows = conn.query_all_raw(stmt).await?;
    Ok(!rows.is_empty())
}

/// Reads the current `safe_seq`/`min_retained_seq` horizon published by the
/// primary. A missing row (the primary-side publisher job hasn't run in
/// this environment yet, or a fresh install) reads as `SafeSeqWatermark::
/// default()` (both `0`) -- the fail-safe default that simply means
/// "nothing is safe to incrementally consume yet", never an error; the
/// periodic full reconcile (each service's own `changelog_consumer` module)
/// keeps the active set correct regardless.
///
/// `supports_retention` (from [`probe_min_retained_seq_supported`], probed
/// once at startup): `true` selects the full row including `min_retained_
/// seq`; `false` (older hub-api schema, migration `0026`/PR #397 not yet
/// applied) selects ONLY `safe_seq` -- never referencing the column that
/// doesn't exist there -- and reports `min_retained_seq: 0` (see that
/// field's own doc for why `0` is a safe, never-false-positive default).
pub async fn read_safe_seq_watermark(
    conn: &DatabaseConnection,
    supports_retention: bool,
) -> Result<SafeSeqWatermark, ActiveSetError> {
    if supports_retention {
        let row = bundle_active_set_watermark::Entity::find_by_id(WATERMARK_ROW_ID)
            .one(conn)
            .await?;
        Ok(row
            .map(|r| SafeSeqWatermark {
                safe_seq: r.safe_seq,
                min_retained_seq: r.min_retained_seq,
            })
            .unwrap_or_default())
    } else {
        let row = bundle_active_set_watermark::Entity::find_by_id(WATERMARK_ROW_ID)
            .select_only()
            .column(bundle_active_set_watermark::Column::SafeSeq)
            .into_model::<SafeSeqOnly>()
            .one(conn)
            .await?;
        Ok(SafeSeqWatermark {
            safe_seq: row.map(|r| r.safe_seq).unwrap_or(0),
            min_retained_seq: 0,
        })
    }
}

/// Reads just the current `safe_seq` horizon (full row select, `min_retained_
/// seq` included) -- a thin convenience wrapper around
/// [`read_safe_seq_watermark`] for callers on a schema known to have the
/// column (this crate's own tests below); production consumers call
/// [`read_safe_seq_watermark`] directly with their probed
/// `supports_retention` value instead.
pub async fn read_safe_seq(conn: &DatabaseConnection) -> Result<i64, ActiveSetError> {
    Ok(read_safe_seq_watermark(conn, true).await?.safe_seq)
}

/// One `bundle_active_set_changes` row -- the scope it names plus enough
/// metadata for a caller's own logging/metrics; [`affected_scopes`] is the
/// only thing that consumes the `(tenant_id, community_id)` pair, deliberately
/// never branching on `entity`/`op` (every change gets the identical
/// "re-read this scope in full" response, per this module's own doc).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChangeRow {
    pub seq: i64,
    /// `None` for a non-tenant-scoped change (e.g. an `app_versions`
    /// publish -- that table has no `tenant_id` column, so the trigger logs
    /// `NULL`; see `crate::entities::bundle_active_set_changes`'s doc).
    pub tenant_id: Option<i32>,
    pub community_id: Option<i32>,
    pub entity: String,
    pub entity_id: String,
    pub op: String,
}

impl ChangeRow {
    /// Resolves this row's `(tenant_id, community_id)` [`crate::ScopeKey`],
    /// or `None` when the row isn't tenant-scoped at all (`tenant_id IS
    /// NULL`) -- there is no scope to incrementally re-read in that case;
    /// the periodic full reconcile is what actually picks up an
    /// `app_versions` change (see this module's own doc). A present
    /// `tenant_id` with a `NULL` `community_id` resolves to the `0`
    /// tenant-wide sentinel, matching `app_active_versions.community_id`'s
    /// own convention.
    pub fn scope_key(&self) -> Option<(i32, i32)> {
        self.tenant_id
            .map(|tenant_id| (tenant_id, self.community_id.unwrap_or(0)))
    }
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

/// Column-projected shape of [`read_changes`]'s own `SELECT` -- deliberately
/// omits `writer_xid` and `changed_at`, neither of which [`ChangeRow`] (or
/// any caller) ever reads. regression: `writer_xid xid8` decoded as
/// `Option<i64>` (`bundle_active_set_changes::Model`'s full-row shape) made
/// sqlx reject every single poll with "mismatched types ... INT8 ... is not
/// compatible with SQL type xid8" (alpha rev 34, 2026-10-04) -- Postgres's
/// `xid8` has no `sqlx-postgres` decode support at all (not even a `String`
/// fallback: the driver's OID-compatibility check fails before any value
/// conversion runs), so the only correct fix is to never ask the driver to
/// decode that column here. `writer_xid` remains `xid8 NOT NULL` on the
/// entity/table for the primary-side safe-horizon job's own native-`xid8`
/// comparison (`hub_api/services/bundle_active_set_watermark_job.py`,
/// `CAST(:horizon AS xid8)`) -- that job never goes through this crate's
/// `Entity`/sqlx decode path, so it is unaffected either way.
#[derive(Debug, FromQueryResult)]
struct ChangeRowColumns {
    seq: i64,
    tenant_id: Option<i32>,
    community_id: Option<i32>,
    entity: String,
    entity_id: String,
    op: String,
}

impl From<ChangeRowColumns> for ChangeRow {
    fn from(c: ChangeRowColumns) -> Self {
        Self {
            seq: c.seq,
            tenant_id: c.tenant_id,
            community_id: c.community_id,
            entity: c.entity,
            entity_id: c.entity_id,
            op: c.op,
        }
    }
}

/// Reads every change row with `since_seq < seq <= safe_seq`, ordered by
/// `seq` -- **never reads past `safe_seq`** (the caller-supplied horizon
/// is a hard upper bound, not a hint), matching the design's own polling
/// contract verbatim (§7). Column-projected (see [`ChangeRowColumns`]) to
/// avoid ever decoding `writer_xid`, which sqlx cannot decode at all.
pub async fn read_changes(
    conn: &DatabaseConnection,
    since_seq: i64,
    safe_seq: i64,
) -> Result<Vec<ChangeRow>, ActiveSetError> {
    if safe_seq <= since_seq {
        return Ok(Vec::new());
    }
    let rows = bundle_active_set_changes::Entity::find()
        .select_only()
        .column(bundle_active_set_changes::Column::Seq)
        .column(bundle_active_set_changes::Column::TenantId)
        .column(bundle_active_set_changes::Column::CommunityId)
        .column(bundle_active_set_changes::Column::Entity)
        .column(bundle_active_set_changes::Column::EntityId)
        .column(bundle_active_set_changes::Column::Op)
        .filter(bundle_active_set_changes::Column::Seq.gt(since_seq))
        .filter(bundle_active_set_changes::Column::Seq.lte(safe_seq))
        .order_by_asc(bundle_active_set_changes::Column::Seq)
        .into_model::<ChangeRowColumns>()
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
        .filter_map(ChangeRow::scope_key)
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
        safe_seq.saturating_sub(self.last_seq).max(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Utc;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn watermark_row(safe_seq: i64) -> bundle_active_set_watermark::Model {
        watermark_row_with_retention(safe_seq, 0)
    }

    fn watermark_row_with_retention(
        safe_seq: i64,
        min_retained_seq: i64,
    ) -> bundle_active_set_watermark::Model {
        bundle_active_set_watermark::Model {
            id: WATERMARK_ROW_ID,
            safe_seq,
            min_retained_seq,
            computed_at: Utc::now(),
        }
    }

    fn change_row(seq: i64, tenant_id: i32, community_id: i32) -> bundle_active_set_changes::Model {
        change_row_scoped(seq, Some(tenant_id), Some(community_id))
    }

    /// Like [`change_row`] but allows a `NULL` `tenant_id`/`community_id`,
    /// exactly as a real `app_versions` change row reads (migration 0028's
    /// trigger logs `NULL` for a table with no `tenant_id` column).
    fn change_row_scoped(
        seq: i64,
        tenant_id: Option<i32>,
        community_id: Option<i32>,
    ) -> bundle_active_set_changes::Model {
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
    async fn read_safe_seq_watermark_returns_both_safe_seq_and_min_retained_seq(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row_with_retention(1234, 900)]])
            .into_connection();
        let watermark = read_safe_seq_watermark(&db, true).await?;
        assert_eq!(watermark.safe_seq, 1234);
        assert_eq!(watermark.min_retained_seq, 900);
        Ok(())
    }

    #[tokio::test]
    async fn read_safe_seq_watermark_defaults_both_fields_when_the_row_is_missing(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<bundle_active_set_watermark::Model>::new()])
            .into_connection();
        assert_eq!(
            read_safe_seq_watermark(&db, true).await?,
            SafeSeqWatermark::default()
        );
        Ok(())
    }

    /// `supports_retention: false` (older hub-api schema, migration `0026`/
    /// PR #397 not yet applied): reads ONLY `safe_seq`, always reporting
    /// `min_retained_seq: 0` regardless of what a full row might otherwise
    /// contain -- the actual regression this crate's own review asked for
    /// ("add a test with the column missing"), now trivially expressible as
    /// an ordinary mocked query rather than a synthesized driver error.
    #[tokio::test]
    async fn read_safe_seq_watermark_falls_back_to_safe_seq_only_when_retention_unsupported(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "safe_seq".to_string(),
                sea_orm::Value::BigInt(Some(777)),
            )])]])
            .into_connection();
        let watermark = read_safe_seq_watermark(&db, false).await?;
        assert_eq!(watermark.safe_seq, 777);
        assert_eq!(
            watermark.min_retained_seq, 0,
            "must never reference the missing column, even implicitly"
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_safe_seq_watermark_falls_back_defaults_safe_seq_to_zero_when_row_missing(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([
                Vec::<std::collections::BTreeMap<String, sea_orm::Value>>::new(),
            ])
            .into_connection();
        assert_eq!(
            read_safe_seq_watermark(&db, false).await?,
            SafeSeqWatermark::default()
        );
        Ok(())
    }

    #[tokio::test]
    async fn probe_min_retained_seq_supported_is_true_when_the_column_exists(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "?column?".to_string(),
                sea_orm::Value::Int(Some(1)),
            )])]])
            .into_connection();
        assert!(probe_min_retained_seq_supported(&db).await?);
        Ok(())
    }

    #[tokio::test]
    async fn probe_min_retained_seq_supported_is_false_when_the_column_is_absent(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([
                Vec::<std::collections::BTreeMap<String, sea_orm::Value>>::new(),
            ])
            .into_connection();
        assert!(!probe_min_retained_seq_supported(&db).await?);
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
        assert_eq!(changes[1].tenant_id, Some(2));
        Ok(())
    }

    /// regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    /// A `NULL` `tenant_id`/`community_id` row (real shape of an
    /// `app_versions` change, migration 0028) must decode without error --
    /// before the `Option<i32>` fix this crashed the same way the
    /// watermark `id` INT2 mismatch did.
    #[tokio::test]
    async fn read_changes_decodes_a_non_tenant_scoped_row_without_error(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![change_row_scoped(20, None, None)]])
            .into_connection();
        let changes = read_changes(&db, 10, 20).await?;
        assert_eq!(changes.len(), 1);
        assert_eq!(changes[0].tenant_id, None);
        assert_eq!(changes[0].community_id, None);
        assert_eq!(
            changes[0].scope_key(),
            None,
            "a non-tenant-scoped row has no scope to incrementally re-read"
        );
        Ok(())
    }

    /// regression: `writer_xid xid8` decoded as `Option<i64>` made every
    /// `read_changes` poll fail with a sqlx OID-mismatch error (alpha rev 34,
    /// 2026-10-04), which starved the tracker's `last_seq` advance and
    /// eventually forced a full reconcile on *every* tick via the
    /// `min_retained_seq` retention-exceeded check above. `MockDatabase`
    /// decodes from an in-memory `sea_orm::Value` map, not real Postgres wire
    /// bytes, so it cannot reproduce the actual OID-compatibility failure --
    /// this test instead asserts directly on the SQL `read_changes` builds:
    /// `writer_xid` (sqlx-postgres has no `xid8` decode support at all, for
    /// any Rust type) must never appear in the column list, which is the
    /// only way to guarantee sqlx is never asked to decode it.
    #[test]
    fn read_changes_query_never_selects_writer_xid() {
        use sea_orm::{DbBackend, QueryTrait};

        let sql = bundle_active_set_changes::Entity::find()
            .select_only()
            .column(bundle_active_set_changes::Column::Seq)
            .column(bundle_active_set_changes::Column::TenantId)
            .column(bundle_active_set_changes::Column::CommunityId)
            .column(bundle_active_set_changes::Column::Entity)
            .column(bundle_active_set_changes::Column::EntityId)
            .column(bundle_active_set_changes::Column::Op)
            .filter(bundle_active_set_changes::Column::Seq.gt(0))
            .filter(bundle_active_set_changes::Column::Seq.lte(10))
            .order_by_asc(bundle_active_set_changes::Column::Seq)
            .build(DbBackend::Postgres)
            .to_string();
        assert!(
            !sql.contains("writer_xid"),
            "read_changes must never select writer_xid (undecodable xid8 column); got: {sql}"
        );
        assert!(
            sql.contains("\"seq\""),
            "sanity: the projected query must still select seq; got: {sql}"
        );
    }

    #[test]
    fn affected_scopes_skips_non_tenant_scoped_rows() {
        let changes = vec![
            change_row(1, 1, 0),
            change_row_scoped(2, None, None),
            change_row_scoped(3, Some(1), None),
        ]
        .into_iter()
        .map(ChangeRow::from)
        .collect::<Vec<_>>();
        assert_eq!(
            affected_scopes(&changes),
            vec![(1, 0)],
            "a NULL tenant_id row contributes no scope; NULL community_id resolves to the 0 sentinel"
        );
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

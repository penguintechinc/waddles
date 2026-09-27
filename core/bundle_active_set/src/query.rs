//! The active-set read and its cheap watermark pre-check (spec: "poll a
//! cheap CHANGE-WATERMARK first and only do the full active-set re-read
//! ... when it moves"). Both queries are scoped to one `(tenant_id,
//! community_id)` pair and, for [`read_active_set`], an optional single
//! `app_id` -- callers pass `None` to manage the whole scope's active set
//! (spec's multi-app requirement) or `Some(app_id)` to scope to one bundle.

use sea_orm::{ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder};
use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::entities::{app_active_versions, app_install_approvals, app_versions};

#[derive(Debug, Error)]
pub enum ActiveSetError {
    #[error("database query failed: {0}")]
    Db(#[from] DbErr),
}

/// One ACTIVE, APPROVED bundle version in scope -- the row shape
/// [`crate::diff`] and each service's own executor `load`/`unload` wiring
/// consume.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ActiveBundleRow {
    pub app_id: String,
    pub version: String,
    pub digest: String,
    pub component_key: String,
    pub sidecar_key: String,
}

/// A cheap change signal for one `(tenant_id, community_id)` scope: a
/// SHA-256 fingerprint over every `(app_id, version_id)` pair currently
/// active in that scope, sorted by `app_id` for a deterministic byte
/// sequence regardless of the row order Postgres happens to return.
///
/// **Not `COUNT(*) + SUM(version_id)` (security review fix -- that
/// approach silently cancels):** if app A's `version_id` decreases by N in
/// the same tick app B's increases by N, both the count and the sum stay
/// identical, so that watermark would never move and the hot-swap would
/// miss the change until an unrelated row nudged the sum back out of
/// coincidental alignment -- proven by
/// `read_watermark_moves_when_two_rows_shift_by_equal_and_opposite_amounts`
/// below, which is the exact regression this type exists to prevent. A
/// cryptographic hash over every row's *own* identity (not an aggregate
/// that discards which row changed) cannot cancel this way: two distinct
/// active sets hashing to the same digest would require a genuine SHA-256
/// collision, not a coincidental arithmetic identity.
///
/// Also sidesteps the `count`/`version_sum` design's other documented
/// concern: `app_active_versions` has no `updated_at` column to fall back
/// to (only `activated_at`, `DEFAULT NOW()` on INSERT only, with no
/// confirmed guarantee a future activation-service's rollback UPDATE also
/// bumps it) -- this fingerprint depends on neither column.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Watermark(String);

/// Reads [`Watermark`] for `(tenant_id, community_id)` -- one query
/// (`app_active_versions` only, no join), small result set (bounded by the
/// number of apps active in this one scope). Cheap enough to run every
/// poll tick even when nothing has changed; still meaningfully cheaper
/// than [`read_active_set`], which additionally joins `app_versions` and
/// `app_install_approvals`.
pub async fn read_watermark(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
) -> Result<Watermark, ActiveSetError> {
    let rows = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id))
        .order_by_asc(app_active_versions::Column::AppId)
        .all(conn)
        .await?;

    let mut hasher = Sha256::new();
    for row in &rows {
        // `app_id` is the natural unique key within this one `(tenant_id,
        // community_id)` scope (the table's own PK), so sorting by it
        // alone -- no secondary sort needed -- yields a deterministic
        // sequence. A `\0`/`\n` separator between fields/rows prevents a
        // pathological app_id boundary shift (e.g. "ab"+"1" vs "a"+"b1")
        // from ever producing the same byte stream for two different sets.
        hasher.update(row.app_id.as_bytes());
        hasher.update(b"\0");
        hasher.update(row.version_id.to_le_bytes());
        hasher.update(b"\n");
    }
    Ok(Watermark(format!("{:x}", hasher.finalize())))
}

/// Content-addressed `component_key`/`sidecar_key` derivation --
/// **defensive fallback only**, used by [`read_active_set`] exclusively
/// when `app_versions.component_key` is `NULL` (a row published before the
/// column-adding migration and/or hub-api's publish-step backfill landed;
/// see `crate::entities::app_versions`'s module doc for the full
/// contract). No longer the primary path -- `read_active_set` reads the
/// real `component_key`/`sidecar_key` columns directly when set. `digest`
/// is the `sha256:<64 hex>` `artifact_digest`; this strips the `sha256:`
/// prefix and keys both objects by the raw hex digest under `bundles/`,
/// the same convention this crate used before the real columns existed --
/// kept only so an un-backfilled row still loads instead of being dropped.
pub fn derive_component_keys(digest: &str) -> (String, String) {
    let hex = digest.strip_prefix("sha256:").unwrap_or(digest);
    (
        format!("bundles/{hex}/component.wasm"),
        format!("bundles/{hex}/sidecar.json"),
    )
}

/// Why one `app_active_versions` row was excluded from
/// [`ActiveSetRead::rows`] -- ops-visibility fix (security review):
/// exclusion used to be a `debug!`/`warn!` log line only, easy to miss when
/// a feature silently goes dark (e.g. an approval expiring/getting
/// superseded with nothing re-approving it). Returned alongside the rows
/// so each service's own `bundle_loader` can drive a Prometheus counter
/// from it (`telemetry::register_bundle_loader_excluded_metrics`), not
/// just a log line.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum ExclusionReason {
    /// `app_active_versions.version_id` points at no row in `app_versions`
    /// at all -- a referential-integrity gap, never expected in a healthy
    /// system.
    MissingVersionRow,
    /// No current (`superseded_by IS NULL`) `app_install_approvals` row
    /// matches -- the version is active but not (or no longer) approved.
    NoApproval,
    /// The `app_versions` row has no `artifact_digest` yet (not published,
    /// or a race with a concurrent publish).
    MissingDigest,
}

impl ExclusionReason {
    /// Stable label value for the Prometheus counter -- never the `Debug`
    /// form, which is not a contract callers should depend on.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::MissingVersionRow => "missing_version_row",
            Self::NoApproval => "no_approval",
            Self::MissingDigest => "missing_digest",
        }
    }
}

/// Why one row's `component_key`/`sidecar_key` came from
/// [`derive_component_keys`]'s fallback rather than the real
/// `app_versions` columns -- rollout visibility (component_key contract):
/// the row still loads (never excluded), but an un-backfilled row is a
/// signal worth surfacing, not silently patched over. Shares the same
/// `(app_id, reason)` shape and Prometheus label space as
/// [`ExclusionReason`] (both plug into the same
/// `bundle_active_set_excluded_total`-style counter via `as_str`), kept as
/// a separate type since a degraded row is a distinct condition from an
/// excluded one.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum DegradedReason {
    /// `app_versions.component_key` is `NULL` -- published before the
    /// column-adding migration and/or hub-api's publish-step backfill
    /// landed. Expected during rollout, not a steady-state condition.
    MissingComponentKey,
}

impl DegradedReason {
    /// Stable label value for the Prometheus counter -- same convention as
    /// [`ExclusionReason::as_str`].
    pub fn as_str(self) -> &'static str {
        match self {
            Self::MissingComponentKey => "missing_component_key",
        }
    }
}

/// [`read_active_set`]'s full result: the ACTIVE+APPROVED rows to load,
/// every row this tick excluded and why (see [`ExclusionReason`]'s doc),
/// and every row that loaded but via [`derive_component_keys`]'s fallback
/// rather than a real `component_key` column value (see
/// [`DegradedReason`]'s doc) -- exclusions and degradations are both data,
/// never just a log line.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ActiveSetRead {
    pub rows: Vec<ActiveBundleRow>,
    pub excluded: Vec<(String, ExclusionReason)>,
    pub degraded: Vec<(String, DegradedReason)>,
}

/// Reads the full ACTIVE, APPROVED set for `(tenant_id, community_id)`,
/// optionally narrowed to one `app_id`. Three sequential queries (no
/// SeaORM `Relation` wiring -- see `crate::entities`'s module doc) joined
/// in Rust:
///
/// 1. `app_active_versions` rows in scope (the ACTIVE half).
/// 2. `app_versions` rows for the resulting `version_id`s (the digest).
/// 3. `app_install_approvals` current (`superseded_by IS NULL`) rows for
///    the resulting `(app_id, version)` pairs in this tenant (the
///    APPROVED half) -- matched against each active row's own
///    `community_id` per the sentinel-mismatch rule documented on
///    `crate::entities::app_install_approvals`.
///
/// A version with no current approval, or an `app_versions` row with no
/// `artifact_digest` set (not yet published, or a data race with a
/// concurrent publish), is excluded from [`ActiveSetRead::rows`] (recorded
/// in [`ActiveSetRead::excluded`], never silently dropped) rather than
/// erroring the whole read -- one bad/incomplete row must never block
/// every other bundle's hot-swap. A row that loads but with a `NULL`
/// `component_key` (derived via [`derive_component_keys`]'s fallback
/// instead) is recorded in [`ActiveSetRead::degraded`], not excluded.
pub async fn read_active_set(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    app_id: Option<&str>,
) -> Result<ActiveSetRead, ActiveSetError> {
    let mut active_q = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id));
    if let Some(app_id) = app_id {
        active_q = active_q.filter(app_active_versions::Column::AppId.eq(app_id));
    }
    let active_rows = active_q.all(conn).await?;
    if active_rows.is_empty() {
        return Ok(ActiveSetRead::default());
    }

    let version_ids: Vec<i64> = active_rows.iter().map(|r| r.version_id).collect();
    let version_rows = app_versions::Entity::find()
        .filter(app_versions::Column::Id.is_in(version_ids))
        .all(conn)
        .await?;
    let versions_by_id: std::collections::HashMap<i64, app_versions::Model> =
        version_rows.into_iter().map(|v| (v.id, v)).collect();

    let approval_rows = app_install_approvals::Entity::find()
        .filter(app_install_approvals::Column::TenantId.eq(tenant_id))
        .filter(app_install_approvals::Column::SupersededBy.is_null())
        .all(conn)
        .await?;

    let mut rows = Vec::with_capacity(active_rows.len());
    let mut excluded = Vec::new();
    let mut degraded = Vec::new();
    for active in &active_rows {
        let Some(version_row) = versions_by_id.get(&active.version_id) else {
            tracing::warn!(
                app_id = %active.app_id,
                version_id = active.version_id,
                reason = ExclusionReason::MissingVersionRow.as_str(),
                "excluding from active set: active_versions points at a version_id with no app_versions row"
            );
            excluded.push((active.app_id.clone(), ExclusionReason::MissingVersionRow));
            continue;
        };

        let approved = approval_rows.iter().any(|appr| {
            appr.app_id == active.app_id
                && appr.version == version_row.version
                && (appr.community_id == Some(active.community_id)
                    || (appr.community_id.is_none() && active.community_id == 0))
        });
        if !approved {
            // Ops-visibility fix (security review): a bundle silently
            // losing its approval is a feature going dark, not routine
            // background noise -- this was `debug!` and easy to miss.
            tracing::warn!(
                app_id = %active.app_id,
                version = %version_row.version,
                reason = ExclusionReason::NoApproval.as_str(),
                "excluding from active set: active version has no current install approval"
            );
            excluded.push((active.app_id.clone(), ExclusionReason::NoApproval));
            continue;
        }

        let Some(digest) = version_row.artifact_digest.clone() else {
            tracing::warn!(
                app_id = %active.app_id,
                version = %version_row.version,
                reason = ExclusionReason::MissingDigest.as_str(),
                "excluding from active set: active, approved version has no artifact_digest yet"
            );
            excluded.push((active.app_id.clone(), ExclusionReason::MissingDigest));
            continue;
        };

        // component_key contract (data-plane half; hub-api half is the
        // migration + publish-step backfill landing in parallel): use the
        // real `app_versions.component_key`/`sidecar_key` columns
        // directly when set -- `derive_component_keys` is now a defensive
        // fallback for rows published before either lands, never the
        // primary path. `sidecar_key` falls back independently of
        // `component_key` (a present component with no sidecar is a
        // distinct, non-degraded case -- not every bundle ships a
        // sidecar).
        let (component_key, sidecar_key) = match version_row.component_key.clone() {
            Some(component_key) => {
                let sidecar_key = version_row
                    .sidecar_key
                    .clone()
                    .unwrap_or_else(|| derive_component_keys(&digest).1);
                (component_key, sidecar_key)
            }
            None => {
                tracing::warn!(
                    app_id = %active.app_id,
                    version = %version_row.version,
                    reason = DegradedReason::MissingComponentKey.as_str(),
                    "app_versions.component_key is NULL; falling back to content-addressed \
                     derivation (row published before the migration/backfill landed)"
                );
                degraded.push((active.app_id.clone(), DegradedReason::MissingComponentKey));
                derive_component_keys(&digest)
            }
        };
        rows.push(ActiveBundleRow {
            app_id: active.app_id.clone(),
            version: version_row.version.clone(),
            digest,
            component_key,
            sidecar_key,
        });
    }

    Ok(ActiveSetRead {
        rows,
        excluded,
        degraded,
    })
}

/// Tracks the last-seen [`Watermark`] for one poller instance and decides
/// whether a tick's freshly-read watermark represents a real change --
/// pure, no I/O, so the "unchanged watermark skips the full read"
/// requirement is testable without a database. The first tick after
/// construction always reports changed (`None` -> `Some`), matching the
/// required "still do one full read on startup to establish current
/// state".
#[derive(Debug, Default)]
pub struct WatermarkTracker {
    last: Option<Watermark>,
}

impl WatermarkTracker {
    pub fn new() -> Self {
        Self::default()
    }

    /// Records `current` and reports whether it differs from the
    /// previously recorded watermark (or there was none yet). Callers do
    /// the full [`read_active_set`] read only when this returns `true`.
    pub fn observe(&mut self, current: Watermark) -> bool {
        let changed = self.last.as_ref() != Some(&current);
        self.last = Some(current);
        changed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn watermark_of(pairs: &[(&str, i64)]) -> Watermark {
        let mut hasher = Sha256::new();
        for (app_id, version_id) in pairs {
            hasher.update(app_id.as_bytes());
            hasher.update(b"\0");
            hasher.update(version_id.to_le_bytes());
            hasher.update(b"\n");
        }
        Watermark(format!("{:x}", hasher.finalize()))
    }

    #[test]
    fn watermark_tracker_reports_changed_on_the_first_observation() {
        let mut tracker = WatermarkTracker::new();
        assert!(tracker.observe(watermark_of(&[("waddles.a", 10)])));
    }

    #[test]
    fn watermark_tracker_reports_unchanged_when_the_watermark_repeats() {
        let mut tracker = WatermarkTracker::new();
        let w = watermark_of(&[("waddles.a", 10)]);
        assert!(tracker.observe(w.clone()));
        assert!(
            !tracker.observe(w.clone()),
            "an unchanged watermark must skip the full read"
        );
        assert!(!tracker.observe(w));
    }

    #[test]
    fn watermark_tracker_reports_changed_when_the_active_set_moves() {
        let mut tracker = WatermarkTracker::new();
        assert!(tracker.observe(watermark_of(&[("waddles.a", 10)])));
        assert!(tracker.observe(watermark_of(&[("waddles.a", 10), ("waddles.b", 1)])));
        let third = watermark_of(&[("waddles.a", 10), ("waddles.b", 2)]);
        assert!(tracker.observe(third.clone()));
        assert!(!tracker.observe(third));
    }

    #[test]
    fn derive_component_keys_strips_the_sha256_prefix_and_is_content_addressed() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let (component, sidecar) = derive_component_keys(&digest);
        assert_eq!(
            component,
            format!("bundles/{}/component.wasm", "a".repeat(64))
        );
        assert_eq!(sidecar, format!("bundles/{}/sidecar.json", "a".repeat(64)));
    }

    #[test]
    fn derive_component_keys_tolerates_a_digest_without_the_prefix() {
        let (component, _sidecar) = derive_component_keys("deadbeef");
        assert_eq!(component, "bundles/deadbeef/component.wasm");
    }

    fn active_model(
        app_id: &str,
        tenant_id: i32,
        community_id: i32,
        version_id: i64,
    ) -> app_active_versions::Model {
        app_active_versions::Model {
            app_id: app_id.to_string(),
            tenant_id,
            community_id,
            version_id,
        }
    }

    #[tokio::test]
    async fn read_watermark_matches_the_pure_fingerprint_of_its_rows() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_model("waddles.a", 1, 0, 10),
                active_model("waddles.b", 1, 0, 3),
            ]])
            .into_connection();
        let watermark = read_watermark(&db, 1, 0).await?;
        assert_eq!(
            watermark,
            watermark_of(&[("waddles.a", 10), ("waddles.b", 3)])
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_watermark_is_a_fixed_empty_hash_on_an_empty_scope() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        let watermark = read_watermark(&db, 1, 0).await?;
        // The empty-scope watermark is SHA-256 of zero bytes, NOT
        // `Watermark::default()`'s derived empty string -- `Default` on
        // this type exists only so `WatermarkTracker` can hold `Option
        // <Watermark>`'s `None` case cleanly, never as a stand-in for "the
        // hash of an empty active set".
        assert_eq!(watermark, watermark_of(&[]));
        Ok(())
    }

    /// Security review fix: **the regression this `Watermark` type exists
    /// to prevent.** The retired `COUNT(*) + SUM(version_id)` watermark
    /// canceled here -- app A's `version_id` drops by 1 (10 -> 9) in the
    /// same tick app B's rises by 1 (5 -> 6): `COUNT` stays 2, `SUM` stays
    /// 15 both before and after, so that watermark would never move and
    /// the hot-swap would silently miss this change. The SHA-256
    /// fingerprint hashes each row's own `(app_id, version_id)` identity,
    /// not an aggregate that discards which row changed, so it cannot
    /// cancel this way.
    #[tokio::test]
    async fn read_watermark_moves_when_two_rows_shift_by_equal_and_opposite_amounts(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_model("waddles.a", 1, 0, 10),
                active_model("waddles.b", 1, 0, 5),
            ]])
            .append_query_results([vec![
                active_model("waddles.a", 1, 0, 9),
                active_model("waddles.b", 1, 0, 6),
            ]])
            .into_connection();

        let before = read_watermark(&db, 1, 0).await?;
        let after = read_watermark(&db, 1, 0).await?;
        assert_ne!(
            before, after,
            "A-1/B+1 in the same tick must still move the watermark"
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_returns_empty_when_no_rows_are_active() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(result.rows.is_empty());
        assert!(result.excluded.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_excludes_a_version_with_no_current_approval(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "b".repeat(64));
        // `MockDatabase::append_query_results` is generic per call over one
        // row type -- three sequential queries against three different
        // entities need three chained calls, not one array mixing
        // `Vec<app_active_versions::Model>`/`Vec<app_versions::Model>`/
        // `Vec<app_install_approvals::Model>` (which wouldn't type-check).
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
            }]])
            .append_query_results([Vec::<app_install_approvals::Model>::new()])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(
            result.rows.is_empty(),
            "no approval row -> excluded, got {:?}",
            result.rows
        );
        assert_eq!(
            result.excluded,
            vec![("waddles.test.app".to_string(), ExclusionReason::NoApproval)]
        );
        Ok(())
    }

    /// `component_key` contract: a `NULL` column value falls back to
    /// [`derive_component_keys`] and records a [`DegradedReason::
    /// MissingComponentKey`] entry -- the row still loads (never
    /// excluded), but the fallback is visible, not silent.
    #[tokio::test]
    async fn read_active_set_falls_back_to_derived_keys_when_component_key_is_null(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: None,
                sidecar_key: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                // Tenant-wide approval encoded as NULL (see the
                // sentinel-mismatch doc on the entity module) against
                // this active row's `community_id = 0` sentinel.
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].app_id, "waddles.test.app");
        assert_eq!(result.rows[0].digest, digest);
        let (expected_component, expected_sidecar) = derive_component_keys(&digest);
        assert_eq!(result.rows[0].component_key, expected_component);
        assert_eq!(result.rows[0].sidecar_key, expected_sidecar);
        assert!(result.excluded.is_empty());
        assert_eq!(
            result.degraded,
            vec![(
                "waddles.test.app".to_string(),
                DegradedReason::MissingComponentKey
            )]
        );
        Ok(())
    }

    /// `component_key` contract, the primary (non-fallback) path: a
    /// non-`NULL` `component_key`/`sidecar_key` column value is used
    /// directly, verbatim -- `derive_component_keys` is never consulted,
    /// and no degraded entry is recorded.
    #[tokio::test]
    async fn read_active_set_uses_the_real_component_key_column_when_present(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "f".repeat(64));
        let real_component_key = "bundles/waddles.test.app/1/real.wasm".to_string();
        let real_sidecar_key = "bundles/waddles.test.app/1/real.json".to_string();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: Some(real_component_key.clone()),
                sidecar_key: Some(real_sidecar_key.clone()),
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows.len(), 1);
        assert_eq!(result.rows[0].component_key, real_component_key);
        assert_eq!(result.rows[0].sidecar_key, real_sidecar_key);
        // Never the derived fallback -- proves the real column value won,
        // not a coincidental match.
        let (derived_component, _) = derive_component_keys(&digest);
        assert_ne!(result.rows[0].component_key, derived_component);
        assert!(result.excluded.is_empty());
        assert!(
            result.degraded.is_empty(),
            "a present component_key must never be recorded as degraded"
        );
        Ok(())
    }

    /// `sidecar_key` falls back independently of `component_key`: a real
    /// `component_key` with a `NULL` `sidecar_key` uses the real component
    /// key verbatim and only derives the sidecar half -- not treated as
    /// degraded (many bundles have no sidecar at all).
    #[tokio::test]
    async fn read_active_set_derives_only_the_sidecar_when_component_key_is_present_but_sidecar_is_null(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "a1".repeat(32));
        let real_component_key = "bundles/waddles.test.app/1/real.wasm".to_string();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: Some(digest.clone()),
                scan_status: "scanned".to_string(),
                component_key: Some(real_component_key.clone()),
                sidecar_key: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(result.rows[0].component_key, real_component_key);
        let (_, expected_sidecar) = derive_component_keys(&digest);
        assert_eq!(result.rows[0].sidecar_key, expected_sidecar);
        assert!(
            result.degraded.is_empty(),
            "a present component_key must never be recorded as degraded, even with a null sidecar_key"
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_excludes_a_version_missing_its_digest() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![app_active_versions::Model {
                app_id: "waddles.test.app".to_string(),
                tenant_id: 1,
                community_id: 0,
                version_id: 10,
            }]])
            .append_query_results([vec![app_versions::Model {
                id: 10,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                artifact_digest: None,
                scan_status: "not_scanned".to_string(),
                component_key: None,
                sidecar_key: None,
            }]])
            .append_query_results([vec![app_install_approvals::Model {
                id: 1,
                tenant_id: 1,
                community_id: None,
                app_id: "waddles.test.app".to_string(),
                version: "1".to_string(),
                superseded_by: None,
            }]])
            .into_connection();
        let result = read_active_set(&db, 1, 0, None).await?;
        assert!(result.rows.is_empty());
        assert_eq!(
            result.excluded,
            vec![(
                "waddles.test.app".to_string(),
                ExclusionReason::MissingDigest
            )]
        );
        Ok(())
    }
}

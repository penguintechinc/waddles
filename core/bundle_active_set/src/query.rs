//! The active-set read and its cheap watermark pre-check (spec: "poll a
//! cheap CHANGE-WATERMARK first and only do the full active-set re-read
//! ... when it moves"). Both queries are scoped to one `(tenant_id,
//! community_id)` pair and, for [`read_active_set`], an optional single
//! `app_id` -- callers pass `None` to manage the whole scope's active set
//! (spec's multi-app requirement) or `Some(app_id)` to scope to one bundle.

use sea_orm::sea_query::{Expr, Func};
use sea_orm::{ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QuerySelect};
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

/// A cheap, single-query change signal for one `(tenant_id, community_id)`
/// scope: the row count and the sum of `version_id` across
/// `app_active_versions`. Comparing this to the previous tick's value
/// detects every kind of change a full active-set read would (activation,
/// deactivation, and rollback/roll-forward alike) without needing
/// `app_active_versions` to carry its own `updated_at` column (it doesn't
/// -- migration `0022_app_versions_and_rbac` gives it only `activated_at`,
/// set by `DEFAULT NOW()` on INSERT with no confirmed guarantee that a
/// future activation-service's rollback UPDATE also bumps it -- see this
/// module's own doc and the crate root's flagged gap). `version_sum`
/// changes on ANY row's `version_id` UPDATE (a rollback to an older
/// `version_id` still changes the sum, since PK `(app_id, tenant_id,
/// community_id)` is unique -- the row's old `version_id` is replaced, not
/// duplicated) and `count` changes on any INSERT/DELETE, so the pair
/// covers activate/deactivate/rollback without relying on `activated_at`
/// semantics this crate doesn't control.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Default)]
pub struct Watermark {
    pub count: i64,
    pub version_sum: i64,
}

/// Reads [`Watermark`] for `(tenant_id, community_id)` -- one indexed
/// aggregate query, no join. Cheap enough to run every poll tick even when
/// nothing has changed.
pub async fn read_watermark(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
) -> Result<Watermark, ActiveSetError> {
    #[derive(sea_orm::FromQueryResult)]
    struct Agg {
        cnt: i64,
        vsum: Option<i64>,
    }

    let agg = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id))
        .select_only()
        .column_as(
            Expr::from(Func::count(Expr::col(app_active_versions::Column::AppId))),
            "cnt",
        )
        .column_as(
            Expr::from(Func::sum(Expr::col(app_active_versions::Column::VersionId))),
            "vsum",
        )
        .into_model::<Agg>()
        .one(conn)
        .await?
        .unwrap_or(Agg { cnt: 0, vsum: None });

    Ok(Watermark {
        count: agg.cnt,
        version_sum: agg.vsum.unwrap_or(0),
    })
}

/// Content-addressed `component_key`/`sidecar_key` derivation --
/// **documented interim substitute for a confirmed schema gap**:
/// `app_versions` has no persisted bucket-key column at all (see
/// `crate::entities::app_versions`'s module doc). `expected` is the
/// `sha256:<64 hex>` `artifact_digest`; this strips the `sha256:` prefix
/// and keys both objects by the raw hex digest under `bundles/`, matching
/// the `ADDRESSING` state name in hub-api's publish state machine
/// (`hub_api/services/bundle_version_service.py`) -- i.e. this assumes
/// the eventual publish step will store bundles content-addressed by
/// digest. Replace this function's body (not its callers) once hub-api's
/// publish step lands and persists real bucket keys.
pub fn derive_component_keys(digest: &str) -> (String, String) {
    let hex = digest.strip_prefix("sha256:").unwrap_or(digest);
    (
        format!("bundles/{hex}/component.wasm"),
        format!("bundles/{hex}/sidecar.json"),
    )
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
/// concurrent publish), is silently excluded rather than erroring the
/// whole read -- one bad/incomplete row must never block every other
/// bundle's hot-swap.
pub async fn read_active_set(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    app_id: Option<&str>,
) -> Result<Vec<ActiveBundleRow>, ActiveSetError> {
    let mut active_q = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id));
    if let Some(app_id) = app_id {
        active_q = active_q.filter(app_active_versions::Column::AppId.eq(app_id));
    }
    let active_rows = active_q.all(conn).await?;
    if active_rows.is_empty() {
        return Ok(Vec::new());
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

    let mut out = Vec::with_capacity(active_rows.len());
    for active in &active_rows {
        let Some(version_row) = versions_by_id.get(&active.version_id) else {
            tracing::warn!(
                app_id = %active.app_id,
                version_id = active.version_id,
                "active_versions points at a version_id with no app_versions row; skipping"
            );
            continue;
        };

        let approved = approval_rows.iter().any(|appr| {
            appr.app_id == active.app_id
                && appr.version == version_row.version
                && (appr.community_id == Some(active.community_id)
                    || (appr.community_id.is_none() && active.community_id == 0))
        });
        if !approved {
            tracing::debug!(
                app_id = %active.app_id,
                version = %version_row.version,
                "active version has no current install approval; excluding from active set"
            );
            continue;
        }

        let Some(digest) = version_row.artifact_digest.clone() else {
            tracing::warn!(
                app_id = %active.app_id,
                version = %version_row.version,
                "active, approved version has no artifact_digest yet; excluding from active set"
            );
            continue;
        };

        let (component_key, sidecar_key) = derive_component_keys(&digest);
        out.push(ActiveBundleRow {
            app_id: active.app_id.clone(),
            version: version_row.version.clone(),
            digest,
            component_key,
            sidecar_key,
        });
    }

    Ok(out)
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
        let changed = self.last != Some(current);
        self.last = Some(current);
        changed
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    #[test]
    fn watermark_tracker_reports_changed_on_the_first_observation() {
        let mut tracker = WatermarkTracker::new();
        assert!(tracker.observe(Watermark {
            count: 1,
            version_sum: 10
        }));
    }

    #[test]
    fn watermark_tracker_reports_unchanged_when_the_watermark_repeats() {
        let mut tracker = WatermarkTracker::new();
        let w = Watermark {
            count: 1,
            version_sum: 10,
        };
        assert!(tracker.observe(w));
        assert!(
            !tracker.observe(w),
            "an unchanged watermark must skip the full read"
        );
        assert!(!tracker.observe(w));
    }

    #[test]
    fn watermark_tracker_reports_changed_when_count_or_sum_moves() {
        let mut tracker = WatermarkTracker::new();
        assert!(tracker.observe(Watermark {
            count: 1,
            version_sum: 10
        }));
        assert!(tracker.observe(Watermark {
            count: 2,
            version_sum: 10
        }));
        assert!(tracker.observe(Watermark {
            count: 2,
            version_sum: 99
        }));
        assert!(!tracker.observe(Watermark {
            count: 2,
            version_sum: 99
        }));
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

    #[tokio::test]
    async fn read_watermark_reads_count_and_version_sum() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([[maplit_row(3, Some(42))]])
            .into_connection();
        let watermark = read_watermark(&db, 1, 0).await?;
        assert_eq!(
            watermark,
            Watermark {
                count: 3,
                version_sum: 42
            }
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_watermark_defaults_to_zero_on_an_empty_scope() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([[maplit_row(0, None)]])
            .into_connection();
        let watermark = read_watermark(&db, 1, 0).await?;
        assert_eq!(watermark, Watermark::default());
        Ok(())
    }

    /// Builds a mocked aggregate row for `read_watermark`'s
    /// `into_model::<Agg>()` -- `sea_orm`'s `MockDatabase` accepts rows as
    /// `BTreeMap<String, sea_orm::Value>` (`IntoMockRow`), keyed by the
    /// exact column aliases `read_watermark` selects (`cnt`/`vsum`).
    fn maplit_row(
        cnt: i64,
        vsum: Option<i64>,
    ) -> std::collections::BTreeMap<String, sea_orm::Value> {
        let mut m = std::collections::BTreeMap::new();
        m.insert("cnt".to_string(), sea_orm::Value::BigInt(Some(cnt)));
        m.insert("vsum".to_string(), sea_orm::Value::BigInt(vsum));
        m
    }

    #[tokio::test]
    async fn read_active_set_returns_empty_when_no_rows_are_active() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        let rows = read_active_set(&db, 1, 0, None).await?;
        assert!(rows.is_empty());
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
            }]])
            .append_query_results([Vec::<app_install_approvals::Model>::new()])
            .into_connection();
        let rows = read_active_set(&db, 1, 0, None).await?;
        assert!(rows.is_empty(), "no approval row -> excluded, got {rows:?}");
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_includes_an_active_and_approved_tenant_wide_version(
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
        let rows = read_active_set(&db, 1, 0, None).await?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].app_id, "waddles.test.app");
        assert_eq!(rows[0].digest, digest);
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
        let rows = read_active_set(&db, 1, 0, None).await?;
        assert!(rows.is_empty());
        Ok(())
    }
}

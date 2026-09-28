//! Multi-tenant active-set + source-binding reads (dataplane scale design
//! rev 4, user requirement: "every svc_process/svc_action pod serves ALL
//! tenants"). Replaces the single-`(tenant_id, community_id)`-scoped
//! `crate::query::read_active_set`/`crate::bindings::read_source_bindings`
//! as the PRIMARY read path for the multi-tenant change-log consumer (each
//! service's own `changelog_consumer` module) -- those single-scope
//! functions are NOT retired (still used for the affected-scope
//! incremental re-read after a change-log tick, see this module's own
//! `read_active_set_all` doc), only no longer the sole entry point.
//!
//! Both reads here do THREE bulk queries total (active rows, versions,
//! approvals/bindings) regardless of how many distinct `(tenant_id,
//! community_id)` scopes exist, then bucket the results in Rust -- avoiding
//! the O(tenant count) query fan-out a naive "loop `read_active_set` over
//! every known scope" implementation would cost at startup / full-reconcile
//! time (design §0 sizing: 100s of tenants, 10,000s of communities).

use std::collections::{HashMap, HashSet};

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::bindings::SourceBinding;
use crate::entities::{
    app_active_versions, app_install_approvals, app_source_bindings, app_versions,
};
use crate::query::{assemble_active_set, ActiveBundleRow, ActiveSetError, ActiveSetRead};

/// A `(tenant_id, community_id)` pair -- the same scope key
/// `app_active_versions`'s own composite key (minus `app_id`) already
/// names; given its own type alias here so every multi-tenant caller
/// (`crate::changelog`, each service's `changelog_consumer`) spells it
/// identically instead of repeating the raw tuple type.
pub type ScopeKey = (i32, i32);

/// Reads the ACTIVE, APPROVED set for **every** `(tenant_id, community_id)`
/// scope in the database, bucketed by scope -- the "on start, full
/// active-set read for ALL tenants/communities" requirement, and the
/// periodic full-reconcile safety net (dataplane scale design §7: "bounds
/// the blast radius of any change-log defect to one interval, independent
/// of the change-log's own correctness"). An empty database (no active
/// rows at all) returns an empty map, never an error.
pub async fn read_active_set_all(
    conn: &DatabaseConnection,
) -> Result<HashMap<ScopeKey, ActiveSetRead>, ActiveSetError> {
    let active_rows = app_active_versions::Entity::find().all(conn).await?;
    if active_rows.is_empty() {
        return Ok(HashMap::new());
    }

    let version_ids: Vec<i64> = active_rows.iter().map(|r| r.version_id).collect();
    let version_rows = app_versions::Entity::find()
        .filter(app_versions::Column::Id.is_in(version_ids))
        .all(conn)
        .await?;
    let versions_by_id: HashMap<i64, app_versions::Model> =
        version_rows.into_iter().map(|v| (v.id, v)).collect();

    // UNFILTERED across every tenant -- one bulk query instead of one per
    // scope. `crate::query::assemble_active_set`'s explicit `tenant_id`
    // comparison (see that function's own doc) is what keeps this safe:
    // an approval row can only ever satisfy an active row from its own
    // tenant, never cross-matched just because two tenants happen to share
    // an `app_id`/`version` string.
    let approval_rows = app_install_approvals::Entity::find()
        .filter(app_install_approvals::Column::SupersededBy.is_null())
        .all(conn)
        .await?;

    let mut by_scope: HashMap<ScopeKey, Vec<app_active_versions::Model>> = HashMap::new();
    for row in active_rows {
        by_scope
            .entry((row.tenant_id, row.community_id))
            .or_default()
            .push(row);
    }

    let mut result = HashMap::with_capacity(by_scope.len());
    for (scope, rows) in by_scope {
        result.insert(
            scope,
            assemble_active_set(&rows, &versions_by_id, &approval_rows),
        );
    }
    Ok(result)
}

/// Reads every `app_source_bindings` row for a currently-ACTIVE app, for
/// **every** tenant/community, bucketed by scope -- the multi-tenant
/// counterpart to `crate::bindings::read_source_bindings`, same "ACTIVE
/// apps only" scoping rule (see that function's own doc), same two-bulk-
/// query shape as [`read_active_set_all`] rather than one query pair per
/// scope.
pub async fn read_source_bindings_all(
    conn: &DatabaseConnection,
) -> Result<HashMap<ScopeKey, Vec<SourceBinding>>, ActiveSetError> {
    let active_keys: HashSet<(i32, i32, String)> = app_active_versions::Entity::find()
        .all(conn)
        .await?
        .into_iter()
        .map(|row| (row.tenant_id, row.community_id, row.app_id))
        .collect();
    if active_keys.is_empty() {
        return Ok(HashMap::new());
    }

    let binding_rows = app_source_bindings::Entity::find().all(conn).await?;

    let mut result: HashMap<ScopeKey, Vec<SourceBinding>> = HashMap::new();
    for row in binding_rows {
        let key = (row.tenant_id, row.community_id, row.app_id.clone());
        if active_keys.contains(&key) {
            result
                .entry((row.tenant_id, row.community_id))
                .or_default()
                .push(SourceBinding {
                    app_id: row.app_id,
                    platform: row.platform,
                    source_id: row.source_id,
                });
        }
    }
    Ok(result)
}

/// Flattens a per-scope active-set map into the flat `app_id`-keyed list
/// each service's own executor `Load`/`Unload` wire calls operate on
/// (`crate::diff::plan`'s own signature -- unchanged by this crate's move
/// to multi-tenant, see this module's own root doc for why: the executor's
/// `on_load` registry has always been keyed by `app_id` alone, one process
/// wide, never per-tenant).
///
/// **Known limitation, explicit by design, not silently glossed over:** if
/// the SAME `app_id` is independently active with two DIFFERENT digests
/// across two different `(tenant_id, community_id)` scopes (e.g. a gradual
/// per-tenant version rollout), only one digest can occupy the executor's
/// single `app_id` registry slot at a time. This function resolves that
/// conflict deterministically -- scopes are visited in sorted
/// `(tenant_id, community_id)` order, first-seen digest for a given
/// `app_id` wins, every later scope's conflicting row is dropped -- and
/// reports the conflict count so a caller can drive a Prometheus counter
/// (never silently pick a "wrong" tenant's version with no signal). Real
/// per-tenant bundle isolation is out of scope for this change (dataplane
/// scale design §3's lazy-load/LRU-by-digest executor model, migration
/// steps 5/6, is the eventual fix -- not yet built).
pub fn flatten_by_scope(
    by_scope: &HashMap<ScopeKey, ActiveSetRead>,
) -> (Vec<ActiveBundleRow>, u64) {
    let mut scopes: Vec<&ScopeKey> = by_scope.keys().collect();
    scopes.sort();

    let mut seen: HashMap<&str, &str> = HashMap::new();
    let mut conflicts = 0u64;
    let mut rows = Vec::new();
    for scope in scopes {
        for row in &by_scope[scope].rows {
            match seen.get(row.app_id.as_str()) {
                Some(existing_digest) if *existing_digest == row.digest.as_str() => {}
                Some(_) => {
                    conflicts += 1;
                }
                None => {
                    seen.insert(row.app_id.as_str(), row.digest.as_str());
                    rows.push(row.clone());
                }
            }
        }
    }
    (rows, conflicts)
}

/// Per-tenant active-app counts across every community -- the
/// `waddles_tenant_active_apps` gauge's exact value set, labeled by
/// `tenant_id` ONLY (bounded cardinality: 100s of tenants per design §0
/// sizing) rather than by `(tenant_id, community_id)` or `app_id`, either
/// of which would be 10,000s-to-30,000s-wide and an unbounded-cardinality
/// metric-explosion hazard (`rules/critical-rules.md` Observability).
pub fn tenant_active_app_counts(by_scope: &HashMap<ScopeKey, ActiveSetRead>) -> HashMap<i32, i64> {
    let mut counts: HashMap<i32, i64> = HashMap::new();
    for ((tenant_id, _community_id), active_set) in by_scope {
        *counts.entry(*tenant_id).or_insert(0) += active_set.rows.len() as i64;
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn active_row(
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

    fn version_row(id: i64, app_id: &str, version: &str, digest: &str) -> app_versions::Model {
        app_versions::Model {
            id,
            app_id: app_id.to_string(),
            version: version.to_string(),
            artifact_digest: Some(digest.to_string()),
            scan_status: "scanned".to_string(),
            component_key: None,
            sidecar_key: None,
        }
    }

    fn approval_row(
        tenant_id: i32,
        community_id: Option<i32>,
        app_id: &str,
        version: &str,
    ) -> app_install_approvals::Model {
        app_install_approvals::Model {
            id: 1,
            tenant_id,
            community_id,
            app_id: app_id.to_string(),
            version: version.to_string(),
            superseded_by: None,
        }
    }

    fn binding_row(
        tenant_id: i32,
        community_id: i32,
        app_id: &str,
        platform: &str,
        source_id: &str,
    ) -> app_source_bindings::Model {
        app_source_bindings::Model {
            tenant_id,
            community_id,
            app_id: app_id.to_string(),
            platform: platform.to_string(),
            source_id: source_id.to_string(),
        }
    }

    #[tokio::test]
    async fn read_active_set_all_returns_empty_map_for_an_empty_database(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        assert!(read_active_set_all(&db).await?.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn read_active_set_all_buckets_rows_by_their_own_scope() -> Result<(), ActiveSetError> {
        let digest_a = format!("sha256:{}", "a".repeat(64));
        let digest_b = format!("sha256:{}", "b".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_row("waddles.a", 1, 0, 10),
                active_row("waddles.b", 2, 5, 20),
            ]])
            .append_query_results([vec![
                version_row(10, "waddles.a", "1", &digest_a),
                version_row(20, "waddles.b", "1", &digest_b),
            ]])
            .append_query_results([vec![
                approval_row(1, None, "waddles.a", "1"),
                approval_row(2, Some(5), "waddles.b", "1"),
            ]])
            .into_connection();

        let by_scope = read_active_set_all(&db).await?;
        assert_eq!(by_scope.len(), 2);
        assert_eq!(by_scope[&(1, 0)].rows.len(), 1);
        assert_eq!(by_scope[&(1, 0)].rows[0].app_id, "waddles.a");
        assert_eq!(by_scope[&(2, 5)].rows.len(), 1);
        assert_eq!(by_scope[&(2, 5)].rows[0].app_id, "waddles.b");
        Ok(())
    }

    /// The exact regression `crate::query::assemble_active_set`'s tenant
    /// check exists to prevent: two tenants' apps sharing an `app_id` and
    /// `version` string must NOT cross-match each other's approval when
    /// `approval_rows` is read unfiltered across every tenant (this
    /// function's own bulk-query shape).
    #[tokio::test]
    async fn read_active_set_all_does_not_cross_match_approvals_across_tenants(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_row("waddles.shared", 1, 0, 10),
                active_row("waddles.shared", 2, 0, 20),
            ]])
            .append_query_results([vec![
                version_row(10, "waddles.shared", "1", &digest),
                version_row(20, "waddles.shared", "1", &digest),
            ]])
            // Only tenant 1 has a current approval for "waddles.shared"@1.
            .append_query_results([vec![approval_row(1, None, "waddles.shared", "1")]])
            .into_connection();

        let by_scope = read_active_set_all(&db).await?;
        assert_eq!(by_scope[&(1, 0)].rows.len(), 1, "tenant 1 is approved");
        assert!(
            by_scope[&(2, 0)].rows.is_empty(),
            "tenant 2 must not inherit tenant 1's approval"
        );
        assert_eq!(
            by_scope[&(2, 0)].excluded,
            vec![(
                "waddles.shared".to_string(),
                crate::query::ExclusionReason::NoApproval
            )]
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_source_bindings_all_returns_empty_map_when_nothing_is_active(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        assert!(read_source_bindings_all(&db).await?.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn read_source_bindings_all_buckets_bindings_by_scope_and_excludes_inactive(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_row("waddles.a", 1, 0, 10),
                active_row("waddles.b", 2, 5, 20),
            ]])
            .append_query_results([vec![
                binding_row(1, 0, "waddles.a", "twitch", "tw-x"),
                binding_row(2, 5, "waddles.b", "discord", "dg-y"),
                // Inactive app in tenant 1 -- must not appear anywhere.
                binding_row(1, 0, "waddles.inactive", "twitch", "tw-z"),
            ]])
            .into_connection();

        let by_scope = read_source_bindings_all(&db).await?;
        assert_eq!(by_scope.len(), 2);
        assert_eq!(by_scope[&(1, 0)].len(), 1);
        assert_eq!(by_scope[&(1, 0)][0].app_id, "waddles.a");
        assert_eq!(by_scope[&(2, 5)].len(), 1);
        assert_eq!(by_scope[&(2, 5)][0].app_id, "waddles.b");
        Ok(())
    }

    fn bundle_row(app_id: &str, digest: &str) -> ActiveBundleRow {
        ActiveBundleRow {
            app_id: app_id.to_string(),
            version: "1".to_string(),
            digest: digest.to_string(),
            component_key: format!("bundles/{digest}/component.wasm"),
            sidecar_key: format!("bundles/{digest}/sidecar.json"),
        }
    }

    fn active_set(rows: Vec<ActiveBundleRow>) -> ActiveSetRead {
        ActiveSetRead {
            rows,
            excluded: Vec::new(),
            degraded: Vec::new(),
        }
    }

    #[test]
    fn flatten_by_scope_merges_disjoint_apps_across_scopes_with_no_conflicts() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![bundle_row("waddles.a", "d1")]));
        by_scope.insert((2, 0), active_set(vec![bundle_row("waddles.b", "d2")]));

        let (rows, conflicts) = flatten_by_scope(&by_scope);
        assert_eq!(conflicts, 0);
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().any(|r| r.app_id == "waddles.a"));
        assert!(rows.iter().any(|r| r.app_id == "waddles.b"));
    }

    #[test]
    fn flatten_by_scope_deduplicates_the_same_app_id_and_digest_across_scopes() {
        // Two different tenants both install the SAME app_id at the SAME
        // digest -- must appear once, not twice, in the flattened result
        // (the executor's `on_load` registry is keyed by app_id alone).
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![bundle_row("waddles.shared", "d1")]));
        by_scope.insert((2, 0), active_set(vec![bundle_row("waddles.shared", "d1")]));

        let (rows, conflicts) = flatten_by_scope(&by_scope);
        assert_eq!(
            conflicts, 0,
            "identical digest across scopes is not a conflict"
        );
        assert_eq!(rows.len(), 1);
    }

    #[test]
    fn flatten_by_scope_reports_a_conflict_and_keeps_the_first_scope_deterministically() {
        // Same app_id, DIFFERENT digests, in two different scopes -- the
        // known cross-tenant-version-conflict limitation this function's
        // own doc describes. Sorted scope order makes (1,0) always win
        // over (2,0), deterministically.
        let mut by_scope = HashMap::new();
        by_scope.insert((2, 0), active_set(vec![bundle_row("waddles.shared", "d2")]));
        by_scope.insert((1, 0), active_set(vec![bundle_row("waddles.shared", "d1")]));

        let (rows, conflicts) = flatten_by_scope(&by_scope);
        assert_eq!(conflicts, 1);
        assert_eq!(rows.len(), 1);
        assert_eq!(
            rows[0].digest, "d1",
            "the lowest-sorted scope (1,0) must win deterministically"
        );
    }

    #[test]
    fn flatten_by_scope_is_empty_for_an_empty_map() {
        let (rows, conflicts) = flatten_by_scope(&HashMap::new());
        assert!(rows.is_empty());
        assert_eq!(conflicts, 0);
    }

    #[test]
    fn tenant_active_app_counts_sums_across_communities_within_one_tenant() {
        let mut by_scope = HashMap::new();
        by_scope.insert(
            (7, 0),
            active_set(vec![
                bundle_row("waddles.a", "d1"),
                bundle_row("waddles.b", "d2"),
            ]),
        );
        by_scope.insert((7, 5), active_set(vec![bundle_row("waddles.c", "d3")]));
        by_scope.insert((9, 0), active_set(vec![bundle_row("waddles.d", "d4")]));

        let counts = tenant_active_app_counts(&by_scope);
        assert_eq!(counts.len(), 2, "bounded by tenant count, not scope count");
        assert_eq!(counts[&7], 3);
        assert_eq!(counts[&9], 1);
    }

    #[test]
    fn tenant_active_app_counts_is_empty_for_an_empty_map() {
        assert!(tenant_active_app_counts(&HashMap::new()).is_empty());
    }
}

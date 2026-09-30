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

use sea_orm::{
    AccessMode, ColumnTrait, ConnectionTrait, DatabaseConnection, EntityTrait, IsolationLevel,
    QueryFilter, TransactionTrait,
};

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

/// A `(tenant_id, community_id, app_id)` triple -- the full identity of
/// "one binding's currently-active-or-loaded app" once a scope is no
/// longer collapsed away (see [`scoped_active_rows`]'s doc for why this
/// replaced the app_id-only flattening this crate used to do).
pub type AppScope = (i32, i32, String);

/// `app_versions::Column::Id.is_in(..)` batch size: Postgres has no hard
/// limit on an `IN (...)` list, but an unbounded one (10,000s of version
/// ids at this design's sizing, §0) risks pathological planning time and an
/// oversized prepared-statement parameter list. Chunking is transparent to
/// every caller -- [`find_versions_chunked`] merges the chunks back into one
/// `Vec` before returning.
const VERSION_ID_CHUNK_SIZE: usize = 1000;

/// Reads `app_versions` rows for `version_ids` in batches of
/// [`VERSION_ID_CHUNK_SIZE`] rather than one unbounded `IN (...)` -- see
/// that constant's doc. Generic over `ConnectionTrait` so callers can pass
/// either a plain `&DatabaseConnection` or a `&DatabaseTransaction` (this
/// module's own callers always pass the latter, see [`read_active_set_all`]'s
/// doc for why the three reads share one transaction).
async fn find_versions_chunked<C: ConnectionTrait>(
    conn: &C,
    version_ids: &[i64],
) -> Result<Vec<app_versions::Model>, ActiveSetError> {
    let mut all = Vec::with_capacity(version_ids.len());
    for chunk in version_ids.chunks(VERSION_ID_CHUNK_SIZE) {
        let rows = app_versions::Entity::find()
            .filter(app_versions::Column::Id.is_in(chunk.to_vec()))
            .all(conn)
            .await?;
        all.extend(rows);
    }
    Ok(all)
}

/// Reads the ACTIVE, APPROVED set for **every** `(tenant_id, community_id)`
/// scope in the database, bucketed by scope -- the "on start, full
/// active-set read for ALL tenants/communities" requirement, and the
/// periodic full-reconcile safety net (dataplane scale design §7: "bounds
/// the blast radius of any change-log defect to one interval, independent
/// of the change-log's own correctness"). An empty database (no active
/// rows at all) returns an empty map, never an error.
///
/// **Snapshot consistency (security/correctness review):** the three reads
/// below (active rows, versions, approvals) run inside one `REPEATABLE
/// READ` read-only transaction, not as three independent autocommit
/// queries -- without that, a concurrent write between reads (e.g. a
/// version row deleted, or an approval superseded, in the moment between
/// this function's first and third query) could assemble a torn view: an
/// active row referencing a version that "disappeared" mid-read, or an
/// approval matched against a since-changed version string. `REPEATABLE
/// READ` pins this transaction to a single consistent snapshot for its
/// whole duration, so all three queries see the exact same point in time
/// regardless of what else commits concurrently -- Postgres's own
/// definition of that isolation level, not something this crate emulates.
/// `AccessMode::ReadOnly` is defense-in-depth alongside the RO account's
/// grants and `crate::reader::connect`'s session-level read-only option
/// (belt-and-suspenders, matching this crate's existing convention).
/// `crate::query::assemble_active_set`'s own exclusion handling (missing
/// version row / no approval / missing digest, see its doc) is what keeps
/// this graceful even for a row genuinely deleted just before the snapshot
/// was taken -- excluded, never a panic or a hard error.
pub async fn read_active_set_all(
    conn: &DatabaseConnection,
) -> Result<HashMap<ScopeKey, ActiveSetRead>, ActiveSetError> {
    let txn = conn
        .begin_with_config(
            Some(IsolationLevel::RepeatableRead),
            Some(AccessMode::ReadOnly),
        )
        .await?;

    let active_rows = app_active_versions::Entity::find().all(&txn).await?;
    if active_rows.is_empty() {
        txn.commit().await?;
        return Ok(HashMap::new());
    }

    let version_ids: Vec<i64> = active_rows.iter().map(|r| r.version_id).collect();
    let version_rows = find_versions_chunked(&txn, &version_ids).await?;
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
        .all(&txn)
        .await?;
    txn.commit().await?;

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
    // Same snapshot-consistency rationale as `read_active_set_all`'s own
    // doc: a binding for an app that gets deactivated between these two
    // reads must not be spuriously included (or a newly-activated app's
    // binding spuriously excluded) just because the two queries landed on
    // different points in time.
    let txn = conn
        .begin_with_config(
            Some(IsolationLevel::RepeatableRead),
            Some(AccessMode::ReadOnly),
        )
        .await?;

    let active_keys: HashSet<(i32, i32, String)> = app_active_versions::Entity::find()
        .all(&txn)
        .await?
        .into_iter()
        .map(|row| (row.tenant_id, row.community_id, row.app_id))
        .collect();
    if active_keys.is_empty() {
        txn.commit().await?;
        return Ok(HashMap::new());
    }

    let binding_rows = app_source_bindings::Entity::find().all(&txn).await?;
    txn.commit().await?;

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

/// Flattens a per-scope active-set map into an [`AppScope`]-keyed map --
/// **scope-preserving, no cross-scope deduplication or "first seen wins"
/// collapsing** (replaces the retired `flatten_by_scope`, which collapsed
/// onto a flat `app_id`-keyed list because the executor's registry used to
/// be keyed by `app_id` alone, one process-wide slot; see
/// `core/bundle_executor/src/invoke.rs`'s module doc for the digest-keyed,
/// refcounted registry that replaced it).
///
/// The SAME `app_id` independently active with two DIFFERENT digests across
/// two different `(tenant_id, community_id)` scopes (e.g. a gradual
/// per-tenant version rollout) is now simply two distinct [`AppScope`]
/// entries -- each service's own `changelog_consumer::apply_active_set`
/// diffs this map against its own [`AppScope`]-keyed `loaded` state
/// (`crate::diff::plan_scoped`) and sends one independent `Load`/`Unload`
/// pair per scope, which the executor's digest-keyed, refcounted registry
/// resolves correctly -- there is no conflict left to detect or report
/// here; two scopes referencing different digests for the same `app_id` is
/// simply the expected, correctly-isolated multi-tenant case.
pub fn scoped_active_rows(
    by_scope: &HashMap<ScopeKey, ActiveSetRead>,
) -> HashMap<AppScope, ActiveBundleRow> {
    let mut rows = HashMap::new();
    for (&(tenant_id, community_id), active_set) in by_scope {
        for row in &active_set.rows {
            rows.insert((tenant_id, community_id, row.app_id.clone()), row.clone());
        }
    }
    rows
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
            artifact_signature: None,
            artifact_signature_key_id: None,
            artifact_signed_approval_id: None,
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

    /// Snapshot-consistency regression: the three reads must run inside ONE
    /// transaction, not three independent autocommit queries -- a
    /// `MockDatabase` flushes an open transaction's statements into exactly
    /// one `transaction_log` entry on commit (vs. one entry per
    /// autocommit-query call), so asserting a single entry here directly
    /// proves the three queries share one snapshot rather than each
    /// observing a potentially different point in time.
    #[tokio::test]
    async fn read_active_set_all_runs_its_three_reads_in_one_transaction(
    ) -> Result<(), ActiveSetError> {
        let digest = format!("sha256:{}", "a".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", "1", &digest)]])
            .append_query_results([vec![approval_row(1, None, "waddles.a", "1")]])
            .into_connection();
        read_active_set_all(&db).await?;
        let log = db.into_transaction_log();
        assert_eq!(
            log.len(),
            1,
            "all three reads must be grouped into one committed transaction, got {log:?}"
        );
        Ok(())
    }

    /// `.is_in(..)` batching regression: with more `version_id`s than
    /// [`VERSION_ID_CHUNK_SIZE`], `find_versions_chunked` must issue
    /// multiple queries and merge every chunk's rows back together -- proven
    /// by queuing one distinct result set per expected chunk (a single
    /// unbounded query would leave the second chunk's queued result unread,
    /// and the merged output would be missing that chunk's row).
    #[tokio::test]
    async fn find_versions_chunked_batches_and_merges_large_id_lists() -> Result<(), ActiveSetError>
    {
        let ids: Vec<i64> = (1..=(VERSION_ID_CHUNK_SIZE as i64 + 1)).collect();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![version_row(1, "waddles.a", "1", "sha256:aa")]])
            .append_query_results([vec![version_row(
                VERSION_ID_CHUNK_SIZE as i64 + 1,
                "waddles.b",
                "1",
                "sha256:bb",
            )]])
            .into_connection();
        let rows = find_versions_chunked(&db, &ids).await?;
        assert_eq!(
            rows.len(),
            2,
            "both chunks' queued results must be consumed and merged"
        );
        assert!(rows.iter().any(|r| r.app_id == "waddles.a"));
        assert!(rows.iter().any(|r| r.app_id == "waddles.b"));
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
            artifact_signature: None,
            artifact_signature_key_id: None,
            artifact_signed_approval_id: None,
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
    fn scoped_active_rows_keys_disjoint_apps_by_their_own_scope() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![bundle_row("waddles.a", "d1")]));
        by_scope.insert((2, 0), active_set(vec![bundle_row("waddles.b", "d2")]));

        let rows = scoped_active_rows(&by_scope);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[&(1, 0, "waddles.a".to_string())].digest, "d1");
        assert_eq!(rows[&(2, 0, "waddles.b".to_string())].digest, "d2");
    }

    /// **The primary regression test for this multi-tenant correctness
    /// fix:** the SAME `app_id`, independently active at TWO DIFFERENT
    /// digests in two different scopes, must produce TWO entries -- never
    /// collapsed onto one (the retired `flatten_by_scope`'s "first seen
    /// wins" behavior, which silently ran the wrong bundle version for
    /// whichever scope lost the collapse).
    #[test]
    fn scoped_active_rows_keeps_both_digests_for_the_same_app_id_across_scopes() {
        let mut by_scope = HashMap::new();
        by_scope.insert((2, 0), active_set(vec![bundle_row("waddles.shared", "d2")]));
        by_scope.insert((1, 0), active_set(vec![bundle_row("waddles.shared", "d1")]));

        let rows = scoped_active_rows(&by_scope);
        assert_eq!(rows.len(), 2, "no cross-tenant collapsing must occur");
        assert_eq!(rows[&(1, 0, "waddles.shared".to_string())].digest, "d1");
        assert_eq!(rows[&(2, 0, "waddles.shared".to_string())].digest, "d2");
    }

    /// The same `app_id` at the SAME digest across two scopes is simply two
    /// independent entries too (the executor's own registry, not this
    /// function, is what dedupes a shared digest down to one compiled
    /// component -- see `core/bundle_executor/src/invoke.rs`'s refcounted
    /// registry).
    #[test]
    fn scoped_active_rows_keeps_independent_entries_even_for_an_identical_shared_digest() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![bundle_row("waddles.shared", "d1")]));
        by_scope.insert((2, 0), active_set(vec![bundle_row("waddles.shared", "d1")]));

        let rows = scoped_active_rows(&by_scope);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[&(1, 0, "waddles.shared".to_string())].digest, "d1");
        assert_eq!(rows[&(2, 0, "waddles.shared".to_string())].digest, "d1");
    }

    #[test]
    fn scoped_active_rows_is_empty_for_an_empty_map() {
        assert!(scoped_active_rows(&HashMap::new()).is_empty());
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

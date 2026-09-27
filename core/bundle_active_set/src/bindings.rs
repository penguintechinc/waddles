//! Per-`(tenant, community)` source-stream bindings for currently ACTIVE
//! apps -- the query `svc_process`'s source-binding supervisor polls to
//! decide which `(app_id, platform, source_id)` consumer tasks should be
//! running (spec: hub-api is the sole writer of `app_source_bindings`;
//! this stage only ever reads it).
//!
//! Scoped to ACTIVE apps only (an `app_active_versions` row exists for the
//! `app_id` in this `(tenant_id, community_id)` scope) -- deliberately
//! *not* also gated on APPROVED the way [`crate::query::read_active_set`]
//! is: a binding for an app that is active but not yet approved (or whose
//! version has no digest yet) still resolves here, and the consumer it
//! spawns simply dead-letters every delivered entry as `BundleError`/
//! `ExecutorUnavailable` until the bundle actually loads (the same
//! graceful "not ready yet" handling `crate::spine::handle_delivered`
//! already gives an empty `digest`) -- rather than silently never
//! subscribing to a stream a binding row says it should.

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::entities::{app_active_versions, app_source_bindings};
use crate::query::ActiveSetError;

/// One source stream a currently ACTIVE app in this scope is bound to.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceBinding {
    pub app_id: String,
    pub platform: String,
    pub source_id: String,
}

/// Reads every `app_source_bindings` row for `(tenant_id, community_id)`
/// whose `app_id` currently has an `app_active_versions` row in the same
/// scope -- two sequential queries (no `Relation` wiring, matching this
/// crate's established join-in-Rust convention, see `crate::entities`'s
/// module doc) rather than a single SQL join.
pub async fn read_source_bindings(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
) -> Result<Vec<SourceBinding>, ActiveSetError> {
    let active_app_ids: std::collections::HashSet<String> = app_active_versions::Entity::find()
        .filter(app_active_versions::Column::TenantId.eq(tenant_id))
        .filter(app_active_versions::Column::CommunityId.eq(community_id))
        .all(conn)
        .await?
        .into_iter()
        .map(|row| row.app_id)
        .collect();
    if active_app_ids.is_empty() {
        return Ok(Vec::new());
    }

    let bindings = app_source_bindings::Entity::find()
        .filter(app_source_bindings::Column::TenantId.eq(tenant_id))
        .filter(app_source_bindings::Column::CommunityId.eq(community_id))
        .all(conn)
        .await?
        .into_iter()
        .filter(|row| active_app_ids.contains(&row.app_id))
        .map(|row| SourceBinding {
            app_id: row.app_id,
            platform: row.platform,
            source_id: row.source_id,
        })
        .collect();
    Ok(bindings)
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn active_row(app_id: &str) -> app_active_versions::Model {
        app_active_versions::Model {
            app_id: app_id.to_string(),
            tenant_id: 1,
            community_id: 0,
            version_id: 10,
        }
    }

    fn binding_row(app_id: &str, platform: &str, source_id: &str) -> app_source_bindings::Model {
        app_source_bindings::Model {
            tenant_id: 1,
            community_id: 0,
            app_id: app_id.to_string(),
            platform: platform.to_string(),
            source_id: source_id.to_string(),
        }
    }

    #[tokio::test]
    async fn read_source_bindings_returns_empty_when_no_apps_are_active(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<app_active_versions::Model>::new()])
            .into_connection();
        let bindings = read_source_bindings(&db, 1, 0).await?;
        assert!(bindings.is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn read_source_bindings_returns_bindings_for_an_active_app() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .into_connection();
        let bindings = read_source_bindings(&db, 1, 0).await?;
        assert_eq!(
            bindings,
            vec![SourceBinding {
                app_id: "waddles.a".to_string(),
                platform: "twitch".to_string(),
                source_id: "tw-channelA".to_string(),
            }]
        );
        Ok(())
    }

    #[tokio::test]
    async fn read_source_bindings_excludes_bindings_for_an_inactive_app(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![
                binding_row("waddles.a", "twitch", "tw-channelA"),
                binding_row("waddles.inactive", "discord", "dg-x"),
            ]])
            .into_connection();
        let bindings = read_source_bindings(&db, 1, 0).await?;
        assert_eq!(bindings.len(), 1, "got {bindings:?}");
        assert_eq!(bindings[0].app_id, "waddles.a");
        Ok(())
    }

    #[tokio::test]
    async fn read_source_bindings_returns_multiple_bindings_for_one_active_app(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![
                binding_row("waddles.a", "twitch", "tw-channelA"),
                binding_row("waddles.a", "discord", "dg-x"),
            ]])
            .into_connection();
        let bindings = read_source_bindings(&db, 1, 0).await?;
        assert_eq!(bindings.len(), 2, "got {bindings:?}");
        Ok(())
    }
}

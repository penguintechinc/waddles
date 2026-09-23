//! Real (non-fake) implementations of `crate::dispatch`'s narrow traits,
//! wired to a live Postgres connection -- kept separate from `dispatch.rs`
//! itself so that module's own tests stay fake-backed and fast (see
//! `implementing-database-patterns` skill: `MockDatabase`-shape tests
//! belong next to the entity, control-flow tests belong next to the logic
//! they exercise).

use std::collections::HashMap;
use std::sync::Mutex;

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::db::entities::{communities, tenants};
use crate::dispatch::{AuditSink, ResolvedTenant, TenantResolver};
use crate::retry::DispatchRecord;

/// Cache key: `(tenant_slug, community_slug)`.
type TenantCacheKey = (String, Option<String>);

/// Resolves `(tenant_slug, community_slug)` to `(tenants.id,
/// communities.id)` against a live Postgres connection, memoized per
/// `(tenant_slug, community_slug)` pair for this process's lifetime --
/// mirrors the Python runner's `_resolve_tenant_id` memoization, extended
/// to community since D30 makes `envelope.community` a slug rather than an
/// already-numeric id (see `crate::db::entities::communities`'s doc for
/// why this differs from the pre-D30 Python behavior).
pub struct DbTenantResolver {
    db: DatabaseConnection,
    cache: Mutex<HashMap<TenantCacheKey, ResolvedTenant>>,
}

impl DbTenantResolver {
    pub fn new(db: DatabaseConnection) -> Self {
        Self {
            db,
            cache: Mutex::new(HashMap::new()),
        }
    }
}

impl TenantResolver for DbTenantResolver {
    fn resolve<'a>(
        &'a self,
        tenant_slug: &'a str,
        community_slug: Option<&'a str>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Option<ResolvedTenant>> + Send + 'a>>
    {
        Box::pin(async move {
            let key = (tenant_slug.to_string(), community_slug.map(str::to_string));
            if let Some(cached) = self
                .cache
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get(&key)
            {
                return Some(*cached);
            }

            let tenant = tenants::Entity::find()
                .filter(tenants::Column::Slug.eq(tenant_slug))
                .one(&self.db)
                .await
                .ok()
                .flatten()?;

            let community_id = match community_slug {
                Some(slug) => {
                    let community = communities::Entity::find()
                        .filter(communities::Column::Name.eq(slug))
                        .one(&self.db)
                        .await
                        .ok()
                        .flatten();
                    match community {
                        Some(c) => Some(c.id),
                        // Unresolvable community slug: still record against
                        // the tenant (tenant-wide), same permissive fallback
                        // the Python runner's own broad exception handler
                        // takes for an audit-log write it must never let
                        // block the dispatch outcome it's recording.
                        None => None,
                    }
                }
                None => None,
            };

            let resolved = (tenant.id, community_id);
            self.cache
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .insert(key, resolved);
            Some(resolved)
        })
    }
}

/// Writes a [`DispatchRecord`] through
/// `crate::db::entities::action_dispatch_log::insert`.
pub struct DbAuditSink {
    db: DatabaseConnection,
}

impl DbAuditSink {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

impl AuditSink for DbAuditSink {
    fn record<'a>(
        &'a self,
        tenant_id: i32,
        community_id: Option<i32>,
        app_id: &'a str,
        record: &'a DispatchRecord,
        envelope_ts: Option<chrono::DateTime<chrono::FixedOffset>>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'a>> {
        Box::pin(async move {
            if let Err(err) = crate::db::entities::action_dispatch_log::insert(
                &self.db,
                tenant_id,
                community_id,
                app_id.to_string(),
                record.target_type.clone(),
                record.status.to_string(),
                record.attempt as i32,
                record.http_status,
                record.detail.clone(),
                envelope_ts,
            )
            .await
            {
                // Spec/Python parity: an audit-log write failure must never
                // mask the dispatch outcome it's trying to record -- logged
                // at ERROR, never propagated.
                tracing::error!(error = %err, app_id, "action_dispatch_log insert failed");
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn mock_db_with_tenant_and_community() -> DatabaseConnection {
        MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![tenants::Model {
                id: 7,
                slug: "acme".to_string(),
            }]])
            .append_query_results([vec![communities::Model {
                id: 3,
                name: "main".to_string(),
            }]])
            .into_connection()
    }

    #[tokio::test]
    async fn resolves_tenant_and_community_on_first_call() {
        let db = mock_db_with_tenant_and_community();
        let resolver = DbTenantResolver::new(db);
        let resolved = resolver.resolve("acme", Some("main")).await;
        assert_eq!(resolved, Some((7, Some(3))));
    }

    #[tokio::test]
    async fn caches_result_and_does_not_reissue_queries() {
        let db = mock_db_with_tenant_and_community();
        let resolver = DbTenantResolver::new(db);
        let first = resolver.resolve("acme", Some("main")).await;
        // The mock only has one result queued per query; a second call
        // hitting the DB again would panic on an empty result queue --
        // succeeding here proves the cache short-circuited it.
        let second = resolver.resolve("acme", Some("main")).await;
        assert_eq!(first, second);
    }

    #[tokio::test]
    async fn unresolvable_tenant_returns_none() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<tenants::Model>::new()])
            .into_connection();
        let resolver = DbTenantResolver::new(db);
        assert_eq!(resolver.resolve("no-such-tenant", None).await, None);
    }

    #[tokio::test]
    async fn unresolvable_community_falls_back_to_tenant_wide() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![tenants::Model {
                id: 7,
                slug: "acme".to_string(),
            }]])
            .append_query_results([Vec::<communities::Model>::new()])
            .into_connection();
        let resolver = DbTenantResolver::new(db);
        assert_eq!(
            resolver.resolve("acme", Some("no-such-community")).await,
            Some((7, None))
        );
    }

    #[tokio::test]
    async fn audit_sink_records_without_panicking_on_success() {
        // Postgres backend inserts go through `INSERT ... RETURNING`,
        // which sea-orm's mock treats as a query result, not an exec
        // result -- see `db::entities::action_dispatch_log`'s own insert
        // test for the same note.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![crate::db::entities::action_dispatch_log::Model {
                id: 1,
                tenant_id: 1,
                community_id: Some(2),
                app_id: "waddles.bot.commands.default".to_string(),
                target_type: "irc_relay".to_string(),
                status: "success".to_string(),
                attempt: 1,
                http_status: None,
                detail: "ok".to_string(),
                envelope_ts: None,
                dispatched_at: chrono::Utc::now().into(),
            }]])
            .into_connection();
        let sink = DbAuditSink::new(db);
        let record = DispatchRecord {
            target_type: "irc_relay".to_string(),
            status: "success",
            attempt: 1,
            http_status: None,
            detail: "ok".to_string(),
        };
        sink.record(1, Some(2), "waddles.bot.commands.default", &record, None)
            .await;
    }

    #[tokio::test]
    async fn audit_sink_logs_and_swallows_a_db_error() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_errors([sea_orm::DbErr::Custom("simulated".to_string())])
            .into_connection();
        let sink = DbAuditSink::new(db);
        let record = DispatchRecord {
            target_type: "irc_relay".to_string(),
            status: "non_retryable_failure",
            attempt: 1,
            http_status: Some(400),
            detail: "bad".to_string(),
        };
        // Must not panic even though the insert fails.
        sink.record(1, None, "waddles.bot.commands.default", &record, None)
            .await;
    }
}

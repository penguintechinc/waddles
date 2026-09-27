//! Resolves the numeric `BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID`
//! scope (already an `(i32, i32)` pair by the time any caller in this
//! crate's dependents reaches this module) to the tenant slug/community
//! name `penguin_spine::Scope::source_stream` needs -- the two are
//! independent identifier spaces (`app_active_versions.tenant_id` is a
//! plain numeric scope key, `penguin_spine::Scope` keys Valkey streams by
//! slug/name strings), and conflating them (e.g. hardcoding a fixed
//! "global" tenant slug regardless of the numeric scope actually
//! configured) is a tenant-isolation bug: two differently-scoped services
//! could end up reading/writing the exact same Valkey stream keys.
//!
//! Queried through the SAME read-only reader connection
//! (`crate::reader::connect`) every other query in this crate uses --
//! `tenants`/`communities` are two more tables the RO role
//! (`waddles_bundle_reader`) needs `SELECT` on, alongside the three named
//! in this crate's root doc.

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::entities::{communities, tenants};
use crate::query::ActiveSetError;

/// A tenant slug and, for a community-scoped activation, its community
/// name -- the exact `(tenant, community)` shape
/// `penguin_spine::Scope::new` takes. `community_name: None` for the
/// tenant-wide sentinel (`community_id == 0`, matching
/// `app_active_versions.community_id`'s own convention).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedScope {
    pub tenant_slug: String,
    pub community_name: Option<String>,
}

/// Resolves `(tenant_id, community_id)` to a [`ResolvedScope`], or `Ok(None)`
/// when either half can't be resolved -- **fail-closed, by design**: callers
/// must never fall back to a hardcoded/guessed scope on a resolution
/// failure (a missing tenant row, a community that exists but belongs to a
/// DIFFERENT tenant, or a dangling `BUNDLE_SCOPE_TENANT_ID`/
/// `BUNDLE_SCOPE_COMMUNITY_ID` misconfiguration) -- and stop instead of
/// starting a source-consumption path scoped to the wrong tenant. A query
/// error propagates as `Err` (distinct from `Ok(None)`) so a caller can log
/// the two cases differently if it wants to, though both mean "do not
/// proceed" identically.
///
/// `community_id == 0` is the tenant-wide sentinel (`app_active_versions
/// .community_id`'s own convention) -- resolved without a second query,
/// `community_name: None`. Any other value must resolve to a `communities`
/// row whose OWN `tenant_id` matches `tenant_id` -- a community id that
/// exists but belongs to a different tenant is treated exactly like a
/// missing row (`Ok(None)`), never silently resolved against the wrong
/// tenant.
pub async fn resolve_scope(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
) -> Result<Option<ResolvedScope>, ActiveSetError> {
    let Some(tenant) = tenants::Entity::find_by_id(tenant_id).one(conn).await? else {
        return Ok(None);
    };

    if community_id == 0 {
        return Ok(Some(ResolvedScope {
            tenant_slug: tenant.slug,
            community_name: None,
        }));
    }

    let community = communities::Entity::find()
        .filter(communities::Column::Id.eq(community_id))
        .filter(communities::Column::TenantId.eq(tenant_id))
        .one(conn)
        .await?;
    let Some(community) = community else {
        return Ok(None);
    };

    Ok(Some(ResolvedScope {
        tenant_slug: tenant.slug,
        community_name: Some(community.name),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn tenant_row(id: i32, slug: &str) -> tenants::Model {
        tenants::Model {
            id,
            slug: slug.to_string(),
        }
    }

    fn community_row(id: i32, name: &str, tenant_id: i32) -> communities::Model {
        communities::Model {
            id,
            name: name.to_string(),
            tenant_id,
        }
    }

    #[tokio::test]
    async fn resolve_scope_resolves_the_tenant_wide_sentinel_without_a_community_query(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![tenant_row(7, "acme")]])
            .into_connection();
        let resolved = resolve_scope(&db, 7, 0).await?;
        assert_eq!(
            resolved,
            Some(ResolvedScope {
                tenant_slug: "acme".to_string(),
                community_name: None,
            })
        );
        Ok(())
    }

    #[tokio::test]
    async fn resolve_scope_resolves_a_community_scoped_activation() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![tenant_row(7, "acme")]])
            .append_query_results([vec![community_row(3, "main", 7)]])
            .into_connection();
        let resolved = resolve_scope(&db, 7, 3).await?;
        assert_eq!(
            resolved,
            Some(ResolvedScope {
                tenant_slug: "acme".to_string(),
                community_name: Some("main".to_string()),
            })
        );
        Ok(())
    }

    #[tokio::test]
    async fn resolve_scope_fails_closed_when_the_tenant_row_is_missing(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<tenants::Model>::new()])
            .into_connection();
        assert_eq!(resolve_scope(&db, 999, 0).await?, None);
        Ok(())
    }

    #[tokio::test]
    async fn resolve_scope_fails_closed_when_the_community_row_is_missing(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![tenant_row(7, "acme")]])
            .append_query_results([Vec::<communities::Model>::new()])
            .into_connection();
        assert_eq!(resolve_scope(&db, 7, 999).await?, None);
        Ok(())
    }

    /// Cross-tenant regression test: a community id that genuinely exists
    /// but belongs to a DIFFERENT tenant must resolve `None`, exactly like
    /// a missing row -- never silently resolved against the wrong tenant.
    /// The query itself filters on both `Id` and `TenantId`, so a mock
    /// queue returning an empty result here is the correct simulation of
    /// what a real `WHERE id = ? AND tenant_id = ?` returns for this case.
    #[tokio::test]
    async fn resolve_scope_fails_closed_on_a_cross_tenant_community_id(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![tenant_row(7, "acme")]])
            .append_query_results([Vec::<communities::Model>::new()])
            .into_connection();
        assert_eq!(
            resolve_scope(&db, 7, 3).await?,
            None,
            "a community id belonging to another tenant must not resolve"
        );
        Ok(())
    }
}

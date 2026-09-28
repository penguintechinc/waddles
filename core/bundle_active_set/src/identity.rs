//! Resolves one `(platform, platform_user_id)` pair to a linked
//! `hub_users` UUID, or `None` when the platform identity is unknown to
//! this scope or has never been OAuth-linked -- the read half of the
//! PII-tokenization hard invariant (`docs/superpowers/specs/
//! 2026-09-28-bundle-permissions-and-capability-gate.md` S10.1/S10.3): a
//! bundle only ever sees a UUID (linked) or an ephemeral pseudonym
//! (unlinked/unknown) for any user, never a raw platform username/login.
//!
//! **`community_id == 0` is the tenant-wide sentinel** (`app_active_
//! versions.community_id`'s own convention, `crate::scope::resolve_scope`)
//! -- a tenant-wide-scoped instance resolves membership across EVERY
//! community belonging to `tenant_id`, not literal community row id `0`
//! (security review fix: the original version filtered `community_id =
//! 0`, which only ever matched a real community that happened to be
//! numbered zero -- never the intended "any community in this tenant"
//! semantics, silently returning "unlinked" for every tenant-wide lookup).
//!
//! **Read-only, no upsert.** This module never creates a
//! `community_members` row -- the RO reader connection
//! (`crate::reader::connect`) is a real database-enforced read-only role,
//! so a write isn't even possible here, and callers needing an "unknown
//! user" fallback mint an ephemeral pseudonym elsewhere (`crate::identity`
//! intentionally owns no minting logic of its own -- see
//! `core/svc_process::pii_tokenize`'s module doc: pseudonym minting now
//! happens INSIDE the PII boundary, hub-api's `POST /api/v1/internal/
//! identities/ephemeral`, never locally computable).

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::entities::{communities, community_members};
use crate::query::ActiveSetError;

/// Resolves `(tenant_id, community_id)` to the set of `community_members
/// .community_id` values this scope actually covers -- `[community_id]`
/// unscoped (the common case, no extra query), or every community
/// belonging to `tenant_id` when `community_id == 0` (tenant-wide). An
/// empty result (a tenant with zero communities, pathological but
/// possible) makes the caller's `is_in` filter match nothing, never
/// everything -- fail-closed, same posture as [`crate::scope::
/// resolve_scope`].
async fn resolve_community_scope(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
) -> Result<Vec<i32>, ActiveSetError> {
    if community_id != 0 {
        return Ok(vec![community_id]);
    }
    let rows = communities::Entity::find()
        .filter(communities::Column::TenantId.eq(tenant_id))
        .all(conn)
        .await?;
    Ok(rows.into_iter().map(|c| c.id).collect())
}

/// Looks up `community_members.user_id` for `(platform, platform_user_id)`
/// within `(tenant_id, community_id)`'s scope (see [`resolve_community_
/// scope`] for the tenant-wide `community_id == 0` semantics). `Ok(None)`
/// covers "no member row exists yet", "a member row exists but `user_id`
/// is `NULL`/empty", and "no community in scope" -- callers treat all
/// three identically as "unlinked, mint an ephemeral pseudonym instead".
pub async fn resolve_linked_user_id(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    platform: &str,
    platform_user_id: &str,
) -> Result<Option<String>, ActiveSetError> {
    let scope = resolve_community_scope(conn, tenant_id, community_id).await?;
    if scope.is_empty() {
        return Ok(None);
    }

    let row = community_members::Entity::find()
        .filter(community_members::Column::CommunityId.is_in(scope))
        .filter(community_members::Column::Platform.eq(platform))
        .filter(community_members::Column::PlatformUserId.eq(platform_user_id))
        .one(conn)
        .await?;

    Ok(row.and_then(|r| r.user_id).filter(|id| !id.is_empty()))
}

/// A `community_members` row matched by handle, keyed back to its own
/// `platform_user_id` (needed for [`crate::identity`]'s callers to mint a
/// stable ephemeral pseudonym even when the match itself is unlinked)
/// alongside its `user_id` half (mirrors [`resolve_linked_user_id`]'s
/// contract exactly).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HandleMatch {
    pub platform_user_id: String,
    pub user_id: Option<String>,
}

/// Best-effort, case-insensitive match of an unstructured `@handle`
/// mention against this scope's own `display_name` column -- **not** a
/// reliable identity lookup (this column can drift from the platform's
/// live current username, and matches are never guaranteed unique), so
/// `Ok(None)` covers "no member has ever used this display name" AND
/// "ambiguous, more than one member matches" (an ambiguous match is
/// treated exactly like no match: silently picking one of several
/// candidates would risk tokenizing a mention to the WRONG user's UUID,
/// which is strictly worse than falling back to an ephemeral pseudonym).
/// Tenant-wide (`community_id == 0`) widens the ambiguity check across
/// every community in `tenant_id`, not just one -- see [`resolve_
/// community_scope`].
///
/// Fetches this scope's member rows and matches in Rust rather than a SQL
/// `LOWER(display_name) = LOWER($1)` filter -- keeps the comparison
/// dialect-independent (no `ILIKE`/`LOWER()` portability difference
/// between Postgres and the `sqlite` test backend `crate::reader`'s own
/// doc mentions), and per-scope member counts are small enough that this
/// is not a meaningful cost next to the network round trip itself.
pub async fn resolve_member_by_handle(
    conn: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    platform: &str,
    handle: &str,
) -> Result<Option<HandleMatch>, ActiveSetError> {
    let scope = resolve_community_scope(conn, tenant_id, community_id).await?;
    if scope.is_empty() {
        return Ok(None);
    }

    let rows = community_members::Entity::find()
        .filter(community_members::Column::CommunityId.is_in(scope))
        .filter(community_members::Column::Platform.eq(platform))
        .all(conn)
        .await?;

    let mut matches = rows.into_iter().filter(|r| {
        r.display_name
            .as_deref()
            .is_some_and(|d| d.eq_ignore_ascii_case(handle))
    });

    let Some(first) = matches.next() else {
        return Ok(None);
    };
    if matches.next().is_some() {
        // Ambiguous -- more than one member shares this display name in
        // this scope. Never guess.
        return Ok(None);
    }

    Ok(Some(HandleMatch {
        platform_user_id: first.platform_user_id,
        user_id: first.user_id.filter(|id| !id.is_empty()),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn member(community_id: i32, user_id: Option<&str>) -> community_members::Model {
        member_with_handle(community_id, user_id, None)
    }

    fn member_with_handle(
        community_id: i32,
        user_id: Option<&str>,
        display_name: Option<&str>,
    ) -> community_members::Model {
        community_members::Model {
            id: 1,
            community_id,
            platform: "twitch".to_string(),
            platform_user_id: "999".to_string(),
            user_id: user_id.map(str::to_string),
            display_name: display_name.map(str::to_string),
        }
    }

    fn community(id: i32, tenant_id: i32) -> communities::Model {
        communities::Model {
            id,
            name: format!("community-{id}"),
            tenant_id,
        }
    }

    #[tokio::test]
    async fn resolves_a_linked_member_to_its_hub_user_id() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member(
                7,
                Some("11111111-1111-4111-8111-111111111111"),
            )]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 1, 7, "twitch", "999").await?;
        assert_eq!(
            resolved.as_deref(),
            Some("11111111-1111-4111-8111-111111111111")
        );
        Ok(())
    }

    #[tokio::test]
    async fn returns_none_when_no_member_row_exists() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<community_members::Model>::new()])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 1, 7, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn returns_none_when_the_member_row_exists_but_is_unlinked() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member(7, None)]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 1, 7, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn treats_an_empty_string_user_id_as_unlinked() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member(7, Some(""))]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 1, 7, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    /// Security review fix regression test: `community_id == 0` (tenant-
    /// wide scope) must resolve membership across EVERY community
    /// belonging to `tenant_id` -- not literal community row id `0`, which
    /// would silently behave as "always unlinked" for a tenant-wide
    /// instance since no real member row is ever scoped to community `0`.
    #[tokio::test]
    async fn tenant_wide_scope_resolves_a_member_from_any_of_the_tenants_communities(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            // First query: resolve_community_scope's `communities` lookup
            // for tenant 1 -- two communities, 7 and 9.
            .append_query_results([vec![community(7, 1), community(9, 1)]])
            // Second query: the member row lives under community 9, not 7.
            .append_query_results([vec![member(
                9,
                Some("33333333-3333-4333-8333-333333333333"),
            )]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 1, 0, "twitch", "999").await?;
        assert_eq!(
            resolved.as_deref(),
            Some("33333333-3333-4333-8333-333333333333")
        );
        Ok(())
    }

    /// A tenant with zero communities (pathological) must resolve to
    /// "unlinked", never issue a query that -- via an empty `IN ()` -- some
    /// SQL dialects could otherwise mishandle.
    #[tokio::test]
    async fn tenant_wide_scope_with_no_communities_resolves_to_unlinked(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<communities::Model>::new()])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 1, 0, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn resolve_by_handle_matches_case_insensitively_and_returns_the_linked_user_id(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member_with_handle(
                7,
                Some("22222222-2222-4222-8222-222222222222"),
                Some("SomeUser"),
            )]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 1, 7, "twitch", "someuser").await?;
        let matched = matched.expect("case-insensitive display_name match");
        assert_eq!(matched.platform_user_id, "999");
        assert_eq!(
            matched.user_id.as_deref(),
            Some("22222222-2222-4222-8222-222222222222")
        );
        Ok(())
    }

    #[tokio::test]
    async fn resolve_by_handle_returns_the_platform_user_id_of_an_unlinked_match(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member_with_handle(7, None, Some("someuser"))]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 1, 7, "twitch", "someuser")
            .await?
            .expect("match found");
        assert_eq!(matched.platform_user_id, "999");
        assert!(matched.user_id.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn resolve_by_handle_returns_none_when_no_member_matches() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member_with_handle(7, None, Some("someoneelse"))]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 1, 7, "twitch", "someuser").await?;
        assert!(matched.is_none());
        Ok(())
    }

    /// An ambiguous handle (two members sharing the same display name)
    /// must never guess -- both are excluded, never a coin flip that could
    /// tokenize a mention to the wrong person's UUID.
    #[tokio::test]
    async fn resolve_by_handle_returns_none_on_an_ambiguous_match() -> Result<(), ActiveSetError> {
        let mut first = member_with_handle(
            7,
            Some("11111111-1111-4111-8111-111111111111"),
            Some("dupe"),
        );
        first.platform_user_id = "1".to_string();
        let mut second = member_with_handle(
            7,
            Some("22222222-2222-4222-8222-222222222222"),
            Some("dupe"),
        );
        second.platform_user_id = "2".to_string();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![first, second]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 1, 7, "twitch", "dupe").await?;
        assert!(matched.is_none());
        Ok(())
    }
}

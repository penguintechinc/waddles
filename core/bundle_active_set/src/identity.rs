//! Resolves one `(platform, platform_user_id)` pair to a linked
//! `hub_users` UUID, or `None` when the platform identity is unknown to
//! this community or has never been OAuth-linked -- the read half of the
//! PII-tokenization hard invariant (`docs/superpowers/specs/
//! 2026-09-28-bundle-permissions-and-capability-gate.md` S10.1/S10.3): a
//! bundle only ever sees a UUID (linked) or a deterministic ephemeral
//! pseudonym (unlinked/unknown) for any user, never a raw platform
//! username/login.
//!
//! **Read-only, no upsert.** This module never creates a
//! `community_members` row -- the RO reader connection
//! (`crate::reader::connect`) is a real database-enforced read-only role,
//! so a write isn't even possible here, and callers needing an "unknown
//! user" fallback mint a deterministic ephemeral pseudonym instead
//! (`crate::identity` intentionally owns no minting logic of its own --
//! that's each service's own PII-tokenization pass, since the pseudonym
//! namespace/derivation is a cross-cutting concern shared with
//! unstructured mention resolution, not something only this crate's
//! callers need).

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::entities::community_members;
use crate::query::ActiveSetError;

/// Looks up `community_members.user_id` for `(community_id, platform,
/// platform_user_id)`. `Ok(None)` covers both "no member row exists yet"
/// and "a member row exists but `user_id` is `NULL`/empty" -- callers
/// (`crate::identity`'s own doc) treat both identically as "unlinked,
/// mint an ephemeral pseudonym instead", so this function collapses them
/// rather than making every caller repeat that same empty-string check.
pub async fn resolve_linked_user_id(
    conn: &DatabaseConnection,
    community_id: i32,
    platform: &str,
    platform_user_id: &str,
) -> Result<Option<String>, ActiveSetError> {
    let row = community_members::Entity::find()
        .filter(community_members::Column::CommunityId.eq(community_id))
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
/// mention against this community's own `display_name` column -- **not**
/// a reliable identity lookup (see [`community_members::Model::
/// display_name`]'s own doc for why), so `Ok(None)` covers both "no
/// member has ever used this display name" and "ambiguous, more than one
/// member matches" (an ambiguous match is treated exactly like no match:
/// silently picking one of several candidates would risk tokenizing a
/// mention to the WRONG user's UUID, which is strictly worse than falling
/// back to an ephemeral pseudonym).
///
/// Fetches this `(community_id, platform)`'s member rows and matches in
/// Rust rather than a SQL `LOWER(display_name) = LOWER($1)` filter --
/// keeps the comparison dialect-independent (no `ILIKE`/`LOWER()`
/// portability difference between Postgres and the `sqlite` test backend
/// `crate::reader`'s own doc mentions), and per-community member counts
/// are small enough that this is not a meaningful cost next to the
/// network round trip itself.
///
/// [`community_members::Model::display_name`]: crate::entities::community_members::Model
pub async fn resolve_member_by_handle(
    conn: &DatabaseConnection,
    community_id: i32,
    platform: &str,
    handle: &str,
) -> Result<Option<HandleMatch>, ActiveSetError> {
    let rows = community_members::Entity::find()
        .filter(community_members::Column::CommunityId.eq(community_id))
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
        // this community. Never guess.
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

    fn member(user_id: Option<&str>) -> community_members::Model {
        member_with_handle(user_id, None)
    }

    fn member_with_handle(
        user_id: Option<&str>,
        display_name: Option<&str>,
    ) -> community_members::Model {
        community_members::Model {
            id: 1,
            community_id: 7,
            platform: "twitch".to_string(),
            platform_user_id: "999".to_string(),
            user_id: user_id.map(str::to_string),
            display_name: display_name.map(str::to_string),
        }
    }

    #[tokio::test]
    async fn resolves_a_linked_member_to_its_hub_user_id() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member(Some("11111111-1111-4111-8111-111111111111"))]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 7, "twitch", "999").await?;
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
        let resolved = resolve_linked_user_id(&db, 7, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn returns_none_when_the_member_row_exists_but_is_unlinked() -> Result<(), ActiveSetError>
    {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member(None)]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 7, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn treats_an_empty_string_user_id_as_unlinked() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member(Some(""))]])
            .into_connection();
        let resolved = resolve_linked_user_id(&db, 7, "twitch", "999").await?;
        assert!(resolved.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn resolve_by_handle_matches_case_insensitively_and_returns_the_linked_user_id(
    ) -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member_with_handle(
                Some("22222222-2222-4222-8222-222222222222"),
                Some("SomeUser"),
            )]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 7, "twitch", "someuser").await?;
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
            .append_query_results([vec![member_with_handle(None, Some("someuser"))]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 7, "twitch", "someuser")
            .await?
            .expect("match found");
        assert_eq!(matched.platform_user_id, "999");
        assert!(matched.user_id.is_none());
        Ok(())
    }

    #[tokio::test]
    async fn resolve_by_handle_returns_none_when_no_member_matches() -> Result<(), ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![member_with_handle(None, Some("someoneelse"))]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 7, "twitch", "someuser").await?;
        assert!(matched.is_none());
        Ok(())
    }

    /// An ambiguous handle (two members sharing the same display name)
    /// must never guess -- both are excluded, never a coin flip that could
    /// tokenize a mention to the wrong person's UUID.
    #[tokio::test]
    async fn resolve_by_handle_returns_none_on_an_ambiguous_match() -> Result<(), ActiveSetError> {
        let mut first =
            member_with_handle(Some("11111111-1111-4111-8111-111111111111"), Some("dupe"));
        first.platform_user_id = "1".to_string();
        let mut second =
            member_with_handle(Some("22222222-2222-4222-8222-222222222222"), Some("dupe"));
        second.platform_user_id = "2".to_string();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![first, second]])
            .into_connection();
        let matched = resolve_member_by_handle(&db, 7, "twitch", "dupe").await?;
        assert!(matched.is_none());
        Ok(())
    }
}

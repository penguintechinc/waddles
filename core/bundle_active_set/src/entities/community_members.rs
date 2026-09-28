//! Read-only SeaORM entity for `community_members`
//! (`config/postgres/migrations/000_create_base_schema.sql`) -- the
//! existing per-community platform-identity mapping every chat/reputation
//! path already writes through (`hub_api/services/community_reputation_service.py`,
//! `core/reputation_module`'s membership tracking): `UNIQUE(community_id,
//! platform, platform_user_id)`. This is the "existing users/identity
//! mapping table" `crate::identity::resolve_linked_user_id` reads --
//! `hub_users` itself has no per-platform lookup column of its own (its
//! `id` is a plain `SERIAL`, not a UUID), so `community_members.user_id`
//! (a nullable `VARCHAR`, populated only once a platform account is
//! OAuth-linked to a hub account) is the closest existing UUID-shaped
//! identity a data-plane RO read can resolve without writing anything.
//!
//! Declares only the columns `crate::identity` reads -- `user_id` for
//! structured (platform_user_id-keyed) resolution, plus `display_name`
//! for `crate::identity::resolve_member_by_handle`'s best-effort
//! unstructured `@handle` mention match (PII-tokenization invariant: no
//! other column -- `avatar_url`/`bio`/etc -- is ever read here).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "community_members")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub community_id: i32,
    pub platform: String,
    pub platform_user_id: String,
    /// `NULL`/empty until this platform account is OAuth-linked to a real
    /// `hub_users` row -- both states mean "unlinked" to
    /// `crate::identity::resolve_linked_user_id`, which never
    /// distinguishes "no member row at all" from "member row, but never
    /// linked" (both fall back to the caller's ephemeral pseudonym).
    pub user_id: Option<String>,
    /// The platform username/handle recorded when this member row was
    /// first created (legacy Python normalizer parity:
    /// `discord_ingest.py`/`twitch_ingest.py`'s `author_username`).
    /// `crate::identity::resolve_member_by_handle`'s only use of this
    /// column is a best-effort, case-insensitive match against an
    /// unstructured `@handle` chat mention -- a known-imperfect signal
    /// (this column can drift from the platform's live current username,
    /// and matches are never guaranteed unique), documented rather than
    /// silently assumed reliable.
    pub display_name: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_000() {
        assert_eq!(Entity.table_name(), "community_members");
    }
}

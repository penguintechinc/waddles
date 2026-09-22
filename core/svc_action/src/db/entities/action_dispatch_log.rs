//! SeaORM entity for `action_dispatch_log`
//! (`config/postgres/migrations/074_action_dispatch_log.sql`) -- this
//! service's own audit-trail table for every dispatch attempt. `detail` is
//! a short human-readable status string only -- per that migration's own
//! header comment and `rules/security.md` ("log masked, never raw PII"),
//! never the request/response body or a resolved secret.
//!
//! TODO(M3): executor integration -- blocked on M2. No code in this
//! scaffold inserts rows yet; the dispatch loop (§4.3 of
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`) writes
//! through this entity once the bundle-executor wire protocol and
//! `penguin-spine` land.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "action_dispatch_log")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub tenant_id: i32,
    pub community_id: Option<i32>,
    pub app_id: String,
    pub target_type: String,
    pub status: String,
    pub attempt: i32,
    pub http_status: Option<i32>,
    pub detail: String,
    pub envelope_ts: Option<DateTimeWithTimeZone>,
    pub dispatched_at: DateTimeWithTimeZone,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_074() {
        assert_eq!(Entity.table_name(), "action_dispatch_log");
    }
}

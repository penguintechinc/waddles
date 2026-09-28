//! Read-only SeaORM entity for `bundle_active_set_watermark` (dataplane
//! scale design rev 4, §7) -- a single row (`id = 1`) the PRIMARY updates
//! periodically with the exact xid-safe horizon (`safe_seq`) every replica
//! may consume `bundle_active_set_changes` up to:
//!
//! ```sql
//! UPDATE bundle_active_set_watermark SET safe_seq = $safe_seq, computed_at = now()
//!   WHERE id = 1;
//! ```
//!
//! This crate never writes this table (design: "No replica ever computes
//! its own horizon; correctness no longer depends on replication lag at
//! all, only on the primary's own live view") -- `crate::changelog::
//! read_safe_seq` is a trivial `SELECT safe_seq FROM
//! bundle_active_set_watermark WHERE id = 1`, treating a missing row (the
//! primary-side publisher job hasn't run yet in this environment) as
//! `safe_seq = 0` -- never consuming beyond a horizon that hasn't been
//! published is the fail-safe default, not an error.
//!
//! `min_retained_seq` (hub-api migration `0026_bundle_active_set_changelog`,
//! waddles PR #397): the lowest `seq` still present in
//! `bundle_active_set_changes` after the last retention prune (default
//! 48h) -- lets a consumer whose `last_seq` has fallen behind retention
//! detect it directly (`crate::changelog::read_safe_seq_watermark`) rather
//! than relying solely on the "lowest returned change row's seq" heuristic
//! (`core/svc_process`'s/`core/svc_action`'s own `changelog_consumer`
//! modules' fallback for an older hub-api schema without this column yet).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "bundle_active_set_watermark")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub id: i32,
    pub safe_seq: i64,
    pub min_retained_seq: i64,
    pub computed_at: DateTimeUtc,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_the_dataplane_scale_design() {
        assert_eq!(Entity.table_name(), "bundle_active_set_watermark");
    }
}

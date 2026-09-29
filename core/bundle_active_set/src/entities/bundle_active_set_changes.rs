//! Read-only SeaORM entity for `bundle_active_set_changes` (dataplane scale
//! design rev 4, §7: "Sequence-gap fix ... compute the safe horizon on the
//! primary, publish it, consume it read-only everywhere else"). hub-api
//! (or a small dedicated job) is the sole writer -- an `INSERT`-only
//! append log, one row per `(entity, entity_id, op)` change to
//! `app_active_versions`/`app_source_bindings`, driven by a Postgres
//! trigger on those tables. This crate only ever reads `seq > since_seq
//! AND seq <= safe_seq` (`crate::changelog::read_changes`) -- it never
//! computes the `xmin`-based safe horizon itself (that computation only
//! ever runs on the PRIMARY, per the design's own correctness argument:
//! a replica's `xl_running_xacts` view of in-flight primary transactions
//! is inherently lagged, so a replica-side horizon can be unsafe).
//!
//! Column contract: `seq` is the monotonic, gapless-per-committed-row
//! ordering key every consumer polls by; `tenant_id`/`community_id` name
//! the affected `(tenant, community)` scope directly (no need to re-derive
//! it from `entity_id`) so `crate::changelog::affected_scopes` needs no
//! `entity`-specific parsing; `entity`/`entity_id`/`op` are metadata only
//! (never branched on by this crate -- every change, regardless of which
//! table/row it names, triggers the exact same "re-read this scope's
//! active set in full" response); `writer_xid` is the `xmin` system value
//! the primary's own safe-horizon computation already consumed before
//! publishing this row's `seq` as visible -- kept for observability/audit
//! only, this crate's own read path never re-derives visibility from it.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "bundle_active_set_changes")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub seq: i64,
    pub tenant_id: i32,
    pub community_id: i32,
    pub entity: String,
    pub entity_id: String,
    pub op: String,
    pub changed_at: DateTimeUtc,
    pub writer_xid: Option<i64>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_the_dataplane_scale_design() {
        assert_eq!(Entity.table_name(), "bundle_active_set_changes");
    }
}

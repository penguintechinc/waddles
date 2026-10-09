//! SeaORM entity for `overlay_view_credentials`
//! (`config/postgres/migrations/100_overlay_view_credentials.sql`) -- the
//! hashed-at-rest VIEW credential table `overlay_auth::view::
//! ViewCredentialStore` reads against. Only the columns
//! [`crate::overlay::view_store::SeaOrmViewCredentialStore`] needs are
//! declared (`created_at`/`updated_at` are unused here, same precedent as
//! the other two entities in this module).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "overlay_view_credentials")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub community_id: i64,
    pub key_hash: String,
    pub previous_key_hash: Option<String>,
    pub is_active: bool,
    pub rotated_at: Option<DateTimeWithTimeZone>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

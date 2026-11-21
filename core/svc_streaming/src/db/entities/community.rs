//! Read-only view of `communities` (owned by the base-schema migration, not
//! this service). Used only to resolve a `community_id` path param to its
//! owning `tenant_id` for tenant scoping -- see [`crate::api::tenancy`].

use sea_orm::entity::prelude::*;
use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement};

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "communities")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub tenant_id: i32,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

/// The community's public UUID (`communities.public_id`), as text, or `None` when the community
/// does not exist or carries no `public_id`.
///
/// hub-api addresses communities by this UUID in every URL (the numeric `id` is internal), so any
/// call this service makes to hub-api resolves it first. Kept out of [`Model`] on purpose: the
/// column is `UUID` in Postgres, and `CAST(.. AS TEXT)` reads it identically on SQLite test DBs.
pub async fn public_id(
    db: &DatabaseConnection,
    community_id: i32,
) -> Result<Option<String>, DbErr> {
    let backend = db.get_database_backend();
    let sql = match backend {
        DbBackend::Postgres => {
            "SELECT CAST(public_id AS TEXT) AS public_id FROM communities WHERE id = $1"
        }
        _ => "SELECT CAST(public_id AS TEXT) AS public_id FROM communities WHERE id = ?",
    };
    let row = db
        .query_one_raw(Statement::from_sql_and_values(
            backend,
            sql,
            [community_id.into()],
        ))
        .await?;
    match row {
        Some(row) => row.try_get::<Option<String>>("", "public_id"),
        None => Ok(None),
    }
}

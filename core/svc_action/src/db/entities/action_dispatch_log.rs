//! SeaORM entity for `action_dispatch_log`
//! (`config/postgres/migrations/074_action_dispatch_log.sql`) -- this
//! service's own audit-trail table for every dispatch attempt. `detail` is
//! a short human-readable status string only -- per that migration's own
//! header comment and `rules/security.md` ("log masked, never raw PII"),
//! never the request/response body or a resolved secret.
//!
//! `crate::dispatch`'s retry-with-backoff outcome (`crate::retry::
//! DispatchRecord`) is written through [`insert`] once the entry's attempt
//! sequence (success, non-retryable failure, or exhausted retries) is
//! final -- exactly one row per dispatched entry, mirroring the Python
//! runner's `services/dispatch_log.py::record_dispatch` call shape.

use sea_orm::entity::prelude::*;
use sea_orm::Set;

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

/// Inserts one audit row for a completed dispatch attempt sequence.
/// `id`/`dispatched_at` are DB-generated (`NotSet`); every other column is
/// caller-supplied. Never queries first -- this table is append-only, one
/// row per attempt sequence, never updated (mirrors `action_dispatch_log`
/// migration 074's own header comment).
#[allow(clippy::too_many_arguments)]
pub async fn insert(
    db: &DatabaseConnection,
    tenant_id: i32,
    community_id: Option<i32>,
    app_id: String,
    target_type: String,
    status: String,
    attempt: i32,
    http_status: Option<i32>,
    detail: String,
    envelope_ts: Option<DateTimeWithTimeZone>,
) -> Result<(), DbErr> {
    let model = ActiveModel {
        id: sea_orm::ActiveValue::NotSet,
        tenant_id: Set(tenant_id),
        community_id: Set(community_id),
        app_id: Set(app_id),
        target_type: Set(target_type),
        status: Set(status),
        attempt: Set(attempt),
        http_status: Set(http_status),
        detail: Set(detail),
        envelope_ts: Set(envelope_ts),
        dispatched_at: Set(chrono::Utc::now().into()),
    };
    Entity::insert(model).exec(db).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    #[test]
    fn table_name_matches_migration_074() {
        assert_eq!(Entity.table_name(), "action_dispatch_log");
    }

    #[tokio::test]
    async fn insert_issues_exactly_one_statement_with_expected_columns() {
        // Postgres backend inserts go through `INSERT ... RETURNING`,
        // which sea-orm's mock treats as a query result, not an exec
        // result -- unlike MySQL/SQLite's `last_insert_id()` exec path.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![Model {
                id: 1,
                tenant_id: 7,
                community_id: Some(3),
                app_id: "waddles.bot.commands.default".to_string(),
                target_type: "irc_relay".to_string(),
                status: "success".to_string(),
                attempt: 1,
                http_status: None,
                detail: "relayed to twitch channel=#c".to_string(),
                envelope_ts: None,
                dispatched_at: chrono::Utc::now().into(),
            }]])
            .into_connection();

        insert(
            &db,
            7,
            Some(3),
            "waddles.bot.commands.default".to_string(),
            "irc_relay".to_string(),
            "success".to_string(),
            1,
            None,
            "relayed to twitch channel=#c".to_string(),
            None,
        )
        .await
        .expect("insert succeeds against the mock backend");

        let log = db.into_transaction_log();
        assert_eq!(log.len(), 1, "exactly one statement must be issued");
    }

    #[tokio::test]
    async fn insert_propagates_a_db_error() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_errors([DbErr::Custom("simulated failure".to_string())])
            .into_connection();

        let result = insert(
            &db,
            7,
            None,
            "waddles.bot.commands.default".to_string(),
            "irc_relay".to_string(),
            "retryable_failure".to_string(),
            4,
            Some(503),
            "unavailable".to_string(),
            None,
        )
        .await;
        assert!(result.is_err());
    }
}

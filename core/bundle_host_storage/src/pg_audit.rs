//! Durable Postgres audit sink: one `bundle_storage_audit` row per cross-prefix
//! access (allowed and denied) and per owner tag change. DDL:
//! `scripts/db/bundle_storage_audit.sql` (alembic 0080); the connecting role
//! `waddles_bundle_storage_audit` is INSERT-only.

use async_trait::async_trait;
use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, DbErr, Statement,
    Value,
};

use crate::audit::{AuditEvent, AuditOutcome, AuditSink};
use crate::error::StorageError;

/// Non-secret connection settings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuditConnectConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    /// `waddles_bundle_storage_audit` in every real deployment.
    pub user: String,
}

/// Opens the pooled connection. `sqlx_logging(false)` -- never echo the URL.
pub async fn connect(
    cfg: &AuditConnectConfig,
    password: &str,
) -> Result<DatabaseConnection, DbErr> {
    let url = format!(
        "postgres://{user}:{password}@{host}:{port}/{name}",
        user = cfg.user,
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
    );
    let mut opts = ConnectOptions::new(url);
    opts.max_connections(4)
        .min_connections(1)
        .sqlx_logging(false);
    Database::connect(opts).await
}

/// [`AuditSink`] writing to `bundle_storage_audit`.
pub struct PgAuditSink {
    db: DatabaseConnection,
}

impl PgAuditSink {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

/// Placeholder recorded instead of a field that carries NUL/control characters.
pub const INVALID_FIELD: &str = "<invalid>";

/// Clips to `max` chars; a value with NUL/control characters (guest-influenced
/// strings such as an owner or key) is recorded as `<invalid>` so the row is ALWAYS
/// written (Postgres rejects NUL in text, which would otherwise drop a denial row).
pub fn clip(s: &str, max: usize) -> String {
    if s.chars().any(char::is_control) {
        return INVALID_FIELD.to_string();
    }
    s.chars().take(max).collect()
}

#[async_trait]
impl AuditSink for PgAuditSink {
    async fn record(&self, e: AuditEvent) -> Result<(), StorageError> {
        let tenant = i64::try_from(e.tenant)
            .map_err(|_| StorageError::Backend("tenant id out of range".into()))?;
        let community = i64::try_from(e.community)
            .map_err(|_| StorageError::Backend("community id out of range".into()))?;
        let outcome = match e.outcome {
            AuditOutcome::Allowed => "allowed",
            AuditOutcome::Denied => "denied",
        };
        let stmt = Statement::from_sql_and_values(
            DbBackend::Postgres,
            "INSERT INTO bundle_storage_audit \
             (tenant_id, community_id, caller_app_id, owner_app_id, op, object_key, outcome, reason, request_id) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            vec![
                Value::BigInt(Some(tenant)),
                Value::BigInt(Some(community)),
                Value::String(Some(clip(&e.caller, 255))),
                Value::String(Some(clip(&e.owner, 255))),
                Value::String(Some(e.op.to_string())),
                Value::String(Some(clip(&e.key, 600))),
                Value::String(Some(outcome.to_string())),
                Value::String(Some(clip(&e.reason, 400))),
                Value::String(Some(clip(&e.request_id, 64))),
            ],
        );
        self.db
            .execute_raw(stmt)
            .await
            .map(|_| ())
            .map_err(|err| StorageError::Backend(format!("audit insert failed: {err}")))
    }
}

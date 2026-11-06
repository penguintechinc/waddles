//! Audit of cross-prefix access (allowed AND denied) and tag operations.

use async_trait::async_trait;

use crate::error::StorageError;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AuditOutcome {
    Allowed,
    Denied,
}

/// One cross-prefix access attempt.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuditEvent {
    pub caller: String,
    pub owner: String,
    pub tenant: u64,
    pub community: u64,
    pub op: &'static str,
    pub key: String,
    pub outcome: AuditOutcome,
    pub reason: String,
    /// Host-assigned request id (correlation).
    pub request_id: String,
}

/// Durable sink for audit events. Production: `pg_audit::PgAuditSink` (the
/// `bundle_storage_audit` ledger). `record` is fallible: callers fail the audited
/// operation when an ALLOWED cross-prefix access / tag change cannot be recorded.
#[async_trait]
pub trait AuditSink: Send + Sync {
    async fn record(&self, event: AuditEvent) -> Result<(), StorageError>;
}

/// Default sink: structured `tracing` event (target `bundle_storage_audit`).
pub struct TracingAuditSink;

#[async_trait]
impl AuditSink for TracingAuditSink {
    async fn record(&self, e: AuditEvent) -> Result<(), StorageError> {
        tracing::info!(
            target: "bundle_storage_audit",
            caller = %e.caller, owner = %e.owner, tenant = e.tenant, community = e.community,
            op = e.op, key = %e.key, outcome = ?e.outcome, reason = %e.reason,
            "cross-prefix storage access"
        );
        Ok(())
    }
}

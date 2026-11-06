//! Audit of cross-prefix access (allowed AND denied).

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
}

/// Sink for audit events; production wiring forwards to `bundle_audit`.
pub trait AuditSink: Send + Sync {
    fn record(&self, event: AuditEvent);
}

/// Default sink: structured `tracing` event (target `bundle_storage_audit`).
pub struct TracingAuditSink;

impl AuditSink for TracingAuditSink {
    fn record(&self, e: AuditEvent) {
        tracing::info!(
            target: "bundle_storage_audit",
            caller = %e.caller, owner = %e.owner, tenant = e.tenant, community = e.community,
            op = e.op, key = %e.key, outcome = ?e.outcome, reason = %e.reason,
            "cross-prefix storage access"
        );
    }
}

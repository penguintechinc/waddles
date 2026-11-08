//! Audit trail: every open, RCON command and close. Records carry the connection id,
//! command text, sizes and outcome -- NEVER the password, host or resolved IP.

use crate::directory::Scope;

/// One audited fact.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuditEvent {
    /// A session/exchange open attempt and its outcome (`ok` or a fixed error label/code).
    Open {
        scope: Scope,
        server: String,
        op: &'static str,
        outcome: String,
    },
    /// One RCON command the guest wrote on an authenticated session (allowed by the host caps).
    Command {
        scope: Scope,
        server: String,
        command: String,
        outcome: String,
    },
    /// A session closed, with its byte totals.
    Close {
        scope: Scope,
        server: String,
        bytes_in: usize,
        bytes_out: usize,
    },
}

/// Destination for audit records (default: structured log; production may also persist).
pub trait AuditSink: Send + Sync {
    /// Records one event; must not block or fail the call.
    fn record(&self, event: &AuditEvent);
}

/// Emits each event as a structured `tracing` record on target `gameserver_net_audit`.
pub struct TracingAudit;

impl AuditSink for TracingAudit {
    fn record(&self, event: &AuditEvent) {
        match event {
            AuditEvent::Open {
                scope,
                server,
                op,
                outcome,
            } => tracing::info!(
                target: "gameserver_net_audit",
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                server = %server,
                op,
                outcome = %outcome,
                "gameserver-net open"
            ),
            AuditEvent::Command {
                scope,
                server,
                command,
                outcome,
            } => tracing::info!(
                target: "gameserver_net_audit",
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                server = %server,
                command = %command,
                outcome = %outcome,
                "gameserver-net rcon command"
            ),
            AuditEvent::Close {
                scope,
                server,
                bytes_in,
                bytes_out,
            } => tracing::info!(
                target: "gameserver_net_audit",
                tenant_id = scope.tenant_id,
                community_id = scope.community_id,
                server = %server,
                bytes_in,
                bytes_out,
                "gameserver-net close"
            ),
        }
    }
}

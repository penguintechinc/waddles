//! Audit logging: tenant, community, app, destination category and host,
//! and the allow/deny decision -- **never payload/body bytes**. Emitted as
//! a structured `tracing` event on a dedicated target so log pipelines can
//! route/retain it separately from ordinary operational logs.

use tracing::info;

use crate::ip_policy::DestinationCategory;

pub const AUDIT_TARGET: &str = "egress_proxy.audit";

#[allow(clippy::too_many_arguments)]
pub fn log_decision(
    tenant: &str,
    community: &str,
    app: &str,
    category: DestinationCategory,
    host: &str,
    port: u16,
    allowed: bool,
    reason: Option<&str>,
) {
    info!(
        target: AUDIT_TARGET,
        tenant,
        community,
        app,
        category = ?category,
        host,
        port,
        allowed,
        reason,
        "egress_proxy.audit_decision"
    );
}

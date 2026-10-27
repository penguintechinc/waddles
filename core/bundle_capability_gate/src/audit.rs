//! Audit-log events and metrics for every `authorize()` decision (spec
//! SS5.4): "Every denial: (1) audit-logged (`tracing` + OTel, sanitized per
//! existing `capabilities.rs` pattern), (2) counted
//! (`waddles_bundle_capability_denied_total{app_id, permission, reason}`)".
//!
//! `app_id`/`permission`/`reason` only -- never a target UUID, params, or any
//! other field a `HostCallBody.args` payload might carry, keeping this event
//! stream itself compliant with the same PII boundary spec SS10.1 imposes
//! everywhere else (a target_user UUID is not PII per SS10.1, but this
//! module still keeps to the three labels spec SS5.4 names, deliberately).

use crate::denied::Denied;
use crate::permission::PermissionId;
use crate::scope::InvokeScope;

pub(crate) fn record_denied(scope: &InvokeScope, permission: &PermissionId, reason: Denied) {
    tracing::warn!(
        tenant_id = scope.tenant_id(),
        community_id = scope.community_id(),
        app_id = %scope.app_id(),
        app_version = scope.app_version(),
        permission = %permission,
        reason = reason.reason_str(),
        "bundle capability gate: denied"
    );
    metrics::counter!(
        "waddles_bundle_capability_denied_total",
        "app_id" => scope.app_id().to_string(),
        "permission" => permission.canonical_id(),
        "reason" => reason.reason_str(),
    )
    .increment(1);
}

pub(crate) fn record_authorized(scope: &InvokeScope, permission: &PermissionId) {
    tracing::debug!(
        tenant_id = scope.tenant_id(),
        community_id = scope.community_id(),
        app_id = %scope.app_id(),
        app_version = scope.app_version(),
        permission = %permission,
        "bundle capability gate: authorized"
    );
    metrics::counter!(
        "waddles_bundle_capability_authorized_total",
        "app_id" => scope.app_id().to_string(),
        "permission" => permission.canonical_id(),
    )
    .increment(1);
}

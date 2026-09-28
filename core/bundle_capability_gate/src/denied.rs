//! `Denied`: the typed denial reasons `authorize()` returns (spec SS5.4) --
//! the stable `reason` vocabulary every capability's WIT `denied(string)`
//! error eventually carries. This crate's own [`crate::gate::CapabilityGate::
//! authorize`] triggers a subset directly (see each variant's doc); the rest
//! are reserved so capability-specific, post-authorize validation (spec
//! SS5.3, not this gate) reports through the same shared vocabulary for
//! consistent audit logging and metrics.
//!
//! **Out of scope, intentionally:** the *global* per-bundle and
//! per-publisher reputation caps and the distribution/entropy anomaly
//! auto-suspend threshold (spec SS7.3, Gemini condition 4) are hub-api-side
//! controls -- they aggregate across every community/tenant an app is
//! activated in platform-wide, which is outside any single `authorize()`
//! call's (tenant, community, app) scope. This crate's [`crate::quota`]
//! ledger only enforces the per-call, per-user, and per-scope
//! (community/tenant) caps that *are* checkable from a single call's
//! `InvokeScope`. Spec SS12 Phase 10 tracks the hub-api-side aggregation job
//! and its platform-wide suspend-and-notify action as separate work.

use std::fmt;

/// Mirrors spec SS5.4's exact reason strings.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Denied {
    /// The community's current grant set has no entry for this permission
    /// id at this `(tenant, community, app_id, app_version)` -- including a
    /// missing/invalidated `GrantSnapshot` entry entirely (fail-closed on a
    /// cache miss, spec SS4/SS5.3). Triggered by this crate.
    NotGranted,
    /// A capability implementation passed a [`crate::resource::ResourceRef`]
    /// variant that doesn't match the permission's `AppScoped`/
    /// `ReputationScoped` classification (spec SS5.2) -- a host wiring bug,
    /// never a guest-triggerable condition. Triggered by this crate.
    ResourceScopeMismatch,
    /// A cumulative/aggregate cap was exceeded (spec SS7.3's daily
    /// aggregates). Triggered by this crate for [`crate::permission::Quota::
    /// ReputationDelta`]'s two aggregate checks.
    QuotaExceeded,
    /// A [`crate::permission::Quota::CallsPerWindow`] rate limit was
    /// exceeded. Triggered by this crate.
    RateLimited,
    /// A `ReputationScoped` `target_user` does not belong to the
    /// invocation's community/tenant (spec SS5.2). Triggered by this crate.
    UserNotInScope,
    /// A `reputation.*.write` per-call `delta` fell outside the granted
    /// `delta_min`/`delta_max` bound or the catalog's per-call ceiling (spec
    /// SS1/SS7.2 step 2). Triggered by this crate.
    DeltaOutOfBounds,
    /// A `chat.send`/`moderation` platform, or a `users.profile.read`
    /// platform with no non-identifying attribute to offer (spec SS10.2), is
    /// not supported. Reserved -- not triggered by this crate's `authorize()`
    /// today (permission-id parsing already rejects an uncompiled platform
    /// before a `ResourceRef` is ever built); kept for the capability-
    /// specific per-platform check spec SS10.2 describes.
    UnsupportedPlatform,
    /// Reserved for `ai.generate`'s host-side PII rejection (spec SS9) --
    /// capability-specific, not triggered by this crate.
    ContainsPii,
}

impl Denied {
    /// The stable string spec SS5.4 defines for audit logs/metrics labels.
    pub fn reason_str(&self) -> &'static str {
        match self {
            Self::NotGranted => "not_granted",
            Self::ResourceScopeMismatch => "resource_scope_mismatch",
            Self::QuotaExceeded => "quota_exceeded",
            Self::RateLimited => "rate_limited",
            Self::UserNotInScope => "user_not_in_scope",
            Self::DeltaOutOfBounds => "delta_out_of_bounds",
            Self::UnsupportedPlatform => "unsupported_platform",
            Self::ContainsPii => "contains_pii",
        }
    }
}

impl fmt::Display for Denied {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.reason_str())
    }
}

impl std::error::Error for Denied {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reason_str_matches_spec_ss5_4_exactly() {
        assert_eq!(Denied::NotGranted.reason_str(), "not_granted");
        assert_eq!(
            Denied::ResourceScopeMismatch.reason_str(),
            "resource_scope_mismatch"
        );
        assert_eq!(Denied::QuotaExceeded.reason_str(), "quota_exceeded");
        assert_eq!(Denied::RateLimited.reason_str(), "rate_limited");
        assert_eq!(Denied::UserNotInScope.reason_str(), "user_not_in_scope");
        assert_eq!(Denied::DeltaOutOfBounds.reason_str(), "delta_out_of_bounds");
        assert_eq!(
            Denied::UnsupportedPlatform.reason_str(),
            "unsupported_platform"
        );
        assert_eq!(Denied::ContainsPii.reason_str(), "contains_pii");
    }
}

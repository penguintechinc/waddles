//! Spec §13.5 PostHog flag keys this crate gates on, resolved through
//! `penguin_licensing::LicenseClient::flag_enabled` with that crate's own
//! last-known-cached/fail-closed-to-OFF semantics -- see its module doc
//! ("Graceful degradation on outage: ... nothing cached → community/free
//! tier with flags and gated features defaulting OFF"). Both flags below
//! are `min_tier: free` (spec: "core product, no entitlement gate"), so
//! `flag_enabled` alone is sufficient for either -- no `check_feature`
//! license-tier check is needed.
//!
//! [`FeatureFlag`] is a narrow trait wrapping one flag's live state --
//! the same per-dependency-seam pattern this crate already uses for every
//! other external dependency (`crate::capabilities::RelayQueue`,
//! `crate::egress::HttpTransport`, `crate::dispatch::AuditSink`, ...): the
//! real implementation ([`LicenseFlag`]) is a thin wrapper over a live
//! `penguin_licensing::LicenseClient`, and [`StaticFlag`] is both this
//! crate's test fixture (no live license/PostHog server needed to hold a
//! flag ON/OFF) and production's fallback value when even the
//! no-network-required default `LicenseConfig` fails to construct (see
//! `crate::lib::build_license_client`'s doc for why that path exists at
//! all despite being unreachable in practice).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

/// Gates the action-stage drain loop (`crate::dispatch::drain_loop`). OFF
/// ⇒ the stage serves `/health`/`/metrics` and drains nothing -- the safe
/// state during rollout.
pub const RUST_DATA_PLANE_FLAG: &str = "waddles.core.rust-data-plane";

/// Gates the bundle `http` host capability (`crate::egress::EgressGuard`).
/// OFF ⇒ every egress call is denied `feature_disabled`.
pub const BUNDLE_EGRESS_FLAG: &str = "waddles.core.bundle-egress";

/// One flag's live enabled/disabled state. Object-safe (a manually-boxed
/// future, matching every other async trait in this crate) so callers can
/// hold `Arc<dyn FeatureFlag>` without an `async_trait` dependency.
pub trait FeatureFlag: Send + Sync {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

/// The real [`FeatureFlag`]: `key` resolved via a live
/// `penguin_licensing::LicenseClient` (cheap to clone via `Arc`, per that
/// crate's own doc -- `LicenseFlag` holds its own clone so many flags can
/// share one client).
pub struct LicenseFlag {
    client: Arc<penguin_licensing::LicenseClient>,
    key: &'static str,
}

impl LicenseFlag {
    pub fn new(client: Arc<penguin_licensing::LicenseClient>, key: &'static str) -> Self {
        Self { client, key }
    }
}

impl FeatureFlag for LicenseFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(self.client.flag_enabled(self.key))
    }
}

/// A [`FeatureFlag`] that never varies. Production's fallback when the
/// license client itself failed to construct (spec's fail-closed-to-OFF
/// posture applied to gating as a whole, not just to a never-seen flag --
/// the same "must never silently fail open" precedent `crate::hop`'s
/// missing-keyring handling already sets); this crate's own tests use it
/// as the primary way to hold a flag ON or OFF deterministically without a
/// live license/PostHog server.
pub struct StaticFlag(pub bool);

impl FeatureFlag for StaticFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        let value = self.0;
        Box::pin(async move { value })
    }
}

/// Type-erased convenience, mirroring `crate::capabilities::boxed`.
pub fn boxed(flag: impl FeatureFlag + 'static) -> Arc<dyn FeatureFlag> {
    Arc::new(flag)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn static_flag_reports_its_fixed_value() {
        assert!(StaticFlag(true).enabled().await);
        assert!(!StaticFlag(false).enabled().await);
    }

    #[tokio::test]
    async fn boxed_wraps_a_flag_as_an_arc_dyn() {
        let flag: Arc<dyn FeatureFlag> = boxed(StaticFlag(true));
        assert!(flag.enabled().await);
    }

    /// Proves `LicenseFlag` genuinely calls through to a real
    /// `penguin_licensing::LicenseClient` rather than being its own
    /// independent stub: a cold client that has never successfully
    /// fetched anything (no network access in this test, no `refresh()`
    /// call) has an empty snapshot, and `flag_enabled` documents that as
    /// "never-seen flags are OFF" -- so this must report `false`, proving
    /// the wrapper reaches the real fail-closed-to-OFF behavior rather
    /// than defaulting some other way on its own.
    #[tokio::test]
    async fn license_flag_reaches_a_real_cold_client_and_fails_closed_to_off() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = LicenseFlag::new(client, RUST_DATA_PLANE_FLAG);
        assert!(!flag.enabled().await);
    }
}

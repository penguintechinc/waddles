//! Feature-flag gate for the process-stage drain loop (spec §13.5):
//! `waddles.core.rust-data-plane` (PostHog-compatible, `penguin_licensing`,
//! `license.penguintech.io`), default OFF.
//!
//! [`FeatureGate`] is a narrow, object-safe trait -- the same "wrap the
//! external dependency behind a one-method trait" pattern this crate
//! already uses for `StreamReader` (`crate::spine`) and `SpineOps`
//! (`crate::spine`): production wires [`LicenseFeatureGate`] (a real
//! `Arc<penguin_licensing::LicenseClient>`), tests wire a fixed or
//! toggleable fake, so the drain loop's own tests never perform real
//! network I/O against `license.penguintech.io`.
//!
//! `penguin_licensing::LicenseClient::flag_enabled` is already exactly the
//! contract this task requires (crate's own module doc, `packages/
//! rust-licensing/src/lib.rs`): non-blocking (never inline network I/O --
//! reads the current cached snapshot and schedules a background refresh
//! when stale), fail-closed default OFF ("never-seen flags are OFF"), and
//! graceful degradation on an unreachable license server (keeps serving
//! the last-known-cached snapshot; `None` cached -> OFF). This module adds
//! no additional caching/backoff of its own -- it would only duplicate
//! what the crate already does correctly.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use penguin_licensing::{LicenseClient, LicenseConfig, LicenseError};

/// The flag key gating the process-stage drain loop (spec §13.5). Flag-key
/// convention: `{product}.{feature-name}` (`rules/critical-rules.md`
/// Feature Flags & License Tiers) -- product is `waddles`.
pub const RUST_DATA_PLANE_FLAG: &str = "waddles.core.rust-data-plane";

/// Answers "should the drain loop run right now?". Object-safe (a
/// manually-boxed future rather than `async fn` in a trait), mirroring
/// `crate::capabilities::CapabilityHandler`'s identical rationale.
pub trait FeatureGate: Send + Sync {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

/// Production [`FeatureGate`]: delegates to a real
/// `penguin_licensing::LicenseClient`.
pub struct LicenseFeatureGate(Arc<LicenseClient>);

impl LicenseFeatureGate {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureGate for LicenseFeatureGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move { self.0.flag_enabled(RUST_DATA_PLANE_FLAG).await })
    }
}

/// Builds the process's `penguin_licensing::LicenseClient` from the
/// standard env vars (`LICENSE_KEY`, `LICENSE_SERVER_URL`, `POSTHOG_HOST`,
/// `POSTHOG_KEY`, optionally `LICENSE_DEPLOYMENT_DOMAIN`) -- `crate::lib::try_start_process_loop`'s caller. Never
/// touches the network itself (`LicenseClient::new` only builds an HTTP
/// client and validates URL schemes); the first real request happens
/// lazily, in the background, the first time [`FeatureGate::enabled`] is
/// polled and finds the cache stale.
///
/// Fails only on a malformed `LICENSE_SERVER_URL`/`POSTHOG_HOST` (bad URL
/// syntax, or a non-HTTPS non-localhost scheme -- `LicenseConfig::
/// validate_urls`) -- the caller treats this the same as every other
/// startup-config gate in this crate (log a warning, disable the drain
/// loop rather than starting it unverified).
pub fn build_license_client(product: &str) -> Result<Arc<LicenseClient>, LicenseError> {
    let mut cfg = LicenseConfig::from_env(product)?;
    // Set deployment domain from env var if present and non-empty,
    // to enable domain-based license bypass for internal deployments.
    if let Ok(domain) = std::env::var("LICENSE_DEPLOYMENT_DOMAIN") {
        if !domain.trim().is_empty() {
            cfg = cfg.with_deployment_domain(domain);
        }
    }
    LicenseClient::new(cfg)
}

#[cfg(test)]
pub(crate) mod test_support {
    //! Fakes for `crate::spine`'s own drain-loop tests -- kept here (not
    //! `#[cfg(test)]` inline in `spine.rs`) so both this module's own
    //! tests and `crate::spine`'s can share them without duplicating the
    //! boilerplate.
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// A [`FeatureGate`] whose answer is fixed at construction.
    pub struct FixedGate(pub bool);
    impl FeatureGate for FixedGate {
        fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
            let value = self.0;
            Box::pin(async move { value })
        }
    }

    /// A [`FeatureGate`] whose answer can be flipped at runtime -- proves
    /// the drain loop reacts to a live flag flip without a restart.
    #[derive(Default)]
    pub struct ToggleGate(pub AtomicBool);
    impl ToggleGate {
        pub fn new(initial: bool) -> Self {
            Self(AtomicBool::new(initial))
        }
        pub fn set(&self, value: bool) {
            self.0.store(value, Ordering::SeqCst);
        }
    }
    impl FeatureGate for ToggleGate {
        fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
            Box::pin(async move { self.0.load(Ordering::SeqCst) })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::{FixedGate, ToggleGate};
    use super::*;

    #[tokio::test]
    async fn fixed_gate_returns_its_constructed_value() {
        assert!(FixedGate(true).enabled().await);
        assert!(!FixedGate(false).enabled().await);
    }

    #[tokio::test]
    async fn toggle_gate_reflects_the_current_value() {
        let gate = ToggleGate::new(false);
        assert!(!gate.enabled().await);
        gate.set(true);
        assert!(gate.enabled().await);
        gate.set(false);
        assert!(!gate.enabled().await);
    }

    #[test]
    fn rust_data_plane_flag_matches_the_product_flag_key_convention() {
        assert_eq!(RUST_DATA_PLANE_FLAG, "waddles.core.rust-data-plane");
    }

    #[test]
    fn build_license_client_succeeds_with_no_env_configured() {
        // Defaults (`https://license.penguintech.io` for both URLs, no
        // key) are already valid HTTPS -- `LicenseConfig::from_env` must
        // succeed even with nothing set, matching "default OFF, never a
        // hard startup requirement" for a feature-flag dependency.
        let client = build_license_client("waddles-test-defaults");
        assert!(client.is_ok());
    }

    #[test]
    fn build_license_client_rejects_a_malformed_license_server_url() {
        // SAFETY: `cargo test` runs single-threaded per test binary by
        // default is not guaranteed, but this crate's other env-mutating
        // tests already rely on `std::env::set_var`/`remove_var` without
        // a lock when the variable is exclusive to one test -- no other
        // test in this crate reads `LICENSE_SERVER_URL`.
        unsafe { std::env::set_var("LICENSE_SERVER_URL", "not a url") };
        let result = build_license_client("waddles-test-bad-url");
        unsafe { std::env::remove_var("LICENSE_SERVER_URL") };
        assert!(result.is_err());
    }

    #[test]
    fn license_feature_gate_wraps_a_real_client() {
        // Compile-time + runtime proof that `LicenseFeatureGate` actually
        // implements `FeatureGate` over a real (never-refreshed)
        // `LicenseClient` -- no network I/O, since `flag_enabled` is only
        // called inside the `#[tokio::test]` below.
        let client = build_license_client("waddles-test-gate-wrap").expect("valid defaults");
        let _gate: Box<dyn FeatureGate> = Box::new(LicenseFeatureGate::new(client));
    }

    #[tokio::test]
    async fn license_feature_gate_defaults_off_with_no_snapshot_yet() {
        // A freshly built client has never fetched anything -- `flag_
        // enabled` must return `false` (fail-closed default) rather than
        // blocking on a network round trip. The call schedules a
        // background refresh (fire-and-forget); this test does not wait
        // for or assert on it.
        let client = build_license_client("waddles-test-gate-default-off").expect("valid defaults");
        let gate = LicenseFeatureGate::new(client);
        assert!(!gate.enabled().await);
    }
}

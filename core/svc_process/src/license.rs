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
    let cfg = LicenseConfig::from_env(product)?;
    let domain_env = std::env::var("LICENSE_DEPLOYMENT_DOMAIN").ok();
    let cfg = apply_deployment_domain(cfg, domain_env.as_deref());
    LicenseClient::new(cfg)
}

/// Applies an optional `LICENSE_DEPLOYMENT_DOMAIN` override to `cfg`,
/// trimming whitespace and treating an empty/whitespace-only value the
/// same as "unset". Pure -- no env access of its own -- so the trim-and-
/// apply logic is unit-testable directly (see the `apply_deployment_
/// domain_*` tests below) without mutating the process-wide
/// `LICENSE_DEPLOYMENT_DOMAIN` env var, which is a data race under
/// `cargo test`'s default parallel execution: any other test calling
/// [`build_license_client`] concurrently would observe the mutated value
/// too, e.g. making `LicenseConfig::domain_bypassed` unexpectedly `true`
/// for a client a different, unrelated test expected to stay un-bypassed
/// (the exact nondeterministic CI failure this refactor fixes).
/// [`build_license_client`] reads the env exactly once and passes the
/// result in here.
fn apply_deployment_domain(cfg: LicenseConfig, raw: Option<&str>) -> LicenseConfig {
    match raw.map(str::trim) {
        Some(domain) if !domain.is_empty() => cfg.with_deployment_domain(domain.to_owned()),
        _ => cfg,
    }
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
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variable. Mirrors
    // `core/svc_ingest/src/license.rs`'s identical guard for the identical
    // hazard. Only `LICENSE_SERVER_URL` needs this now -- the
    // `LICENSE_DEPLOYMENT_DOMAIN` env var is no longer mutated by any test
    // in this file; `apply_deployment_domain`'s tests exercise that logic
    // as a pure function instead (no env access at all).
    static ENV_LOCK: Mutex<()> = Mutex::new(());

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
        //
        // Guarded by ENV_LOCK: `build_license_client` reads the ambient
        // `LICENSE_SERVER_URL` env var via `LicenseConfig::from_env`, so
        // this must not overlap with the malformed-URL test below, which
        // transiently sets it to an invalid value.
        let _guard = ENV_LOCK.lock().unwrap();
        let client = build_license_client("waddles-test-defaults");
        assert!(client.is_ok());
    }

    #[test]
    fn build_license_client_rejects_a_malformed_license_server_url() {
        // Guarded by ENV_LOCK -- see its doc comment: this is the only
        // remaining env-mutating test in this file (`LICENSE_SERVER_URL`
        // parsing lives in the external `penguin_licensing::LicenseConfig
        // ::from_env`, so it can't be tested as a pure function the way
        // `apply_deployment_domain` is). Without the lock, this
        // transiently-invalid value would race with every other test that
        // calls `build_license_client`/`from_env` concurrently.
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
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
        //
        // Guarded by ENV_LOCK -- see `build_license_client_succeeds_with_
        // no_env_configured`.
        let _guard = ENV_LOCK.lock().unwrap();
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
        //
        // The ENV_LOCK guard is scoped to just the `build_license_client`
        // call (dropped before the `.await` below) -- `std::sync::
        // MutexGuard` is `!Send` and must never be held across an await
        // point; once the client is built, the env value has already been
        // read into an owned `Url`/`String`, so later mutation elsewhere
        // cannot affect it.
        let client = {
            let _guard = ENV_LOCK.lock().unwrap();
            build_license_client("waddles-test-gate-default-off").expect("valid defaults")
        };
        let gate = LicenseFeatureGate::new(client);
        assert!(!gate.enabled().await);
    }

    #[test]
    fn apply_deployment_domain_none_leaves_config_unset() {
        // No env access at all -- `LicenseConfig::new` never reads the
        // process environment, and neither does `apply_deployment_domain`
        // itself, so this test needs no ENV_LOCK guard and can run fully
        // in parallel with every other test in this crate.
        let cfg = LicenseConfig::new("waddles-test-domain-none").expect("valid defaults");
        let cfg = apply_deployment_domain(cfg, None);
        assert!(cfg.deployment_domain.is_none());
    }

    #[test]
    fn apply_deployment_domain_sets_a_trimmed_value() {
        let cfg = LicenseConfig::new("waddles-test-domain-set").expect("valid defaults");
        let cfg = apply_deployment_domain(cfg, Some("  test.penguintech.cloud  "));
        assert_eq!(
            cfg.deployment_domain.as_deref(),
            Some("test.penguintech.cloud")
        );
    }

    #[test]
    fn apply_deployment_domain_ignores_empty_or_whitespace_only() {
        let cfg = LicenseConfig::new("waddles-test-domain-empty").expect("valid defaults");
        let cfg = apply_deployment_domain(cfg, Some("   "));
        assert!(cfg.deployment_domain.is_none());

        let cfg = LicenseConfig::new("waddles-test-domain-empty-str").expect("valid defaults");
        let cfg = apply_deployment_domain(cfg, Some(""));
        assert!(cfg.deployment_domain.is_none());
    }
}

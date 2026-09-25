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

/// PenguinTech/Waddles-owned bypass suffix -- the sole license/flag
/// bypass lever, and it must be a hardcoded source-level constant, never
/// an env var, CLI flag, or Helm-templated value (`rules/critical-
/// rules.md` Feature Flags & License Tiers: "bypass is domain-based ONLY,
/// never env var/CLI arg/config flag" -- a prior revision of this file
/// read `LICENSE_DEPLOYMENT_DOMAIN` from the environment, which let
/// anyone with Helm-values/env access fabricate an arbitrary bypass
/// domain string with no real DNS control; that was reverted).
///
/// `waddles.app` is not one of the pinned `penguin_licensing` crate's
/// *default* bypass suffixes (`penguintech.cloud`/`penguincloud.io` --
/// see `LicenseConfig::DEFAULT_BYPASS_DOMAINS`), so it is explicitly
/// registered via [`LicenseConfig::with_bypass_domain`] below -- exactly
/// the mechanism that crate's own module doc describes: "Product `.app`
/// domains are added in code with `LicenseConfig::with_bypass_domain`"
/// (`packages/rust-licensing/src/config.rs`). `domain_bypassed()`'s own
/// match rule (`domain == suffix || domain.ends_with(".{suffix}")`)
/// already does suffix/subdomain matching, so registering the bare apex
/// here makes every `*.waddles.app` deployment domain bypass, not just
/// this exact literal (`waddles_app_bypass_domain_matches_any_subdomain`
/// below proves it).
const BYPASS_DOMAIN: &str = "waddles.app";

/// This service's own deployment domain -- a `*.waddles.app` subdomain,
/// hardcoded in source (see [`BYPASS_DOMAIN`]'s doc for why it can never
/// be an env var/config value). Distinct per service (`svc-ingest.
/// waddles.app`/`svc-process.waddles.app`/`svc-action.waddles.app`) so
/// each binary's own bypass is traceable to the service that claimed it,
/// though all three resolve bypass true against the same registered
/// [`BYPASS_DOMAIN`] suffix.
const DEPLOYMENT_DOMAIN: &str = "svc-process.waddles.app";

/// Builds the process's `penguin_licensing::LicenseClient` from the
/// standard env vars (`LICENSE_KEY`, `LICENSE_SERVER_URL`, `POSTHOG_HOST`,
/// `POSTHOG_KEY`) -- `crate::lib::try_start_process_loop`'s caller --
/// plus the hardcoded [`DEPLOYMENT_DOMAIN`]/[`BYPASS_DOMAIN`] bypass.
/// Never touches the network itself (`LicenseClient::new` only builds an
/// HTTP client and validates URL schemes); the first real request happens
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
    let cfg = cfg.with_bypass_domain(BYPASS_DOMAIN);
    let cfg = apply_deployment_domain(cfg, Some(DEPLOYMENT_DOMAIN));
    LicenseClient::new(cfg)
}

/// Applies an optional deployment-domain value to `cfg`, trimming
/// whitespace and treating an empty/whitespace-only value the same as
/// "unset". Pure -- no env access of its own -- so the trim-and-apply
/// logic is unit-testable directly (see the `apply_deployment_domain_*`
/// tests below) independent of where the caller's value comes from.
/// [`build_license_client`] always passes the hardcoded
/// [`DEPLOYMENT_DOMAIN`] constant; this function stays generic over
/// `Option<&str>` because a `None`/empty input is still meaningful
/// behavior worth testing on its own (e.g. a future caller building a
/// [`LicenseConfig`] with no bypass at all).
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
    async fn license_feature_gate_defaults_off_for_a_non_bypassed_client_with_no_snapshot() {
        // A freshly built, non-bypassed client has never fetched anything
        // -- `flag_enabled` must return `false` (fail-closed default)
        // rather than blocking on a network round trip. Built directly
        // via `LicenseConfig::new` (no `with_bypass_domain`/`with_
        // deployment_domain`) rather than through `build_license_client`,
        // which now *always* applies the hardcoded `DEPLOYMENT_DOMAIN`
        // bypass (see `build_license_client_hardcoded_domain_bypasses_
        // flag_checks` below) -- this test isolates the underlying
        // fail-closed contract from that bypass. No env access at all, so
        // no ENV_LOCK guard needed.
        let cfg = LicenseConfig::new("waddles-test-gate-default-off").expect("valid defaults");
        let client = LicenseClient::new(cfg).expect("client construction");
        let gate = LicenseFeatureGate::new(client);
        assert!(!gate.enabled().await);
    }

    #[tokio::test]
    async fn build_license_client_hardcoded_domain_bypasses_flag_checks() {
        // Proves the net effect the hardcoded DEPLOYMENT_DOMAIN bypass is
        // meant to have: `build_license_client`'s own output resolves
        // every flag ON, with zero network access and zero env/config
        // spoofability (`rules/critical-rules.md` Feature Flags & License
        // Tiers: bypass is domain-based ONLY -- satisfied entirely in
        // source here).
        let client = {
            let _guard = ENV_LOCK.lock().unwrap();
            build_license_client("waddles-test-hardcoded-domain").expect("valid defaults")
        };
        assert!(client.bypass_active());
        let gate = LicenseFeatureGate::new(client);
        assert!(gate.enabled().await);
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

    #[test]
    fn waddles_app_bypass_domain_matches_any_subdomain() {
        // `LicenseConfig::domain_bypassed()`'s own match rule is
        // `domain == suffix || domain.ends_with(".{suffix}")` (`packages/
        // rust-licensing/src/config.rs`) -- proves registering the bare
        // `BYPASS_DOMAIN` apex via `with_bypass_domain` makes both the
        // apex itself AND any `*.waddles.app` subdomain (this service's
        // own `DEPLOYMENT_DOMAIN` included) resolve bypass true, not just
        // one exact literal.
        let apex = LicenseConfig::new("waddles-test-apex-match")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN);
        let apex = apply_deployment_domain(apex, Some(BYPASS_DOMAIN));
        assert!(apex.domain_bypassed());

        let subdomain = LicenseConfig::new("waddles-test-subdomain-match")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN);
        let subdomain = apply_deployment_domain(subdomain, Some(DEPLOYMENT_DOMAIN));
        assert!(subdomain.domain_bypassed());

        let arbitrary_subdomain = LicenseConfig::new("waddles-test-arbitrary-subdomain")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN);
        let arbitrary_subdomain =
            apply_deployment_domain(arbitrary_subdomain, Some("anything.waddles.app"));
        assert!(arbitrary_subdomain.domain_bypassed());
    }

    #[test]
    fn a_non_bypass_domain_does_not_resolve_bypass() {
        // Two negative cases: a domain sharing no suffix with
        // `BYPASS_DOMAIN` at all, and the adversarial near-miss
        // `evil-waddles.app` -- which contains the substring
        // `waddles.app` but does NOT end with the required `.waddles.app`
        // dot-boundary, so a naive substring check would wrongly bypass
        // it while the real suffix check correctly rejects it.
        let unrelated = LicenseConfig::new("waddles-test-unrelated-domain")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN);
        let unrelated = apply_deployment_domain(unrelated, Some("example.com"));
        assert!(!unrelated.domain_bypassed());

        let near_miss = LicenseConfig::new("waddles-test-near-miss-domain")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN);
        let near_miss = apply_deployment_domain(near_miss, Some("evil-waddles.app"));
        assert!(!near_miss.domain_bypassed());
    }
}

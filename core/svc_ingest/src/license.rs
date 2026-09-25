//! The `waddles.core.rust-data-plane` feature-flag gate (spec S13.5) on
//! the receive/produce/outbound-drain loops: a
//! `penguin_licensing::LicenseClient` built from the standard PenguinTech
//! environment variables (`LICENSE_KEY`/`LICENSE_SERVER_URL`/
//! `POSTHOG_HOST`/`POSTHOG_KEY`), shared as one `Arc` and checked once at
//! startup before `crate::lib::run_with_shutdown` spawns any receiver.
//!
//! Fail-closed by the crate's own design, never this module's own retry
//! logic: `LicenseClient::flag_enabled` performs no inline network I/O --
//! it serves the current cached snapshot (`false` for a never-seen flag,
//! or when nothing has been fetched yet) and schedules a background
//! refresh when stale, so a slow or unreachable license/flag server can
//! never block or crash startup. `build_license_client` mirrors that
//! contract for its own failure mode (a malformed `LICENSE_SERVER_URL`/
//! `POSTHOG_HOST`): logged once, `None` returned, every subsequent gate
//! check treated exactly like a never-seen flag (OFF) rather than a
//! startup panic.
//!
//! **Known limitation, not silently assumed away:** this gate is checked
//! once, at startup. Flipping the flag off in PostHog after the receivers
//! are already running does not stop them mid-flight -- there is no
//! existing mechanism in this service to cancel an already-spawned
//! receiver task based on a later flag change (spec S13.5's "continuous
//! re-evaluation" applies to the `--dev` flag's own re-check tick, not
//! documented here as satisfied). A follow-up owns wiring a periodic
//! re-check that can tear down and restart the receiver tasks.

use std::sync::Arc;

/// The flag key gating every fixed-platform receiver plus the outbound
/// relay drain (spec S13.5's convention: `{product}.{feature-name}`).
pub const RUST_DATA_PLANE_FLAG: &str = "waddles.core.rust-data-plane";

/// Product identifier passed to `penguin_licensing::LicenseConfig` --
/// distinct from the flag key's own leading segment only by convention
/// (both happen to be `"waddles"` today).
const PRODUCT: &str = "waddles";

/// PenguinTech/Waddles-owned bypass suffix -- the sole license/flag
/// bypass lever, hardcoded in source (never an env var, CLI flag, or Helm
/// value -- `rules/critical-rules.md` Feature Flags & License Tiers:
/// "bypass is domain-based ONLY, never env var/CLI arg/config flag". A
/// prior revision of this file read `LICENSE_DEPLOYMENT_DOMAIN` from the
/// environment, which let anyone with Helm-values/env access fabricate
/// an arbitrary bypass domain string with no real DNS control; that was
/// reverted).
///
/// `waddles.app` is not one of the pinned `penguin_licensing` crate's
/// *default* bypass suffixes (`penguintech.cloud`/`penguincloud.io` --
/// see `LicenseConfig::DEFAULT_BYPASS_DOMAINS`), so it is explicitly
/// registered via [`penguin_licensing::LicenseConfig::with_bypass_domain`]
/// below -- exactly the mechanism that crate's own module doc describes:
/// "Product `.app` domains are added in code with `LicenseConfig::
/// with_bypass_domain`" (`packages/rust-licensing/src/config.rs`).
/// `domain_bypassed()`'s own match rule (`domain == suffix ||
/// domain.ends_with(".{suffix}")`) already does suffix/subdomain
/// matching, so registering the bare apex here makes every
/// `*.waddles.app` deployment domain bypass, not just this exact literal
/// (`waddles_app_bypass_domain_matches_any_subdomain` below proves it).
const BYPASS_DOMAIN: &str = "waddles.app";

/// This service's own deployment domain -- a `*.waddles.app` subdomain,
/// hardcoded in source (see [`BYPASS_DOMAIN`]'s doc for why it can never
/// be an env var/config value). Distinct per service (`svc-ingest.
/// waddles.app`/`svc-process.waddles.app`/`svc-action.waddles.app`) so
/// each binary's own bypass is traceable to the service that claimed it,
/// though all three resolve bypass true against the same registered
/// [`BYPASS_DOMAIN`] suffix.
const DEPLOYMENT_DOMAIN: &str = "svc-ingest.waddles.app";

/// Builds the shared `LicenseClient` from the standard environment
/// variables plus the hardcoded [`DEPLOYMENT_DOMAIN`]/[`BYPASS_DOMAIN`]
/// bypass, and starts its background refresh loop. Returns `None` (never
/// an error) on a config problem (e.g. a malformed `LICENSE_SERVER_URL`)
/// -- logged once; every gate check then fails closed to OFF via
/// [`rust_data_plane_enabled`]'s own `None` branch, matching the crate's
/// "never-seen flag defaults OFF" contract rather than crashing startup
/// over a license-server misconfiguration.
pub fn build_license_client() -> Option<Arc<penguin_licensing::LicenseClient>> {
    let cfg = match penguin_licensing::LicenseConfig::from_env(PRODUCT) {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license config invalid; {RUST_DATA_PLANE_FLAG} defaults OFF"
            );
            return None;
        }
    };
    let cfg = cfg
        .with_bypass_domain(BYPASS_DOMAIN)
        .with_deployment_domain(DEPLOYMENT_DOMAIN);
    match penguin_licensing::LicenseClient::new(cfg) {
        Ok(client) => {
            // The returned `JoinHandle` is intentionally dropped -- "drop
            // it to let the loop run for the process lifetime" is the
            // crate's own documented contract for `spawn_refresh`.
            client.spawn_refresh();
            Some(client)
        }
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license client construction failed; {RUST_DATA_PLANE_FLAG} defaults OFF"
            );
            None
        }
    }
}

/// Checks the `waddles.core.rust-data-plane` flag. Non-blocking (the
/// crate's own `flag_enabled` never performs inline network I/O) and
/// fail-closed: no client (see [`build_license_client`]) or a never-seen/
/// unreachable flag both evaluate to `false`.
pub async fn rust_data_plane_enabled(
    client: Option<&Arc<penguin_licensing::LicenseClient>>,
) -> bool {
    match client {
        Some(client) => client.flag_enabled(RUST_DATA_PLANE_FLAG).await,
        None => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variables. Scoped to
    // this file: no other module touches `LICENSE_*`/`POSTHOG_*`.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    fn clear_license_env() {
        for var in [
            "LICENSE_KEY",
            "LICENSE_SERVER_URL",
            "POSTHOG_HOST",
            "POSTHOG_KEY",
        ] {
            // SAFETY: serialized by ENV_LOCK.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[tokio::test]
    async fn rust_data_plane_enabled_is_false_when_no_client() {
        assert!(!rust_data_plane_enabled(None).await);
    }

    #[tokio::test]
    async fn rust_data_plane_enabled_is_false_for_a_fresh_client_with_no_snapshot() {
        // `crate::crypto::ensure_installed()` first: `LicenseClient::new`
        // builds an HTTPS `reqwest::Client` (a different rustls crypto
        // backend than this crate's own `ring` pin -- see that module's
        // doc comment), and `flag_enabled` below schedules a real
        // background TLS-capable refresh attempt.
        crate::crypto::ensure_installed();
        // A freshly-built client (no `spawn_refresh`/`refresh` call, no
        // snapshot ever fetched) must fail closed to OFF -- exercises the
        // real `flag_enabled` call (which schedules its own fire-and-
        // forget background refresh attempt; this test never awaits it).
        let cfg = penguin_licensing::LicenseConfig::new("test-product")
            .expect("default config is always valid");
        let client = penguin_licensing::LicenseClient::new(cfg).expect("client construction");
        assert!(!rust_data_plane_enabled(Some(&client)).await);
    }

    #[tokio::test]
    async fn rust_data_plane_enabled_true_under_domain_bypass() {
        crate::crypto::ensure_installed();
        // Domain bypass (security.md / critical-rules.md: PenguinTech-
        // internal domains) makes every flag/feature evaluate enabled --
        // even with zero network access, proving the gate isn't
        // accidentally hard-wired to "always false in tests".
        let cfg = penguin_licensing::LicenseConfig::new("test-product")
            .expect("default config is always valid")
            .with_deployment_domain("svc-ingest.penguintech.cloud");
        let client = penguin_licensing::LicenseClient::new(cfg).expect("client construction");
        assert!(client.bypass_active());
        assert!(rust_data_plane_enabled(Some(&client)).await);
    }

    #[tokio::test]
    async fn build_license_client_succeeds_with_default_env() {
        // `#[tokio::test]`, not `#[test]`: `build_license_client` calls
        // `LicenseClient::spawn_refresh`, which `tokio::spawn`s onto the
        // ambient runtime -- requires a runtime context to exist.
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        clear_license_env();
        // Defaults (no LICENSE_*/POSTHOG_* set) resolve to the real
        // license.penguintech.io HTTPS URL, which passes `validate_urls`.
        assert!(build_license_client().is_some());
        clear_license_env();
    }

    #[tokio::test]
    async fn build_license_client_hardcoded_domain_bypasses_flag_checks() {
        // Proves the net effect the hardcoded DEPLOYMENT_DOMAIN bypass is
        // meant to have: `build_license_client`'s own output resolves
        // every flag ON, with zero network access and zero env/config
        // spoofability (`rules/critical-rules.md` Feature Flags & License
        // Tiers: bypass is domain-based ONLY -- satisfied entirely in
        // source here).
        crate::crypto::ensure_installed();
        let client = {
            let _guard = ENV_LOCK.lock().unwrap();
            clear_license_env();
            let client = build_license_client().expect("valid defaults");
            clear_license_env();
            client
        };
        assert!(client.bypass_active());
        assert!(rust_data_plane_enabled(Some(&client)).await);
    }

    #[test]
    fn build_license_client_none_on_invalid_server_url() {
        // Plain `#[test]` is fine here: the invalid URL fails
        // `validate_urls` inside `LicenseConfig::from_env` before
        // `spawn_refresh`/`tokio::spawn` is ever reached.
        let _guard = ENV_LOCK.lock().unwrap();
        clear_license_env();
        // SAFETY: serialized by ENV_LOCK above.
        unsafe { std::env::set_var("LICENSE_SERVER_URL", "ftp://not-https.example.com") };
        assert!(
            build_license_client().is_none(),
            "a non-HTTPS, non-localhost server URL must fail closed, not panic"
        );
        clear_license_env();
    }

    #[test]
    fn flag_key_matches_the_spec_s13_5_convention() {
        assert_eq!(RUST_DATA_PLANE_FLAG, "waddles.core.rust-data-plane");
    }

    #[test]
    fn waddles_app_bypass_domain_matches_any_subdomain() {
        // `LicenseConfig::domain_bypassed()`'s own match rule is
        // `domain == suffix || domain.ends_with(".{suffix}")` (`packages/
        // rust-licensing/src/config.rs`) -- proves registering the bare
        // `BYPASS_DOMAIN` apex via `with_bypass_domain` makes both the
        // apex itself AND any `*.waddles.app` subdomain (this service's
        // own `DEPLOYMENT_DOMAIN` included) resolve bypass true, not just
        // one exact literal. No env access -- no ENV_LOCK guard needed.
        let apex = penguin_licensing::LicenseConfig::new("waddles-test-apex-match")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain(BYPASS_DOMAIN);
        assert!(apex.domain_bypassed());

        let subdomain = penguin_licensing::LicenseConfig::new("waddles-test-subdomain-match")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain(DEPLOYMENT_DOMAIN);
        assert!(subdomain.domain_bypassed());

        let arbitrary_subdomain =
            penguin_licensing::LicenseConfig::new("waddles-test-arbitrary-subdomain")
                .expect("valid defaults")
                .with_bypass_domain(BYPASS_DOMAIN)
                .with_deployment_domain("anything.waddles.app");
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
        let unrelated = penguin_licensing::LicenseConfig::new("waddles-test-unrelated-domain")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain("example.com");
        assert!(!unrelated.domain_bypassed());

        let near_miss = penguin_licensing::LicenseConfig::new("waddles-test-near-miss-domain")
            .expect("valid defaults")
            .with_bypass_domain(BYPASS_DOMAIN)
            .with_deployment_domain("evil-waddles.app");
        assert!(!near_miss.domain_bypassed());
    }
}

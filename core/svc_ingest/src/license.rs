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

/// Builds the shared `LicenseClient` from the standard environment
/// variables and starts its background refresh loop. Returns `None`
/// (never an error) on a config problem (e.g. a malformed
/// `LICENSE_SERVER_URL`) -- logged once; every gate check then fails
/// closed to OFF via [`rust_data_plane_enabled`]'s own `None` branch,
/// matching the crate's "never-seen flag defaults OFF" contract rather
/// than crashing startup over a license-server misconfiguration.
pub fn build_license_client() -> Option<Arc<penguin_licensing::LicenseClient>> {
    let mut cfg = match penguin_licensing::LicenseConfig::from_env(PRODUCT) {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::warn!(
                error = %err,
                "license config invalid; {RUST_DATA_PLANE_FLAG} defaults OFF"
            );
            return None;
        }
    };
    // Set deployment domain from env var if present and non-empty,
    // to enable domain-based license bypass for internal deployments.
    if let Ok(domain) = std::env::var("LICENSE_DEPLOYMENT_DOMAIN") {
        if !domain.trim().is_empty() {
            cfg = cfg.with_deployment_domain(domain);
        }
    }
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
}

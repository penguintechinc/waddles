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

use std::future::Future;
use std::pin::Pin;

/// Derives a flag key's Docker ENV baseline variable: `FLAG_` + key uppercased, `.`/`-` -> `_`
/// (e.g. `FLAG_WADDLES_CORE_RUST_DATA_PLANE`).
pub fn env_flag_var_name(key: &str) -> String {
    let mut name = String::with_capacity(5 + key.len());
    name.push_str("FLAG_");
    for ch in key.chars() {
        match ch {
            '.' | '-' => name.push('_'),
            c => name.extend(c.to_uppercase()),
        }
    }
    name
}

/// Parses `key`'s `FLAG_*` env baseline; `None` when unset or unrecognized.
pub fn env_flag_value(key: &str) -> Option<bool> {
    let raw = std::env::var(env_flag_var_name(key)).ok()?;
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "on" | "yes" => Some(true),
        "false" | "0" | "off" | "no" => Some(false),
        _ => None,
    }
}

/// Feature-flag evaluation over a license client: PostHog snapshot, then the `FLAG_*` ENV
/// baseline, then OFF. The license-bypass domain answers LICENSE/entitlement checks only
/// (`tier`/`check_feature`) and never forces a feature flag or kill-switch -- the library's own
/// `flag_enabled` returns `true` for every key under bypass, so it is deliberately not used for
/// the value (user decision 2026-11-12).
pub trait PosthogFlag {
    /// Resolves `key`: PostHog snapshot, else ENV baseline, else `false`.
    fn flag_on<'a>(&'a self, key: &'a str) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

impl PosthogFlag for Arc<penguin_licensing::LicenseClient> {
    fn flag_on<'a>(&'a self, key: &'a str) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            // Side effect only: `validate` schedules a background refresh when the snapshot is
            // stale or missing -- bypass or not (`flag_enabled` returns early under bypass).
            let _ = self.validate().await;
            self.snapshot()
                .and_then(|snap| snap.flags.get(key).copied())
                .or_else(|| env_flag_value(key))
                .unwrap_or(false)
        })
    }
}

/// The PostHog snapshot's explicit value for `key`, or `None` when PostHog has never defined it
/// (no snapshot yet, or the key is unseen). Never forced by the bypass domain (license-only).
pub async fn posthog_flag_value(
    client: &Arc<penguin_licensing::LicenseClient>,
    key: &str,
) -> Option<bool> {
    // Side effect only: schedules a background refresh when the snapshot is stale or missing.
    let _ = client.validate().await;
    client
        .snapshot()
        .and_then(|snap| snap.flags.get(key).copied())
}

/// Flag precedence: an explicit PostHog value wins (so an explicit OFF beats `FLAG_*=true`),
/// else the ENV baseline, else OFF.
#[must_use]
pub fn posthog_then_env(posthog: Option<bool>, env_baseline: bool) -> bool {
    posthog.unwrap_or(env_baseline)
}

/// Resolves a flag with an optional client: no client => ENV baseline, else OFF.
pub async fn flag_on_opt(
    client: Option<&Arc<penguin_licensing::LicenseClient>>,
    key: &str,
) -> bool {
    match client {
        Some(c) => c.flag_on(key).await,
        None => env_flag_value(key).unwrap_or(false),
    }
}

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

/// Process-wide license client, set once at startup so later gates (e.g. the
/// connector feed's unverified-binding kill-switch) reuse it instead of
/// spawning a second refresh loop.
static SHARED_CLIENT: std::sync::OnceLock<Option<Arc<penguin_licensing::LicenseClient>>> =
    std::sync::OnceLock::new();

/// Publishes `client` as the process-wide license client (first call wins).
pub fn set_shared_license_client(client: Option<Arc<penguin_licensing::LicenseClient>>) {
    let _ = SHARED_CLIENT.set(client);
}

/// The process-wide license client, or `None` when unset/unbuilt (gates then
/// fall back to their env baseline / default).
pub fn shared_license_client() -> Option<Arc<penguin_licensing::LicenseClient>> {
    SHARED_CLIENT.get().cloned().flatten()
}

/// Checks the `waddles.core.rust-data-plane` flag (PostHog -> ENV -> OFF). Non-blocking and
/// fail-closed: a never-seen/unreachable flag evaluates to `false`; bypass domains never force it.
pub async fn rust_data_plane_enabled(
    client: Option<&Arc<penguin_licensing::LicenseClient>>,
) -> bool {
    flag_on_opt(client, RUST_DATA_PLANE_FLAG).await
}

#[cfg(test)]
// Env-mutating tests hold the std `ENV_LOCK` across awaits on purpose: each `#[tokio::test]` is
// single-threaded and the lock only serializes process-global env access between tests.
#[allow(clippy::await_holding_lock)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variables. Scoped to
    // this file: no other module touches `LICENSE_*`/`POSTHOG_*`.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    include!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../test_support/licensing_mock.rs"
    ));

    fn set_env(var: &str, val: &str) {
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var(var, val) };
    }

    fn unset_env(var: &str) {
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var(var) };
    }

    #[test]
    fn posthog_then_env_precedence_table() {
        assert!(
            !posthog_then_env(Some(false), true),
            "explicit OFF beats env ON"
        );
        assert!(
            posthog_then_env(Some(true), false),
            "explicit ON beats env OFF"
        );
        assert!(posthog_then_env(None, true), "unseen -> env baseline");
        assert!(!posthog_then_env(None, false), "unseen, no baseline -> OFF");
    }

    // regression: env must not beat an explicit PostHog OFF (3b)
    #[tokio::test]
    async fn explicit_posthog_off_beats_env_true_for_flag_on() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        let key = "waddles.test-explicit-off";
        let client = seeded_client(r#"{"waddles.test-explicit-off":false}"#, false).await;
        set_env(&env_flag_var_name(key), "true");
        let got = client.flag_on(key).await;
        unset_env(&env_flag_var_name(key));
        assert!(!got);
    }

    // regression: env must not beat an explicit PostHog OFF (3b) -- canary flag site
    #[tokio::test]
    async fn explicit_posthog_off_beats_env_true_for_canary_enabled() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        let client = seeded_client(r#"{"waddles.command-latency-canary":false}"#, false).await;
        set_env(command_latency::canary::ENV_BASELINE, "true");
        let got = crate::canary::canary_enabled(Some(&client)).await;
        let unseen = seeded_client("{}", false).await;
        let unseen_got = crate::canary::canary_enabled(Some(&unseen)).await;
        unset_env(command_latency::canary::ENV_BASELINE);
        assert!(!got, "explicit PostHog OFF beats FLAG_*=true");
        assert!(unseen_got, "unseen key falls to the ENV baseline");
        assert!(!crate::canary::canary_enabled(Some(&unseen)).await);
    }

    // regression: env must not beat an explicit PostHog OFF (3b) -- command-latency kill switch
    #[tokio::test]
    async fn explicit_posthog_off_beats_env_true_for_kill_switch_wanted() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        let client = seeded_client(
            r#"{"waddles.disable-command-latency-telemetry":false}"#,
            false,
        )
        .await;
        set_env(command_latency::switch::ENV_BASELINE, "true");
        let got = crate::canary::kill_switch_wanted(Some(&client)).await;
        let unseen = seeded_client("{}", false).await;
        let unseen_got = crate::canary::kill_switch_wanted(Some(&unseen)).await;
        unset_env(command_latency::switch::ENV_BASELINE);
        assert!(!got, "explicit PostHog OFF keeps the mechanism ON");
        assert!(unseen_got, "unseen key falls to the ENV baseline");
        assert!(!crate::canary::kill_switch_wanted(Some(&unseen)).await);
    }

    // regression: env must not beat an explicit PostHog OFF (3b) -- analytics producer site
    #[tokio::test]
    async fn explicit_posthog_off_beats_env_true_for_analytics_flag_wanted() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        let client = seeded_client(r#"{"waddles.analytics-stream":false}"#, false).await;
        assert!(!crate::analytics::flag_wanted(Some(&client), true).await);
        let unseen = seeded_client("{}", false).await;
        assert!(crate::analytics::flag_wanted(Some(&unseen), true).await);
        assert!(!crate::analytics::flag_wanted(Some(&unseen), false).await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_unset_flag_is_off() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        unset_env(&env_flag_var_name(RUST_DATA_PLANE_FLAG));
        let client = seeded_client(r#"{"waddles.other":true}"#, true).await;
        assert!(client.bypass_active());
        assert!(!rust_data_plane_enabled(Some(&client)).await);
        assert!(!crate::analytics::flag_wanted(Some(&client), false).await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_explicitly_off_flag_stays_off() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        unset_env(&env_flag_var_name(RUST_DATA_PLANE_FLAG));
        let client = seeded_client(r#"{"waddles.core.rust-data-plane":false}"#, true).await;
        assert!(client.bypass_active());
        assert_eq!(
            posthog_flag_value(&client, RUST_DATA_PLANE_FLAG).await,
            Some(false)
        );
        assert!(!rust_data_plane_enabled(Some(&client)).await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_license_check_passes() {
        crate::crypto::ensure_installed();
        let client = seeded_client("{}", true).await;
        assert!(client.bypass_active());
        assert!(client.check_feature("any-licensed-feature").await);
        assert!(client.check_tier(penguin_licensing::Tier::Enterprise).await);
    }

    // regression: bypass domains are license-only; kill switch ON-by-absence under bypass
    #[tokio::test]
    async fn bypass_client_kill_switch_follows_posthog_not_bypass() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        unset_env(command_latency::switch::ENV_BASELINE);
        let unset = seeded_client("{}", true).await;
        assert!(unset.bypass_active());
        assert!(
            !crate::canary::kill_switch_wanted(Some(&unset)).await,
            "unset kill switch under bypass -> telemetry stays ON"
        );
        let on = seeded_client(
            r#"{"waddles.disable-command-latency-telemetry":true}"#,
            true,
        )
        .await;
        assert!(
            crate::canary::kill_switch_wanted(Some(&on)).await,
            "an explicit ON kill switch opts out even under bypass"
        );
        let off = seeded_client(
            r#"{"waddles.disable-command-latency-telemetry":false}"#,
            true,
        )
        .await;
        assert!(!crate::canary::kill_switch_wanted(Some(&off)).await);
    }

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
        // Reads the FLAG_* env baseline: serialize against the env-mutating tests.
        let _guard = ENV_LOCK.lock().unwrap();
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

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn rust_data_plane_flag_stays_off_under_domain_bypass() {
        crate::crypto::ensure_installed();
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var(env_flag_var_name(RUST_DATA_PLANE_FLAG)) };
        // Bypass is LICENSE-only: tier/entitlement are granted, a feature flag is not forced.
        let cfg = penguin_licensing::LicenseConfig::new("test-product")
            .expect("default config is always valid")
            .with_deployment_domain("svc-ingest.penguintech.cloud");
        let client = penguin_licensing::LicenseClient::new(cfg).expect("client construction");
        assert!(client.bypass_active());
        assert!(
            !rust_data_plane_enabled(Some(&client)).await,
            "unset flag is OFF on bypass"
        );
        assert!(client.check_feature("any-licensed-feature").await);
        assert!(client.check_tier(penguin_licensing::Tier::Enterprise).await);
        // Explicitly-off baseline stays off; explicit-on baseline is honored (not forced).
        unsafe { std::env::set_var(env_flag_var_name(RUST_DATA_PLANE_FLAG), "false") };
        assert!(!rust_data_plane_enabled(Some(&client)).await);
        unsafe { std::env::set_var(env_flag_var_name(RUST_DATA_PLANE_FLAG), "true") };
        assert!(rust_data_plane_enabled(Some(&client)).await);
        unsafe { std::env::remove_var(env_flag_var_name(RUST_DATA_PLANE_FLAG)) };
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
    async fn build_license_client_hardcoded_domain_bypasses_license_checks_only() {
        // regression: bypass domains are license-only (2026-11-12 decision)
        crate::crypto::ensure_installed();
        let client = {
            let _guard = ENV_LOCK.lock().unwrap();
            clear_license_env();
            // SAFETY: serialized by ENV_LOCK.
            unsafe { std::env::remove_var(env_flag_var_name(RUST_DATA_PLANE_FLAG)) };
            let client = build_license_client().expect("valid defaults");
            clear_license_env();
            client
        };
        assert!(client.bypass_active());
        assert!(client.check_feature("any-licensed-feature").await);
        assert!(!rust_data_plane_enabled(Some(&client)).await);
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

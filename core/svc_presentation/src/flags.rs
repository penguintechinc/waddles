//! PostHog flag gates for this service's features (image upload P6/P9, the
//! caption overlay) -- `rules/general.md` Red Flags: "Feature merged without
//! a PostHog feature flag wrapping it". Mirrors `core/svc_action/src/flags.rs`'s plain opt-in
//! `LicenseFlag` shape (not the kill-switch inversion that module's other
//! flags use -- this is a brand-new capability, not toggling off an
//! already-default-on path), trimmed to just what this crate needs.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

/// Gates `crate::images::upload::upload_image` (P6). OFF (the default for
/// a never-seen flag, per `rules/critical-rules.md` Feature Flags & License
/// Tiers) ⇒ the upload route returns 403 rather than ever touching the
/// object store or `overlay_images` table.
pub const IMAGE_UPLOAD_FLAG: &str = "waddles.core.overlay-image-upload";

/// Gates every caption route in `crate::http::captions` (the OBS page, the
/// websocket, and the PUSH-guarded ingest). Same OFF-by-default semantics as
/// [`IMAGE_UPLOAD_FLAG`]: a never-seen flag leaves the Rust caption path
/// off, so the Python `browser_source_core_module` caption path stays the
/// live one until this is deliberately switched on at the parity cutover.
pub const CAPTIONS_FLAG: &str = "waddles.core.overlay-captions";

/// One flag's live enabled/disabled state -- object-safe (boxed future) so
/// callers can hold `Arc<dyn FeatureFlag>`, same shape
/// `core/svc_action::flags::FeatureFlag` already establishes.
pub trait FeatureFlag: Send + Sync {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

/// Derives a flag key's Docker ENV baseline variable: `FLAG_` + key uppercased, `.`/`-` -> `_`.
fn env_flag_var_name(key: &str) -> String {
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

/// Parses `key`'s `FLAG_*` baseline through an injected lookup, so tests never mutate the
/// process-global environment.
fn env_flag_value_from(key: &str, lookup: &dyn Fn(&str) -> Option<String>) -> Option<bool> {
    let raw = lookup(&env_flag_var_name(key))?;
    match raw.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "on" | "yes" => Some(true),
        "false" | "0" | "off" | "no" => Some(false),
        _ => None,
    }
}

/// Feature-flag evaluation over a license client: PostHog snapshot, then the `FLAG_*` ENV
/// baseline, then OFF. The license-bypass domain answers LICENSE/entitlement checks only
/// (`tier`/`check_feature`) and never forces a feature flag (user decision 2026-11-12). The
/// library's `flag_enabled` evaluates the PostHog snapshot normally (penguin-libs ad096f5d) but
/// has no ENV baseline, so this resolver supplies it.
pub trait PosthogFlag {
    /// Resolves `key`: PostHog snapshot, else ENV baseline, else `false`.
    fn flag_on<'a>(&'a self, key: &'a str) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

impl PosthogFlag for Arc<penguin_licensing::LicenseClient> {
    fn flag_on<'a>(&'a self, key: &'a str) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(resolve_flag(self, key, &|name| std::env::var(name).ok()))
    }
}

/// Resolves `key` for `client`: PostHog snapshot, else the baseline from `lookup`, else OFF.
/// Production passes the process env; tests pass a map.
async fn resolve_flag(
    client: &Arc<penguin_licensing::LicenseClient>,
    key: &str,
    lookup: &(dyn Fn(&str) -> Option<String> + Sync),
) -> bool {
    // Side effect only: `validate` schedules a background refresh when the snapshot is
    // stale or missing, on bypass domains too.
    let _ = client.validate().await;
    client
        .snapshot()
        .and_then(|snap| snap.flags.get(key).copied())
        .or_else(|| env_flag_value_from(key, lookup))
        .unwrap_or(false)
}

/// The real [`FeatureFlag`]: `key` resolved via a live
/// `penguin_licensing::LicenseClient` (cheap `Arc` clone).
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
        Box::pin(self.client.flag_on(self.key))
    }
}

/// A [`FeatureFlag`] that never varies -- this crate's test fixture, and
/// production's fallback when [`build_license_client`] itself returns
/// `None` (fails closed to OFF, never panics or silently enables the
/// feature).
pub struct StaticFlag(pub bool);

impl FeatureFlag for StaticFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        let value = self.0;
        Box::pin(async move { value })
    }
}

/// Type-erased convenience, mirroring `core/svc_action::flags::boxed`.
pub fn boxed(flag: impl FeatureFlag + 'static) -> Arc<dyn FeatureFlag> {
    Arc::new(flag)
}

/// `waddles` is the shared product name every Rust data-plane service
/// registers flags/license entitlements under (`core/svc_action`,
/// `core/svc_process`, `core/svc_ingest` all use the identical literal).
const LICENSE_PRODUCT: &str = "waddles";

/// PenguinTech/Waddles-owned bypass suffix -- hardcoded, never an env var/
/// CLI flag/Helm value (`rules/critical-rules.md` Feature Flags & License
/// Tiers: "bypass is domain-based ONLY"). Identical literal to
/// `core/svc_action::BYPASS_DOMAIN` -- all Rust data-plane services
/// register the same apex suffix.
const BYPASS_DOMAIN: &str = "waddles.app";

/// This service's own deployment domain -- distinct per service so each
/// binary's bypass is traceable to the service that claimed it, same
/// `*.waddles.app` convention as `core/svc_action`/`core/svc_process`.
const DEPLOYMENT_DOMAIN: &str = "svc-presentation.waddles.app";

/// Builds the shared `penguin_licensing::LicenseClient` [`IMAGE_UPLOAD_FLAG`]
/// resolves against, from the standard `LICENSE_KEY`/`LICENSE_SERVER_URL`/
/// `POSTHOG_HOST`/`POSTHOG_KEY` environment variables plus the hardcoded
/// [`DEPLOYMENT_DOMAIN`]/[`BYPASS_DOMAIN`] bypass. `None` only if even the
/// no-network-required default `LicenseConfig` fails to build -- not
/// reachable in practice, handled rather than unwrapped; [`image_upload_flag`]
/// falls back to [`StaticFlag`]`(false)` (fail-closed-to-OFF) in that case.
pub fn build_license_client() -> Option<Arc<penguin_licensing::LicenseClient>> {
    let cfg = match penguin_licensing::LicenseConfig::from_env(LICENSE_PRODUCT) {
        Ok(cfg) => cfg,
        Err(err) => {
            tracing::error!(
                error = %err,
                "LICENSE_SERVER_URL/POSTHOG_HOST invalid; falling back to defaults \
                 (IMAGE_UPLOAD_FLAG defaults OFF until a valid config is set)"
            );
            match penguin_licensing::LicenseConfig::new(LICENSE_PRODUCT) {
                Ok(cfg) => cfg,
                Err(err) => {
                    tracing::error!(
                        error = %err,
                        "LicenseConfig::new failed unexpectedly; image-upload flag gating disabled"
                    );
                    return None;
                }
            }
        }
    };
    let cfg = cfg
        .with_bypass_domain(BYPASS_DOMAIN)
        .with_deployment_domain(DEPLOYMENT_DOMAIN);
    match penguin_licensing::LicenseClient::new(cfg) {
        Ok(client) => Some(client),
        Err(err) => {
            tracing::error!(error = %err, "LicenseClient::new failed; image-upload flag gating disabled");
            None
        }
    }
}

/// Builds the [`FeatureFlag`] for `key`: a live [`LicenseFlag`] over
/// `license` when available, or a fixed "disabled" answer otherwise -- the
/// single place the fail-closed-to-OFF fallback is applied.
fn flag_for(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
    key: &'static str,
) -> Arc<dyn FeatureFlag> {
    match license {
        Some(client) => boxed(LicenseFlag::new(Arc::clone(client), key)),
        None => boxed(StaticFlag(false)),
    }
}

/// The [`FeatureFlag`] `crate::images::upload::upload_image` gates on
/// ([`IMAGE_UPLOAD_FLAG`]).
pub fn image_upload_flag(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
) -> Arc<dyn FeatureFlag> {
    flag_for(license, IMAGE_UPLOAD_FLAG)
}

/// The [`FeatureFlag`] every `crate::http::captions` route gates on
/// ([`CAPTIONS_FLAG`]).
pub fn captions_flag(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
) -> Arc<dyn FeatureFlag> {
    flag_for(license, CAPTIONS_FLAG)
}

#[cfg(test)]
mod tests {
    use super::*;

    include!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../test_support/licensing_mock.rs"
    ));

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_unset_flag_is_off() {
        let client = seeded_client(r#"{"waddles.other":true}"#, true).await;
        assert!(client.bypass_active());
        let flag = image_upload_flag(&Some(client));
        assert!(!flag.enabled().await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_explicitly_off_flag_stays_off() {
        let client = seeded_client(r#"{"waddles.core.overlay-captions":false}"#, true).await;
        assert!(client.bypass_active());
        let flag = captions_flag(&Some(client));
        assert!(!flag.enabled().await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_license_check_passes() {
        let client = seeded_client("{}", true).await;
        assert!(client.bypass_active());
        assert!(client.check_feature("any-licensed-feature").await);
        assert!(client.check_tier(penguin_licensing::Tier::Enterprise).await);
    }

    // regression: env must not beat an explicit PostHog OFF (3b)
    // The baseline is injected through a lookup; the process env is never mutated.
    #[tokio::test]
    async fn explicit_posthog_off_beats_env_baseline_true() {
        let var = env_flag_var_name(CAPTIONS_FLAG);
        let lookup = |name: &str| (name == var).then(|| "true".to_string());
        let client = seeded_client(r#"{"waddles.core.overlay-captions":false}"#, false).await;
        let off = resolve_flag(&client, CAPTIONS_FLAG, &lookup).await;
        let unseen_client = seeded_client("{}", false).await;
        let unseen = resolve_flag(&unseen_client, CAPTIONS_FLAG, &lookup).await;
        assert!(!off, "explicit PostHog OFF beats FLAG_*=true");
        assert!(unseen, "an unseen key falls to the ENV baseline");
    }

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

    #[test]
    fn build_license_client_succeeds_with_no_license_env_vars_set() {
        assert!(build_license_client().is_some());
    }

    #[tokio::test]
    async fn image_upload_flag_defaults_disabled_when_no_license_client_is_available() {
        let flag = image_upload_flag(&None);
        assert!(!flag.enabled().await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn image_upload_flag_on_a_bypassed_client_stays_off_and_license_passes() {
        let client = build_license_client().expect("defaults always build a client");
        assert!(
            client.bypass_active(),
            "sanity check: build_license_client's own hardcoded domain must bypass"
        );
        assert!(client.check_feature("any-licensed-feature").await);
        assert!(client.check_tier(penguin_licensing::Tier::Enterprise).await);
        let flag = image_upload_flag(&Some(client));
        assert!(
            !flag.enabled().await,
            "a bypassed client must NOT force a never-seen feature flag ON"
        );
    }

    #[tokio::test]
    async fn license_flag_reaches_a_real_cold_client_and_fails_closed_to_off() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = LicenseFlag::new(client, IMAGE_UPLOAD_FLAG);
        assert!(!flag.enabled().await);
    }

    #[test]
    fn image_upload_flag_key_matches_the_product_flag_key_convention() {
        assert_eq!(IMAGE_UPLOAD_FLAG, "waddles.core.overlay-image-upload");
    }

    #[test]
    fn captions_flag_key_matches_the_product_flag_key_convention() {
        assert_eq!(CAPTIONS_FLAG, "waddles.core.overlay-captions");
    }

    #[tokio::test]
    async fn captions_flag_defaults_disabled_when_no_license_client_is_available() {
        let flag = captions_flag(&None);
        assert!(!flag.enabled().await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn captions_flag_on_a_bypassed_client_stays_off_until_baseline_set() {
        let client = build_license_client().expect("defaults always build a client");
        let flag = captions_flag(&Some(client));
        assert!(!flag.enabled().await);
    }

    #[tokio::test]
    async fn captions_flag_fails_closed_to_off_on_a_cold_client() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = captions_flag(&Some(client));
        assert!(!flag.enabled().await);
    }
}

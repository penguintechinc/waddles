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

/// Opt-out kill-switch for the DB-driven active-bundle loader
/// (`crate::bundle_loader`) -- **inverted from the retired
/// `waddles.core.db-bundle-config` flag it replaces** (user decision,
/// 2026-09-27, mirrors `core/svc_process/src/license.rs`'s identical
/// `DISABLE_DB_BUNDLE_CONFIG_FLAG`/`DbBundleConfigGate` inversion). The
/// retired flag's semantics were "ON enables the DB-driven path"
/// (opt-in, default OFF); this flag's semantics are the opposite: the
/// DB-driven path is the default, and this raw flag being ON is what opts
/// back OUT of it.
///
/// Unlike `core/svc_process`, this crate has no dedicated `FeatureGate`
/// wrapper type per flag -- callers build the *negated* `FeatureFlag` via
/// [`NegatedFlag`] instead of reading this flag's raw value directly, so
/// `crate::bundle_loader::run_tick`'s `flag.enabled()` still asks "is the
/// DB-driven path enabled?", not "is the kill-switch flag raw-ON?". Never
/// seen / license server unreachable ⇒ `LicenseFlag`'s own fail-closed
/// `false` negates to `true` (DB-driven path enabled, the default); the
/// legacy `ACTION_BUNDLE_*` env override (`crate::try_start_env_bundle_
/// loader`) only ever starts when `crate::resolve_db_path_active` finds
/// this negated value `false` (kill-switch raw-ON) or the DB loader's own
/// `DB_READER_*`/`BUNDLE_SCOPE_TENANT_ID` prerequisites unset -- mutual
/// exclusion, `crate::lib`'s top doc.
pub const DISABLE_DB_BUNDLE_CONFIG_FLAG: &str = "waddles.core.disable-db-bundle-config";

/// Gates the multi-tenant per-tenant-bot credential resolution path
/// (`crate::credentials`, #500 data-plane half) -- same key as
/// `core/svc_ingest/src/license.rs::GUILD_PAIRING_FLAG`, checked
/// independently by each service. OFF preserves today's single
/// platform-bot-for-everyone send path unchanged; ON routes sends through
/// `crate::credentials::resolve_credentials` (tenant 0 -> platform bot,
/// every other tenant -> its own app, fail-closed, no fallback).
pub const GUILD_PAIRING_FLAG: &str = "waddles.guild-pairing";

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

/// Wraps another [`FeatureFlag`] and reports the boolean negation of its
/// current value -- a general-purpose adapter for a plain opt-out
/// kill-switch flag (ON = disabled). **Not used for
/// [`DISABLE_DB_BUNDLE_CONFIG_FLAG`]** -- see [`DisableDbBundleConfigFlag`]'s
/// own doc for why a bare negation is wrong for a flag whose raw value can
/// be forced `true` by this crate's own hardcoded license-bypass domain.
pub struct NegatedFlag(pub Arc<dyn FeatureFlag>);

impl FeatureFlag for NegatedFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move { !self.0.enabled().await })
    }
}

/// Production [`FeatureFlag`] for [`DISABLE_DB_BUNDLE_CONFIG_FLAG`] --
/// checks `LicenseClient::bypass_active()` directly rather than going
/// through the generic [`NegatedFlag`] wrapper, mirroring
/// `core/svc_process/src/license.rs::DbBundleConfigGate`'s identical fix.
///
/// **Bypass-awareness fix:** `crate::lib::build_license_client` always
/// registers this service's own hardcoded deployment domain against its
/// bypass suffix, so `bypass_active()` is `true` for every deployment of
/// this service -- and a bypassed client's `flag_enabled` reads `true` for
/// ANY key, since bypass means "this PenguinTech-owned deployment gets
/// every feature unlocked". Naively negating that raw `true` for this
/// OPT-OUT kill-switch would read as "bypass -> kill-switch raw-ON -> DB
/// path permanently DISABLED" -- the exact opposite of what bypass is
/// supposed to mean. [`enabled`] checks bypass first and short-circuits to
/// `true` (DB path enabled, the correct "unlocked" outcome).
pub struct DisableDbBundleConfigFlag(Arc<penguin_licensing::LicenseClient>);

impl DisableDbBundleConfigFlag {
    pub fn new(client: Arc<penguin_licensing::LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureFlag for DisableDbBundleConfigFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            if self.0.bypass_active() {
                return true;
            }
            !self.0.flag_enabled(DISABLE_DB_BUNDLE_CONFIG_FLAG).await
        })
    }
}

/// Builds the [`FeatureFlag`] `crate::lib::try_start_changelog_consumer`
/// (and the startup path-selection dispatch) gates on: [`DisableDbBundleConfigFlag`]
/// over a real client, or a fixed "DB path enabled" answer when no license
/// client is available at all (mirrors `crate::lib::flag_or_closed`'s own
/// `None` branch, just with the final, already-inverted boolean this
/// specific flag's callers expect).
pub fn db_bundle_config_flag(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
) -> Arc<dyn FeatureFlag> {
    match license {
        Some(client) => boxed(DisableDbBundleConfigFlag::new(Arc::clone(client))),
        None => boxed(StaticFlag(true)),
    }
}

/// Opt-out kill-switch for the multi-tenant, change-log-driven active-set
/// loader (`crate::changelog_consumer`) -- dataplane scale design rev 4,
/// §8 step 2: "Multi-tenant watermark polling ...
/// waddles.core.disable-multi-tenant-watermark". Same inversion convention
/// as [`DISABLE_DB_BUNDLE_CONFIG_FLAG`]: unseen/OFF/license-server-
/// unreachable means the multi-tenant path is ENABLED (the default, and
/// the user's own hard requirement -- "every svc_process/svc_action pod
/// serves ALL tenants"); ON opts back OUT of it, falling back to the
/// existing `ACTION_APP_ID`/`ACTION_BUNDLE_*` env selection and the
/// `crate::distribution` catalog poll -- there is no remaining
/// single-tenant DB-driven path to fall back to (`BUNDLE_SCOPE_TENANT_ID`/
/// `BUNDLE_SCOPE_COMMUNITY_ID` were removed in this same change).
pub const DISABLE_MULTI_TENANT_WATERMARK_FLAG: &str = "waddles.core.disable-multi-tenant-watermark";

/// Production [`FeatureFlag`] for [`DISABLE_MULTI_TENANT_WATERMARK_FLAG`] --
/// same bypass-aware negation shape as [`DisableDbBundleConfigFlag`] (see
/// that type's own doc for the full bypass-awareness rationale, identical
/// here).
pub struct DisableMultiTenantWatermarkFlag(Arc<penguin_licensing::LicenseClient>);

impl DisableMultiTenantWatermarkFlag {
    pub fn new(client: Arc<penguin_licensing::LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureFlag for DisableMultiTenantWatermarkFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            if self.0.bypass_active() {
                return true;
            }
            !self
                .0
                .flag_enabled(DISABLE_MULTI_TENANT_WATERMARK_FLAG)
                .await
        })
    }
}

/// Builds the combined "is the multi-tenant changelog-consumer path
/// enabled?" [`FeatureFlag`] -- `crate::flags::db_bundle_config_flag`'s
/// answer AND [`DisableMultiTenantWatermarkFlag`]'s answer, both already
/// negated (`true` = enabled). Mirrors `core/svc_process::license::AllGate`.
pub fn multi_tenant_watermark_flag(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
) -> Arc<dyn FeatureFlag> {
    match license {
        Some(client) => boxed(DisableMultiTenantWatermarkFlag::new(Arc::clone(client))),
        None => boxed(StaticFlag(true)),
    }
}

/// Combines multiple [`FeatureFlag`]s with logical AND, short-circuiting on
/// the first `false`.
pub struct AllFlags(pub Vec<Arc<dyn FeatureFlag>>);

impl FeatureFlag for AllFlags {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            for flag in &self.0 {
                if !flag.enabled().await {
                    return false;
                }
            }
            true
        })
    }
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

    #[tokio::test]
    async fn negated_flag_reports_the_opposite_of_the_wrapped_flag() {
        assert!(!NegatedFlag(boxed(StaticFlag(true))).enabled().await);
        assert!(NegatedFlag(boxed(StaticFlag(false))).enabled().await);
    }

    /// The kill-switch inversion's core regression test: a never-seen
    /// `DISABLE_DB_BUNDLE_CONFIG_FLAG` (every fresh deployment's starting
    /// state, and a permanently-unreachable license server's steady state)
    /// must leave the DB-driven path ENABLED, not disabled -- the opposite
    /// of the retired `waddles.core.db-bundle-config` flag's own
    /// default-OFF contract. Built via a non-bypassed client
    /// (`LicenseConfig::new` directly, no deployment-domain bypass) so this
    /// isolates the underlying fail-closed contract from the bypass fix
    /// below.
    #[tokio::test]
    async fn disable_db_bundle_config_flag_defaults_enabled_when_never_seen() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-kill-switch-default")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = DisableDbBundleConfigFlag::new(client);
        assert!(
            flag.enabled().await,
            "an unseen kill-switch flag must leave the DB-driven path enabled"
        );
    }

    /// Bypass-awareness regression test: `crate::lib::build_license_client`'s
    /// hardcoded self-domain bypass makes `flag_enabled` read `true` for
    /// ANY key, `DISABLE_DB_BUNDLE_CONFIG_FLAG` included -- naively negating
    /// that raw `true` (what the generic [`NegatedFlag`] would do) would
    /// report the DB-driven path DISABLED for every deployment of this
    /// service, permanently. [`DisableDbBundleConfigFlag::enabled`] must
    /// check `bypass_active()` first and report `true` instead.
    #[tokio::test]
    async fn disable_db_bundle_config_flag_stays_enabled_under_a_bypassed_client() {
        // Self-contained apex-domain bypass (mirrors `crate::lib`'s own
        // `build_license_client_hardcoded_domain_bypasses_flag_checks`
        // "apex" case) rather than reaching into that module's private
        // `build_license_client`/`BYPASS_DOMAIN`/`DEPLOYMENT_DOMAIN` --
        // this test only needs *a* bypassed client, not this crate's exact
        // production domain values.
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-flags-bypass")
            .expect("default LicenseConfig::new never fails")
            .with_bypass_domain("waddles.app")
            .with_deployment_domain("waddles.app");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid config never fails");
        assert!(
            client.bypass_active(),
            "sanity check: this client must actually be bypassed"
        );
        let flag = DisableDbBundleConfigFlag::new(client);
        assert!(
            flag.enabled().await,
            "bypass must leave the DB-driven path enabled, not disabled"
        );
    }

    #[tokio::test]
    async fn db_bundle_config_flag_defaults_enabled_when_no_license_client_is_available() {
        let flag = db_bundle_config_flag(&None);
        assert!(flag.enabled().await);
    }

    #[test]
    fn disable_multi_tenant_watermark_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            DISABLE_MULTI_TENANT_WATERMARK_FLAG,
            "waddles.core.disable-multi-tenant-watermark"
        );
    }

    #[tokio::test]
    async fn multi_tenant_watermark_flag_defaults_enabled_when_no_license_client_is_available() {
        let flag = multi_tenant_watermark_flag(&None);
        assert!(flag.enabled().await);
    }

    #[tokio::test]
    async fn disable_multi_tenant_watermark_flag_defaults_enabled_when_never_seen() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-multi-tenant-default")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = DisableMultiTenantWatermarkFlag::new(client);
        assert!(
            flag.enabled().await,
            "an unseen kill-switch flag must leave the multi-tenant path enabled"
        );
    }

    #[tokio::test]
    async fn all_flags_is_enabled_only_when_every_wrapped_flag_is_enabled() {
        let flags = AllFlags(vec![boxed(StaticFlag(true)), boxed(StaticFlag(true))]);
        assert!(flags.enabled().await);
    }

    #[tokio::test]
    async fn all_flags_is_disabled_when_any_wrapped_flag_is_disabled() {
        let flags = AllFlags(vec![boxed(StaticFlag(true)), boxed(StaticFlag(false))]);
        assert!(!flags.enabled().await);
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

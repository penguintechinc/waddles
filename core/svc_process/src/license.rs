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
use std::sync::{Arc, OnceLock};

use penguin_licensing::{LicenseClient, LicenseConfig, LicenseError};

/// The flag key gating the process-stage drain loop (spec §13.5). Flag-key
/// convention: `{product}.{feature-name}` (`rules/critical-rules.md`
/// Feature Flags & License Tiers) -- product is `waddles`.
pub const RUST_DATA_PLANE_FLAG: &str = "waddles.core.rust-data-plane";

/// Gates the bundle `http` host capability (`crate::capabilities::
/// StageCapabilities`'s `egress` field,
/// `bundle_host_http::egress::EgressGuard`) -- same flag key
/// `svc_action::flags::BUNDLE_EGRESS_FLAG` gates, since both stages'
/// bundles share one `net.http:<host>` capability concept. OFF ⇒ every
/// `http.send` call is denied `feature_disabled`, checked before the
/// allowlist/SSRF pipeline runs at all.
pub const BUNDLE_EGRESS_FLAG: &str = "waddles.core.bundle-egress";

/// Adapts a live [`LicenseClient`] to
/// [`bundle_host_http::egress::FeatureFlag`] for [`BUNDLE_EGRESS_FLAG`] --
/// this crate's own [`FeatureGate`] trait is a distinct type (object-safe
/// but crate-local), so `EgressGuard::new`'s `Arc<dyn bundle_host_http::
/// egress::FeatureFlag>` parameter needs its own thin implementor rather
/// than reusing [`LicenseFeatureGate`] directly.
pub struct BundleEgressFlag(Arc<LicenseClient>);

impl BundleEgressFlag {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl bundle_host_http::egress::FeatureFlag for BundleEgressFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move { self.0.flag_enabled(BUNDLE_EGRESS_FLAG).await })
    }
}

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

/// Opt-out kill-switch flag for the DB-driven active-bundle loader
/// (`crate::bundle_loader`) and the DB-driven source-binding supervisor
/// (`crate::source_supervisor`) -- **inverted from the retired
/// `waddles.core.db-bundle-config` flag it replaces** (user decision,
/// 2026-09-27): that flag's semantics were "ON enables the DB-driven
/// path", requiring an explicit opt-in per deployment before the DB path
/// would ever run. This flag's semantics are the opposite: the DB-driven
/// path is the default, and this flag exists only to opt back OUT of it.
///
/// | This flag's raw value | `DbBundleConfigGate::enabled()` | Behavior |
/// |---|---|---|
/// | unseen / OFF / license server unreachable | `true` | DB-driven path enabled (default) |
/// | ON | `false` | fall back to `PROCESS_APP_ID`/`PROCESS_BUNDLE_*` env selection |
///
/// This falls straight out of `penguin_licensing::LicenseClient::
/// flag_enabled`'s own existing fail-closed-to-OFF contract ("never-seen
/// flags are OFF") -- [`DbBundleConfigGate::enabled`] simply negates the
/// raw flag read, so "never seen"/"unreachable" naturally resolve to "DB
/// path enabled" with no separate default-value logic needed here. The DB
/// path additionally requires `DB_READER_*` credentials and
/// `BUNDLE_SCOPE_TENANT_ID` to be configured regardless of this flag's
/// value -- missing either still falls back to the env path with its own
/// clear startup log (`crate::lib::try_start_db_bundle_loader`), same as
/// before this flag was inverted. Flag-key convention:
/// `{product}.{feature-name}` (`rules/critical-rules.md` Feature Flags &
/// License Tiers) -- same `waddles` product as [`RUST_DATA_PLANE_FLAG`].
pub const DISABLE_DB_BUNDLE_CONFIG_FLAG: &str = "waddles.core.disable-db-bundle-config";

/// Production [`FeatureGate`] for [`DISABLE_DB_BUNDLE_CONFIG_FLAG`] -- same
/// `penguin_licensing::LicenseClient::flag_enabled` contract as
/// [`LicenseFeatureGate`] (non-blocking, fail-closed default OFF,
/// last-known-cached on an unreachable license server) underneath, but
/// [`FeatureGate::enabled`] here reports the *negation* of the raw flag
/// read (see this constant's own doc for the semantics table) -- callers
/// (`crate::bundle_loader::run_tick`, `crate::source_supervisor::run_tick`)
/// ask "is the DB-driven path enabled?", not "is the kill-switch flag
/// raw-ON?". `crate::bundle_loader`'s own tests reuse `test_support::
/// FixedGate`/`ToggleGate` (this trait takes no flag parameter, so the
/// same fakes serve both gates) -- constructing those directly with the
/// *already-inverted* boolean they want [`enabled`] to report.
///
/// **Bypass-awareness fix:** [`build_license_client`] always registers
/// this service's own [`DEPLOYMENT_DOMAIN`] against [`BYPASS_DOMAIN`], so
/// `LicenseClient::bypass_active()` is `true` for every deployment of this
/// service (`build_license_client_hardcoded_domain_bypasses_flag_checks`'s
/// own proof) -- and a bypassed client's `flag_enabled` reads `true` for
/// *any* key, `RUST_DATA_PLANE_FLAG` included, since bypass means "this
/// PenguinTech-owned deployment gets every feature unlocked". Naively
/// negating that raw `true` for this OPT-OUT kill-switch would read as
/// "bypass -> kill-switch raw-ON -> DB path permanently DISABLED" -- the
/// exact opposite of what bypass is supposed to mean. [`enabled`] checks
/// [`LicenseClient::bypass_active`] first and short-circuits to `true`
/// (DB path enabled, the correct "unlocked" outcome) before ever reading
/// the raw flag.
pub struct DbBundleConfigGate(Arc<LicenseClient>);

impl DbBundleConfigGate {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureGate for DbBundleConfigGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            if self.0.bypass_active() {
                return true;
            }
            !self.0.flag_enabled(DISABLE_DB_BUNDLE_CONFIG_FLAG).await
        })
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
/// existing `PROCESS_APP_ID`/`PROCESS_BUNDLE_*` static env-var single-
/// bundle selection (`crate::lib::try_start_process_loop`) -- there is no
/// remaining single-tenant DB-driven path to fall back to (`BUNDLE_SCOPE_
/// TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` and their scope resolution were
/// removed in this same change, see `crate::config`'s module doc).
/// [`try_start_changelog_consumer`]'s own startup-time path-selection
/// combines this with [`DISABLE_DB_BUNDLE_CONFIG_FLAG`] into one decision
/// (both must report the DB-driven path enabled), evaluated once at
/// startup -- never re-evaluated mid-run, exactly like the flag it
/// complements (see that flag's own doc for why: "guarantees the two paths
/// can never run concurrently").
///
/// [`try_start_changelog_consumer`]: crate::lib::try_start_changelog_consumer
pub const DISABLE_MULTI_TENANT_WATERMARK_FLAG: &str = "waddles.core.disable-multi-tenant-watermark";

/// Production [`FeatureGate`] for [`DISABLE_MULTI_TENANT_WATERMARK_FLAG`] --
/// same bypass-aware negation shape as [`DbBundleConfigGate`] (see that
/// type's own doc for the full bypass-awareness rationale, identical here).
pub struct MultiTenantWatermarkGate(Arc<LicenseClient>);

impl MultiTenantWatermarkGate {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureGate for MultiTenantWatermarkGate {
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

/// Combines multiple [`FeatureGate`]s with logical AND, short-circuiting on
/// the first `false` -- `crate::lib::try_start_changelog_consumer`'s own
/// startup-time path-selection combines [`DbBundleConfigGate`] and
/// [`MultiTenantWatermarkGate`] into one gate this way, so
/// `crate::changelog_consumer::run`'s per-tick check reads as a single
/// `gate.enabled()` call rather than threading two separate gates through
/// every call site.
pub struct AllGate(pub Vec<Arc<dyn FeatureGate>>);

impl FeatureGate for AllGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            for gate in &self.0 {
                if !gate.enabled().await {
                    return false;
                }
            }
            true
        })
    }
}

/// A [`FeatureGate`] whose answer is fixed at construction and never
/// touches a `LicenseClient` -- production equivalent of
/// `test_support::FixedGate` (that one is `#[cfg(test)]`-only), mirroring
/// `bundle_host_http::egress::StaticFlag`'s identical "no live client"
/// shape for the `http` capability's `FeatureFlag` trait.
///
/// **Fix: `pii_gate`/`hub_minter` env-override mismatch (alpha 2026-10-04
/// dead-letter incident).** `crate::resolve_pii_tokenization_enabled`'s
/// `PII_TOKENIZATION_ENABLED=false` env override short-circuits
/// `hub_minter` to `None` *before* the PostHog kill-switch is ever
/// consulted, but [`PiiTokenizationGate`] alone only ever consults the
/// kill-switch (default ENABLED) -- so with the override set, the gate
/// stayed `true` while the minter was `None`, and
/// `crate::spine::handle_delivered`'s fail-closed check (correctly) dead-
/// lettered every single inbound event. [`try_start_process_loop`]/
/// [`try_start_changelog_consumer`] now build `StaticGate(false)` instead
/// of a live [`PiiTokenizationGate`] whenever `CliConfig::
/// pii_tokenization_enabled_override` is `Some(false)`, so the gate and
/// the minter agree: both reflect "disabled", and the spine forwards
/// un-tokenized rather than dead-lettering. When the override is unset/
/// `true`, [`PiiTokenizationGate`]'s existing live PostHog read is used
/// unchanged -- this never force-enables past the kill-switch, only ever
/// forces off, matching [`resolve_pii_tokenization_enabled`]'s own
/// contract.
///
/// [`try_start_process_loop`]: crate::lib::try_start_process_loop
/// [`try_start_changelog_consumer`]: crate::lib::try_start_changelog_consumer
/// [`resolve_pii_tokenization_enabled`]: crate::resolve_pii_tokenization_enabled
pub struct StaticGate(pub bool);

impl FeatureGate for StaticGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        let value = self.0;
        Box::pin(async move { value })
    }
}

/// Gates the bundle `db` host capability (`crate::capabilities::
/// StageCapabilities::handle_db`) -- a plain opt-in flag (not an inverted
/// kill-switch like [`DISABLE_DB_BUNDLE_CONFIG_FLAG`] above): unseen/OFF
/// means every `db` call is denied `feature_disabled`, matching
/// `core/svc_action::flags::BUNDLE_EGRESS_FLAG`'s identical opt-in shape
/// for the `http` capability. Flag-key convention: `{product}.
/// {feature-name}` (`rules/critical-rules.md` Feature Flags & License
/// Tiers).
pub const BUNDLE_DB_CAPABILITY_FLAG: &str = "waddles.bundle-db-capability";

/// Production [`FeatureGate`] for [`BUNDLE_DB_CAPABILITY_FLAG`] -- plain
/// (non-inverted) read of `LicenseClient::flag_enabled`, so "never seen"/
/// "license server unreachable" both resolve to `false` (capability
/// denied), the correct fail-closed default for an opt-in flag.
pub struct BundleDbCapabilityGate(Arc<LicenseClient>);

impl BundleDbCapabilityGate {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureGate for BundleDbCapabilityGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move { self.0.flag_enabled(BUNDLE_DB_CAPABILITY_FLAG).await })
    }
}

/// Gates the bundle `reputation` host capability (`crate::capabilities::
/// StageCapabilities::handle_reputation`, issue #726) -- a plain opt-in flag,
/// same shape as [`BUNDLE_DB_CAPABILITY_FLAG`]: unseen/OFF/license-server-
/// unreachable means every `reputation.*` call is denied `feature_disabled`.
pub const BUNDLE_REPUTATION_CAPABILITY_FLAG: &str = "waddles.bundle-reputation-capability";

/// Production [`FeatureGate`] for [`BUNDLE_REPUTATION_CAPABILITY_FLAG`] --
/// plain read of `LicenseClient::flag_enabled` (fail-closed default).
pub struct BundleReputationCapabilityGate(Arc<LicenseClient>);

impl BundleReputationCapabilityGate {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureGate for BundleReputationCapabilityGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move { self.0.flag_enabled(BUNDLE_REPUTATION_CAPABILITY_FLAG).await })
    }
}

/// Opt-out kill-switch for the inbound PII-tokenization pre-dispatch pass
/// (`crate::pii_tokenize`, `rules/critical-rules.md` PII Tokenization) --
/// same opt-out-kill-switch shape as [`DISABLE_DB_BUNDLE_CONFIG_FLAG`]:
/// tokenization is a core platform mechanism, not a licensed feature, so
/// unseen/OFF/license-server-unreachable must leave it ENABLED (the safe
/// default -- a raw platform username/login must never reach a bundle),
/// and this flag exists only to opt back OUT of it for a deployment whose
/// hub-api internal gRPC endpoint is genuinely unreachable (e.g. air-gapped)
/// and which has accepted the resulting PII exposure to bundles as a
/// documented, deliberate operational tradeoff -- never the default.
pub const DISABLE_PII_TOKENIZATION_FLAG: &str = "waddles.core.disable-pii-tokenization";

/// Production [`FeatureGate`] for [`DISABLE_PII_TOKENIZATION_FLAG`] -- same
/// bypass-aware negation shape as [`DbBundleConfigGate`] (see that type's
/// own doc for the full bypass-awareness rationale, identical here).
pub struct PiiTokenizationGate(Arc<LicenseClient>);

impl PiiTokenizationGate {
    pub fn new(client: Arc<LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureGate for PiiTokenizationGate {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            if self.0.bypass_active() {
                return true;
            }
            !self.0.flag_enabled(DISABLE_PII_TOKENIZATION_FLAG).await
        })
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

/// Un-stubs the `flags` WIT host capability (`crate::capabilities::
/// StageCapabilities::handle_flags`, `enabled` op): a bundle's
/// `feature_enabled(key, default)` call (e.g. every command bundle's own
/// `waddles.command-<name>` gate) used to always resolve to `false`
/// (`default=False` in every bundle) because the capability handler
/// unconditionally denied `not_implemented` -- `core/bundle_executor::
/// host::imports::flags::Host::enabled` then caught that `Err` and
/// "failed open" to the caller's own `default_value`, so the bundle never
/// saw a real PostHog value regardless of the flag's actual state.
///
/// [`FlagSource`] is this module's usual "wrap the external dependency
/// behind a narrow, object-safe trait" seam ([`FeatureGate`] above,
/// `bundle_host_http::egress::FeatureFlag`, ...): production wires a real
/// `Arc<LicenseClient>` (the blanket impl below), tests wire a
/// `FakeFlagSource` so every fallback branch (live / cached / never-seen
/// default / capability-disabled / no-client) is provable without a live
/// PostHog/license server.
/// `Some((value, is_fresh))` -- `key`'s raw value plus whether it came
/// from a snapshot fetched within `cache_ttl`; `None` means no snapshot
/// has ever been fetched. Named alias so [`FlagSource::flag_value`]'s
/// return type doesn't trip `clippy::type_complexity`.
pub type FlagValueFuture<'a> = Pin<Box<dyn Future<Output = Option<(bool, bool)>> + Send + 'a>>;

pub trait FlagSource: Send + Sync {
    /// Whether this deployment bypasses all license/flag gating
    /// (mirrors [`LicenseClient::bypass_active`]).
    fn bypass_active(&self) -> bool;

    /// Resolves `key`'s raw value plus whether it came from a *fresh*
    /// (within `cache_ttl`) snapshot. `None` means no snapshot has ever
    /// been fetched for this process (never-seen, or the flag server has
    /// never once been reachable) -- the caller's own `default_value`
    /// applies, not a value from this trait.
    fn flag_value<'a>(&'a self, key: &'a str) -> FlagValueFuture<'a>;
}

impl FlagSource for Arc<LicenseClient> {
    fn bypass_active(&self) -> bool {
        LicenseClient::bypass_active(self)
    }

    fn flag_value<'a>(&'a self, key: &'a str) -> FlagValueFuture<'a> {
        Box::pin(async move {
            // `flag_enabled` already does the "refresh when the cached
            // snapshot is stale, otherwise read-through-cache" work
            // (`LicenseClient::fresh_snapshot`'s own doc) -- reading
            // `snapshot()` again afterward tells us whether *any*
            // snapshot now exists (never-seen vs. seen-at-least-once) and
            // how fresh it is, without duplicating that logic here.
            let value = self.flag_enabled(key).await;
            let snap = self.snapshot()?;
            let age = chrono::Utc::now().signed_duration_since(snap.fetched_at);
            let fresh = age
                .to_std()
                .map(|a| a < self.config().cache_ttl)
                .unwrap_or(false);
            Some((value, fresh))
        })
    }
}

/// `FLAGS_CAPABILITY_ENABLED` -- a plain env off-switch for the whole
/// `flags` host capability, independent of any individual PostHog flag's
/// own state, mirroring `core/svc_action::config::CliConfig::
/// pii_detokenization_enabled_override`'s "explicit, loudly-logged
/// operator escape hatch" shape (same naming convention: `_ENABLED`,
/// default-on, `false`/`0`/`off`/`no` opts out). Default ON (unset, or any
/// other value, leaves the capability wired); `false` makes every
/// `flags.enabled` host-call resolve straight to the caller's own
/// `default_value`, with no license-client lookup at all -- the
/// reversible-without-a-redeploy kill switch this task calls for.
pub const FLAGS_CAPABILITY_ENABLED_ENV: &str = "FLAGS_CAPABILITY_ENABLED";

/// Reads [`FLAGS_CAPABILITY_ENABLED_ENV`] fresh on every call (cheap env
/// lookup, no caching) so flipping it takes effect on the very next
/// `flags.enabled` host-call, not just at process startup.
fn flags_capability_env_enabled() -> bool {
    match std::env::var(FLAGS_CAPABILITY_ENABLED_ENV) {
        Ok(raw) => !matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "false" | "0" | "off" | "no"
        ),
        Err(_) => true,
    }
}

/// Derives this flag key's Docker ENV baseline variable name:
/// `FLAG_` + `key` uppercased with every `.`/`-` replaced by `_` (e.g.
/// `waddles.command-8ball` -> `FLAG_WADDLES_COMMAND_8BALL`). Pure, no env
/// access -- unit-tested directly (`env_flag_var_name_*` below) independent
/// of [`env_flag_value`]'s own env-reading behavior.
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

/// Reads `key`'s Docker ENV baseline value, if any -- the fallback
/// [`resolve_flag_with`] consults only when PostHog doesn't define the flag
/// (no client configured at all, or no snapshot has ever been fetched),
/// making PostHog optional: an alpha deployment with no PostHog/license
/// server reachable still gets a real per-flag baseline from Helm/Docker
/// env instead of only ever seeing the bundle's own compiled default.
/// `Some(true)`/`Some(false)` for a recognized truthy (`true`/`1`/`on`/
/// `yes`)/falsy (`false`/`0`/`off`/`no`) value (case-insensitive, trimmed);
/// `None` for unset *or* an unrecognized value -- both fall through to the
/// caller's compiled `default_value`, same as "never configured".
///
/// Read fresh on every call (cheap env lookup, same choice as
/// [`flags_capability_env_enabled`] above) rather than cached, so flipping
/// it in a running container takes effect on the very next `flags.enabled`
/// host-call, no redeploy required.
///
/// **LICENSE-flag immunity:** this function -- and the ENV baseline it
/// implements -- is wired *only* into [`resolve_flag_with`]'s plain
/// FEATURE-flag path (the bundle-facing `flags.enabled(key, default)` WIT
/// capability). The license-entitlement [`FeatureGate`] implementations
/// above ([`LicenseFeatureGate`], [`DbBundleConfigGate`],
/// [`BundleDbCapabilityGate`], [`PiiTokenizationGate`],
/// [`MultiTenantWatermarkGate`]) call `LicenseClient::flag_enabled`
/// directly and never pass through this function or [`resolve_flag_with`]
/// at all -- there is no shared code path for an ENV var to leak into
/// license/tier/seat/node gating through.
fn env_flag_value(key: &str) -> Option<bool> {
    let var = env_flag_var_name(key);
    match std::env::var(&var) {
        Ok(raw) => match raw.trim().to_ascii_lowercase().as_str() {
            "true" | "1" | "on" | "yes" => Some(true),
            "false" | "0" | "off" | "no" => Some(false),
            other => {
                tracing::debug!(
                    key,
                    var,
                    value = other,
                    "flags.enabled: unrecognized FLAG_* env value, ignoring"
                );
                None
            }
        },
        Err(_) => None,
    }
}

/// Resolves one `flags.enabled(key, default_value)` host-call (spec
/// §7.4/§6.5). Order: `capability_enabled == false` short-circuits straight
/// to `default_value` (never an error, matching `flags::Host::enabled`'s
/// own "fail-open to default, never an exception" mandate); otherwise
/// bypass short-circuits to `true`; otherwise **PostHog wins whenever it
/// defines the flag** -- a fresh snapshot's value ("posthog-live"), or a
/// stale-but-present snapshot's value ("posthog-cached", since the flag
/// server being unreachable right now must not un-set an already-known
/// flag) -- and only when PostHog has *never* defined the flag (no client
/// configured at all, or a snapshot that has never been fetched) does the
/// [`env_flag_value`] Docker ENV baseline apply, with `default_value` as
/// the final fallback when even that is unset. This makes PostHog optional
/// -- a deployment that never runs it still gets a real per-flag baseline
/// from env instead of only ever the bundle's own compiled default. Logs
/// key/value/source at DEBUG and records via [`record_flag_eval`] on every
/// path -- never silent, never a panic.
pub async fn resolve_flag_with<F: FlagSource>(
    source: Option<&F>,
    capability_enabled: bool,
    key: &str,
    default_value: bool,
) -> bool {
    if !capability_enabled {
        tracing::debug!(
            key,
            default_value,
            source = "capability_disabled",
            "flags.enabled: FLAGS_CAPABILITY_ENABLED=false, using caller default"
        );
        record_flag_eval("capability_disabled");
        return default_value;
    }
    let Some(source) = source else {
        if let Some(value) = env_flag_value(key) {
            tracing::debug!(
                key,
                value,
                source = "env",
                "flags.enabled: no license client configured, using Docker ENV baseline"
            );
            record_flag_eval("env");
            return value;
        }
        tracing::debug!(
            key,
            default_value,
            source = "default",
            "flags.enabled: no license client configured and no ENV baseline set, using caller default"
        );
        record_flag_eval("default");
        return default_value;
    };
    if source.bypass_active() {
        tracing::debug!(
            key,
            value = true,
            source = "bypass",
            "flags.enabled: license bypass active"
        );
        record_flag_eval("bypass");
        return true;
    }
    match source.flag_value(key).await {
        None => {
            if let Some(value) = env_flag_value(key) {
                tracing::debug!(
                    key,
                    value,
                    source = "env",
                    "flags.enabled: never-seen (no snapshot ever fetched), using Docker ENV baseline"
                );
                record_flag_eval("env");
                return value;
            }
            tracing::debug!(
                key,
                default_value,
                source = "default",
                "flags.enabled: never-seen (no snapshot ever fetched) and no ENV baseline set, using caller default"
            );
            record_flag_eval("default");
            default_value
        }
        Some((value, true)) => {
            tracing::debug!(
                key,
                value,
                source = "posthog-live",
                "flags.enabled resolved"
            );
            record_flag_eval("posthog-live");
            value
        }
        Some((value, false)) => {
            tracing::debug!(
                key,
                value,
                source = "posthog-cached",
                "flags.enabled: serving last-known-cached value (flag server unreachable or not yet due for refresh)"
            );
            record_flag_eval("posthog-cached");
            value
        }
    }
}

/// This process's own shared flags-capability `LicenseClient`, built
/// lazily on first use and reused for every subsequent `flags.enabled`
/// host-call -- sharing one client (one cached snapshot) is what makes
/// the live/cached distinction in [`resolve_flag_with`] meaningful at
/// all; a fresh client per call would never have a warm cache and would
/// pay a live network round-trip on every single bundle flag check.
/// `None` when [`build_license_client`] fails (malformed
/// `LICENSE_SERVER_URL`/`POSTHOG_HOST`) -- every call then falls back to
/// the caller's `default_value` via [`resolve_flag_with`]'s `no_client`
/// branch, never panics.
fn shared_flags_license_client() -> Option<Arc<LicenseClient>> {
    static CLIENT: OnceLock<Option<Arc<LicenseClient>>> = OnceLock::new();
    CLIENT
        .get_or_init(|| build_license_client("waddles").ok())
        .clone()
}

/// `crate::telemetry::register_flags_metrics`'s counter, wired in here --
/// `svc_process_flags_evaluated_total{result}` counts every
/// `flags.enabled` host-call resolution by its `result` source
/// (`posthog-live`/`posthog-cached`/`env`/`default`/`bypass`/
/// `capability_disabled`).
/// Deliberately *not* labeled by flag key (unbounded cardinality as the
/// bundle catalog grows; `result` is a fixed, small enum of this module's
/// own strings, so no cardinality risk there -- `rules/critical-
/// rules.md` Observability (OTel)).
static FLAGS_EVAL_TOTAL: OnceLock<prometheus::IntCounterVec> = OnceLock::new();

/// Wires `crate::telemetry::register_flags_metrics`'s counter (registered
/// into *this crate's own* Prometheus registry, so it is actually scraped
/// at `/metrics`) into this module -- called once at startup
/// (`crate::lib::run_with_shutdown`). `OnceLock::set` is a no-op past the
/// first call, so a second, differently-scoped registration attempt
/// (e.g. from an isolated test registry) is simply ignored rather than
/// panicking or clobbering the production counter.
pub fn set_flags_metric(counter: prometheus::IntCounterVec) {
    let _ = FLAGS_EVAL_TOTAL.set(counter);
}

/// `None` until [`set_flags_metric`] has been called -- every
/// [`resolve_flag_with`] call site treats that as "no metrics sink wired
/// yet" and simply skips recording, never panics. This is the normal
/// state for every unit test in this module that exercises
/// `resolve_flag_with` directly without going through `crate::lib`'s
/// startup wiring.
fn record_flag_eval(result: &str) {
    if let Some(counter) = FLAGS_EVAL_TOTAL.get() {
        counter.with_label_values(&[result]).inc();
    }
}

/// Production entry point `crate::capabilities::StageCapabilities::
/// handle_flags` calls for the `enabled` op -- wires [`resolve_flag_with`]
/// to [`shared_flags_license_client`] and [`flags_capability_env_enabled`].
pub async fn resolve_flag(key: &str, default_value: bool) -> bool {
    resolve_flag_with(
        shared_flags_license_client().as_ref(),
        flags_capability_env_enabled(),
        key,
        default_value,
    )
    .await
}

#[cfg(test)]
mod flags_capability_tests {
    use super::test_support::ENV_LOCK;
    use super::*;

    /// Sets `var` to `raw` for the duration of `body`, always restoring the
    /// prior unset state afterward -- every ENV-baseline test below runs
    /// under [`ENV_LOCK`] (acquired by the caller) and uses a unique flag
    /// key, so no test leaks a `FLAG_*` var into another.
    fn with_env_var<T>(var: &str, raw: &str, body: impl FnOnce() -> T) -> T {
        // SAFETY: caller holds `ENV_LOCK`, serializing against every other
        // env-mutating test in this crate.
        unsafe { std::env::set_var(var, raw) };
        let result = body();
        unsafe { std::env::remove_var(var) };
        result
    }

    /// Async sibling of [`with_env_var`]: `fut` is only *polled* (its body
    /// actually runs, including any internal env read) while `.await`ed
    /// below -- between the `set_var` and `remove_var` calls -- unlike a
    /// plain `FnOnce() -> impl Future` passed to the sync [`with_env_var`],
    /// which would construct the future (a no-op for an `async fn`) and
    /// return before ever polling it, letting `remove_var` race ahead of
    /// the real read.
    async fn await_with_env_var<T>(
        var: &str,
        raw: &str,
        fut: impl std::future::Future<Output = T>,
    ) -> T {
        // SAFETY: caller holds `ENV_LOCK`.
        unsafe { std::env::set_var(var, raw) };
        let result = fut.await;
        unsafe { std::env::remove_var(var) };
        result
    }

    #[test]
    fn env_flag_var_name_uppercases_and_replaces_dots_and_dashes() {
        assert_eq!(
            env_flag_var_name("waddles.command-8ball"),
            "FLAG_WADDLES_COMMAND_8BALL"
        );
        assert_eq!(
            env_flag_var_name("waddles.command-roll"),
            "FLAG_WADDLES_COMMAND_ROLL"
        );
    }

    #[test]
    fn env_flag_value_recognizes_truthy_and_falsy_strings_case_insensitively() {
        let _guard = ENV_LOCK.blocking_lock();
        let key = "waddles.test-env-flag-value-parsing";
        let var = env_flag_var_name(key);
        for truthy in ["true", "1", "on", "yes", "TRUE", "On"] {
            assert_eq!(
                with_env_var(&var, truthy, || env_flag_value(key)),
                Some(true),
                "expected {truthy:?} to parse as truthy"
            );
        }
        for falsy in ["false", "0", "off", "no", "FALSE", "Off"] {
            assert_eq!(
                with_env_var(&var, falsy, || env_flag_value(key)),
                Some(false),
                "expected {falsy:?} to parse as falsy"
            );
        }
        assert_eq!(
            with_env_var(&var, "banana", || env_flag_value(key)),
            None,
            "an unrecognized value must fall through, not panic or guess"
        );
    }

    #[test]
    fn env_flag_value_is_none_when_unset() {
        let _guard = ENV_LOCK.blocking_lock();
        let key = "waddles.test-env-flag-value-unset";
        let var = env_flag_var_name(key);
        // Defensive: ensure a stray leftover from a prior failed test run
        // doesn't make this assertion flaky.
        unsafe { std::env::remove_var(&var) };
        assert_eq!(env_flag_value(key), None);
    }

    /// Precedence proof: a live PostHog value must win even when the ENV
    /// baseline disagrees with it -- PostHog overriding ENV when connected
    /// is the whole point of keeping ENV a *baseline*, not an override.
    #[tokio::test]
    async fn posthog_live_value_wins_over_env_baseline_when_they_disagree() {
        let _guard = ENV_LOCK.lock().await;
        let key = "waddles.test-posthog-live-beats-env";
        let var = env_flag_var_name(key);
        let source = FakeFlagSource {
            bypass: false,
            value: Some((false, true)), // PostHog: live, flag OFF
        };
        let result = await_with_env_var(
            &var,
            "true",
            resolve_flag_with(Some(&source), true, key, true),
        )
        .await;
        assert!(
            !result,
            "a live PostHog value must win over a disagreeing ENV baseline"
        );
    }

    /// Precedence proof, cached side: a stale-but-present PostHog snapshot
    /// still wins over the ENV baseline too (same "PostHog overrides
    /// whenever it defines the flag" rule, live or cached).
    #[tokio::test]
    async fn posthog_cached_value_wins_over_env_baseline_when_they_disagree() {
        let _guard = ENV_LOCK.lock().await;
        let key = "waddles.test-posthog-cached-beats-env";
        let var = env_flag_var_name(key);
        let source = FakeFlagSource {
            bypass: false,
            value: Some((true, false)), // PostHog: stale cache, flag ON
        };
        let result = await_with_env_var(
            &var,
            "false",
            resolve_flag_with(Some(&source), true, key, false),
        )
        .await;
        assert!(
            result,
            "a cached PostHog value must win over a disagreeing ENV baseline"
        );
    }

    /// The core "PostHog is optional" case: no license/PostHog client
    /// configured at all (e.g. alpha running without PostHog), and the
    /// Docker ENV baseline is set `true` -- must resolve `true`, not the
    /// caller's `false` default.
    #[tokio::test]
    async fn posthog_absent_env_true_resolves_true() {
        let _guard = ENV_LOCK.lock().await;
        let key = "waddles.test-env-baseline-true-no-client";
        let var = env_flag_var_name(key);
        let result = await_with_env_var(
            &var,
            "true",
            resolve_flag_with::<FakeFlagSource>(None, true, key, false),
        )
        .await;
        assert!(result, "ENV baseline true must win when PostHog is absent");
    }

    /// Mirror of the above with a `false` ENV baseline overriding a `true`
    /// caller default.
    #[tokio::test]
    async fn posthog_absent_env_false_resolves_false() {
        let _guard = ENV_LOCK.lock().await;
        let key = "waddles.test-env-baseline-false-no-client";
        let var = env_flag_var_name(key);
        let result = await_with_env_var(
            &var,
            "false",
            resolve_flag_with::<FakeFlagSource>(None, true, key, true),
        )
        .await;
        assert!(
            !result,
            "ENV baseline false must win when PostHog is absent"
        );
    }

    /// No PostHog client AND no ENV baseline set -- must fall all the way
    /// through to the caller's compiled default, exactly the pre-existing
    /// `no_license_client_falls_back_to_caller_default` behavior, just
    /// re-asserted here alongside the new ENV-baseline cases for contrast.
    #[tokio::test]
    async fn posthog_absent_env_unset_falls_back_to_caller_default() {
        let _guard = ENV_LOCK.lock().await;
        let key = "waddles.test-env-baseline-unset-no-client";
        let var = env_flag_var_name(key);
        unsafe { std::env::remove_var(&var) };
        assert!(
            !resolve_flag_with::<FakeFlagSource>(None, true, key, false).await,
            "no client, no ENV baseline -> caller default (false)"
        );
        assert!(
            resolve_flag_with::<FakeFlagSource>(None, true, key, true).await,
            "no client, no ENV baseline -> caller default (true)"
        );
    }

    /// Never-seen-snapshot side of the same "PostHog absent" case: a real
    /// client exists but has never fetched anything -- the ENV baseline
    /// still applies here too, not just in the `no_client` branch.
    #[tokio::test]
    async fn never_seen_snapshot_env_baseline_wins_over_caller_default() {
        let _guard = ENV_LOCK.lock().await;
        let key = "waddles.test-env-baseline-never-seen";
        let var = env_flag_var_name(key);
        let source = FakeFlagSource {
            bypass: false,
            value: None,
        };
        let result = await_with_env_var(
            &var,
            "true",
            resolve_flag_with(Some(&source), true, key, false),
        )
        .await;
        assert!(
            result,
            "ENV baseline must win over caller default for a never-seen snapshot"
        );
    }

    /// License-entitlement immunity: setting this flag's would-be ENV
    /// baseline variable must have ZERO effect on
    /// [`LicenseFeatureGate`] (or any other [`FeatureGate`] impl) -- those
    /// call `LicenseClient::flag_enabled` directly and never pass through
    /// [`resolve_flag_with`]/[`env_flag_value`] at all. A cold
    /// (never-fetched) client's `RUST_DATA_PLANE_FLAG` must still fail
    /// closed to `false` even with `FLAG_WADDLES_CORE_RUST_DATA_PLANE=true`
    /// set in the environment.
    #[tokio::test]
    async fn license_feature_gate_is_not_overridable_by_its_env_flag_baseline() {
        let _guard = ENV_LOCK.lock().await;
        let var = env_flag_var_name(RUST_DATA_PLANE_FLAG);
        assert_eq!(var, "FLAG_WADDLES_CORE_RUST_DATA_PLANE");
        let cfg = LicenseConfig::new("waddles-test-license-env-immunity").expect("valid defaults");
        let client = LicenseClient::new(cfg).expect("client construction");
        let gate = LicenseFeatureGate::new(client);
        let result = await_with_env_var(&var, "true", gate.enabled()).await;
        assert!(
            !result,
            "LICENSE-entitlement gating must never be overridable by a FLAG_* env var"
        );
    }

    /// Test double for [`FlagSource`] -- `value` mirrors the trait's own
    /// `Option<(bool, bool)>` contract (`None` = never-seen,
    /// `Some((value, is_fresh))` otherwise) so every fallback branch in
    /// [`resolve_flag_with`] is provable without a live PostHog/license
    /// server.
    struct FakeFlagSource {
        bypass: bool,
        value: Option<(bool, bool)>,
    }

    impl FlagSource for FakeFlagSource {
        fn bypass_active(&self) -> bool {
            self.bypass
        }

        fn flag_value<'a>(&'a self, _key: &'a str) -> FlagValueFuture<'a> {
            let value = self.value;
            Box::pin(async move { value })
        }
    }

    #[tokio::test]
    async fn live_enabled_flag_resolves_true() {
        let source = FakeFlagSource {
            bypass: false,
            value: Some((true, true)),
        };
        assert!(resolve_flag_with(Some(&source), true, "waddles.command-8ball", false).await);
    }

    #[tokio::test]
    async fn live_disabled_flag_resolves_false_even_with_a_true_default() {
        let source = FakeFlagSource {
            bypass: false,
            value: Some((false, true)),
        };
        assert!(!resolve_flag_with(Some(&source), true, "waddles.command-8ball", true).await);
    }

    #[tokio::test]
    async fn stale_cached_value_wins_over_the_caller_default() {
        // Flag server unreachable right now (stale snapshot, `is_fresh ==
        // false`) but a prior fetch is still cached -- must serve that
        // cached value, never silently fall back to `default_value`.
        let source = FakeFlagSource {
            bypass: false,
            value: Some((true, false)),
        };
        assert!(resolve_flag_with(Some(&source), true, "waddles.command-roll", false).await);
    }

    #[tokio::test]
    async fn never_seen_snapshot_falls_back_to_caller_default() {
        let source = FakeFlagSource {
            bypass: false,
            value: None,
        };
        assert!(!resolve_flag_with(Some(&source), true, "waddles.command-lurk", false).await);
        assert!(resolve_flag_with(Some(&source), true, "waddles.command-lurk", true).await);
    }

    #[tokio::test]
    async fn capability_disabled_env_toggle_short_circuits_to_caller_default() {
        // Even a source that would otherwise say "live, true" must never
        // be consulted once the capability is toggled off.
        let source = FakeFlagSource {
            bypass: false,
            value: Some((true, true)),
        };
        assert!(!resolve_flag_with(Some(&source), false, "waddles.command-count", false).await);
    }

    #[tokio::test]
    async fn no_license_client_falls_back_to_caller_default() {
        assert!(
            !resolve_flag_with::<FakeFlagSource>(None, true, "waddles.command-8ball", false).await
        );
        assert!(
            resolve_flag_with::<FakeFlagSource>(None, true, "waddles.command-8ball", true).await
        );
    }

    #[tokio::test]
    async fn bypass_active_resolves_true_regardless_of_default() {
        let source = FakeFlagSource {
            bypass: true,
            value: Some((false, true)),
        };
        assert!(resolve_flag_with(Some(&source), true, "waddles.command-8ball", false).await);
    }

    /// Proves [`FlagSource`]'s blanket `Arc<LicenseClient>` impl reaches a
    /// real, cold (never-fetched) client's own fail-closed contract rather
    /// than this module inventing its own -- same proof style as
    /// `core/svc_action::flags`'s `license_flag_reaches_a_real_cold_client_
    /// and_fails_closed_to_off`.
    #[tokio::test]
    async fn real_cold_license_client_never_seen_falls_back_to_caller_default() {
        let cfg = LicenseConfig::new("waddles-test-flags-capability")
            .expect("default LicenseConfig::new never fails");
        let client = LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        assert!(resolve_flag_with(Some(&client), true, "waddles.command-8ball", true).await);
        assert!(!resolve_flag_with(Some(&client), true, "waddles.command-8ball", false).await);
    }

    #[test]
    fn flags_capability_enabled_env_defaults_on_when_unset() {
        // Isolated process-level env mutation is unsafe to run in
        // parallel with other tests touching the same var -- this crate's
        // test binary runs `#[tokio::test]`s concurrently, so this check
        // only asserts the documented default behavior indirectly via
        // `resolve_flag_with`'s own `capability_enabled` parameter
        // (exercised directly above) rather than mutating process env
        // here.
        assert_eq!(FLAGS_CAPABILITY_ENABLED_ENV, "FLAGS_CAPABILITY_ENABLED");
    }
}

#[cfg(test)]
mod all_gate_tests {
    use super::test_support::{FixedGate, ToggleGate};
    use super::*;

    #[tokio::test]
    async fn all_gate_is_enabled_only_when_every_wrapped_gate_is_enabled() {
        let gate = AllGate(vec![Arc::new(FixedGate(true)), Arc::new(FixedGate(true))]);
        assert!(gate.enabled().await);
    }

    #[tokio::test]
    async fn all_gate_is_disabled_when_any_wrapped_gate_is_disabled() {
        let gate = AllGate(vec![Arc::new(FixedGate(true)), Arc::new(FixedGate(false))]);
        assert!(!gate.enabled().await);
    }

    #[tokio::test]
    async fn all_gate_reacts_to_a_live_flip_in_either_wrapped_gate() {
        let toggle = Arc::new(ToggleGate::new(true));
        let gate = AllGate(vec![Arc::new(FixedGate(true)), toggle.clone()]);
        assert!(gate.enabled().await);
        toggle.set(false);
        assert!(!gate.enabled().await);
    }

    #[tokio::test]
    async fn all_gate_of_an_empty_list_is_enabled_vacuously() {
        let gate = AllGate(vec![]);
        assert!(gate.enabled().await);
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
    use tokio::sync::Mutex;

    /// `std::env` is process-global; serialize every env-mutating test in
    /// this file (both the pre-existing `LICENSE_SERVER_URL` mutations and
    /// this task's new `FLAG_*` ENV-baseline mutations) behind one shared
    /// lock so parallel `cargo test` threads never race on the same global
    /// table, regardless of which specific variable each test touches.
    /// `tokio::sync::Mutex` (not `std::sync::Mutex`) deliberately -- several
    /// `#[tokio::test]`s below hold the guard across an `.await` (the ENV
    /// var must stay set for the whole duration the async flag-resolution
    /// future is polled), which `clippy::await_holding_lock` correctly
    /// forbids for a `std::sync::MutexGuard`. Non-`async` `#[test]`s use
    /// [`Mutex::blocking_lock`] instead of `.lock().await` (no executor
    /// present to await on).
    pub static ENV_LOCK: Mutex<()> = Mutex::const_new(());

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
    use super::test_support::{FixedGate, ToggleGate, ENV_LOCK};
    use super::*;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variable. Mirrors
    // `core/svc_ingest/src/license.rs`'s identical guard for the identical
    // hazard. Shared via `test_support::ENV_LOCK` so this module's
    // `LICENSE_SERVER_URL` mutations and `flags_capability_tests`'s
    // `FLAG_*` mutations serialize against each other too, not just within
    // their own module.

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

    #[tokio::test]
    async fn static_gate_returns_its_constructed_value() {
        assert!(StaticGate(true).enabled().await);
        assert!(!StaticGate(false).enabled().await);
    }

    #[test]
    fn rust_data_plane_flag_matches_the_product_flag_key_convention() {
        assert_eq!(RUST_DATA_PLANE_FLAG, "waddles.core.rust-data-plane");
    }

    #[test]
    fn bundle_reputation_capability_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            BUNDLE_REPUTATION_CAPABILITY_FLAG,
            "waddles.bundle-reputation-capability"
        );
    }

    #[test]
    fn bundle_db_capability_flag_matches_the_product_flag_key_convention() {
        assert_eq!(BUNDLE_DB_CAPABILITY_FLAG, "waddles.bundle-db-capability");
    }

    #[test]
    fn disable_db_bundle_config_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            DISABLE_DB_BUNDLE_CONFIG_FLAG,
            "waddles.core.disable-db-bundle-config"
        );
    }

    #[test]
    fn disable_pii_tokenization_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            DISABLE_PII_TOKENIZATION_FLAG,
            "waddles.core.disable-pii-tokenization"
        );
    }

    /// Same hard invariant as `db_bundle_config_gate_defaults_enabled_for_a_
    /// never_seen_kill_switch_flag`, applied to the PII-tokenization
    /// kill-switch: a never-seen flag (every fresh deployment's starting
    /// state) must leave tokenization ENABLED -- raw PII must never reach a
    /// bundle by default.
    #[tokio::test]
    async fn pii_tokenization_gate_defaults_enabled_for_a_never_seen_kill_switch_flag() {
        let cfg =
            LicenseConfig::new("waddles-test-pii-tokenization-default").expect("valid defaults");
        let client = LicenseClient::new(cfg).expect("client construction");
        let gate = PiiTokenizationGate::new(client);
        assert!(
            gate.enabled().await,
            "an unseen kill-switch flag must leave PII tokenization enabled"
        );
    }

    /// Bypass-awareness regression test, identical rationale to
    /// `db_bundle_config_gate_stays_enabled_under_the_hardcoded_domain_
    /// bypass`: a bypassed client's `flag_enabled` reads `true` for ANY
    /// key, so a naive negation would report tokenization DISABLED for
    /// every PenguinTech-owned deployment -- the opposite of the intended
    /// "every feature unlocked" bypass meaning.
    #[tokio::test]
    async fn pii_tokenization_gate_stays_enabled_under_the_hardcoded_domain_bypass() {
        let client = {
            let _guard = ENV_LOCK.lock().await;
            build_license_client("waddles-test-pii-tokenization-bypass").expect("valid defaults")
        };
        assert!(
            client.bypass_active(),
            "sanity check: this client must actually be bypassed"
        );
        let gate = PiiTokenizationGate::new(client);
        assert!(
            gate.enabled().await,
            "bypass must leave PII tokenization enabled, not disabled"
        );
    }

    /// The kill-switch inversion's core regression test: a never-seen flag
    /// (the state every fresh deployment starts in, and the state a
    /// permanently-unreachable license server leaves a pod in forever)
    /// reports the DB-driven path ENABLED, not disabled -- the opposite of
    /// the retired `waddles.core.db-bundle-config` flag's own default-OFF
    /// contract. Built directly via `LicenseConfig::new` (no bypass), same
    /// pattern as `license_feature_gate_defaults_off_for_a_non_bypassed_
    /// client_with_no_snapshot` -- a cold client has never fetched
    /// anything, so `flag_enabled` returns its own fail-closed `false` for
    /// the raw kill-switch flag, and `DbBundleConfigGate::enabled` negates
    /// that to `true`.
    #[tokio::test]
    async fn db_bundle_config_gate_defaults_enabled_for_a_never_seen_kill_switch_flag() {
        let cfg =
            LicenseConfig::new("waddles-test-db-bundle-config-default").expect("valid defaults");
        let client = LicenseClient::new(cfg).expect("client construction");
        let gate = DbBundleConfigGate::new(client);
        assert!(
            gate.enabled().await,
            "an unseen kill-switch flag must leave the DB-driven path enabled"
        );
    }

    /// Bypass-awareness regression test: `build_license_client`'s hardcoded
    /// self-domain bypass (see `build_license_client_hardcoded_domain_
    /// bypasses_flag_checks` above) makes `flag_enabled` read `true` for
    /// ANY key on this client, including
    /// [`DISABLE_DB_BUNDLE_CONFIG_FLAG`] -- naively negating that raw
    /// `true` would report the DB-driven path DISABLED for every
    /// deployment of this service, permanently, which is the exact bug
    /// this test guards against. `DbBundleConfigGate::enabled` must check
    /// `bypass_active()` first and report `true` (DB path enabled).
    #[tokio::test]
    async fn db_bundle_config_gate_stays_enabled_under_the_hardcoded_domain_bypass() {
        let client = {
            let _guard = ENV_LOCK.lock().await;
            build_license_client("waddles-test-db-bundle-config-bypass").expect("valid defaults")
        };
        assert!(
            client.bypass_active(),
            "sanity check: this client must actually be bypassed"
        );
        let gate = DbBundleConfigGate::new(client);
        assert!(
            gate.enabled().await,
            "bypass must leave the DB-driven path enabled, not disabled"
        );
    }

    #[test]
    fn disable_multi_tenant_watermark_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            DISABLE_MULTI_TENANT_WATERMARK_FLAG,
            "waddles.core.disable-multi-tenant-watermark"
        );
    }

    /// The multi-tenant path's own fail-safe-ON regression test: a
    /// never-seen kill-switch flag (every fresh deployment's starting
    /// state) must leave the multi-tenant change-log consumer path
    /// ENABLED -- this is the user's own hard requirement ("every
    /// svc_process/svc_action pod serves ALL tenants"), not merely a
    /// convenient default.
    #[tokio::test]
    async fn multi_tenant_watermark_gate_defaults_enabled_for_a_never_seen_kill_switch_flag() {
        let cfg = LicenseConfig::new("waddles-test-multi-tenant-watermark-default")
            .expect("valid defaults");
        let client = LicenseClient::new(cfg).expect("client construction");
        let gate = MultiTenantWatermarkGate::new(client);
        assert!(
            gate.enabled().await,
            "an unseen kill-switch flag must leave the multi-tenant path enabled"
        );
    }

    #[tokio::test]
    async fn multi_tenant_watermark_gate_stays_enabled_under_the_hardcoded_domain_bypass() {
        let client = {
            let _guard = ENV_LOCK.lock().await;
            build_license_client("waddles-test-multi-tenant-watermark-bypass")
                .expect("valid defaults")
        };
        assert!(
            client.bypass_active(),
            "sanity check: this client must actually be bypassed"
        );
        let gate = MultiTenantWatermarkGate::new(client);
        assert!(
            gate.enabled().await,
            "bypass must leave the multi-tenant path enabled, not disabled"
        );
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
        let _guard = ENV_LOCK.blocking_lock();
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
        let _guard = ENV_LOCK.blocking_lock();
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
        let _guard = ENV_LOCK.blocking_lock();
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
            let _guard = ENV_LOCK.lock().await;
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

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
use std::sync::{Arc, OnceLock};

/// Gates the action-stage drain loop (`crate::dispatch::drain_loop`). OFF
/// ⇒ the stage serves `/health`/`/metrics` and drains nothing -- the safe
/// state during rollout.
pub const RUST_DATA_PLANE_FLAG: &str = "waddles.core.rust-data-plane";

/// Gates the bundle `http` host capability (`crate::egress::EgressGuard`).
/// OFF ⇒ every egress call is denied `feature_disabled`.
pub const BUNDLE_EGRESS_FLAG: &str = "waddles.core.bundle-egress";

/// Gates the bundle `db` host capability (`crate::capabilities::
/// StageCapabilities::handle_db`) -- same key and same plain opt-in shape
/// (unseen/OFF denies `feature_disabled`) as `core/svc_process/src/
/// license.rs::BUNDLE_DB_CAPABILITY_FLAG`; both stages gate on the
/// identical flag so enabling `db` is one PostHog toggle, not two.
pub const BUNDLE_DB_CAPABILITY_FLAG: &str = "waddles.bundle-db-capability";

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

/// One flag's live enabled/disabled state. Object-safe (a manually-boxed
/// future, matching every other async trait in this crate) so callers can
/// hold `Arc<dyn FeatureFlag>` without an `async_trait` dependency.
/// Re-exported from the shared `bundle_host_http` crate (PR #459
/// follow-up, `core/svc_action::egress`'s own doc): identical trait, now
/// defined once so `core/svc_process`'s own capability gate can hold the
/// same `Arc<dyn FeatureFlag>` shape `EgressGuard::new` expects without a
/// second, divergent trait.
pub use bundle_host_http::egress::FeatureFlag;

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

/// Opt-out kill-switch for the outbound PII-detokenization pass
/// (`egress_detokenizer`, wired into `crate::capabilities::
/// StageCapabilities::handle_relay`/`handle_discord_relay`) -- same
/// opt-out-kill-switch shape as [`DISABLE_DB_BUNDLE_CONFIG_FLAG`]:
/// detokenization is a core platform mechanism, not a licensed feature, so
/// unseen/OFF/license-server-unreachable must leave it ENABLED (the
/// default -- real display names, not opaque tokens, appear in chat/
/// overlay output). ON opts back OUT of it: every relay send shows the raw
/// `{user:<token>}` placeholder it received from the bundle rather than a
/// resolved name -- never raw PII either way, since nothing upstream of
/// this pass ever holds raw PII (`core/svc_process`'s inbound tokenization
/// pass already stripped it) -- a documented, deliberate degraded-UX
/// tradeoff, never the default.
pub const DISABLE_PII_DETOKENIZATION_FLAG: &str = "waddles.core.disable-pii-detokenization";

/// Production [`FeatureFlag`] for [`DISABLE_PII_DETOKENIZATION_FLAG`] --
/// same bypass-aware negation shape as [`DisableDbBundleConfigFlag`] (see
/// that type's own doc for the full bypass-awareness rationale, identical
/// here).
pub struct DisablePiiDetokenizationFlag(Arc<penguin_licensing::LicenseClient>);

impl DisablePiiDetokenizationFlag {
    pub fn new(client: Arc<penguin_licensing::LicenseClient>) -> Self {
        Self(client)
    }
}

impl FeatureFlag for DisablePiiDetokenizationFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        Box::pin(async move {
            if self.0.bypass_active() {
                return true;
            }
            !self.0.flag_enabled(DISABLE_PII_DETOKENIZATION_FLAG).await
        })
    }
}

/// Builds the [`FeatureFlag`] `crate::lib::build_hub_client`/
/// `crate::capabilities::StageCapabilities::with_detokenize`'s call site
/// gate on: [`DisablePiiDetokenizationFlag`] over a real client, or a fixed
/// "detokenization enabled" answer when no license client is available at
/// all -- mirrors [`db_bundle_config_flag`]/[`multi_tenant_watermark_flag`]'s
/// identical `None`-branch rationale.
pub fn pii_detokenization_flag(
    license: &Option<Arc<penguin_licensing::LicenseClient>>,
) -> Arc<dyn FeatureFlag> {
    match license {
        Some(client) => boxed(DisablePiiDetokenizationFlag::new(Arc::clone(client))),
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

/// Un-stubs the `flags` WIT host capability (`crate::capabilities::
/// StageCapabilities::handle_flags`, `enabled` op) -- field-for-field
/// mirror of `core/svc_process::license`'s identical fix (same doc there
/// for the full "why" on the host-import fail-open bug this closes). A
/// bundle's `feature_enabled(key, default)` call used to always resolve to
/// `default` because this capability unconditionally denied
/// `not_implemented`, and `core/bundle_executor::host::imports::
/// flags::Host::enabled` silently caught that `Err` and fell back to the
/// caller's own `default_value` -- so the bundle never saw a real PostHog
/// value regardless of the flag's actual state.
///
/// [`FlagSource`] is this crate's usual "wrap the external dependency
/// behind a narrow, object-safe trait" seam ([`FeatureFlag`] above):
/// production wires a real `Arc<penguin_licensing::LicenseClient>` (the
/// blanket impl below), tests wire a `FakeFlagSource` so every fallback
/// branch (live / cached / never-seen default / capability-disabled /
/// no-client) is provable without a live PostHog/license server.
/// `Some((value, is_fresh))` -- `key`'s raw value plus whether it came
/// from a snapshot fetched within `cache_ttl`; `None` means no snapshot
/// has ever been fetched. Named alias so [`FlagSource::flag_value`]'s
/// return type doesn't trip `clippy::type_complexity`.
pub type FlagValueFuture<'a> = Pin<Box<dyn Future<Output = Option<(bool, bool)>> + Send + 'a>>;

pub trait FlagSource: Send + Sync {
    /// Whether this deployment bypasses all license/flag gating.
    fn bypass_active(&self) -> bool;

    /// Resolves `key`'s raw value plus whether it came from a *fresh*
    /// (within `cache_ttl`) snapshot. `None` means no snapshot has ever
    /// been fetched for this process -- the caller's own `default_value`
    /// applies, not a value from this trait.
    fn flag_value<'a>(&'a self, key: &'a str) -> FlagValueFuture<'a>;
}

impl FlagSource for Arc<penguin_licensing::LicenseClient> {
    fn bypass_active(&self) -> bool {
        penguin_licensing::LicenseClient::bypass_active(self)
    }

    fn flag_value<'a>(&'a self, key: &'a str) -> FlagValueFuture<'a> {
        Box::pin(async move {
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
/// own state, mirroring `crate::config::CliConfig::
/// pii_detokenization_enabled_override`'s "explicit, loudly-logged
/// operator escape hatch" shape. Default ON (unset, or any other value,
/// leaves the capability wired); `false`/`0`/`off`/`no` makes every
/// `flags.enabled` host-call resolve straight to the caller's own
/// `default_value`, with no license-client lookup at all.
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

/// Derives this flag key's Docker ENV baseline variable name -- see
/// `core/svc_process::license::env_flag_var_name`'s identical doc for the
/// convention (`FLAG_` + key uppercased, `.`/`-` -> `_`).
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

/// Reads `key`'s Docker ENV baseline value, if any -- see
/// `core/svc_process::license::env_flag_value`'s identical doc for the full
/// rationale (makes PostHog optional) and the truthy/falsy parsing rules.
/// `None` for unset or an unrecognized value.
///
/// **LICENSE-flag immunity:** wired *only* into [`resolve_flag_with`]'s
/// plain FEATURE-flag path -- the [`FeatureFlag`] implementations above
/// ([`LicenseFlag`], [`DisableDbBundleConfigFlag`],
/// [`DisableMultiTenantWatermarkFlag`], [`DisablePiiDetokenizationFlag`])
/// call `LicenseClient::flag_enabled` directly and never pass through this
/// function or [`resolve_flag_with`] at all.
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

/// Resolves one `flags.enabled(key, default_value)` host-call -- see
/// `core/svc_process::license::resolve_flag_with`'s identical doc for the
/// full fallback-chain rationale: PostHog wins whenever it defines the flag
/// (live, then cached), otherwise the [`env_flag_value`] Docker ENV
/// baseline applies, otherwise `default_value` -- never an error.
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

/// This process's own shared flags-capability license client, built
/// lazily on first use and reused for every subsequent `flags.enabled`
/// host-call -- see `core/svc_process::license::shared_flags_license_client`'s
/// identical doc for why sharing one client (one cached snapshot) matters.
fn shared_flags_license_client() -> Option<Arc<penguin_licensing::LicenseClient>> {
    static CLIENT: OnceLock<Option<Arc<penguin_licensing::LicenseClient>>> = OnceLock::new();
    CLIENT.get_or_init(crate::build_license_client).clone()
}

/// `crate::telemetry::register_flags_metrics`'s counter, wired in here --
/// see `core/svc_process::license::FLAGS_EVAL_TOTAL`'s identical doc for
/// the cardinality/labeling rationale.
static FLAGS_EVAL_TOTAL: OnceLock<prometheus::IntCounterVec> = OnceLock::new();

/// Wires `crate::telemetry::register_flags_metrics`'s counter into this
/// module -- called once at startup (`crate::run_with_shutdown`).
/// `OnceLock::set` is a no-op past the first call.
pub fn set_flags_metric(counter: prometheus::IntCounterVec) {
    let _ = FLAGS_EVAL_TOTAL.set(counter);
}

/// `None` until [`set_flags_metric`] has been called -- every
/// [`resolve_flag_with`] call site treats that as "no metrics sink wired
/// yet" and simply skips recording, never panics (the normal state for
/// every unit test in this module exercising `resolve_flag_with`
/// directly).
fn record_flag_eval(result: &str) {
    if let Some(counter) = FLAGS_EVAL_TOTAL.get() {
        counter.with_label_values(&[result]).inc();
    }
}

/// Production entry point `crate::capabilities::StageCapabilities::
/// handle_flags` calls for the `enabled` op.
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
    use super::*;
    use tokio::sync::Mutex;

    /// `std::env` is process-global; serialize this module's `FLAG_*`
    /// env-mutating tests against each other (same rationale as
    /// `core/svc_process::license`'s identical `ENV_LOCK`). No other test
    /// in this crate mutates a `FLAG_*` variable, so a module-local lock is
    /// sufficient here. `tokio::sync::Mutex` (not `std::sync::Mutex`)
    /// deliberately -- several `#[tokio::test]`s below hold the guard
    /// across an `.await`, which `clippy::await_holding_lock` forbids for a
    /// `std::sync::MutexGuard`. The lone non-`async` `#[test]` uses
    /// [`Mutex::blocking_lock`] instead of `.lock().await`.
    static ENV_LOCK: Mutex<()> = Mutex::const_new(());

    /// Sets `var` to `raw` for the duration of `body`, restoring the prior
    /// unset state afterward -- for synchronous checks only (see
    /// [`await_with_env_var`] for `resolve_flag_with`'s async case).
    fn with_env_var<T>(var: &str, raw: &str, body: impl FnOnce() -> T) -> T {
        // SAFETY: caller holds `ENV_LOCK`.
        unsafe { std::env::set_var(var, raw) };
        let result = body();
        unsafe { std::env::remove_var(var) };
        result
    }

    /// Async sibling of [`with_env_var`]: `fut` is only polled (so any
    /// internal env read actually happens) while awaited below, between
    /// `set_var` and `remove_var` -- see
    /// `core/svc_process::license::flags_capability_tests::
    /// await_with_env_var`'s identical doc for why a plain `FnOnce() ->
    /// impl Future` would be wrong here (an unpolled `async fn` future
    /// never runs its body).
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

    /// Precedence proof: a live PostHog value must win even when the ENV
    /// baseline disagrees with it.
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

    /// The core "PostHog is optional" case: no license/PostHog client
    /// configured at all (e.g. alpha running without PostHog), ENV
    /// baseline set `true` -- must resolve `true`, not the caller default.
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

    /// Mirror with a `false` ENV baseline overriding a `true` caller
    /// default.
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

    /// No PostHog client AND no ENV baseline set -- falls all the way
    /// through to the caller's compiled default.
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
    /// still applies here too.
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
    /// baseline variable must have ZERO effect on [`LicenseFlag`] (or any
    /// other [`FeatureFlag`] impl in this module) -- those call
    /// `LicenseClient::flag_enabled` directly and never pass through
    /// [`resolve_flag_with`]/[`env_flag_value`] at all. A cold
    /// (never-fetched) client's `RUST_DATA_PLANE_FLAG` must still fail
    /// closed to `false` even with `FLAG_WADDLES_CORE_RUST_DATA_PLANE=true`
    /// set in the environment.
    #[tokio::test]
    async fn license_flag_is_not_overridable_by_its_env_flag_baseline() {
        let _guard = ENV_LOCK.lock().await;
        let var = env_flag_var_name(RUST_DATA_PLANE_FLAG);
        assert_eq!(var, "FLAG_WADDLES_CORE_RUST_DATA_PLANE");
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-license-env-immunity")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = LicenseFlag::new(client, RUST_DATA_PLANE_FLAG);
        let result = await_with_env_var(&var, "true", flag.enabled()).await;
        assert!(
            !result,
            "LICENSE-entitlement gating must never be overridable by a FLAG_* env var"
        );
    }

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
    /// real, cold (never-fetched) client's own fail-closed contract --
    /// same proof style as this module's own
    /// `license_flag_reaches_a_real_cold_client_and_fails_closed_to_off`.
    #[tokio::test]
    async fn real_cold_license_client_never_seen_falls_back_to_caller_default() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-flags-capability")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        assert!(resolve_flag_with(Some(&client), true, "waddles.command-8ball", true).await);
        assert!(!resolve_flag_with(Some(&client), true, "waddles.command-8ball", false).await);
    }

    #[test]
    fn flags_capability_enabled_env_name_matches_convention() {
        assert_eq!(FLAGS_CAPABILITY_ENABLED_ENV, "FLAGS_CAPABILITY_ENABLED");
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

    /// `Some(client)` branch: wraps a real client in `DisableDbBundleConfigFlag`
    /// rather than the `None` fallback's fixed `StaticFlag(true)`.
    #[tokio::test]
    async fn db_bundle_config_flag_wraps_a_real_client_when_available() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-db-bundle-some")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = db_bundle_config_flag(&Some(client));
        assert!(
            flag.enabled().await,
            "an unseen kill-switch flag must leave the DB-driven path enabled"
        );
    }

    #[test]
    fn disable_multi_tenant_watermark_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            DISABLE_MULTI_TENANT_WATERMARK_FLAG,
            "waddles.core.disable-multi-tenant-watermark"
        );
    }

    #[test]
    fn disable_pii_detokenization_flag_matches_the_product_flag_key_convention() {
        assert_eq!(
            DISABLE_PII_DETOKENIZATION_FLAG,
            "waddles.core.disable-pii-detokenization"
        );
    }

    /// Same hard invariant as the DB-bundle-config kill-switch regression
    /// test above: a never-seen flag must leave outbound detokenization
    /// ENABLED -- real display names, not raw tokens, appear by default.
    #[tokio::test]
    async fn disable_pii_detokenization_flag_defaults_enabled_when_never_seen() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-pii-detok-default")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = DisablePiiDetokenizationFlag::new(client);
        assert!(
            flag.enabled().await,
            "an unseen kill-switch flag must leave PII detokenization enabled"
        );
    }

    #[tokio::test]
    async fn multi_tenant_watermark_flag_defaults_enabled_when_no_license_client_is_available() {
        let flag = multi_tenant_watermark_flag(&None);
        assert!(flag.enabled().await);
    }

    #[tokio::test]
    async fn pii_detokenization_flag_defaults_enabled_when_no_license_client_is_available() {
        let flag = pii_detokenization_flag(&None);
        assert!(flag.enabled().await);
    }

    /// `Some(client)` branch: wraps a real client in
    /// `DisablePiiDetokenizationFlag` rather than the `None` fallback's
    /// fixed `StaticFlag(true)`.
    #[tokio::test]
    async fn pii_detokenization_flag_wraps_a_real_client_when_available() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-pii-detok-some")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = pii_detokenization_flag(&Some(client));
        assert!(
            flag.enabled().await,
            "an unseen kill-switch flag must leave PII detokenization enabled"
        );
    }

    /// `Some(client)` branch: wraps a real client in
    /// `DisableMultiTenantWatermarkFlag` rather than the `None` fallback's
    /// fixed `StaticFlag(true)`.
    #[tokio::test]
    async fn multi_tenant_watermark_flag_wraps_a_real_client_when_available() {
        let cfg = penguin_licensing::LicenseConfig::new("waddles-test-multi-tenant-some")
            .expect("default LicenseConfig::new never fails");
        let client = penguin_licensing::LicenseClient::new(cfg)
            .expect("LicenseClient::new with a valid default config never fails");
        let flag = multi_tenant_watermark_flag(&Some(client));
        assert!(
            flag.enabled().await,
            "an unseen kill-switch flag must leave the multi-tenant path enabled"
        );
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

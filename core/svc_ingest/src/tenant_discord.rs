//! Tenant -> Discord application ownership rule.
//!
//! The process-wide Discord application (`DISCORD_BOT_TOKEN`) serves ONLY the
//! default tenant. Every other tenant brings its own application; its bot token
//! reaches this service solely through a tenant-scoped `discord` connection
//! binding (`crate::outbound_creds_discord`, one gateway per binding in the
//! connector runtime). A non-default tenant NEVER falls back to the env
//! credential: the send/event is refused loudly with the stable code
//! [`TENANT_DISCORD_APP_MISSING`], counted on
//! `waddles_tenant_discord_app_missing_total{surface}`, logged at WARN and
//! emitted as a tracing (span) event. Logs carry numeric tenant ids only.
//!
//! "Default tenant" is the numeric id in `DEFAULT_TENANT_ID` (published once at
//! startup via [`set_default_tenant_id`]); an absent/`0` tenant stamp is always
//! the default tenant. An unconfigured id (`0`) makes every positive tenant
//! non-default (fail closed).

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::LazyLock;

/// Stable machine code: a non-default tenant has no Discord application of its own.
pub const TENANT_DISCORD_APP_MISSING: &str = "TENANT_DISCORD_APP_MISSING";

/// Surface label: the legacy env-token outbound drain refused a non-default tenant's op.
pub const SURFACE_OUTBOUND_ENV: &str = "outbound_env";
/// Surface label: the per-community executor found no binding for a non-default tenant.
pub const SURFACE_OUTBOUND_BINDING: &str = "outbound_binding";
/// Surface label: the env-token gateway dropped an event owned by a non-default tenant.
pub const SURFACE_GATEWAY_ENV: &str = "gateway_env";

static DEFAULT_TENANT_ID: AtomicI64 = AtomicI64::new(0);

static APP_MISSING: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "waddles_tenant_discord_app_missing_total",
            "Discord ops/events refused because a non-default tenant has no Discord application of its own (env credentials are default-tenant only), by surface",
        ),
        &["surface"],
    )
    .expect("valid metric definition")
});

/// Publishes the default tenant's numeric id (startup, from `DEFAULT_TENANT_ID`).
pub fn set_default_tenant_id(id: i64) {
    DEFAULT_TENANT_ID.store(id, Ordering::SeqCst);
}

/// The configured default tenant id (`0` = unconfigured).
#[must_use]
pub fn default_tenant_id() -> i64 {
    DEFAULT_TENANT_ID.load(Ordering::SeqCst)
}

/// `true` when `tenant` is the default tenant given `default_id`: an absent
/// or `0` stamp always is; a positive id only when it equals `default_id`.
#[must_use]
pub fn is_default_tenant_of(tenant: Option<i64>, default_id: i64) -> bool {
    match tenant {
        None | Some(0) => true,
        Some(t) => t == default_id,
    }
}

/// [`is_default_tenant_of`] against the process-wide default id.
#[must_use]
pub fn is_default_tenant(tenant: Option<i64>) -> bool {
    is_default_tenant_of(tenant, default_tenant_id())
}

/// Registers the refusal counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(APP_MISSING.clone()))
}

/// Count of refusals on `surface` (test/observability helper).
#[must_use]
pub fn app_missing_count(surface: &str) -> u64 {
    APP_MISSING.with_label_values(&[surface]).get()
}

/// Records one refusal: counter + WARN log + tracing event (numeric tenant only).
pub fn record_app_missing(surface: &'static str, op: &str, tenant: Option<i64>) {
    APP_MISSING.with_label_values(&[surface]).inc();
    tracing::warn!(
        code = TENANT_DISCORD_APP_MISSING,
        surface,
        op,
        tenant_id = tenant.unwrap_or_default(),
        "non-default tenant has no discord application of its own; env credentials are default-tenant only, refused"
    );
}

/// Whether the env-token Discord gateway may publish `event`. With the
/// dataplane scope resolver installed, an event whose guild resolves to a
/// non-default tenant is refused (counted, logged): that tenant must run its
/// own application. Without a resolver every event carries the static
/// default-tenant scope, and an unresolvable source is left to `publish_event`
/// to drop.
pub async fn env_gateway_event_allowed<S: crate::scope_resolver::ScopeSource>(
    resolver: Option<&crate::scope_resolver::ScopeResolver<S>>,
    event: &penguin_spine::PlatformEvent,
    default_id: i64,
) -> bool {
    let (Some(r), Some(src)) = (
        resolver,
        event.source.as_ref().map(|s| s.account_id.as_str()),
    ) else {
        return true;
    };
    if src.is_empty() || src == "dm" {
        return true;
    }
    match r.resolve(&event.platform, src).await {
        Ok(res) if !is_default_tenant_of(Some(res.tenant_id), default_id) => {
            record_app_missing(SURFACE_GATEWAY_ENV, "gateway.event", Some(res.tenant_id));
            false
        }
        _ => true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_tenant_rule_is_fail_closed() {
        assert!(is_default_tenant_of(None, 0));
        assert!(is_default_tenant_of(Some(0), 5));
        assert!(is_default_tenant_of(Some(5), 5));
        assert!(!is_default_tenant_of(Some(7), 5));
        assert!(
            !is_default_tenant_of(Some(1), 0),
            "unconfigured id: positive tenants are non-default"
        );
        assert!(!is_default_tenant_of(Some(-3), 0));
    }

    #[test]
    fn record_counts_per_surface() {
        let before = app_missing_count("unit_test_surface");
        record_app_missing("unit_test_surface", "chat.send", Some(9));
        assert_eq!(app_missing_count("unit_test_surface"), before + 1);
    }

    use crate::scope_resolver::{
        FetchError, RawScope, ResolverConfig, ResolverMetrics, ScopeResolver, ScopeSource,
    };
    use penguin_spine::{PlatformEvent, Source};
    use std::sync::Arc;

    struct Fixed(i64);
    impl ScopeSource for Fixed {
        async fn fetch(&self, _p: &str, _s: &str) -> Result<RawScope, FetchError> {
            Ok(RawScope {
                tenant_id: self.0,
                community_id: 9,
                tenant_slug: "acme".to_string(),
                community_name: "main".to_string(),
            })
        }
    }
    struct NoMetrics;
    impl ResolverMetrics for NoMetrics {
        fn scope_resolution(&self, _platform: &str, _outcome: &'static str) {}
        fn scope_mismatch(&self) {}
    }

    fn event(guild: &str) -> PlatformEvent {
        PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload: serde_json::Map::new(),
            occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
            source: Some(Source {
                platform: "discord".to_string(),
                account_id: guild.to_string(),
                channel_id: None,
            }),
        }
    }

    #[tokio::test]
    async fn env_gateway_carries_only_default_tenant_events() {
        let mk = |t| ScopeResolver::new(Fixed(t), ResolverConfig::default(), Arc::new(NoMetrics));
        let before = app_missing_count(SURFACE_GATEWAY_ENV);
        let own = mk(5);
        let foreign = mk(6);
        assert!(env_gateway_event_allowed(Some(&own), &event("111"), 5).await);
        assert!(!env_gateway_event_allowed(Some(&foreign), &event("111"), 5).await);
        assert!(
            !env_gateway_event_allowed(Some(&own), &event("111"), 0).await,
            "unconfigured default fails closed"
        );
        assert!(
            env_gateway_event_allowed(Some(&foreign), &event("dm"), 5).await,
            "no guild: left to publish"
        );
        assert!(env_gateway_event_allowed(None::<&ScopeResolver<Fixed>>, &event("111"), 5).await);
        assert_eq!(app_missing_count(SURFACE_GATEWAY_ENV), before + 2);
    }
}

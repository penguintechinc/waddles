//! `waddles.disable-unverified-binding-routing` -- opt-out kill-switch for routing
//! ownership-UNVERIFIED (legacy, pre-0103) bindings (user decision 2026-11-12).
//!
//! ```text
//!  unseen / OFF -> legacy: a sole unverified claimant still routes
//!  ON           -> unverified bindings never route (WARN + skip metric per binding)
//! ```
//!
//! Resolution mirrors `svc_action::flags::resolve_flag_with`: PostHog (when it has a
//! snapshot) -> `FLAG_WADDLES_DISABLE_UNVERIFIED_BINDING_ROUTING` env baseline -> OFF.
//! The service's hardcoded `*.waddles.app` license bypass reads EVERY flag ON; for this
//! kill-switch that would silently flip every environment, so bypass is treated as "no
//! PostHog value" (same fix as `svc_action::flags::DisableDbBundleConfigFlag`).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

/// The PostHog flag key.
pub const DISABLE_UNVERIFIED_BINDING_ROUTING_FLAG: &str =
    "waddles.disable-unverified-binding-routing";

/// The Docker ENV baseline variable for [`DISABLE_UNVERIFIED_BINDING_ROUTING_FLAG`].
pub const DISABLE_UNVERIFIED_BINDING_ROUTING_ENV: &str =
    "FLAG_WADDLES_DISABLE_UNVERIFIED_BINDING_ROUTING";

/// Boxed future returned by [`UnverifiedRoutingGate::disabled`].
pub type GateFuture<'a> = Pin<Box<dyn Future<Output = bool> + Send + 'a>>;

/// Whether unverified-binding routing is disabled (`true` = never route unverified).
/// Object-safe so the feed can hold `Arc<dyn UnverifiedRoutingGate>`.
pub trait UnverifiedRoutingGate: Send + Sync {
    /// `true` when the kill-switch is ON.
    fn disabled(&self) -> GateFuture<'_>;
}

/// Narrow view of the PostHog/license client used by [`resolve`].
pub trait FlagLookup: Send + Sync {
    /// Whether the deployment-domain license bypass is active.
    fn bypass_active(&self) -> bool;
    /// The flag's PostHog value, or `None` when no snapshot was ever fetched.
    fn posthog_value(&self) -> Pin<Box<dyn Future<Output = Option<bool>> + Send + '_>>;
}

impl FlagLookup for Arc<penguin_licensing::LicenseClient> {
    fn bypass_active(&self) -> bool {
        penguin_licensing::LicenseClient::bypass_active(self)
    }

    fn posthog_value(&self) -> Pin<Box<dyn Future<Output = Option<bool>> + Send + '_>> {
        Box::pin(async move {
            self.snapshot()?;
            Some(
                self.flag_enabled(DISABLE_UNVERIFIED_BINDING_ROUTING_FLAG)
                    .await,
            )
        })
    }
}

/// Parses a `FLAG_*` env value (strict truthy/falsy set; anything else is ignored).
fn parse_env(raw: Option<&str>) -> Option<bool> {
    match raw?.trim().to_ascii_lowercase().as_str() {
        "true" | "1" | "on" | "yes" => Some(true),
        "false" | "0" | "off" | "no" => Some(false),
        _ => None,
    }
}

/// Resolves the kill-switch: PostHog (non-bypass, snapshot present) -> env -> OFF.
/// Returns the value and its source label (`posthog`/`env`/`default`).
pub async fn resolve(source: Option<&dyn FlagLookup>, env: Option<&str>) -> (bool, &'static str) {
    if let Some(s) = source.filter(|s| !s.bypass_active()) {
        if let Some(v) = s.posthog_value().await {
            return (v, "posthog");
        }
    }
    match parse_env(env) {
        Some(v) => (v, "env"),
        None => (false, "default"),
    }
}

/// Production gate: the process-wide license client (if built) plus the env baseline,
/// re-read on every discovery so a flip takes effect on the next reconcile.
#[derive(Default)]
pub struct LicenseUnverifiedGate;

impl UnverifiedRoutingGate for LicenseUnverifiedGate {
    fn disabled(&self) -> GateFuture<'_> {
        Box::pin(async move {
            let client = crate::license::shared_license_client();
            let env = std::env::var(DISABLE_UNVERIFIED_BINDING_ROUTING_ENV).ok();
            let (v, source) = resolve(
                client.as_ref().map(|c| c as &dyn FlagLookup),
                env.as_deref(),
            )
            .await;
            tracing::debug!(
                flag = DISABLE_UNVERIFIED_BINDING_ROUTING_FLAG,
                value = v,
                source,
                "unverified-binding routing kill-switch resolved"
            );
            v
        })
    }
}

/// Fixed-value gate (tests).
pub struct StaticGate(pub bool);

impl UnverifiedRoutingGate for StaticGate {
    fn disabled(&self) -> GateFuture<'_> {
        let v = self.0;
        Box::pin(async move { v })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Fake {
        bypass: bool,
        value: Option<bool>,
    }

    impl FlagLookup for Fake {
        fn bypass_active(&self) -> bool {
            self.bypass
        }
        fn posthog_value(&self) -> Pin<Box<dyn Future<Output = Option<bool>> + Send + '_>> {
            let v = self.value;
            Box::pin(async move { v })
        }
    }

    #[test]
    fn env_var_name_follows_the_flag_convention() {
        let derived: String = DISABLE_UNVERIFIED_BINDING_ROUTING_FLAG
            .chars()
            .map(|c| {
                if c == '.' || c == '-' {
                    '_'
                } else {
                    c.to_ascii_uppercase()
                }
            })
            .collect();
        assert_eq!(
            format!("FLAG_{derived}"),
            DISABLE_UNVERIFIED_BINDING_ROUTING_ENV
        );
    }

    #[tokio::test]
    async fn unseen_everywhere_is_off() {
        assert_eq!(resolve(None, None).await, (false, "default"));
        let never = Fake {
            bypass: false,
            value: None,
        };
        assert_eq!(resolve(Some(&never), None).await, (false, "default"));
        assert_eq!(resolve(None, Some("garbage")).await, (false, "default"));
    }

    #[tokio::test]
    async fn env_baseline_applies_without_posthog() {
        assert_eq!(resolve(None, Some("true")).await, (true, "env"));
        assert_eq!(resolve(None, Some(" ON ")).await, (true, "env"));
        assert_eq!(resolve(None, Some("0")).await, (false, "env"));
    }

    #[tokio::test]
    async fn posthog_wins_over_env() {
        let on = Fake {
            bypass: false,
            value: Some(true),
        };
        assert_eq!(resolve(Some(&on), Some("false")).await, (true, "posthog"));
        let off = Fake {
            bypass: false,
            value: Some(false),
        };
        assert_eq!(resolve(Some(&off), Some("true")).await, (false, "posthog"));
    }

    #[tokio::test]
    async fn license_bypass_never_flips_the_kill_switch() {
        // regression: the *.waddles.app bypass reads every PostHog flag ON.
        let bypass = Fake {
            bypass: true,
            value: Some(true),
        };
        assert_eq!(resolve(Some(&bypass), None).await, (false, "default"));
        assert_eq!(resolve(Some(&bypass), Some("on")).await, (true, "env"));
    }

    #[tokio::test]
    async fn static_gate_reports_its_value() {
        assert!(StaticGate(true).disabled().await);
        assert!(!StaticGate(false).disabled().await);
    }
}

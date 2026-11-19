//! The `waddles.analytics-stream` consumer-enable flag. Data collection is an explicit
//! opt-in per environment: the flag is ON only via the ENV baseline
//! (`FLAG_WADDLES_ANALYTICS_STREAM`, chart value `config.analyticsStreamEnabled`) or the
//! PostHog flag -- and NEVER because of a license-bypass domain (flags are not license
//! features; this is the exact rule the svc-ingest producer applies, so consumer and
//! producer cannot disagree). No client, an unreachable flag server, a never-seen flag, or a
//! bypass-active client all mean OFF, in which case the consumer idles but the service stays
//! healthy. Checked every loop iteration (the client serves a cached snapshot, no inline
//! network I/O), so flipping the flag off is a live kill-switch.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// Flag key (`{product}.{feature}`), default OFF.
pub const FLAG_KEY: &str = "waddles.analytics-stream";
/// Docker/chart ENV baseline for [`FLAG_KEY`].
pub const ENV_BASELINE: &str = "FLAG_WADDLES_ANALYTICS_STREAM";

const PRODUCT: &str = "waddles";

/// Whether an ENV baseline value turns the flag ON.
pub fn env_on(value: Option<&str>) -> bool {
    matches!(
        value.map(str::trim),
        Some("1" | "true" | "TRUE" | "True" | "yes" | "on")
    )
}

/// Flag source.
#[derive(Clone)]
pub enum FlagGate {
    /// PostHog via the license client, plus the ENV baseline.
    Licensed {
        /// The client (absent => PostHog never consulted).
        client: Option<Arc<penguin_licensing::LicenseClient>>,
        /// The ENV baseline already evaluated at startup.
        env_on: bool,
    },
    /// Fixed value (tests).
    Fixed(Arc<AtomicBool>),
}

impl FlagGate {
    /// A fixed gate.
    pub fn fixed(v: bool) -> Self {
        Self::Fixed(Arc::new(AtomicBool::new(v)))
    }

    /// Builds the gate from the standard license environment and the ENV baseline. A license
    /// configuration problem leaves only the ENV baseline (default OFF). No bypass domain is
    /// ever registered.
    pub fn from_env() -> Self {
        let env_on = env_on(std::env::var(ENV_BASELINE).ok().as_deref());
        let client = match penguin_licensing::LicenseConfig::from_env(PRODUCT) {
            Ok(cfg) => match penguin_licensing::LicenseClient::new(cfg) {
                Ok(client) => {
                    client.spawn_refresh();
                    Some(client)
                }
                Err(err) => {
                    tracing::warn!(error = %err, "license client construction failed; {FLAG_KEY} follows the ENV baseline only");
                    None
                }
            },
            Err(err) => {
                tracing::warn!(error = %err, "license config invalid; {FLAG_KEY} follows the ENV baseline only");
                None
            }
        };
        Self::Licensed { client, env_on }
    }

    /// Whether the consumer is enabled right now.
    pub async fn enabled(&self) -> bool {
        match self {
            Self::Licensed { client, env_on } => {
                // Precedence: PostHog value if seen (an explicit OFF beats the ENV baseline),
                // else the ENV baseline, else OFF. Bypass domains are license-only (2026-11-12):
                // `flag_enabled` evaluates the PostHog snapshot normally (penguin-libs ad096f5d)
                // but has no ENV baseline, so read the snapshot directly (refreshed by
                // `spawn_refresh`) and fall back to the ENV baseline here.
                client
                    .as_ref()
                    .and_then(|c| c.snapshot())
                    .and_then(|snap| snap.flags.get(FLAG_KEY).copied())
                    .unwrap_or(*env_on)
            }
            Self::Fixed(v) => v.load(Ordering::Relaxed),
        }
    }

    /// Sets a fixed gate (no-op for a licensed gate); used by tests.
    pub fn set_fixed(&self, v: bool) {
        if let Self::Fixed(a) = self {
            a.store(v, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    include!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../test_support/licensing_mock.rs"
    ));

    // regression: env must not beat an explicit PostHog OFF (3b)
    #[tokio::test]
    async fn explicit_posthog_off_beats_env_baseline_true() {
        crate::crypto::ensure_installed();
        let c = seeded_client(r#"{"waddles.analytics-stream":false}"#, false).await;
        let g = FlagGate::Licensed {
            client: Some(c),
            env_on: true,
        };
        assert!(!g.enabled().await, "explicit PostHog OFF beats FLAG_*=true");
    }

    #[tokio::test]
    async fn explicit_posthog_on_and_unseen_key_follow_posthog_then_env() {
        crate::crypto::ensure_installed();
        let on = seeded_client(r#"{"waddles.analytics-stream":true}"#, false).await;
        let g = FlagGate::Licensed {
            client: Some(on),
            env_on: false,
        };
        assert!(g.enabled().await, "an explicit PostHog ON enables it");
        let unseen = seeded_client(r#"{"waddles.other":true}"#, false).await;
        let g = FlagGate::Licensed {
            client: Some(unseen.clone()),
            env_on: true,
        };
        assert!(g.enabled().await, "unseen key falls to the ENV baseline");
        let g = FlagGate::Licensed {
            client: Some(unseen),
            env_on: false,
        };
        assert!(!g.enabled().await, "unseen, no baseline -> OFF");
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_unset_flag_is_off() {
        crate::crypto::ensure_installed();
        let c = seeded_client(r#"{"waddles.other":true}"#, true).await;
        assert!(c.bypass_active());
        let g = FlagGate::Licensed {
            client: Some(c),
            env_on: false,
        };
        assert!(!g.enabled().await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_explicitly_off_flag_stays_off() {
        crate::crypto::ensure_installed();
        let c = seeded_client(r#"{"waddles.analytics-stream":false}"#, true).await;
        assert!(c.bypass_active());
        let g = FlagGate::Licensed {
            client: Some(c),
            env_on: false,
        };
        assert!(!g.enabled().await);
    }

    // regression: bypass domains are license-only (2026-11-12 decision)
    #[tokio::test]
    async fn bypass_client_license_check_passes() {
        crate::crypto::ensure_installed();
        let c = seeded_client("{}", true).await;
        assert!(c.bypass_active());
        assert!(c.check_feature("any-licensed-feature").await);
        assert!(c.check_tier(penguin_licensing::Tier::Enterprise).await);
    }

    fn client(bypass: bool) -> Arc<penguin_licensing::LicenseClient> {
        crate::crypto::ensure_installed();
        let mut cfg = penguin_licensing::LicenseConfig::new("t").unwrap();
        if bypass {
            cfg = cfg
                .with_bypass_domain("waddles.app")
                .with_deployment_domain("svc-analytics.waddles.app");
        }
        penguin_licensing::LicenseClient::new(cfg).unwrap()
    }

    #[tokio::test]
    async fn fixed_gate_toggles() {
        let g = FlagGate::fixed(false);
        assert!(!g.enabled().await);
        g.set_fixed(true);
        assert!(g.enabled().await);
    }

    #[test]
    fn env_baseline_values() {
        for on in ["1", "true", "TRUE", "True", "yes", "on", " true "] {
            assert!(env_on(Some(on)), "{on}");
        }
        for off in ["", "0", "false", "off", "no", "2"] {
            assert!(!env_on(Some(off)), "{off}");
        }
        assert!(!env_on(None));
    }

    #[tokio::test]
    async fn default_is_off_without_client_and_with_a_never_seen_flag() {
        assert!(
            !FlagGate::Licensed {
                client: None,
                env_on: false
            }
            .enabled()
            .await
        );
        let g = FlagGate::Licensed {
            client: Some(client(false)),
            env_on: false,
        };
        assert!(!g.enabled().await, "never-seen flag defaults OFF");
    }

    #[tokio::test]
    async fn domain_bypass_never_turns_collection_on() {
        // regression: bypass domains are license-only (2026-11-12 decision)
        let c = client(true);
        assert!(
            c.bypass_active(),
            "precondition: bypass is active on this client"
        );
        assert!(
            !c.flag_enabled(FLAG_KEY).await,
            "bypass no longer forces feature flags (penguin-licensing ad096f5d)"
        );
        let g = FlagGate::Licensed {
            client: Some(c),
            env_on: false,
        };
        assert!(
            !g.enabled().await,
            "but data collection stays OFF on a bypass domain"
        );
    }

    #[tokio::test]
    async fn env_baseline_is_the_explicit_opt_in() {
        assert!(
            FlagGate::Licensed {
                client: None,
                env_on: true
            }
            .enabled()
            .await
        );
        let g = FlagGate::Licensed {
            client: Some(client(true)),
            env_on: true,
        };
        assert!(g.enabled().await);
    }

    #[tokio::test]
    async fn from_env_defaults_off() {
        crate::crypto::ensure_installed();
        let g = FlagGate::from_env();
        assert!(matches!(g, FlagGate::Licensed { .. }));
        assert!(!g.enabled().await);
        g.set_fixed(true); // no-op on a licensed gate
        assert!(!g.enabled().await);
    }
}

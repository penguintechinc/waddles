//! Egress detokenization: the PII boundary's outbound half for platform
//! sends (the user rule: only the gateway -- svc-ingest -- and
//! svc-presentation may ever hold true PII).
//!
//! Every producer upstream (svc-process, svc-action, bundles) works with
//! `{user:<token>}` placeholders only. The outbound drain calls
//! [`Egress::prepare`] **immediately before each platform send**: one
//! batched, read-only Postgres lookup per message under svc-ingest's own
//! column-scoped role ([`crate::display_names`]; hub-api is the control-plane
//! writer and stays out of the hot path), the names substituted with
//! per-sink escaping, and the result wrapped in a
//! [`DetokenizedAction`] -- the only action type an executor or platform
//! sender accepts. Nothing is cached: the resolved names live in that one
//! value and are dropped when the send returns.
//!
//! **Never fails open.** Kill switch ON (`waddles.core.disable-pii-detokenization`
//! or `PII_DETOKENIZATION_ENABLED=false`), no resolver configured, no tenant
//! on the entry, or a resolver error all send the TOKENS unchanged -- never a
//! guessed name, never a partial substitution.
//!
//! Logs, metrics and errors carry fixed outcome codes and counts only; no
//! name, token or message text.

use std::ops::Deref;
use std::sync::{Arc, LazyLock, OnceLock};

use std::time::Duration;

use egress_detokenizer::{DetokenizedText, DisplayNameResolver, Sink};

use crate::display_names::PgDisplayNameResolver;

use crate::config::CliConfig;
use crate::outbound_ops::OutboundAction;

/// Opt-out kill-switch flag: unseen/OFF/unreachable leaves detokenization
/// ENABLED (a core platform mechanism, not a licensed feature); ON sends the
/// tokens unchanged.
pub const DISABLE_PII_DETOKENIZATION_FLAG: &str = "waddles.core.disable-pii-detokenization";

/// Per-query budget for the display-name lookup (connect, acquire, query).
pub const DISPLAY_NAME_DB_TIMEOUT: Duration = Duration::from_secs(2);

static DETOKENIZED: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_egress_detokenize_total",
            "Outbound sends passed through the egress detokenizer, by fixed outcome code",
        ),
        &["outcome"],
    )
    .expect("valid metric definition")
});

/// Registers the egress counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(DETOKENIZED.clone()))
}

/// Count of one outcome (test/observability helper).
#[must_use]
pub fn outcome_count(outcome: &str) -> u64 {
    DETOKENIZED.with_label_values(&[outcome]).get()
}

/// Where the kill switch is read from.
pub enum KillSwitch {
    /// A fixed answer: `true` = detokenization enabled (env override, tests).
    Fixed(bool),
    /// The PostHog-backed license client: bypass domain => enabled, otherwise
    /// enabled unless [`DISABLE_PII_DETOKENIZATION_FLAG`] is ON.
    License(Arc<penguin_licensing::LicenseClient>),
}

impl KillSwitch {
    async fn detokenization_enabled(&self) -> bool {
        match self {
            Self::Fixed(enabled) => *enabled,
            Self::License(client) => {
                if client.bypass_active() {
                    return true;
                }
                !client.flag_enabled(DISABLE_PII_DETOKENIZATION_FLAG).await
            }
        }
    }
}

/// An outbound action whose `chat.send`/`dm.send` text has been through the
/// egress pass. Executors and platform senders take this type, so a send path
/// cannot be handed text that skipped it. Derefs to the inner
/// [`OutboundAction`] (with the substituted text) for everything that only
/// reads the action.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetokenizedAction {
    action: OutboundAction,
    text: Option<DetokenizedText>,
}

impl DetokenizedAction {
    /// The detokenized message text of a `chat.send`/`dm.send`; `None` for ops
    /// that carry no free text.
    #[must_use]
    pub fn text(&self) -> Option<&DetokenizedText> {
        self.text.as_ref()
    }

    /// Wraps `action` with its text exactly as received (tokens stay tokens).
    /// For tests and for actions that never carry user text.
    #[must_use]
    pub fn tokens_unchanged(action: OutboundAction) -> Self {
        let text = match &action {
            OutboundAction::ChatSend { text, .. } | OutboundAction::DmSend { text, .. } => {
                Some(egress_detokenizer::tokens_unchanged(text))
            }
            _ => None,
        };
        Self { action, text }
    }
}

impl Deref for DetokenizedAction {
    type Target = OutboundAction;

    fn deref(&self) -> &OutboundAction {
        &self.action
    }
}

/// The per-process egress pass: a resolver (or none) plus the kill switch.
pub struct Egress {
    resolver: Option<Arc<dyn DisplayNameResolver>>,
    kill_switch: KillSwitch,
}

impl Egress {
    /// Never resolves: every send carries its tokens unchanged.
    #[must_use]
    pub fn disabled() -> Self {
        Self {
            resolver: None,
            kill_switch: KillSwitch::Fixed(false),
        }
    }

    /// An egress pass over `resolver`, gated by `kill_switch`.
    #[must_use]
    pub fn new(resolver: Arc<dyn DisplayNameResolver>, kill_switch: KillSwitch) -> Self {
        Self {
            resolver: Some(resolver),
            kill_switch,
        }
    }

    /// Builds the production pass from the process config: the
    /// `PII_DETOKENIZATION_ENABLED=false` env override disables it (loudly);
    /// an unset `DATABASE_URL` leaves it without a resolver (ERROR -- tokens
    /// are sent); otherwise a lazily-connecting Postgres resolver under the
    /// PostHog kill switch. `database_url` comes from the environment only
    /// (it carries the role's password) and is never logged.
    #[must_use]
    pub fn from_cli(
        cli: &CliConfig,
        license: Option<Arc<penguin_licensing::LicenseClient>>,
        database_url: Option<String>,
    ) -> Self {
        if cli.pii_detokenization_enabled_override == Some(false) {
            tracing::warn!(
                "PII_DETOKENIZATION_ENABLED=false env override set; outbound sends carry \
                 {{user:<token>}} placeholders (operator escape hatch for dev/air-gapped \
                 deployments; never set in a production tenant)"
            );
            return Self::disabled();
        }
        let Some(url) = database_url.filter(|u| !u.trim().is_empty()) else {
            tracing::error!(
                "DATABASE_URL unset; no display-name resolver, so outbound sends carry \
                 {{user:<token>}} placeholders"
            );
            return Self::disabled();
        };
        let resolver = Arc::new(PgDisplayNameResolver::new(url, DISPLAY_NAME_DB_TIMEOUT));
        let kill_switch = match license {
            Some(client) => KillSwitch::License(client),
            None => KillSwitch::Fixed(true),
        };
        Self::new(resolver, kill_switch)
    }

    /// Runs the egress pass for one entry, immediately before its platform
    /// send. `platform` picks the escaping sink; a `chat.send`/`dm.send` text
    /// is resolved under the entry's MAC-bound `tenant_id`. Every other op
    /// passes through unchanged.
    pub async fn prepare(&self, platform: &str, action: OutboundAction) -> DetokenizedAction {
        let sink = match platform {
            "discord" => Sink::Discord,
            _ => Sink::Twitch,
        };
        match action {
            OutboundAction::ChatSend {
                channel,
                text,
                origin_channel,
                tenant_id,
            } => {
                let dt = self.detokenize(&text, tenant_id, sink).await;
                DetokenizedAction {
                    action: OutboundAction::ChatSend {
                        channel,
                        text: dt.as_str().to_string(),
                        origin_channel,
                        tenant_id,
                    },
                    text: Some(dt),
                }
            }
            OutboundAction::DmSend {
                user_id,
                text,
                origin_channel,
                tenant_id,
            } => {
                let dt = self.detokenize(&text, tenant_id, sink).await;
                DetokenizedAction {
                    action: OutboundAction::DmSend {
                        user_id,
                        text: dt.as_str().to_string(),
                        origin_channel,
                        tenant_id,
                    },
                    text: Some(dt),
                }
            }
            other => DetokenizedAction {
                action: other,
                text: None,
            },
        }
    }

    async fn detokenize(&self, text: &str, tenant_id: Option<i64>, sink: Sink) -> DetokenizedText {
        let has_tokens = !egress_detokenizer::extract_tokens(text).is_empty();
        if !has_tokens {
            DETOKENIZED.with_label_values(&["no_tokens"]).inc();
            return egress_detokenizer::tokens_unchanged(text);
        }
        let Some(resolver) = self.resolver.as_deref() else {
            DETOKENIZED.with_label_values(&["no_resolver"]).inc();
            return egress_detokenizer::tokens_unchanged(text);
        };
        if !self.kill_switch.detokenization_enabled().await {
            DETOKENIZED.with_label_values(&["kill_switch"]).inc();
            return egress_detokenizer::tokens_unchanged(text);
        }
        let Some(tenant) = tenant_id.filter(|t| *t > 0) else {
            DETOKENIZED.with_label_values(&["no_tenant"]).inc();
            tracing::warn!("egress entry carries no tenant; sending tokens unchanged");
            return egress_detokenizer::tokens_unchanged(text);
        };
        let out = egress_detokenizer::detokenize_for_egress(
            text,
            &tenant.to_string(),
            Some(resolver),
            sink,
        )
        .await;
        let outcome = if out.as_str() == text {
            "tokens_sent"
        } else {
            "resolved"
        };
        DETOKENIZED.with_label_values(&[outcome]).inc();
        out
    }
}

static INSTALLED: OnceLock<Arc<Egress>> = OnceLock::new();

/// Installs the process-wide egress pass (startup, before any drain starts).
/// A second install is ignored.
pub fn install(egress: Egress) {
    if INSTALLED.set(Arc::new(egress)).is_err() {
        tracing::warn!("egress detokenizer already installed; keeping the first");
    }
}

/// The installed egress pass. If none was installed (a start path that skipped
/// startup wiring) this is [`Egress::disabled`], loudly: the drain then sends
/// tokens, never names.
#[must_use]
pub fn installed() -> Arc<Egress> {
    if let Some(e) = INSTALLED.get() {
        return Arc::clone(e);
    }
    tracing::error!("egress detokenizer not installed; outbound sends carry tokens");
    Arc::new(Egress::disabled())
}

#[cfg(test)]
mod tests {
    use super::*;
    use egress_detokenizer::DetokenizeError;
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    /// Counts calls and records the tenant each one named; resolves from `names`, or fails.
    struct CountingResolver {
        calls: AtomicUsize,
        tenants: Mutex<Vec<String>>,
        names: HashMap<String, String>,
        fail: bool,
    }

    impl CountingResolver {
        fn new(names: &[(&str, &str)], fail: bool) -> Arc<Self> {
            Arc::new(Self {
                calls: AtomicUsize::new(0),
                tenants: Mutex::new(Vec::new()),
                names: names
                    .iter()
                    .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                    .collect(),
                fail,
            })
        }

        fn calls(&self) -> usize {
            self.calls.load(Ordering::SeqCst)
        }
    }

    impl DisplayNameResolver for CountingResolver {
        fn resolve_many<'a>(
            &'a self,
            tenant_id: &'a str,
            tokens: Vec<String>,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<HashMap<String, String>, DetokenizeError>>
                    + Send
                    + 'a,
            >,
        > {
            self.calls.fetch_add(1, Ordering::SeqCst);
            self.tenants.lock().unwrap().push(tenant_id.to_string());
            Box::pin(async move {
                if self.fail {
                    return Err(DetokenizeError::ResolutionUnavailable("down".to_string()));
                }
                Ok(tokens
                    .into_iter()
                    .filter_map(|t| self.names.get(&t).cloned().map(|n| (t, n)))
                    .collect())
            })
        }
    }

    fn chat(text: &str, tenant_id: Option<i64>) -> OutboundAction {
        OutboundAction::ChatSend {
            channel: "123456789012345678".to_string(),
            text: text.to_string(),
            origin_channel: Some("123456789012345678".to_string()),
            tenant_id,
        }
    }

    fn text_of(d: &DetokenizedAction) -> &str {
        d.text().expect("chat text").as_str()
    }

    #[tokio::test]
    async fn chat_text_is_resolved_per_send_under_the_stamped_tenant_and_escaped() {
        let r = CountingResolver::new(&[("u1", "a_b*")], false);
        let e = Egress::new(r.clone(), KillSwitch::Fixed(true));
        let d = e.prepare("discord", chat("hi {user:u1}", Some(7))).await;
        assert_eq!(text_of(&d), "hi a\\_b\\*");
        assert!(matches!(&*d, OutboundAction::ChatSend { text, .. } if text == "hi a\\_b\\*"));
        // Nothing cached: a second send asks again.
        e.prepare("discord", chat("hi {user:u1}", Some(7))).await;
        assert_eq!(r.calls(), 2);
        assert_eq!(r.tenants.lock().unwrap().as_slice(), ["7", "7"]);
    }

    #[tokio::test]
    async fn twitch_sink_strips_control_characters_from_the_name() {
        let r = CountingResolver::new(&[("u1", "bo\r\nb")], false);
        let e = Egress::new(r, KillSwitch::Fixed(true));
        let d = e.prepare("twitch", chat("hi {user:u1}", Some(7))).await;
        assert_eq!(text_of(&d), "hi bob");
    }

    /// Contract: every path that is not a resolvable platform text send sends the tokens and
    /// makes ZERO resolver calls.
    #[tokio::test]
    async fn non_resolving_paths_send_tokens_with_zero_resolver_calls() {
        let r = CountingResolver::new(&[("u1", "Alice")], false);
        let live = Egress::new(r.clone(), KillSwitch::Fixed(true));
        let killed = Egress::new(r.clone(), KillSwitch::Fixed(false));
        let tokens = "hi {user:u1}";

        // Kill switch ON.
        let d = killed.prepare("discord", chat(tokens, Some(7))).await;
        assert_eq!(text_of(&d), tokens);
        // No tenant on the entry.
        let d = live.prepare("discord", chat(tokens, None)).await;
        assert_eq!(text_of(&d), tokens);
        // Text with no placeholders.
        let d = live.prepare("discord", chat("plain", Some(7))).await;
        assert_eq!(text_of(&d), "plain");
        // Ops that carry no free text (destination is not a text send).
        let del = OutboundAction::ChatDelete {
            channel: "1".to_string(),
            message_id: "2".to_string(),
            origin_channel: Some("1".to_string()),
        };
        let d = live.prepare("discord", del.clone()).await;
        assert_eq!(*d, del);
        assert!(d.text().is_none());
        // No resolver configured at all.
        let d = Egress::disabled()
            .prepare("discord", chat(tokens, Some(7)))
            .await;
        assert_eq!(text_of(&d), tokens);

        assert_eq!(r.calls(), 0, "none of these paths may touch the resolver");
    }

    #[tokio::test]
    async fn resolver_failure_sends_tokens_never_a_name() {
        let r = CountingResolver::new(&[("u1", "Alice")], true);
        let e = Egress::new(r.clone(), KillSwitch::Fixed(true));
        let d = e.prepare("discord", chat("hi {user:u1}", Some(7))).await;
        assert_eq!(text_of(&d), "hi {user:u1}");
        assert_eq!(r.calls(), 1);
    }

    #[tokio::test]
    async fn dm_text_is_resolved_too() {
        let r = CountingResolver::new(&[("u1", "Alice")], false);
        let e = Egress::new(r, KillSwitch::Fixed(true));
        let dm = OutboundAction::DmSend {
            user_id: "42".to_string(),
            text: "yo {user:u1}".to_string(),
            origin_channel: "1".to_string(),
            tenant_id: Some(9),
        };
        let d = e.prepare("discord", dm).await;
        assert_eq!(text_of(&d), "yo Alice");
    }

    #[tokio::test]
    async fn env_override_false_builds_a_disabled_pass() {
        use clap::Parser;
        let cli = CliConfig::parse_from([
            "svc-ingest",
            "--pii-detokenization-enabled-override",
            "false",
        ]);
        let e = Egress::from_cli(&cli, None, Some("postgresql://x@127.0.0.1:1/db".into()));
        assert!(e.resolver.is_none());
        let d = e.prepare("discord", chat("hi {user:u1}", Some(7))).await;
        assert_eq!(text_of(&d), "hi {user:u1}");
    }

    #[tokio::test]
    async fn unset_database_url_builds_a_disabled_pass() {
        use clap::Parser;
        let cli = CliConfig::parse_from(["svc-ingest"]);
        assert!(Egress::from_cli(&cli, None, None).resolver.is_none());
        assert!(Egress::from_cli(&cli, None, Some("  ".into()))
            .resolver
            .is_none());
        assert!(
            Egress::from_cli(&cli, None, Some("postgresql://x@h/db".into()))
                .resolver
                .is_some()
        );
    }
}

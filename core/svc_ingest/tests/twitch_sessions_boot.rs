//! Startup wiring for dynamic Twitch sessions: mode dispatch, fail-loud
//! prerequisites and manager task lifecycle. Own integration-test binary
//! (own process) so the thread-local log capture below cannot race unit
//! tests.

use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use clap::Parser;
use connection_resolver::{
    BindingPage, ConnectionRef, ConnectionResolve, ResolveError, ResolvedConnection,
};
use svc_ingest::config::{CliConfig, TwitchMode};
use svc_ingest::dispatch_twitch_mode;
use svc_ingest::twitch_sessions::boot::{
    manager_exit_is_fatal, spawn_manager_task, validate_dynamic_prereqs,
};
use svc_ingest::twitch_sessions::{
    DiscoveryOutcome, LeaseError, LeaseOutcome, ReconcileReport, SessionDeps, SessionEnd,
    SessionLease, SessionManager, SessionMetrics, SessionRunner, SessionSpec,
};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

fn cli(args: &[&str]) -> CliConfig {
    let mut full = vec!["svc-ingest"];
    full.extend_from_slice(args);
    CliConfig::parse_from(full)
}

#[test]
fn dynamic_without_service_jwt_endpoint_is_fatal() {
    let c = cli(&["--hub-api-url", "https://hub.example"]);
    let err = validate_dynamic_prereqs(&c, true).expect_err("endpoint unset must be fatal");
    let text = err.to_string();
    assert!(text.contains("SERVICE_JWT_TOKEN_ENDPOINT"), "{text}");
}

#[test]
fn dynamic_with_plaintext_hub_outside_alpha_is_fatal() {
    for env in ["beta", "production", ""] {
        let c = cli(&[
            "--hub-api-url",
            "http://hub-api:8204",
            "--service-jwt-token-endpoint",
            "http://hub-api:8204/internal/service-token",
            "--environment",
            env,
        ]);
        assert!(
            validate_dynamic_prereqs(&c, true).is_err(),
            "plaintext hub must be refused in env {env:?}"
        );
    }
    // Alpha tolerates plaintext; https is fine anywhere.
    let alpha = cli(&[
        "--hub-api-url",
        "http://hub-api:8204",
        "--service-jwt-token-endpoint",
        "http://x/internal/service-token",
        "--environment",
        "alpha",
    ]);
    assert!(validate_dynamic_prereqs(&alpha, true).is_ok());
}

#[test]
fn dynamic_without_scope_resolver_is_fatal() {
    let c = cli(&[
        "--hub-api-url",
        "https://hub.example",
        "--service-jwt-token-endpoint",
        "https://hub.example/internal/service-token",
    ]);
    let err = validate_dynamic_prereqs(&c, false).expect_err("resolver missing must be fatal");
    assert!(err.to_string().contains("scope resolver"), "{err}");
    assert!(validate_dynamic_prereqs(&c, true).is_ok());
}

#[test]
fn dynamic_mode_does_not_start_static_receiver() {
    let static_calls = AtomicU32::new(0);
    let dynamic_calls = AtomicU32::new(0);
    dispatch_twitch_mode(
        TwitchMode::Dynamic,
        || {
            static_calls.fetch_add(1, Ordering::SeqCst);
        },
        || {
            dynamic_calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        },
    )
    .expect("dynamic ok");
    assert_eq!(static_calls.load(Ordering::SeqCst), 0);
    assert_eq!(dynamic_calls.load(Ordering::SeqCst), 1);

    // A dynamic start failure propagates (fatal), never falls back to static.
    let r = dispatch_twitch_mode(
        TwitchMode::Dynamic,
        || {
            static_calls.fetch_add(1, Ordering::SeqCst);
        },
        || Err(anyhow::anyhow!("boom")),
    );
    assert!(r.is_err());
    assert_eq!(static_calls.load(Ordering::SeqCst), 0);

    // Off starts neither.
    dispatch_twitch_mode(
        TwitchMode::Off,
        || {
            static_calls.fetch_add(1, Ordering::SeqCst);
        },
        || {
            dynamic_calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        },
    )
    .expect("off ok");
    assert_eq!(static_calls.load(Ordering::SeqCst), 0);
    assert_eq!(dynamic_calls.load(Ordering::SeqCst), 1);
}

struct FieldText(String);
impl tracing::field::Visit for FieldText {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        self.0.push_str(&format!("{}={:?} ", field.name(), value));
    }
}

/// Minimal capturing subscriber: records `(level, rendered fields)` per event.
struct Capture(Arc<Mutex<Vec<(tracing::Level, String)>>>);
impl tracing::Subscriber for Capture {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        tracing::span::Id::from_u64(1)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, event: &tracing::Event<'_>) {
        let mut v = FieldText(String::new());
        event.record(&mut v);
        self.0
            .lock()
            .expect("lock")
            .push((*event.metadata().level(), v.0));
    }
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

#[test]
fn flag_off_with_complete_static_env_is_static_dev_and_warns() {
    let lines = Arc::new(Mutex::new(Vec::new()));
    let _g = tracing::subscriber::set_default(Capture(lines.clone()));
    let ran = AtomicU32::new(0);
    dispatch_twitch_mode(
        TwitchMode::StaticDev,
        || {
            ran.fetch_add(1, Ordering::SeqCst);
        },
        || Err(anyhow::anyhow!("dynamic must not run")),
    )
    .expect("static dev ok");
    assert_eq!(ran.load(Ordering::SeqCst), 1);
    let got = lines.lock().expect("lock");
    assert!(
        got.iter().any(|(lvl, text)| *lvl == tracing::Level::WARN
            && text.contains("single-tenant static identity")
            && text.contains("dev fallback")),
        "expected the dev-fallback WARN, got {got:?}"
    );
}

struct EmptyResolver;
impl ConnectionResolve for EmptyResolver {
    async fn resolve(&self, _: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Err(ResolveError::NotFound)
    }
    async fn list_bindings(
        &self,
        _: &str,
        _: Option<&str>,
        _: i64,
        _: u32,
    ) -> Result<BindingPage, ResolveError> {
        Ok(BindingPage {
            items: vec![],
            next_after: None,
        })
    }
}

struct NoLease;
impl SessionLease for NoLease {
    async fn acquire(&self, _: (i64, i64), _: Duration) -> Result<bool, LeaseError> {
        Ok(true)
    }
    async fn renew(&self, _: (i64, i64), _: Duration) -> Result<bool, LeaseError> {
        Ok(true)
    }
    async fn release(&self, _: (i64, i64)) -> Result<(), LeaseError> {
        Ok(())
    }
}

struct NoRunner;
impl SessionRunner for NoRunner {
    fn spawn(&self, _: SessionSpec, _: oneshot::Receiver<()>) -> JoinHandle<SessionEnd> {
        tokio::spawn(async { SessionEnd::Shutdown })
    }
}

#[derive(Default)]
struct CountMetrics {
    reconciles: AtomicU32,
}
impl SessionMetrics for CountMetrics {
    fn discovery(&self, _: DiscoveryOutcome) {}
    fn lease_event(&self, _: LeaseOutcome) {}
    fn session_ended(&self, _: SessionEnd) {}
    fn reconcile_done(&self, _: &ReconcileReport, _: Duration, _: usize) {
        self.reconciles.fetch_add(1, Ordering::SeqCst);
    }
    fn set_live_sessions(&self, _: usize) {}
    fn resolve_failed(&self, _: &'static str) {}
    fn discovery_skipped(&self, _: &'static str, _: u32) {}
}

#[tokio::test]
async fn manager_task_ends_on_process_shutdown() {
    let metrics = Arc::new(CountMetrics::default());
    let manager = SessionManager::new(
        Arc::new(EmptyResolver),
        Arc::new(NoLease),
        SessionDeps {
            runner: Arc::new(NoRunner),
            metrics: metrics.clone(),
        },
    )
    .with_timing(Duration::from_secs(10), Duration::from_secs(1));
    let (tx, rx) = oneshot::channel::<()>();
    let handle = spawn_manager_task(manager, async move {
        let _ = rx.await;
    });
    // The manager reconciles immediately on start.
    for _ in 0..200 {
        if metrics.reconciles.load(Ordering::SeqCst) >= 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(
        metrics.reconciles.load(Ordering::SeqCst) >= 1,
        "manager never reconciled"
    );
    assert!(!handle.is_finished(), "must keep running until shutdown");
    tx.send(()).expect("shutdown receiver alive");
    tokio::time::timeout(Duration::from_secs(5), handle)
        .await
        .expect("manager task must end on shutdown")
        .expect("manager task must not panic");
}

#[tokio::test]
async fn unexpected_manager_exit_is_fatal_but_clean_shutdown_is_not() {
    // Clean shutdown after a requested stop.
    let clean = tokio::spawn(async {}).await;
    assert!(!manager_exit_is_fatal(&clean, true));
    // Ended with no shutdown requested: fatal.
    assert!(manager_exit_is_fatal(&clean, false));
    // A panicking manager task is fatal even if shutdown was flagged.
    let panicked = tokio::spawn(async { panic!("boom") }).await;
    assert!(manager_exit_is_fatal(&panicked, true));
    assert!(manager_exit_is_fatal(&panicked, false));
}

#[tokio::test]
async fn try_start_with_missing_prereqs_fails_loud_and_never_marks_the_receiver_enabled() {
    use svc_ingest::config::Config;
    use svc_ingest::telemetry::register_ingest_metrics;
    use svc_ingest::twitch_sessions::boot::try_start_twitch_sessions;
    use svc_ingest::ReceiverReadiness;

    // Flag ON but no SERVICE_JWT_TOKEN_ENDPOINT: the process must refuse to start.
    let config =
        Config::from_cli(cli(&["--hub-api-url", "https://hub.example"])).expect("config from cli");
    let registry = prometheus::Registry::new();
    let metrics = Arc::new(register_ingest_metrics(&registry));
    let readiness = Arc::new(ReceiverReadiness::new());
    let dek_provider = svc_ingest::identity_crypto::ConfiguredDekProvider::HubApi(
        svc_ingest::identity_crypto::TtlCachedDekProvider::new(
            svc_ingest::identity_crypto::HubApiDekProvider::new(
                reqwest::Client::new(),
                "http://hub-api:8204",
                svc_ingest::identity_crypto::UnimplementedMachineJwtProvider,
                "svc_ingest",
            ),
            svc_ingest::identity_crypto::DEFAULT_DEK_CACHE_TTL,
        ),
    );
    let err = try_start_twitch_sessions(&config, metrics, readiness.clone(), dek_provider)
        .expect_err("missing prereqs must be a startup error, never a silent no-op");
    assert!(
        err.to_string().contains("SERVICE_JWT_TOKEN_ENDPOINT"),
        "{err}"
    );
    assert!(
        !readiness.twitch_irc.is_enabled(),
        "a failed start must not flag the receiver as enabled"
    );
}

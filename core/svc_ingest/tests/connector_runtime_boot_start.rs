//! The runtime's startup functions (`start_bridge_with`, `connect_tokenizer`,
//! `start_runtime`, `start_connector_with`) against REAL mTLS material, a REAL
//! Valkey and spine, with fakes only for hub-api (resolver, scope authority,
//! identity tokenizer). Success and failure paths; the failure paths prove a
//! missing prerequisite is a named fatal error and nothing is half-started.
//!
//! One test fn: it mutates process env and the runtime status is process-global.
//! Needs `VALKEY_URL` (unset FAILS).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use connection_resolver::{
    BindingPage, ConnectionRef, ConnectionResolve, ResolveError, ResolvedConnection,
};
use rcgen::{CertificateParams, DnType, IsCa, KeyPair};
use svc_ingest::config::{CliConfig, Config, Secret};
use svc_ingest::connector::host_api_listener::HostApiTls;
use svc_ingest::connector::host_calls::{CallScope, IdentityTokenizer, TokenizeError};
use svc_ingest::connector::webhook::BoxFut;
use svc_ingest::connector_runtime::boot::{
    connect_tokenizer, start_bridge, start_bridge_with, start_connector_with, start_runtime,
    ConnectorDeps, HostBridge, RuntimeSettings,
};
use svc_ingest::connector_runtime::scoped::ScopeLookup;
use svc_ingest::connector_runtime::selector::ConnectorSelector;
use svc_ingest::connector_runtime::webhook_boot::WebhookEnv;
use svc_ingest::connector_runtime::Readiness;
use svc_ingest::identity_crypto::{
    ConfiguredDekProvider, LocalDevDekProvider, TtlCachedDekProvider, DEFAULT_DEK_CACHE_TTL,
};
use svc_ingest::scope_resolver::{ResolvedScope, Unresolved};

struct EmptyHub;

impl ConnectionResolve for EmptyHub {
    async fn resolve(&self, _r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        Err(ResolveError::Malformed("unused"))
    }
    async fn list_bindings(
        &self,
        _p: &str,
        _c: Option<&str>,
        _a: i64,
        _l: u32,
    ) -> Result<BindingPage, ResolveError> {
        Ok(BindingPage {
            items: vec![],
            next_after: None,
        })
    }
}

struct NoScope;
impl ScopeLookup for NoScope {
    fn lookup<'a>(
        &'a self,
        _p: &'a str,
        _s: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        Box::pin(async { Err(Unresolved::NotFound) })
    }
}

struct NoTok;
impl IdentityTokenizer for NoTok {
    fn tokenize<'a>(
        &'a self,
        _s: &'a CallScope,
        _id: &'a str,
    ) -> BoxFut<'a, Result<String, TokenizeError>> {
        Box::pin(async { Err(TokenizeError::Unavailable) })
    }
}

fn write_pki(dir: &std::path::Path) -> HostApiTls {
    svc_ingest::crypto::ensure_installed();
    let mut ca_p = CertificateParams::new(Vec::<String>::new()).unwrap();
    ca_p.distinguished_name
        .push(DnType::CommonName, "boot-start-ca");
    ca_p.is_ca = IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let ca_k = KeyPair::generate().unwrap();
    let ca = ca_p.self_signed(&ca_k).unwrap();
    let issuer = rcgen::Issuer::new(ca_p, ca_k);
    let leaf_k = KeyPair::generate().unwrap();
    let leaf = CertificateParams::new(vec!["localhost".to_string()])
        .unwrap()
        .signed_by(&leaf_k, &issuer)
        .unwrap();
    let put = |name: &str, body: String| {
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        p
    };
    HostApiTls {
        server_cert: put("tls.crt", leaf.pem()),
        server_key: put("tls.key", leaf_k.serialize_pem()),
        client_ca: put("ca.crt", ca.pem()),
        client_identity: "spiffe://waddlebot/executor-connector".into(),
    }
}

fn config(valkey_kid: bool) -> Config {
    let mut cli = CliConfig::parse_from(["svc-ingest", "--environment", "alpha"]);
    cli.dynamic_connections = true;
    if valkey_kid {
        cli.binding_active_kid = "k1".into();
    }
    let mut c = Config::from_cli(cli).unwrap();
    if valkey_kid {
        c.envelope_binding_keys = Some(Secret::new("k1:0102030405060708090a0b0c0d0e0f10"));
    }
    c
}

fn dek() -> ConfiguredDekProvider<svc_ingest::identity_crypto::UnimplementedMachineJwtProvider> {
    ConfiguredDekProvider::LocalDev(TtlCachedDekProvider::new(
        LocalDevDekProvider::new("local", "BOOT_START_KEK").unwrap(),
        DEFAULT_DEK_CACHE_TTL,
    ))
}

fn lookup(map: HashMap<String, String>) -> impl Fn(&str) -> Option<String> + Sync {
    move |k| map.get(k).cloned()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn startup_functions_succeed_with_real_dependencies_and_fail_loud_without() {
    let valkey = std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: this test needs a REAL Valkey (no silent skip)");
    let dir = std::env::temp_dir().join(format!("boot-start-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let tls = write_pki(&dir);
    // SAFETY: the only test in this process, before other threads read env.
    unsafe {
        std::env::set_var("BOOT_START_KEK", "0123456789abcdef0123456789abcdef");
        std::env::set_var("SECURITY_TRANSPORT_TLS", "false");
        std::env::set_var("SECURITY_TRANSPORT_AUTH", "false");
    }
    // Senders are kept alive so the listeners keep serving (a dropped sender
    // resolves the receiver, which is the shutdown signal).
    let keep = std::sync::Mutex::new(Vec::new());
    let never = || {
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        keep.lock().unwrap().push(tx);
        rx
    };

    // ---- start_bridge_with: real mTLS listener ----
    let (bridge, bound) = start_bridge_with(
        &tls,
        "127.0.0.1:0".parse().unwrap(),
        Arc::new(NoTok),
        &["twitch"],
        never,
    )
    .await
    .expect("listener starts");
    assert_ne!(bound.port(), 0);
    assert!(!bridge.link.is_attached());
    tokio::net::TcpStream::connect(bound)
        .await
        .expect("the listener accepts TCP");
    // Failures: unreadable certificate, unknown platform, occupied port.
    let mut bad = tls.clone();
    bad.server_cert = dir.join("missing.crt");
    assert!(start_bridge_with(
        &bad,
        "127.0.0.1:0".parse().unwrap(),
        Arc::new(NoTok),
        &["twitch"],
        never
    )
    .await
    .is_err());
    assert!(start_bridge_with(
        &tls,
        "127.0.0.1:0".parse().unwrap(),
        Arc::new(NoTok),
        &["slack"],
        never
    )
    .await
    .is_err());
    let err = start_bridge_with(&tls, bound, Arc::new(NoTok), &["twitch"], never)
        .await
        .err()
        .expect("port already bound");
    assert!(err.to_string().contains("bind"), "{err}");

    // ---- connect_tokenizer / start_bridge: named failures ----
    let cli = config(true).cli;
    let no_env = lookup(HashMap::new());
    let err = connect_tokenizer(&cli, &no_env).await.err().expect("fails");
    assert!(err.to_string().contains("HUB_API_GRPC_ENDPOINT"), "{err}");
    let unreachable = lookup(HashMap::from([(
        "HUB_API_GRPC_ENDPOINT".to_owned(),
        "https://127.0.0.1:1".to_owned(),
    )]));
    assert!(connect_tokenizer(&cli, &unreachable).await.is_err());
    let env = |pairs: &[(&str, String)]| -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), v.clone()))
            .collect()
    };
    let host_api = env(&[
        (
            "HOST_API_SERVER_CERT_FILE",
            tls.server_cert.display().to_string(),
        ),
        (
            "HOST_API_SERVER_KEY_FILE",
            tls.server_key.display().to_string(),
        ),
        (
            "HOST_API_CLIENT_CA_FILE",
            tls.client_ca.display().to_string(),
        ),
        ("HOST_API_CLIENT_IDENTITY", tls.client_identity.clone()),
    ]);
    let err = start_bridge(&cli, &lookup(host_api.clone()), &["twitch"], never)
        .await
        .err()
        .expect("no hub endpoint");
    assert!(err.to_string().contains("HUB_API_GRPC_ENDPOINT"), "{err}");

    // ---- start_connector: the real wrapper refuses without the installed scope resolver ----
    let mut st0 = svc_ingest::http::AppState::new(config(true), prometheus::Registry::new());
    let m0 = Arc::new(svc_ingest::telemetry::register_ingest_metrics(
        &prometheus::Registry::new(),
    ));
    let sel0 = ConnectorSelector::from_env(&|k: &str| {
        (k == "CONNECTOR_TWITCH_ENABLED").then(|| "true".to_owned())
    })
    .unwrap();
    let w0 = Arc::new(WebhookEnv::from_lookup(&|_: &str| None).unwrap());
    let err = svc_ingest::connector_runtime::boot::start_connector(
        &config(true),
        &sel0,
        &w0,
        &mut st0,
        m0,
        dek(),
    )
    .await
    .err()
    .expect("no scope resolver installed");
    assert!(
        err.to_string().contains("dataplane scope resolver"),
        "{err}"
    );

    // ---- start_connector_with: failures before any side effect that matters ----
    let sel = ConnectorSelector::from_env(&|k: &str| {
        (k == "CONNECTOR_TWITCH_ENABLED").then(|| "true".to_owned())
    })
    .unwrap();
    let wenv = Arc::new(
        WebhookEnv::from_lookup(&|k: &str| match k {
            "CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS" => Some("discord".to_owned()),
            "CONNECTOR_ROUTE_KEY_SECRET" => Some("0123456789abcdef0123456789abcdef".to_owned()),
            _ => None,
        })
        .unwrap(),
    );
    let registry = prometheus::Registry::new();
    let mut state = svc_ingest::http::AppState::new(config(true), registry.clone());
    let metrics = Arc::new(svc_ingest::telemetry::register_ingest_metrics(
        &prometheus::Registry::new(),
    ));
    let mut full = host_api.clone();
    full.insert("SENDER_EXEC_DIGEST_TWITCH".into(), "sha256:twitch".into());
    let deps = |m: HashMap<String, String>| ConnectorDeps {
        resolver: Arc::new(EmptyHub),
        scope: Arc::new(NoScope) as Arc<dyn ScopeLookup>,
        tokenizer: Arc::new(NoTok) as Arc<dyn IdentityTokenizer>,
        lookup: Box::leak(Box::new(lookup(m))),
    };
    // Missing digest: named, nothing registered.
    let err = start_connector_with(
        &config(true),
        &sel,
        &wenv,
        &mut state,
        metrics.clone(),
        dek(),
        deps(host_api.clone()),
    )
    .await
    .err()
    .expect("missing digest");
    assert!(
        err.to_string().contains("SENDER_EXEC_DIGEST_TWITCH"),
        "{err}"
    );
    assert!(state.webhook.is_none());
    // Spine config absent (VALKEY_URL unset): named failure, still no ingress.
    // SAFETY: as above.
    unsafe { std::env::remove_var("VALKEY_URL") };
    let err = start_connector_with(
        &config(true),
        &sel,
        &wenv,
        &mut state,
        metrics.clone(),
        dek(),
        deps(full.clone()),
    )
    .await
    .err()
    .expect("no valkey url");
    assert!(err.to_string().contains("spine config"), "{err}");
    unsafe { std::env::set_var("VALKEY_URL", &valkey) };
    assert!(state.webhook.is_none());

    // ---- start_connector_with: success ----
    // Without a binding keyring the runtime refuses (named), AFTER the ingress
    // and listener exist -- so give it a fresh registry/state per attempt.
    let mut state_nokey =
        svc_ingest::http::AppState::new(config(false), prometheus::Registry::new());
    let mut nokey_env = full.clone();
    nokey_env.insert("SENDER_EXEC_HOST_API_PORT".into(), free_port().to_string());
    let err = start_connector_with(
        &config(false),
        &sel,
        &wenv,
        &mut state_nokey,
        metrics.clone(),
        dek(),
        deps(nokey_env),
    )
    .await
    .err()
    .expect("no keyring");
    assert!(err.to_string().contains("ENVELOPE_BINDING_KEYS"), "{err}");

    let mut ok_env = full.clone();
    ok_env.insert("SENDER_EXEC_HOST_API_PORT".into(), free_port().to_string());
    // Fresh registry: the failed attempts above already registered their series.
    let mut state = svc_ingest::http::AppState::new(config(true), prometheus::Registry::new());
    let started = start_connector_with(
        &config(true),
        &sel,
        &wenv,
        &mut state,
        metrics.clone(),
        dek(),
        deps(ok_env),
    )
    .await
    .expect("connector boot succeeds with real dependencies");
    assert!(state.webhook.is_some(), "ingress stored for the router");
    assert!(!started.link.is_attached());
    // The runtime reconciles (empty feed): after the first pass the platform reports
    // a state other than `Disabled`, and the status handle is installed.
    assert!(svc_ingest::connector_runtime::ready::installed().is_some());
    for _ in 0..100 {
        if !matches!(started.ready.ready("twitch"), Readiness::Disabled) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(!matches!(
        started.ready.ready("twitch"),
        Readiness::Disabled
    ));
    assert_eq!(started.ready.ready("youtube"), Readiness::Disabled);

    // ---- start_runtime: a second status install is refused, loudly ----
    let settings = RuntimeSettings::from_inputs(&config(true).cli, &sel, &lookup(full)).unwrap();
    let bridge2 = HostBridge::new(Arc::new(NoTok), &["twitch"]).unwrap();
    let redis = redis::Client::open(valkey.as_str()).unwrap();
    let err = start_runtime(
        &config(true),
        &settings,
        bridge2,
        Arc::new(EmptyHub),
        Arc::new(NoScope),
        None,
        &redis,
        penguin_spine::SpineConfig::from_env().unwrap(),
        metrics,
        dek(),
        prometheus::Registry::new(),
        Arc::new(NoTok),
    )
    .await
    .err()
    .expect("second install refused");
    assert!(err.to_string().contains("already installed"), "{err}");
    let _ = std::fs::remove_dir_all(&dir);
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

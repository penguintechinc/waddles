//! Live end-to-end: the Rust `ConnectionResolver` against a REAL hub-api over a REAL Postgres.
//!
//! `#[ignore]` by default -- these need the harness started by `scripts/ci/connection-resolver-live.sh`
//! (`make test-connection-resolver-live`), which boots Postgres 17 (Alembic head) + the hub-api
//! blueprints (`hub_api/tests/live/live_hub.py`) with identities rendered from the REAL Helm chart,
//! and exports `LIVE_HUB_MANIFEST` (a JSON file: hub url, admin JWT, community ids, SA tokens).
//! Nothing here is mocked: connections are seeded through the hub-api admin API, secrets are sealed
//! and opened by the real vault, tokens are minted by the real `/internal/service-token`.
//!
//! Each test prints one `LIVE_TELEMETRY logs=N spans=N metrics=N` line (resolver tracing events,
//! resolver spans, `ResolverMetrics` data points); the script sums them and fails on zero.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{
    ConnectionKind, ConnectionRef, ConnectionResolver, ConnectionSecret, ResolveError,
    ResolverConfig, ResolverMetrics, SCOPE_CONNECTIONS_RESOLVE_STATIC,
};
use serde_json::{json, Value};
use tracing::span::{Attributes, Id, Record};
use tracing::{Event, Metadata, Subscriber};

/// Counts tracing events (log records) and spans emitted while a test runs.
struct CountingSubscriber {
    logs: Arc<AtomicU64>,
    spans: Arc<AtomicU64>,
    next_id: AtomicU64,
}

impl Subscriber for CountingSubscriber {
    fn enabled(&self, _: &Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, _: &Attributes<'_>) -> Id {
        self.spans.fetch_add(1, Ordering::SeqCst);
        Id::from_u64(self.next_id.fetch_add(1, Ordering::SeqCst) + 1)
    }
    fn record(&self, _: &Id, _: &Record<'_>) {}
    fn record_follows_from(&self, _: &Id, _: &Id) {}
    fn event(&self, _: &Event<'_>) {
        self.logs.fetch_add(1, Ordering::SeqCst);
    }
    fn enter(&self, _: &Id) {}
    fn exit(&self, _: &Id) {}
}

/// Counts every `ResolverMetrics` data point the resolver emits.
#[derive(Default)]
struct CountingMetrics {
    points: AtomicU64,
}

impl ResolverMetrics for CountingMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {
        self.points.fetch_add(1, Ordering::SeqCst);
    }
    fn resolve_failed(&self, _: &'static str, _: Duration) {
        self.points.fetch_add(1, Ordering::SeqCst);
    }
    fn list(&self, _: &'static str) {
        self.points.fetch_add(1, Ordering::SeqCst);
    }
}

/// Per-test harness handle: manifest, SA-token files, telemetry counters and the tracing guard.
struct Live {
    hub_url: String,
    admin_token: String,
    community_a: i64,
    community_b: i64,
    pg_container: String,
    action_sa: PathBuf,
    ingest_sa: PathBuf,
    metrics: Arc<CountingMetrics>,
    logs: Arc<AtomicU64>,
    spans: Arc<AtomicU64>,
    http: reqwest::Client,
    _guard: tracing::subscriber::DefaultGuard,
}

impl Live {
    /// Load the manifest (`LIVE_HUB_MANIFEST`), write the SA token files and install telemetry.
    /// A missing manifest is a hard failure, never a skip: the gate must not pass by not running.
    fn load(test: &str) -> Self {
        let path = std::env::var("LIVE_HUB_MANIFEST")
            .expect("LIVE_HUB_MANIFEST not set -- run via scripts/ci/connection-resolver-live.sh");
        let m: Value = serde_json::from_str(&std::fs::read_to_string(path).expect("manifest"))
            .expect("manifest json");
        let id = |k: &str| m[k].as_i64().unwrap_or_else(|| panic!("manifest.{k}"));
        let dir = std::env::temp_dir().join(format!("live-sa-{}-{test}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("sa dir");
        let write_sa = |name: &str, account: &str| {
            let token = m["sa_tokens"]
                .as_object()
                .expect("sa_tokens")
                .iter()
                .find(|(_, v)| v.as_str() == Some(account))
                .map(|(k, _)| k.clone())
                .unwrap_or_else(|| panic!("no SA token for {account}"));
            let p = dir.join(name);
            std::fs::write(&p, token).expect("write sa token");
            p
        };
        let logs = Arc::new(AtomicU64::new(0));
        let spans = Arc::new(AtomicU64::new(0));
        let guard = tracing::subscriber::set_default(CountingSubscriber {
            logs: logs.clone(),
            spans: spans.clone(),
            next_id: AtomicU64::new(0),
        });
        Self {
            hub_url: m["hub_url"].as_str().expect("hub_url").to_string(),
            admin_token: m["admin_token"].as_str().expect("admin_token").to_string(),
            community_a: id("community_a"),
            community_b: id("community_b"),
            pg_container: m["pg_container"].as_str().expect("pg").to_string(),
            action_sa: write_sa("action", "svc-action"),
            ingest_sa: write_sa("ingest", "svc-ingest"),
            metrics: Arc::new(CountingMetrics::default()),
            logs,
            spans,
            http: reqwest::Client::new(),
            _guard: guard,
        }
    }

    /// Resolver authenticating as the given ServiceAccount token file with `scope`.
    fn resolver(&self, sa: &Path, scope: &str) -> ConnectionResolver {
        let mut cfg = ResolverConfig::new(
            self.hub_url.clone(),
            "alpha",
            format!("{}/internal/service-token", self.hub_url),
            sa.to_string_lossy().to_string(),
        );
        cfg.resolve_scope = scope.to_string();
        ConnectionResolver::new(cfg, self.metrics.clone()).expect("resolver")
    }

    /// svc-action's resolver (static-capable scope), as deployed by the chart.
    fn action_resolver(&self) -> ConnectionResolver {
        self.resolver(&self.action_sa, SCOPE_CONNECTIONS_RESOLVE_STATIC)
    }

    /// Create one static connection through the admin API; returns its numeric id.
    async fn create(
        &self,
        community: i64,
        provider: &str,
        scheme: i64,
        doc: Value,
        meta: Value,
    ) -> i64 {
        let resp = self
            .http
            .post(format!(
                "{}/api/v1/communities/{community}/provider-connections",
                self.hub_url
            ))
            .bearer_auth(&self.admin_token)
            .json(&json!({
                "provider": provider,
                "secret": {"scheme": scheme, "document": doc},
                "metadata": meta,
            }))
            .send()
            .await
            .expect("create request");
        assert_eq!(
            resp.status().as_u16(),
            201,
            "admin create {provider} failed"
        );
        let body: Value = resp.json().await.expect("create body");
        body["connection"]["id"].as_i64().expect("connection id")
    }

    /// Print this test's telemetry counts for the gate script and assert nothing was silent.
    fn report(&self, test: &str) {
        let (logs, spans, metrics) = (
            self.logs.load(Ordering::SeqCst),
            self.spans.load(Ordering::SeqCst),
            self.metrics.points.load(Ordering::SeqCst),
        );
        println!("LIVE_TELEMETRY test={test} logs={logs} spans={spans} metrics={metrics}");
        assert!(
            spans >= 1 && metrics >= 1,
            "resolver emitted no telemetry in {test}"
        );
    }
}

fn reference(community: i64, connection: i64) -> ConnectionRef {
    ConnectionRef {
        community_id: community,
        connection_id: connection,
    }
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "needs the live harness (scripts/ci/connection-resolver-live.sh)"]
async fn live_resolve_each_kind_roundtrip() {
    let live = Live::load("roundtrip");
    let a = live.community_a;
    let http = live
        .create(
            a,
            "external_http",
            1,
            json!({"token": "RT-HTTP-bearer-7f3a91"}),
            json!({"url": "https://hooks.example.com/rt-roundtrip"}),
        )
        .await;
    let lambda = live
        .create(
            a,
            "aws_lambda",
            4,
            json!({"access_key_id": "RT-AKID-11aa", "secret_access_key": "RT-SECRET-22bb"}),
            json!({"region": "us-east-1", "function": "rt-roundtrip-fn"}),
        )
        .await;
    let gcp = live
        .create(
            a,
            "gcp_function",
            5,
            json!({"client_email": "rt@proj.iam.example", "private_key": "RT-PRIVATE-KEY-33cc"}),
            json!({"project": "rt-proj", "region": "us-central1", "function": "rt-roundtrip-gcp"}),
        )
        .await;
    let resolver = live.action_resolver();

    let r = resolver
        .resolve(reference(a, http))
        .await
        .expect("http resolve");
    assert_eq!(
        (r.kind, r.community_id, r.connection_id),
        (ConnectionKind::ExternalHttp, a, http)
    );
    assert_eq!(
        r.endpoint.url.as_deref(),
        Some("https://hooks.example.com/rt-roundtrip")
    );
    match &r.secret {
        ConnectionSecret::Bearer { token } => assert_eq!(token.expose(), "RT-HTTP-bearer-7f3a91"),
        other => panic!("expected Bearer, got {other:?}"),
    }

    let r = resolver
        .resolve(reference(a, lambda))
        .await
        .expect("lambda resolve");
    assert_eq!(r.kind, ConnectionKind::AwsLambda);
    assert_eq!(r.endpoint.region.as_deref(), Some("us-east-1"));
    assert_eq!(r.endpoint.function.as_deref(), Some("rt-roundtrip-fn"));
    match &r.secret {
        ConnectionSecret::AwsAccessKey {
            access_key_id,
            secret_access_key,
            session_token,
        } => {
            assert_eq!(access_key_id.expose(), "RT-AKID-11aa");
            assert_eq!(secret_access_key.expose(), "RT-SECRET-22bb");
            assert!(session_token.is_none());
        }
        other => panic!("expected AwsAccessKey, got {other:?}"),
    }

    let r = resolver
        .resolve(reference(a, gcp))
        .await
        .expect("gcp resolve");
    assert_eq!(r.kind, ConnectionKind::GcpFunction);
    assert_eq!(r.endpoint.project.as_deref(), Some("rt-proj"));
    match &r.secret {
        ConnectionSecret::GcpServiceAccount {
            client_email,
            private_key_pem,
            ..
        } => {
            assert_eq!(client_email, "rt@proj.iam.example");
            assert_eq!(private_key_pem.expose(), "RT-PRIVATE-KEY-33cc");
        }
        other => panic!("expected GcpServiceAccount, got {other:?}"),
    }
    assert_eq!(
        live.metrics.points.load(Ordering::SeqCst),
        3,
        "one metric point per resolve"
    );
    live.report("roundtrip");
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "needs the live harness (scripts/ci/connection-resolver-live.sh)"]
async fn live_cross_community_resolve_is_not_found() {
    let live = Live::load("cross");
    let conn = live
        .create(
            live.community_a,
            "external_http",
            1,
            json!({"token": "XC-bearer-9d8e"}),
            json!({"url": "https://hooks.example.com/xc"}),
        )
        .await;
    let resolver = live.action_resolver();
    // Granted to A: A resolves it ...
    resolver
        .resolve(reference(live.community_a, conn))
        .await
        .expect("A resolves");
    // ... B (same tenant, never granted) gets the existence-hiding 404, never a 403/409.
    let err = resolver
        .resolve(reference(live.community_b, conn))
        .await
        .expect_err("B must not resolve A's connection");
    assert_eq!(err, ResolveError::NotFound);
    live.report("cross");
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "needs the live harness (scripts/ci/connection-resolver-live.sh)"]
async fn live_tampered_ciphertext_surfaces_fixed_error() {
    let live = Live::load("tamper");
    let secret = "TP-SECRET-TOKEN-5a6b7c";
    let conn = live
        .create(
            live.community_a,
            "external_http",
            1,
            json!({"token": secret}),
            json!({"url": "https://hooks.example.com/tp"}),
        )
        .await;
    let resolver = live.action_resolver();
    resolver
        .resolve(reference(live.community_a, conn))
        .await
        .expect("intact resolves");

    // Flip one ciphertext character directly in Postgres (AES-GCM tag must now fail to verify).
    let sql = format!(
        "UPDATE platform_connections SET sealed_secret = overlay(sealed_secret placing \
         CASE WHEN substr(sealed_secret, 20, 1) = 'A' THEN 'B' ELSE 'A' END from 20 for 1) \
         WHERE id = {conn}"
    );
    let out = Command::new("docker")
        .args([
            "exec",
            "-i",
            &live.pg_container,
            "psql",
            "-U",
            "waddlebot",
            "-d",
            "waddlebot",
            "-tAq",
            "-c",
            &sql,
        ])
        .output()
        .expect("docker exec psql");
    assert!(
        out.status.success(),
        "tamper UPDATE failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let err = resolver
        .resolve(reference(live.community_a, conn))
        .await
        .expect_err("tampered ciphertext must not resolve");
    assert!(
        matches!(err, ResolveError::Transient(_) | ResolveError::Malformed(_)),
        "unexpected error: {err:?}"
    );
    let rendered = format!("{err} | {err:?}");
    assert!(
        !rendered.contains(secret),
        "error text leaked the secret: {rendered}"
    );
    live.report("tamper");
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "needs the live harness (scripts/ci/connection-resolver-live.sh)"]
async fn live_legacy_service_key_is_rejected() {
    let live = Live::load("legacy");
    let conn = live
        .create(
            live.community_a,
            "external_http",
            1,
            json!({"token": "LG-bearer-1f2e"}),
            json!({"url": "https://hooks.example.com/lg"}),
        )
        .await;
    // The retired shared-secret header must not authenticate on EITHER data-plane route.
    let list = live
        .http
        .get(format!(
            "{}/api/v1/internal/provider-connections?provider=external_http",
            live.hub_url
        ))
        .header("X-Service-Key", "anything-at-all")
        .send()
        .await
        .expect("list");
    assert_eq!(list.status().as_u16(), 401, "list accepted X-Service-Key");
    let resolve = live
        .http
        .post(format!(
            "{}/api/v1/internal/communities/{}/provider-connections/{conn}/resolve",
            live.hub_url, live.community_a
        ))
        .header("X-Service-Key", "anything-at-all")
        .send()
        .await
        .expect("resolve");
    assert_eq!(
        resolve.status().as_u16(),
        401,
        "resolve accepted X-Service-Key"
    );
    assert!(!resolve
        .text()
        .await
        .expect("body")
        .contains("LG-bearer-1f2e"));

    // And a caller whose bootstrap SA token the cluster does not recognise maps to Auth.
    let bogus = std::env::temp_dir().join(format!("live-bogus-sa-{}", std::process::id()));
    std::fs::write(&bogus, "not-a-known-service-account").expect("write");
    let err = live
        .resolver(&bogus, SCOPE_CONNECTIONS_RESOLVE_STATIC)
        .resolve(reference(live.community_a, conn))
        .await
        .expect_err("unknown SA must not mint");
    assert_eq!(err, ResolveError::Auth);
    live.report("legacy");
}

#[tokio::test(flavor = "current_thread")]
#[ignore = "needs the live harness (scripts/ci/connection-resolver-live.sh)"]
async fn live_chart_grants_split_list_from_static_resolve() {
    let live = Live::load("grants");
    let conn = live
        .create(
            live.community_a,
            "external_http",
            1,
            json!({"token": "GR-bearer-4c5d"}),
            json!({"url": "https://hooks.example.com/gr"}),
        )
        .await;
    let ingest = live.resolver(
        &live.ingest_sa,
        connection_resolver::SCOPE_CONNECTIONS_RESOLVE,
    );
    // svc-ingest (chart: resolve + list, NOT static): lists bindings ...
    let page = ingest
        .list_bindings("external_http", None, 0, 50)
        .await
        .expect("ingest lists");
    assert!(
        page.items.iter().any(|b| b.connection_id == conn),
        "binding not listed"
    );
    // ... but cannot resolve a static-secret connection (403 -> Auth).
    let err = ingest
        .resolve(reference(live.community_a, conn))
        .await
        .expect_err("no static scope");
    assert_eq!(err, ResolveError::Auth);
    // svc-action (chart: resolve + resolve:static, NOT list): resolves, cannot list (mint rejected).
    let action = live.action_resolver();
    action
        .resolve(reference(live.community_a, conn))
        .await
        .expect("action resolves static");
    let err = action
        .list_bindings("external_http", None, 0, 50)
        .await
        .expect_err("no list scope");
    assert_eq!(err, ResolveError::Auth);
    live.report("grants");
}

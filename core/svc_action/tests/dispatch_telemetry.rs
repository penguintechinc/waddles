//! Dispatcher telemetry: a flag-ON `chat.send` emits a `dispatch.route` span
//! (bounded, PII-free attributes) and a route metric data point. Own file =
//! own process, so the tracing callsite cache is not shared with sibling
//! tests running concurrently.

use std::collections::{BTreeMap, HashMap};

use opentelemetry::metrics::MeterProvider;
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use svc_action::capabilities::ROUTED_METRIC;
use svc_action::metrics_facade::OtelRecorder;
use tracing::field::{Field, Visit};

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};
use svc_action::capabilities::{
    CapabilityHandler, InvokeScope, RelayQueue, StageCapabilities, DISCORD_DRAIN_READY_KEY,
    DISCORD_OP_ACK_KEY_PREFIX,
};
use svc_action::config::Secret;
use svc_action::distribution::BundleCatalog;
use svc_action::egress::{
    EgressGuard, EgressLimits, HttpTransport, TransportRequest, TransportResponse,
};
use svc_action::flags::{boxed, StaticFlag};
use svc_action::usage::UsageBatcher;

const APP_ID: &str = "waddles.telemetry.test";
const ORIGIN_CHANNEL: &str = "123456789012345678";
const BUNDLE_CHANNEL: &str = "999999999999999999";

/// Fake drain-backed queue: records pushes, advertises a ready drain and
/// acks every op carrying an `op_id`.
#[derive(Default)]
struct FakeQueue {
    pushed: Mutex<Vec<serde_json::Value>>,
    acks: Mutex<HashMap<String, String>>,
}

/// Shared handle so the test can read what the capability pushed.
struct SharedQueue(Arc<FakeQueue>);

impl RelayQueue for SharedQueue {
    fn lpush<'a>(
        &'a self,
        key: &'a str,
        value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        self.0.lpush(key, value)
    }
    fn key_exists<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<bool, String>> + Send + 'a>> {
        self.0.key_exists(key)
    }
    fn incr_window<'a>(
        &'a self,
        key: &'a str,
        window_secs: u64,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send + 'a>> {
        self.0.incr_window(key, window_secs)
    }
    fn take<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<String>, String>> + Send + 'a>> {
        self.0.take(key)
    }
}

impl RelayQueue for FakeQueue {
    fn lpush<'a>(
        &'a self,
        _key: &'a str,
        value: String,
    ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
        Box::pin(async move {
            let v: serde_json::Value = serde_json::from_str(&value).map_err(|e| e.to_string())?;
            if let Some(id) = v.get("op_id").and_then(|i| i.as_str()) {
                self.acks.lock().map_err(|e| e.to_string())?.insert(
                    format!("{DISCORD_OP_ACK_KEY_PREFIX}{id}"),
                    r#"{"ok":true}"#.to_string(),
                );
            }
            self.pushed.lock().map_err(|e| e.to_string())?.push(v);
            Ok(())
        })
    }

    fn key_exists<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<bool, String>> + Send + 'a>> {
        Box::pin(async move { Ok(key == DISCORD_DRAIN_READY_KEY) })
    }

    fn incr_window<'a>(
        &'a self,
        _key: &'a str,
        _window_secs: u64,
    ) -> Pin<Box<dyn Future<Output = Result<u64, String>> + Send + 'a>> {
        Box::pin(async { Ok(1) })
    }

    fn take<'a>(
        &'a self,
        key: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<Option<String>, String>> + Send + 'a>> {
        Box::pin(async move { Ok(self.acks.lock().map_err(|e| e.to_string())?.remove(key)) })
    }
}

/// Fake Discord REST transport recording every request it is asked to send.
#[derive(Default)]
struct FakeTransport {
    requests: Mutex<Vec<TransportRequest>>,
}

impl HttpTransport for FakeTransport {
    fn send<'a>(
        &'a self,
        req: TransportRequest,
        _timeout: Duration,
        _max_response_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<TransportResponse, HostResultError>> + Send + 'a>> {
        if let Ok(mut r) = self.requests.lock() {
            r.push(req);
        }
        Box::pin(async {
            Ok(TransportResponse {
                status: 200,
                headers: vec![],
                body: b"{}".to_vec(),
                truncated: false,
            })
        })
    }
}

fn scope() -> InvokeScope {
    InvokeScope {
        tenant: "acme".to_string(),
        community: Some("main".to_string()),
        app_id: APP_ID.to_string(),
        origin_channel_id: Some(ORIGIN_CHANNEL.to_string()),
        tenant_id: 7,
        community_id: 3,
        app_version: 1,
    }
}

fn gate() -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: HashMap::from([(
                "chat.send:discord".to_string(),
                GrantedPermission {
                    permission_id: "chat.send:discord".to_string(),
                    params: serde_json::json!({}),
                },
            )]),
        },
    );
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ))
}

fn egress() -> Arc<EgressGuard> {
    Arc::new(EgressGuard::new(
        Arc::new(svc_action::egress::ReqwestTransport::new()),
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        Arc::new(BundleCatalog::new()),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("telemetry_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        boxed(StaticFlag(true)),
    ))
}

fn caps(
    on: bool,
    queue: Arc<FakeQueue>,
    transport: Arc<FakeTransport>,
) -> StageCapabilities<SharedQueue> {
    StageCapabilities::new(
        SharedQueue(queue),
        egress(),
        Arc::new(Mutex::new(UsageBatcher::new())),
        gate(),
    )
    .with_discord(transport, Secret::new("t"))
    .with_egress_dispatcher_flag(boxed(StaticFlag(on)))
}

fn chat_send(text: &str) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Relay,
        op: "send".to_string(),
        args: serde_json::json!({
            "provider": "discord",
            "message_json": serde_json::json!({"text": text, "channel": BUNDLE_CHANNEL}).to_string(),
        }),
        call_id: 1,
    }
}

type Captured = Vec<(String, BTreeMap<String, String>)>;

#[derive(Default)]
struct Fields(BTreeMap<String, String>);

impl Visit for Fields {
    fn record_str(&mut self, f: &Field, v: &str) {
        self.0.insert(f.name().to_string(), v.to_string());
    }
    fn record_debug(&mut self, f: &Field, v: &dyn std::fmt::Debug) {
        self.0.insert(f.name().to_string(), format!("{v:?}"));
    }
}

/// Minimal subscriber recording every span's name and fields.
#[derive(Default, Clone)]
struct Spans(Arc<Mutex<Captured>>);

impl tracing::Subscriber for Spans {
    fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
        true
    }
    fn new_span(&self, a: &tracing::span::Attributes<'_>) -> tracing::span::Id {
        let mut f = Fields::default();
        a.record(&mut f);
        let mut g = self.0.lock().expect("lock");
        g.push((a.metadata().name().to_string(), f.0));
        tracing::span::Id::from_u64(g.len() as u64)
    }
    fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
    fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
    fn event(&self, _: &tracing::Event<'_>) {}
    fn enter(&self, _: &tracing::span::Id) {}
    fn exit(&self, _: &tracing::span::Id) {}
}

#[tokio::test]
async fn routed_chat_send_emits_span_and_metric() {
    let exporter = InMemoryMetricExporter::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(exporter.clone()).build())
        .build();
    let recorder = OtelRecorder::new(provider.meter("test"));
    let spans = Spans::default();
    let c = caps(
        true,
        Arc::new(FakeQueue::default()),
        Arc::new(FakeTransport::default()),
    );
    {
        let _m = metrics::set_default_local_recorder(&recorder);
        let _s = tracing::subscriber::set_default(spans.clone());
        c.handle(&scope(), chat_send("secret text"))
            .await
            .expect("dispatch");
    }

    let captured = spans.0.lock().expect("lock");
    let (_, attrs) = captured
        .iter()
        .find(|(n, _)| n == "dispatch.route")
        .expect("dispatch.route span emitted");
    assert_eq!(attrs["provider"], "discord");
    assert_eq!(attrs["op"], "chat.send");
    assert_eq!(attrs["destination"], "stateful_platform_reply");
    assert_eq!(attrs["flag_on"], "true");
    assert!(
        attrs.values().all(|v| !v.contains("secret text")),
        "span attributes must be PII-free"
    );

    provider.force_flush().expect("flush");
    let finished = exporter.get_finished_metrics().expect("readable");
    let points: usize = finished
        .iter()
        .flat_map(|r| r.scope_metrics())
        .flat_map(|s| s.metrics())
        .filter(|m| m.name() == ROUTED_METRIC.trim_end_matches("_total"))
        .count();
    assert!(
        points >= 1,
        "route metric data point emitted (got {points})"
    );
}

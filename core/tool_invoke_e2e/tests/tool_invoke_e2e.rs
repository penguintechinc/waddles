//! Real-path e2e for the bundle-to-bundle `tool.invoke` framework (P3b Task
//! 7a). NOTHING on the call path is scripted: a REAL wasmtime executor runs
//! REAL componentized bundles (`tool-caller-fixture-src` calling through the
//! Rust SDK client, `tool-provider-fixture-src` serving through the SDK
//! `ToolRegistry`), and every `tool.invoke` they make is authorized and routed
//! by svc-process's REAL `handle_tool` (parse -> self/cycle/depth guard ->
//! requested-tools -> P2 gate -> target resolution + P1 app->app matrix ->
//! idempotency scoping) and its REAL nested invoke. Only the pipe standing in
//! for the mTLS socket is synthetic.
//!
//! Review-Focus matrix, one test per case: (a) happy, (b) app->app, (c) grant,
//! (d) self + cycle, (e) depth, (f) digest/version pin, (g) invalid args,
//! (h) slow provider, (i) cross-tenant -- plus the telemetry gate.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_active_set::{
    ActiveBundleRow, ActiveVersionSnapshot, ExportedToolInfo, RequestedToolInfo, ToolManifestInfo,
};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger, PermissionId,
};
use opentelemetry::metrics::MeterProvider;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use penguin_bundle_host::wire::Message;
use serde_json::{json, Value};
use svc_process::capabilities::{
    CapabilityHandler, HttpEgressCatalog, StageCapabilities, ToolContext,
};
use svc_process::stage_metrics::StageMetrics;
use tool_invoke_e2e::{digest_of, Rig};

const CALLER_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/tool_caller_fixture.wasm");
const PROVIDER_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/tool_provider_fixture.wasm");

const CALLER: &str = "waddles.e2e.app.caller";
const SVC1: &str = "waddles.e2e.svc.one";
const SVC2: &str = "waddles.e2e.svc.two";
const SVC3: &str = "waddles.e2e.svc.three";
/// An app bundle that (maliciously) exports a tool: app->app must still fail.
const APP2: &str = "waddles.e2e.app.other";

const TENANT: i32 = 7;
const COMMUNITY: i32 = 3;
const V_CALLER: i64 = 11;
const V_SVC1: i64 = 22;
const V_SVC2: i64 = 33;
const V_SVC3: i64 = 44;
const V_APP2: i64 = 55;

/// Hard ceiling on any one scenario: a self-call/cycle deadlock must fail the
/// test, never hang CI.
const SCENARIO_CAP: Duration = Duration::from_secs(60);

fn tools(
    service: bool,
    exports: &[(&str, bool)],
    requests: &[(&str, &str, bool)],
) -> ToolManifestInfo {
    ToolManifestInfo {
        is_tool_service: service,
        exported: exports
            .iter()
            .map(|(n, w)| ExportedToolInfo {
                name: (*n).to_string(),
                write: *w,
            })
            .collect(),
        requested: requests
            .iter()
            .map(|(t, n, w)| RequestedToolInfo {
                target: (*t).to_string(),
                tool: (*n).to_string(),
                write: *w,
            })
            .collect(),
    }
}

fn row(app: &str, version_id: i64, wasm: &[u8], tools: ToolManifestInfo) -> ActiveBundleRow {
    ActiveBundleRow {
        app_id: app.to_string(),
        version: "1.0.0".to_string(),
        version_id,
        digest: digest_of(wasm),
        component_key: String::new(),
        sidecar_key: String::new(),
        artifact_signature: None,
        artifact_signature_key_id: None,
        artifact_signed_approval_id: None,
        declared_capabilities: Vec::new(),
        tools,
    }
}

/// The default world's manifest rows.
fn default_rows() -> Vec<ActiveBundleRow> {
    let svc_tools = [
        ("echo", false),
        ("guarded", true),
        ("relay", false),
        ("slow", false),
    ];
    vec![
        row(
            CALLER,
            V_CALLER,
            CALLER_WASM,
            tools(
                false,
                &[],
                &[
                    (SVC1, "echo", false),
                    (SVC1, "guarded", true),
                    (SVC1, "relay", false),
                    (SVC1, "slow", false),
                    (APP2, "echo", false),
                    (CALLER, "go", false),
                ],
            ),
        ),
        row(
            SVC1,
            V_SVC1,
            PROVIDER_WASM,
            tools(
                true,
                &svc_tools,
                &[
                    (SVC2, "relay", false),
                    (SVC2, "echo", false),
                    (SVC1, "echo", false),
                ],
            ),
        ),
        row(
            SVC2,
            V_SVC2,
            PROVIDER_WASM,
            tools(
                true,
                &[("echo", false), ("relay", false)],
                &[
                    (SVC3, "echo", false),
                    (SVC1, "relay", false),
                    (SVC1, "echo", false),
                ],
            ),
        ),
        row(
            SVC3,
            V_SVC3,
            PROVIDER_WASM,
            tools(true, &[("echo", false)], &[]),
        ),
        row(
            APP2,
            V_APP2,
            PROVIDER_WASM,
            tools(false, &[("echo", false)], &[]),
        ),
    ]
}

/// `(tenant, grantee, grantee version, target, tool)`.
type Grant = (i32, &'static str, i64, &'static str, &'static str);

fn default_grants(tenant: i32) -> Vec<Grant> {
    vec![
        (tenant, CALLER, V_CALLER, SVC1, "echo"),
        (tenant, CALLER, V_CALLER, SVC1, "guarded"),
        (tenant, CALLER, V_CALLER, SVC1, "relay"),
        (tenant, CALLER, V_CALLER, SVC1, "slow"),
        // Present on purpose: a grant alone must NOT authorize app->app.
        (tenant, CALLER, V_CALLER, APP2, "echo"),
        (tenant, CALLER, V_CALLER, CALLER, "go"),
        (tenant, SVC1, V_SVC1, SVC2, "relay"),
        (tenant, SVC1, V_SVC1, SVC2, "echo"),
        (tenant, SVC1, V_SVC1, SVC1, "echo"),
        (tenant, SVC2, V_SVC2, SVC3, "echo"),
        (tenant, SVC2, V_SVC2, SVC1, "relay"),
        (tenant, SVC2, V_SVC2, SVC1, "echo"),
    ]
}

fn gate(grants: &[Grant]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    let mut by_scope: HashMap<GrantScopeKey, HashMap<String, GrantedPermission>> = HashMap::new();
    for (tenant, grantee, version, target, tool) in grants {
        let cid = PermissionId::ToolCall {
            target: (*target).to_string(),
            tool: (*tool).to_string(),
        }
        .canonical_id();
        by_scope
            .entry(GrantScopeKey {
                tenant_id: *tenant,
                community_id: COMMUNITY,
                app_id: (*grantee).to_string(),
                app_version: *version,
            })
            .or_default()
            .insert(
                cid.clone(),
                GrantedPermission {
                    permission_id: cid,
                    params: json!({}),
                },
            );
    }
    for (key, grants) in by_scope {
        snapshot.set(
            key,
            GrantSet {
                permission_snapshot_hash: "e2e".to_string(),
                grants,
            },
        );
    }
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ))
}

fn egress() -> Arc<bundle_host_http::egress::EgressGuard> {
    Arc::new(bundle_host_http::egress::EgressGuard::new(
        Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
        bundle_host_http::egress::EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        HttpEgressCatalog::new(),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("tool_e2e_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
    ))
}

/// A read-back view of an in-memory OTel pipeline.
struct Telemetry {
    provider: SdkMeterProvider,
    exporter: InMemoryMetricExporter,
}

/// One exported series: name, attributes, count (histogram) or sum (counter).
struct Point {
    name: String,
    attrs: BTreeMap<String, String>,
    value: f64,
}

impl Telemetry {
    fn new() -> Self {
        let exporter = InMemoryMetricExporter::default();
        let provider = SdkMeterProvider::builder()
            .with_reader(PeriodicReader::builder(exporter.clone()).build())
            .build();
        Self { provider, exporter }
    }

    fn points(&self) -> Vec<Point> {
        self.provider.force_flush().expect("flush");
        let mut out = Vec::new();
        for resource in &self.exporter.get_finished_metrics().expect("readable") {
            for scope in resource.scope_metrics() {
                for metric in scope.metrics() {
                    let attrs = |it: &mut dyn Iterator<Item = &opentelemetry::KeyValue>| {
                        it.map(|kv| (kv.key.to_string(), kv.value.to_string()))
                            .collect::<BTreeMap<_, _>>()
                    };
                    match metric.data() {
                        AggregatedMetrics::U64(MetricData::Sum(s)) => {
                            for dp in s.data_points() {
                                out.push(Point {
                                    name: metric.name().to_string(),
                                    attrs: attrs(&mut dp.attributes()),
                                    value: dp.value() as f64,
                                });
                            }
                        }
                        AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                            for dp in h.data_points() {
                                out.push(Point {
                                    name: metric.name().to_string(),
                                    attrs: attrs(&mut dp.attributes()),
                                    value: dp.count() as f64,
                                });
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        out
    }

    /// Highest count/sum of `name` series whose attributes include `filter`.
    fn count(&self, name: &str, filter: &[(&str, &str)]) -> f64 {
        self.points()
            .into_iter()
            .filter(|p| {
                p.name == name
                    && filter
                        .iter()
                        .all(|(k, v)| p.attrs.get(*k).map(String::as_str) == Some(*v))
            })
            .map(|p| p.value)
            .fold(0.0, f64::max)
    }
}

struct World {
    rig: Rig,
    snapshot: ActiveVersionSnapshot,
    gate: Arc<CapabilityGate>,
    metrics: Arc<StageMetrics>,
    telemetry: Telemetry,
}

impl World {
    /// Rows are `(tenant, community, row)`; the executor loads every
    /// distinct app once (keyed by app id, platform digest = the real one).
    async fn new(entries: Vec<(i32, i32, ActiveBundleRow)>, grants: &[Grant]) -> Self {
        let rig = Rig::start(&[
            (CALLER, CALLER_WASM),
            (SVC1, PROVIDER_WASM),
            (SVC2, PROVIDER_WASM),
            (SVC3, PROVIDER_WASM),
            (APP2, PROVIDER_WASM),
        ])
        .await
        .expect("rig starts (executor + stage link + 5 real components loaded)");
        let snapshot = ActiveVersionSnapshot::new();
        snapshot.update_scoped_all(
            &entries
                .into_iter()
                .map(|(t, c, r)| ((t, c, r.app_id.clone()), r))
                .collect(),
        );
        let telemetry = Telemetry::new();
        let metrics = Arc::new(StageMetrics::new(&telemetry.provider.meter("tool-e2e")));
        Self {
            rig,
            snapshot,
            gate: gate(grants),
            metrics,
            telemetry,
        }
    }

    async fn default() -> Self {
        let entries = default_rows()
            .into_iter()
            .map(|r| (TENANT, COMMUNITY, r))
            .collect();
        Self::new(entries, &default_grants(TENANT)).await
    }

    /// The REAL per-invocation stage scope for the top-level call of `app`.
    fn caps(
        &self,
        tenant_id: i32,
        app: &str,
        version: i64,
        deadline: Option<Instant>,
    ) -> Arc<dyn CapabilityHandler> {
        let c: StageCapabilities = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            app.to_string(),
            tenant_id,
            COMMUNITY,
            version,
            egress(),
            Arc::clone(&self.gate),
        )
        .with_tool(ToolContext {
            connections: Arc::clone(&self.rig.registry),
            snapshot: self.snapshot.clone(),
            chain: vec![app.to_string()],
            deadline,
            trace: None,
        })
        .with_stage_metrics(Arc::clone(&self.metrics));
        Arc::new(c)
    }

    /// Runs the caller app's `go` tool: it invokes `target.tool(args)` through
    /// the REAL SDK client, and the host's typed outcome comes straight back.
    async fn go(&self, tenant_id: i32, req: Value) -> Value {
        self.go_with_deadline(tenant_id, req, Duration::from_secs(20))
            .await
    }

    async fn go_with_deadline(&self, tenant_id: i32, req: Value, budget: Duration) -> Value {
        let caps = self.caps(tenant_id, CALLER, V_CALLER, Some(Instant::now() + budget));
        let reply = tokio::time::timeout(
            SCENARIO_CAP,
            self.rig
                .call_tool(CALLER, CALLER_WASM, "go", req, 30_000, caps),
        )
        .await
        .expect("scenario must not hang (self-call/cycle deadlock guard)")
        .expect("host-api transport");
        match reply {
            Message::Result(r) => r.payload,
            other => panic!("expected a tool envelope, got {other:?}"),
        }
    }
}

fn ok<'a>(t: &'a str, n: &'a str) -> [(&'a str, &'a str); 3] {
    [("target", t), ("tool", n), ("outcome", "ok")]
}

fn call(target: &str, tool: &str, args: Value, key: Option<&str>) -> Value {
    json!({"target": target, "tool": tool, "args": args, "key": key})
}

fn relay(via: &str, target: &str, tool: &str, args: Value) -> Value {
    call(
        via,
        "relay",
        json!({"target": target, "tool": tool, "args": args}),
        None,
    )
}

#[tokio::test(flavor = "multi_thread")]
async fn a_happy_path_app_to_service_returns_the_providers_result_and_emits_telemetry() {
    let w = World::default().await;
    // Read tool through the real client -> real stage -> real provider.
    let out = w
        .go(TENANT, call(SVC1, "echo", json!({"msg": "hi"}), None))
        .await;
    assert_eq!(out, json!({"ok": {"msg": "hi"}}));

    // Write tool: the host-forwarded, principal-scoped idempotency key reaches
    // the provider's handler through the SDK registry's reserved field.
    let out = w
        .go(
            TENANT,
            call(SVC1, "guarded", json!({"n": 1}), Some("order-1")),
        )
        .await;
    let key = out["ok"]["idempotency_key"]
        .as_str()
        .expect("key forwarded");
    assert_eq!(key.len(), 64, "sha256 hex, principal scoped");
    assert_ne!(key, "order-1", "never the caller's raw key");
    assert_eq!(out["ok"]["guarded"], true);
    assert_eq!(out["ok"]["args"], json!({"n": 1}));

    // Depth 2 (caller -> svc1.relay -> svc2.echo) is a real nested chain.
    let out = w
        .go(TENANT, relay(SVC1, SVC2, "echo", json!({"deep": true})))
        .await;
    assert_eq!(out, json!({"ok": {"relayed": {"deep": true}}}));

    // Telemetry gate: counts printed, zero is a failure.
    let counts = [
        (
            "waddles_tool_call",
            w.telemetry.count("waddles_tool_call", &ok(SVC1, "echo")),
        ),
        (
            "waddles_tool_call_latency",
            w.telemetry
                .count("waddles_tool_call_latency", &ok(SVC1, "echo")),
        ),
        (
            "guarded",
            w.telemetry.count("waddles_tool_call", &ok(SVC1, "guarded")),
        ),
        (
            "hop2",
            w.telemetry.count("waddles_tool_call", &ok(SVC2, "echo")),
        ),
    ];
    println!("TELEMETRY counts: {counts:?}");
    for (name, n) in counts {
        assert!(n >= 1.0, "{name}: zero data points is a FAIL");
    }
    for p in w.telemetry.points() {
        assert!(
            !p.attrs
                .values()
                .any(|v| v.contains("hi") || v.contains("order-1")),
            "request data leaked into a label: {:?}",
            p.attrs
        );
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn b_app_to_app_is_forbidden_even_with_a_grant_present() {
    let w = World::default().await;
    let out = w
        .go(TENANT, call(APP2, "echo", json!({"x": 1}), None))
        .await;
    assert_eq!(out, json!({"tool_error": "Forbidden"}));
    // Refused before resolution completes -> unresolved labels (guest-supplied
    // names never mint series).
    assert!(
        w.telemetry.count(
            "waddles_tool_call",
            &[("outcome", "forbidden"), ("target", "unresolved")]
        ) >= 1.0
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn c_missing_or_mismatched_grant_is_not_granted() {
    // No grant for CALLER -> SVC1:echo at all.
    let grants: Vec<Grant> = default_grants(TENANT)
        .into_iter()
        .filter(|g| !(g.1 == CALLER && g.3 == SVC1 && g.4 == "echo"))
        .collect();
    let entries = default_rows()
        .into_iter()
        .map(|r| (TENANT, COMMUNITY, r))
        .collect();
    let w = World::new(entries, &grants).await;
    assert_eq!(
        w.go(TENANT, call(SVC1, "echo", json!({}), None)).await,
        json!({"tool_error": "NotGranted"})
    );
    // A different tool of the same target remains granted.
    assert_eq!(
        w.go(
            TENANT,
            call(
                SVC1,
                "relay",
                json!({"target": SVC2, "tool": "echo", "args": 1}),
                None
            )
        )
        .await["ok"]["relayed"],
        json!(1)
    );

    // A grant pinned to another caller VERSION is not this caller's grant.
    let grants: Vec<Grant> = default_grants(TENANT)
        .into_iter()
        .map(|mut g| {
            if g.1 == CALLER {
                g.2 += 1;
            }
            g
        })
        .collect();
    let entries = default_rows()
        .into_iter()
        .map(|r| (TENANT, COMMUNITY, r))
        .collect();
    let w = World::new(entries, &grants).await;
    assert_eq!(
        w.go(TENANT, call(SVC1, "echo", json!({}), None)).await,
        json!({"tool_error": "NotGranted"})
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn d_self_call_and_cycle_fail_fast_with_typed_errors() {
    let w = World::default().await;
    // Caller -> itself.
    assert_eq!(
        w.go(TENANT, call(CALLER, "go", json!({}), None)).await,
        json!({"tool_error": "Forbidden"})
    );
    // Provider -> itself (svc1.relay -> svc1.echo): the nested guard refuses.
    assert_eq!(
        w.go(TENANT, relay(SVC1, SVC1, "echo", json!({}))).await,
        json!({"tool_error": "Forbidden"})
    );
    // Cycle: caller -> svc1.relay -> svc2.relay -> svc1.echo.
    let cycle = call(
        SVC1,
        "relay",
        json!({"target": SVC2, "tool": "relay",
               "args": {"target": SVC1, "tool": "echo", "args": {}}}),
        None,
    );
    assert_eq!(
        w.go(TENANT, cycle).await,
        json!({"tool_error": "Forbidden"}),
        "cycle must fail fast (the SCENARIO_CAP guard fails a hang)"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn e_depth_three_is_refused() {
    let w = World::default().await;
    // caller -> svc1.relay -> svc2.relay -> svc3.echo is hop 3.
    let deep = call(
        SVC1,
        "relay",
        json!({"target": SVC2, "tool": "relay",
               "args": {"target": SVC3, "tool": "echo", "args": {}}}),
        None,
    );
    assert_eq!(
        w.go(TENANT, deep).await,
        json!({"tool_error": "DepthExceeded"})
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn f_digest_or_version_pin_mismatch_is_target_unavailable() {
    // The platform-installed digest for SVC1 is not the component the executor
    // has pinned -> the nested invoke is refused (tampered/stale target).
    let mut rows = default_rows();
    let bogus = format!("sha256:{}", "ab".repeat(32));
    rows[1].digest = bogus;
    let entries = rows.into_iter().map(|r| (TENANT, COMMUNITY, r)).collect();
    let w = World::new(entries, &default_grants(TENANT)).await;
    assert_eq!(
        w.go(TENANT, call(SVC1, "echo", json!({}), None)).await,
        json!({"tool_error": "TargetUnavailable"})
    );

    // An unknown (never installed) target is the same typed refusal.
    let rows: Vec<_> = default_rows()
        .into_iter()
        .filter(|r| r.app_id != SVC1)
        .collect();
    let entries = rows.into_iter().map(|r| (TENANT, COMMUNITY, r)).collect();
    let w = World::new(entries, &default_grants(TENANT)).await;
    assert_eq!(
        w.go(TENANT, call(SVC1, "echo", json!({}), None)).await,
        json!({"tool_error": "TargetUnavailable"})
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn g_invalid_args_are_refused() {
    let w = World::default().await;
    // A write tool without an idempotency key.
    let out = w
        .go(TENANT, call(SVC1, "guarded", json!({"n": 1}), None))
        .await;
    assert!(out["tool_error"]["InvalidArgs"].is_string(), "{out}");
    // Oversized args never reach the provider.
    let big = "x".repeat(70 * 1024);
    let out = w
        .go(TENANT, call(SVC1, "echo", json!({"blob": big}), None))
        .await;
    assert!(out["tool_error"]["InvalidArgs"].is_string(), "{out}");
    // A malformed tool name is refused before any lookup.
    let out = w
        .go(TENANT, call(SVC1, "Not A Tool", json!({}), None))
        .await;
    assert!(out["tool_error"]["InvalidArgs"].is_string(), "{out}");
}

#[tokio::test(flavor = "multi_thread")]
async fn h_a_slow_provider_trips_the_host_timeout() {
    let w = World::default().await;
    let started = Instant::now();
    let out = w
        .go_with_deadline(
            TENANT,
            call(SVC1, "slow", json!({}), None),
            Duration::from_millis(1500),
        )
        .await;
    assert_eq!(out, json!({"tool_error": "Timeout"}));
    assert!(
        started.elapsed() < Duration::from_secs(10),
        "the call must end at the host budget, not run to completion"
    );
    assert!(
        w.telemetry.count(
            "waddles_tool_call",
            &[("target", SVC1), ("tool", "slow"), ("outcome", "timeout")]
        ) >= 1.0
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn i_cross_tenant_same_app_id_fails_closed() {
    // The caller runs in tenant 8 (own grants, own caller row); SVC1 is
    // installed ONLY in tenant 7. Same app ids, different tenant: the target
    // must not resolve.
    let mut entries: Vec<(i32, i32, ActiveBundleRow)> = default_rows()
        .into_iter()
        .map(|r| (TENANT, COMMUNITY, r))
        .collect();
    entries.push((8, COMMUNITY, default_rows().remove(0)));
    let mut grants = default_grants(TENANT);
    grants.extend(default_grants(8).into_iter().filter(|g| g.1 == CALLER));
    let w = World::new(entries, &grants).await;
    assert_eq!(
        w.go(8, call(SVC1, "echo", json!({}), None)).await,
        json!({"tool_error": "TargetUnavailable"})
    );
    // Control: the same call in the tenant that DOES have SVC1 succeeds.
    assert_eq!(
        w.go(TENANT, call(SVC1, "echo", json!({"ok": 1}), None))
            .await,
        json!({"ok": {"ok": 1}})
    );

    // SVC1 installed in tenant 8 too, but tenant 8's caller holds no grant:
    // tenant 7's grants never apply there.
    let mut entries: Vec<(i32, i32, ActiveBundleRow)> = default_rows()
        .into_iter()
        .map(|r| (TENANT, COMMUNITY, r))
        .collect();
    for r in default_rows() {
        entries.push((8, COMMUNITY, r));
    }
    let w = World::new(entries, &default_grants(TENANT)).await;
    assert_eq!(
        w.go(8, call(SVC1, "echo", json!({}), None)).await,
        json!({"tool_error": "NotGranted"})
    );
}

//! Real-path e2e for the reputation SERVICE bundle (bundle-MCP-tools P5 Task 4).
//!
//! The REAL production reputation bundle (`bundles/rust/reputation`, built with
//! `cargo component`, committed as `tests/fixtures/reputation_service.wasm`)
//! runs in the REAL wasmtime executor; a REAL caller app reaches it through the
//! REAL `tool.invoke` path (svc-process `handle_tool`: requested-tools -> P2
//! gate -> app->service matrix -> required-scope check on the host-verified
//! actor -> nested invoke), and the bundle's `reputation` / `kv` imports are
//! served by svc-process's REAL `StageCapabilities` (the P2 gate + its
//! catalog-driven reputation caps). Only the durable stores are in-memory
//! (a score map and an empty kv), standing in for Postgres/Valkey; the SQL
//! behaviour (clamp, atomicity, idempotency) is covered by
//! `core/bundle_host_reputation/tests/postgres_integration.rs`.
//!
//! Review-Focus matrix, one test each: draw-filter `get_many`, admin override
//! (admin lands + ledgered, non-admin denied before the store), 24h cap
//! enforced regardless of bundle logic, community isolation, spoofed `actor`
//! args refused, and the telemetry gate (counts printed, zero = FAIL).
//! Rebuild the fixture: see `tests/fixtures/README.md`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bundle_active_set::{
    ActiveBundleRow, ActiveVersionSnapshot, ExportedToolInfo, RequestedToolInfo, ToolManifestInfo,
};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger, PermissionId,
};
use bundle_host_kv::ReconcileOutcome;
use bundle_host_kv::{BoxFuture as KvFuture, CapabilitySnapshot, KvBackend, QuotaOutcome};
use bundle_host_reputation::{
    BoxFuture, IdempotencyKey, ReputationCaps, ReputationError, ReputationScope, ReputationStore,
    ScoreEntry,
};
use opentelemetry::metrics::MeterProvider;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use penguin_bundle_host::wire::Message;
use serde_json::{json, Value};
use svc_process::capabilities::{
    CapabilityHandler, HttpEgressCatalog, ReputationWiring, StageCapabilities, ToolContext,
};
use svc_process::identity::{
    BoxFuture as IdFuture, IdentityError, IdentityScope, IdentityWiring, InvocationIdentity,
    MemberDirectory,
};
use svc_process::license::StaticGate;
use svc_process::stage_metrics::StageMetrics;
use tool_invoke_e2e::{digest_of, Rig};
use uuid::Uuid;

const CALLER_WASM: &[u8] =
    include_bytes!("../../bundle_executor/tests/fixtures/tool_caller_fixture.wasm");
const REP_WASM: &[u8] = include_bytes!("fixtures/reputation_service.wasm");

const CALLER: &str = "waddles.e2e.app.giveaway";
const REP: &str = "waddles.core.service.reputation";
const TENANT: i32 = 7;
const COMMUNITY_A: i32 = 3;
const COMMUNITY_B: i32 = 4;
const V_CALLER: i64 = 11;
const V_REP: i64 = 22;
const SCENARIO_CAP: Duration = Duration::from_secs(60);
const ACTOR_UUID: &str = "11111111-1111-4111-8111-111111111111";

/// Tools the reputation bundle exports: `(name, is_write, required_scope)`.
const REP_TOOLS: [(&str, bool, Option<&str>); 6] = [
    ("get_score", false, None),
    ("get_many", false, None),
    ("leaderboard", false, None),
    ("rep", false, None),
    ("adjust", true, None),
    ("override", true, Some("community:admin")),
];

/// One ledger row the in-memory store appends (mirrors the SQL ledger kinds).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Ledger {
    community: i32,
    user: Uuid,
    kind: &'static str,
    value: i64,
}

/// In-memory stand-in for the Postgres store: scores keyed `(community, user)`
/// plus an append-only ledger. The cap enforcement under test lives in the
/// gate (catalog quotas), which runs BEFORE this store.
#[derive(Default)]
struct MemStore {
    scores: Mutex<HashMap<(i32, Uuid), i64>>,
    ledger: Mutex<Vec<Ledger>>,
}

impl MemStore {
    fn score(&self, community: i32, user: Uuid) -> i64 {
        *self
            .scores
            .lock()
            .unwrap()
            .get(&(community, user))
            .unwrap_or(&0)
    }
    fn ledger(&self) -> Vec<Ledger> {
        self.ledger.lock().unwrap().clone()
    }
}

impl ReputationStore for MemStore {
    fn get<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move { Ok(self.score(scope.community_id, user)) })
    }

    fn adjust<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        delta: i32,
        _reason: &'a str,
        _caps: ReputationCaps,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            let mut s = self.scores.lock().unwrap();
            let v = s.entry((scope.community_id, user)).or_insert(0);
            *v += i64::from(delta);
            self.ledger.lock().unwrap().push(Ledger {
                community: scope.community_id,
                user,
                kind: "adjust",
                value: i64::from(delta),
            });
            Ok(*v)
        })
    }

    fn override_score<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        absolute_score: i64,
        _reason: &'a str,
        _key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            self.scores
                .lock()
                .unwrap()
                .insert((scope.community_id, user), absolute_score);
            self.ledger.lock().unwrap().push(Ledger {
                community: scope.community_id,
                user,
                kind: "admin_override",
                value: absolute_score,
            });
            Ok(absolute_score)
        })
    }

    fn adjust_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        items: &'a [(Uuid, i32)],
        _reason: &'a str,
        _caps: ReputationCaps,
        _key: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async move {
            let mut s = self.scores.lock().unwrap();
            for (user, delta) in items {
                *s.entry((scope.community_id, *user)).or_insert(0) += i64::from(*delta);
                self.ledger.lock().unwrap().push(Ledger {
                    community: scope.community_id,
                    user: *user,
                    kind: "adjust",
                    value: i64::from(*delta),
                });
            }
            Ok(())
        })
    }

    fn get_many<'a>(
        &'a self,
        scope: &'a ReputationScope,
        users: &'a [Uuid],
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async move {
            Ok(users
                .iter()
                .map(|u| ScoreEntry {
                    user: *u,
                    score: self.score(scope.community_id, *u),
                })
                .collect())
        })
    }

    fn leaderboard<'a>(
        &'a self,
        scope: &'a ReputationScope,
        limit: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async move {
            let mut v: Vec<ScoreEntry> = self
                .scores
                .lock()
                .unwrap()
                .iter()
                .filter(|((c, _), _)| *c == scope.community_id)
                .map(|((_, u), s)| ScoreEntry {
                    user: *u,
                    score: *s,
                })
                .collect();
            v.sort_by(|a, b| b.score.cmp(&a.score).then(a.user.cmp(&b.user)));
            v.truncate(limit as usize);
            Ok(v)
        })
    }
}

/// A kv with no keys: every community runs on the bundle's documented defaults
/// (delta policy win=+1 / loss=-1 / loyalty=+1, default tier ladder).
struct EmptyKv;

impl KvBackend for EmptyKv {
    fn get<'a>(&'a self, _k: &'a str) -> KvFuture<'a, Result<Option<Vec<u8>>, String>> {
        Box::pin(async { Ok(None) })
    }
    fn set_with_quota<'a>(
        &'a self,
        _d: &'a str,
        _l: &'a str,
        _c: &'a str,
        _v: &'a [u8],
        _t: u32,
        _m: u64,
    ) -> KvFuture<'a, Result<QuotaOutcome<()>, String>> {
        Box::pin(async { Err("e2e kv is read-only".to_string()) })
    }
    fn delete<'a>(
        &'a self,
        _d: &'a str,
        _l: &'a str,
        _c: &'a str,
    ) -> KvFuture<'a, Result<bool, String>> {
        Box::pin(async { Err("e2e kv is read-only".to_string()) })
    }
    fn increment_with_quota<'a>(
        &'a self,
        _d: &'a str,
        _l: &'a str,
        _c: &'a str,
        _delta: i64,
        _t: u32,
        _m: u64,
    ) -> KvFuture<'a, Result<QuotaOutcome<i64>, String>> {
        Box::pin(async { Err("e2e kv is read-only".to_string()) })
    }
    fn increment_rate<'a>(&'a self, _r: &'a str, _w: u64) -> KvFuture<'a, Result<u64, String>> {
        Box::pin(async { Ok(1) })
    }
    fn reconcile_count_if_missing<'a>(
        &'a self,
        _l: &'a str,
        _p: &'a str,
        _k: &'a str,
        _t: u64,
        _s: u64,
    ) -> KvFuture<'a, Result<ReconcileOutcome, String>> {
        Box::pin(async { Ok(ReconcileOutcome::AlreadyPresent) })
    }
}

/// Roster: the platform account `1001` is a member with `role` (if any).
struct Roster {
    role: Option<&'static str>,
}

impl MemberDirectory for Roster {
    fn member_by_platform_id<'a>(
        &'a self,
        _scope: IdentityScope,
        _platform: &'a str,
        id: &'a str,
    ) -> IdFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move {
            if id == "1001" {
                Uuid::parse_str(ACTOR_UUID).map_err(|e| IdentityError::Backend(e.to_string()))
            } else {
                Err(IdentityError::NotAMember)
            }
        })
    }

    fn role_for<'a>(
        &'a self,
        _scope: IdentityScope,
        _platform: &'a str,
        _id: &'a str,
    ) -> IdFuture<'a, Result<String, IdentityError>> {
        Box::pin(async move {
            self.role
                .map(str::to_string)
                .ok_or(IdentityError::NotLinked)
        })
    }

    fn confirm_member<'a>(
        &'a self,
        _scope: IdentityScope,
        user: Uuid,
    ) -> IdFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move { Ok(user) })
    }
}

/// Read-back view of an in-memory OTel pipeline.
struct Telemetry {
    provider: SdkMeterProvider,
    exporter: InMemoryMetricExporter,
}

impl Telemetry {
    fn new() -> Self {
        let exporter = InMemoryMetricExporter::default();
        let provider = SdkMeterProvider::builder()
            .with_reader(PeriodicReader::builder(exporter.clone()).build())
            .build();
        Self { provider, exporter }
    }

    /// Highest count/sum of `name` series whose attributes include `filter`.
    fn count(&self, name: &str, filter: &[(&str, &str)]) -> f64 {
        self.provider.force_flush().expect("flush");
        let mut best = 0.0_f64;
        for resource in &self.exporter.get_finished_metrics().expect("readable") {
            for scope in resource.scope_metrics() {
                for metric in scope.metrics() {
                    if metric.name() != name {
                        continue;
                    }
                    let mut points: Vec<(BTreeMap<String, String>, f64)> = Vec::new();
                    let attrs = |it: &mut dyn Iterator<Item = &opentelemetry::KeyValue>| {
                        it.map(|kv| (kv.key.to_string(), kv.value.to_string()))
                            .collect::<BTreeMap<_, _>>()
                    };
                    match metric.data() {
                        AggregatedMetrics::U64(MetricData::Sum(s)) => {
                            for dp in s.data_points() {
                                points.push((attrs(&mut dp.attributes()), dp.value() as f64));
                            }
                        }
                        AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                            for dp in h.data_points() {
                                points.push((attrs(&mut dp.attributes()), dp.count() as f64));
                            }
                        }
                        _ => {}
                    }
                    for (a, v) in points {
                        if filter
                            .iter()
                            .all(|(k, want)| a.get(*k).map(String::as_str) == Some(*want))
                        {
                            best = best.max(v);
                        }
                    }
                }
            }
        }
        best
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

fn rows() -> Vec<ActiveBundleRow> {
    vec![
        row(
            CALLER,
            V_CALLER,
            CALLER_WASM,
            ToolManifestInfo {
                is_tool_service: false,
                exported: Vec::new(),
                requested: REP_TOOLS
                    .iter()
                    .map(|(n, w, _)| RequestedToolInfo {
                        target: REP.to_string(),
                        tool: (*n).to_string(),
                        write: *w,
                    })
                    .collect(),
            },
        ),
        row(
            REP,
            V_REP,
            REP_WASM,
            ToolManifestInfo {
                is_tool_service: true,
                exported: REP_TOOLS
                    .iter()
                    .map(|(n, w, s)| ExportedToolInfo {
                        name: (*n).to_string(),
                        write: *w,
                        required_scope: s.map(str::to_string),
                    })
                    .collect(),
                requested: Vec::new(),
            },
        ),
    ]
}

fn grant_set(ids: Vec<(String, Value)>) -> GrantSet {
    GrantSet {
        permission_snapshot_hash: "e2e".to_string(),
        grants: ids
            .into_iter()
            .map(|(id, params)| {
                (
                    id.clone(),
                    GrantedPermission {
                        permission_id: id,
                        params,
                    },
                )
            })
            .collect(),
    }
}

/// Gate: the caller is granted every reputation tool; the reputation service
/// holds the `reputation` host grants (matching `hub-manifest.yaml`) + kv.
/// `members` are active in `COMMUNITY_A` ONLY (isolation is host-asserted).
fn gate(members: &[Uuid]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    for community in [COMMUNITY_A, COMMUNITY_B] {
        snapshot.set(
            GrantScopeKey {
                tenant_id: TENANT,
                community_id: community,
                app_id: CALLER.to_string(),
                app_version: V_CALLER,
            },
            grant_set(
                REP_TOOLS
                    .iter()
                    .map(|(n, _, _)| {
                        (
                            PermissionId::ToolCall {
                                target: REP.to_string(),
                                tool: (*n).to_string(),
                            }
                            .canonical_id(),
                            json!({}),
                        )
                    })
                    .collect(),
            ),
        );
        snapshot.set(
            GrantScopeKey {
                tenant_id: TENANT,
                community_id: community,
                app_id: REP.to_string(),
                app_version: V_REP,
            },
            grant_set(vec![
                ("reputation.read".to_string(), json!({})),
                (
                    "reputation.community.write".to_string(),
                    json!({"delta_min": -5, "delta_max": 5,
                           "reason_codes": ["game.win", "game.loss", "loyalty.bonus", "admin.adjust"]}),
                ),
                ("reputation.community.admin".to_string(), json!({})),
                ("storage.kv".to_string(), json!({})),
            ]),
        );
    }
    let membership = InMemoryMembership::new();
    for m in members {
        membership.add_community_member(TENANT, COMMUNITY_A, *m);
    }
    Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(membership),
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
            prometheus::Opts::new("rep_e2e_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
    ))
}

struct World {
    rig: Rig,
    snapshot: ActiveVersionSnapshot,
    gate: Arc<CapabilityGate>,
    store: Arc<MemStore>,
    metrics: Arc<StageMetrics>,
    telemetry: Telemetry,
    /// Roster role of platform account `1001` (None = unlinked).
    role: Option<&'static str>,
    /// Platform account that triggered the event (None = system event).
    account: Option<&'static str>,
    /// Active members of `COMMUNITY_A`.
    alice: Uuid,
    bob: Uuid,
}

impl World {
    async fn new(role: Option<&'static str>, account: Option<&'static str>) -> Self {
        let rig = Rig::start(&[(CALLER, CALLER_WASM), (REP, REP_WASM)])
            .await
            .expect("rig starts (executor + stage link + real reputation component loaded)");
        let snapshot = ActiveVersionSnapshot::new();
        let mut entries = HashMap::new();
        for community in [COMMUNITY_A, COMMUNITY_B] {
            for r in rows() {
                entries.insert((TENANT, community, r.app_id.clone()), r);
            }
        }
        snapshot.update_scoped_all(&entries);
        let (alice, bob) = (Uuid::new_v4(), Uuid::new_v4());
        let telemetry = Telemetry::new();
        let metrics = Arc::new(StageMetrics::new(&telemetry.provider.meter("rep-e2e")));
        Self {
            rig,
            snapshot,
            gate: gate(&[alice, bob]),
            store: Arc::new(MemStore::default()),
            metrics,
            telemetry,
            role,
            account,
            alice,
            bob,
        }
    }

    /// Real per-invocation stage scope for the caller app in `community`,
    /// with the reputation + kv wirings the production stage attaches.
    fn caps(
        &self,
        community: i32,
        app: &str,
        version: i64,
        event: &str,
    ) -> Arc<dyn CapabilityHandler> {
        let kv_caps = Arc::new(CapabilitySnapshot::new());
        kv_caps.update(REP.to_string(), ["storage.kv".to_string()]);
        let c = StageCapabilities::new(
            "acme".to_string(),
            Some("main".to_string()),
            app.to_string(),
            TENANT,
            community,
            version,
            egress(),
            Arc::clone(&self.gate),
        )
        .with_kv(EmptyKv, kv_caps)
        .with_reputation(ReputationWiring {
            store: Arc::clone(&self.store) as Arc<dyn ReputationStore>,
            flag: Arc::new(StaticGate(true)),
        })
        .with_tool(ToolContext {
            connections: Arc::clone(&self.rig.registry),
            snapshot: self.snapshot.clone(),
            chain: vec![app.to_string()],
            deadline: Some(Instant::now() + Duration::from_secs(20)),
            trace: None,
            read_only: false,
        })
        .with_stage_metrics(Arc::clone(&self.metrics))
        .with_identity(
            IdentityWiring {
                directory: Arc::new(Roster { role: self.role }) as Arc<dyn MemberDirectory>,
                handles: None,
                flag: Arc::new(StaticGate(true)),
            },
            Arc::new(
                InvocationIdentity::new("twitch", self.account.map(str::to_string), vec![])
                    .with_event_id(event),
            ),
        );
        Arc::new(c)
    }

    /// The caller app's `go` tool: invokes `REP.tool(args)` via the real SDK
    /// client; the host's typed outcome comes straight back.
    async fn go_in(
        &self,
        community: i32,
        event: &str,
        tool: &str,
        args: Value,
        key: &str,
    ) -> Value {
        let caps = self.caps(community, CALLER, V_CALLER, event);
        let req = json!({"target": REP, "tool": tool, "args": args, "key": key});
        let reply = tokio::time::timeout(
            SCENARIO_CAP,
            self.rig
                .call_tool(CALLER, CALLER_WASM, "go", req, 30_000, caps),
        )
        .await
        .expect("scenario must not hang")
        .expect("host-api transport");
        match reply {
            Message::Result(r) => r.payload,
            other => panic!("expected a tool envelope, got {other:?}"),
        }
    }

    async fn go(&self, event: &str, tool: &str, args: Value, key: &str) -> Value {
        self.go_in(COMMUNITY_A, event, tool, args, key).await
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn a_draw_filter_fetches_scores_in_one_get_many_through_tool_invoke() {
    let w = World::new(None, None).await;
    w.store
        .scores
        .lock()
        .unwrap()
        .insert((COMMUNITY_A, w.alice), 120);
    let out = w
        .go(
            "evt-a",
            "get_many",
            json!({"users": [w.bob.to_string(), w.alice.to_string()]}),
            "k-a",
        )
        .await;
    // Request order preserved; tier derived by the bundle from the score.
    let entries = out["ok"]["entries"].as_array().expect("entries");
    assert_eq!(entries.len(), 2, "{out}");
    assert_eq!(entries[0]["user"], json!(w.bob.to_string()), "{out}");
    assert_eq!(entries[0]["score"], 0, "{out}");
    assert_eq!(entries[1]["user"], json!(w.alice.to_string()), "{out}");
    assert_eq!(entries[1]["score"], 120, "{out}");
    assert!(entries[1]["tier"].is_string(), "{out}");

    // Telemetry gate: counts printed, zero is a FAIL.
    let ok = |t: &'static str| [("target", REP), ("tool", t), ("outcome", "ok")];
    let counts = [
        (
            "waddles_tool_call",
            w.telemetry.count("waddles_tool_call", &ok("get_many")),
        ),
        (
            "waddles_tool_call_latency",
            w.telemetry
                .count("waddles_tool_call_latency", &ok("get_many")),
        ),
    ];
    println!("TELEMETRY counts: {counts:?}");
    for (name, n) in counts {
        assert!(n >= 1.0, "{name}: zero data points is a FAIL");
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn b_admin_override_lands_and_is_ledgered_for_a_verified_admin() {
    let w = World::new(Some("community-admin"), Some("1001")).await;
    // A spoofed `actor` argument is refused outright -- never trusted.
    let spoof = w
        .go(
            "evt-b0",
            "override",
            json!({"actor": ACTOR_UUID, "user": w.alice.to_string(), "absolute_score": -50}),
            "k-b0",
        )
        .await;
    println!("SPOOF {spoof}");
    assert!(spoof.get("ok").is_none(), "{spoof}");
    assert!(
        w.store.ledger().is_empty(),
        "spoof must not reach the store"
    );

    let out = w
        .go(
            "evt-b1",
            "override",
            json!({"user": w.alice.to_string(), "absolute_score": -50, "reason": "admin.adjust"}),
            "k-b1",
        )
        .await;
    assert_eq!(out["ok"]["score"], -50, "{out}");
    assert_eq!(w.store.score(COMMUNITY_A, w.alice), -50);
    assert_eq!(
        w.store.ledger(),
        vec![Ledger {
            community: COMMUNITY_A,
            user: w.alice,
            kind: "admin_override",
            value: -50,
        }],
        "the override is audit-ledgered as admin_override"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn c_override_is_denied_for_non_admins_before_the_store() {
    for (role, account) in [
        (Some("moderator"), Some("1001")),
        (Some("member"), Some("1001")),
        (None, Some("1001")),
        (Some("community-owner"), None),
    ] {
        let w = World::new(role, account).await;
        let out = w
            .go(
                "evt-c",
                "override",
                json!({"user": w.alice.to_string(), "absolute_score": 999}),
                "k-c",
            )
            .await;
        assert_eq!(
            out,
            json!({"tool_error": "Forbidden"}),
            "{role:?}/{account:?}"
        );
        assert!(w.store.ledger().is_empty(), "{role:?}: store untouched");
        assert_eq!(w.store.score(COMMUNITY_A, w.alice), 0);
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn d_the_24h_cap_is_enforced_by_the_host_whatever_the_bundle_policy_says() {
    let w = World::new(None, None).await;
    // Default policy: win = +1; the catalog per-user 24h |delta| cap is 5.
    let mut applied = 0;
    let mut refused = None;
    for i in 0..8 {
        let out = w
            .go(
                &format!("evt-d{i}"),
                "adjust",
                json!({"user": w.alice.to_string(), "event": "win"}),
                &format!("k-d{i}"),
            )
            .await;
        if out.get("ok").is_some() {
            applied += 1;
        } else {
            refused = Some(out);
            break;
        }
    }
    println!("CAP applied={applied} refused={refused:?}");
    assert_eq!(applied, 5, "exactly the catalog cap worth of deltas lands");
    assert!(refused.is_some(), "the 6th is refused by the host cap");
    assert_eq!(w.store.score(COMMUNITY_A, w.alice), 5, "no bypass");
}

#[tokio::test(flavor = "multi_thread")]
async fn e_scores_never_cross_communities() {
    let w = World::new(None, None).await;
    w.store
        .scores
        .lock()
        .unwrap()
        .insert((COMMUNITY_A, w.alice), 77);
    // Same user, community B scope: host-asserted membership refuses the read
    // and the write; A's data is untouched.
    let read = w
        .go_in(
            COMMUNITY_B,
            "evt-e1",
            "get_score",
            json!({"user": w.alice.to_string()}),
            "k-e1",
        )
        .await;
    println!("ISOLATION read={read}");
    assert!(read.get("ok").is_none(), "{read}");
    let write = w
        .go_in(
            COMMUNITY_B,
            "evt-e2",
            "adjust",
            json!({"user": w.alice.to_string(), "event": "win"}),
            "k-e2",
        )
        .await;
    assert!(write.get("ok").is_none(), "{write}");
    assert_eq!(w.store.score(COMMUNITY_A, w.alice), 77);
    assert_eq!(w.store.score(COMMUNITY_B, w.alice), 0);
    assert!(w.store.ledger().is_empty());
}

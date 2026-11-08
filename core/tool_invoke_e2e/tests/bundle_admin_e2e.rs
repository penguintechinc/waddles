//! Real-path e2e for the host side of hub-api's bundle-admin gateway.
//!
//! A request SIGNED exactly as hub-api's `bundle_tool_transport.sign_call` signs one is pushed onto
//! a REAL Valkey list; svc-process's REAL `bundle_admin::run_consumer` pops it, verifies the MAC,
//! applies the replay guard, authorizes it through the shared admin planner and runs the REAL
//! production reputation bundle (`tests/fixtures/reputation_service.wasm`) in the REAL wasmtime
//! executor, with the bundle's `reputation` / `kv` imports served by svc-process's REAL
//! `StageCapabilities`. The reply is read back from the REAL result list. Only the durable
//! reputation store and kv are in-memory (as in `reputation_service_e2e.rs`); the executor adapter
//! below builds the same capability set `StageAdminExecutor` does, minus the production
//! `SupervisorDeps` (which needs a database).
//!
//! Needs `VALKEY_URL` (a real Valkey); an unset var FAILS, never skips.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bundle_active_set::{
    ActiveBundleRow, ActiveVersionSnapshot, ExportedToolInfo, ToolManifestInfo,
};
use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_kv::ReconcileOutcome;
use bundle_host_kv::{BoxFuture as KvFuture, CapabilitySnapshot, KvBackend, QuotaOutcome};
use bundle_host_reputation::{
    BoxFuture, IdempotencyKey, ReputationCaps, ReputationError, ReputationScope, ReputationStore,
    ScoreEntry,
};
use bundle_tool_call::{ActorContext, ToolRefusal};
use serde_json::{json, Value};
use svc_process::bundle_admin::{
    run_consumer, AdminExecutor, AdminRequest, KeyRing, ValkeyBackend, REQUEST_QUEUE, RESULT_PREFIX,
};
use svc_process::capabilities::{
    HttpEgressCatalog, ReputationWiring, StageCapabilities, ToolContext,
};
use svc_process::identity::InvocationIdentity;
use svc_process::license::StaticGate;
use tool_invoke_e2e::{digest_of, Rig};
use uuid::Uuid;

const REP_WASM: &[u8] = include_bytes!("fixtures/reputation_service.wasm");
const REP: &str = "waddles.core.service.reputation";
const TENANT: i32 = 7;
const COMMUNITY: i32 = 3;
const V_REP: i64 = 22;
const KEY: [u8; 32] = [7u8; 32];

const REP_TOOLS: [(&str, bool, Option<&str>); 3] = [
    ("leaderboard", false, None),
    ("adjust", true, None),
    ("override", true, Some("community:admin")),
];

#[derive(Default)]
struct MemStore {
    scores: Mutex<HashMap<(i32, Uuid), i64>>,
    overrides: Mutex<Vec<(Uuid, i64)>>,
}

impl ReputationStore for MemStore {
    fn get<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            Ok(*self
                .scores
                .lock()
                .unwrap()
                .get(&(scope.community_id, user))
                .unwrap_or(&0))
        })
    }
    fn adjust<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _u: Uuid,
        _d: i32,
        _r: &'a str,
        _c: ReputationCaps,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async { Err(ReputationError::Backend("unused in this test".into())) })
    }
    fn override_score<'a>(
        &'a self,
        scope: &'a ReputationScope,
        user: Uuid,
        absolute_score: i64,
        _r: &'a str,
        _k: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async move {
            self.scores
                .lock()
                .unwrap()
                .insert((scope.community_id, user), absolute_score);
            self.overrides.lock().unwrap().push((user, absolute_score));
            Ok(absolute_score)
        })
    }
    fn adjust_many<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _i: &'a [(Uuid, i32)],
        _r: &'a str,
        _c: ReputationCaps,
        _k: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async { Err(ReputationError::Backend("unused in this test".into())) })
    }
    fn get_many<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _u: &'a [Uuid],
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async { Ok(Vec::new()) })
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
        Box::pin(async { Err("read-only".to_string()) })
    }
    fn delete<'a>(
        &'a self,
        _d: &'a str,
        _l: &'a str,
        _c: &'a str,
    ) -> KvFuture<'a, Result<bool, String>> {
        Box::pin(async { Err("read-only".to_string()) })
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
        Box::pin(async { Err("read-only".to_string()) })
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

fn grants(ids: Vec<(&str, Value)>) -> GrantSet {
    GrantSet {
        permission_snapshot_hash: "e2e".to_string(),
        grants: ids
            .into_iter()
            .map(|(id, params)| {
                (
                    id.to_string(),
                    GrantedPermission {
                        permission_id: id.to_string(),
                        params,
                    },
                )
            })
            .collect(),
    }
}

/// The reputation service's OWN grants (matching its hub-manifest); it needs no `tool.call`
/// grant to run its own admin tools.
fn gate(members: &[Uuid]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT,
            community_id: COMMUNITY,
            app_id: REP.to_string(),
            app_version: V_REP,
        },
        grants(vec![
            ("reputation.read", json!({})),
            (
                "reputation.community.write",
                json!({"delta_min": -5, "delta_max": 5,
                       "reason_codes": ["game.win", "game.loss", "loyalty.bonus", "admin.adjust"]}),
            ),
            ("reputation.community.admin", json!({})),
            ("storage.kv", json!({})),
        ]),
    );
    let membership = InMemoryMembership::new();
    for m in members {
        membership.add_community_member(TENANT, COMMUNITY, *m);
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
            prometheus::Opts::new("admin_e2e_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition"),
        bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
    ))
}

struct Harness {
    rig: Rig,
    snapshot: ActiveVersionSnapshot,
    gate: Arc<CapabilityGate>,
    store: Arc<MemStore>,
    alice: Uuid,
    bob: Uuid,
}

/// Builds the caller's capability set like `StageAdminExecutor` and runs the shared admin planner.
struct E2eExecutor(Arc<Harness>);

impl AdminExecutor for E2eExecutor {
    async fn execute(
        &self,
        req: &AdminRequest,
        scope: (i32, i32),
        args: &Value,
        actor: ActorContext,
    ) -> Result<Value, ToolRefusal> {
        let h = &self.0;
        let kv_caps = Arc::new(CapabilitySnapshot::new());
        kv_caps.update(REP.to_string(), ["storage.kv".to_string()]);
        let caps = StageCapabilities::new(
            scope.0.to_string(),
            Some(scope.1.to_string()),
            req.caller_app_id.clone(),
            scope.0,
            scope.1,
            V_REP,
            egress(),
            Arc::clone(&h.gate),
        )
        .with_verified_scope(Some(scope))
        .with_kv(EmptyKv, kv_caps)
        .with_reputation(ReputationWiring {
            store: Arc::clone(&h.store) as Arc<dyn ReputationStore>,
            flag: Arc::new(StaticGate(true)),
        })
        .with_tool(ToolContext {
            connections: Arc::clone(&h.rig.registry),
            snapshot: h.snapshot.clone(),
            chain: vec![req.caller_app_id.clone()],
            deadline: Some(Instant::now() + Duration::from_secs(20)),
            trace: None,
            read_only: false,
        })
        .with_identity(
            svc_process::identity::IdentityWiring {
                directory: Arc::new(NoDirectory),
                handles: None,
                flag: Arc::new(StaticGate(true)),
            },
            Arc::new(InvocationIdentity::for_verified_admin(actor.clone())),
        );
        caps.invoke_admin_tool(args, actor).await
    }
}

/// The admin path must never need the member directory: the signed actor is pre-verified.
struct NoDirectory;

impl svc_process::identity::MemberDirectory for NoDirectory {
    fn member_by_platform_id<'a>(
        &'a self,
        _s: svc_process::identity::IdentityScope,
        _p: &'a str,
        _id: &'a str,
    ) -> svc_process::identity::BoxFuture<'a, Result<Uuid, svc_process::identity::IdentityError>>
    {
        Box::pin(async { Err(svc_process::identity::IdentityError::NotAMember) })
    }
    fn role_for<'a>(
        &'a self,
        _s: svc_process::identity::IdentityScope,
        _p: &'a str,
        _id: &'a str,
    ) -> svc_process::identity::BoxFuture<'a, Result<String, svc_process::identity::IdentityError>>
    {
        Box::pin(async { Err(svc_process::identity::IdentityError::NotLinked) })
    }
    fn confirm_member<'a>(
        &'a self,
        _s: svc_process::identity::IdentityScope,
        user: Uuid,
    ) -> svc_process::identity::BoxFuture<'a, Result<Uuid, svc_process::identity::IdentityError>>
    {
        Box::pin(async move { Ok(user) })
    }
}

fn rep_row() -> ActiveBundleRow {
    ActiveBundleRow {
        app_id: REP.to_string(),
        version: "1.0.0".to_string(),
        version_id: V_REP,
        digest: digest_of(REP_WASM),
        component_key: String::new(),
        sidecar_key: String::new(),
        artifact_signature: None,
        artifact_signature_key_id: None,
        artifact_signed_approval_id: None,
        declared_capabilities: Vec::new(),
        tools: ToolManifestInfo {
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
    }
}

async fn harness() -> Arc<Harness> {
    let rig = Rig::start(&[(REP, REP_WASM)])
        .await
        .expect("rig starts with the real reputation component loaded");
    let snapshot = ActiveVersionSnapshot::new();
    let mut entries = HashMap::new();
    entries.insert((TENANT, COMMUNITY, REP.to_string()), rep_row());
    snapshot.update_scoped_all(&entries);
    let (alice, bob) = (Uuid::new_v4(), Uuid::new_v4());
    Arc::new(Harness {
        rig,
        snapshot,
        gate: gate(&[alice, bob]),
        store: Arc::new(MemStore::default()),
        alice,
        bob,
    })
}

fn valkey_url() -> String {
    std::env::var("VALKEY_URL")
        .expect("VALKEY_URL must be set: the bundle-admin e2e needs a REAL Valkey (no silent skip)")
}

fn now_ms() -> i64 {
    chrono_now_ms()
}

fn chrono_now_ms() -> i64 {
    i64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}

/// Starts the real consumer and returns a connection for pushing requests / reading replies.
async fn start(
    h: &Arc<Harness>,
) -> (
    redis::aio::MultiplexedConnection,
    tokio::sync::oneshot::Sender<()>,
) {
    let client = redis::Client::open(valkey_url()).unwrap();
    // Long response timeout: the reader BLPOPs for up to 20s.
    let cfg =
        redis::AsyncConnectionConfig::new().set_response_timeout(Some(Duration::from_secs(60)));
    let conn = client
        .get_multiplexed_async_connection_with_config(&cfg)
        .await
        .unwrap();
    // A previous run's leftovers must not feed this one.
    let mut c = conn.clone();
    let _: () = redis::cmd("DEL")
        .arg(REQUEST_QUEUE)
        .query_async(&mut c)
        .await
        .unwrap();
    let ring = Arc::new(KeyRing::parse(&format!("k1:{}", "07".repeat(32))).unwrap());
    // The backend gets its OWN connection: the reader below BLPOPs on `conn`, and a blocking
    // command stalls every other user of a multiplexed connection (production hub-api opens a
    // fresh connection per call for the same reason).
    let backend_conn = client.get_multiplexed_async_connection().await.unwrap();
    let backend = Arc::new(ValkeyBackend::new(backend_conn));
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    tokio::spawn(run_consumer(
        client,
        ring,
        backend,
        Arc::new(E2eExecutor(Arc::clone(h))),
        4,
        async {
            let _ = rx.await;
        },
    ));
    (conn, tx)
}

fn signed(id: &str, scope: &str, tool: &str, args: &str) -> AdminRequest {
    AdminRequest::signed(
        &KEY,
        "k1",
        id,
        (i64::from(TENANT), i64::from(COMMUNITY)),
        (42, scope),
        (REP, REP),
        tool,
        args,
        now_ms() + 60_000,
    )
    .unwrap()
}

/// Pushes `wire` and waits up to `wait` for the reply on the request's result list.
async fn roundtrip(
    conn: &redis::aio::MultiplexedConnection,
    id: &str,
    wire: String,
    wait: u64,
) -> Option<Value> {
    let mut c = conn.clone();
    let key = format!("{RESULT_PREFIX}{id}");
    let _: () = redis::cmd("DEL")
        .arg(&key)
        .query_async(&mut c)
        .await
        .unwrap();
    let _: () = redis::cmd("LPUSH")
        .arg(REQUEST_QUEUE)
        .arg(wire)
        .query_async(&mut c)
        .await
        .unwrap();
    let popped: Option<(String, String)> = redis::cmd("BLPOP")
        .arg(&key)
        .arg(wait)
        .query_async(&mut c)
        .await
        .unwrap();
    popped.map(|(_, raw)| serde_json::from_str(&raw).unwrap())
}

fn unique(prefix: &str) -> String {
    format!("{prefix}-{}", Uuid::new_v4().simple())
}

#[tokio::test(flavor = "multi_thread")]
async fn hub_api_signed_requests_reach_the_real_reputation_bundle_and_reply() {
    let h = harness().await;
    let (conn, stop) = start(&h).await;
    let (alice, bob) = (h.alice, h.bob);
    h.store
        .scores
        .lock()
        .unwrap()
        .insert((COMMUNITY, alice), 120);
    h.store.scores.lock().unwrap().insert((COMMUNITY, bob), 5);

    // 1. A moderator reads the leaderboard (no required_scope tool).
    let id = unique("lb");
    let reply = roundtrip(
        &conn,
        &id,
        signed(&id, "community:moderate", "leaderboard", r#"{"limit":5}"#).to_wire(),
        20,
    )
    .await
    .expect("leaderboard reply");
    assert_eq!(reply["ok"], true, "{reply}");
    let entries = reply["result"]["entries"].as_array().expect("entries");
    assert_eq!(entries.len(), 2, "{reply}");
    assert_eq!(entries[0]["user"], json!(alice.to_string()), "{reply}");
    assert_eq!(entries[0]["score"], 120, "{reply}");

    // 2. A community admin overrides a score: lands in the store, replied ok.
    let id = unique("ov");
    let args = json!({"user": bob.to_string(), "absolute_score": 77, "reason": "admin.adjust"})
        .to_string();
    let reply = roundtrip(
        &conn,
        &id,
        signed(&id, "community:admin", "override", &args).to_wire(),
        20,
    )
    .await
    .expect("override reply");
    assert_eq!(reply["ok"], true, "{reply}");
    assert_eq!(reply["result"]["score"], 77, "{reply}");
    assert_eq!(h.store.overrides.lock().unwrap().as_slice(), &[(bob, 77)]);

    // 3. A moderator's signed scope does NOT satisfy `community:admin`: refused before the store.
    let id = unique("mod");
    let args = json!({"user": alice.to_string(), "absolute_score": -999}).to_string();
    let reply = roundtrip(
        &conn,
        &id,
        signed(&id, "community:moderate", "override", &args).to_wire(),
        20,
    )
    .await
    .expect("refusal reply");
    assert_eq!(reply["ok"], false, "{reply}");
    assert_eq!(reply["code"], "forbidden", "{reply}");
    assert_eq!(
        h.store.overrides.lock().unwrap().len(),
        1,
        "store untouched"
    );

    // 4. Replay: the same signed bytes (same request_id) never run twice.
    let id = unique("rp");
    let args = json!({"user": alice.to_string(), "absolute_score": 1}).to_string();
    let wire = signed(&id, "community:admin", "override", &args).to_wire();
    let first = roundtrip(&conn, &id, wire.clone(), 20)
        .await
        .expect("first");
    assert_eq!(first["ok"], true, "{first}");
    let second = roundtrip(&conn, &id, wire, 20).await.expect("replay reply");
    assert_eq!(second["code"], "request_replayed", "{second}");
    assert_eq!(
        h.store.overrides.lock().unwrap().len(),
        2,
        "exactly one more override"
    );

    // 5. A forged MAC is dropped: no reply (untrusted request_id) and nothing executed.
    let id = unique("forged");
    let mut v: Value =
        serde_json::from_str(&signed(&id, "community:admin", "override", &args).to_wire()).unwrap();
    v["actor_scope"] = json!("community:owner");
    assert!(
        roundtrip(&conn, &id, v.to_string(), 3).await.is_none(),
        "forged request must get no reply"
    );
    assert_eq!(h.store.overrides.lock().unwrap().len(), 2);

    // 6. An expired request is refused-by-drop as well.
    let id = unique("old");
    let stale = AdminRequest::signed(
        &KEY,
        "k1",
        &id,
        (i64::from(TENANT), i64::from(COMMUNITY)),
        (42, "community:admin"),
        (REP, REP),
        "override",
        &args,
        now_ms() - 1,
    )
    .unwrap();
    assert!(roundtrip(&conn, &id, stale.to_wire(), 3).await.is_none());

    // Denominator: replies observed = 5 (lb, ov, mod, rp x2) + 2 intentionally silent drops.
    println!("bundle-admin e2e: 5 replies verified, 2 forged/expired requests dropped silently");
    let _ = stop.send(());
}

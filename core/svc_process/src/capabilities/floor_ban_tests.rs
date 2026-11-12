//! Automated hard-ban gate tests: the REAL `handle_tool` dispatch, the real planner/gate and a
//! real handshaken `Connection` to a scripted fake executor, against a stateful fake reputation
//! store (idempotency, throttle, audit and durable-retry records). The store's own SQL is proven
//! against a real Postgres in `alembic/tests/test_0098_reputation_floor_ban.py`.

use std::collections::HashSet;
use std::sync::atomic::{AtomicU32, Ordering};

use bundle_host_reputation::{
    BoxFuture, DueFloorBan, FloorBanBegin, FloorBanOutcome, FloorScope, IdempotencyKey,
    ReputationCaps, ReputationError, ReputationScope, ReputationStore, ScoreEntry,
};
use uuid::Uuid;

use super::*;
use crate::capabilities::floor_ban::{detect_claim, summarise, FloorResult};
use crate::capabilities::ReputationWiring;
use crate::identity::InvocationIdentity;
use crate::license::test_support::FixedGate;
use bundle_tool_call::ToolRequest;

const LOGIC: &str = "waddles.core.app.reputation-accrual";
const REP: &str = "waddles.core.service.reputation";
const DISCORD: &str = "waddles.core.service.discord";
const BAN: &str = "discord.moderation.ban";
const USER: &str = "3f2b8c1e-9d4a-4c55-8a10-0a1b2c3d4e5f";
const LOGIC_VERSION: i64 = 11;
const REP_VERSION: i64 = 22;
const DISCORD_VERSION: i64 = 33;

fn unsupported() -> ReputationError {
    ReputationError::Backend("unused in this test".to_string())
}

/// Stateful fake of the floor-ban half of the store.
struct FloorStore {
    /// `Some(app)` answers the lookup, `None` makes it fail.
    active: Mutex<Option<Option<String>>>,
    communities: Vec<i32>,
    not_breached: Mutex<HashSet<i32>>,
    done: Mutex<HashSet<(i32, String)>>,
    rate_limited: Mutex<bool>,
    due: Mutex<Vec<DueFloorBan>>,
    begins: Mutex<Vec<(i32, i32, String, String)>>,
    records: Mutex<Vec<(i32, FloorBanOutcome, String)>>,
    begin_calls: AtomicU32,
}

impl FloorStore {
    fn new(active: &str, communities: &[i32]) -> Arc<Self> {
        Arc::new(Self {
            active: Mutex::new(Some(Some(active.to_string()))),
            communities: communities.to_vec(),
            not_breached: Mutex::default(),
            done: Mutex::default(),
            rate_limited: Mutex::new(false),
            due: Mutex::default(),
            begins: Mutex::default(),
            records: Mutex::default(),
            begin_calls: AtomicU32::new(0),
        })
    }
    fn records(&self) -> Vec<(i32, FloorBanOutcome, String)> {
        let mut r = self.records.lock().unwrap().clone();
        r.sort_by_key(|(c, _, _)| *c);
        r
    }
}

impl ReputationStore for FloorStore {
    fn get<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _u: Uuid,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async { Err(unsupported()) })
    }
    fn adjust<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _u: Uuid,
        _d: i32,
        _r: &'a str,
        _c: ReputationCaps,
        _k: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<i64, ReputationError>> {
        Box::pin(async { Err(unsupported()) })
    }
    fn adjust_many<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _i: &'a [(Uuid, i32)],
        _r: &'a str,
        _c: ReputationCaps,
        _k: &'a IdempotencyKey,
    ) -> BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async { Err(unsupported()) })
    }
    fn get_many<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _u: &'a [Uuid],
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async { Err(unsupported()) })
    }
    fn leaderboard<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _l: u32,
    ) -> BoxFuture<'a, Result<Vec<ScoreEntry>, ReputationError>> {
        Box::pin(async { Err(unsupported()) })
    }
    fn active_logic_app<'a>(
        &'a self,
        _s: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Option<String>, ReputationError>> {
        Box::pin(async {
            self.active
                .lock()
                .unwrap()
                .clone()
                .ok_or_else(|| ReputationError::Backend("db down".into()))
        })
    }
    fn tenant_communities<'a>(
        &'a self,
        _s: &'a ReputationScope,
    ) -> BoxFuture<'a, Result<Vec<i32>, ReputationError>> {
        Box::pin(async { Ok(self.communities.clone()) })
    }
    fn verify_floor_breach<'a>(
        &'a self,
        scope: &'a ReputationScope,
        _u: Uuid,
        _f: FloorScope,
    ) -> BoxFuture<'a, Result<bool, ReputationError>> {
        Box::pin(async move {
            Ok(!self
                .not_breached
                .lock()
                .unwrap()
                .contains(&scope.community_id))
        })
    }
    fn floor_ban_begin<'a>(
        &'a self,
        scope: &'a ReputationScope,
        target: i32,
        _u: Uuid,
        _f: FloorScope,
        key: &'a str,
        platform: &'a str,
    ) -> BoxFuture<'a, Result<FloorBanBegin, ReputationError>> {
        Box::pin(async move {
            self.begin_calls.fetch_add(1, Ordering::SeqCst);
            self.begins.lock().unwrap().push((
                scope.community_id,
                target,
                key.to_string(),
                platform.to_string(),
            ));
            if self
                .done
                .lock()
                .unwrap()
                .contains(&(target, key.to_string()))
            {
                return Ok(FloorBanBegin::AlreadyDone);
            }
            if *self.rate_limited.lock().unwrap() {
                return Ok(FloorBanBegin::RateLimited);
            }
            Ok(FloorBanBegin::Proceed)
        })
    }
    fn floor_ban_record<'a>(
        &'a self,
        _s: &'a ReputationScope,
        target: i32,
        _u: Uuid,
        _f: FloorScope,
        key: &'a str,
        outcome: FloorBanOutcome,
        error: &'a str,
    ) -> BoxFuture<'a, Result<(), ReputationError>> {
        Box::pin(async move {
            if outcome == FloorBanOutcome::Done {
                self.done.lock().unwrap().insert((target, key.to_string()));
            }
            self.records
                .lock()
                .unwrap()
                .push((target, outcome, error.to_string()));
            Ok(())
        })
    }
    fn floor_ban_due<'a>(
        &'a self,
        _s: &'a ReputationScope,
        _l: u32,
    ) -> BoxFuture<'a, Result<Vec<DueFloorBan>, ReputationError>> {
        Box::pin(async { Ok(std::mem::take(&mut *self.due.lock().unwrap())) })
    }
}

fn rows() -> Vec<ActiveBundleRow> {
    let ban = ExportedToolInfo {
        name: BAN.to_string(),
        write: true,
        required_scope: Some("community:moderate".to_string()),
        host_op: None,
    };
    vec![
        row(LOGIC, LOGIC_VERSION, app(&[(REP, "get_score", false)])),
        row(
            REP,
            REP_VERSION,
            service(&[("get_score", false)], &[(DISCORD, BAN, true)]),
        ),
        row(
            DISCORD,
            DISCORD_VERSION,
            ToolManifestInfo {
                is_tool_service: true,
                exported: vec![ban],
                requested: Vec::new(),
            },
        ),
    ]
}

/// A gate granting the reputation service `tool.call` on the ban tool in each community.
fn ban_gate(communities: &[i32]) -> Arc<CapabilityGate> {
    let snapshot = InMemoryGrantSnapshot::new();
    let cid = PermissionId::ToolCall {
        target: DISCORD.to_string(),
        tool: BAN.to_string(),
    }
    .canonical_id();
    for c in communities {
        snapshot.set(
            GrantScopeKey {
                tenant_id: TENANT_ID,
                community_id: *c,
                app_id: REP.to_string(),
                app_version: REP_VERSION,
            },
            GrantSet {
                permission_snapshot_hash: "h".to_string(),
                grants: HashMap::from([(
                    cid.clone(),
                    GrantedPermission {
                        permission_id: cid.clone(),
                        params: serde_json::json!({}),
                    },
                )]),
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

struct World {
    exec: FakeExecutor,
    store: Arc<FloorStore>,
    caps: StageCapabilities,
}

/// The reputation SERVICE's capability set at hop 2 (`chain = [LOGIC, REP]`): `snapshot_in`
/// communities carry the bundles, `granted` communities carry the service's ban grant.
async fn world(
    app_id: &str,
    chain: Vec<&str>,
    snapshot_in: &[i32],
    granted: &[i32],
    flag_on: bool,
    applied: bool,
) -> World {
    let exec = fake_executor(Behavior::Reply(serde_json::json!({"ok": {"banned": true}}))).await;
    let entries: Vec<(i32, i32, ActiveBundleRow)> = snapshot_in
        .iter()
        .flat_map(|c| rows().into_iter().map(move |r| (TENANT_ID, *c, r)))
        .collect();
    let chain: Vec<String> = chain.into_iter().map(str::to_string).collect();
    let mut caps = caps_scoped(
        entries,
        ban_gate(granted),
        Arc::clone(&exec.registry),
        chain,
        Some(Instant::now() + Duration::from_secs(10)),
    );
    caps.app_id = app_id.to_string();
    caps.app_version = REP_VERSION;
    let store = FloorStore::new(LOGIC, &[3, 4, 5, 6]);
    caps.reputation = Some(ReputationWiring {
        store: Arc::clone(&store) as Arc<dyn ReputationStore>,
        flag: Arc::new(FixedGate(true)),
        auto_ban_flag: Some(Arc::new(FixedGate(flag_on))),
    });
    let identity = InvocationIdentity::new("discord", Some("12345".to_string()), vec![]);
    if applied {
        identity.mark_reputation_applied();
    }
    caps.invocation_identity = Some(Arc::new(identity));
    World { exec, store, caps }
}

async fn standard(applied: bool, flag_on: bool) -> World {
    world(REP, vec![LOGIC, REP], &[3], &[3], flag_on, applied).await
}

fn ban_call(reason: &str, key: Option<&str>) -> HostCallBody {
    HostCallBody {
        app_id: REP.to_string(),
        capability: CapabilityKind::Db,
        op: "tool.invoke".to_string(),
        args: serde_json::json!({
            "target": DISCORD, "tool": BAN,
            "args": {"user": USER, "reason": reason}, "idempotency_key": key,
        }),
        call_id: 1,
    }
}

#[tokio::test]
async fn community_hard_floor_ban_happy_path_carries_the_host_asserted_automated_actor() {
    let w = standard(true, true).await;
    let out = run(&w.caps, ban_call("community_hard_floor", Some("evt1:hard")))
        .await
        .unwrap();
    assert_eq!(out["ok"]["banned"], 1);
    let inv = recorded(&w.exec);
    assert_eq!(inv.len(), 1);
    assert_eq!(inv[0].app_id, DISCORD);
    // The actor is the fixed automated identity with ONLY the moderate scope.
    assert_eq!(
        inv[0].payload["args"]["__ctx"],
        serde_json::json!({"actor": "system:reputation_floor", "scopes": ["community:moderate"]})
    );
    assert_eq!(
        w.store.records(),
        vec![(3, FloorBanOutcome::Done, String::new())]
    );
    let begins = w.store.begins.lock().unwrap().clone();
    assert_eq!(
        begins,
        vec![(3, 3, "evt1:hard".to_string(), "discord".to_string())]
    );
}

#[tokio::test]
async fn a_non_logic_app_chain_origin_is_forbidden_and_never_acts() {
    let w = standard(true, true).await;
    *w.store.active.lock().unwrap() = Some(Some("waddles.core.app.other-logic".to_string()));
    let err = run(&w.caps, ban_call("community_hard_floor", Some("k1")))
        .await
        .unwrap_err();
    assert_eq!(err, "forbidden");
    assert!(recorded(&w.exec).is_empty());
    assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn no_or_ambiguous_active_logic_app_and_a_lookup_failure_all_fail_closed() {
    for active in [Some(None), None] {
        let w = standard(true, true).await;
        *w.store.active.lock().unwrap() = active;
        assert_eq!(
            run(&w.caps, ban_call("community_hard_floor", Some("k1")))
                .await
                .unwrap_err(),
            "forbidden"
        );
        assert!(recorded(&w.exec).is_empty());
        assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 0);
    }
}

#[tokio::test]
async fn only_the_core_reputation_service_hop_chain_is_eligible() {
    // A third-party caller impersonating the service shape, a service called directly (chain of
    // one), and a deeper chain are all refused by the ordinary scope check.
    let cases: [(&str, Vec<&str>); 3] = [
        (
            "waddles.acme.service.reputation",
            vec![LOGIC, "waddles.acme.service.reputation"],
        ),
        (REP, vec![REP]),
        (REP, vec![LOGIC, "waddles.acme.app.relay", REP]),
    ];
    for (app_id, chain) in cases {
        let w = world(app_id, chain, &[3], &[3], true, true).await;
        let code = run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err();
        // Ineligible: the ordinary scope check refuses (a too-deep chain trips the depth guard).
        assert!(
            code == "forbidden" || code == "depth_exceeded",
            "{app_id}: {code}"
        );
        assert!(recorded(&w.exec).is_empty());
        assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 0);
    }
}

#[tokio::test]
async fn human_origin_without_moderator_scope_and_no_prior_apply_is_forbidden() {
    // The same eligible chain, but NO reputation apply succeeded in this invocation: the
    // automated actor is not attached and the human (no `community:moderate`) is refused.
    let w = standard(false, true).await;
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&w.exec).is_empty());
    assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn a_non_floor_reason_never_gets_the_automated_actor() {
    let w = standard(true, true).await;
    for reason in ["manual", "community_soft_floor", ""] {
        assert_eq!(
            run(&w.caps, ban_call(reason, Some("k1")))
                .await
                .unwrap_err(),
            "forbidden",
            "{reason:?}"
        );
    }
    assert!(recorded(&w.exec).is_empty());
}

#[tokio::test]
async fn floor_not_breached_is_refused_recorded_and_no_ban_runs() {
    let w = standard(true, true).await;
    w.store.not_breached.lock().unwrap().insert(3);
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&w.exec).is_empty(), "no platform call");
    assert_eq!(
        w.store.records(),
        vec![(
            3,
            FloorBanOutcome::Refused,
            "floor_not_breached".to_string()
        )]
    );
}

#[tokio::test]
async fn flag_off_or_unwired_refuses_before_touching_the_store() {
    let w = standard(true, false).await;
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    let mut w2 = standard(true, true).await;
    if let Some(r) = w2.caps.reputation.as_mut() {
        r.auto_ban_flag = None;
    }
    assert_eq!(
        run(&w2.caps, ban_call("tenant_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    for w in [&w, &w2] {
        assert!(recorded(&w.exec).is_empty());
        assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 0);
        assert!(w.store.records().is_empty());
    }
}

#[tokio::test]
async fn tenant_fan_out_records_every_community_and_a_failing_one_is_retried_durably() {
    // Community 4 has no ban grant (the per-community authorization refuses it); 3, 5, 6 succeed.
    let w = world(REP, vec![LOGIC, REP], &[3, 4, 5, 6], &[3, 5, 6], true, true).await;
    let err = run(&w.caps, ban_call("tenant_floor", Some("evt9:tenant")))
        .await
        .unwrap_err();
    assert_eq!(
        err, "target_unavailable",
        "a partial fan-out is never reported as success"
    );
    assert_eq!(
        recorded(&w.exec).len(),
        3,
        "only the granted communities were banned"
    );
    assert_eq!(
        w.store.records(),
        vec![
            (3, FloorBanOutcome::Done, String::new()),
            (4, FloorBanOutcome::Failed, "not_granted".to_string()),
            (5, FloorBanOutcome::Done, String::new()),
            (6, FloorBanOutcome::Done, String::new()),
        ]
    );
    // Every begun slot names the ORIGIN community (3) and the claim's platform.
    let begins = w.store.begins.lock().unwrap().clone();
    assert_eq!(begins.len(), 4);
    assert!(begins
        .iter()
        .all(|(origin, _, k, p)| *origin == 3 && k == "evt9:tenant" && p == "discord"));
}

#[tokio::test]
async fn a_due_durable_retry_is_re_run_and_recorded_on_the_next_floor_call() {
    let w = world(REP, vec![LOGIC, REP], &[3, 4], &[3, 4], true, true).await;
    let user = Uuid::parse_str(USER).unwrap();
    w.store.due.lock().unwrap().push(DueFloorBan {
        community_id: 4,
        origin_community_id: 3,
        user,
        scope: FloorScope::Tenant,
        key: "evt-old:tenant".to_string(),
        platform: "discord".to_string(),
    });
    run(
        &w.caps,
        ban_call("community_hard_floor", Some("evt-new:hard")),
    )
    .await
    .unwrap();
    assert_eq!(
        recorded(&w.exec).len(),
        2,
        "the new ban and the retried one"
    );
    assert_eq!(
        w.store.records(),
        vec![
            (3, FloorBanOutcome::Done, String::new()),
            (4, FloorBanOutcome::Done, String::new()),
        ]
    );
}

#[tokio::test]
async fn an_idempotent_replay_does_nothing_the_second_time() {
    let w = standard(true, true).await;
    let call = || ban_call("community_hard_floor", Some("evt1:hard"));
    run(&w.caps, call()).await.unwrap();
    run(&w.caps, call()).await.unwrap();
    assert_eq!(
        recorded(&w.exec).len(),
        1,
        "one platform ban for two identical calls"
    );
    assert_eq!(
        w.store.records().len(),
        1,
        "the replay is not re-audited as a new action"
    );
    assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn the_per_community_rate_limit_blocks_the_ban() {
    let w = standard(true, true).await;
    *w.store.rate_limited.lock().unwrap() = true;
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "target_unavailable"
    );
    assert!(recorded(&w.exec).is_empty());
}

#[tokio::test]
async fn malformed_floor_arguments_are_invalid_args_and_never_act() {
    let w = standard(true, true).await;
    // No idempotency key.
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", None))
            .await
            .unwrap_err(),
        "invalid_args"
    );
    // Extra argument (e.g. a guest trying to smuggle a target or duration).
    let mut call = ban_call("community_hard_floor", Some("k1"));
    call.args["args"]["duration"] = serde_json::json!(60);
    assert_eq!(run(&w.caps, call).await.unwrap_err(), "invalid_args");
    // Non-UUID user.
    let mut call = ban_call("community_hard_floor", Some("k1"));
    call.args["args"]["user"] = serde_json::json!("1234");
    assert_eq!(run(&w.caps, call).await.unwrap_err(), "invalid_args");
    assert!(recorded(&w.exec).is_empty());
    assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 0);
}

#[test]
fn the_applied_fact_and_the_actor_are_never_inherited_across_a_hop() {
    let base =
        InvocationIdentity::new("discord", Some("1".into()), vec![]).with_system_originated(true);
    base.mark_reputation_applied();
    assert!(base.reputation_applied());
    // Core -> core hop (the floor chain's own shape) and a hop through a third party: both fresh.
    for (caller, target) in [
        (LOGIC, REP),
        (REP, DISCORD),
        ("waddles.acme.app.relay", REP),
    ] {
        assert!(
            !base
                .for_tool_call(Some("k".into()), caller, target)
                .reputation_applied(),
            "{caller} -> {target}"
        );
    }
    // A fresh identity (no apply) is not applied.
    assert!(!InvocationIdentity::new("discord", None, vec![]).reputation_applied());
}

#[tokio::test]
async fn the_nested_target_hop_cannot_reclaim_the_automated_actor() {
    // The fake executor makes the DISCORD hop issue its own floor-ban tool.invoke. That hop's
    // capability set has app_id = DISCORD and a fresh identity, so it is refused: the actor is
    // not inherited across the hop.
    let w = standard(true, true).await;
    let nested = ban_call("community_hard_floor", Some("nested")).args;
    let exec = fake_executor(Behavior::NestedCallThenReply(nested)).await;
    let mut caps = w.caps;
    if let Some(t) = caps.tool.as_mut() {
        t.connections = Arc::clone(&exec.registry);
    }
    run(&caps, ban_call("community_hard_floor", Some("evt1:hard")))
        .await
        .unwrap();
    // The outer ban ran once; the nested claim from the target hop never reached the floor gate
    // (no second slot was begun) and issued no second platform ban.
    assert_eq!(recorded(&exec).len(), 1);
    assert_eq!(w.store.begin_calls.load(Ordering::SeqCst), 1);
    assert_eq!(w.store.records().len(), 1);
}

#[test]
fn only_a_floor_ban_on_the_matching_core_service_is_a_claim() {
    let req = |target: &str, tool: &str, reason: &str| ToolRequest {
        target: target.to_string(),
        tool: tool.to_string(),
        args: serde_json::json!({"user": USER, "reason": reason}),
        idempotency_key: Some("k".to_string()),
    };
    assert_eq!(
        detect_claim(&req(DISCORD, BAN, "community_hard_floor")),
        Some(("discord".to_string(), FloorScope::CommunityHard))
    );
    assert_eq!(
        detect_claim(&req(
            "waddles.core.service.twitch",
            "twitch.moderation.ban",
            "tenant_floor"
        )),
        Some(("twitch".to_string(), FloorScope::Tenant))
    );
    for bad in [
        req(DISCORD, BAN, "manual"),
        req(DISCORD, "discord.moderation.kick", "tenant_floor"),
        req("waddles.core.service.twitch", BAN, "tenant_floor"),
        req(DISCORD, "moderation.ban", "tenant_floor"),
        req("waddles.acme.service.discord", BAN, "tenant_floor"),
    ] {
        assert_eq!(detect_claim(&bad), None, "{bad:?}");
    }
}

#[test]
fn summary_is_ok_only_when_everything_is_banned_and_names_counts_only() {
    use FloorResult::*;
    let ok = summarise(FloorScope::Tenant, &[Done, Done]).unwrap();
    assert_eq!(ok["ok"]["banned"], 2);
    let partial = summarise(FloorScope::Tenant, &[Done, Failed("not_granted".into())]).unwrap_err();
    assert_eq!(partial.code(), "target_unavailable");
    assert!(!partial.message().contains(USER));
    assert_eq!(
        summarise(FloorScope::Tenant, &[Done, RateLimited])
            .unwrap_err()
            .code(),
        "target_unavailable"
    );
    assert_eq!(
        summarise(FloorScope::CommunityHard, &[Refused])
            .unwrap_err()
            .code(),
        "forbidden"
    );
}

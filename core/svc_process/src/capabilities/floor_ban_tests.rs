//! Automated hard-ban gate tests: the REAL `handle_tool` dispatch and the real planner/gate,
//! against a stateful fake reputation store (the enqueue outbox: idempotency, quota, refusal).
//! The gate never invokes the platform bundle (delivery is the action stage's outbox dispatch,
//! tested in `svc_action`); the store's own SQL is proven against a real Postgres in
//! `alembic/tests/test_0102_reputation_floor_ban.py`.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU32, Ordering};

use bundle_host_reputation::{
    BoxFuture, FloorEnqueue, FloorScope, IdempotencyKey, ReputationCaps, ReputationError,
    ReputationScope, ReputationStore, ScoreEntry,
};
use uuid::Uuid;

use super::*;
use crate::capabilities::floor_ban::{detect_claim, floor_ban_key, summarise, FloorResult};
use crate::capabilities::ReputationWiring;
use crate::identity::InvocationIdentity;
use crate::license::test_support::FixedGate;
use bundle_tool_call::ToolRequest;

const LOGIC: &str = "waddles.core.app.reputation-accrual";
const REP: &str = "waddles.core.service.reputation";
const DISCORD: &str = "waddles.core.service.discord";
const BAN: &str = "discord.moderation.ban";
const USER: &str = "3f2b8c1e-9d4a-4c55-8a10-0a1b2c3d4e5f";
const OTHER_USER: &str = "9a8b7c6d-1111-4222-8333-444455556666";
const EVENT: &str = "evt-floor-1";
const LOGIC_VERSION: i64 = 11;
const REP_VERSION: i64 = 22;
const DISCORD_VERSION: i64 = 33;

fn unsupported() -> ReputationError {
    ReputationError::Backend("unused in this test".to_string())
}

/// One recorded enqueue: `(origin, target, user, scope, key, platform)`.
type Enqueued = (i32, i32, Uuid, FloorScope, String, String);

/// Stateful fake of the floor-ban half of the store: the enqueue outbox.
struct FloorStore {
    /// `Some(app)` answers the lookup, `None` makes it fail.
    active: Mutex<Option<Option<String>>>,
    communities: Vec<i32>,
    /// Communities whose enqueue answers `Refused` (SQL re-verification said not breached).
    not_breached: Mutex<HashSet<i32>>,
    rate_limited: Mutex<bool>,
    /// When set, the enqueue answers `UserCapped` (the user already has bans in the window).
    user_capped: Mutex<bool>,
    /// Slots already enqueued: `(target, key)` -> row id.
    known: Mutex<HashMap<(i32, String), i64>>,
    /// Every enqueue: `(origin, target, user, scope, key, platform)`.
    enqueues: Mutex<Vec<Enqueued>>,
    enqueue_calls: AtomicU32,
}

impl FloorStore {
    fn new(active: &str, communities: &[i32]) -> Arc<Self> {
        Arc::new(Self {
            active: Mutex::new(Some(Some(active.to_string()))),
            communities: communities.to_vec(),
            not_breached: Mutex::default(),
            rate_limited: Mutex::new(false),
            user_capped: Mutex::new(false),
            known: Mutex::default(),
            enqueues: Mutex::default(),
            enqueue_calls: AtomicU32::new(0),
        })
    }
    fn enqueues(&self) -> Vec<Enqueued> {
        let mut r = self.enqueues.lock().unwrap().clone();
        r.sort_by_key(|e| e.1);
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
    fn floor_ban_enqueue<'a>(
        &'a self,
        scope: &'a ReputationScope,
        target: i32,
        user: Uuid,
        floor: FloorScope,
        key: &'a str,
        platform: &'a str,
    ) -> BoxFuture<'a, Result<FloorEnqueue, ReputationError>> {
        Box::pin(async move {
            self.enqueue_calls.fetch_add(1, Ordering::SeqCst);
            if let Some(id) = self.known.lock().unwrap().get(&(target, key.to_string())) {
                return Ok(FloorEnqueue::AlreadyKnown(*id));
            }
            if self.not_breached.lock().unwrap().contains(&target) {
                return Ok(FloorEnqueue::Refused(0));
            }
            if *self.rate_limited.lock().unwrap() {
                return Ok(FloorEnqueue::RateLimited(0));
            }
            if *self.user_capped.lock().unwrap() {
                return Ok(FloorEnqueue::UserCapped);
            }
            let id = i64::from(target) * 100;
            self.known
                .lock()
                .unwrap()
                .insert((target, key.to_string()), id);
            self.enqueues.lock().unwrap().push((
                scope.community_id,
                target,
                user,
                floor,
                key.to_string(),
                platform.to_string(),
            ));
            Ok(FloorEnqueue::Queued(id))
        })
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
    let identity =
        InvocationIdentity::new("discord", Some("12345".to_string()), vec![]).with_event_id(EVENT);
    if applied {
        identity.mark_reputation_applied(Uuid::parse_str(USER).unwrap(), 3);
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
async fn community_hard_floor_ban_is_enqueued_for_the_action_stage_and_never_invokes_the_target() {
    let w = standard(true, true).await;
    let out = run(&w.caps, ban_call("community_hard_floor", Some("guest-key")))
        .await
        .unwrap();
    assert_eq!(out["ok"]["queued"], 1);
    // H1: delivery is the OUTBOX, not a cross-stage tool call: no executor invoke at all.
    assert!(
        recorded(&w.exec).is_empty(),
        "the process stage must never invoke the platform bundle"
    );
    let user = Uuid::parse_str(USER).unwrap();
    assert_eq!(
        w.store.enqueues(),
        vec![(
            3,
            3,
            user,
            FloorScope::CommunityHard,
            floor_ban_key(EVENT, user, FloorScope::CommunityHard),
            "discord".to_string()
        )]
    );
}

#[tokio::test]
async fn the_stored_key_is_host_derived_never_the_guest_chosen_one() {
    // M3: two calls with DIFFERENT guest keys in the same invocation (same event id) collapse to
    // one slot, and the stored key never contains the guest's value.
    let w = standard(true, true).await;
    run(
        &w.caps,
        ban_call("community_hard_floor", Some("guest-key-A")),
    )
    .await
    .unwrap();
    run(
        &w.caps,
        ban_call("community_hard_floor", Some("guest-key-B")),
    )
    .await
    .unwrap();
    let enq = w.store.enqueues();
    assert_eq!(enq.len(), 1, "one slot per (event, user, scope)");
    assert!(!enq[0].4.contains("guest"), "{}", enq[0].4);
    assert!(enq[0].4.starts_with("fb:"));
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn a_floor_ban_without_a_host_event_id_or_a_retry_token_is_refused() {
    let w = standard(true, true).await;
    // No retry token at all: a write tool requires one.
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", None))
            .await
            .unwrap_err(),
        "invalid_args"
    );
    // An invocation with no host-derived event id cannot derive a key: fail closed.
    let mut caps = w.caps;
    let identity = InvocationIdentity::new("discord", Some("12345".to_string()), vec![]);
    identity.mark_reputation_applied(Uuid::parse_str(USER).unwrap(), 3);
    caps.invocation_identity = Some(Arc::new(identity));
    assert_eq!(
        run(&caps, ban_call("community_hard_floor", Some("k")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
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
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
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
        assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
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
        assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
    }
}

#[tokio::test]
async fn human_origin_without_moderator_scope_and_no_prior_apply_is_forbidden() {
    // The same eligible chain, but NO reputation apply succeeded in this invocation: the
    // automated path is not taken and the human (no `community:moderate`) is refused.
    let w = standard(false, true).await;
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(recorded(&w.exec).is_empty());
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn h2_an_apply_of_another_user_or_in_another_community_does_not_unlock_the_ban() {
    let applied_user = Uuid::parse_str(OTHER_USER).unwrap();
    let target_user = Uuid::parse_str(USER).unwrap();
    // The applied user is someone else: banning USER is not unlocked.
    let mut w = standard(false, true).await;
    let identity =
        InvocationIdentity::new("discord", Some("12345".to_string()), vec![]).with_event_id(EVENT);
    identity.mark_reputation_applied(applied_user, 3);
    w.caps.invocation_identity = Some(Arc::new(identity));
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert_eq!(
        run(&w.caps, ban_call("tenant_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden",
        "the tenant scope is bound to the same user"
    );
    // The right user, but applied in a DIFFERENT community: not unlocked either.
    let identity =
        InvocationIdentity::new("discord", Some("12345".to_string()), vec![]).with_event_id(EVENT);
    identity.mark_reputation_applied(target_user, 4);
    w.caps.invocation_identity = Some(Arc::new(identity));
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
    // The right user in the right community: unlocked.
    let identity =
        InvocationIdentity::new("discord", Some("12345".to_string()), vec![]).with_event_id(EVENT);
    identity.mark_reputation_applied(target_user, 3);
    w.caps.invocation_identity = Some(Arc::new(identity));
    assert!(run(&w.caps, ban_call("community_hard_floor", Some("k1")))
        .await
        .is_ok());
}

#[tokio::test]
async fn a_non_floor_reason_never_gets_the_automated_path() {
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
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn floor_not_breached_is_refused_by_the_sql_reverification_at_enqueue() {
    // M1: the breach is re-verified inside the enqueue transaction; the host trusts only that.
    let w = standard(true, true).await;
    w.store.not_breached.lock().unwrap().insert(3);
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("k1")))
            .await
            .unwrap_err(),
        "forbidden"
    );
    assert!(w.store.enqueues().is_empty(), "no row is queued");
    assert!(recorded(&w.exec).is_empty());
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
        assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
    }
}

#[tokio::test]
async fn a_platform_the_action_stage_cannot_deliver_is_refused_loudly_not_queued_to_die() {
    let w = standard(true, true).await;
    let mut call = ban_call("community_hard_floor", Some("k1"));
    call.args["target"] = serde_json::json!("waddles.core.service.twitch");
    call.args["tool"] = serde_json::json!("twitch.moderation.ban");
    // Ineligible-by-shape falls to the ordinary path (no grant for twitch): refused either way,
    // and nothing is ever enqueued.
    assert!(run(&w.caps, call).await.is_err());
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn tenant_fan_out_enqueues_every_authorised_community_and_reports_a_partial_failure() {
    // Community 4 has no ban grant (the per-community authorization refuses it); 3, 5, 6 queue.
    let w = world(REP, vec![LOGIC, REP], &[3, 4, 5, 6], &[3, 5, 6], true, true).await;
    let err = run(&w.caps, ban_call("tenant_floor", Some("evt9:tenant")))
        .await
        .unwrap_err();
    assert_eq!(
        err, "target_unavailable",
        "a partial fan-out is never reported as success"
    );
    assert!(recorded(&w.exec).is_empty());
    let enq = w.store.enqueues();
    assert_eq!(
        enq.iter().map(|e| e.1).collect::<Vec<_>>(),
        vec![3, 5, 6],
        "only the granted communities were queued"
    );
    // Every slot names the ORIGIN community (3), the tenant scope and the claim's platform.
    assert!(enq
        .iter()
        .all(|e| e.0 == 3 && e.3 == FloorScope::Tenant && e.5 == "discord"));
}

#[tokio::test]
async fn an_idempotent_replay_queues_nothing_the_second_time() {
    let w = standard(true, true).await;
    let call = || ban_call("community_hard_floor", Some("evt1:hard"));
    run(&w.caps, call()).await.unwrap();
    run(&w.caps, call()).await.unwrap();
    assert_eq!(w.store.enqueues().len(), 1, "one outbox row for two calls");
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn m2_the_rate_limit_is_recorded_and_the_caller_message_says_it_is_retried_later() {
    let w = standard(true, true).await;
    *w.store.rate_limited.lock().unwrap() = true;
    let err = w
        .caps
        .handle(ban_call("community_hard_floor", Some("k1")))
        .await
        .unwrap_err();
    assert_eq!(err.code, "target_unavailable");
    assert!(
        err.message.contains("retried after the quota window"),
        "{}",
        err.message
    );
    assert!(recorded(&w.exec).is_empty());
}

#[tokio::test]
async fn every_enqueue_result_is_counted_with_pii_free_labels() {
    let harness = crate::stage_metrics::test_support::Harness::new();
    let metrics = Arc::new(crate::stage_metrics::StageMetrics::new(&harness.meter()));
    let mut w = standard(true, true).await;
    w.caps = w.caps.with_stage_metrics(Arc::clone(&metrics));
    run(&w.caps, ban_call("community_hard_floor", Some("k1")))
        .await
        .unwrap();
    *w.store.rate_limited.lock().unwrap() = true;
    w.store.known.lock().unwrap().clear();
    let _ = run(&w.caps, ban_call("community_hard_floor", Some("k2"))).await;
    let queued = [("scope", "community_hard_floor"), ("result", "queued")];
    let limited = [
        ("scope", "community_hard_floor"),
        ("result", "rate_limited"),
    ];
    assert_eq!(
        harness.count("waddles_floor_ban_enqueue_total", &queued),
        1.0
    );
    assert_eq!(
        harness.count("waddles_floor_ban_enqueue_total", &limited),
        1.0
    );
    for p in harness.points() {
        assert!(
            !p.attrs.values().any(|v| v.contains(USER)),
            "a user id leaked into a label: {p:?}"
        );
    }
}

#[tokio::test]
async fn malformed_floor_arguments_are_invalid_args_and_never_act() {
    let w = standard(true, true).await;
    // Extra argument (e.g. a guest trying to smuggle a target or duration).
    let mut call = ban_call("community_hard_floor", Some("k1"));
    call.args["args"]["duration"] = serde_json::json!(60);
    assert_eq!(run(&w.caps, call).await.unwrap_err(), "invalid_args");
    // Non-UUID user.
    let mut call = ban_call("community_hard_floor", Some("k1"));
    call.args["args"]["user"] = serde_json::json!("1234");
    assert_eq!(run(&w.caps, call).await.unwrap_err(), "invalid_args");
    assert!(recorded(&w.exec).is_empty());
    assert_eq!(w.store.enqueue_calls.load(Ordering::SeqCst), 0);
}

#[test]
fn the_applied_fact_is_per_user_per_community_and_never_inherited_across_a_hop() {
    let user = Uuid::parse_str(USER).unwrap();
    let other = Uuid::parse_str(OTHER_USER).unwrap();
    let base =
        InvocationIdentity::new("discord", Some("1".into()), vec![]).with_system_originated(true);
    base.mark_reputation_applied(user, 3);
    assert!(base.reputation_applied_for(user, 3));
    assert!(!base.reputation_applied_for(other, 3), "another user");
    assert!(!base.reputation_applied_for(user, 4), "another community");
    // Core -> core hop (the floor chain's own shape) and a hop through a third party: both fresh.
    for (caller, target) in [
        (LOGIC, REP),
        (REP, DISCORD),
        ("waddles.acme.app.relay", REP),
    ] {
        assert!(
            !base
                .for_tool_call(Some("k".into()), caller, target)
                .reputation_applied_for(user, 3),
            "{caller} -> {target}"
        );
    }
    // A fresh identity (no apply) is not applied.
    assert!(!InvocationIdentity::new("discord", None, vec![]).reputation_applied_for(user, 3));
}

#[test]
fn the_applied_set_is_bounded_and_fails_closed_past_the_bound() {
    let base = InvocationIdentity::new("discord", None, vec![]);
    for i in 0..2000u128 {
        base.mark_reputation_applied(Uuid::from_u128(i), 3);
    }
    assert!(base.reputation_applied_for(Uuid::from_u128(0), 3));
    assert!(!base.reputation_applied_for(Uuid::from_u128(1999), 3));
}

#[test]
fn the_host_key_is_sql_safe_stable_and_separates_event_user_and_scope() {
    let u = Uuid::from_u128(7);
    let k = floor_ban_key("evt-1", u, FloorScope::CommunityHard);
    assert!(k.starts_with("fb:") && k.len() == 43, "{k}");
    assert!(k
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b':'));
    assert_eq!(k, floor_ban_key("evt-1", u, FloorScope::CommunityHard));
    assert_ne!(k, floor_ban_key("evt-2", u, FloorScope::CommunityHard));
    assert_ne!(k, floor_ban_key("evt-1", u, FloorScope::Tenant));
    assert_ne!(
        k,
        floor_ban_key("evt-1", Uuid::from_u128(8), FloorScope::CommunityHard)
    );
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
fn summary_is_ok_only_when_everything_is_queued_and_names_counts_only() {
    use FloorResult::*;
    let ok = summarise(FloorScope::Tenant, &[Queued, Queued]).unwrap();
    assert_eq!(ok["ok"]["queued"], 2);
    let partial =
        summarise(FloorScope::Tenant, &[Queued, Failed("not_granted".into())]).unwrap_err();
    assert_eq!(partial.code(), "target_unavailable");
    assert!(!partial.message().contains(USER));
    let limited = summarise(FloorScope::Tenant, &[Queued, RateLimited]).unwrap_err();
    assert_eq!(limited.code(), "target_unavailable");
    assert!(limited.message().contains("retried after the quota window"));
    assert_eq!(
        summarise(FloorScope::CommunityHard, &[Refused])
            .unwrap_err()
            .code(),
        "forbidden"
    );
}

#[tokio::test]
async fn the_key_derives_from_the_root_event_id_not_the_per_hop_guest_namespace() {
    // Two tool hops of the SAME root event whose per-hop mutation namespaces differ (each derives
    // from a different guest retry token) must land in the SAME slot: the key is bound to the
    // hop-verified root event id, which `for_tool_call` preserves unchanged.
    let user = Uuid::parse_str(USER).unwrap();
    let root =
        InvocationIdentity::new("discord", Some("12345".to_string()), vec![]).with_event_id(EVENT);
    assert_eq!(root.root_event_id(), Some(EVENT));
    let mut w = standard(false, true).await;
    for guest in ["tool:guest-A", "tool:guest-B"] {
        let hop = root.for_tool_call(Some(guest.to_string()), LOGIC, REP);
        assert_eq!(
            hop.event_id(),
            Some(guest),
            "the per-hop namespace is guest-derived"
        );
        assert_eq!(
            hop.root_event_id(),
            Some(EVENT),
            "the root id survives the hop"
        );
        hop.mark_reputation_applied(user, 3);
        w.caps.invocation_identity = Some(Arc::new(hop));
        run(&w.caps, ban_call("community_hard_floor", Some("any")))
            .await
            .unwrap();
    }
    let enq = w.store.enqueues();
    assert_eq!(enq.len(), 1, "one slot per (root event, user, scope)");
    assert_eq!(
        enq[0].4,
        floor_ban_key(EVENT, user, FloorScope::CommunityHard)
    );
    // An invocation with no root event id (e.g. a bundle-admin call) fails closed.
    let orphan = InvocationIdentity::new("discord", None, vec![]);
    orphan.mark_reputation_applied(user, 3);
    let orphan = orphan.for_tool_call(Some("tool:x".into()), LOGIC, REP);
    orphan.mark_reputation_applied(user, 3);
    w.caps.invocation_identity = Some(Arc::new(orphan));
    assert_eq!(
        run(&w.caps, ban_call("community_hard_floor", Some("any")))
            .await
            .unwrap_err(),
        "forbidden"
    );
}

#[tokio::test]
async fn a_user_capped_enqueue_is_success_but_counted_separately() {
    let harness = crate::stage_metrics::test_support::Harness::new();
    let metrics = Arc::new(crate::stage_metrics::StageMetrics::new(&harness.meter()));
    let mut w = standard(true, true).await;
    w.caps = w.caps.with_stage_metrics(Arc::clone(&metrics));
    *w.store.user_capped.lock().unwrap() = true;
    let out = run(&w.caps, ban_call("community_hard_floor", Some("k1")))
        .await
        .unwrap();
    assert_eq!(out["ok"]["queued"], 1);
    assert!(w.store.enqueues().is_empty(), "nothing more is recorded");
    let capped = [("scope", "community_hard_floor"), ("result", "user_capped")];
    assert_eq!(
        harness.count("waddles_floor_ban_enqueue_total", &capped),
        1.0
    );
}

//! The `identity` capability's real path joined end to end on the stage side --
//! `tokenize_event_with_mentions` (the real inbound PII pass) -> the per-invoke
//! `InvocationIdentity` -> `CapabilityHandler::handle` (host-call decode) -> the
//! REAL `CapabilityGate` -> the REAL `PgMemberDirectory` reading the REAL
//! `community_member_identities` view (the exact shipped DDL,
//! `scripts/db/bundle_identity_resolve.sql`) as the least-privilege
//! `waddles_bundle_reader` role -> and then the resolved UUIDs fed straight
//! into the REAL `economy` capability + store, proving they are exactly the
//! values the economy accepts. Only hub-api's `ResolveHandle` (a network call
//! into the PII boundary) and the pseudonym minter are doubles; both have
//! their own hub-api-side integration tests. (The executor half -- real wasm
//! calling the import and emitting this exact wire shape -- is
//! `core/bundle_executor/tests/stage_next_identity.rs`.)
//!
//! Requires Docker via `testcontainers`.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::sync::Arc;

use bundle_capability_gate::{
    CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryQuotaLedger, SnapshotMembership,
};
use bundle_host_economy::{
    connect, load_membership, ConnectConfig, EconomyStore, PostgresEconomyStore,
};
use bundle_host_http::egress::{boxed, EgressGuard, EgressLimits, ReqwestTransport, StaticFlag};
use penguin_bundle_host::wire::{CapabilityKind, HostCallBody, HostResultError};
use penguin_spine::PlatformEvent;
use sea_orm::{ConnectionTrait, Database, DatabaseConnection, Statement};
use svc_process::capabilities::{
    CapabilityHandler, EconomyWiring, HttpEgressCatalog, StageCapabilities,
};
use svc_process::identity::{
    BoxFuture, HandleResolver, IdentityError, IdentityWiring, InvocationIdentity, PgMemberDirectory,
};
use svc_process::license::StaticGate;
use svc_process::pii_tokenize::{
    tokenize_event_with_mentions, IdentityMinter, MintItem, MintResult,
};
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};
use uuid::Uuid;

const SU_PW: &str = "postgres_test_superuser_pw";
const ECO_PW: &str = "waddles_economy_runtime_test_pw";
const READER_PW: &str = "waddles_bundle_reader_test_pw";
const APP_ID: &str = "waddles.core.test-identity";
const IDENTITY_DDL: &str = include_str!("../../../scripts/db/bundle_identity_resolve.sql");
const ECONOMY_DDL: &str = include_str!("../../../scripts/db/bundle_economy_store.sql");
const ECONOMY_IDEMPOTENCY_DDL: &str =
    include_str!("../../../scripts/db/bundle_economy_idempotency.sql");

/// Tenant 1 has communities 10 (the one under test) and 11 (a sibling);
/// tenant 2 has community 20 -- and reuses the SAME platform account id as
/// alice, to prove cross-tenant isolation.
const TENANT: i32 = 1;
const COMMUNITY: i32 = 10;
const SIBLING_COMMUNITY: i32 = 11;
const OTHER_TENANT: i32 = 2;
const OTHER_TENANT_COMMUNITY: i32 = 20;

struct World {
    _container: ContainerAsync<GenericImage>,
    su: DatabaseConnection,
    reader: DatabaseConnection,
    eco_conn: DatabaseConnection,
    alice: Uuid,
    bob: Uuid,
    cross_tenant_alice: Uuid,
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql)
        .await
        .unwrap_or_else(|e| panic!("sql failed: {e}\n{sql}"));
}

/// One `community_members` fixture row (SQL fragments for the nullable columns).
struct Member {
    community: i32,
    platform_user_id: &'static str,
    uuid: Option<Uuid>,
    is_active: &'static str,
    left_at: &'static str,
    removed_at: &'static str,
}

impl Member {
    /// An active, never-left, never-removed member.
    fn new(community: i32, platform_user_id: &'static str, uuid: Option<Uuid>) -> Self {
        Self {
            community,
            platform_user_id,
            uuid,
            is_active: "true",
            left_at: "NULL",
            removed_at: "NULL",
        }
    }
    fn left(mut self) -> Self {
        self.left_at = "now()";
        self
    }
    fn removed(mut self) -> Self {
        self.removed_at = "now()";
        self
    }
    fn with_is_active(mut self, sql: &'static str) -> Self {
        self.is_active = sql;
        self
    }
}

async fn world() -> World {
    let container = GenericImage::new("postgres", "17.6-bookworm")
        .with_exposed_port(ContainerPort::Tcp(5432))
        .with_wait_for(WaitFor::log(
            LogWaitStrategy::new(
                LogSource::BothStd,
                "database system is ready to accept connections",
            )
            .with_times(2),
        ))
        .with_env_var("POSTGRES_PASSWORD", SU_PW)
        .with_env_var("POSTGRES_DB", "waddles_test")
        .start()
        .await
        .expect("postgres container starts");
    let host = container.get_host().await.unwrap().to_string();
    let port = container.get_host_port_ipv4(5432).await.unwrap();
    let su = Database::connect(format!(
        "postgres://postgres:{SU_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();

    // The legacy tables the shipped DDL reads, with only the columns it (and the
    // migration chain's bootstrap in alembic/tests/pg_docker.py) touch.
    exec(
        &su,
        "CREATE TABLE tenants (id SERIAL PRIMARY KEY, slug TEXT);
         CREATE TABLE communities (id SERIAL PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id));
         CREATE TABLE hub_users (id SERIAL PRIMARY KEY, uuid UUID NOT NULL DEFAULT gen_random_uuid(),
                                 username TEXT);
         CREATE TABLE community_members (
             id SERIAL PRIMARY KEY,
             community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,
             user_id VARCHAR(255),
             platform VARCHAR(50),
             platform_user_id VARCHAR(255),
             display_name VARCHAR(255),
             is_active BOOLEAN DEFAULT true,
             left_at TIMESTAMP,
             removed_at TIMESTAMP,
             UNIQUE (community_id, platform, platform_user_id));
         INSERT INTO tenants (id, slug) VALUES (1, 't1'), (2, 't2');
         INSERT INTO communities (id, tenant_id) VALUES (10, 1), (11, 1), (20, 2);",
    )
    .await;
    exec(
        &su,
        &format!(
            "CREATE ROLE waddles_economy_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{ECO_PW}';
             CREATE ROLE waddles_bundle_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE \
             NOREPLICATION PASSWORD '{READER_PW}'"
        ),
    )
    .await;
    exec(&su, ECONOMY_DDL).await;
    exec(&su, ECONOMY_IDEMPOTENCY_DDL).await;
    // The shipped identity DDL (also grants the view to waddles_bundle_reader).
    exec(&su, IDENTITY_DDL).await;

    let (alice, bob, cross_tenant_alice) = (Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
    let members = [
        // alice / bob: active members with a resolved identity
        Member::new(COMMUNITY, "1001", Some(alice)),
        Member::new(COMMUNITY, "2002", Some(bob)),
        // carol: an active member whose identity is unresolved (user_uuid NULL)
        Member::new(COMMUNITY, "3003", None),
        // dave left; erin was removed; 6006 has is_active IS NULL; 7007 is inactive
        Member::new(COMMUNITY, "4004", Some(Uuid::new_v4())).left(),
        Member::new(COMMUNITY, "5005", Some(Uuid::new_v4())).removed(),
        Member::new(COMMUNITY, "6006", Some(Uuid::new_v4())).with_is_active("NULL"),
        Member::new(COMMUNITY, "7007", Some(Uuid::new_v4())).with_is_active("false"),
        // an active member of a SIBLING community of the same tenant
        Member::new(SIBLING_COMMUNITY, "8008", Some(Uuid::new_v4())),
        // the SAME platform id as alice, in another tenant's community
        Member::new(OTHER_TENANT_COMMUNITY, "1001", Some(cross_tenant_alice)),
    ];
    for m in members {
        let uuid_sql = m.uuid.map_or("NULL".to_string(), |u| format!("'{u}'"));
        exec(
            &su,
            &format!(
                "INSERT INTO community_members (community_id, platform, platform_user_id, \
                 display_name, is_active, left_at, removed_at, user_uuid) \
                 VALUES ({}, 'twitch', '{}', 'Raw Display Name', {}, {}, {}, {uuid_sql})",
                m.community, m.platform_user_id, m.is_active, m.left_at, m.removed_at,
            ),
        )
        .await;
    }
    // Privileged hub-side funding (the runtime role cannot mint).
    exec(
        &su,
        &format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (1, 10, '{alice}', 500)"
        ),
    )
    .await;

    // Production path: the directory connects as the least-privilege reader.
    let reader = Database::connect(format!(
        "postgres://waddles_bundle_reader:{READER_PW}@{host}:{port}/waddles_test"
    ))
    .await
    .unwrap();
    let eco_conn = connect(
        &ConnectConfig {
            host: host.clone(),
            port,
            name: "waddles_test".to_string(),
            user: "waddles_economy_runtime".to_string(),
        },
        ECO_PW,
    )
    .await
    .unwrap();

    World {
        _container: container,
        su,
        reader,
        eco_conn,
        alice,
        bob,
        cross_tenant_alice,
    }
}

fn grant(id: &str, params: serde_json::Value) -> (String, GrantedPermission) {
    (
        id.to_string(),
        GrantedPermission {
            permission_id: id.to_string(),
            params,
        },
    )
}

/// A scripted hub-api `ResolveHandle` (the only network dependency left).
struct ScriptedHandles(Result<Uuid, IdentityError>);

impl HandleResolver for ScriptedHandles {
    fn resolve_handle<'a>(
        &'a self,
        _tenant_id: i32,
        _platform: &'a str,
        _reference: &'a str,
    ) -> BoxFuture<'a, Result<Uuid, IdentityError>> {
        Box::pin(async move { self.0.clone() })
    }
}

/// Mints a RANDOM pseudonym per platform id -- deliberately NOT the member's
/// `community_members.user_uuid` -- so a test proves the capability does not
/// depend on the bundle-visible token being the community UUID.
struct RandomPseudonymMinter;

impl IdentityMinter for RandomPseudonymMinter {
    fn mint_many<'a>(&'a self, _tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
        Box::pin(async move {
            Ok(items
                .into_iter()
                .map(|i| (i.platform_user_id, Uuid::new_v4().to_string()))
                .collect::<HashMap<_, _>>())
        })
    }
}

/// Builds the stage's per-invoke capability set for one `(tenant, community)`
/// with the real gate, the real directory (as the reader role), the real
/// economy store, and the given invocation facts.
async fn stage(
    w: &World,
    tenant: i32,
    community: i32,
    invocation: InvocationIdentity,
    handles: Option<Result<Uuid, IdentityError>>,
) -> StageCapabilities {
    // Production membership path for the economy gate: snapshot from the DB.
    let membership = Arc::new(SnapshotMembership::new());
    assert!(membership.replace_all(load_membership(&w.eco_conn, None).await.unwrap()));

    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: tenant,
            community_id: community,
            app_id: APP_ID.to_string(),
            app_version: 1,
        },
        GrantSet {
            permission_snapshot_hash: "test".to_string(),
            grants: [
                grant("identity.resolve", serde_json::json!({})),
                grant("economy.read", serde_json::json!({})),
                grant("economy.wager", serde_json::json!({"max_bet": 50})),
                grant("economy.transfer", serde_json::json!({"max_amount": 200})),
            ]
            .into_iter()
            .collect(),
        },
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        membership,
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: std::time::Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
            allowed_ports: vec![443],
            proxy_url: None,
        },
        HttpEgressCatalog::new(),
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("e2e_identity_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        boxed(StaticFlag(true)),
    ));
    StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        APP_ID.to_string(),
        tenant,
        community,
        1,
        egress,
        gate,
    )
    .with_economy(EconomyWiring {
        store: Arc::new(PostgresEconomyStore::new(w.eco_conn.clone())) as Arc<dyn EconomyStore>,
        flag: Arc::new(StaticGate(true)),
    })
    .with_identity(
        IdentityWiring {
            directory: Arc::new(PgMemberDirectory::new(w.reader.clone())),
            handles: handles.map(|h| Arc::new(ScriptedHandles(h)) as Arc<dyn HandleResolver>),
            flag: Arc::new(StaticGate(true)),
        },
        Arc::new(invocation),
    )
}

fn call(op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: APP_ID.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

async fn user_of(caps: &StageCapabilities, op: &str, args: serde_json::Value) -> Uuid {
    let out = caps.handle(call(op, args)).await.unwrap_or_else(|e| {
        panic!("{op} must succeed, got {}: {}", e.code, e.message);
    });
    Uuid::parse_str(out["user"].as_str().expect("user string")).expect("a canonical uuid")
}

async fn refusal(caps: &StageCapabilities, op: &str, args: serde_json::Value) -> HostResultError {
    caps.handle(call(op, args))
        .await
        .expect_err("expected a refusal")
}

fn event(user_id: &str, text: &str) -> PlatformEvent {
    PlatformEvent {
        platform: "twitch".to_string(),
        event_type: "chat.message".to_string(),
        actor: Some("alice_display".to_string()),
        payload: serde_json::json!({
            "user_id": user_id,
            "author_id": user_id,
            "text": text,
        })
        .as_object()
        .cloned()
        .unwrap(),
        occurred_at: "2026-10-09T00:00:00.000Z".to_string(),
        source: None,
    }
}

/// The ids a bundle could see in a tokenized message: every `{user:<token>}`.
fn placeholder_tokens(text: &str) -> Vec<String> {
    text.split("{user:")
        .skip(1)
        .filter_map(|rest| rest.split('}').next().map(str::to_string))
        .collect()
}

/// Tokenizes `raw` exactly as `spine::handle_delivered` does and builds the
/// invocation's identity facts from the RAW event + the mention bindings.
async fn invocation_for(raw: &PlatformEvent) -> (InvocationIdentity, PlatformEvent) {
    invocation_for_event(raw, &Uuid::new_v4().to_string()).await
}

/// As [`invocation_for`] for the spine event `event_id`: building it twice with
/// the same id is a redelivery of that event.
async fn invocation_for_event(
    raw: &PlatformEvent,
    event_id: &str,
) -> (InvocationIdentity, PlatformEvent) {
    let tokenized = tokenize_event_with_mentions(raw, "acme", &RandomPseudonymMinter)
        .await
        .unwrap();
    (
        InvocationIdentity::from_event(raw, tokenized.mentions).with_event_id(event_id),
        tokenized.event,
    )
}

/// The whole points-game prerequisite: a `!steal <@mention>` message. The
/// bundle only sees opaque pseudonym tokens (NOT the community UUIDs); the
/// capability resolves both people to the UUIDs the economy requires, and the
/// economy then accepts them -- while it refuses the pseudonyms the bundle held.
#[tokio::test]
async fn steal_flow_actor_and_mention_resolve_to_the_uuids_the_economy_accepts() {
    let w = world().await;
    let raw = event("1001", "!steal <@2002> 120");
    let (invocation, visible) = invocation_for(&raw).await;
    let caps = stage(&w, TENANT, COMMUNITY, invocation, None).await;

    // What the bundle holds: pseudonym tokens, none of them a community UUID.
    let visible_text = visible.payload["text"].as_str().unwrap();
    let tokens = placeholder_tokens(visible_text);
    // One placeholder: the `<@2002>` mention (the scanner does not read the
    // digits inside it as a second `@handle` mention).
    assert_eq!(tokens.len(), 1, "{visible_text}");
    let bob_token = &tokens[0];
    let actor_token = visible.actor.clone().unwrap();
    for held in tokens
        .iter()
        .map(String::as_str)
        .chain([actor_token.as_str()])
    {
        assert!(
            !held.contains(&w.alice.to_string()) && !held.contains(&w.bob.to_string()),
            "the fixture's pseudonyms must differ from the community UUIDs ({held})"
        );
    }
    assert!(!visible_text.contains("2002"), "{visible_text}");

    // The actor and the mention resolve to the community UUIDs.
    let actor = user_of(&caps, "identity.resolve_actor", serde_json::json!({})).await;
    let target = user_of(
        &caps,
        "identity.resolve_mention",
        serde_json::json!({ "token": bob_token }),
    )
    .await;
    assert_eq!(actor, w.alice);
    assert_eq!(target, w.bob);

    // The pseudonym the bundle held is NOT accepted by the economy ...
    let err = refusal(
        &caps,
        "economy.balance",
        serde_json::json!({ "user": bob_token }),
    )
    .await;
    assert_eq!(err.code, "user_not_in_scope");

    // ... but the resolved UUIDs are: a real transfer through gate + store.
    let out = caps
        .handle(call(
            "economy.transfer",
            serde_json::json!({
                "from": actor.to_string(), "to": target.to_string(), "amount": 120
            }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({}));
    let out = caps
        .handle(call(
            "economy.balance",
            serde_json::json!({ "user": target.to_string() }),
        ))
        .await
        .unwrap();
    assert_eq!(out, serde_json::json!({ "balance": 120 }));
}

/// Every non-success state is an explicit, distinct refusal read from the real
/// database -- never a default/guessed UUID.
#[tokio::test]
async fn real_database_refusals_are_explicit_and_scoped() {
    let w = world().await;

    // An actor that is an active member but has NO resolved identity.
    let (inv, _) = invocation_for(&event("3003", "!gamble 5")).await;
    let caps = stage(&w, TENANT, COMMUNITY, inv, None).await;
    assert_eq!(
        refusal(&caps, "identity.resolve_actor", serde_json::json!({}))
            .await
            .code,
        "not_linked"
    );

    // Not an active member of THIS community, for every way of not being one.
    for (platform_user_id, why) in [
        ("4004", "left_at set"),
        ("5005", "removed_at set"),
        ("6006", "is_active IS NULL (fail-closed)"),
        ("7007", "is_active = false"),
        ("8008", "member of a SIBLING community of the same tenant"),
        ("9999", "no such account at all"),
    ] {
        let (inv, _) = invocation_for(&event(platform_user_id, "!gamble 5")).await;
        let caps = stage(&w, TENANT, COMMUNITY, inv, None).await;
        assert_eq!(
            refusal(&caps, "identity.resolve_actor", serde_json::json!({}))
                .await
                .code,
            "not_a_member",
            "{platform_user_id}: {why}"
        );
    }

    // Mentions fail the same way: unlinked / sibling-community targets.
    let (inv, visible) = invocation_for(&event("1001", "!steal <@3003> !steal <@8008>")).await;
    let tokens = placeholder_tokens(visible.payload["text"].as_str().unwrap());
    let caps = stage(&w, TENANT, COMMUNITY, inv, None).await;
    assert_eq!(
        tokens.len(),
        2,
        "tokens appear in text order: 3003 then 8008"
    );
    assert_eq!(
        refusal(
            &caps,
            "identity.resolve_mention",
            serde_json::json!({ "token": tokens[0] })
        )
        .await
        .code,
        "not_linked"
    );
    assert_eq!(
        refusal(
            &caps,
            "identity.resolve_mention",
            serde_json::json!({ "token": tokens[1] })
        )
        .await
        .code,
        "not_a_member"
    );
    // A token that was never in the message is not a lookup oracle.
    assert_eq!(
        refusal(
            &caps,
            "identity.resolve_mention",
            serde_json::json!({ "token": Uuid::new_v4().to_string() })
        )
        .await
        .code,
        "not_found"
    );
}

/// Tenant scoping is enforced by the query itself: the same platform account id
/// in another tenant resolves to THAT tenant's UUID, and a community paired
/// with the wrong tenant resolves to nothing.
#[tokio::test]
async fn resolution_is_tenant_and_community_scoped_by_the_query() {
    let w = world().await;

    // Same platform id "1001": tenant 1 -> alice, tenant 2 -> a different UUID.
    let (inv_a, _) = invocation_for(&event("1001", "hi")).await;
    let in_tenant_1 = stage(&w, TENANT, COMMUNITY, inv_a, None).await;
    let (inv_b, _) = invocation_for(&event("1001", "hi")).await;
    let in_tenant_2 = stage(&w, OTHER_TENANT, OTHER_TENANT_COMMUNITY, inv_b, None).await;
    let a = user_of(
        &in_tenant_1,
        "identity.resolve_actor",
        serde_json::json!({}),
    )
    .await;
    let x = user_of(
        &in_tenant_2,
        "identity.resolve_actor",
        serde_json::json!({}),
    )
    .await;
    assert_eq!(a, w.alice);
    assert_eq!(x, w.cross_tenant_alice);
    assert_ne!(a, x);

    // Community 10 under the WRONG tenant (2): the tenant predicate fails closed.
    let (inv_c, _) = invocation_for(&event("1001", "hi")).await;
    let mismatched = stage(&w, OTHER_TENANT, COMMUNITY, inv_c, None).await;
    assert_eq!(
        refusal(&mismatched, "identity.resolve_actor", serde_json::json!({}))
            .await
            .code,
        "not_a_member"
    );
}

/// A free-text `@handle` goes through hub-api's resolver, then is CONFIRMED an
/// active member of this community in the real database.
#[tokio::test]
async fn handle_mentions_resolve_through_hub_api_then_the_real_membership_check() {
    let w = world().await;
    let raw = event("1001", "!steal @SomeViewer");
    let (inv, visible) = invocation_for(&raw).await;
    let tokens = placeholder_tokens(visible.payload["text"].as_str().unwrap());
    assert_eq!(tokens.len(), 1);

    // hub-api says the handle is bob (a member): confirmed.
    let caps = stage(&w, TENANT, COMMUNITY, inv, Some(Ok(w.bob))).await;
    let got = user_of(
        &caps,
        "identity.resolve_mention",
        serde_json::json!({ "token": tokens[0] }),
    )
    .await;
    assert_eq!(got, w.bob);

    // hub-api resolves to an identity that exists in the tenant but is NOT a
    // member of this community: not_a_member (no cross-community oracle).
    let (inv, visible) = invocation_for(&raw).await;
    let tokens = placeholder_tokens(visible.payload["text"].as_str().unwrap());
    let caps = stage(&w, TENANT, COMMUNITY, inv, Some(Ok(Uuid::new_v4()))).await;
    assert_eq!(
        refusal(
            &caps,
            "identity.resolve_mention",
            serde_json::json!({ "token": tokens[0] })
        )
        .await
        .code,
        "not_a_member"
    );

    // Ambiguous / unknown handles are explicit errors; no resolver = unavailable.
    for (resolver, code) in [
        (Some(Err(IdentityError::Ambiguous)), "ambiguous"),
        (Some(Err(IdentityError::NotFound)), "not_found"),
        (None, "unavailable"),
    ] {
        let (inv, visible) = invocation_for(&raw).await;
        let tokens = placeholder_tokens(visible.payload["text"].as_str().unwrap());
        let caps = stage(&w, TENANT, COMMUNITY, inv, resolver).await;
        assert_eq!(
            refusal(
                &caps,
                "identity.resolve_mention",
                serde_json::json!({ "token": tokens[0] })
            )
            .await
            .code,
            code
        );
    }
}

/// The directory runs as `waddles_bundle_reader`: it can resolve through the
/// view but has no path to PII -- the base table's display names are off limits.
#[tokio::test]
async fn the_reader_role_resolves_through_the_view_but_cannot_read_pii() {
    let w = world().await;
    let (inv, _) = invocation_for(&event("1001", "hi")).await;
    let caps = stage(&w, TENANT, COMMUNITY, inv, None).await;
    assert_eq!(
        user_of(&caps, "identity.resolve_actor", serde_json::json!({})).await,
        w.alice
    );

    // The raw display name is in the table, and the reader cannot see it.
    for sql in [
        "SELECT display_name FROM community_members LIMIT 1",
        "SELECT * FROM community_members LIMIT 1",
    ] {
        let err = w
            .reader
            .query_one_raw(Statement::from_string(sea_orm::DbBackend::Postgres, sql))
            .await
            .expect_err("the reader must not be able to read community_members");
        assert!(
            err.to_string().contains("permission denied"),
            "{sql}: {err}"
        );
    }
    // And the view never projects it.
    let row =
        w.su.query_one_raw(Statement::from_string(
            sea_orm::DbBackend::Postgres,
            "SELECT COUNT(*)::BIGINT FROM information_schema.columns \
             WHERE table_name = 'community_member_identities' \
               AND column_name IN ('display_name', 'username', 'user_id')",
        ))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(row.try_get_by_index::<i64>(0).unwrap(), 0);
}

async fn fund(w: &World, user: Uuid, amount: i64) {
    exec(
        &w.su,
        &format!(
            "INSERT INTO economy_balances (tenant_id, community_id, user_uuid, balance) \
             VALUES (1, 10, '{user}', {amount}) \
             ON CONFLICT (tenant_id, community_id, user_uuid) DO UPDATE SET balance = {amount}"
        ),
    )
    .await;
}

async fn balance_of(w: &World, user: Uuid) -> i64 {
    w.su.query_one_raw(Statement::from_string(
        sea_orm::DbBackend::Postgres,
        format!("SELECT balance FROM economy_balances WHERE user_uuid = '{user}'"),
    ))
    .await
    .unwrap()
    .unwrap()
    .try_get_by_index::<i64>(0)
    .unwrap()
}

/// Regression (#751 review, theft) with the REAL directory: the actor is
/// derived from the event's platform account (alice, `1001`), so a `!steal
/// <@2002>` bundle that resolves bob and then names HIM as the payer is
/// refused -- the funds move only in the direction the actor chose.
#[tokio::test]
async fn a_bundle_that_resolves_the_mention_cannot_use_it_as_the_payer() {
    let w = world().await;
    fund(&w, w.bob, 300).await;
    let raw = event("1001", "!steal <@2002> 100");
    let (invocation, visible) = invocation_for(&raw).await;
    let bob_token = placeholder_tokens(visible.payload["text"].as_str().unwrap()).remove(0);
    let caps = stage(&w, TENANT, COMMUNITY, invocation, None).await;
    let actor = user_of(&caps, "identity.resolve_actor", serde_json::json!({})).await;
    let target = user_of(
        &caps,
        "identity.resolve_mention",
        serde_json::json!({ "token": bob_token }),
    )
    .await;
    assert_eq!((actor, target), (w.alice, w.bob));

    // Theft: pay FROM the mentioned member (to the actor), and wager their funds.
    for (op, args) in [
        (
            "economy.transfer",
            serde_json::json!({"from": target.to_string(), "to": actor.to_string(), "amount": 100}),
        ),
        (
            "economy.wager",
            serde_json::json!({"user": target.to_string(), "stake": 50, "payout": 0}),
        ),
    ] {
        let err = refusal(&caps, op, args).await;
        assert_eq!(err.code, "actor_mismatch", "{op}: {}", err.message);
    }
    assert_eq!(
        balance_of(&w, w.bob).await,
        300,
        "the victim was not debited"
    );
    assert_eq!(balance_of(&w, w.alice).await, 500);

    // The direction the actor chose is fine.
    caps.handle(call(
        "economy.transfer",
        serde_json::json!({"from": actor.to_string(), "to": target.to_string(), "amount": 100}),
    ))
    .await
    .unwrap();
    assert_eq!(balance_of(&w, w.alice).await, 400);
    assert_eq!(balance_of(&w, w.bob).await, 400);
}

/// An actor the directory cannot bind (unlinked / not a member / no account on
/// the event) cannot move money at all -- loudly, never unbound.
#[tokio::test]
async fn an_actor_the_directory_cannot_bind_cannot_move_money() {
    let w = world().await;
    let args = |w: &World| serde_json::json!({"from": w.alice.to_string(), "to": w.bob.to_string(), "amount": 5});
    for (account, want) in [
        ("3003", "not_linked"),   // active member, user_uuid NULL
        ("4004", "not_a_member"), // left
        ("9999", "not_a_member"), // no such account
    ] {
        let (inv, _) = invocation_for(&event(account, "!pay")).await;
        let caps = stage(&w, TENANT, COMMUNITY, inv, None).await;
        let err = refusal(&caps, "economy.transfer", args(&w)).await;
        assert_eq!(err.code, want, "{account}");
    }
    assert_eq!(balance_of(&w, w.alice).await, 500);
}

/// Regression (#751 review, double-spend) with the REAL directory: the same
/// event redelivered through a fresh invocation credits once.
#[tokio::test]
async fn a_redelivered_steal_event_moves_the_money_once() {
    let w = world().await;
    let event_id = Uuid::new_v4().to_string();
    let raw = event("1001", "!steal <@2002> 120");
    for delivery in 0..3 {
        let (inv, _) = invocation_for_event(&raw, &event_id).await;
        let caps = stage(&w, TENANT, COMMUNITY, inv, None).await;
        caps.handle(call(
            "economy.transfer",
            serde_json::json!({
                "from": w.alice.to_string(), "to": w.bob.to_string(), "amount": 120
            }),
        ))
        .await
        .unwrap_or_else(|e| panic!("delivery {delivery}: {} {}", e.code, e.message));
    }
    assert_eq!(balance_of(&w, w.alice).await, 380);
    assert_eq!(balance_of(&w, w.bob).await, 120);
}

//! Real caller WASM -> economy WASM -> host gates -> least-privilege Postgres.
//! Redis supplies the actual scoped policy KV import; no money boundary is mocked.
use super::*;
use crate::docker_container::{start_container, DockerContainer};
use bundle_capability_gate::{MembershipCheck, SnapshotMembership};
use bundle_host_economy::{
    connect, load_membership, pot_account, ConnectConfig, EconomyStore, PostgresEconomyStore,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};
use svc_process::capabilities::EconomyWiring;
use svc_process::identity::PgMemberDirectory;

const SERVICE: &str = "waddles.core.service.economy";
const BOB: &str = "22222222-2222-4222-8222-222222222222";
const PASSWORD: &str = "ephemeral-economy-e2e-password";

/// Run one external build operation and fail visibly on any missing tool or failure.
fn checked(command: &mut std::process::Command) -> std::process::Output {
    let output = command.output().expect("build tool must be available");
    assert!(
        output.status.success(),
        "build failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output
}

/// Use a freshly built artifact override or build the exact production Rust stage.
fn component() -> &'static [u8] {
    let path = match std::env::var_os("WADDLES_ECONOMY_COMPONENT") {
        Some(path) => std::path::PathBuf::from(path),
        None => {
            let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap();
            let output = checked(
                std::process::Command::new("docker")
                    .current_dir(root)
                    .args([
                        "build",
                        "-q",
                        "--target",
                        "bundle-economy",
                        "-f",
                        "bundles/Dockerfile.core-bundles",
                        ".",
                    ]),
            );
            let image = String::from_utf8(output.stdout).unwrap();
            let created =
                checked(std::process::Command::new("docker").args(["create", image.trim()]));
            let container = String::from_utf8(created.stdout).unwrap();
            let path =
                std::env::temp_dir().join(format!("economy-service-{}.wasm", std::process::id()));
            let source = format!("{}:/repo/bundles/rust/economy/target/wasm32-wasip2/release/waddle_bundle_economy_service.wasm", container.trim());
            let copy = std::process::Command::new("docker")
                .arg("cp")
                .arg(source)
                .arg(&path)
                .output()
                .expect("docker cp available");
            checked(std::process::Command::new("docker").args(["rm", container.trim()]));
            assert!(
                copy.status.success(),
                "artifact extraction failed: {}",
                String::from_utf8_lossy(&copy.stderr)
            );
            path
        }
    };
    Box::leak(
        std::fs::read(path)
            .expect("real economy component exists")
            .into_boxed_slice(),
    )
}

struct DatabaseFixture {
    _postgres: DockerContainer,
    _redis: DockerContainer,
    admin: DatabaseConnection,
    runtime: DatabaseConnection,
    kv: redis::aio::MultiplexedConnection,
    membership: Arc<SnapshotMembership>,
}

/// Create actual schema, least-privilege role, membership view and Redis policy store.
async fn database() -> DatabaseFixture {
    let postgres = start_container(
        "postgres:17.6-bookworm",
        5432,
        &[
            ("POSTGRES_PASSWORD", PASSWORD),
            ("POSTGRES_DB", "economy_e2e"),
        ],
        &["pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
        Duration::from_secs(240),
    )
    .await;
    let host = postgres.host().to_string();
    let port = postgres.port();
    let admin = Database::connect(format!(
        "postgres://postgres:{PASSWORD}@{host}:{port}/economy_e2e"
    ))
    .await
    .unwrap();
    admin.execute_unprepared(&format!(
        "CREATE TABLE tenants(id INTEGER PRIMARY KEY); CREATE TABLE communities(id INTEGER PRIMARY KEY, tenant_id INTEGER REFERENCES tenants(id));
         CREATE TABLE hub_users(id INTEGER PRIMARY KEY, uuid UUID);
         CREATE TABLE community_members(id SERIAL PRIMARY KEY, community_id INTEGER REFERENCES communities(id), user_id TEXT,
           platform TEXT, platform_user_id TEXT, is_active BOOLEAN DEFAULT TRUE, left_at TIMESTAMP, removed_at TIMESTAMP, user_uuid_unavailable_reason TEXT);
         CREATE TABLE community_permission_grants(
           community_id INTEGER NOT NULL REFERENCES communities(id), tenant_id INTEGER NOT NULL REFERENCES tenants(id),
           app_id VARCHAR(255) NOT NULL, permission_id VARCHAR(255) NOT NULL, params_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
           granted_by INTEGER REFERENCES hub_users(id), granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
           revoked_by INTEGER REFERENCES hub_users(id), revoked_at TIMESTAMPTZ,
           PRIMARY KEY (community_id, app_id, permission_id));
         INSERT INTO hub_users VALUES(41, NULL);
         INSERT INTO tenants VALUES({TENANT}); INSERT INTO communities VALUES({COMMUNITY},{TENANT});
         CREATE ROLE waddles_economy_runtime LOGIN PASSWORD '{PASSWORD}';"
    )).await.unwrap();
    for sql in [
        include_str!("../../../../scripts/db/bundle_economy_store.sql"),
        include_str!("../../../../scripts/db/bundle_economy_idempotency.sql"),
        include_str!("../../../../scripts/db/bundle_economy_ledger_kinds.sql"),
        include_str!("../../../../scripts/db/bundle_economy_elevated.sql"),
        include_str!("../../../../scripts/db/bundle_identity_role.sql"),
    ] {
        admin.execute_unprepared(sql).await.unwrap();
    }
    let pot = pot_account(&bundle_host_economy::EconomyScope {
        tenant_id: TENANT,
        community_id: COMMUNITY,
        app_id: SERVICE.into(),
    });
    admin.execute_unprepared(&format!(
        "INSERT INTO community_members(community_id,user_uuid,platform,platform_user_id,role) VALUES
        ({COMMUNITY},'{ACTOR_UUID}','twitch','1001','community-admin'),({COMMUNITY},'{BOB}','twitch','1002','member');
        INSERT INTO economy_balances(tenant_id,community_id,user_uuid,balance) VALUES({TENANT},{COMMUNITY},'{pot}',100);"
    )).await.unwrap();
    let runtime = connect(
        &ConnectConfig {
            host,
            port,
            name: "economy_e2e".into(),
            user: "waddles_economy_runtime".into(),
        },
        PASSWORD,
    )
    .await
    .unwrap();
    let membership = Arc::new(SnapshotMembership::new());
    assert!(membership.replace_all(load_membership(&runtime, None).await.unwrap()));
    let redis = start_container(
        "redis:7.4.2-alpine",
        6379,
        &[],
        &["redis-cli", "ping"],
        Duration::from_secs(120),
    )
    .await;
    let kv = redis::Client::open(format!("redis://{}:{}/", redis.host(), redis.port()))
        .unwrap()
        .get_multiplexed_async_connection()
        .await
        .unwrap();
    DatabaseFixture {
        _postgres: postgres,
        _redis: redis,
        admin,
        runtime,
        kv,
        membership,
    }
}

struct EconomyWorld {
    db: DatabaseFixture,
    rig: Rig,
    wasm: &'static [u8],
    snapshot: ActiveVersionSnapshot,
    gate: Arc<CapabilityGate>,
    telemetry: Telemetry,
    metrics: Arc<StageMetrics>,
}

impl EconomyWorld {
    /// Load the real compiled service and grant exactly the caller tools and service host imports.
    async fn new() -> Self {
        Self::new_with(&[]).await
    }

    /// The default world: the caller is approved for the MINT-class elevated grant
    /// only (the existing assertions mint, set and pay), never `transfer_from`.
    async fn new_with(ungranted: &[&str]) -> Self {
        Self::new_full(ungranted, &["economy.app_mint"]).await
    }

    /// Like [`Self::new`], but the caller REQUESTS every tool while the platform never
    /// grants `tool.call` for those in `ungranted` (an unapproved caller), and holds
    /// exactly the elevated grants in `elevated` (in the grant snapshot AND the
    /// `community_permission_grants` table the store re-reads in-transaction).
    async fn new_full(ungranted: &[&str], elevated: &[&str]) -> Self {
        let wasm = component();
        let db = database().await;
        let names = [
            ("adjust_balance", true),
            ("transfer", true),
            ("pay_pot", true),
            ("exchange", true),
            ("points_set", true),
            ("transfer_from", true),
            ("get_balance", false),
        ];
        let requests = names
            .iter()
            .map(|(n, w)| (SERVICE, *n, *w))
            .collect::<Vec<_>>();
        for id in elevated {
            db.admin
                .execute_unprepared(&format!(
                    "INSERT INTO community_permission_grants \
                       (community_id, tenant_id, app_id, permission_id, params_json, granted_by) \
                     VALUES ({COMMUNITY}, {TENANT}, '{CALLER}', '{id}', \
                       '{{\"max_per_op\":100,\"max_per_day\":1000,\"max_per_target_per_day\":500}}'::jsonb, 41)"
                ))
                .await
                .unwrap();
        }
        let mut service = tools(true, &names, &[]);
        // Mirrors the shipped manifests: these tools carry `required_scope: community:admin`.
        for admin_tool in ["points_set", "exchange"] {
            service
                .exported
                .iter_mut()
                .find(|t| t.name == admin_tool)
                .unwrap()
                .required_scope = Some("community:admin".into());
        }
        let snapshot = ActiveVersionSnapshot::new();
        snapshot.update_scoped_all(
            &[
                (
                    (TENANT, COMMUNITY, CALLER.into()),
                    row(CALLER, V_CALLER, CALLER_WASM, tools(false, &[], &requests)),
                ),
                (
                    (TENANT, COMMUNITY, SERVICE.into()),
                    row(SERVICE, V_SVC1, wasm, service),
                ),
            ]
            .into_iter()
            .collect(),
        );
        let grants = InMemoryGrantSnapshot::new();
        let mut caller: std::collections::HashMap<String, GrantedPermission> = names
            .iter()
            .filter(|(n, _)| !ungranted.contains(n))
            .map(|(n, _)| {
                let id = format!("tool.call:{SERVICE}:{n}");
                (
                    id.clone(),
                    GrantedPermission {
                        permission_id: id,
                        params: json!({}),
                    },
                )
            })
            .collect();
        for id in elevated {
            caller.insert(
                (*id).to_string(),
                GrantedPermission {
                    permission_id: (*id).to_string(),
                    params: json!({"max_per_op":100,"max_per_day":1000,"max_per_target_per_day":500}),
                },
            );
        }
        grants.set(
            GrantScopeKey {
                tenant_id: TENANT,
                community_id: COMMUNITY,
                app_id: CALLER.into(),
                app_version: V_CALLER,
            },
            GrantSet {
                permission_snapshot_hash: "caller".into(),
                grants: caller,
            },
        );
        let service_grants = [
            "economy.read",
            "economy.credit",
            "economy.debit",
            "economy.transfer",
            "economy.pot.pay",
            "economy.admin_adjust",
            "economy.transfer_from",
            "storage.kv",
        ]
        .into_iter()
        .map(|id| {
            (
                id.to_string(),
                GrantedPermission {
                    permission_id: id.into(),
                    params: json!({"max_amount":1000}),
                },
            )
        })
        .collect();
        grants.set(
            GrantScopeKey {
                tenant_id: TENANT,
                community_id: COMMUNITY,
                app_id: SERVICE.into(),
                app_version: V_SVC1,
            },
            GrantSet {
                permission_snapshot_hash: "service".into(),
                grants: service_grants,
            },
        );
        let gate = Arc::new(CapabilityGate::new(
            Arc::new(grants),
            db.membership.clone() as Arc<dyn MembershipCheck>,
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        ));
        let telemetry = Telemetry::new();
        // The OTel meter provider is process-global: only the world that asserts telemetry
        // (the fully-granted one) may install it, or a parallel test would steal its points.
        if ungranted.is_empty() {
            opentelemetry::global::set_meter_provider(telemetry.provider.clone());
        }
        let metrics = Arc::new(StageMetrics::new(
            &telemetry.provider.meter("economy-service-e2e"),
        ));
        let rig = Rig::start(&[(CALLER, CALLER_WASM), (SERVICE, wasm)])
            .await
            .expect("real caller and economy WASM load");
        Self {
            db,
            rig,
            wasm,
            snapshot,
            gate,
            telemetry,
            metrics,
        }
    }

    /// Execute a caller request with a new host invocation and the same durable DB state.
    async fn call(&self, name: &str, args: Value, key: &str, account: &str) -> Value {
        let snapshot = Arc::new(bundle_host_kv::CapabilitySnapshot::new());
        snapshot.update(SERVICE, vec!["storage.kv".into()]);
        let caps = StageCapabilities::new(
            "acme".into(),
            Some("main".into()),
            CALLER.into(),
            TENANT,
            COMMUNITY,
            V_CALLER,
            egress(),
            self.gate.clone(),
        )
        .with_tool(ToolContext {
            connections: self.rig.registry.clone(),
            snapshot: self.snapshot.clone(),
            chain: vec![CALLER.into()],
            deadline: Some(Instant::now() + Duration::from_secs(30)),
            trace: None,
            read_only: false,
        })
        .with_economy(EconomyWiring {
            store: Arc::new(PostgresEconomyStore::new(self.db.runtime.clone()))
                as Arc<dyn EconomyStore>,
            flag: Arc::new(StaticGate(true)),
        })
        .with_identity(
            IdentityWiring {
                directory: Arc::new(PgMemberDirectory::new(self.db.admin.clone())),
                handles: None,
                flag: Arc::new(StaticGate(true)),
            },
            Arc::new(
                InvocationIdentity::new("twitch", Some(account.into()), vec![])
                    .with_event_id(uuid::Uuid::new_v4().to_string()),
            ),
        )
        .with_kv(self.db.kv.clone(), snapshot)
        .with_stage_metrics(self.metrics.clone());
        let reply = tokio::time::timeout(
            Duration::from_secs(45),
            self.rig.call_tool(
                CALLER,
                CALLER_WASM,
                "go",
                call(SERVICE, name, args, Some(key)),
                30000,
                Arc::new(caps),
            ),
        )
        .await
        .unwrap()
        .unwrap();
        match reply {
            Message::Result(result) => result.payload,
            other => panic!("expected tool result, got {other:?}"),
        }
    }

    /// Read committed balances directly as an independent assertion on the database.
    async fn balance(&self, user: &str) -> i64 {
        let rows=self.db.admin.query_all_raw(sea_orm::Statement::from_string(sea_orm::DbBackend::Postgres,format!("SELECT balance FROM economy_balances WHERE tenant_id={TENANT} AND community_id={COMMUNITY} AND user_uuid='{user}'"))).await.unwrap();
        rows.first()
            .map(|r| r.try_get_by_index(0).unwrap())
            .unwrap_or(0)
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn real_economy_service_preserves_atomicity_idempotency_caps_and_admin_scope() {
    let w = EconomyWorld::new().await;
    let credit = json!({"user":ACTOR_UUID,"delta":100});
    assert_eq!(
        w.call("adjust_balance", credit.clone(), "mint-once", "1001")
            .await["ok"]["balance"],
        100
    );
    assert_eq!(
        w.call("adjust_balance", credit, "mint-once", "1001").await["ok"]["balance"],
        100
    );
    assert_eq!(w.balance(ACTOR_UUID).await, 100);
    let transfer = json!({"from":ACTOR_UUID,"to":BOB,"amount":30});
    assert!(w
        .call("transfer", transfer.clone(), "transfer-once", "1001")
        .await
        .get("ok")
        .is_some());
    w.call("transfer", transfer, "transfer-once", "1001").await;
    assert_eq!(
        (w.balance(ACTOR_UUID).await, w.balance(BOB).await),
        (70, 30)
    );
    let exchange = json!({"user":ACTOR_UUID,"from_amount":5});
    assert_eq!(
        w.call("exchange", exchange.clone(), "exchange-once", "1001")
            .await["ok"]["balance"],
        80
    );
    w.call("exchange", exchange, "exchange-once", "1001").await;
    assert_eq!(w.balance(ACTOR_UUID).await, 80);
    // regression: p981 finding 3 -- `exchange` is admin-only: a plain member (1002) is refused
    // and nothing is minted.
    let member_exchange = w
        .call(
            "exchange",
            json!({"user":BOB,"from_amount":5}),
            "exchange-member",
            "1002",
        )
        .await;
    assert!(member_exchange.get("ok").is_none(), "{member_exchange:?}");
    assert_eq!(w.balance(BOB).await, 30);
    let payout = json!({"winners":[{"user":ACTOR_UUID,"amount":20},{"user":BOB,"amount":30}]});
    w.call("pay_pot", payout.clone(), "pot-once", "1001").await;
    w.call("pay_pot", payout, "pot-once", "1001").await;
    assert_eq!(
        (w.balance(ACTOR_UUID).await, w.balance(BOB).await),
        (100, 60)
    );
    let bad = w
        .call(
            "pay_pot",
            json!({"winners":[{"user":ACTOR_UUID,"amount":20},{"user":BOB,"amount":100}]}),
            "pot-refused",
            "1001",
        )
        .await;
    assert!(bad.get("tool_error").is_some(), "{bad:?}");
    assert_eq!(
        (w.balance(ACTOR_UUID).await, w.balance(BOB).await),
        (100, 60)
    );
    let over = w
        .call(
            "adjust_balance",
            json!({"user":ACTOR_UUID,"delta":1001}),
            "over-cap",
            "1001",
        )
        .await;
    assert!(over.get("tool_error").is_some());
    assert_eq!(w.balance(ACTOR_UUID).await, 100);
    // regression: p981 finding 1 -- a debit is bound to the invocation's ACTOR: a granted caller
    // cannot burn ANOTHER member's funds through `adjust_balance` (BOB is not actor 1001).
    let burn_other = w
        .call(
            "adjust_balance",
            json!({"user":BOB,"delta":-5}),
            "burn-other",
            "1001",
        )
        .await;
    assert!(burn_other.get("ok").is_none(), "{burn_other:?}");
    assert_eq!(w.balance(BOB).await, 60);
    let denied = w
        .call(
            "points_set",
            json!({"user":BOB,"balance":777}),
            "admin-denied",
            "1002",
        )
        .await;
    assert!(denied.get("tool_error").is_some());
    assert_eq!(w.balance(BOB).await, 60);
    assert_eq!(
        w.call(
            "points_set",
            json!({"user":BOB,"balance":77}),
            "admin-allowed",
            "1001"
        )
        .await["ok"]["balance"],
        77
    );
    assert!(w.telemetry.count("waddles_tool_call", &[]) > 0.0);
    assert!(w.telemetry.count("waddles_bundle_economy_calls_total", &[]) > 0.0);
    assert!(!w.wasm.is_empty());
}

/// regression: p981 findings 1-2 -- a caller the platform never granted `tool.call` for the
/// money tools is refused before any money moves (the grant, not the tool body, is the gate).
#[tokio::test(flavor = "multi_thread")]
async fn an_unapproved_caller_cannot_reach_the_money_tools() {
    let w = EconomyWorld::new_with(&["adjust_balance", "pay_pot", "exchange"]).await;
    for (name, args) in [
        ("adjust_balance", json!({"user":ACTOR_UUID,"delta":10})),
        (
            "pay_pot",
            json!({"winners":[{"user":ACTOR_UUID,"amount":1}]}),
        ),
        ("exchange", json!({"user":ACTOR_UUID,"from_amount":5})),
    ] {
        let reply = w.call(name, args, "ungranted", "1001").await;
        assert!(reply.get("ok").is_none(), "{name}: {reply:?}");
        assert!(reply["tool_error"] == "NotGranted", "{name}: {reply:?}");
    }
    assert_eq!(w.balance(ACTOR_UUID).await, 0, "nothing was minted");
}

/// Sets a balance as the privileged hub-side writer (the runtime role cannot mint).
async fn fund(w: &EconomyWorld, user: &str, balance: i64) {
    w.db.admin
        .execute_unprepared(&format!(
            "INSERT INTO economy_balances(tenant_id,community_id,user_uuid,balance) \
             VALUES({TENANT},{COMMUNITY},'{user}',{balance}) \
             ON CONFLICT (tenant_id,community_id,user_uuid) DO UPDATE SET balance={balance}"
        ))
        .await
        .unwrap();
}

async fn audit_rows(w: &EconomyWorld) -> i64 {
    w.db.admin
        .query_one_raw(sea_orm::Statement::from_string(
            sea_orm::DbBackend::Postgres,
            "SELECT COUNT(*) FROM economy_elevated_audit".to_string(),
        ))
        .await
        .unwrap()
        .unwrap()
        .try_get_by_index(0)
        .unwrap()
}

/// Regression (security review): `tool.call:...:adjust_balance` alone must not let an app
/// mint, debit a non-actor or `transfer_from` -- the CALLING app needs its own approved
/// elevated grant. Real caller WASM -> real economy WASM -> real gates -> real Postgres.
#[tokio::test(flavor = "multi_thread")]
async fn an_app_without_elevated_grants_cannot_mint_debit_others_or_transfer_from() {
    let w = EconomyWorld::new_full(&[], &[]).await;
    fund(&w, BOB, 60).await;
    for (name, args, key) in [
        (
            "adjust_balance",
            json!({"user":ACTOR_UUID,"delta":10}),
            "e-mint",
        ),
        ("adjust_balance", json!({"user":BOB,"delta":-5}), "e-burn"),
        (
            "transfer_from",
            json!({"from":BOB,"to":ACTOR_UUID,"amount":5,"reason":"steal"}),
            "e-steal",
        ),
    ] {
        let reply = w.call(name, args, key, "1001").await;
        assert!(reply.get("ok").is_none(), "{name}: {reply:?}");
        assert_eq!(reply["tool_error"], "NotGranted", "{name}: {reply:?}");
    }
    assert_eq!((w.balance(ACTOR_UUID).await, w.balance(BOB).await), (0, 60));
    assert_eq!(audit_rows(&w).await, 0);
}

/// The `transfer_from` primitive end to end: moves a NON-actor's funds once (idempotent),
/// returns both balances, honours the admin-set per-call cap and leaves an audit row naming
/// the calling app and the approving admin.
#[tokio::test(flavor = "multi_thread")]
async fn transfer_from_moves_a_non_actors_funds_once_under_the_callers_grant_and_caps() {
    let w = EconomyWorld::new_full(&[], &["economy.transfer_from"]).await;
    fund(&w, BOB, 60).await;
    let steal = json!({"from":BOB,"to":ACTOR_UUID,"amount":25,"reason":"steal"});
    let first = w
        .call("transfer_from", steal.clone(), "steal-once", "1001")
        .await;
    assert_eq!(first["ok"]["from_balance"], 35, "{first:?}");
    assert_eq!(first["ok"]["to_balance"], 25, "{first:?}");
    // The retry of the same call (same key) replays: nothing moves a second time.
    let replay = w.call("transfer_from", steal, "steal-once", "1001").await;
    assert_eq!(replay["ok"]["from_balance"], 35, "{replay:?}");
    assert_eq!(
        (w.balance(BOB).await, w.balance(ACTOR_UUID).await),
        (35, 25)
    );
    // Above the admin-set per-call cap (100): refused, nothing moves.
    fund(&w, BOB, 1_000).await;
    let big = w
        .call(
            "transfer_from",
            json!({"from":BOB,"to":ACTOR_UUID,"amount":101,"reason":"steal"}),
            "steal-big",
            "1001",
        )
        .await;
    assert!(big.get("ok").is_none(), "{big:?}");
    assert_eq!(w.balance(BOB).await, 1_000);
    // The mint class was NOT granted: minting through the service is still refused.
    let mint = w
        .call(
            "adjust_balance",
            json!({"user":ACTOR_UUID,"delta":10}),
            "no-mint",
            "1001",
        )
        .await;
    assert_eq!(mint["tool_error"], "NotGranted", "{mint:?}");
    // One audit row (the applied steal): calling app + approving admin recorded.
    assert_eq!(audit_rows(&w).await, 1);
    let approved: i64 =
        w.db.admin
            .query_one_raw(sea_orm::Statement::from_string(
                sea_orm::DbBackend::Postgres,
                format!(
                    "SELECT COUNT(*) FROM economy_elevated_audit WHERE caller_app_id = '{CALLER}' \
                 AND service_app_id = '{SERVICE}' AND approved_by = 41 AND amount = 25 \
                 AND reason = 'steal' AND status = 2"
                ),
            ))
            .await
            .unwrap()
            .unwrap()
            .try_get_by_index(0)
            .unwrap();
    assert_eq!(approved, 1);
}

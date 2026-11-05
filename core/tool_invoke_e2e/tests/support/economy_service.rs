//! Real caller WASM -> economy WASM -> host gates -> least-privilege Postgres.
//! Redis supplies the actual scoped policy KV import; no money boundary is mocked.
use super::*;
use bundle_capability_gate::{MembershipCheck, SnapshotMembership};
use bundle_host_economy::{
    connect, load_membership, pot_account, ConnectConfig, EconomyStore, PostgresEconomyStore,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};
use svc_process::capabilities::EconomyWiring;
use svc_process::identity::PgMemberDirectory;
use testcontainers::core::logs::LogSource;
use testcontainers::core::wait::LogWaitStrategy;
use testcontainers::core::{ContainerPort, WaitFor};
use testcontainers::runners::AsyncRunner;
use testcontainers::{ContainerAsync, GenericImage, ImageExt};

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
                        "rust-bundle-builder",
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
    _postgres: ContainerAsync<GenericImage>,
    _redis: ContainerAsync<GenericImage>,
    admin: DatabaseConnection,
    runtime: DatabaseConnection,
    kv: redis::aio::MultiplexedConnection,
    membership: Arc<SnapshotMembership>,
}

/// Create actual schema, least-privilege role, membership view and Redis policy store.
async fn database() -> DatabaseFixture {
    let postgres = GenericImage::new("postgres", "17.6-bookworm")
        .with_exposed_port(ContainerPort::Tcp(5432))
        .with_wait_for(WaitFor::log(
            LogWaitStrategy::new(
                LogSource::BothStd,
                "database system is ready to accept connections",
            )
            .with_times(2),
        ))
        .with_env_var("POSTGRES_PASSWORD", PASSWORD)
        .with_env_var("POSTGRES_DB", "economy_e2e")
        .start()
        .await
        .expect("real Postgres starts");
    let host = postgres.get_host().await.unwrap().to_string();
    let port = postgres.get_host_port_ipv4(5432).await.unwrap();
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
         INSERT INTO tenants VALUES({TENANT}); INSERT INTO communities VALUES({COMMUNITY},{TENANT});
         CREATE ROLE waddles_economy_runtime LOGIN PASSWORD '{PASSWORD}';"
    )).await.unwrap();
    for sql in [
        include_str!("../../../../scripts/db/bundle_economy_store.sql"),
        include_str!("../../../../scripts/db/bundle_economy_idempotency.sql"),
        include_str!("../../../../scripts/db/bundle_economy_ledger_kinds.sql"),
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
    let redis = GenericImage::new("redis", "7.4.2-alpine")
        .with_exposed_port(ContainerPort::Tcp(6379))
        .with_wait_for(WaitFor::message_on_stdout("Ready to accept connections"))
        .start()
        .await
        .expect("real Redis starts");
    let kv = redis::Client::open(format!(
        "redis://{}:{}/",
        redis.get_host().await.unwrap(),
        redis.get_host_port_ipv4(6379).await.unwrap()
    ))
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

    /// Like [`Self::new`], but the caller REQUESTS every tool while the platform never
    /// grants `tool.call` for those in `ungranted` (an unapproved caller).
    async fn new_with(ungranted: &[&str]) -> Self {
        let wasm = component();
        let db = database().await;
        let names = [
            ("adjust_balance", true),
            ("transfer", true),
            ("pay_pot", true),
            ("exchange", true),
            ("points_set", true),
            ("get_balance", false),
        ];
        let requests = names
            .iter()
            .map(|(n, w)| (SERVICE, *n, *w))
            .collect::<Vec<_>>();
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
        let caller = names
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

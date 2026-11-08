//! `gameserver_net.*` host-capability tests: the REAL [`CapabilityGate`], the REAL `handle`
//! dispatch and the REAL `bundle_host_gameserver_net::GameNet` against a loopback RCON server
//! (loopback is reachable only because this crate's dev-dependency enables `test-loopback`).

use super::super::*;
use crate::license::test_support::FixedGate;
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use bundle_capability_gate::{
    GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
    InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
};
use bundle_host_gameserver_net::directory::{DirFuture, ServerDirectory, ServerTarget};
use bundle_host_gameserver_net::rcon::encode_packet;
use bundle_host_gameserver_net::{
    AuditEvent, AuditSink, GameNet, GameNetConfig, NetError, Scope, Secret,
};
use bundle_host_http::egress::{ReqwestTransport, StaticFlag};
use std::sync::Mutex;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const TENANT_ID: i32 = 7;
const COMMUNITY_ID: i32 = 3;
const CORE_APP: &str = "waddles.core.service.gameserver";
const VENDOR_APP: &str = "acme.vendor.thing";
const VERSION: i64 = 1;
const SERVER: &str = "123e4567-e89b-12d3-a456-426614174000";
const PASSWORD: &str = "guest-must-never-see-this";

struct Dir {
    port: u16,
}

impl ServerDirectory for Dir {
    fn lookup<'a>(
        &'a self,
        scope: &'a Scope,
        server: &'a str,
    ) -> DirFuture<'a, Result<ServerTarget, NetError>> {
        Box::pin(async move {
            if scope.tenant_id != i64::from(TENANT_ID)
                || scope.community_id != i64::from(COMMUNITY_ID)
                || server != SERVER
            {
                return Err(NetError::Denied("not_registered"));
            }
            Ok(ServerTarget {
                server_id: SERVER.to_string(),
                host: "127.0.0.1".to_string(),
                port: self.port,
                password: Secret::new(PASSWORD.to_string()),
            })
        })
    }
}

#[derive(Default)]
struct Capture(Mutex<Vec<AuditEvent>>);
impl AuditSink for Capture {
    fn record(&self, e: &AuditEvent) {
        self.0.lock().unwrap().push(e.clone());
    }
}

async fn read_pkt(s: &mut tokio::net::TcpStream) -> Option<(i32, i32, Vec<u8>)> {
    let mut size = [0u8; 4];
    s.read_exact(&mut size).await.ok()?;
    let n = i32::from_le_bytes(size) as usize;
    let mut rest = vec![0u8; n];
    s.read_exact(&mut rest).await.ok()?;
    Some((
        i32::from_le_bytes(rest[0..4].try_into().unwrap()),
        i32::from_le_bytes(rest[4..8].try_into().unwrap()),
        rest[8..n - 2].to_vec(),
    ))
}

async fn rcon_server() -> u16 {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = l.local_addr().unwrap().port();
    tokio::spawn(async move {
        loop {
            let Ok((mut s, _)) = l.accept().await else {
                return;
            };
            tokio::spawn(async move {
                let Some((id, _, body)) = read_pkt(&mut s).await else {
                    return;
                };
                let ok = body == PASSWORD.as_bytes();
                let _ = s
                    .write_all(&encode_packet(if ok { id } else { -1 }, 2, b""))
                    .await;
                while let Some((id, ty, body)) = read_pkt(&mut s).await {
                    if ty == 2 {
                        let _ = s
                            .write_all(&encode_packet(id, 0, &[b"echo:", &body[..]].concat()))
                            .await;
                    }
                }
            });
        }
    });
    port
}

struct Fx {
    caps: StageCapabilities,
    audit: Arc<Capture>,
}

fn fixture(app: &str, grants: &[&str], flag_on: bool, wired: bool, port: u16) -> Fx {
    let snapshot = InMemoryGrantSnapshot::new();
    snapshot.set(
        GrantScopeKey {
            tenant_id: TENANT_ID,
            community_id: COMMUNITY_ID,
            app_id: app.to_string(),
            app_version: VERSION,
        },
        GrantSet {
            permission_snapshot_hash: "t".to_string(),
            grants: grants
                .iter()
                .map(|id| {
                    (
                        (*id).to_string(),
                        GrantedPermission {
                            permission_id: (*id).to_string(),
                            params: serde_json::json!({}),
                        },
                    )
                })
                .collect(),
        },
    );
    let gate = Arc::new(CapabilityGate::new(
        Arc::new(snapshot),
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(InMemoryInstancePolicySnapshot::new()),
    ));
    let egress = Arc::new(EgressGuard::new(
        Arc::new(ReqwestTransport::new()),
        bundle_host_http::egress::EgressLimits {
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
            prometheus::Opts::new("test_gs_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap(),
        bundle_host_http::egress::boxed(StaticFlag(true)),
    ));
    let mut caps = StageCapabilities::new(
        "acme".to_string(),
        Some("main".to_string()),
        app.to_string(),
        TENANT_ID,
        COMMUNITY_ID,
        VERSION,
        egress,
        gate,
    );
    let audit = Arc::new(Capture::default());
    if wired {
        let cfg = GameNetConfig {
            allow_loopback: true,
            read_timeout: std::time::Duration::from_secs(2),
            ..GameNetConfig::default()
        };
        caps = caps.with_gameserver_net(GameServerNetWiring {
            net: Arc::new(GameNet::new(
                Arc::new(Dir { port }),
                Arc::clone(&audit) as Arc<dyn AuditSink>,
                cfg,
            )),
            flag: Arc::new(FixedGate(flag_on)),
        });
    }
    Fx { caps, audit }
}

fn call(app: &str, op: &str, args: serde_json::Value) -> HostCallBody {
    HostCallBody {
        app_id: app.to_string(),
        capability: CapabilityKind::Db,
        op: op.to_string(),
        args,
        call_id: 1,
    }
}

fn open_args() -> serde_json::Value {
    serde_json::json!({ "server": SERVER, "scheme": "source-rcon" })
}

async fn code(f: &Fx, app: &str, op: &str, args: serde_json::Value) -> String {
    f.caps
        .handle(call(app, op, args))
        .await
        .expect_err("expected a denial")
        .code
}

#[tokio::test]
async fn ungranted_open_is_denied_before_any_socket() {
    let f = fixture(CORE_APP, &[], true, true, 1);
    assert_eq!(
        code(&f, CORE_APP, "gameserver_net.rcon_open", open_args()).await,
        "not_granted"
    );
    assert!(f.audit.0.lock().unwrap().is_empty());
}

#[tokio::test]
async fn vendor_namespace_is_refused_even_with_a_grant() {
    let f = fixture(VENDOR_APP, &["gameserver.net"], true, true, 1);
    assert_eq!(
        code(&f, VENDOR_APP, "gameserver_net.rcon_open", open_args()).await,
        "not_granted"
    );
    assert!(f.audit.0.lock().unwrap().is_empty());
}

#[tokio::test]
async fn unwired_and_flag_off_fail_loud() {
    let f = fixture(CORE_APP, &["gameserver.net"], true, false, 1);
    assert_eq!(
        code(&f, CORE_APP, "gameserver_net.rcon_open", open_args()).await,
        "not_implemented"
    );
    let f = fixture(CORE_APP, &["gameserver.net"], false, true, 1);
    assert_eq!(
        code(&f, CORE_APP, "gameserver_net.rcon_open", open_args()).await,
        "feature_disabled"
    );
}

#[tokio::test]
async fn unknown_op_and_bad_args_are_rejected() {
    let f = fixture(CORE_APP, &["gameserver.net"], true, true, 1);
    assert_eq!(
        code(&f, CORE_APP, "gameserver_net.exec", serde_json::json!({})).await,
        "unknown_op"
    );
    assert_eq!(
        code(
            &f,
            CORE_APP,
            "gameserver_net.rcon_open",
            serde_json::json!({"server": SERVER, "scheme": "telnet"})
        )
        .await,
        "invalid_args"
    );
    assert_eq!(
        code(
            &f,
            CORE_APP,
            "gameserver_net.tcp_read",
            serde_json::json!({"handle": "x", "max": 1})
        )
        .await,
        "invalid_args"
    );
}

#[tokio::test]
async fn full_session_round_trip_never_exposes_the_password() {
    let port = rcon_server().await;
    let f = fixture(CORE_APP, &["gameserver.net"], true, true, port);
    // Guest-supplied host/port/tenant/community are ignored: only `server` + `scheme` are read.
    let mut args = open_args();
    args["host"] = serde_json::json!("169.254.169.254");
    args["port"] = serde_json::json!(80);
    args["tenant_id"] = serde_json::json!(999);
    args["community_id"] = serde_json::json!(999);
    let opened = f
        .caps
        .handle(call(CORE_APP, "gameserver_net.rcon_open", args))
        .await
        .unwrap();
    let handle = opened["handle"].as_u64().unwrap();

    let write = serde_json::json!({"handle": handle, "bytes_b64": B64.encode(encode_packet(5, 2, b"status"))});
    f.caps
        .handle(call(CORE_APP, "gameserver_net.tcp_write", write))
        .await
        .unwrap();
    let read = f
        .caps
        .handle(call(
            CORE_APP,
            "gameserver_net.tcp_read",
            serde_json::json!({"handle": handle, "max": 4096}),
        ))
        .await
        .unwrap();
    let bytes = B64.decode(read["bytes_b64"].as_str().unwrap()).unwrap();
    assert!(String::from_utf8_lossy(&bytes).contains("echo:status"));

    let all = format!("{opened}{read}{:?}", f.audit.0.lock().unwrap());
    assert!(!all.contains(PASSWORD), "password leaked: {all}");
    assert!(!all.contains("127.0.0.1"));

    f.caps
        .handle(call(
            CORE_APP,
            "gameserver_net.tcp_close",
            serde_json::json!({"handle": handle}),
        ))
        .await
        .unwrap();
    // a closed/unknown handle is an explicit error, never a silent success
    assert_eq!(
        code(
            &f,
            CORE_APP,
            "gameserver_net.tcp_read",
            serde_json::json!({"handle": handle, "max": 1})
        )
        .await,
        "invalid_args"
    );
}

#[tokio::test]
async fn host_rejects_smuggled_commands_and_unregistered_servers() {
    let port = rcon_server().await;
    let f = fixture(CORE_APP, &["gameserver.net"], true, true, port);
    let handle = f
        .caps
        .handle(call(CORE_APP, "gameserver_net.rcon_open", open_args()))
        .await
        .unwrap()["handle"]
        .as_u64()
        .unwrap();
    let bad = serde_json::json!({"handle": handle, "bytes_b64": B64.encode(encode_packet(1, 2, b"say a\nstop"))});
    let err = f
        .caps
        .handle(call(CORE_APP, "gameserver_net.tcp_write", bad))
        .await
        .unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("denied", "command_rejected")
    );

    let other = serde_json::json!({"server": "00000000-0000-0000-0000-000000000000", "scheme": "source-rcon"});
    let err = f
        .caps
        .handle(call(CORE_APP, "gameserver_net.rcon_open", other))
        .await
        .unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("denied", "not_registered")
    );
}

// regression: the cap must count a session that is out of the map for in-flight I/O
#[tokio::test]
async fn session_cap_holds_while_a_session_is_out_of_the_table_for_io() {
    let port = rcon_server().await;
    let f = fixture(CORE_APP, &["gameserver.net"], true, true, port);
    let open = || {
        call(
            CORE_APP,
            "gameserver_net.tcp_open",
            serde_json::json!({"server": SERVER}),
        )
    };
    let h1 = f.caps.handle(open()).await.unwrap()["handle"]
        .as_u64()
        .unwrap();
    let h2 = f.caps.handle(open()).await.unwrap()["handle"]
        .as_u64()
        .unwrap();
    // Simulate both sessions mid-I/O (taken out of the map): map.len() would now be 0.
    let s1 = f.caps.take_session(h1).await.unwrap();
    let s2 = f.caps.take_session(h2).await.unwrap();
    let err = f.caps.handle(open()).await.unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("denied", "too_many_connections")
    );
    f.caps.put_session(h1, s1).await;
    f.caps.put_session(h2, s2).await;
    let err = f.caps.handle(open()).await.unwrap_err();
    assert_eq!(err.message, "too_many_connections");
    // closing frees the permit; a failed open frees its permit too
    f.caps
        .handle(call(
            CORE_APP,
            "gameserver_net.tcp_close",
            serde_json::json!({"handle": h1}),
        ))
        .await
        .unwrap();
    f.caps.handle(open()).await.unwrap();
    f.caps
        .handle(call(
            CORE_APP,
            "gameserver_net.tcp_close",
            serde_json::json!({"handle": h2}),
        ))
        .await
        .unwrap();
    let bad = || {
        call(
            CORE_APP,
            "gameserver_net.tcp_open",
            serde_json::json!({"server": "00000000-0000-0000-0000-000000000000"}),
        )
    };
    // (the gate quota is 10 opens/s: keep the total number of opens in this test below it)
    for _ in 0..2 {
        let e = f.caps.handle(bad()).await.unwrap_err();
        assert_eq!(
            e.message, "not_registered",
            "a failed open must not leak a permit"
        );
    }
    f.caps.handle(open()).await.unwrap();
}

#[tokio::test]
async fn per_invocation_session_cap_is_enforced() {
    let port = rcon_server().await;
    let f = fixture(CORE_APP, &["gameserver.net"], true, true, port);
    for _ in 0..2 {
        f.caps
            .handle(call(
                CORE_APP,
                "gameserver_net.tcp_open",
                serde_json::json!({"server": SERVER}),
            ))
            .await
            .unwrap();
    }
    let err = f
        .caps
        .handle(call(
            CORE_APP,
            "gameserver_net.tcp_open",
            serde_json::json!({"server": SERVER}),
        ))
        .await
        .unwrap_err();
    assert_eq!(
        (err.code.as_str(), err.message.as_str()),
        ("denied", "too_many_connections")
    );
}

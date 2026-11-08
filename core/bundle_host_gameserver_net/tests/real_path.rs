//! Real-path tests: the REAL `GameNet` (guard, limits, RCON login, budgets, audit) against a real
//! loopback RCON/TCP/UDP server. Loopback is reachable ONLY because this file is compiled with the
//! `test-loopback` feature and the config sets it explicitly; `tests/normal_mode.rs` proves the
//! default posture refuses it. Run: `cargo test --features test-loopback`.
#![cfg(feature = "test-loopback")]
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_host_gameserver_net::directory::{DirFuture, ServerDirectory, ServerTarget};
use bundle_host_gameserver_net::rcon::encode_packet;
use bundle_host_gameserver_net::{
    AuditEvent, AuditSink, GameNet, GameNetConfig, NetError, Scheme, Scope, Secret,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, UdpSocket};

const SERVER: &str = "123e4567-e89b-12d3-a456-426614174000";
const PASSWORD: &str = "s3cr3t-rcon-pw";
const SCOPE: Scope = Scope {
    tenant_id: 7,
    community_id: 3,
};

/// Directory with exactly one registered server, for exactly one (tenant, community).
struct OneServer {
    scope: Scope,
    host: String,
    port: u16,
    password: String,
}

impl ServerDirectory for OneServer {
    fn lookup<'a>(
        &'a self,
        scope: &'a Scope,
        server: &'a str,
    ) -> DirFuture<'a, Result<ServerTarget, NetError>> {
        Box::pin(async move {
            if *scope != self.scope || server != SERVER {
                return Err(NetError::Denied("not_registered"));
            }
            Ok(ServerTarget {
                server_id: SERVER.to_string(),
                host: self.host.clone(),
                port: self.port,
                password: Secret::new(self.password.clone()),
            })
        })
    }
}

#[derive(Default)]
struct Capture(Mutex<Vec<AuditEvent>>);

impl AuditSink for Capture {
    fn record(&self, event: &AuditEvent) {
        self.0.lock().unwrap().push(event.clone());
    }
}

fn cfg() -> GameNetConfig {
    GameNetConfig {
        allow_loopback: true,
        connect_timeout: Duration::from_secs(2),
        read_timeout: Duration::from_secs(2),
        ..GameNetConfig::default()
    }
}

fn net_with(addr: SocketAddr, password: &str, cfg: GameNetConfig) -> (GameNet, Arc<Capture>) {
    let audit = Arc::new(Capture::default());
    let dir = Arc::new(OneServer {
        scope: SCOPE,
        host: addr.ip().to_string(),
        port: addr.port(),
        password: password.to_string(),
    });
    (
        GameNet::new(dir, Arc::clone(&audit) as Arc<dyn AuditSink>, cfg),
        audit,
    )
}

async fn read_packet(s: &mut tokio::net::TcpStream) -> Option<(i32, i32, Vec<u8>)> {
    let mut size = [0u8; 4];
    s.read_exact(&mut size).await.ok()?;
    let size = i32::from_le_bytes(size) as usize;
    let mut rest = vec![0u8; size];
    s.read_exact(&mut rest).await.ok()?;
    let id = i32::from_le_bytes(rest[0..4].try_into().unwrap());
    let ty = i32::from_le_bytes(rest[4..8].try_into().unwrap());
    Some((id, ty, rest[8..size - 2].to_vec()))
}

/// A real RCON server: Source-style empty type-0 before the auth reply, then answers each exec
/// with `reply`. Records every body it receives.
async fn rcon_server(
    password: &'static str,
    reply: Vec<u8>,
) -> (SocketAddr, Arc<Mutex<Vec<String>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let seen2 = Arc::clone(&seen);
    tokio::spawn(async move {
        loop {
            let Ok((mut s, _)) = listener.accept().await else {
                return;
            };
            let seen = Arc::clone(&seen2);
            let reply = reply.clone();
            tokio::spawn(async move {
                let Some((id, ty, body)) = read_packet(&mut s).await else {
                    return;
                };
                assert_eq!(ty, 3);
                let ok = body == password.as_bytes();
                let _ = s.write_all(&encode_packet(id, 0, b"")).await;
                let auth_id = if ok { id } else { -1 };
                let _ = s.write_all(&encode_packet(auth_id, 2, b"")).await;
                if !ok {
                    return;
                }
                while let Some((id, ty, body)) = read_packet(&mut s).await {
                    if ty == 2 {
                        seen.lock()
                            .unwrap()
                            .push(String::from_utf8_lossy(&body).into_owned());
                        let _ = s.write_all(&encode_packet(id, 0, &reply)).await;
                    }
                }
            });
        }
    });
    (addr, seen)
}

fn exec(id: i32, cmd: &str) -> Vec<u8> {
    encode_packet(id, 2, cmd.as_bytes())
}

#[tokio::test]
async fn rcon_round_trip_host_logs_in_and_guest_never_sees_the_password() {
    let (addr, seen) = rcon_server(PASSWORD, b"hostname: demo".to_vec()).await;
    let (net, audit) = net_with(addr, PASSWORD, cfg());
    let mut s = net
        .rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
        .await
        .unwrap();
    s.write_all(&exec(9, "status")).await.unwrap();
    let resp = s.read(4096).await.unwrap();
    assert!(String::from_utf8_lossy(&resp).contains("hostname: demo"));
    assert_eq!(seen.lock().unwrap().as_slice(), ["status"]);
    drop(s);

    let events = audit.0.lock().unwrap();
    let rendered = format!("{events:?}");
    assert!(
        !rendered.contains(PASSWORD),
        "password in audit: {rendered}"
    );
    assert!(!rendered.contains("127.0.0.1"), "address in audit");
    assert!(events.iter().any(|e| matches!(e,
        AuditEvent::Command { verb, len, outcome, server, .. }
            if verb == "status" && *len == 6 && outcome == "ok" && server == SERVER)));
    assert!(events.iter().any(
        |e| matches!(e, AuditEvent::Open { op: "rcon_open_source", outcome, .. } if outcome == "ok")
    ));
    assert!(events
        .iter()
        .any(|e| matches!(e, AuditEvent::Close { bytes_out, .. } if *bytes_out > 0)));
    // The response bytes the guest received never contain the password either.
    assert!(!String::from_utf8_lossy(&resp).contains(PASSWORD));
}

#[tokio::test]
async fn wrong_password_is_auth_failed_without_leaking_it() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let (net, audit) = net_with(addr, "wrong-password", cfg());
    let err = net
        .rcon_open(&SCOPE, SERVER, Scheme::MinecraftRcon)
        .await
        .err()
        .unwrap();
    assert_eq!(err, NetError::AuthFailed);
    let rendered = format!("{err:?}{err}{:?}", audit.0.lock().unwrap());
    assert!(!rendered.contains("wrong-password"));
}

#[tokio::test]
async fn unregistered_wrong_scope_and_noncanonical_server_ids_are_denied() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let (net, _) = net_with(addr, PASSWORD, cfg());
    let other_community = Scope {
        tenant_id: 7,
        community_id: 4,
    };
    let other_tenant = Scope {
        tenant_id: 8,
        community_id: 3,
    };
    for (scope, server) in [
        (other_community, SERVER),
        (other_tenant, SERVER),
        (SCOPE, "00000000-0000-0000-0000-000000000000"),
        (SCOPE, "127.0.0.1:25575"),
    ] {
        let err = net
            .rcon_open(&scope, server, Scheme::SourceRcon)
            .await
            .err()
            .unwrap();
        assert_eq!(
            err,
            NetError::Denied("not_registered"),
            "{scope:?} {server}"
        );
    }
}

#[tokio::test]
async fn host_rejects_smuggled_commands_oversize_and_reauth_on_the_wire() {
    let (addr, seen) = rcon_server(PASSWORD, vec![]).await;
    let (net, audit) = net_with(addr, PASSWORD, cfg());
    let mut s = net
        .rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
        .await
        .unwrap();
    for bad in [
        exec(1, "say hi\nstop"),
        exec(1, &"a".repeat(513)),
        encode_packet(1, 3, b"newpass"),
        vec![1, 2, 3],
    ] {
        assert_eq!(
            s.write_all(&bad).await.unwrap_err(),
            NetError::Denied("command_rejected")
        );
    }
    assert!(
        seen.lock().unwrap().is_empty(),
        "nothing reached the server"
    );
    assert!(audit.0.lock().unwrap().iter().any(|e| matches!(e,
        AuditEvent::Command { outcome, .. } if outcome == "denied:command_rejected")));
}

// regression: the audit log must never carry raw command text (passwords / PII in arguments)
#[tokio::test]
async fn audit_never_records_raw_command_arguments() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let c = GameNetConfig {
        rcon_max_out: 4096,
        ..cfg()
    };
    let (net, audit) = net_with(addr, PASSWORD, c);
    let mut s = net
        .rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
        .await
        .unwrap();
    for cmd in [
        "rcon_password hunter2-new",
        "sv_password lobbyp4ss",
        "say my pin is 424242",
        "ban someone@example.com",
        "frobnicate secret-argument",
    ] {
        s.write_all(&exec(1, cmd)).await.unwrap();
    }
    drop(s);
    let events = audit.0.lock().unwrap();
    let rendered = format!("{events:?}");
    for leak in [
        "hunter2-new",
        "lobbyp4ss",
        "424242",
        "someone@example.com",
        "secret-argument",
    ] {
        assert!(
            !rendered.contains(leak),
            "{leak} leaked into audit: {rendered}"
        );
    }
    let verbs: Vec<(&str, &str)> = events
        .iter()
        .filter_map(|e| match e {
            AuditEvent::Command { verb, digest, .. } => Some((verb.as_str(), digest.as_str())),
            _ => None,
        })
        .collect();
    assert_eq!(verbs.len(), 5);
    assert_eq!(verbs[0], ("rcon_password", "-"));
    assert_eq!(verbs[1], ("sv_password", "-"));
    assert_eq!(verbs[2].0, "say");
    assert_eq!(verbs[3].0, "ban");
    assert_eq!(verbs[4].0, "other");
    assert!(verbs[2..].iter().all(|(_, d)| d.len() == 16));
}

#[tokio::test]
async fn byte_budgets_in_and_out_are_enforced() {
    let big = vec![b'x'; 3000];
    let (addr, _) = rcon_server(PASSWORD, big).await;
    let c = GameNetConfig {
        rcon_max_in: 2000,
        rcon_max_out: 40,
        ..cfg()
    };
    let (net, _) = net_with(addr, PASSWORD, c);
    let mut s = net
        .rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
        .await
        .unwrap();
    s.write_all(&exec(1, "status")).await.unwrap(); // 20 bytes
    s.write_all(&exec(2, "status")).await.unwrap(); // 40 total
    assert_eq!(
        s.write_all(&exec(3, "x")).await.unwrap_err(),
        NetError::TooLarge
    );
    let mut total = 0;
    let mut hit = false;
    for _ in 0..8 {
        match s.read(1500).await {
            Ok(b) => total += b.len(),
            Err(NetError::TooLarge) => {
                hit = true;
                break;
            }
            Err(NetError::Timeout) => break,
            Err(e) => panic!("{e:?}"),
        }
    }
    assert!(hit, "in-budget must trip (read {total})");
    assert!(total <= 2000, "never more than the budget: {total}");
}

#[tokio::test]
async fn rate_limit_per_connection_and_per_community() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let c = GameNetConfig {
        per_connection_per_min: 2,
        ..cfg()
    };
    let (net, _) = net_with(addr, PASSWORD, c);
    for _ in 0..2 {
        net.rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
            .await
            .unwrap();
    }
    let err = net
        .rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
        .await
        .err()
        .unwrap();
    assert_eq!(err, NetError::Denied("rate_limited"));

    let c = GameNetConfig {
        per_community_per_min: 1,
        ..cfg()
    };
    let (net, _) = net_with(addr, PASSWORD, c);
    net.tcp_open(&SCOPE, SERVER).await.unwrap();
    assert_eq!(
        net.tcp_open(&SCOPE, SERVER).await.err().unwrap(),
        NetError::Denied("rate_limited")
    );
}

#[tokio::test]
async fn concurrent_session_cap_frees_on_drop() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let c = GameNetConfig {
        max_concurrent_per_community: 1,
        ..cfg()
    };
    let (net, _) = net_with(addr, PASSWORD, c);
    let first = net.tcp_open(&SCOPE, SERVER).await.unwrap();
    assert_eq!(
        net.tcp_open(&SCOPE, SERVER).await.err().unwrap(),
        NetError::Denied("too_many_connections")
    );
    drop(first);
    net.tcp_open(&SCOPE, SERVER).await.unwrap();
}

#[tokio::test]
async fn read_deadline_is_enforced() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let c = GameNetConfig {
        read_timeout: Duration::from_millis(150),
        ..cfg()
    };
    let (net, _) = net_with(addr, PASSWORD, c);
    let mut s = net
        .rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
        .await
        .unwrap();
    assert_eq!(s.read(64).await.unwrap_err(), NetError::Timeout);
}

#[tokio::test]
async fn unreachable_port_is_unreachable_not_a_hang() {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    drop(l);
    let (net, _) = net_with(addr, PASSWORD, cfg());
    assert_eq!(
        net.tcp_open(&SCOPE, SERVER).await.err().unwrap(),
        NetError::Unreachable
    );
}

#[tokio::test]
async fn udp_exchange_round_trip_and_caps() {
    let sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let addr = sock.local_addr().unwrap();
    tokio::spawn(async move {
        let mut buf = [0u8; 2048];
        while let Ok((n, peer)) = sock.recv_from(&mut buf).await {
            let reply: Vec<u8> = if buf[..n].starts_with(b"big") {
                vec![7u8; 1000]
            } else {
                b"info-reply".to_vec()
            };
            let _ = sock.send_to(&reply, peer).await;
        }
    });
    let (net, _) = net_with(addr, PASSWORD, cfg());
    let out = net
        .udp_exchange(&SCOPE, SERVER, b"hello", 1400)
        .await
        .unwrap();
    assert_eq!(out, b"info-reply");
    // response larger than the guest's max-response: TooLarge, never truncated silently
    assert_eq!(
        net.udp_exchange(&SCOPE, SERVER, b"big", 100)
            .await
            .unwrap_err(),
        NetError::TooLarge
    );
    // guest cannot raise the ceiling or send an oversized/empty request
    assert_eq!(
        net.udp_exchange(&SCOPE, SERVER, b"hello", 1401)
            .await
            .unwrap_err(),
        NetError::TooLarge
    );
    assert_eq!(
        net.udp_exchange(&SCOPE, SERVER, &[0u8; 513], 100)
            .await
            .unwrap_err(),
        NetError::TooLarge
    );
    assert_eq!(
        net.udp_exchange(&SCOPE, SERVER, b"", 100)
            .await
            .unwrap_err(),
        NetError::TooLarge
    );
}

#[tokio::test]
async fn udp_timeout_when_no_reply() {
    let sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let addr = sock.local_addr().unwrap();
    let c = GameNetConfig {
        read_timeout: Duration::from_millis(150),
        ..cfg()
    };
    let (net, _) = net_with(addr, PASSWORD, c);
    assert_eq!(
        net.udp_exchange(&SCOPE, SERVER, b"x", 100)
            .await
            .unwrap_err(),
        NetError::Timeout
    );
    drop(sock);
}

#[tokio::test]
async fn tcp_session_reads_until_peer_closes() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (mut s, _) = listener.accept().await.unwrap();
        let mut b = [0u8; 16];
        let n = s.read(&mut b).await.unwrap();
        s.write_all(&b[..n]).await.unwrap();
    });
    let (net, _) = net_with(addr, PASSWORD, cfg());
    let mut s = net.tcp_open(&SCOPE, SERVER).await.unwrap();
    s.write_all(b"ping").await.unwrap();
    assert_eq!(s.read(16).await.unwrap(), b"ping");
    assert!(s.read(16).await.unwrap().is_empty(), "empty = peer closed");
}

#[tokio::test]
async fn rcon_without_a_stored_password_is_refused() {
    let (addr, _) = rcon_server(PASSWORD, vec![]).await;
    let (net, _) = net_with(addr, "", cfg());
    assert_eq!(
        net.rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
            .await
            .err()
            .unwrap(),
        NetError::Denied("no_password")
    );
}

//! Normal-mode posture: with the DEFAULT config (and in a release build, where the
//! `test-loopback` feature does not exist) every non-public target class is refused before any
//! socket is opened -- including a live loopback listener that WOULD accept.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::{Arc, Mutex};

use bundle_host_gameserver_net::directory::{DirFuture, ServerDirectory, ServerTarget};
use bundle_host_gameserver_net::{
    AuditEvent, AuditSink, GameNet, GameNetConfig, NetError, Scheme, Scope, Secret,
};
use tokio::net::TcpListener;

const SERVER: &str = "123e4567-e89b-12d3-a456-426614174000";
const SCOPE: Scope = Scope {
    tenant_id: 1,
    community_id: 1,
};

struct Fixed(String, u16);

impl ServerDirectory for Fixed {
    fn lookup<'a>(
        &'a self,
        _scope: &'a Scope,
        _server: &'a str,
    ) -> DirFuture<'a, Result<ServerTarget, NetError>> {
        Box::pin(async move {
            Ok(ServerTarget {
                server_id: SERVER.to_string(),
                host: self.0.clone(),
                port: self.1,
                password: Secret::new("pw-must-not-leak".into()),
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

fn net(host: &str, port: u16) -> (GameNet, Arc<Capture>) {
    let audit = Arc::new(Capture::default());
    (
        GameNet::new(
            Arc::new(Fixed(host.to_string(), port)),
            Arc::clone(&audit) as Arc<dyn AuditSink>,
            GameNetConfig::default(),
        ),
        audit,
    )
}

#[tokio::test]
async fn every_private_class_is_rejected_for_all_three_ops() {
    for host in [
        "10.0.0.5",
        "172.16.0.1",
        "192.168.0.10",
        "127.0.0.1",
        "169.254.169.254",
        "169.254.10.10",
        "100.64.0.9",
        "224.0.0.251",
        "0.0.0.0",
        "::1",
        "fd00::5",
        "fe80::1",
        "ff02::1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
    ] {
        let (n, audit) = net(host, 25575);
        let want = NetError::Denied("forbidden_address");
        assert_eq!(
            n.rcon_open(&SCOPE, SERVER, Scheme::SourceRcon)
                .await
                .err()
                .unwrap(),
            want,
            "rcon {host}"
        );
        assert_eq!(
            n.tcp_open(&SCOPE, SERVER).await.err().unwrap(),
            want,
            "tcp {host}"
        );
        assert_eq!(
            n.udp_exchange(&SCOPE, SERVER, b"x", 100).await.unwrap_err(),
            want,
            "udp {host}"
        );
        let rendered = format!("{:?}", audit.0.lock().unwrap());
        assert!(!rendered.contains("pw-must-not-leak"));
        assert!(
            !rendered.contains(host),
            "address leaked into audit for {host}"
        );
    }
}

#[tokio::test]
async fn a_live_loopback_listener_is_never_dialed_in_normal_mode() {
    let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = l.local_addr().unwrap().port();
    let accepted = tokio::spawn(async move {
        tokio::time::timeout(std::time::Duration::from_millis(400), l.accept())
            .await
            .is_ok()
    });
    let (n, _) = net("127.0.0.1", port);
    assert_eq!(
        n.tcp_open(&SCOPE, SERVER).await.err().unwrap(),
        NetError::Denied("forbidden_address")
    );
    assert!(
        !accepted.await.unwrap(),
        "no connection may reach the listener"
    );
}

#[tokio::test]
async fn malformed_registered_hosts_are_refused() {
    for host in ["", "evil.example/../x", "a b", "user@host", "x\n"] {
        let (n, _) = net(host, 25575);
        assert_eq!(
            n.tcp_open(&SCOPE, SERVER).await.err().unwrap(),
            NetError::Denied("invalid_target"),
            "{host:?}"
        );
    }
}

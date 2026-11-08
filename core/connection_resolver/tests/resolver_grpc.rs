//! Real-path behaviour of `ConnectionResolver` over gRPC: a real tonic hub on loopback, a real
//! (rcgen-issued) TLS handshake, a fake token endpoint. Covers one-call-per-resolve, bound
//! bearer tokens, error mapping end to end, the breaker, pagination, metrics, the long-lived
//! channel surviving a hub restart, and TLS trust pinning.

mod common;

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use common::{cfg, mints, oauth_reply, start, start_at, token_server, Hub, Rec};
use connection_resolver::{
    ConnectionRef, ConnectionResolve, ConnectionResolver, ConnectionSecret, NotUsableReason,
    ResolveError, ResolverConfig,
};
use hub_client::pb::{ConnectionBinding, ListBindingsResponse};
use tonic::Status;

const R: ConnectionRef = ConnectionRef {
    community_id: 7,
    connection_id: 12,
};

fn ok_hub() -> Hub {
    Hub::resolving(|_, req| Ok(oauth_reply(req.community_id, req.connection_id)))
}

#[tokio::test]
async fn two_resolves_make_two_hub_calls_and_one_token_mint() {
    let hub = ok_hub();
    let running = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    for _ in 0..2 {
        let c = r.resolve(R).await.unwrap();
        match &c.secret {
            ConnectionSecret::OAuthAccessToken { access_token } => {
                assert_eq!(access_token.expose(), "PLAIN-ACCESS");
            }
            other => panic!("unexpected secret {other:?}"),
        }
    }
    assert_eq!(
        hub.resolve_calls.load(Ordering::SeqCst),
        2,
        "no credential cache"
    );
    assert_eq!(mints(&tokens).await, 1, "only the bearer is cached");
}

#[tokio::test]
async fn resolve_sends_the_community_bound_bearer_as_grpc_metadata() {
    // The hub only answers a request whose bearer is the one minted for exactly that
    // (community, connection); anything else is UNAUTHENTICATED.
    let hub = Hub::resolving(|token, req| {
        if token != format!("tok-{}-{}", req.community_id, req.connection_id) {
            return Err(Status::unauthenticated("unauthorized"));
        }
        Ok(oauth_reply(req.community_id, req.connection_id))
    });
    let running = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    assert!(r.resolve(R).await.is_ok());
    let other = ConnectionRef {
        community_id: 8,
        connection_id: 13,
    };
    assert!(r.resolve(other).await.is_ok());
    assert_eq!(
        mints(&tokens).await,
        2,
        "one bound token per (community, connection)"
    );
}

#[tokio::test]
async fn a_reply_for_another_connection_is_refused() {
    // A hub bug (or a swapped reply) must never hand community 7 another community's secret.
    let hub = Hub::resolving(|_, _| Ok(oauth_reply(8, 12)));
    let running = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    assert_eq!(
        r.resolve(R).await.unwrap_err(),
        ResolveError::Malformed("resolve response is for another connection")
    );
}

#[tokio::test]
async fn statuses_map_to_the_error_taxonomy_over_the_wire() {
    let cases: Vec<(Status, ResolveError)> = vec![
        (
            Status::not_found("connection not found"),
            ResolveError::NotFound,
        ),
        (
            Status::failed_precondition("not_usable:disabled"),
            ResolveError::NotUsable(NotUsableReason::Disabled),
        ),
        (Status::permission_denied("forbidden"), ResolveError::Auth),
        (Status::unauthenticated("unauthorized"), ResolveError::Auth),
        (
            Status::unavailable("connections temporarily unavailable"),
            ResolveError::Transient("hub server error"),
        ),
        (
            Status::invalid_argument("bad"),
            ResolveError::Malformed("unexpected status"),
        ),
    ];
    for (status, want) in cases {
        let code = status.code();
        let hub = Hub::resolving(move |_, _| Err(status.clone()));
        let running = start(&hub).await;
        let tokens = token_server().await;
        let r = ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default()))
            .unwrap();
        assert_eq!(r.resolve(R).await.unwrap_err(), want, "{code:?}");
    }
}

#[tokio::test]
async fn slow_and_unreachable_hubs_are_transient_and_bounded() {
    let mut hub = ok_hub();
    hub.delay = Duration::from_secs(2);
    let running = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    let started = std::time::Instant::now();
    let e = r.resolve(R).await.unwrap_err();
    assert_eq!(e, ResolveError::Transient("hub rate limited or timed out"));
    assert!(e.is_transient());
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "per-RPC deadline bounds the call"
    );

    // Nothing listens on this port: the lazy channel's connect failure is transient too.
    let dead =
        ConnectionResolver::new(cfg("http://127.0.0.1:9", &tokens), Arc::new(Rec::default()))
            .unwrap();
    assert!(matches!(
        dead.resolve(R).await,
        Err(ResolveError::Transient(_))
    ));
}

#[tokio::test]
async fn token_failures_map_to_auth_or_transient() {
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};
    let hub = ok_hub();
    let running = start(&hub).await;

    // Token endpoint rejects the bootstrap -> Auth (on both resolve and list).
    let rejecting = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/service-token"))
        .respond_with(ResponseTemplate::new(401))
        .mount(&rejecting)
        .await;
    let r = ConnectionResolver::new(cfg(&running.endpoint, &rejecting), Arc::new(Rec::default()))
        .unwrap();
    assert_eq!(r.resolve(R).await.unwrap_err(), ResolveError::Auth);
    assert_eq!(
        r.list_bindings("twitch", None, 0, 10).await.unwrap_err(),
        ResolveError::Auth
    );
    assert_eq!(
        hub.resolve_calls.load(Ordering::SeqCst),
        0,
        "no token, no RPC"
    );

    let tokens = token_server().await;
    // Token endpoint unreachable -> Transient.
    let mut c = cfg(&running.endpoint, &tokens);
    c.token_endpoint = "http://127.0.0.1:1/internal/service-token".to_string();
    let r2 = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
    assert_eq!(
        r2.resolve(R).await.unwrap_err(),
        ResolveError::Transient("token endpoint unreachable")
    );

    // Unreadable SA token file -> Transient.
    let mut c = cfg(&running.endpoint, &tokens);
    c.sa_token_path = "/nonexistent/sa-token".to_string();
    let r3 = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
    assert_eq!(
        r3.resolve(R).await.unwrap_err(),
        ResolveError::Transient("service account token unreadable")
    );
}

#[tokio::test]
async fn auth_and_notfound_never_trip_the_breaker() {
    for status in [Status::unauthenticated("x"), Status::not_found("x")] {
        let hub = Hub::resolving(move |_, _| Err(status.clone()));
        let running = start(&hub).await;
        let tokens = token_server().await;
        let mut c = cfg(&running.endpoint, &tokens);
        c.breaker_threshold = 2;
        let r = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
        for _ in 0..6 {
            let e = r.resolve(R).await.unwrap_err();
            assert!(!e.is_transient(), "{e:?}");
            assert_ne!(e, ResolveError::CircuitOpen, "breaker must stay neutral");
        }
        assert_eq!(hub.resolve_calls.load(Ordering::SeqCst), 6);
    }
}

#[tokio::test]
async fn breaker_sheds_after_threshold_without_hitting_the_hub() {
    let hub = Hub::resolving(|_, _| Err(Status::internal("boom")));
    let running = start(&hub).await;
    let tokens = token_server().await;
    let mut c = cfg(&running.endpoint, &tokens);
    c.breaker_threshold = 2;
    c.breaker_cooldown = Duration::from_secs(60);
    let rec = Arc::new(Rec::default());
    let r = ConnectionResolver::new(c, rec.clone()).unwrap();
    for _ in 0..2 {
        assert!(matches!(
            r.resolve(R).await,
            Err(ResolveError::Transient(_))
        ));
    }
    for _ in 0..3 {
        let e = r.resolve(R).await.unwrap_err();
        assert_eq!(e, ResolveError::CircuitOpen);
        assert!(e.is_transient());
    }
    assert_eq!(
        r.list_bindings("twitch", None, 0, 1).await.unwrap_err(),
        ResolveError::CircuitOpen
    );
    assert_eq!(
        hub.resolve_calls.load(Ordering::SeqCst),
        2,
        "open breaker must not call the hub"
    );
    let calls = rec.calls.lock().unwrap().clone();
    assert!(calls.contains(&"resolve_failed:circuit_open".to_string()));
    assert!(calls.contains(&"list:circuit_open".to_string()));
}

fn binding(community: i64) -> ConnectionBinding {
    ConnectionBinding {
        community_id: community,
        connection_id: community,
        tenant_id: 3,
        secret_version: 1,
        provider: "twitch".into(),
        kind: 1,
        metadata: [("channel".to_owned(), "x".to_owned())].into(),
        ..Default::default()
    }
}

#[tokio::test]
async fn list_bindings_uses_the_unbound_list_token_and_paginates() {
    let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
    let seen_in_hub = seen.clone();
    let hub = Hub::new(
        Arc::new(|_, _| Err(Status::unimplemented("resolve not scripted"))),
        Arc::new(move |token, req| {
            seen_in_hub.lock().unwrap().push((
                token.to_string(),
                req.provider.clone(),
                req.capability.clone(),
                req.after,
                req.limit,
            ));
            Ok(match req.after {
                0 => ListBindingsResponse {
                    items: vec![binding(1)],
                    next_after: Some(1),
                },
                _ => ListBindingsResponse {
                    items: vec![binding(2)],
                    next_after: None,
                },
            })
        }),
    );
    let running = start(&hub).await;
    let tokens = token_server().await;
    let rec = Arc::new(Rec::default());
    let r = ConnectionResolver::new(cfg(&running.endpoint, &tokens), rec.clone()).unwrap();
    let p1 = r
        .list_bindings("twitch", Some("chat.send"), 0, 1)
        .await
        .unwrap();
    assert_eq!((p1.next_after, p1.items[0].community_id), (Some(1), 1));
    assert_eq!(
        p1.items[0].metadata.get("channel").map(String::as_str),
        Some("x")
    );
    let p2 = r
        .list_bindings("twitch", Some("chat.send"), 1, 1)
        .await
        .unwrap();
    assert_eq!((p2.next_after, p2.items[0].community_id), (None, 2));
    assert_eq!(
        *seen.lock().unwrap(),
        vec![
            ("list-tok".into(), "twitch".into(), "chat.send".into(), 0, 1),
            ("list-tok".into(), "twitch".into(), "chat.send".into(), 1, 1),
        ]
    );
    // The list token request carried no binding fields (unbound scope), minted once.
    let mints_seen = tokens.received_requests().await.unwrap();
    assert_eq!(mints_seen.len(), 1);
    let body = String::from_utf8_lossy(&mints_seen[0].body).to_string();
    assert!(
        !body.contains("community_id") && !body.contains("connection_id"),
        "{body}"
    );
    assert_eq!(
        rec.calls
            .lock()
            .unwrap()
            .iter()
            .filter(|c| *c == "list:ok")
            .count(),
        2
    );
}

#[tokio::test]
async fn list_without_capability_sends_an_empty_filter_and_malformed_pages_fail_closed() {
    let hub = Hub::new(
        Arc::new(|_, _| Err(Status::unimplemented("x"))),
        Arc::new(|_, req| {
            assert_eq!(req.capability, "", "no capability filter on the wire");
            // A binding without a server-derived tenant must fail the whole page closed.
            Ok(ListBindingsResponse {
                items: vec![ConnectionBinding {
                    tenant_id: 0,
                    ..binding(1)
                }],
                next_after: None,
            })
        }),
    );
    let running = start(&hub).await;
    let tokens = token_server().await;
    let rec = Arc::new(Rec::default());
    let r = ConnectionResolver::new(cfg(&running.endpoint, &tokens), rec.clone()).unwrap();
    assert_eq!(
        r.list_bindings("x", None, 0, 5).await.unwrap_err(),
        ResolveError::Malformed("binding tenantId must be >= 1")
    );
    assert_eq!(rec.calls.lock().unwrap().as_slice(), ["list:malformed"]);
}

#[tokio::test]
async fn errors_never_contain_server_text() {
    for status in [
        Status::internal("SECRET-LOOKING-sk-live-abc123 password=hunter2"),
        Status::invalid_argument("SECRET-LOOKING-sk-live-abc123 password=hunter2"),
        Status::failed_precondition("SECRET-LOOKING-sk-live-abc123 password=hunter2"),
    ] {
        let hub = Hub::resolving(move |_, _| Err(status.clone()));
        let running = start(&hub).await;
        let tokens = token_server().await;
        let r = ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default()))
            .unwrap();
        let e = r.resolve(R).await.unwrap_err();
        let rendered = format!("{e} | {e:?}");
        assert!(
            !rendered.contains("SECRET") && !rendered.contains("hunter2"),
            "{rendered}"
        );
    }
}

#[tokio::test]
async fn metrics_are_emitted_once_per_resolve_without_secret_labels() {
    let hub = Hub::resolving(|_, req| {
        if req.connection_id == 13 {
            return Err(Status::not_found("x"));
        }
        Ok(oauth_reply(req.community_id, req.connection_id))
    });
    let running = start(&hub).await;
    let tokens = token_server().await;
    let rec = Arc::new(Rec::default());
    let r = ConnectionResolver::new(cfg(&running.endpoint, &tokens), rec.clone()).unwrap();
    r.resolve(R).await.unwrap();
    r.resolve(ConnectionRef {
        community_id: 7,
        connection_id: 13,
    })
    .await
    .unwrap_err();
    let calls = rec.calls.lock().unwrap().clone();
    assert_eq!(
        calls,
        vec![
            "resolve:1:ok".to_string(),
            "resolve_failed:not_found".to_string()
        ]
    );
    for c in &calls {
        assert!(!c.contains("PLAIN") && !c.contains("tok"), "{c}");
    }
}

#[tokio::test]
async fn trait_seam_delegates_to_the_real_client() {
    async fn through<T: ConnectionResolve>(t: &T) -> (bool, bool) {
        (
            t.resolve(R).await.is_ok(),
            t.list_bindings("p", None, 0, 1).await.is_err(),
        )
    }
    let hub = ok_hub();
    let running = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    assert_eq!(through(&r).await, (true, true));
    assert!(format!("{r:?}").contains("ConnectionResolver"));
}

#[tokio::test]
async fn concurrent_resolves_each_hit_the_hub_over_one_shared_channel() {
    let hub = ok_hub();
    let running = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&running.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    let (a, b, c) = tokio::join!(r.resolve(R), r.resolve(R), r.resolve(R));
    assert!(a.is_ok() && b.is_ok() && c.is_ok());
    assert_eq!(hub.resolve_calls.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn the_long_lived_channel_redials_after_a_hub_restart() {
    let hub = ok_hub();
    let first = start(&hub).await;
    let tokens = token_server().await;
    let r =
        ConnectionResolver::new(cfg(&first.endpoint, &tokens), Arc::new(Rec::default())).unwrap();
    assert!(r.resolve(R).await.is_ok());

    // Hub goes away: calls fail transiently (never hang, never panic).
    let addr = first.addr;
    first.stop().await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(matches!(
        r.resolve(R).await,
        Err(ResolveError::Transient(_))
    ));

    // Same address comes back: the SAME resolver (same channel) recovers without being rebuilt.
    let _second = start_at(&hub, addr, None).await;
    let mut recovered = false;
    for _ in 0..20 {
        if r.resolve(R).await.is_ok() {
            recovered = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    assert!(recovered, "channel must re-dial the restarted hub");
}

/// A throwaway CA + a localhost leaf signed by it (`rcgen`, generated per test -- no key
/// material is ever committed).
struct Pki {
    ca_pem: String,
    leaf_cert_pem: String,
    leaf_key_pem: String,
}

fn pki() -> Pki {
    let ca_key = rcgen::KeyPair::generate().unwrap();
    let ca_params = rcgen::CertificateParams::new(vec!["resolver-test-ca".to_string()]).unwrap();
    let ca_cert = ca_params.self_signed(&ca_key).unwrap();
    let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);
    let leaf_key = rcgen::KeyPair::generate().unwrap();
    let leaf_params = rcgen::CertificateParams::new(vec!["localhost".to_string()]).unwrap();
    let leaf = leaf_params.signed_by(&leaf_key, &issuer).unwrap();
    Pki {
        ca_pem: ca_cert.pem(),
        leaf_cert_pem: leaf.pem(),
        leaf_key_pem: leaf_key.serialize_pem(),
    }
}

fn write_pem(contents: &str, label: &str) -> String {
    let path =
        std::env::temp_dir().join(format!("conn-resolver-{}-{label}.pem", std::process::id()));
    std::fs::write(&path, contents).unwrap();
    path.to_string_lossy().to_string()
}

fn tls_config(running_port: u16, tokens: &wiremock::MockServer, ca_path: &str) -> ResolverConfig {
    let mut c = cfg(&format!("https://localhost:{running_port}"), tokens);
    c.environment = "beta".into(); // https is accepted in every environment
    c.with_grpc_ca(Some(ca_path.to_string()))
}

#[tokio::test]
async fn real_tls_handshake_trusts_the_pinned_ca_and_rejects_any_other() {
    let good = pki();
    let hub = ok_hub();
    let running = start_at(
        &hub,
        std::net::SocketAddr::from(([127, 0, 0, 1], free_port())),
        Some((good.leaf_cert_pem.clone(), good.leaf_key_pem.clone())),
    )
    .await;
    let tokens = token_server().await;

    // Pinned to the CA that signed the server cert: the call succeeds over TLS.
    let ca_path = write_pem(&good.ca_pem, "trusted-ca");
    let r = ConnectionResolver::new(
        tls_config(running.addr.port(), &tokens, &ca_path),
        Arc::new(Rec::default()),
    )
    .unwrap();
    assert!(
        r.resolve(R).await.is_ok(),
        "TLS to a server signed by the pinned CA works"
    );
    assert_eq!(hub.resolve_calls.load(Ordering::SeqCst), 1);

    // Pinned to a DIFFERENT CA: the handshake fails, the call never reaches the service.
    let other_ca = write_pem(&pki().ca_pem, "other-ca");
    let before = hub.resolve_calls.load(Ordering::SeqCst);
    let r2 = ConnectionResolver::new(
        tls_config(running.addr.port(), &tokens, &other_ca),
        Arc::new(Rec::default()),
    )
    .unwrap();
    assert!(matches!(
        r2.resolve(R).await,
        Err(ResolveError::Transient(_))
    ));
    assert_eq!(
        hub.resolve_calls.load(Ordering::SeqCst),
        before,
        "untrusted peer never served"
    );

    // Plaintext client against a TLS server also fails (no silent downgrade).
    let plain = ConnectionResolver::new(
        cfg(
            &format!("http://127.0.0.1:{}", running.addr.port()),
            &tokens,
        ),
        Arc::new(Rec::default()),
    )
    .unwrap();
    assert!(plain.resolve(R).await.is_err());
}

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

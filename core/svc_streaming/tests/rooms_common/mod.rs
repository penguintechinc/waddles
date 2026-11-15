//! Shared gRPC harness for `tests/rooms_*.rs` -- not itself a test binary (a `mod.rs`
//! under a subdirectory is cargo's standard way to share code between integration-test
//! crates). The `RoomsService` runs in-process behind the production mTLS accept loop
//! ([`svc_streaming::rooms::grpc::serve`]) and is driven through the generated tonic
//! client over a real rustls handshake, with real ES256 machine JWTs.
//!
//! `dead_code` is allowed: each test binary uses a subset of these helpers.
#![allow(dead_code)]

use std::sync::Arc;

use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde_json::json;
use svc_streaming::rooms::grpc::pb::rooms_service_client::RoomsServiceClient;
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc::{build_grpc_server_config, serve};
use svc_streaming::rooms::grpc_auth::ServiceJwtVerifier;
use svc_streaming::rooms::host_api::HostApiTls;
use svc_streaming::rooms::token::TokenKeys;
use svc_streaming::rooms::{IdlePolicy, RoomService};
use svc_streaming::rtc::config::RtcConfig;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Identity};
use tonic::Request;

pub const SUBJECT: &str = "spiffe://penguintech.io/test/hub-api";
pub const KID: &str = "k1";

/// The ES256 room-token key pair of this test binary (throwaway, generated once per process).
pub fn test_token_keys() -> TokenKeys {
    static PEM: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    let pem = PEM.get_or_init(|| rcgen::KeyPair::generate().unwrap().serialize_pem());
    TokenKeys::from_ec_pem("room-test", pem).unwrap()
}

/// A running gRPC server plus a connected mTLS client and the signing half of hub-api's key.
pub struct Harness {
    pub client: RoomsServiceClient<Channel>,
    pub signing_pem: String,
    pub registry: prometheus::Registry,
    pub port: u16,
    pub ca_pem: String,
    pub svc: Arc<RoomService>,
    /// The client certificate/key (PEM) the server trusts, for extra connections.
    pub client_identity: (String, String),
    _dir: std::path::PathBuf,
}

/// Knobs for [`harness_with`].
#[derive(Clone)]
pub struct Opts {
    pub moderation: bool,
    pub idle: Option<IdlePolicy>,
    /// `false` leaves `WEBRTC_ROOM_TOKEN_SIGNING_KEY` unset (health must then be NOT_SERVING).
    pub token_secret: bool,
}

impl Default for Opts {
    fn default() -> Self {
        Self {
            moderation: true,
            idle: None,
            token_secret: true,
        }
    }
}

fn ca() -> (rcgen::CertificateParams, rcgen::KeyPair, rcgen::Certificate) {
    let key = rcgen::KeyPair::generate().unwrap();
    let mut params = rcgen::CertificateParams::new(vec!["test-ca".to_string()]).unwrap();
    params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let cert = params.self_signed(&key).unwrap();
    (params, key, cert)
}

/// Starts the gRPC server (mTLS + JWT) with the moderation flag as given.
pub async fn harness(udp_base: u16, moderation: bool) -> Harness {
    harness_with(
        udp_base,
        Opts {
            moderation,
            ..Opts::default()
        },
    )
    .await
}

/// Starts the gRPC server (mTLS + JWT) and returns a connected client.
pub async fn harness_with(udp_base: u16, opts: Opts) -> Harness {
    let dir = std::env::temp_dir().join(format!("svc-streaming-grpc-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let w = |name: &str, body: &str| {
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        p
    };

    let (ca_params, ca_key, ca_cert) = ca();
    let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);
    let server_key = rcgen::KeyPair::generate().unwrap();
    let server_cert = rcgen::CertificateParams::new(vec!["localhost".to_string()])
        .unwrap()
        .signed_by(&server_key, &issuer)
        .unwrap();
    let client_key = rcgen::KeyPair::generate().unwrap();
    let client_cert = rcgen::CertificateParams::new(vec!["hub-api".to_string()])
        .unwrap()
        .signed_by(&client_key, &issuer)
        .unwrap();
    let tls = HostApiTls {
        server_cert: w("server.pem", &server_cert.pem()),
        server_key: w("server.key", &server_key.serialize_pem()),
        client_ca: w("ca.pem", &ca_cert.pem()),
        port: 0,
    };
    let server_config = build_grpc_server_config(&tls).expect("server config");

    // ES256 signing key for hub-api; the server only ever sees the public half.
    let signing = rcgen::KeyPair::generate().unwrap();
    let keys =
        ServiceJwtVerifier::keys_from_pem([(KID, signing.public_key_pem().as_str())]).unwrap();
    let auth = Arc::new(ServiceJwtVerifier::new(
        keys,
        "waddlebot-internal",
        vec!["hub-api".to_string()],
        vec![SUBJECT.to_string()],
    ));

    let registry = prometheus::Registry::new();
    let factory = Arc::new(
        PeerConnectionFactory::new(RtcConfig {
            bind_ip: "127.0.0.1".parse().unwrap(),
            udp_port_range: (udp_base, udp_base + 10),
            nat_1to1_ip: None,
        })
        .unwrap(),
    );
    let metrics = RtcMetrics::register(&registry).unwrap();
    let mut service = RoomService::new(
        factory,
        metrics,
        opts.token_secret.then(test_token_keys),
        "http://svc.test".into(),
    )
    .with_moderation(opts.moderation);
    if let Some(idle) = opts.idle {
        service = service.with_idle_policy(idle);
    }
    let rooms = Arc::new(service);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(serve(listener, server_config, rooms.clone(), auth));

    let client_identity = (client_cert.pem(), client_key.serialize_pem());
    let client = connect(port, &ca_cert.pem(), Some(client_identity.clone()))
        .await
        .expect("mTLS client connects");
    Harness {
        client,
        signing_pem: signing.serialize_pem(),
        registry,
        port,
        ca_pem: ca_cert.pem(),
        svc: rooms,
        client_identity,
        _dir: dir,
    }
}

/// Opens a tonic channel to the test server (optionally presenting a client certificate).
pub async fn connect(
    port: u16,
    ca_pem: &str,
    identity: Option<(String, String)>,
) -> Result<RoomsServiceClient<Channel>, tonic::transport::Error> {
    Ok(RoomsServiceClient::new(
        channel(port, ca_pem, identity).await?,
    ))
}

/// Like [`connect`] but returns the raw channel (for non-`RoomsService` clients, e.g. health).
pub async fn channel(
    port: u16,
    ca_pem: &str,
    identity: Option<(String, String)>,
) -> Result<Channel, tonic::transport::Error> {
    let mut tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(ca_pem))
        .domain_name("localhost");
    if let Some((cert, key)) = identity {
        tls = tls.identity(Identity::from_pem(cert, key));
    }
    Channel::from_shared(format!("https://127.0.0.1:{port}"))
        .unwrap()
        .tls_config(tls)?
        .connect()
        .await
}

pub fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Mints a machine JWT like hub-api's issuer would.
pub fn jwt(pem: &str, scope: &str, mutate: impl FnOnce(&mut serde_json::Value)) -> String {
    let t = now();
    let mut claims = json!({
        "iss": "hub-api", "aud": "waddlebot-internal", "sub": SUBJECT, "scope": scope,
        "iat": t, "nbf": t, "exp": t + 60, "jti": uuid::Uuid::new_v4().to_string(),
    });
    mutate(&mut claims);
    let mut header = Header::new(Algorithm::ES256);
    header.kid = Some(KID.into());
    encode(
        &header,
        &claims,
        &EncodingKey::from_ec_pem(pem.as_bytes()).unwrap(),
    )
    .unwrap()
}

/// Wraps `msg` with the `authorization: Bearer <token>` metadata.
pub fn req<T>(msg: T, token: &str) -> Request<T> {
    let mut r = Request::new(msg);
    r.metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    r
}

/// A fresh admin actor (`actor_id`, `actor_role`).
pub fn admin() -> (String, i32) {
    (uuid::Uuid::new_v4().to_string(), RoomRole::Admin as i32)
}

/// A fresh random UUID string.
pub fn uid() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// Creates a room in `community` over gRPC and returns it.
pub async fn create_room(h: &mut Harness, community: &str, timeout: Option<u32>) -> Room {
    let write = jwt(
        &h.signing_pem,
        svc_streaming::rooms::grpc_auth::SCOPE_WRITE,
        |_| {},
    );
    h.client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: community.into(),
                empty_timeout_secs: timeout,
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap()
}

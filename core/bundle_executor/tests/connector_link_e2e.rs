//! Real-path e2e of the connector runtime executor side (PR-A1): boot pinning
//! of a REAL overlay component through a signed bucket source, the second dial
//! loop over REAL mutual TLS with SAN pinning in both directions, and the
//! host-derived `_scope` stamp on every guest host-call.
//!
//! The "svc-ingest" listener here is a test double (a rustls server whose
//! client verifier pins the executor SAN exactly like svc_ingest's
//! `host_api_listener`); everything on the executor side is the production
//! code path (`connector_boot::pin_configured`, `tls::dial_connector`,
//! `serve_connector_link_once`, `ConnectorLinkHandler`, wasmtime, the real
//! discord overlay).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::HashMap;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use bundle_executor::config::CliConfig;
use bundle_executor::connector_boot::{pin_configured, CONNECTOR_PLATFORMS};
use bundle_executor::error::ExecutorError;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::serve_connector_link_once;
use bundle_executor::signing::signing_payload;
use clap::Parser;
use ed25519_dalek::{Signer, SigningKey};
use penguin_bundle_host::wire::{
    read_frame, write_frame, ErrorCode, ExportKind, Frame, HelloLimits, HelloOkBody, HostCallBody,
    HostResultBody, HostResultError, InvokeBody, Message,
};
use rustls::server::danger::{ClientCertVerified, ClientCertVerifier};
use rustls::server::WebPkiClientVerifier;
use rustls::{DigitallySignedStruct, DistinguishedName, RootCertStore, SignatureScheme};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer, UnixTime};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio_rustls::TlsAcceptor;

const DISCORD_WASM: &[u8] = include_bytes!("fixtures/discord_overlay.wasm");
const DISCORD_APP: &str = "waddles.core.service.discord";
const SERVER_ID: &str = "spiffe://waddlebot/svc-ingest-hostapi";
const EXECUTOR_ID: &str = "spiffe://waddlebot/executor-connector";
const STAGE_ID: &str = "spiffe://waddlebot/svc-action";

fn digest_of(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

// ---------------------------------------------------------------- PKI

struct Ca {
    cert: rcgen::Certificate,
    issuer: rcgen::Issuer<'static, rcgen::KeyPair>,
}

fn ca() -> Ca {
    let mut p = rcgen::CertificateParams::new(Vec::<String>::new()).unwrap();
    p.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let k = rcgen::KeyPair::generate().unwrap();
    let cert = p.self_signed(&k).unwrap();
    Ca {
        cert,
        issuer: rcgen::Issuer::new(p, k),
    }
}

/// A leaf whose ONLY URI SAN is `uri` (plus `localhost` DNS for servers).
fn leaf(ca: &Ca, uri: &str, dns: Option<&str>) -> (String, String) {
    let names = dns.map(|d| vec![d.to_string()]).unwrap_or_default();
    let mut p = rcgen::CertificateParams::new(names).unwrap();
    p.subject_alt_names
        .push(rcgen::SanType::URI(uri.try_into().unwrap()));
    let k = rcgen::KeyPair::generate().unwrap();
    let c = p.signed_by(&k, &ca.issuer).unwrap();
    (c.pem(), k.serialize_pem())
}

struct Scratch(PathBuf);

impl Scratch {
    fn new(tag: &str) -> Self {
        let p = std::env::temp_dir().join(format!(
            "runtime-executor-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(&p).unwrap();
        Self(p)
    }

    fn write(&self, name: &str, text: &str) -> PathBuf {
        let p = self.0.join(name);
        std::fs::File::create(&p)
            .unwrap()
            .write_all(text.as_bytes())
            .unwrap();
        p
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Mirrors svc_ingest's `PinnedClientVerifier`: chain validation, then the
/// leaf's URI/DNS SAN must EXACTLY equal the pinned executor identity.
#[derive(Debug)]
struct SanPinningVerifier {
    inner: Arc<dyn ClientCertVerifier>,
    identity: String,
}

impl ClientCertVerifier for SanPinningVerifier {
    fn offer_client_auth(&self) -> bool {
        self.inner.offer_client_auth()
    }
    fn client_auth_mandatory(&self) -> bool {
        true
    }
    fn root_hint_subjects(&self) -> &[DistinguishedName] {
        self.inner.root_hint_subjects()
    }
    fn verify_client_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        intermediates: &[CertificateDer<'_>],
        now: UnixTime,
    ) -> Result<ClientCertVerified, rustls::Error> {
        self.inner
            .verify_client_cert(end_entity, intermediates, now)?;
        let (_, parsed) = x509_parser::parse_x509_certificate(end_entity.as_ref())
            .map_err(|_| rustls::Error::General("unparsable".into()))?;
        let sans = parsed
            .subject_alternative_name()
            .ok()
            .flatten()
            .map(|s| s.value.general_names.clone())
            .unwrap_or_default();
        let hit = sans.iter().any(|n| {
            matches!(n,
                x509_parser::extensions::GeneralName::URI(v)
                | x509_parser::extensions::GeneralName::DNSName(v)
                if *v == self.identity)
        });
        if hit {
            Ok(ClientCertVerified::assertion())
        } else {
            Err(rustls::Error::General("not the pinned executor".into()))
        }
    }
    fn verify_tls12_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls12_signature(m, c, d)
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &CertificateDer<'_>,
        d: &DigitallySignedStruct,
    ) -> Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls13_signature(m, c, d)
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}

fn acceptor(ca: &Ca, server_pem: &(String, String)) -> TlsAcceptor {
    let mut roots = RootCertStore::empty();
    roots.add(ca.cert.der().clone()).unwrap();
    let inner = WebPkiClientVerifier::builder(Arc::new(roots))
        .build()
        .unwrap();
    let verifier = Arc::new(SanPinningVerifier {
        inner,
        identity: EXECUTOR_ID.to_string(),
    });
    let chain: Vec<_> = CertificateDer::pem_slice_iter(server_pem.0.as_bytes())
        .map(Result::unwrap)
        .collect();
    let key = PrivateKeyDer::from_pem_slice(server_pem.1.as_bytes()).unwrap();
    let cfg = rustls::ServerConfig::builder()
        .with_client_cert_verifier(verifier)
        .with_single_cert(chain, key)
        .unwrap();
    TlsAcceptor::from(Arc::new(cfg))
}

// ------------------------------------------------- signed bucket double

struct MapSource(HashMap<String, Vec<u8>>);

impl ComponentSource for MapSource {
    async fn fetch(&self, c: &str, s: &str) -> Result<(Vec<u8>, Vec<u8>), ExecutorError> {
        let get = |k: &str| {
            self.0
                .get(k)
                .cloned()
                .ok_or_else(|| ExecutorError::BucketFetch(format!("no such object {k}")))
        };
        Ok((get(c)?, get(s)?))
    }
}

fn signing_key() -> SigningKey {
    SigningKey::from_bytes(&[9u8; 32])
}

fn keys_json(k: &SigningKey) -> String {
    serde_json::json!({ "k1": BASE64.encode(k.verifying_key().to_bytes()) }).to_string()
}

fn sidecar(k: &SigningKey, app: &str, digest: &str) -> Vec<u8> {
    let payload = signing_payload(app, "1.0.0", digest, 7).unwrap();
    serde_json::to_vec(&serde_json::json!({
        "app_id": app, "version": "1.0.0", "digest": digest, "approval_id": 7,
        "key_id": "k1", "algorithm": "ed25519",
        "signature": BASE64.encode(k.sign(&payload).to_bytes()),
    }))
    .unwrap()
}

fn bucket(wasm: &[u8], signed_by: &SigningKey, stored_digest: &str) -> MapSource {
    let claimed = digest_of(DISCORD_WASM);
    let mut m = HashMap::new();
    m.insert(format!("connectors/discord/{claimed}.wasm"), wasm.to_vec());
    m.insert(
        format!("connectors/discord/{claimed}.json"),
        sidecar(signed_by, DISCORD_APP, stored_digest),
    );
    MapSource(m)
}

fn base_args(extra: &[String]) -> Vec<String> {
    let mut a: Vec<String> = [
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-action:8302",
        "--bundle-signing-public-keys",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    a.push(keys_json(&signing_key()));
    a.extend_from_slice(extra);
    a
}

fn boot_cfg() -> CliConfig {
    let d = digest_of(DISCORD_WASM);
    CliConfig::try_parse_from(base_args(&["--connector-pin-digest-discord".into(), d])).unwrap()
}

// ------------------------------------------------------------ boot pin

#[tokio::test]
async fn boot_pins_the_real_overlay_through_the_signed_bucket() {
    let cfg = boot_cfg();
    let exec = Executor::new(
        &cfg,
        bucket(DISCORD_WASM, &signing_key(), &digest_of(DISCORD_WASM)),
    )
    .unwrap();
    let pinned = pin_configured(&exec, &cfg).await.unwrap();
    assert_eq!(pinned, 1, "denominator: exactly one overlay configured");
    assert_eq!(exec.pinned_connectors().await, 1);
}

#[tokio::test]
async fn boot_with_nothing_configured_pins_nothing() {
    let cfg = CliConfig::try_parse_from(base_args(&[])).unwrap();
    let exec = Executor::new(&cfg, MapSource(HashMap::new())).unwrap();
    assert_eq!(pin_configured(&exec, &cfg).await.unwrap(), 0);
}

async fn boot_error(cfg: &CliConfig, src: MapSource) -> String {
    let exec = Executor::new(cfg, src).unwrap();
    let err = pin_configured(&exec, cfg).await.unwrap_err().to_string();
    assert_eq!(exec.pinned_connectors().await, 0, "nothing may be pinned");
    err
}

#[tokio::test]
async fn digest_mismatch_aborts_boot_naming_the_platform() {
    // The bucket object at the configured digest holds DIFFERENT bytes.
    let other = [DISCORD_WASM, b"tamper"].concat();
    let err = boot_error(
        &boot_cfg(),
        bucket(&other, &signing_key(), &digest_of(DISCORD_WASM)),
    )
    .await;
    assert!(err.contains("discord"), "{err}");
    assert!(err.to_lowercase().contains("digest"), "{err}");
}

#[tokio::test]
async fn bad_signature_aborts_boot() {
    let rogue = SigningKey::from_bytes(&[3u8; 32]);
    let err = boot_error(
        &boot_cfg(),
        bucket(DISCORD_WASM, &rogue, &digest_of(DISCORD_WASM)),
    )
    .await;
    assert!(err.contains("discord"), "{err}");
    assert!(err.to_lowercase().contains("signature"), "{err}");
}

#[tokio::test]
async fn a_sidecar_signed_for_another_digest_aborts_boot() {
    let err = boot_error(
        &boot_cfg(),
        bucket(DISCORD_WASM, &signing_key(), &digest_of(b"something else")),
    )
    .await;
    assert!(err.contains("discord"), "{err}");
}

#[tokio::test]
async fn missing_object_aborts_boot() {
    let err = boot_error(&boot_cfg(), MapSource(HashMap::new())).await;
    assert!(err.contains("discord"), "{err}");
}

#[tokio::test]
async fn empty_signing_keys_abort_boot_never_pin_unverified() {
    let d = digest_of(DISCORD_WASM);
    let mut args = base_args(&["--connector-pin-digest-discord".into(), d.clone()]);
    // Blank out the key set.
    let i = args
        .iter()
        .position(|a| a == "--bundle-signing-public-keys")
        .unwrap();
    args[i + 1] = String::new();
    let cfg = CliConfig::try_parse_from(args).unwrap();
    let err = boot_error(&cfg, bucket(DISCORD_WASM, &signing_key(), &d)).await;
    assert!(
        err.contains("discord") && err.contains("BUNDLE_SIGNING_PUBLIC_KEYS"),
        "{err}"
    );
}

#[tokio::test]
async fn a_malformed_digest_aborts_boot_without_echoing_it() {
    let cfg = CliConfig::try_parse_from(base_args(&[
        "--connector-pin-digest-twitch".into(),
        "not-a-digest".into(),
    ]))
    .unwrap();
    let exec = Executor::new(&cfg, MapSource(HashMap::new())).unwrap();
    let err = pin_configured(&exec, &cfg).await.unwrap_err().to_string();
    assert!(err.contains("CONNECTOR_PIN_DIGEST_TWITCH"), "{err}");
    assert!(!err.contains("not-a-digest"), "{err}");
}

#[test]
fn four_connector_connections_is_refused_at_config_load() {
    let cfg = CliConfig::try_parse_from(base_args(&[
        "--executor-connector-connections".into(),
        "4".into(),
    ]))
    .unwrap();
    assert!(cfg.validate().is_err());
}

// ---------------------------------------------------------- the link

struct Rig {
    _dir: Scratch,
    cfg: CliConfig,
    exec: Arc<Executor<MapSource>>,
    listener: TcpListener,
}

/// Executor with the discord overlay pinned, plus certs on disk and a bound
/// listener port. `client_uri` is the SAN of the cert the executor presents.
async fn rig(tag: &str, ca: &Ca, client_uri: &str) -> Rig {
    let dir = Scratch::new(tag);
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (cc, ck) = leaf(ca, client_uri, None);
    let digest = digest_of(DISCORD_WASM);
    let args = base_args(&[
        "--connector-pin-digest-discord".into(),
        digest.clone(),
        "--connector-host-api-addr".into(),
        format!("localhost:{port}"),
        "--connector-host-api-identity".into(),
        SERVER_ID.into(),
        "--connector-host-api-client-cert-file".into(),
        dir.write("exec.pem", &cc).to_string_lossy().into_owned(),
        "--connector-host-api-client-key-file".into(),
        dir.write("exec.key", &ck).to_string_lossy().into_owned(),
        "--host-api-ca-file".into(),
        dir.write("ca.pem", &ca.cert.pem())
            .to_string_lossy()
            .into_owned(),
        "--executor-probe-file".into(),
        dir.0.join("probe").to_string_lossy().into_owned(),
    ]);
    let cfg = CliConfig::try_parse_from(args).unwrap();
    cfg.validate().unwrap();
    let exec =
        Arc::new(Executor::new(&cfg, bucket(DISCORD_WASM, &signing_key(), &digest)).unwrap());
    assert_eq!(pin_configured(&exec, &cfg).await.unwrap(), 1);
    Rig {
        _dir: dir,
        cfg,
        exec,
        listener,
    }
}

fn hello_ok() -> Message {
    Message::HelloOk(HelloOkBody {
        stage: "svc-ingest".into(),
        protocol_version: 1,
        limits: HelloLimits {
            call_timeout_ms: 2_000,
            memory_mb: 64,
            max_concurrent_calls: 8,
        },
    })
}

fn frame_payload(connection_id: &str) -> serde_json::Value {
    let frame = serde_json::json!({"op":0,"s":1,"t":"MESSAGE_CREATE","d":{
        "author":{"bot":false,"id":"444","username":"someuser"},
        "channel_id":"222","content":"hello chat","id":"333","type":0,"guild_id":"111"}});
    serde_json::json!({
        "ctx": {"platform":"discord","source_id":"src-1","connection_id":connection_id,
                "session_token": r#"{"self_user_id":"999"}"#},
        "bytes": BASE64.encode(serde_json::to_vec(&frame).unwrap()),
        "received_at": "2026-11-05T00:00:00.000Z",
    })
}

/// What the scripted listener saw.
#[derive(Default)]
struct Seen {
    host_calls: Vec<(u64, HostCallBody)>,
    replies: Vec<(u64, Result<serde_json::Value, ErrorCode>)>,
}

/// Accepts one executor over `acceptor`, completes the hello handshake, sends
/// each `invokes` entry in turn answering the guest's host-calls with a typed
/// denial, then hangs up.
async fn serve_script(
    listener: TcpListener,
    acceptor: TlsAcceptor,
    invokes: Vec<(ExportKind, serde_json::Value)>,
    digest: String,
) -> Result<Seen, String> {
    let (tcp, _) = listener.accept().await.map_err(|e| e.to_string())?;
    let mut tls = acceptor.accept(tcp).await.map_err(|e| e.to_string())?;
    let hello = read_frame(&mut tls).await.map_err(|e| e.to_string())?;
    assert!(matches!(hello.message, Message::Hello(_)));
    write_frame(&mut tls, &Frame::new(hello.id, hello_ok()))
        .await
        .map_err(|e| e.to_string())?;
    let mut seen = Seen::default();
    for (i, (export, payload)) in invokes.into_iter().enumerate() {
        let id = 100 + i as u64;
        write_frame(
            &mut tls,
            &Frame::new(
                id,
                Message::Invoke(InvokeBody {
                    app_id: DISCORD_APP.into(),
                    digest: digest.clone(),
                    export,
                    payload,
                    deadline_ms: 2_000,
                    trace: None,
                }),
            ),
        )
        .await
        .map_err(|e| e.to_string())?;
        loop {
            let f = tokio::time::timeout(Duration::from_secs(20), read_frame(&mut tls))
                .await
                .map_err(|_| "timed out".to_string())?
                .map_err(|e| e.to_string())?;
            match f.message {
                Message::HostCall(call) => {
                    seen.host_calls.push((id, call));
                    write_frame(
                        &mut tls,
                        &Frame::new(
                            f.id,
                            Message::HostResult(HostResultBody {
                                result: None,
                                error: Some(HostResultError {
                                    code: "HOST_CALL_DENIED".into(),
                                    message: "scripted".into(),
                                }),
                            }),
                        ),
                    )
                    .await
                    .map_err(|e| e.to_string())?;
                }
                Message::Result(r) => {
                    assert_eq!(f.id, id);
                    seen.replies.push((id, Ok(r.payload)));
                    break;
                }
                Message::Error(e) => {
                    assert_eq!(f.id, id);
                    seen.replies.push((id, Err(e.code)));
                    break;
                }
                other => return Err(format!("unexpected frame {other:?}")),
            }
        }
    }
    Ok(seen)
}

#[tokio::test(flavor = "multi_thread")]
async fn guest_host_calls_cross_the_pinned_mtls_link_stamped_with_the_host_scope() {
    let ca = ca();
    let server = leaf(&ca, SERVER_ID, Some("localhost"));
    let r = rig("happy", &ca, EXECUTOR_ID).await;
    let digest = digest_of(DISCORD_WASM);
    let acc = acceptor(&ca, &server);
    let listener = r.listener;
    let script = tokio::spawn(serve_script(
        listener,
        acc,
        vec![
            (ExportKind::ConnectorOnFrame, frame_payload("conn-42")),
            (ExportKind::ConnectorOnFrame, frame_payload("conn-43")),
            // A stage export must never run on the connector link.
            (ExportKind::Transform, serde_json::json!({})),
        ],
        digest,
    ));
    // The listener hangs up after the script, so the dial returns.
    let served = serve_connector_link_once(&r.cfg, &r.exec).await;
    let seen = script.await.unwrap().expect("scripted listener");
    drop(served);

    assert!(
        seen.host_calls.len() >= 2,
        "denominator: the overlay must have made host-calls, saw {}",
        seen.host_calls.len()
    );
    let mut by_invoke: HashMap<u64, Vec<&HostCallBody>> = HashMap::new();
    for (id, call) in &seen.host_calls {
        assert_eq!(call.app_id, DISCORD_APP);
        assert_eq!(call.call_id, *id, "charged to the invoke frame");
        by_invoke.entry(*id).or_default().push(call);
    }
    for (id, want) in [(100u64, "conn-42"), (101, "conn-43")] {
        let calls = by_invoke
            .get(&id)
            .unwrap_or_else(|| panic!("no calls for {id}"));
        for c in calls {
            assert_eq!(
                c.args["_scope"]["connection_id"], want,
                "{:?}.{} must carry the ctx-derived scope",
                c.capability, c.op
            );
        }
    }
    // Replies: two guest outcomes (frame_error rides a result), and the stage
    // export refused.
    assert_eq!(seen.replies.len(), 3);
    assert!(seen.replies[0].1.is_ok() && seen.replies[1].1.is_ok());
    assert_eq!(seen.replies[2].1, Err(ErrorCode::ExportMissing));
}

#[tokio::test(flavor = "multi_thread")]
async fn a_server_with_the_wrong_san_is_refused_by_the_executor() {
    let ca = ca();
    let wrong = leaf(&ca, "spiffe://waddlebot/svc-action", Some("localhost"));
    let r = rig("wrongsan", &ca, EXECUTOR_ID).await;
    let acc = acceptor(&ca, &wrong);
    let script = tokio::spawn(serve_script(
        r.listener,
        acc,
        vec![(ExportKind::ConnectorOnFrame, frame_payload("c"))],
        digest_of(DISCORD_WASM),
    ));
    let served = serve_connector_link_once(&r.cfg, &r.exec).await;
    assert!(
        served.is_err(),
        "wrong-SAN server must be refused: {served:?}"
    );
    let scripted = script.await.unwrap();
    assert!(
        scripted.is_err(),
        "no hello may ever reach the wrong server"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn a_stage_certificate_presented_as_the_executor_is_refused_by_the_listener() {
    let ca = ca();
    let server = leaf(&ca, SERVER_ID, Some("localhost"));
    // The executor presents a cert whose SAN names a STAGE.
    let r = rig("stagecert", &ca, STAGE_ID).await;
    let acc = acceptor(&ca, &server);
    let script = tokio::spawn(serve_script(
        r.listener,
        acc,
        vec![(ExportKind::ConnectorOnFrame, frame_payload("c"))],
        digest_of(DISCORD_WASM),
    ));
    let served = serve_connector_link_once(&r.cfg, &r.exec).await;
    assert!(served.is_err(), "stage cert must not attach: {served:?}");
    let scripted = script.await.unwrap();
    assert!(scripted.is_err(), "listener must refuse the stage cert");
}

#[tokio::test]
async fn dialing_without_a_configured_connector_address_is_a_typed_error() {
    let cfg = CliConfig::try_parse_from(base_args(&[])).unwrap();
    let exec = Arc::new(Executor::new(&cfg, MapSource(HashMap::new())).unwrap());
    let err = serve_connector_link_once(&cfg, &exec).await.unwrap_err();
    assert!(err.to_string().contains("CONNECTOR_HOST_API_ADDR"), "{err}");
}

#[test]
fn every_platform_has_a_distinct_host_constant_app_id() {
    let ids: std::collections::HashSet<_> = CONNECTOR_PLATFORMS.iter().map(|p| p.app_id).collect();
    assert_eq!(ids.len(), 3);
}

//! The mTLS host-API listener svc_ingest exposes to the bundle executor.
//!
//! The executor DIALS its stage and speaks first (spec S6.6), so for the
//! generic outbound executor (`sender.build-request`) svc_ingest is the
//! listening side: this module owns the TCP accept loop and the mutual-TLS
//! handshake, then hands each authenticated stream to
//! [`ExecutorLink::attach`] (hello/hello-ok + request multiplexing).
//!
//! Security posture, same as `svc_action::host_api`: the client certificate
//! is REQUIRED and verified against the configured CA before a single frame
//! is read; there is no plaintext mode and no optional-client-auth mode. A
//! failed handshake is logged (no peer-supplied text) and the loop keeps
//! accepting -- per-connection, not whole-listener, failure.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use rustls::client::danger::HandshakeSignatureValid;
use rustls::pki_types::UnixTime;
use rustls::server::danger::{ClientCertVerified, ClientCertVerifier};
use rustls::server::WebPkiClientVerifier;
use rustls::{
    DigitallySignedStruct, DistinguishedName, RootCertStore, ServerConfig, SignatureScheme,
};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use tokio::net::TcpListener;
use tokio::sync::{oneshot, Semaphore};
use tokio_rustls::TlsAcceptor;

use super::ExecutorLink;

/// Upper bound on one TLS handshake.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// Connections being handshaken/attached at once; the excess is dropped.
const MAX_PENDING: usize = 8;

/// File locations of the listener's TLS material (paths only, never bytes).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostApiTls {
    /// PEM server certificate chain.
    pub server_cert: PathBuf,
    /// PEM server private key.
    pub server_key: PathBuf,
    /// PEM CA bundle the executor's client certificate must chain to.
    pub client_ca: PathBuf,
    /// The ONLY client identity (URI/DNS SAN, else Subject CN) the executor's
    /// certificate may carry. A certificate that chains to the CA but names
    /// another identity (e.g. a stage/server certificate) is refused.
    pub client_identity: String,
}

/// Wraps the standard client verifier (chain, expiry, client-auth EKU) and
/// additionally requires the leaf to carry exactly the pinned identity.
#[derive(Debug)]
struct PinnedClientVerifier {
    inner: Arc<dyn ClientCertVerifier>,
    identity: String,
}

impl PinnedClientVerifier {
    /// True when `cert`'s URI/DNS SAN or Subject CN equals the pinned identity;
    /// a parse failure is "no match" (fail closed).
    fn matches(&self, cert: &CertificateDer<'_>) -> bool {
        let Ok((_, parsed)) = x509_parser::parse_x509_certificate(cert.as_ref()) else {
            return false;
        };
        if let Ok(Some(san)) = parsed.subject_alternative_name() {
            for name in &san.value.general_names {
                let candidate = match name {
                    x509_parser::extensions::GeneralName::URI(v)
                    | x509_parser::extensions::GeneralName::DNSName(v) => Some(*v),
                    _ => None,
                };
                if candidate == Some(self.identity.as_str()) {
                    return true;
                }
            }
        }
        let cn_matches = parsed
            .subject()
            .iter_common_name()
            .filter_map(|cn| cn.as_str().ok())
            .any(|cn| cn == self.identity);
        cn_matches
    }
}

impl ClientCertVerifier for PinnedClientVerifier {
    fn offer_client_auth(&self) -> bool {
        self.inner.offer_client_auth()
    }

    fn client_auth_mandatory(&self) -> bool {
        self.inner.client_auth_mandatory()
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
        if self.matches(end_entity) {
            Ok(ClientCertVerified::assertion())
        } else {
            Err(rustls::Error::General(
                "client certificate identity is not the pinned executor identity".to_string(),
            ))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls12_signature(message, cert, dss)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        self.inner.verify_tls13_signature(message, cert, dss)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.inner.supported_verify_schemes()
    }
}

/// Why the listener could not be built. Names the file role, never content.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ListenerError {
    /// A TLS file could not be read or parsed.
    #[error("host-api TLS file unusable: {0}")]
    File(&'static str),
    /// The certificate/key/CA combination was rejected by rustls.
    #[error("host-api TLS configuration rejected: {0}")]
    Tls(&'static str),
}

fn certs(path: &Path, role: &'static str) -> Result<Vec<CertificateDer<'static>>, ListenerError> {
    let list = CertificateDer::pem_file_iter(path)
        .map_err(|_| ListenerError::File(role))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| ListenerError::File(role))?;
    if list.is_empty() {
        return Err(ListenerError::File(role));
    }
    Ok(list)
}

/// Builds the mTLS acceptor: server identity plus REQUIRED client-cert
/// verification against the CA bundle.
///
/// # Errors
/// [`ListenerError`] for an unreadable/empty/malformed file or a rejected
/// cert/key pair.
pub fn build_acceptor(tls: &HostApiTls) -> Result<TlsAcceptor, ListenerError> {
    crate::crypto::ensure_installed();
    let chain = certs(&tls.server_cert, "server_cert")?;
    let key = PrivateKeyDer::from_pem_file(&tls.server_key)
        .map_err(|_| ListenerError::File("server_key"))?;
    let mut roots = RootCertStore::empty();
    for ca in certs(&tls.client_ca, "client_ca")? {
        roots.add(ca).map_err(|_| ListenerError::Tls("client_ca"))?;
    }
    if tls.client_identity.trim().is_empty() {
        return Err(ListenerError::Tls("client_identity"));
    }
    let inner = WebPkiClientVerifier::builder(Arc::new(roots))
        .build()
        .map_err(|_| ListenerError::Tls("client_verifier"))?;
    let verifier = Arc::new(PinnedClientVerifier {
        inner,
        identity: tls.client_identity.clone(),
    });
    let cfg = ServerConfig::builder()
        .with_client_cert_verifier(verifier)
        .with_single_cert(chain, key)
        .map_err(|_| ListenerError::Tls("server_identity"))?;
    Ok(TlsAcceptor::from(Arc::new(cfg)))
}

/// Accepts executor connections until `shutdown` resolves, attaching each
/// mutually-authenticated stream to `link`. Never returns an error for a
/// single bad connection.
pub async fn serve(
    listener: TcpListener,
    acceptor: TlsAcceptor,
    link: Arc<ExecutorLink>,
    mut shutdown: oneshot::Receiver<()>,
) {
    let permits = Arc::new(Semaphore::new(MAX_PENDING));
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            accepted = listener.accept() => {
                let tcp = match accepted {
                    Ok((tcp, _)) => tcp,
                    Err(e) => {
                        tracing::warn!(error = %e, "host-api accept failed");
                        continue;
                    }
                };
                // Several executor replicas stay attached at once and this link
                // no longer replaces a stale session on reconnect, so a replica
                // that vanished without a FIN/RST must be reaped by the OS: dead
                // peer detected in roughly 10s + 5s * 3, never left in rotation.
                if let Err(e) = socket2::SockRef::from(&tcp).set_tcp_keepalive(
                    &socket2::TcpKeepalive::new()
                        .with_time(std::time::Duration::from_secs(10))
                        .with_interval(std::time::Duration::from_secs(5))
                        .with_retries(3),
                ) {
                    tracing::warn!(error = %e, "host-api: failed to set TCP keepalive on accepted socket");
                }
                let Ok(permit) = Arc::clone(&permits).try_acquire_owned() else {
                    tracing::warn!("host-api connection dropped: too many pending handshakes");
                    continue;
                };
                let acceptor = acceptor.clone();
                let link = Arc::clone(&link);
                tokio::spawn(async move {
                    let _permit = permit;
                    match tokio::time::timeout(HANDSHAKE_TIMEOUT, acceptor.accept(tcp)).await {
                        Ok(Ok(stream)) => {
                            if let Err(e) = link.attach(stream).await {
                                tracing::warn!(error = %e, "host-api executor attach failed");
                            }
                        }
                        Ok(Err(_)) => tracing::warn!("host-api mTLS handshake refused"),
                        Err(_) => tracing::warn!("host-api mTLS handshake timed out"),
                    }
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_bundle_host::wire::message::PROTOCOL_VERSION;
    use penguin_bundle_host::wire::{
        read_frame, write_frame, Frame, HelloBody, Message, SandboxInfo,
    };
    use rcgen::{CertificateParams, DnType, IsCa, KeyPair};
    use std::io::Write;

    struct Pki {
        dir: tempfile_dir::Dir,
        tls: HostApiTls,
        ca_pem: String,
        client_cert_pem: String,
        client_key_pem: String,
        rogue_cert_pem: String,
        rogue_key_pem: String,
        /// Same CA, but a STAGE identity (a server certificate used as a client).
        stage_cert_pem: String,
        stage_key_pem: String,
    }

    /// Minimal self-cleaning temp dir (no extra dependency).
    mod tempfile_dir {
        use std::path::{Path, PathBuf};
        pub struct Dir(PathBuf);
        impl Dir {
            pub fn new(tag: &str) -> Self {
                let p = std::env::temp_dir().join(format!(
                    "svc-ingest-hostapi-{tag}-{}-{:?}",
                    std::process::id(),
                    std::thread::current().id()
                ));
                std::fs::create_dir_all(&p).unwrap();
                Self(p)
            }
            pub fn path(&self) -> &Path {
                &self.0
            }
        }
        impl Drop for Dir {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
    }

    fn ca(name: &str) -> (rcgen::Certificate, rcgen::Issuer<'static, KeyPair>) {
        let mut p = CertificateParams::new(Vec::<String>::new()).unwrap();
        p.distinguished_name.push(DnType::CommonName, name);
        p.is_ca = IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        let k = KeyPair::generate().unwrap();
        let cert = p.self_signed(&k).unwrap();
        (cert, rcgen::Issuer::new(p, k))
    }

    fn leaf(
        name: &str,
        sans: Vec<String>,
        issuer: &(rcgen::Certificate, rcgen::Issuer<'static, KeyPair>),
    ) -> (String, String) {
        let mut p = CertificateParams::new(sans).unwrap();
        p.distinguished_name.push(DnType::CommonName, name);
        let k = KeyPair::generate().unwrap();
        let c = p.signed_by(&k, &issuer.1).unwrap();
        (c.pem(), k.serialize_pem())
    }

    fn pki(tag: &str) -> Pki {
        crate::crypto::ensure_installed();
        let dir = tempfile_dir::Dir::new(tag);
        let good = ca("good-ca");
        let rogue = ca("rogue-ca");
        let (srv_c, srv_k) = leaf("svc-ingest", vec!["localhost".into()], &good);
        let (cli_c, cli_k) = leaf("executor", vec!["executor".into()], &good);
        let (rg_c, rg_k) = leaf("executor", vec!["executor".into()], &rogue);
        let (st_c, st_k) = leaf("svc-action", vec!["svc-action".into()], &good);
        let w = |n: &str, s: &str| {
            let p = dir.path().join(n);
            std::fs::File::create(&p)
                .unwrap()
                .write_all(s.as_bytes())
                .unwrap();
            p
        };
        let ca_pem = good.0.pem();
        let tls = HostApiTls {
            server_cert: w("srv.pem", &srv_c),
            server_key: w("srv.key", &srv_k),
            client_ca: w("ca.pem", &ca_pem),
            client_identity: "executor".to_string(),
        };
        Pki {
            dir,
            tls,
            ca_pem,
            client_cert_pem: cli_c,
            client_key_pem: cli_k,
            rogue_cert_pem: rg_c,
            rogue_key_pem: rg_k,
            stage_cert_pem: st_c,
            stage_key_pem: st_k,
        }
    }

    fn client(p: &Pki, cert: &str, key: &str) -> tokio_rustls::TlsConnector {
        let mut roots = RootCertStore::empty();
        for c in CertificateDer::pem_slice_iter(p.ca_pem.as_bytes()) {
            roots.add(c.unwrap()).unwrap();
        }
        let chain: Vec<_> = CertificateDer::pem_slice_iter(cert.as_bytes())
            .map(Result::unwrap)
            .collect();
        let key = PrivateKeyDer::from_pem_slice(key.as_bytes()).unwrap();
        let cfg = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_client_auth_cert(chain, key)
            .unwrap();
        tokio_rustls::TlsConnector::from(Arc::new(cfg))
    }

    async fn start(p: &Pki) -> (std::net::SocketAddr, Arc<ExecutorLink>, oneshot::Sender<()>) {
        let acceptor = build_acceptor(&p.tls).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let link = Arc::new(ExecutorLink::new());
        let (tx, rx) = oneshot::channel();
        tokio::spawn(serve(listener, acceptor, Arc::clone(&link), rx));
        (addr, link, tx)
    }

    async fn hello_over(
        addr: std::net::SocketAddr,
        c: tokio_rustls::TlsConnector,
    ) -> Result<Message, String> {
        let tcp = tokio::net::TcpStream::connect(addr)
            .await
            .map_err(|e| e.to_string())?;
        let name = rustls_pki_types::ServerName::try_from("localhost").unwrap();
        let mut tls = c.connect(name, tcp).await.map_err(|e| e.to_string())?;
        let hello = Frame::new(
            1,
            Message::Hello(HelloBody {
                protocol_version: u32::from(PROTOCOL_VERSION),
                executor_version: "t".to_string(),
                wasmtime_version: "t".to_string(),
                wasmtime_abi: "t".to_string(),
                collector: "drc".to_string(),
                sandbox: SandboxInfo {
                    runtime: "runc".to_string(),
                    verified: false,
                },
            }),
        );
        write_frame(&mut tls, &hello)
            .await
            .map_err(|e| e.to_string())?;
        read_frame(&mut tls)
            .await
            .map(|f| f.message)
            .map_err(|e| e.to_string())
    }

    /// Real path end to end: a CA-signed executor over real mTLS reaches the
    /// real [`ConnectorHostCalls`]: an unscoped call is denied, a scoped one served.
    #[tokio::test]
    async fn host_calls_are_scope_checked_over_the_real_mtls_listener() {
        use crate::connector::host_calls::{
            ActiveConnections, CallScope, ConnectorHostCalls, HostCallConfig, IdentityTokenizer,
            TokenizeError,
        };
        use crate::connector::webhook::BoxFut;
        use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};

        struct Fixed;
        impl IdentityTokenizer for Fixed {
            fn tokenize<'a>(
                &'a self,
                _: &'a CallScope,
                _: &'a str,
            ) -> BoxFut<'a, Result<String, TokenizeError>> {
                Box::pin(async { Ok("123e4567-e89b-12d3-a456-426614174000".to_string()) })
            }
        }
        let table = Arc::new(ActiveConnections::new());
        table
            .activate(CallScope {
                platform: "twitch",
                community_id: 5,
                tenant_id: 9,
                connection_id: 11,
            })
            .unwrap();
        let handler = ConnectorHostCalls::new(
            table,
            Arc::new(Fixed),
            HostCallConfig::new(["twitch"]).unwrap(),
        );
        let p = pki("hostcalls");
        let acceptor = build_acceptor(&p.tls).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let link = Arc::new(ExecutorLink::new().with_host_calls(Arc::new(handler)));
        let (_stop, rx) = oneshot::channel();
        tokio::spawn(serve(listener, acceptor, Arc::clone(&link), rx));

        let c = client(&p, &p.client_cert_pem, &p.client_key_pem);
        let tcp = tokio::net::TcpStream::connect(addr).await.unwrap();
        let name = rustls_pki_types::ServerName::try_from("localhost").unwrap();
        let mut tls = c.connect(name, tcp).await.unwrap();
        let hello = Message::Hello(HelloBody {
            protocol_version: u32::from(PROTOCOL_VERSION),
            executor_version: "t".to_string(),
            wasmtime_version: "t".to_string(),
            wasmtime_abi: "t".to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: "runc".to_string(),
                verified: false,
            },
        });
        write_frame(&mut tls, &Frame::new(1, hello)).await.unwrap();
        assert!(matches!(
            read_frame(&mut tls).await.unwrap().message,
            Message::HelloOk(_)
        ));
        let lookup = |scope: Option<&str>| {
            let mut args = serde_json::json!({
                "kind": "platform-identity", "platform": "twitch",
                "platform_user_id": "u-1", "tenant": "9",
            });
            if let Some(c) = scope {
                args["_scope"] = serde_json::json!({ "connection_id": c });
            }
            Message::HostCall(HostCallBody {
                app_id: "waddles.core.service.twitch".to_string(),
                capability: CapabilityKind::Db,
                op: "identity.lookup".to_string(),
                args,
                call_id: 1,
            })
        };
        for (id, scope, want_ok) in [
            (10, None, false),
            (11, Some("999"), false),
            (12, Some("11"), true),
        ] {
            write_frame(&mut tls, &Frame::new(id, lookup(scope)))
                .await
                .unwrap();
            let f = read_frame(&mut tls).await.unwrap();
            assert_eq!(f.id, id);
            match f.message {
                Message::HostResult(r) => {
                    assert_eq!(r.result.is_some(), want_ok, "{scope:?}");
                    assert_eq!(r.error.is_none(), want_ok, "{scope:?}");
                }
                other => panic!("{other:?}"),
            }
        }
        drop(p.dir);
    }

    #[tokio::test]
    async fn a_ca_signed_executor_attaches_and_gets_hello_ok() {
        let p = pki("ok");
        let (addr, link, _stop) = start(&p).await;
        let reply = hello_over(addr, client(&p, &p.client_cert_pem, &p.client_key_pem))
            .await
            .unwrap();
        assert!(matches!(reply, Message::HelloOk(_)));
        assert!(link.is_attached());
        drop(p.dir);
    }

    #[tokio::test]
    async fn a_client_cert_from_another_ca_is_refused_and_nothing_attaches() {
        let p = pki("rogue");
        let (addr, link, _stop) = start(&p).await;
        let res = hello_over(addr, client(&p, &p.rogue_cert_pem, &p.rogue_key_pem)).await;
        assert!(res.is_err(), "rogue client must not get a reply");
        assert!(!link.is_attached());
    }

    /// A certificate that chains to the SAME CA but carries a stage identity
    /// (not the pinned executor one) is refused as an executor.
    #[tokio::test]
    async fn a_stage_certificate_is_refused_as_an_executor() {
        let p = pki("stage");
        let (addr, link, _stop) = start(&p).await;
        let res = hello_over(addr, client(&p, &p.stage_cert_pem, &p.stage_key_pem)).await;
        assert!(res.is_err(), "wrong-SAN client must not get a reply");
        assert!(!link.is_attached());
    }

    #[test]
    fn a_blank_pinned_identity_is_a_configuration_error() {
        let p = pki("blank");
        let mut t = p.tls.clone();
        t.client_identity = "  ".to_string();
        assert_eq!(
            build_acceptor(&t).err(),
            Some(ListenerError::Tls("client_identity"))
        );
    }

    #[tokio::test]
    async fn a_client_without_a_certificate_is_refused() {
        let p = pki("nocert");
        let (addr, link, _stop) = start(&p).await;
        let mut roots = RootCertStore::empty();
        for c in CertificateDer::pem_slice_iter(p.ca_pem.as_bytes()) {
            roots.add(c.unwrap()).unwrap();
        }
        let cfg = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        let res = hello_over(addr, tokio_rustls::TlsConnector::from(Arc::new(cfg))).await;
        assert!(res.is_err());
        assert!(!link.is_attached());
    }

    #[tokio::test]
    async fn shutdown_stops_the_accept_loop() {
        let p = pki("stop");
        let acceptor = build_acceptor(&p.tls).unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (tx, rx) = oneshot::channel();
        let h = tokio::spawn(serve(listener, acceptor, Arc::new(ExecutorLink::new()), rx));
        tx.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), h)
            .await
            .unwrap()
            .unwrap();
    }

    #[test]
    fn build_acceptor_names_the_unusable_file_role_only() {
        let p = pki("bad");
        let mut t = p.tls.clone();
        t.server_cert = p.dir.path().join("missing.pem");
        assert_eq!(
            build_acceptor(&t).err(),
            Some(ListenerError::File("server_cert"))
        );
        let mut t = p.tls.clone();
        t.server_key = p.tls.client_ca.clone(); // a cert, not a key
        assert_eq!(
            build_acceptor(&t).err(),
            Some(ListenerError::File("server_key"))
        );
        let mut t = p.tls.clone();
        t.client_ca = p.dir.path().join("missing.pem");
        assert_eq!(
            build_acceptor(&t).err(),
            Some(ListenerError::File("client_ca"))
        );
        let mut t = p.tls.clone();
        let empty = p.dir.path().join("empty.pem");
        std::fs::write(&empty, "").unwrap();
        t.client_ca = empty;
        assert_eq!(
            build_acceptor(&t).err(),
            Some(ListenerError::File("client_ca"))
        );
    }
}

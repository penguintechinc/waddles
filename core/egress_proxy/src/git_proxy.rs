// SPDX-License-Identifier: AGPL-3.0-or-later
//! `git-proxy` run mode (marketplace-sources Phase 1, Task 7A): a minimal,
//! CONNECT-only forward proxy a plain `git` client can use via
//! `HTTPS_PROXY`. The regular egress proxy cannot serve git -- it demands a
//! machine-JWT plus an EdDSA assertion header, neither of which git can
//! send -- so this mode drops that gate and substitutes a hard, static
//! **host allowlist** instead, while reusing everything else the egress
//! proxy already hardened:
//!
//! - CONNECT only (any other method is `405`), port **443 only**;
//! - the target host must exactly match `GIT_PROXY_ALLOWED_HOSTS`
//!   (case-insensitive, no wildcards, IP literals never match);
//! - DNS is resolved HERE at connect time (callers never resolve or dial,
//!   which closes the hub-api `url_guard` DNS-rebind gap) and EVERY
//!   resolved address is checked with `ip_policy::is_denied` (loopback,
//!   link-local, metadata, RFC1918/ULA, CGNAT, cluster CIDRs) -- one bad
//!   answer denies the whole request; only a checked address is dialed;
//! - no TLS termination: the tunnel carries git's own TLS end to end.
//!
//! Network-level pinning (CiliumNetworkPolicy toFQDNs) is layered on top by
//! the Helm chart; this module is the in-process enforcement.

use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{combinators::BoxBody, BodyExt, Empty, Full};
use hyper::body::Incoming;
use hyper::service::service_fn;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use ipnet::IpNet;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::Semaphore;

use crate::config::{env_or, parse_cidrs, parse_list, ConfigError};
use crate::dns::{Resolver, TokioResolver};
use crate::ip_policy::{self, DestinationCategory};
use crate::metrics::Metrics;

/// The only destination port a git clone over HTTPS needs.
pub const GIT_PORT: u16 = 443;

type BoxError = Box<dyn std::error::Error + Send + Sync>;
type RespBody = BoxBody<Bytes, BoxError>;

/// Environment-derived configuration for the `git-proxy` mode.
#[derive(Debug, Clone)]
pub struct GitProxyConfig {
    pub listen_port: u16,
    pub metrics_port: u16,
    /// Lower-cased, exact-match git hosts. Never empty (fail closed).
    pub allowed_hosts: Vec<String>,
    pub deny_cidrs: Vec<IpNet>,
    pub deny_cluster_cidrs: Vec<IpNet>,
    pub connect_timeout: Duration,
    pub header_read_timeout: Duration,
    pub tunnel_max_duration: Duration,
    pub max_connections: usize,
    pub shutdown_drain: Duration,
}

impl GitProxyConfig {
    /// Loads config from the environment; an empty `GIT_PROXY_ALLOWED_HOSTS`
    /// is a startup error (an allowlist proxy with no allowlist must never
    /// run "open").
    pub fn from_env() -> Result<Self, ConfigError> {
        let num = |name: &'static str, default: &str| -> Result<u64, ConfigError> {
            env_or(name, default)
                .parse::<u64>()
                .map_err(|_| ConfigError::Invalid(name, "not an integer".into()))
        };
        let port = |name: &'static str, default: &str| -> Result<u16, ConfigError> {
            env_or(name, default)
                .parse::<u16>()
                .map_err(|_| ConfigError::Invalid(name, "not a u16".into()))
        };
        let allowed_hosts: Vec<String> = parse_list(&env_or("GIT_PROXY_ALLOWED_HOSTS", ""))
            .into_iter()
            .map(|h| normalize_host(&h))
            .collect();
        if allowed_hosts.is_empty() {
            return Err(ConfigError::Missing("GIT_PROXY_ALLOWED_HOSTS"));
        }
        Ok(Self {
            listen_port: port("PROXY_LISTEN_PORT", "8444")?,
            metrics_port: port("METRICS_PORT", "9090")?,
            allowed_hosts,
            deny_cidrs: parse_cidrs(&env_or("DENY_CIDRS", ""), "DENY_CIDRS")?,
            deny_cluster_cidrs: parse_cidrs(
                &env_or("DENY_CLUSTER_CIDRS", ""),
                "DENY_CLUSTER_CIDRS",
            )?,
            connect_timeout: Duration::from_secs(num("CONNECT_TIMEOUT_SECONDS", "10")?),
            header_read_timeout: Duration::from_secs(num("HEADER_READ_TIMEOUT_SECONDS", "10")?),
            tunnel_max_duration: Duration::from_secs(num("TUNNEL_MAX_DURATION_SECONDS", "900")?),
            max_connections: num("GIT_PROXY_MAX_CONNECTIONS", "32")? as usize,
            shutdown_drain: Duration::from_secs(num("SHUTDOWN_DRAIN_SECONDS", "25")?),
        })
    }
}

/// Lower-cases and strips a trailing root dot so `GitHub.com.` == `github.com`.
fn normalize_host(host: &str) -> String {
    host.trim().trim_end_matches('.').to_ascii_lowercase()
}

/// Why a CONNECT was refused.
#[derive(thiserror::Error, Debug, PartialEq, Eq)]
pub enum GitProxyError {
    #[error("only CONNECT is supported")]
    MethodNotAllowed,
    #[error("bad request: {0}")]
    BadRequest(&'static str),
    #[error("port {0} is not allowed (git-proxy permits 443 only)")]
    PortNotAllowed(u16),
    #[error("host is not on the git allowlist")]
    HostNotAllowed,
    #[error("dns resolution failed")]
    ResolveFailed,
    #[error("denied: {0}")]
    Denied(&'static str),
}

impl GitProxyError {
    fn status(&self) -> StatusCode {
        match self {
            Self::MethodNotAllowed => StatusCode::METHOD_NOT_ALLOWED,
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::PortNotAllowed(_) | Self::HostNotAllowed | Self::Denied(_) => {
                StatusCode::FORBIDDEN
            }
            Self::ResolveFailed => StatusCode::BAD_GATEWAY,
        }
    }

    fn reason(&self) -> &'static str {
        match self {
            Self::MethodNotAllowed => "method_not_allowed",
            Self::BadRequest(_) => "bad_request",
            Self::PortNotAllowed(_) => "port_not_allowed",
            Self::HostNotAllowed => "host_not_allowed",
            Self::ResolveFailed => "resolve_failed",
            Self::Denied(r) => r,
        }
    }
}

/// Pure allow/deny decision (no dial): port 443, exact host allowlist, fresh
/// DNS resolution, then the shared SSRF/cluster-CIDR policy against EVERY
/// resolved address. Returns the single address that may be dialed.
pub async fn decide(
    host: &str,
    port: u16,
    cfg: &GitProxyConfig,
    resolver: &dyn Resolver,
) -> Result<SocketAddr, GitProxyError> {
    if port != GIT_PORT {
        return Err(GitProxyError::PortNotAllowed(port));
    }
    let host = normalize_host(host);
    // IP literals are never on the allowlist, but reject them explicitly so
    // an operator listing an IP cannot bypass the DNS path by accident.
    if host.parse::<IpAddr>().is_ok() || !cfg.allowed_hosts.contains(&host) {
        return Err(GitProxyError::HostNotAllowed);
    }
    let addrs = resolver
        .resolve(&host, port)
        .await
        .map_err(|_| GitProxyError::ResolveFailed)?;
    if addrs.is_empty() {
        return Err(GitProxyError::ResolveFailed);
    }
    for a in &addrs {
        if let Some(reason) = ip_policy::is_denied(
            a.ip(),
            DestinationCategory::Fqdn,
            &cfg.deny_cluster_cidrs,
            &cfg.deny_cidrs,
            false,
        ) {
            return Err(GitProxyError::Denied(reason));
        }
    }
    Ok(addrs[0])
}

/// Extracts `(host, port)` from a CONNECT request; anything else is refused.
fn connect_target<B>(req: &Request<B>) -> Result<(String, u16), GitProxyError> {
    if req.method() != Method::CONNECT {
        return Err(GitProxyError::MethodNotAllowed);
    }
    let authority = req
        .uri()
        .authority()
        .ok_or(GitProxyError::BadRequest("CONNECT missing authority"))?;
    let port = authority
        .port_u16()
        .ok_or(GitProxyError::BadRequest("CONNECT missing port"))?;
    Ok((authority.host().to_string(), port))
}

fn body(bytes: &'static str) -> RespBody {
    Full::new(Bytes::from_static(bytes.as_bytes()))
        .map_err(|never| match never {})
        .boxed()
}

fn deny(err: &GitProxyError) -> Response<RespBody> {
    let mut resp = Response::new(body(err.reason()));
    *resp.status_mut() = err.status();
    if let Ok(v) = err.reason().parse() {
        resp.headers_mut().insert("x-git-proxy-reason", v);
    }
    resp
}

/// Shared per-process state.
pub struct GitProxyState {
    pub cfg: GitProxyConfig,
    pub resolver: Arc<dyn Resolver>,
    pub metrics: Arc<Metrics>,
    pub permits: Arc<Semaphore>,
}

async fn handle(
    state: Arc<GitProxyState>,
    req: Request<Incoming>,
) -> Result<Response<RespBody>, std::convert::Infallible> {
    let (host, port) = match connect_target(&req) {
        Ok(t) => t,
        Err(e) => return Ok(deny(&e)),
    };
    let addr = match decide(&host, port, &state.cfg, state.resolver.as_ref()).await {
        Ok(a) => a,
        Err(e) => {
            tracing::warn!(host = %host, port, reason = e.reason(), "git_proxy.deny");
            state
                .metrics
                .requests_total
                .with_label_values(&["git-connect", "deny", e.reason()])
                .inc();
            return Ok(deny(&e));
        }
    };
    let Ok(permit) = state.permits.clone().try_acquire_owned() else {
        return Ok(deny(&GitProxyError::BadRequest("too many connections")));
    };
    state
        .metrics
        .requests_total
        .with_label_values(&["git-connect", "allow", "-"])
        .inc();
    tracing::info!(host = %host, port, "git_proxy.allow");

    let connect_timeout = state.cfg.connect_timeout;
    let max_duration = state.cfg.tunnel_max_duration;
    tokio::spawn(async move {
        let _permit = permit;
        let upgraded = match hyper::upgrade::on(req).await {
            Ok(u) => u,
            Err(e) => {
                tracing::warn!(error = %e, "git_proxy.upgrade_error");
                return;
            }
        };
        let mut client = TokioIo::new(upgraded);
        // Dial only the address that passed `decide` -- no second resolution.
        let mut server = match tokio::time::timeout(connect_timeout, TcpStream::connect(addr)).await
        {
            Ok(Ok(s)) => s,
            _ => {
                tracing::warn!(%addr, "git_proxy.connect_failed");
                return;
            }
        };
        let _ = tokio::time::timeout(
            max_duration,
            tokio::io::copy_bidirectional(&mut client, &mut server),
        )
        .await;
    });
    let mut resp = Response::new(Empty::<Bytes>::new().map_err(|n| match n {}).boxed());
    *resp.status_mut() = StatusCode::OK;
    Ok(resp)
}

/// Serves the accept loop on an already-bound listener until `shutdown`
/// resolves; `pub` so integration tests can bind an ephemeral port.
pub async fn serve<F>(
    listener: TcpListener,
    state: Arc<GitProxyState>,
    shutdown: F,
) -> anyhow::Result<()>
where
    F: std::future::Future<Output = ()>,
{
    tokio::pin!(shutdown);
    loop {
        let (stream, _) = tokio::select! {
            biased;
            _ = &mut shutdown => break,
            accepted = listener.accept() => accepted?,
        };
        let st = state.clone();
        let conn = hyper::server::conn::http1::Builder::new()
            .timer(hyper_util::rt::TokioTimer::new())
            .header_read_timeout(state.cfg.header_read_timeout)
            .serve_connection(
                TokioIo::new(stream),
                service_fn(move |req| handle(st.clone(), req)),
            )
            .with_upgrades();
        tokio::spawn(async move {
            if let Err(e) = conn.await {
                tracing::debug!(error = %e, "git_proxy.connection_closed");
            }
        });
    }
    Ok(())
}

/// Entry point for `egress-proxy git-proxy`.
pub async fn run() -> anyhow::Result<()> {
    let (guard, registry) = crate::telemetry::init("git-proxy");
    let _guard = guard;
    let cfg = GitProxyConfig::from_env().map_err(anyhow::Error::from)?;
    let metrics = Metrics::new(&registry);
    let listen = (std::net::Ipv4Addr::UNSPECIFIED, cfg.listen_port);
    let metrics_addr = (std::net::Ipv4Addr::UNSPECIFIED, cfg.metrics_port);
    tracing::info!(
        proxy_port = cfg.listen_port,
        hosts = ?cfg.allowed_hosts,
        "git_proxy.starting"
    );
    let state = Arc::new(GitProxyState {
        permits: Arc::new(Semaphore::new(cfg.max_connections)),
        cfg,
        resolver: Arc::new(TokioResolver),
        metrics: metrics.clone(),
    });
    let metrics_server = axum::serve(
        TcpListener::bind(metrics_addr).await?,
        crate::metrics::router(metrics),
    );
    let signal = crate::termination_signal()?;
    let server = serve(TcpListener::bind(listen).await?, state, async move {
        let name = signal.await;
        tracing::info!(signal = name, "git_proxy.shutdown_signal_received");
    });
    tokio::select! {
        res = metrics_server => res.map_err(anyhow::Error::from),
        res = server => res,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use async_trait::async_trait;
    use std::io;

    struct Fake(Vec<IpAddr>);

    #[async_trait]
    impl Resolver for Fake {
        async fn resolve(&self, _h: &str, port: u16) -> io::Result<Vec<SocketAddr>> {
            Ok(self.0.iter().map(|ip| SocketAddr::new(*ip, port)).collect())
        }
    }

    fn cfg() -> GitProxyConfig {
        GitProxyConfig {
            listen_port: 8444,
            metrics_port: 9090,
            allowed_hosts: vec!["github.com".into(), "gitlab.com".into()],
            deny_cidrs: vec![],
            deny_cluster_cidrs: vec!["10.244.0.0/16".parse().unwrap()],
            connect_timeout: Duration::from_secs(1),
            header_read_timeout: Duration::from_secs(1),
            tunnel_max_duration: Duration::from_secs(1),
            max_connections: 4,
            shutdown_drain: Duration::from_secs(1),
        }
    }

    fn public() -> Fake {
        Fake(vec!["140.82.112.3".parse().unwrap()])
    }

    #[tokio::test]
    async fn allowlisted_host_on_443_is_allowed_and_case_insensitive() {
        let a = decide("GitHub.COM.", 443, &cfg(), &public()).await.unwrap();
        assert_eq!(a.port(), 443);
    }

    #[tokio::test]
    async fn non_allowlisted_host_is_rejected() {
        let e = decide("evil.example", 443, &cfg(), &public()).await;
        assert_eq!(e, Err(GitProxyError::HostNotAllowed));
    }

    #[tokio::test]
    async fn suffix_and_subdomain_tricks_are_rejected() {
        for h in ["github.com.evil.example", "api.github.com", "notgithub.com"] {
            assert_eq!(
                decide(h, 443, &cfg(), &public()).await,
                Err(GitProxyError::HostNotAllowed),
                "{h}"
            );
        }
    }

    #[tokio::test]
    async fn ip_literals_are_rejected_even_if_listed() {
        let mut c = cfg();
        c.allowed_hosts.push("140.82.112.3".into());
        assert_eq!(
            decide("140.82.112.3", 443, &c, &public()).await,
            Err(GitProxyError::HostNotAllowed)
        );
    }

    #[tokio::test]
    async fn non_443_ports_are_rejected() {
        for p in [22, 80, 8443, 9418] {
            assert_eq!(
                decide("github.com", p, &cfg(), &public()).await,
                Err(GitProxyError::PortNotAllowed(p))
            );
        }
    }

    #[tokio::test]
    async fn allowlisted_host_resolving_to_private_link_local_or_cluster_is_denied() {
        for ip in [
            "10.0.0.5",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "10.244.3.4",
            "100.64.0.1",
            "::1",
            "fe80::1",
        ] {
            let r = Fake(vec![ip.parse().unwrap()]);
            assert!(
                matches!(
                    decide("github.com", 443, &cfg(), &r).await,
                    Err(GitProxyError::Denied(_))
                ),
                "{ip} must be denied"
            );
        }
    }

    #[tokio::test]
    async fn one_bad_answer_among_public_ones_denies_the_request() {
        let r = Fake(vec![
            "140.82.112.3".parse().unwrap(),
            "169.254.169.254".parse().unwrap(),
        ]);
        assert!(matches!(
            decide("github.com", 443, &cfg(), &r).await,
            Err(GitProxyError::Denied(_))
        ));
    }

    #[tokio::test]
    async fn empty_resolution_fails_closed() {
        assert_eq!(
            decide("github.com", 443, &cfg(), &Fake(vec![])).await,
            Err(GitProxyError::ResolveFailed)
        );
    }

    #[test]
    fn only_connect_is_accepted() {
        let get = Request::builder()
            .method("GET")
            .uri("http://github.com/")
            .body(())
            .unwrap();
        assert_eq!(connect_target(&get), Err(GitProxyError::MethodNotAllowed));
        let ok = Request::builder()
            .method("CONNECT")
            .uri("github.com:443")
            .body(())
            .unwrap();
        assert_eq!(connect_target(&ok), Ok(("github.com".into(), 443)));
        let noport = Request::builder()
            .method("CONNECT")
            .uri("github.com")
            .body(())
            .unwrap();
        assert!(connect_target(&noport).is_err());
    }

    #[test]
    fn config_requires_a_non_empty_allowlist() {
        let _g = crate::ENV_LOCK.blocking_lock();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("GIT_PROXY_ALLOWED_HOSTS") };
        assert!(GitProxyConfig::from_env().is_err());
        unsafe { std::env::set_var("GIT_PROXY_ALLOWED_HOSTS", " GitHub.com , gitlab.com ") };
        let c = GitProxyConfig::from_env().unwrap();
        unsafe { std::env::remove_var("GIT_PROXY_ALLOWED_HOSTS") };
        assert_eq!(c.allowed_hosts, vec!["github.com", "gitlab.com"]);
    }

    #[tokio::test]
    async fn live_server_refuses_non_allowlisted_connect_and_non_connect() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        let registry = prometheus::Registry::new();
        let state = Arc::new(GitProxyState {
            permits: Arc::new(Semaphore::new(4)),
            cfg: cfg(),
            resolver: Arc::new(public()),
            metrics: Metrics::new(&registry),
        });
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(serve(listener, state, std::future::pending::<()>()));
        for (req, want) in [
            (
                "CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n",
                "403",
            ),
            (
                "CONNECT github.com:22 HTTP/1.1\r\nHost: github.com:22\r\n\r\n",
                "403",
            ),
            (
                "GET http://github.com/ HTTP/1.1\r\nHost: github.com\r\n\r\n",
                "405",
            ),
        ] {
            let mut s = TcpStream::connect(addr).await.unwrap();
            s.write_all(req.as_bytes()).await.unwrap();
            let mut buf = [0u8; 64];
            let n = s.read(&mut buf).await.unwrap();
            let line = String::from_utf8_lossy(&buf[..n]);
            assert!(
                line.starts_with(&format!("HTTP/1.1 {want}")),
                "{req} -> {line}"
            );
        }
    }
}

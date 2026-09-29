//! Core proxy pipeline: authenticate the caller, verify the signed
//! allowlist assertion, re-validate the destination against it (including
//! a fresh DNS resolution, never trusting the caller's own resolution),
//! and only then dial the single validated address -- no redirects
//! handled at this layer, no second resolution between check and connect.
//!
//! [`validate`] is the pure, network-connect-free decision function
//! (auth -> assertion -> port allowlist -> destination match -> DNS
//! resolve -> address-category policy); [`handle`] wires it into the
//! actual hyper HTTP CONNECT / forward-HTTP server.

use std::collections::HashSet;
use std::convert::Infallible;
use std::net::{IpAddr, SocketAddr};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Instant;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Empty, Full};
use hyper::body::Incoming;
use hyper::upgrade::Upgraded;
use hyper::{Method, Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use ipnet::IpNet;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tracing::Instrument;
use tracing_opentelemetry::OpenTelemetrySpanExt;

use egress_assertion::FORWARD_AUTHORIZATION_HEADER;

use crate::config::Config;
use crate::dns::Resolver;
use crate::ip_policy::DestinationCategory;
use crate::limits::TenantLimiter;
use crate::metrics::Metrics;
use crate::{assertion, audit, auth, ip_policy};

type BoxError = Box<dyn std::error::Error + Send + Sync>;
pub type RespBody = BoxBody<Bytes, BoxError>;

fn full_body(bytes: impl Into<Bytes>) -> RespBody {
    Full::new(bytes.into())
        .map_err(|never: Infallible| match never {})
        .boxed()
}

fn empty_body() -> RespBody {
    Empty::new()
        .map_err(|never: Infallible| match never {})
        .boxed()
}

#[derive(Debug, Clone)]
pub struct ValidatedTarget {
    pub addr: SocketAddr,
    pub tenant: String,
    pub community: String,
    pub app: String,
    pub category: DestinationCategory,
}

#[derive(thiserror::Error, Debug)]
pub enum ProxyError {
    #[error("auth: {0}")]
    Auth(#[from] auth::AuthError),
    #[error("assertion: {0}")]
    Assertion(#[from] assertion::AssertionError),
    #[error("destination does not match the granted assertion")]
    DestinationMismatch,
    #[error("port {0} is not in the operator-allowed set")]
    PortNotAllowed(u16),
    #[error("dns resolution failed: {0}")]
    ResolveFailed(String),
    #[error("denied: {0}")]
    Denied(&'static str),
    #[error("connect to upstream failed: {0}")]
    ConnectFailed(String),
    #[error("rate limited: {0}")]
    RateLimited(#[from] crate::limits::LimitError),
    #[error("bad request: {0}")]
    BadRequest(String),
}

impl ProxyError {
    fn status(&self) -> StatusCode {
        match self {
            ProxyError::Auth(_) | ProxyError::Assertion(_) => StatusCode::UNAUTHORIZED,
            ProxyError::DestinationMismatch
            | ProxyError::PortNotAllowed(_)
            | ProxyError::Denied(_) => StatusCode::FORBIDDEN,
            ProxyError::ResolveFailed(_) | ProxyError::ConnectFailed(_) => StatusCode::BAD_GATEWAY,
            ProxyError::RateLimited(_) => StatusCode::TOO_MANY_REQUESTS,
            ProxyError::BadRequest(_) => StatusCode::BAD_REQUEST,
        }
    }

    fn reason(&self) -> &'static str {
        match self {
            ProxyError::Auth(_) => "unauthenticated",
            ProxyError::Assertion(_) => "invalid_assertion",
            ProxyError::DestinationMismatch => "destination_mismatch",
            ProxyError::PortNotAllowed(_) => "port_not_allowed",
            ProxyError::ResolveFailed(_) => "resolve_failed",
            ProxyError::Denied(r) => r,
            ProxyError::ConnectFailed(_) => "connect_failed",
            ProxyError::RateLimited(_) => "rate_limited",
            ProxyError::BadRequest(_) => "bad_request",
        }
    }
}

pub struct ValidationDeps<'a> {
    pub trust_bundle: &'a dyn service_auth::TrustBundle,
    pub replay_cache: &'a dyn assertion::ReplayCache,
    pub cfg: &'a Config,
    pub cluster_cidrs: &'a [IpNet],
    pub resolver: &'a dyn Resolver,
}

/// The full request-validation pipeline (spec-equivalent to
/// `bundle_host_http::egress::EgressGuard::send_checked`'s steps 1-7, run
/// a second time at the network egress point): authenticate the calling
/// service, verify the signed allowlist assertion, check the requested
/// port and destination against it, resolve DNS itself (never trusting
/// any resolution the caller may have already done), and re-check the
/// *resolved* address against the always-enforced SSRF/cluster-CIDR
/// policy. Performs no network I/O beyond the DNS resolution itself --
/// callers dial `addr` only after this returns `Ok`.
pub async fn validate(
    headers: &http::HeaderMap,
    host: &str,
    port: u16,
    deps: ValidationDeps<'_>,
) -> Result<ValidatedTarget, ProxyError> {
    let trusted_issuers: Vec<&str> = deps
        .cfg
        .machine_jwt_trusted_issuers
        .iter()
        .map(String::as_str)
        .collect();
    let caller = auth::authenticate(
        headers,
        deps.trust_bundle,
        &deps.cfg.machine_jwt_audience,
        &trusted_issuers,
        &deps.cfg.machine_jwt_required_scope,
        &deps.cfg.allowed_caller_services,
    )
    .await?;

    let assertion = assertion::verify_from_header(
        headers,
        deps.trust_bundle,
        deps.cfg.assertion_max_ttl.as_secs(),
        deps.replay_cache,
    )
    .await?;

    // The assertion must be signed by the same identity the machine JWT
    // just authenticated -- otherwise a token minted by service A (even a
    // validly-signed one) could be replayed alongside service B's own
    // machine JWT.
    if assertion.sub != caller.sub {
        return Err(ProxyError::Assertion(
            assertion::AssertionError::SubMismatch {
                assertion_sub: assertion.sub.clone(),
                jwt_sub: caller.sub.clone(),
            },
        ));
    }

    if !deps.cfg.allowed_ports.contains(&port) {
        return Err(ProxyError::PortNotAllowed(port));
    }

    if !assertion::destination_matches(&assertion, host, port) {
        return Err(ProxyError::DestinationMismatch);
    }

    // DNS resolution is always performed here, never taken from the
    // caller -- the anti-rebinding property under test: even if `host`
    // was already an FQDN the caller itself resolved, this is the only
    // resolution whose result is ever dialed.
    let addrs = if let Ok(ip) = host.parse::<IpAddr>() {
        vec![SocketAddr::new(ip, port)]
    } else {
        deps.resolver
            .resolve(host, port)
            .await
            .map_err(|e| ProxyError::ResolveFailed(e.to_string()))?
    };
    let addr = addrs
        .into_iter()
        .next()
        .ok_or_else(|| ProxyError::ResolveFailed("no addresses returned".into()))?;

    if !assertion::resolved_matches(&assertion, addr.ip()) {
        return Err(ProxyError::DestinationMismatch);
    }

    if let Some(reason) = ip_policy::is_denied(
        addr.ip(),
        assertion.category,
        deps.cluster_cidrs,
        &deps.cfg.deny_cidrs,
        deps.cfg.allow_private_ip,
    ) {
        return Err(ProxyError::Denied(reason));
    }

    Ok(ValidatedTarget {
        addr,
        tenant: assertion.tenant,
        community: assertion.community,
        app: assertion.app,
        category: assertion.category,
    })
}

/// Shared, cloneable state handed to every connection's `service_fn`.
pub struct ProxyState {
    pub cfg: Arc<Config>,
    pub trust_bundle: Arc<dyn service_auth::TrustBundle>,
    pub replay_cache: Arc<dyn assertion::ReplayCache>,
    pub cluster_cidrs: Vec<IpNet>,
    pub resolver: Arc<dyn Resolver>,
    pub limiter: Arc<TenantLimiter>,
    pub metrics: Arc<Metrics>,
}

fn target_host_port(req: &Request<Incoming>) -> Result<(String, u16), ProxyError> {
    if req.method() == Method::CONNECT {
        let authority = req
            .uri()
            .authority()
            .ok_or_else(|| ProxyError::BadRequest("CONNECT missing authority".into()))?;
        let port = authority
            .port_u16()
            .ok_or_else(|| ProxyError::BadRequest("CONNECT missing port".into()))?;
        return Ok((authority.host().to_string(), port));
    }
    if let Some(host) = req.uri().host() {
        let port = req.uri().port_u16().unwrap_or(80);
        return Ok((host.to_string(), port));
    }
    // Origin-form request (no absolute URI) -- fall back to the Host
    // header, parsed as an `http::uri::Authority` (IPv6-aware: handles
    // bracketed literals like `[::1]:8443` correctly) rather than a naive
    // `splitn(2, ':')`, which would misparse an unbracketed IPv6 literal
    // (multiple colons) as an ambiguous host/port split -- silently
    // truncating the host to the first colon-delimited segment.
    let host_header = req
        .headers()
        .get(http::header::HOST)
        .and_then(|v| v.to_str().ok())
        .ok_or_else(|| ProxyError::BadRequest("no absolute URI and no Host header".into()))?;
    let authority = http::uri::Authority::from_str(host_header)
        .map_err(|e| ProxyError::BadRequest(format!("invalid Host header: {e}")))?;
    let port = authority.port_u16().unwrap_or(80);
    Ok((authority.host().to_string(), port))
}

fn error_response(err: &ProxyError) -> Response<RespBody> {
    Response::builder()
        .status(err.status())
        .header("x-egress-proxy-reason", err.reason())
        .body(full_body(err.reason()))
        .expect("building an error response never fails")
}

/// Top-level `service_fn` entry point: extracts any W3C `traceparent` the
/// calling service sent, opens this request's own span as its child (one
/// trace spanning the service boundary, per `critical-rules.md`
/// Observability -- "propagate trace context across every service
/// boundary"), and instruments the whole request lifecycle
/// ([`handle_inner`]) with it before dispatching to [`handle_connect`]/
/// [`handle_forward`].
pub async fn handle(
    state: Arc<ProxyState>,
    req: Request<Incoming>,
) -> Result<Response<RespBody>, Infallible> {
    let mode = if req.method() == Method::CONNECT {
        "connect"
    } else {
        "forward"
    };
    let parent_cx = crate::telemetry::extract_parent_context(req.headers());
    let span = tracing::info_span!("egress_proxy.request", mode, otel.kind = "server");
    let _ = span.set_parent(parent_cx);
    handle_inner(state, req, mode).instrument(span).await
}

/// Dispatches `CONNECT` (TLS tunnel -- wss/Discord gateway, IRC-over-TLS
/// on 6697, any other TLS destination) to [`handle_connect`] and
/// everything else to [`handle_forward`] (plain forward HTTP). Split out
/// of [`handle`] purely so the request span wraps this entire function,
/// including the validation pipeline below.
async fn handle_inner(
    state: Arc<ProxyState>,
    req: Request<Incoming>,
    mode: &'static str,
) -> Result<Response<RespBody>, Infallible> {
    let (host, port) = match target_host_port(&req) {
        Ok(v) => v,
        Err(e) => return Ok(error_response(&e)),
    };

    let started = Instant::now();
    let deps = ValidationDeps {
        trust_bundle: state.trust_bundle.as_ref(),
        replay_cache: state.replay_cache.as_ref(),
        cfg: state.cfg.as_ref(),
        cluster_cidrs: &state.cluster_cidrs,
        resolver: state.resolver.as_ref(),
    };
    let result = validate(req.headers(), &host, port, deps).await;
    state
        .metrics
        .request_duration_seconds
        .with_label_values(&[mode])
        .observe(started.elapsed().as_secs_f64());

    let (tenant, community, app, category) = match &result {
        Ok(target) => (
            target.tenant.clone(),
            target.community.clone(),
            target.app.clone(),
            target.category,
        ),
        // Best-effort audit context for a request that failed validation
        // before/at the assertion step -- audit never blocks on this, and
        // deliberately uses the bare `assertion::verify` (never
        // `verify_from_header`) so a request that already failed
        // validation elsewhere never itself consumes a replay-cache slot.
        Err(_) => match req
            .headers()
            .get(assertion::ASSERTION_HEADER)
            .and_then(|v| v.to_str().ok())
        {
            Some(token) => match assertion::verify(
                token,
                state.trust_bundle.as_ref(),
                state.cfg.assertion_max_ttl.as_secs(),
            )
            .await
            {
                Ok(a) => (a.tenant, a.community, a.app, a.category),
                Err(_) => (
                    "unknown".to_string(),
                    "unknown".to_string(),
                    "unknown".to_string(),
                    DestinationCategory::Fqdn,
                ),
            },
            None => (
                "unknown".to_string(),
                "unknown".to_string(),
                "unknown".to_string(),
                DestinationCategory::Fqdn,
            ),
        },
    };
    audit::log_decision(
        &tenant,
        &community,
        &app,
        category,
        &host,
        port,
        result.is_ok(),
        result.as_ref().err().map(ProxyError::reason),
    );

    let target = match result {
        Ok(t) => t,
        Err(e) => {
            state
                .metrics
                .requests_total
                .with_label_values(&[mode, "deny", e.reason()])
                .inc();
            return Ok(error_response(&e));
        }
    };

    let guard = match state.limiter.acquire(&target.tenant) {
        Ok(g) => g,
        Err(e) => {
            let e = ProxyError::from(e);
            state
                .metrics
                .requests_total
                .with_label_values(&[mode, "deny", e.reason()])
                .inc();
            return Ok(error_response(&e));
        }
    };
    state
        .metrics
        .requests_total
        .with_label_values(&[mode, "allow", "-"])
        .inc();

    if req.method() == Method::CONNECT {
        Ok(handle_connect(state, req, target, guard))
    } else {
        Ok(handle_forward(state, req, target, guard).await)
    }
}

/// Responds `200` immediately and, once the connection upgrades to a raw
/// byte tunnel, dials the already-validated address and relays bytes
/// bidirectionally under the tenant's bandwidth limiter. No TLS is
/// terminated here -- the tunnel carries the client's own TLS handshake
/// straight through, exactly like any other HTTP CONNECT proxy.
fn handle_connect(
    state: Arc<ProxyState>,
    req: Request<Incoming>,
    target: ValidatedTarget,
    guard: crate::limits::ConnectionGuard,
) -> Response<RespBody> {
    let metrics = state.metrics.clone();
    let limiter = state.limiter.clone();
    let connect_timeout = state.cfg.connect_timeout;
    let idle_timeout = state.cfg.tunnel_idle_timeout;
    let max_duration = state.cfg.tunnel_max_duration;
    let tenant = target.tenant.clone();
    let addr = target.addr;

    tokio::spawn(async move {
        let _guard = guard; // held for the tunnel's lifetime
        match hyper::upgrade::on(req).await {
            Ok(upgraded) => {
                if let Err(e) = run_tunnel(
                    upgraded,
                    addr,
                    connect_timeout,
                    idle_timeout,
                    max_duration,
                    tenant,
                    limiter,
                    metrics,
                )
                .await
                {
                    tracing::warn!(error = %e, "egress_proxy.tunnel_error");
                }
            }
            Err(e) => tracing::warn!(error = %e, "egress_proxy.upgrade_error"),
        }
    });

    Response::new(empty_body())
}

#[allow(clippy::too_many_arguments)]
async fn run_tunnel(
    upgraded: Upgraded,
    addr: SocketAddr,
    connect_timeout: std::time::Duration,
    idle_timeout: std::time::Duration,
    max_duration: std::time::Duration,
    tenant: String,
    limiter: Arc<TenantLimiter>,
    metrics: Arc<Metrics>,
) -> std::io::Result<()> {
    let started = Instant::now();
    let server = tokio::time::timeout(connect_timeout, TcpStream::connect(addr))
        .await
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::TimedOut, "connect timed out"))??;
    let (server_r, server_w) = server.into_split();
    let client_io = TokioIo::new(upgraded);
    let (client_r, client_w) = tokio::io::split(client_io);

    let egress = copy_with_throttle(
        client_r,
        server_w,
        limiter.clone(),
        tenant.clone(),
        metrics.clone(),
        "egress",
        idle_timeout,
    );
    let ingress = copy_with_throttle(
        server_r,
        client_w,
        limiter,
        tenant.clone(),
        metrics.clone(),
        "ingress",
        idle_timeout,
    );
    // Hard ceiling on the whole tunnel's lifetime regardless of activity --
    // dropping this future on timeout drops both copy futures, closing
    // both socket halves they own.
    let (egress_bytes, ingress_bytes) =
        match tokio::time::timeout(max_duration, join_copy_futures(egress, ingress)).await {
            Ok(pair) => pair,
            Err(_) => {
                tracing::debug!(tenant = %tenant, "egress_proxy.tunnel_max_duration_exceeded");
                (0, 0)
            }
        };

    // Observed once per tunnel, on close -- the "connection-duration" and
    // "bytes-transferred" histograms `critical-rules.md` Observability
    // requires, distinct from `request_duration_seconds`'s validate+dial
    // latency observed before the tunnel even opens.
    metrics
        .connection_duration_seconds
        .with_label_values(&["connect"])
        .observe(started.elapsed().as_secs_f64());
    let total_bytes = egress_bytes + ingress_bytes;
    metrics
        .connection_bytes
        .with_label_values(&[&tenant])
        .observe(total_bytes as f64);
    Ok(())
}

/// `tokio::join!` isn't itself expressible as a value that
/// `tokio::time::timeout` can wrap directly (it's a macro, not a future) --
/// this tiny helper packages the pair as a single `Future` so the whole
/// join can be raced against `max_duration` as one unit, dropping both
/// copy loops together on timeout.
async fn join_copy_futures(
    egress: impl std::future::Future<Output = std::io::Result<u64>>,
    ingress: impl std::future::Future<Output = std::io::Result<u64>>,
) -> (u64, u64) {
    let (e, i) = tokio::join!(egress, ingress);
    (e.unwrap_or(0), i.unwrap_or(0))
}

async fn copy_with_throttle<R, W>(
    mut reader: R,
    mut writer: W,
    limiter: Arc<TenantLimiter>,
    tenant: String,
    metrics: Arc<Metrics>,
    direction: &'static str,
    idle_timeout: std::time::Duration,
) -> std::io::Result<u64>
where
    R: AsyncRead + Unpin,
    W: AsyncWrite + Unpin,
{
    let mut buf = vec![0u8; 16 * 1024];
    let mut total = 0u64;
    loop {
        let n = match tokio::time::timeout(idle_timeout, reader.read(&mut buf)).await {
            Ok(result) => result?,
            Err(_) => {
                // No bytes in either direction within `idle_timeout` --
                // close gracefully rather than holding the tenant's
                // connection-limiter slot (and the underlying sockets)
                // open indefinitely.
                let _ = writer.shutdown().await;
                return Ok(total);
            }
        };
        if n == 0 {
            let _ = writer.shutdown().await;
            return Ok(total);
        }
        limiter.throttle(&tenant, n).await;
        writer.write_all(&buf[..n]).await?;
        total += n as u64;
        metrics
            .bytes_transferred_total
            .with_label_values(&[&tenant, direction])
            .inc_by(n as u64);
    }
}

/// Plain forward-HTTP proxying (`net.http.fqdn`/`.public-ip` categories,
/// port 80 by default): dials the validated address and relays the
/// request/response as a normal HTTP/1.1 client connection. The request
/// body streams through unbuffered (`Incoming` implements `http_body::
/// Body` directly); the response body is boxed, not buffered, for the
/// same reason.
async fn handle_forward(
    state: Arc<ProxyState>,
    req: Request<Incoming>,
    target: ValidatedTarget,
    guard: crate::limits::ConnectionGuard,
) -> Response<RespBody> {
    let _guard = guard;
    match forward_inner(&state, req, &target).await {
        Ok(resp) => resp,
        Err(e) => {
            tracing::warn!(error = %e, tenant = %target.tenant, "egress_proxy.forward_error");
            error_response(&ProxyError::ConnectFailed(e.to_string()))
        }
    }
}

// FORWARD_AUTHORIZATION_HEADER: the inbound header this proxy accepts as
// an explicit, deliberate end-to-end credential the calling bundle wants
// delivered to the destination's own `Authorization` header (e.g. a
// third-party API key substituted by `bundle_host_http`'s secret-handle
// broker). Kept distinct from `Authorization` itself specifically so the
// blanket proxy-hop-credential strip in `build_outbound_headers` can never
// accidentally let a caller-supplied `Authorization` (which, on the
// inbound side, is *this proxy's own* machine-JWT bearer credential --
// see `auth::authenticate`) leak straight through under its own name.
// Canonical definition (shared with `core/bundle_host_http`'s signer) now
// lives in `egress_assertion`, imported at the top of this file.

/// Builds the outbound request headers from an explicit allowlist of the
/// inbound headers, rather than relaying `inbound` verbatim (CRITICAL
/// security-review finding: verbatim relay leaked this proxy's own
/// `Authorization` machine-JWT bearer and `X-Waddles-Egress-Assertion`
/// straight to arbitrary internet destinations).
///
/// Stripped, always:
/// - `Authorization` / `Proxy-Authorization` -- these are *this proxy
///   hop's own* credentials (the calling service's machine JWT), never
///   meant for the destination.
/// - Every `X-Waddles-*` header -- proxy-internal signaling
///   (`X-Waddles-Egress-Assertion`, this function's own
///   `X-Waddles-Forward-Authorization`), never end-to-end.
/// - Standard HTTP/1.1 hop-by-hop headers (RFC 7230 §6.1) plus whatever
///   extra header names the caller's own `Connection` header lists.
///
/// [`FORWARD_AUTHORIZATION_HEADER`] is the one deliberate escape hatch: a
/// bundle that needs to send its *own* credential to the destination
/// (after `bundle_host_http`'s secret-handle substitution) sets that
/// distinct header, which is mapped back to a real `Authorization` header
/// on the outbound request only -- after every other `x-waddles-*`/
/// `authorization` header has already been stripped, so it can never be
/// smuggled in under a different name.
fn build_outbound_headers(inbound: &http::HeaderMap) -> http::HeaderMap {
    let mut hop_by_hop: HashSet<String> = [
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    if let Some(connection) = inbound
        .get(http::header::CONNECTION)
        .and_then(|v| v.to_str().ok())
    {
        for token in connection.split(',') {
            hop_by_hop.insert(token.trim().to_ascii_lowercase());
        }
    }

    let mut outbound = http::HeaderMap::with_capacity(inbound.len());
    for (name, value) in inbound.iter() {
        let lower = name.as_str().to_ascii_lowercase();
        if lower == "authorization"
            || lower == "proxy-authorization"
            || lower.starts_with("x-waddles-")
            || hop_by_hop.contains(lower.as_str())
        {
            continue;
        }
        outbound.append(name, value.clone());
    }
    if let Some(forwarded_auth) = inbound.get(FORWARD_AUTHORIZATION_HEADER) {
        outbound.insert(http::header::AUTHORIZATION, forwarded_auth.clone());
    }
    outbound
}

async fn forward_inner(
    state: &Arc<ProxyState>,
    req: Request<Incoming>,
    target: &ValidatedTarget,
) -> Result<Response<RespBody>, BoxError> {
    let started = Instant::now();
    let stream =
        tokio::time::timeout(state.cfg.connect_timeout, TcpStream::connect(target.addr)).await??;
    let io = TokioIo::new(stream);
    let (mut sender, conn) = hyper::client::conn::http1::handshake(io).await?;
    tokio::spawn(async move {
        if let Err(e) = conn.await {
            tracing::debug!(error = %e, "egress_proxy.forward_conn_closed");
        }
    });

    // Rewrite absolute-form ("http://host/path") to origin-form ("/path")
    // before handing the request to the upstream server, as any HTTP/1.1
    // forward proxy must, and rebuild the headers from the allowlist
    // instead of relaying the inbound set verbatim (see
    // `build_outbound_headers`).
    let (mut parts, body) = req.into_parts();
    if let Some(path_and_query) = parts.uri.path_and_query().cloned() {
        let mut new_uri_parts = http::uri::Parts::default();
        new_uri_parts.path_and_query = Some(path_and_query);
        parts.uri = http::Uri::from_parts(new_uri_parts).unwrap_or(parts.uri);
    }
    parts.headers = build_outbound_headers(&parts.headers);
    let req = Request::from_parts(parts, body);

    let resp = sender.send_request(req).await?;
    let (parts, body) = resp.into_parts();

    // Observed here rather than after the body finishes streaming (the
    // response body is intentionally never buffered -- module doc): total
    // round-trip time and, where the upstream declares one, `Content-
    // Length` as an approximate size. A chunked/unknown-length response
    // simply skips the bytes observation; `bytes_transferred_total` (the
    // CONNECT tunnel's exact running counter) remains the authoritative
    // byte-accounting signal for this proxy overall.
    state
        .metrics
        .connection_duration_seconds
        .with_label_values(&["forward"])
        .observe(started.elapsed().as_secs_f64());
    if let Some(len) = parts
        .headers
        .get(http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<f64>().ok())
    {
        state
            .metrics
            .connection_bytes
            .with_label_values(&[&target.tenant])
            .observe(len);
    }

    let body = body.map_err(|e| Box::new(e) as BoxError).boxed();
    Ok(Response::from_parts(parts, body))
}

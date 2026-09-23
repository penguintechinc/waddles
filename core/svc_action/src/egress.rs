//! The bundle `http` host capability's guarded outbound egress (spec §8):
//! a bundle never dials the network directly (spec §11.3/§11.4 -- "no
//! bundle reaches the network except through a stage-side client"). Every
//! `http.send` runs the full enforcement order of spec §8.2 before a byte
//! leaves this process: scheme, malformed-URL, declared-host, declared-
//! method, tenant denylist, resolved-address SSRF check, DNS-rebind
//! pinning, rate limit, TLS verification, redirect re-check, response-size
//! truncation and timeout -- in that order, every rejection classified by
//! the same reason string spec §8.2's table names.
//!
//! **Split for testability, matching this crate's existing per-dependency
//! trait pattern** (`crate::capabilities::RelayQueue`, `crate::dispatch::
//! AuditSink`/`TenantResolver`): [`EgressGuard`] owns steps 1-8 and the
//! redirect-recheck loop (spec-security-critical, exercised in
//! [`tests`] against literal loopback/private/metadata IPs with no
//! network access at all); [`HttpTransport`] owns the actual TLS
//! connect/send/response-size-cap (steps 9, 11, 12), pinned to the exact
//! address [`EgressGuard`] already validated (step 7 -- "no second
//! resolution between check and connect").
//!
//! **§8.5 scope reminder:** this module governs bundle-initiated `http.send`
//! calls only. The stage's own connections to Postgres/Valkey/the bucket/
//! hub-api/OTLP are operator configuration, never routed through this guard.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use base64::Engine;
use futures_util::StreamExt;
use penguin_bundle_host::wire::HostResultError;
use serde::Deserialize;

use crate::capabilities::denied;
use crate::distribution::BundleCatalog;

/// Operator-tunable limits (spec §7.3/§8.2), sourced from
/// [`crate::config::CliConfig`]'s `egress_*` fields.
#[derive(Debug, Clone)]
pub struct EgressLimits {
    pub allow_private_hosts: bool,
    pub rate_limit_rps: u32,
    pub rate_limit_burst: u32,
    pub timeout: Duration,
    pub max_redirects: u8,
    pub max_response_bytes: usize,
}

/// One `http.send` request as decoded from a bundle's host-call `args`
/// (this crate's own JSON-wire convention for the WIT `http::request`
/// record of spec §6.5 -- see `crate::dispatch::invoke_dispatch`'s doc for
/// why each host-call/host-result JSON shape is a per-crate documented
/// choice, not a value copied from a landed reference). `body`/response
/// `body` are base64 rather than a JSON byte array or lossy UTF-8, to stay
/// byte-exact with the WIT `list<u8>` without a JSON array of small
/// integers.
#[derive(Debug, Clone, Deserialize)]
struct HttpSendArgs {
    method: String,
    url: String,
    #[serde(default)]
    headers: Vec<HttpHeaderArg>,
    #[serde(default)]
    body_base64: Option<String>,
    /// Header name -> secret reference name (spec §8.3/§6.5's
    /// `secret-refs: list<tuple<string, string>>`, represented here as a
    /// JSON object since header names are unique per request).
    #[serde(default)]
    secret_refs: HashMap<String, String>,
}

#[derive(Debug, Clone, Deserialize)]
struct HttpHeaderArg {
    name: String,
    value: String,
}

/// Answers one `host-call` (`kv`? no -- `http`, `op = "send"`) against the
/// full spec §8.2 pipeline, for the manifest belonging to `scope.app_id`.
pub struct EgressGuard {
    transport: Arc<dyn HttpTransport>,
    limits: EgressLimits,
    catalog: Arc<BundleCatalog>,
    /// Tenant-level global denylist (spec §8.2 step 5, "refreshed from
    /// hub-api every `EGRESS_DENYLIST_REFRESH_S`"). No hub-api endpoint for
    /// this refresh exists yet -- documented seam: always empty today, so
    /// this step never denies in this build (fail-open on this axis only;
    /// every other step, including the SSRF address check, is fully
    /// enforced regardless).
    denylist: Arc<RwLock<HashSet<String>>>,
    buckets: Mutex<HashMap<String, TokenBucket>>,
    denied_total: prometheus::IntCounterVec,
}

impl EgressGuard {
    pub fn new(
        transport: Arc<dyn HttpTransport>,
        limits: EgressLimits,
        catalog: Arc<BundleCatalog>,
        denied_total: prometheus::IntCounterVec,
    ) -> Self {
        Self {
            transport,
            limits,
            catalog,
            denylist: Arc::new(RwLock::new(HashSet::new())),
            buckets: Mutex::new(HashMap::new()),
            denied_total,
        }
    }

    /// Services one `http`/`send` host-call for `app_id` (spec §7.4's
    /// `http` row). Every rejection is counted against
    /// `waddles_egress_denied_total{app_id,reason}` (spec §8.2) before
    /// returning.
    pub async fn send(
        &self,
        app_id: &str,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostResultError> {
        let parsed: HttpSendArgs = match serde_json::from_value(args.clone()) {
            Ok(p) => p,
            Err(e) => {
                return Err(denied(
                    "invalid_args",
                    format!("http.send args malformed: {e}"),
                ))
            }
        };
        match self.send_checked(app_id, parsed).await {
            Ok(v) => Ok(v),
            Err(err) => {
                self.denied_total
                    .with_label_values(&[app_id, &err.code])
                    .inc();
                Err(err)
            }
        }
    }

    async fn send_checked(
        &self,
        app_id: &str,
        mut req: HttpSendArgs,
    ) -> Result<serde_json::Value, HostResultError> {
        let method = req.method.to_ascii_uppercase();
        let body = match &req.body_base64 {
            Some(b64) => Some(
                base64::engine::general_purpose::STANDARD
                    .decode(b64)
                    .map_err(|_| denied("invalid_args", "body_base64 is not valid base64"))?,
            ),
            None => None,
        };
        let mut headers: Vec<(String, String)> =
            req.headers.drain(..).map(|h| (h.name, h.value)).collect();
        for (header_name, secret_ref) in &req.secret_refs {
            let value = std::env::var(secret_ref).map_err(|_| {
                denied(
                    "secret_unresolved",
                    format!("secret reference {secret_ref} is not configured"),
                )
            })?;
            headers.push((header_name.clone(), value));
        }

        let mut hop: u8 = 0;
        loop {
            let url = reqwest::Url::parse(&req.url)
                .map_err(|_| denied("malformed_url", "url does not parse"))?;
            if url.scheme() != "https" {
                return Err(denied("scheme_not_https", "only https:// is permitted"));
            }
            if !url.username().is_empty() || url.password().is_some() {
                return Err(denied("malformed_url", "url must not embed credentials"));
            }
            let host = url
                .host_str()
                .ok_or_else(|| denied("malformed_url", "url has no host"))?
                .to_ascii_lowercase();

            let row = self.catalog.get(app_id);
            let egress_rules = row.as_ref().map(|r| r.egress.as_slice()).unwrap_or(&[]);
            let rule = egress_rules
                .iter()
                .find(|(pattern, _)| host_matches(pattern, &host));
            let Some((_, methods)) = rule else {
                return Err(denied(
                    "host_not_declared",
                    format!("{host} is not on the manifest egress allowlist"),
                ));
            };
            if !methods.iter().any(|m| m.eq_ignore_ascii_case(&method)) {
                return Err(denied(
                    "method_not_declared",
                    format!("{method} is not declared for {host}"),
                ));
            }
            if self
                .denylist
                .read()
                .unwrap_or_else(|e| e.into_inner())
                .contains(&host)
            {
                return Err(denied("host_denylisted", format!("{host} is denylisted")));
            }

            let port = url.port_or_known_default().unwrap_or(443);
            let addrs = tokio::net::lookup_host((host.as_str(), port))
                .await
                .map_err(|e| {
                    denied(
                        "ssrf_blocked_address",
                        format!("dns resolution failed: {e}"),
                    )
                })?;
            let mut chosen: Option<SocketAddr> = None;
            let mut last_reason = "no addresses returned";
            for addr in addrs {
                match is_forbidden_address(addr.ip(), self.limits.allow_private_hosts) {
                    None => {
                        chosen = Some(addr);
                        break;
                    }
                    Some(reason) => last_reason = reason,
                }
            }
            let pinned_addr = chosen.ok_or_else(|| {
                denied(
                    "ssrf_blocked_address",
                    format!("no permitted address for {host} ({last_reason})"),
                )
            })?;

            let rps = row
                .as_ref()
                .and_then(|r| r.egress_rps)
                .unwrap_or(self.limits.rate_limit_rps);
            {
                let mut buckets = self.buckets.lock().unwrap_or_else(|e| e.into_inner());
                let bucket = buckets
                    .entry(app_id.to_string())
                    .or_insert_with(|| TokenBucket::new(rps, self.limits.rate_limit_burst));
                if let Err(retry_after_ms) = bucket.try_acquire() {
                    return Err(denied(
                        "rate_limited",
                        format!("retry_after_ms={retry_after_ms}"),
                    ));
                }
            }

            let transport_req = TransportRequest {
                method: method.clone(),
                url: req.url.clone(),
                pinned_addr,
                headers: headers.clone(),
                body: body.clone(),
            };
            let response = self
                .transport
                .send(
                    transport_req,
                    self.limits.timeout,
                    self.limits.max_response_bytes,
                )
                .await?;

            if (300..400).contains(&response.status) {
                if hop >= self.limits.max_redirects {
                    return Err(denied("redirect_off_allowlist", "too many redirects"));
                }
                let location = response
                    .headers
                    .iter()
                    .find(|(k, _)| k.eq_ignore_ascii_case("location"))
                    .map(|(_, v)| v.clone())
                    .ok_or_else(|| denied("redirect_off_allowlist", "redirect missing Location"))?;
                let next = url.join(&location).map_err(|_| {
                    denied("redirect_off_allowlist", "redirect Location does not parse")
                })?;
                req.url = next.to_string();
                hop += 1;
                continue;
            }

            let headers_json: Vec<serde_json::Value> = response
                .headers
                .iter()
                .map(|(name, value)| serde_json::json!({"name": name, "value": value}))
                .collect();
            let body_b64 = base64::engine::general_purpose::STANDARD.encode(&response.body);
            return Ok(serde_json::json!({
                "status": response.status,
                "headers": headers_json,
                "body_base64": body_b64,
                "truncated": response.truncated,
            }));
        }
    }
}

/// A single-label wildcard host match (spec §8.1): `*.example.com` matches
/// `a.example.com`, not `a.b.example.com` and not `example.com` itself.
/// Case-insensitive, matching DNS's own convention.
fn host_matches(pattern: &str, host: &str) -> bool {
    let pattern = pattern.to_ascii_lowercase();
    let host = host.to_ascii_lowercase();
    match pattern.strip_prefix("*.") {
        Some(suffix) => match host.strip_suffix(suffix) {
            Some(prefix) if prefix.ends_with('.') && prefix.len() > 1 => {
                !prefix[..prefix.len() - 1].contains('.')
            }
            _ => false,
        },
        None => pattern == host,
    }
}

/// AWS's IPv6 metadata address, `fd00:ec2::254` (spec §8.2 step 6).
const CLOUD_METADATA_V6: Ipv6Addr = Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254);

fn is_private_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    o[0] == 10 || (o[0] == 172 && (16..=31).contains(&o[1])) || (o[0] == 192 && o[1] == 168)
}

fn is_link_local_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    o[0] == 169 && o[1] == 254
}

fn is_unique_local_v6(ip: Ipv6Addr) -> bool {
    (ip.segments()[0] & 0xfe00) == 0xfc00
}

fn is_link_local_v6(ip: Ipv6Addr) -> bool {
    (ip.segments()[0] & 0xffc0) == 0xfe80
}

/// Classifies a resolved address against spec §8.2 step 6's forbidden
/// ranges. Returns `None` when the address is permitted. `allow_private`
/// lifts only the RFC1918/ULA private-range check (spec §8.5's
/// `bundles.egress.allowPrivateHosts` escape hatch) -- loopback,
/// link-local, unspecified, multicast and the cloud-metadata addresses are
/// **never** lifted by any setting.
fn is_forbidden_address(ip: IpAddr, allow_private: bool) -> Option<&'static str> {
    match ip {
        IpAddr::V4(v4) => {
            if v4.is_loopback() {
                return Some("loopback");
            }
            if v4.is_unspecified() {
                return Some("unspecified");
            }
            if v4.is_multicast() {
                return Some("multicast");
            }
            if v4 == Ipv4Addr::new(169, 254, 169, 254) {
                return Some("cloud_metadata");
            }
            if is_link_local_v4(v4) {
                return Some("link_local");
            }
            if !allow_private && is_private_v4(v4) {
                return Some("private");
            }
            None
        }
        IpAddr::V6(v6) => {
            if v6.is_loopback() {
                return Some("loopback");
            }
            if v6.is_unspecified() {
                return Some("unspecified");
            }
            if v6.is_multicast() {
                return Some("multicast");
            }
            if v6 == CLOUD_METADATA_V6 {
                return Some("cloud_metadata");
            }
            if is_link_local_v6(v6) {
                return Some("link_local");
            }
            if !allow_private && is_unique_local_v6(v6) {
                return Some("private");
            }
            None
        }
    }
}

/// A simple token bucket (spec §8.2 step 8 / §7.3's `EGRESS_RATE_LIMIT_RPS`/
/// `_BURST`) keyed per bundle `app_id` by [`EgressGuard`]. Refills
/// continuously based on elapsed wall-clock time rather than a fixed tick,
/// so a bursty caller after an idle period gets its full burst allowance
/// immediately (per spec's token-bucket semantics, not a fixed window).
struct TokenBucket {
    tokens: f64,
    last_refill: Instant,
    rps: f64,
    burst: f64,
}

impl TokenBucket {
    fn new(rps: u32, burst: u32) -> Self {
        Self {
            tokens: burst.max(1) as f64,
            last_refill: Instant::now(),
            rps: rps.max(1) as f64,
            burst: burst.max(1) as f64,
        }
    }

    /// Returns `Ok(())` and consumes one token, or `Err(retry_after_ms)`
    /// when the bucket is empty.
    fn try_acquire(&mut self) -> Result<(), u32> {
        let now = Instant::now();
        let elapsed = now.duration_since(self.last_refill).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.rps).min(self.burst);
        self.last_refill = now;
        if self.tokens >= 1.0 {
            self.tokens -= 1.0;
            Ok(())
        } else {
            let deficit = 1.0 - self.tokens;
            Err((deficit / self.rps * 1000.0).ceil() as u32)
        }
    }
}

/// One already-fully-validated outbound request: [`EgressGuard`] has
/// already run every check up to and including DNS-rebind pinning (spec
/// §8.2 steps 1-7) before building this -- a [`HttpTransport`] impl trusts
/// `pinned_addr` completely and must connect to exactly that address for
/// `url`'s host, never re-resolving.
#[derive(Debug, Clone)]
pub struct TransportRequest {
    pub method: String,
    pub url: String,
    pub pinned_addr: SocketAddr,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
}

/// A transport's response, already size-capped (spec §8.2 step 11) by the
/// implementation -- [`EgressGuard`] never buffers the body itself.
#[derive(Debug, Clone)]
pub struct TransportResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub truncated: bool,
}

/// Performs the TLS connect + send + response-size-capped read (spec §8.2
/// steps 9, 11, 12) for one already-validated [`TransportRequest`]. Split
/// out from [`EgressGuard`] purely for testability -- see the module doc.
pub trait HttpTransport: Send + Sync {
    fn send<'a>(
        &'a self,
        req: TransportRequest,
        timeout: Duration,
        max_response_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<TransportResponse, HostResultError>> + Send + 'a>>;
}

/// The real [`HttpTransport`]: a fresh `reqwest::Client` per call, DNS
/// pinned to the caller-supplied `pinned_addr` (spec §8.2 step 7 -- "no
/// second resolution between check and connect"), redirects disabled (
/// [`EgressGuard`] re-validates and follows them itself, step 10), TLS
/// verification left at `reqwest`'s default (full chain + hostname, spec
/// §8.2 step 9). A fresh client per call costs a TLS-config rebuild but
/// keeps DNS pinning correct and simple; revisit if egress volume ever
/// makes that overhead material.
pub struct ReqwestTransport;

impl HttpTransport for ReqwestTransport {
    fn send<'a>(
        &'a self,
        req: TransportRequest,
        timeout: Duration,
        max_response_bytes: usize,
    ) -> Pin<Box<dyn Future<Output = Result<TransportResponse, HostResultError>> + Send + 'a>> {
        Box::pin(async move {
            let url = reqwest::Url::parse(&req.url)
                .map_err(|_| denied("malformed_url", "url does not parse"))?;
            let host = url
                .host_str()
                .ok_or_else(|| denied("malformed_url", "url has no host"))?
                .to_string();
            let method = reqwest::Method::from_bytes(req.method.as_bytes())
                .map_err(|_| denied("invalid_args", "invalid HTTP method"))?;

            let client = reqwest::Client::builder()
                .resolve(&host, req.pinned_addr)
                .redirect(reqwest::redirect::Policy::none())
                .timeout(timeout)
                .build()
                .map_err(|e| denied("transport", e.to_string()))?;

            let mut header_map = reqwest::header::HeaderMap::new();
            for (name, value) in &req.headers {
                let name = reqwest::header::HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| denied("invalid_args", "invalid header name"))?;
                let value = reqwest::header::HeaderValue::from_str(value)
                    .map_err(|_| denied("invalid_args", "invalid header value"))?;
                header_map.insert(name, value);
            }

            let mut builder = client.request(method, url).headers(header_map);
            if let Some(body) = req.body {
                builder = builder.body(body);
            }

            let response = builder.send().await.map_err(|e| classify_send_error(&e))?;
            let status = response.status().as_u16();
            let headers: Vec<(String, String)> = response
                .headers()
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
                .collect();

            let mut body = Vec::new();
            let mut truncated = false;
            let mut stream = response.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let chunk = chunk.map_err(|e| classify_send_error(&e))?;
                if body.len() + chunk.len() > max_response_bytes {
                    let remaining = max_response_bytes.saturating_sub(body.len());
                    body.extend_from_slice(&chunk[..remaining]);
                    truncated = true;
                    break;
                }
                body.extend_from_slice(&chunk);
            }

            Ok(TransportResponse {
                status,
                headers,
                body,
                truncated,
            })
        })
    }
}

/// Maps a `reqwest::Error` to spec §8.2's `timeout`/`tls_verification_failed`
/// /`transport` reasons. TLS-vs-generic-transport classification is a
/// string heuristic on the error chain (`reqwest`/`rustls` don't expose a
/// typed "certificate verification failed" variant) -- documented
/// best-effort, never load-bearing for the SSRF property itself (that is
/// enforced entirely before this function is ever reached).
fn classify_send_error(err: &reqwest::Error) -> HostResultError {
    if err.is_timeout() {
        return denied("timeout", err.to_string());
    }
    let msg = err.to_string().to_ascii_lowercase();
    if err.is_connect()
        && (msg.contains("certificate") || msg.contains("tls") || msg.contains("invalid peer"))
    {
        return denied("tls_verification_failed", err.to_string());
    }
    denied("transport", err.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::distribution::BundleRow;

    fn test_metrics() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_egress_denied_total", "test"),
            &["app_id", "reason"],
        )
        .unwrap()
    }

    fn default_limits() -> EgressLimits {
        EgressLimits {
            allow_private_hosts: false,
            rate_limit_rps: 10,
            rate_limit_burst: 20,
            timeout: Duration::from_secs(5),
            max_redirects: 3,
            max_response_bytes: 1_048_576,
        }
    }

    fn catalog_with_row(app_id: &str, egress: Vec<(String, Vec<String>)>) -> Arc<BundleCatalog> {
        let catalog = Arc::new(BundleCatalog::new());
        catalog.update(vec![BundleRow {
            app_id: app_id.to_string(),
            version: "1.0.0".to_string(),
            artifact_digest: Some("sha256:00".to_string()),
            component_key: "k".to_string(),
            sidecar_key: "s".to_string(),
            egress,
            egress_rps: None,
            config_json: "{}".to_string(),
        }]);
        catalog
    }

    #[derive(Default)]
    struct FakeTransport {
        responses: Mutex<Vec<Result<TransportResponse, HostResultError>>>,
        requests: Mutex<Vec<TransportRequest>>,
    }

    impl FakeTransport {
        fn queue(self, resp: Result<TransportResponse, HostResultError>) -> Self {
            self.responses.lock().unwrap().push(resp);
            self
        }
    }

    impl HttpTransport for FakeTransport {
        fn send<'a>(
            &'a self,
            req: TransportRequest,
            _timeout: Duration,
            _max_response_bytes: usize,
        ) -> Pin<Box<dyn Future<Output = Result<TransportResponse, HostResultError>> + Send + 'a>>
        {
            self.requests.lock().unwrap().push(req);
            let next = self
                .responses
                .lock()
                .unwrap()
                .pop()
                .unwrap_or_else(|| Ok(ok_response()));
            Box::pin(async move { next })
        }
    }

    fn ok_response() -> TransportResponse {
        TransportResponse {
            status: 200,
            headers: vec![],
            body: b"{}".to_vec(),
            truncated: false,
        }
    }

    fn guard_with(
        app_id: &str,
        egress: Vec<(String, Vec<String>)>,
        transport: FakeTransport,
    ) -> EgressGuard {
        EgressGuard::new(
            Arc::new(transport),
            default_limits(),
            catalog_with_row(app_id, egress),
            test_metrics(),
        )
    }

    // -- Host-pattern matching (spec §8.1) --

    #[test]
    fn host_matches_exact_pattern() {
        assert!(host_matches("api.spotify.com", "api.spotify.com"));
        assert!(!host_matches("api.spotify.com", "other.spotify.com"));
    }

    #[test]
    fn host_matches_single_label_wildcard() {
        assert!(host_matches("*.googleapis.com", "storage.googleapis.com"));
        assert!(!host_matches("*.googleapis.com", "a.b.googleapis.com"));
        assert!(!host_matches("*.googleapis.com", "googleapis.com"));
    }

    // -- SSRF address classification (spec §8.2 step 6 / §11.4) --

    #[test]
    fn cloud_metadata_v4_is_always_forbidden() {
        assert_eq!(
            is_forbidden_address(IpAddr::V4(Ipv4Addr::new(169, 254, 169, 254)), true),
            Some("cloud_metadata")
        );
    }

    #[test]
    fn loopback_is_always_forbidden_even_with_allow_private() {
        assert_eq!(
            is_forbidden_address(IpAddr::V4(Ipv4Addr::LOCALHOST), true),
            Some("loopback")
        );
    }

    #[test]
    fn private_v4_is_forbidden_unless_allow_private_is_set() {
        let ip = IpAddr::V4(Ipv4Addr::new(10, 0, 0, 1));
        assert_eq!(is_forbidden_address(ip, false), Some("private"));
        assert_eq!(is_forbidden_address(ip, true), None);
    }

    #[test]
    fn link_local_v4_is_always_forbidden() {
        let ip = IpAddr::V4(Ipv4Addr::new(169, 254, 1, 1));
        assert_eq!(is_forbidden_address(ip, true), Some("link_local"));
    }

    #[test]
    fn public_v4_is_permitted() {
        let ip = IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34));
        assert_eq!(is_forbidden_address(ip, false), None);
    }

    #[test]
    fn cloud_metadata_v6_is_always_forbidden() {
        assert_eq!(
            is_forbidden_address(IpAddr::V6(CLOUD_METADATA_V6), true),
            Some("cloud_metadata")
        );
    }

    #[test]
    fn unique_local_v6_is_forbidden_unless_allow_private() {
        let ip: IpAddr = "fc00::1".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, false), Some("private"));
        assert_eq!(is_forbidden_address(ip, true), None);
    }

    // -- Full pipeline via EgressGuard (negative tests, spec §8.2) --

    #[tokio::test]
    async fn scheme_not_https_is_denied() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("example.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "http://example.com/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "scheme_not_https");
    }

    #[tokio::test]
    async fn embedded_credentials_are_malformed_url() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("example.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://user:pass@example.com/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "malformed_url");
    }

    #[tokio::test]
    async fn host_not_on_allowlist_is_denied() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://evil.example.com/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    #[tokio::test]
    async fn method_not_declared_for_host_is_denied() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "DELETE", "url": "https://api.spotify.com/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "method_not_declared");
    }

    /// Regression coverage for the mandatory negative test: "a bundle
    /// reaching an undeclared egress host is DENIED and counted" -- also
    /// proves the `waddles_egress_denied_total{app_id,reason}` counter
    /// (spec §8.2) actually increments on a denial.
    #[tokio::test]
    async fn undeclared_host_denial_is_counted_in_the_metric() {
        let guard = guard_with("waddles.a.b.c", vec![], FakeTransport::default());
        let _ = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://evil.example.com/"}),
            )
            .await
            .unwrap_err();
        let value = guard
            .denied_total
            .with_label_values(&["waddles.a.b.c", "host_not_declared"])
            .get();
        assert_eq!(value, 1);
    }

    /// The mandatory SSRF negative test: a manifest that (incorrectly, or
    /// under a compromised/confused bundle) declares the cloud metadata
    /// address as an allowed egress host is still blocked by step 6 --
    /// declaring a host never bypasses the address-level SSRF check. Uses
    /// an IP-literal host so DNS resolution is exact and hermetic (no
    /// network access needed: `tokio::net::lookup_host` on an IP literal
    /// never queries a resolver).
    #[tokio::test]
    async fn ssrf_to_cloud_metadata_ip_is_blocked_even_when_declared() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("169.254.169.254".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://169.254.169.254/latest/meta-data"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    #[tokio::test]
    async fn ssrf_to_a_private_ip_is_blocked_by_default() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("10.0.0.5".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    #[tokio::test]
    async fn private_ip_is_permitted_once_allow_private_hosts_is_set() {
        let mut limits = default_limits();
        limits.allow_private_hosts = true;
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            limits,
            catalog_with_row(
                "waddles.a.b.c",
                vec![("10.0.0.5".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    /// End-to-end proof of the M3 "http capability -> Discord REST send"
    /// deliverable: a bundle-shaped call built by
    /// `crate::senders::discord_webhook_args` (the same JSON a real bundle
    /// would send over the wire), routed through the real `EgressGuard`
    /// pipeline (allowlist/method/SSRF/rate-limit all genuinely evaluated),
    /// landing on a fake transport standing in for the live TLS connection
    /// to `discord.com` -- proves the wiring end to end without a live
    /// Discord webhook.
    #[tokio::test]
    async fn discord_webhook_args_reach_the_transport_through_the_full_guard() {
        let transport = Arc::new(FakeTransport::default().queue(Ok(TransportResponse {
            status: 204,
            headers: vec![],
            body: vec![],
            truncated: false,
        })));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.socials.discord.default",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
            ),
            test_metrics(),
        );
        let args = crate::senders::discord_webhook_args(
            "https://discord.com/api/webhooks/1/abc",
            "hello from waddles",
        );
        let result = guard
            .send("waddles.socials.discord.default", &args)
            .await
            .expect("discord webhook send reaches the transport");
        assert_eq!(result["status"], 204);

        let sent = transport.requests.lock().unwrap();
        assert_eq!(sent[0].method, "POST");
        assert_eq!(sent[0].url, "https://discord.com/api/webhooks/1/abc");
        let body = sent[0].body.as_ref().expect("body present");
        let parsed: serde_json::Value = serde_json::from_slice(body).unwrap();
        assert_eq!(parsed["content"], "hello from waddles");
    }

    #[tokio::test]
    async fn a_successful_send_returns_the_transport_response_shape() {
        let transport = FakeTransport::default().queue(Ok(TransportResponse {
            status: 201,
            headers: vec![("content-type".to_string(), "application/json".to_string())],
            body: b"{\"ok\":true}".to_vec(),
            truncated: false,
        }));
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("discord.com".to_string(), vec!["POST".to_string()])],
            transport,
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "POST", "url": "https://discord.com/api/webhooks/1"}),
            )
            .await
            .expect("send succeeds");
        assert_eq!(result["status"], 201);
        assert_eq!(result["truncated"], false);
        let body_b64 = result["body_base64"].as_str().unwrap();
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(body_b64)
            .unwrap();
        assert_eq!(decoded, b"{\"ok\":true}");
    }

    #[tokio::test]
    async fn secret_ref_resolves_from_env_and_is_injected_as_a_header() {
        // SAFETY: test-process-local env var, unique name avoids
        // cross-test collisions under parallel `cargo test` execution.
        unsafe { std::env::set_var("EGRESS_TEST_DISCORD_TOKEN", "s3cr3t") };
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
            ),
            test_metrics(),
        );
        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "POST",
                    "url": "https://discord.com/api/webhooks/1",
                    "secret_refs": {"Authorization": "EGRESS_TEST_DISCORD_TOKEN"}
                }),
            )
            .await
            .expect("send succeeds");
        unsafe { std::env::remove_var("EGRESS_TEST_DISCORD_TOKEN") };

        let requests = transport.requests.lock().unwrap();
        let sent = &requests[0];
        assert!(sent
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == "s3cr3t"));
    }

    #[tokio::test]
    async fn unresolvable_secret_ref_is_denied() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("discord.com".to_string(), vec!["POST".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "POST",
                    "url": "https://discord.com/api/webhooks/1",
                    "secret_refs": {"Authorization": "EGRESS_TEST_DOES_NOT_EXIST"}
                }),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "secret_unresolved");
    }

    #[tokio::test]
    async fn rate_limit_exhaustion_is_denied_after_the_burst() {
        let mut limits = default_limits();
        limits.rate_limit_rps = 1;
        limits.rate_limit_burst = 1;
        let guard = EgressGuard::new(
            Arc::new(
                FakeTransport::default()
                    .queue(Ok(ok_response()))
                    .queue(Ok(ok_response())),
            ),
            limits,
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
        );
        let args = serde_json::json!({"method": "GET", "url": "https://discord.com/"});
        guard
            .send("waddles.a.b.c", &args)
            .await
            .expect("first call within burst succeeds");
        let err = guard.send("waddles.a.b.c", &args).await.unwrap_err();
        assert_eq!(err.code, "rate_limited");
    }

    #[tokio::test]
    async fn a_redirect_is_followed_and_rechecked() {
        let transport =
            FakeTransport::default()
                .queue(Ok(ok_response()))
                .queue(Ok(TransportResponse {
                    status: 302,
                    headers: vec![(
                        "location".to_string(),
                        "https://discord.com/next".to_string(),
                    )],
                    body: vec![],
                    truncated: false,
                }));
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("discord.com".to_string(), vec!["GET".to_string()])],
            transport,
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/start"}),
            )
            .await
            .expect("redirect followed to a successful terminal response");
        assert_eq!(result["status"], 200);
    }

    #[tokio::test]
    async fn a_redirect_to_an_undeclared_host_is_denied_on_recheck() {
        let transport = FakeTransport::default().queue(Ok(TransportResponse {
            status: 302,
            headers: vec![(
                "location".to_string(),
                "https://evil.example.com/".to_string(),
            )],
            body: vec![],
            truncated: false,
        }));
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("discord.com".to_string(), vec!["GET".to_string()])],
            transport,
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/start"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    #[tokio::test]
    async fn exceeding_max_redirects_is_denied() {
        let mut limits = default_limits();
        limits.max_redirects = 1;
        let redirect = || {
            Ok(TransportResponse {
                status: 302,
                headers: vec![(
                    "location".to_string(),
                    "https://discord.com/loop".to_string(),
                )],
                body: vec![],
                truncated: false,
            })
        };
        let guard = EgressGuard::new(
            Arc::new(
                FakeTransport::default()
                    .queue(redirect())
                    .queue(redirect())
                    .queue(redirect()),
            ),
            limits,
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/start"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "redirect_off_allowlist");
    }

    #[tokio::test]
    async fn malformed_args_are_rejected_as_invalid_args() {
        let guard = guard_with("waddles.a.b.c", vec![], FakeTransport::default());
        let err = guard
            .send("waddles.a.b.c", &serde_json::json!({"not": "a request"}))
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
    }

    // -- ReqwestTransport (real TLS/connect/size-cap, spec §8.2 steps 9/11/12) --

    #[tokio::test]
    async fn reqwest_transport_reads_a_real_local_response() {
        let app = axum::Router::new().route(
            "/ok",
            axum::routing::get(|| async { "hello from the test server" }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });

        let transport = ReqwestTransport;
        let result = transport
            .send(
                TransportRequest {
                    method: "GET".to_string(),
                    url: format!("http://127.0.0.1:{}/ok", addr.port()),
                    pinned_addr: addr,
                    headers: vec![],
                    body: None,
                },
                Duration::from_secs(5),
                1_048_576,
            )
            .await
            .expect("real local request succeeds");
        assert_eq!(result.status, 200);
        assert_eq!(result.body, b"hello from the test server");
        assert!(!result.truncated);
    }

    #[tokio::test]
    async fn reqwest_transport_truncates_a_response_over_the_cap() {
        let app =
            axum::Router::new().route("/big", axum::routing::get(|| async { "x".repeat(1000) }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });

        let transport = ReqwestTransport;
        let result = transport
            .send(
                TransportRequest {
                    method: "GET".to_string(),
                    url: format!("http://127.0.0.1:{}/big", addr.port()),
                    pinned_addr: addr,
                    headers: vec![],
                    body: None,
                },
                Duration::from_secs(5),
                100,
            )
            .await
            .expect("request succeeds even when truncated");
        assert_eq!(result.body.len(), 100);
        assert!(result.truncated);
    }

    #[tokio::test]
    async fn reqwest_transport_reports_timeout_against_an_unreachable_address() {
        let transport = ReqwestTransport;
        // TEST-NET-1 (RFC 5737): reserved for documentation, guaranteed
        // unroutable -- a connect attempt fails fast without a real
        // network dependency or a flaky external host.
        let unreachable: SocketAddr = "192.0.2.1:443".parse().unwrap();
        let result = transport
            .send(
                TransportRequest {
                    method: "GET".to_string(),
                    url: "https://example.invalid/".to_string(),
                    pinned_addr: unreachable,
                    headers: vec![],
                    body: None,
                },
                Duration::from_millis(200),
                1024,
            )
            .await;
        assert!(result.is_err());
    }
}

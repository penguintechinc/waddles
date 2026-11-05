//! Rule-free SSRF-guarded outbound HTTP for host-initiated egress.
//!
//! [`crate::egress::EgressGuard`] enforces per-bundle declared-host rules.
//! Host-initiated egress whose destination comes from operator/overlay
//! config rather than a bundle manifest (svc_ingest's poll/WebSocket
//! connector transports) needs the same *network-level* guarantees without
//! a rule source. This module is that single implementation, so services
//! never re-mirror the denylist:
//!
//! - scheme `https`/`wss` only (plain `http`/`ws` solely to an EXACT
//!   allowlisted ip -- the test/dev loopback escape hatch),
//! - no userinfo (`https://a@127.0.0.1/` style host confusion),
//! - resolve-then-connect pinning: every resolved address is checked, the
//!   connection is made to the pinned address, the client never re-resolves
//!   (closes DNS rebinding),
//! - IPv4-mapped/NAT64/compatible IPv6 judged by the embedded v4 rules,
//! - every redirect hop is re-validated and re-pinned; only an allowlist of safe headers
//!   (Accept, Accept-Language, User-Agent, Content-Type) and no body are
//!   forwarded when a hop changes scheme/host/port,
//! - wall-clock and response-size bounds,
//! - typed [`GuardError`]s whose `Display` carries the failure *class*
//!   only -- never the host, address, URL or any header value.

use std::future::Future;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

use tracing::Instrument;

use crate::egress::{embedded_ipv4, is_forbidden_address};

/// DNS lookup ceiling.
const RESOLVE_TIMEOUT: Duration = Duration::from_secs(5);

/// Hard ceiling on followed redirects regardless of the configured limit, so
/// a hostile or mistaken `max_redirects` can never make the hop counter
/// overflow or the chain effectively unbounded.
pub const MAX_REDIRECTS_CEILING: usize = 10;

/// Body-framing headers that describe a body and must not survive its removal
/// (a stale `Content-Length` with no body desyncs the wire framing).
const BODY_HEADERS: [&str; 4] = [
    "content-length",
    "content-type",
    "transfer-encoding",
    "content-encoding",
];

/// The ONLY request headers forwarded across an origin change (allowlist:
/// any other header may carry a credential -- X-API-Key, Client-Id, custom
/// overlay auth -- so unknown means dropped).
const CROSS_ORIGIN_FORWARDED: [&str; 4] =
    ["accept", "accept-language", "user-agent", "content-type"];

/// Typed refusal/failure from the guarded egress path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuardError {
    /// The URL does not parse or has no host.
    MalformedUrl,
    /// Scheme is not `https`/`wss` (or plain http without an exact allowlisted ip).
    SchemeNotAllowed,
    /// The URL carries userinfo.
    UserInfoForbidden,
    /// DNS lookup timed out.
    DnsTimeout,
    /// DNS lookup failed.
    DnsFailed,
    /// The host resolved to no address.
    NoAddress,
    /// An address is in a forbidden range; the payload is the range class.
    ForbiddenAddress(&'static str),
    /// A redirect's `Location` could not be used.
    BadRedirect,
    /// A redirect that must keep the request body crosses origin; the body is
    /// never forwarded to another origin, so the request is refused instead.
    BodyCrossOrigin,
    /// The redirect chain exceeded the configured limit.
    TooManyRedirects,
    /// The overall deadline elapsed.
    Timeout,
    /// The response body exceeded the configured ceiling.
    ResponseTooLarge,
    /// Request construction or transport failure (kind only).
    Transport(&'static str),
}

impl std::fmt::Display for GuardError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::MalformedUrl => f.write_str("malformed endpoint url"),
            Self::SchemeNotAllowed => f.write_str("endpoint scheme not allowed"),
            Self::UserInfoForbidden => f.write_str("endpoint must not carry userinfo"),
            Self::DnsTimeout => f.write_str("dns lookup timed out"),
            Self::DnsFailed => f.write_str("dns lookup failed"),
            Self::NoAddress => f.write_str("host resolved to no address"),
            // Class only -- never the address or host.
            Self::ForbiddenAddress(_) => {
                f.write_str("endpoint resolves to a forbidden address range")
            }
            Self::BadRedirect => f.write_str("invalid redirect"),
            Self::BodyCrossOrigin => {
                f.write_str("redirect would carry the request body to another origin")
            }
            Self::TooManyRedirects => f.write_str("too many redirects"),
            Self::Timeout => f.write_str("egress deadline exceeded"),
            Self::ResponseTooLarge => f.write_str("response over size ceiling"),
            Self::Transport(kind) => write!(f, "egress transport error ({kind})"),
        }
    }
}

impl std::error::Error for GuardError {}

impl GuardError {
    /// Stable low-cardinality label for metrics/logs.
    #[must_use]
    pub fn class(&self) -> &'static str {
        match self {
            Self::MalformedUrl => "malformed_url",
            Self::SchemeNotAllowed => "scheme",
            Self::UserInfoForbidden => "userinfo",
            Self::DnsTimeout => "dns_timeout",
            Self::DnsFailed => "dns_failed",
            Self::NoAddress => "no_address",
            Self::ForbiddenAddress(_) => "forbidden_address",
            Self::BadRedirect => "bad_redirect",
            Self::BodyCrossOrigin => "body_cross_origin",
            Self::TooManyRedirects => "too_many_redirects",
            Self::Timeout => "timeout",
            Self::ResponseTooLarge => "too_large",
            Self::Transport(_) => "transport",
        }
    }
}

static OUTCOMES: LazyLock<Option<prometheus::IntCounterVec>> = LazyLock::new(|| {
    prometheus::register_int_counter_vec!(
        "guarded_egress_total",
        "Guarded host-initiated egress attempts by outcome class",
        &["outcome"]
    )
    .ok()
});

/// Counts one outcome. A registry conflict degrades to no metric, never a failure.
fn record(outcome: &str) {
    if let Some(c) = OUTCOMES.as_ref() {
        c.with_label_values(&[outcome]).inc();
    }
}

fn forbidden_v4_extra(v: Ipv4Addr) -> Option<&'static str> {
    let o = v.octets();
    if o[0] == 0 {
        Some("unspecified")
    } else if o[0] >= 240 {
        Some("reserved")
    } else if (o[0] == 192 && o[1] == 0 && o[2] == 0)
        || (o[0] == 192 && o[1] == 88 && o[2] == 99)
        || (o[0] == 198 && (o[1] & 0xFE) == 18)
        || v.is_documentation()
    {
        Some("special_use")
    } else {
        None
    }
}

fn forbidden_v6_extra(v: Ipv6Addr) -> Option<&'static str> {
    let s = v.segments();
    let s0 = s[0];
    if (s0 & 0xFFC0) == 0xFEC0 {
        Some("site_local")
    } else if (s0 & 0xFE00) == 0xFC00 {
        Some("unique_local")
    } else if s[..4] == [0x100, 0, 0, 0] {
        Some("discard")
    } else if s[..3] == [0x64, 0xff9b, 1] {
        Some("local_nat64")
    } else if s0 == 0x2001 && (s[1] < 0x200 || s[1] == 0x0db8) {
        Some("special_use")
    } else if s0 == 0x2002 {
        Some("6to4")
    } else if s0 == 0x3fff && s[1] < 0x1000 {
        Some("documentation")
    } else if s0 == 0x5f00 {
        Some("special_use")
    } else if (s0 & 0xE000) != 0x2000 {
        // Only 2000::/3 is global unicast; everything else is reserved or
        // unassigned (4000::/3, 6000::/3 ... e000::/3, ::/3 remnants).
        Some("reserved")
    } else {
        None
    }
}

/// Why `ip` may not be dialed by guarded host egress, or `None` for a
/// genuine global-unicast address. Strictly tighter than
/// [`is_forbidden_address`]`(ip, false)`: additionally refuses reserved,
/// documentation, benchmarking, 6to4, site-local and discard ranges. No
/// private range is ever lifted here -- only an exact-ip allowlist can.
#[must_use]
pub fn forbidden_reason(ip: IpAddr) -> Option<&'static str> {
    if let Some(r) = is_forbidden_address(ip, false) {
        return Some(r);
    }
    match ip {
        IpAddr::V4(v) => forbidden_v4_extra(v),
        IpAddr::V6(v) => match embedded_ipv4(v) {
            Some(v4) => forbidden_v4_extra(v4),
            None => forbidden_v6_extra(v),
        },
    }
}

/// Validates userinfo + scheme. `plain_ok` permits http/ws (the caller
/// proves the target ip is on the exact allowlist).
fn check_scheme(url: &reqwest::Url, plain_ok: bool) -> Result<(), GuardError> {
    if !url.username().is_empty() || url.password().is_some() {
        return Err(GuardError::UserInfoForbidden);
    }
    match url.scheme() {
        "https" | "wss" => Ok(()),
        "http" | "ws" if plain_ok => Ok(()),
        _ => Err(GuardError::SchemeNotAllowed),
    }
}

/// Boxed future returned by [`HostResolver::lookup`].
pub type ResolveFuture<'a> =
    Pin<Box<dyn Future<Output = std::io::Result<Vec<IpAddr>>> + Send + 'a>>;

/// Name-resolution seam. Whatever it returns is still run through the full
/// address policy and pinned, so a resolver can never widen what is reachable;
/// it exists so tests can record lookups and simulate DNS rebinding.
pub trait HostResolver: Send + Sync + std::fmt::Debug {
    /// Resolves `host` (canonical, no brackets) for `port`.
    fn lookup<'a>(&'a self, host: &'a str, port: u16) -> ResolveFuture<'a>;
}

/// Default resolver: the system resolver via tokio.
#[derive(Debug)]
struct SystemResolver;

impl HostResolver for SystemResolver {
    fn lookup<'a>(&'a self, host: &'a str, port: u16) -> ResolveFuture<'a> {
        Box::pin(async move {
            let it = tokio::net::lookup_host((host, port)).await?;
            Ok(it.map(|sa| sa.ip()).collect())
        })
    }
}

/// The URL host with exactly one pair of IPv6 brackets removed (never more).
fn unbracket(host: &str) -> &str {
    host.strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host)
}

/// Parses the host as an IP literal, if it is one.
fn literal_ip(host: &str) -> Option<IpAddr> {
    unbracket(host).parse::<IpAddr>().ok()
}

/// Rewrites the URL's host to its canonical form (one trailing dot removed;
/// the URL parser has already lowercased and IDNA-to-ASCII'd it) so the
/// guard, the pin and the request all see the byte-identical host.
fn canonicalize(url: &mut reqwest::Url) -> Result<(), GuardError> {
    let Some(stripped) = url
        .host_str()
        .and_then(|h| h.strip_suffix('.'))
        .map(str::to_owned)
    else {
        return Ok(());
    };
    url.set_host(Some(&stripped))
        .map_err(|_| GuardError::MalformedUrl)
}

/// Resolves the URL's host, requires EVERY address to be permitted (or
/// exactly allowlisted) and returns the address to pin the connection to.
///
/// # Errors
/// [`GuardError`] for a missing host/port, DNS failure/timeout, or any
/// forbidden resolved address.
pub async fn resolve_pinned(
    url: &reqwest::Url,
    allow: &[IpAddr],
) -> Result<SocketAddr, GuardError> {
    resolve_pinned_with(&SystemResolver, url, allow).await
}

async fn resolve_pinned_with(
    resolver: &dyn HostResolver,
    url: &reqwest::Url,
    allow: &[IpAddr],
) -> Result<SocketAddr, GuardError> {
    let port = url
        .port_or_known_default()
        .ok_or(GuardError::MalformedUrl)?;
    let host = url.host_str().ok_or(GuardError::MalformedUrl)?;
    let addrs: Vec<IpAddr> = if let Some(ip) = literal_ip(host) {
        vec![ip]
    } else {
        let name = host.strip_suffix('.').unwrap_or(host);
        tokio::time::timeout(RESOLVE_TIMEOUT, resolver.lookup(name, port))
            .await
            .map_err(|_| GuardError::DnsTimeout)?
            .map_err(|_| GuardError::DnsFailed)?
    };
    let first = *addrs.first().ok_or(GuardError::NoAddress)?;
    for ip in &addrs {
        if let Some(class) = forbidden_reason(*ip) {
            if !allow.contains(ip) {
                return Err(GuardError::ForbiddenAddress(class));
            }
        }
    }
    Ok(SocketAddr::new(first, port))
}

/// Scheme + resolve + pin for one URL; returns the pinned address. Plain
/// http/ws is accepted only when the pinned ip is exactly allowlisted. Every
/// check that needs no DNS (userinfo, scheme, empty allowlist for plain
/// http/ws to a name) runs BEFORE the lookup, so a refused URL never makes
/// the resolver an oracle.
async fn validate_target(
    resolver: &dyn HostResolver,
    url: &reqwest::Url,
    allow: &[IpAddr],
) -> Result<SocketAddr, GuardError> {
    let plain = matches!(url.scheme(), "http" | "ws");
    let named = url.host_str().is_some_and(|h| literal_ip(h).is_none());
    check_scheme(url, plain && !(allow.is_empty() && named))?;
    let pinned = resolve_pinned_with(resolver, url, allow).await?;
    if plain && !allow.contains(&pinned.ip()) {
        return Err(GuardError::SchemeNotAllowed);
    }
    Ok(pinned)
}

/// Builds a one-shot `reqwest` client pinned to the validated address:
/// redirects off, ambient proxies off (they would resolve/connect the host
/// themselves and bypass the pin), explicit timeout. Crate-private on
/// purpose: a raw client is usable against ANY host, so it never leaves this
/// module; callers get [`GuardedClient`] instead.
fn build_pinned(
    url: &reqwest::Url,
    addr: SocketAddr,
    timeout: Duration,
) -> Result<reqwest::Client, GuardError> {
    let mut b = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .timeout(timeout);
    if let Some(host) = url.host_str() {
        b = b.resolve(unbracket(host), addr);
    }
    b.build().map_err(|_| GuardError::Transport("client build"))
}

/// Bounds for [`GuardedClient`].
#[derive(Debug, Clone)]
pub struct GuardedLimits {
    /// Max redirects followed (0 = a 3xx is returned as-is, never followed);
    /// clamped to [`MAX_REDIRECTS_CEILING`].
    pub max_redirects: usize,
    /// Overall wall-clock deadline across all hops.
    pub timeout: Duration,
    /// Response body ceiling in bytes.
    pub max_response_bytes: usize,
}

impl Default for GuardedLimits {
    fn default() -> Self {
        Self {
            max_redirects: 3,
            timeout: Duration::from_secs(15),
            max_response_bytes: 1024 * 1024,
        }
    }
}

/// A finished guarded fetch.
#[derive(Debug)]
pub struct GuardedResponse {
    /// Final HTTP status.
    pub status: u16,
    /// Final response headers.
    pub headers: Vec<(String, String)>,
    /// Body, at most `max_response_bytes`.
    pub body: Vec<u8>,
}

/// SSRF-guarded HTTP client for host-initiated egress. See the module doc.
#[derive(Debug, Clone)]
pub struct GuardedClient {
    limits: GuardedLimits,
    allow: Vec<IpAddr>,
    resolver: Arc<dyn HostResolver>,
}

impl GuardedClient {
    /// `allow` lists exact ips exempted from the denylist (and permitted
    /// plain http); empty in production.
    #[must_use]
    pub fn new(limits: GuardedLimits, allow: Vec<IpAddr>) -> Self {
        Self {
            limits,
            allow,
            resolver: Arc::new(SystemResolver),
        }
    }

    /// Replaces the name resolver (tests: record lookups, simulate rebinding).
    /// The policy still applies to every address the resolver returns.
    #[must_use]
    pub fn with_resolver(mut self, resolver: Arc<dyn HostResolver>) -> Self {
        self.resolver = resolver;
        self
    }

    /// Performs `method url` with `headers`/`body`, following redirects
    /// only through full re-validation + re-pinning.
    ///
    /// # Errors
    /// A typed [`GuardError`]; header values never appear in it.
    pub async fn fetch(
        &self,
        method: reqwest::Method,
        url: &str,
        headers: Vec<(String, String)>,
        body: Option<Vec<u8>>,
    ) -> Result<GuardedResponse, GuardError> {
        let span = tracing::info_span!("guarded_egress.fetch");
        let res = tokio::time::timeout(
            self.limits.timeout,
            self.run(method, url, headers, body).instrument(span),
        )
        .await
        .map_err(|_| GuardError::Timeout)
        .and_then(|r| r);
        match &res {
            Ok(_) => record("ok"),
            Err(e) => {
                record(e.class());
                tracing::warn!(class = e.class(), "guarded egress refused or failed");
            }
        }
        res
    }

    async fn run(
        &self,
        method: reqwest::Method,
        url: &str,
        mut headers: Vec<(String, String)>,
        mut body: Option<Vec<u8>>,
    ) -> Result<GuardedResponse, GuardError> {
        let mut method = method;
        let mut cur = reqwest::Url::parse(url).map_err(|_| GuardError::MalformedUrl)?;
        canonicalize(&mut cur)?;
        let max_hops = self.limits.max_redirects.min(MAX_REDIRECTS_CEILING);
        let mut hops = 0usize;
        loop {
            let pinned = validate_target(self.resolver.as_ref(), &cur, &self.allow).await?;
            let client = build_pinned(&cur, pinned, self.limits.timeout)?;
            let mut req = client.request(method.clone(), cur.clone());
            for (k, v) in &headers {
                let name = reqwest::header::HeaderName::from_bytes(k.as_bytes())
                    .map_err(|_| GuardError::Transport("header name"))?;
                let mut val = reqwest::header::HeaderValue::from_str(v)
                    .map_err(|_| GuardError::Transport("header value"))?;
                val.set_sensitive(true);
                req = req.header(name, val);
            }
            if let Some(b) = &body {
                req = req.body(b.clone());
            }
            let resp = req.send().await.map_err(|e| {
                GuardError::Transport(if e.is_timeout() {
                    "timeout"
                } else {
                    "request failed"
                })
            })?;
            let status = resp.status();
            let code = status.as_u16();
            // Only 301/302/303/307/308 carrying a Location are redirects;
            // 300/304/305/306 and a Location-less 3xx are final responses.
            let location = if max_hops > 0 && matches!(code, 301 | 302 | 303 | 307 | 308) {
                match resp.headers().get(reqwest::header::LOCATION) {
                    Some(v) => Some(v.to_str().map_err(|_| GuardError::BadRedirect)?),
                    None => None,
                }
            } else {
                None
            };
            if let Some(loc) = location {
                hops = hops.checked_add(1).ok_or(GuardError::TooManyRedirects)?;
                if hops > max_hops {
                    return Err(GuardError::TooManyRedirects);
                }
                let mut next = cur.join(loc).map_err(|_| GuardError::BadRedirect)?;
                canonicalize(&mut next)?;
                let same_origin = next.scheme() == cur.scheme()
                    && next.host_str() == cur.host_str()
                    && next.port_or_known_default() == cur.port_or_known_default();
                // RFC 9110 15.4: 301/302 turn POST into GET, 303 turns every
                // method but HEAD into GET, 307/308 never change the method.
                let to_get = match code {
                    301 | 302 => method == reqwest::Method::POST,
                    303 => method != reqwest::Method::GET && method != reqwest::Method::HEAD,
                    _ => false,
                };
                if to_get {
                    method = reqwest::Method::GET;
                    body = None;
                    headers
                        .retain(|(k, _)| !BODY_HEADERS.contains(&k.to_ascii_lowercase().as_str()));
                }
                if !same_origin {
                    // The body may itself be a secret: never forwarded to
                    // another origin, so a hop that must keep it is refused.
                    if body.is_some() {
                        return Err(GuardError::BodyCrossOrigin);
                    }
                    headers.retain(|(k, _)| {
                        CROSS_ORIGIN_FORWARDED.contains(&k.to_ascii_lowercase().as_str())
                    });
                }
                cur = next;
                continue;
            }
            let out_headers: Vec<(String, String)> = resp
                .headers()
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
                .collect();
            if resp
                .content_length()
                .is_some_and(|n| n > self.limits.max_response_bytes as u64)
            {
                return Err(GuardError::ResponseTooLarge);
            }
            let mut out = Vec::new();
            let mut resp = resp;
            while let Some(chunk) = resp
                .chunk()
                .await
                .map_err(|_| GuardError::Transport("body read"))?
            {
                if out.len() + chunk.len() > self.limits.max_response_bytes {
                    return Err(GuardError::ResponseTooLarge);
                }
                out.extend_from_slice(&chunk);
            }
            return Ok(GuardedResponse {
                status: status.as_u16(),
                headers: out_headers,
                body: out,
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // regression: gh-989 -- strip exactly one bracket pair, never over-strip.
    #[test]
    fn unbracket_strips_one_pair_only() {
        assert_eq!(unbracket("[::1]"), "::1");
        assert_eq!(unbracket("[[::1]]"), "[::1]");
        assert_eq!(unbracket("[::1"), "[::1");
        assert_eq!(unbracket("::1]"), "::1]");
        assert!(literal_ip("[[::1]]").is_none());
        assert_eq!(literal_ip("[::1]"), Some("::1".parse().unwrap()));
        assert_eq!(literal_ip("1.2.3.4"), Some("1.2.3.4".parse().unwrap()));
        assert!(literal_ip("example.com").is_none());
    }

    #[test]
    fn canonicalize_strips_one_trailing_dot_and_lowercases() {
        let mut u = reqwest::Url::parse("https://EXAMPLE.com./x").unwrap();
        canonicalize(&mut u).unwrap();
        assert_eq!(u.host_str(), Some("example.com"));
        let mut u = reqwest::Url::parse("https://[::1]/x").unwrap();
        canonicalize(&mut u).unwrap();
        assert_eq!(u.host_str(), Some("[::1]"));
        let mut u = reqwest::Url::parse("https://a../x").unwrap();
        canonicalize(&mut u).unwrap();
        assert_eq!(u.host_str(), Some("a."));
        let mut u = reqwest::Url::parse("https://./x").unwrap();
        assert_eq!(canonicalize(&mut u), Err(GuardError::MalformedUrl));
    }

    #[test]
    fn display_and_class_cover_every_variant() {
        for e in [
            GuardError::BodyCrossOrigin,
            GuardError::BadRedirect,
            GuardError::Transport("k"),
        ] {
            assert!(!e.to_string().is_empty());
            assert!(!e.class().is_empty());
        }
    }
}

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
//! **Extracted from `core/svc_action` (PR #459) into this shared crate**
//! so `core/svc_process` can wire its own `http` bundle capability against
//! the identical, already-hardened pipeline instead of a second copy. See
//! `crate` module doc for the consuming-crate seam
//! ([`EgressRuleSource`]/[`FeatureFlag`]) and the deferred-scope notes.
//!
//! **Split for testability, matching the per-dependency trait pattern this
//! pipeline was extracted with**: [`EgressGuard`] owns steps 1-8 and the
//! redirect-recheck loop (spec-security-critical, exercised in
//! [`tests`] against literal loopback/private/metadata IPs with no
//! network access at all); [`HttpTransport`] owns the actual TLS
//! connect/send/response-size-cap (steps 9, 11, 12), pinned to the exact
//! address [`EgressGuard`] already validated (step 7 -- "no second
//! resolution between check and connect").
//!
//! **§8.5 scope reminder:** this module governs bundle-initiated `http.send`
//! calls only. Each stage's own connections to Postgres/Valkey/the bucket/
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

fn denied(code: &str, message: impl Into<String>) -> HostResultError {
    HostResultError {
        code: code.to_string(),
        message: message.into(),
    }
}

/// One flag's live enabled/disabled state -- the same per-dependency-seam
/// pattern this crate uses for every other external dependency
/// (`HttpTransport`, `CredentialBroker`, `Resolver`). Object-safe (a
/// manually-boxed future rather than `async fn` in a trait) so callers can
/// hold `Arc<dyn FeatureFlag>` without an `async_trait` dependency. Each
/// consuming crate wires its own real implementation over
/// `penguin_licensing::LicenseClient` (`svc_action::flags::LicenseFlag`,
/// `svc_process`'s equivalent) and re-exports/implements this trait
/// directly -- this crate depends only on the trait, never on
/// `penguin_licensing`.
pub trait FeatureFlag: Send + Sync {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>>;
}

/// A [`FeatureFlag`] that never varies -- this crate's own test fixture,
/// and available to consumers as a dependency-free fallback value.
pub struct StaticFlag(pub bool);

impl FeatureFlag for StaticFlag {
    fn enabled<'a>(&'a self) -> Pin<Box<dyn Future<Output = bool> + Send + 'a>> {
        let value = self.0;
        Box::pin(async move { value })
    }
}

/// Type-erased convenience, mirroring this crate's other `Arc<dyn Trait>`
/// seams.
pub fn boxed(flag: impl FeatureFlag + 'static) -> Arc<dyn FeatureFlag> {
    Arc::new(flag)
}

/// One bundle's egress-relevant state that [`EgressGuard`] consults per
/// `http.send` call: the manifest `egress` allowlist (`(host_pattern,
/// methods)`, byte-identical shape to
/// `penguin_bundle_host::manifest::Manifest::egress`), its per-bundle rate
/// override, and its granted secret-reference map (spec §8.3). Each
/// consuming crate's own bundle-snapshot type implements
/// [`EgressRuleSource`] over its existing storage rather than this crate
/// owning a second copy of that state.
#[derive(Debug, Clone, Default)]
pub struct EgressRuleRow {
    pub egress: Vec<(String, Vec<String>)>,
    pub egress_rps: Option<u32>,
    pub granted_secret_refs: HashMap<String, String>,
}

/// Resolves one `app_id`'s current [`EgressRuleRow`]. `None` -- an
/// `app_id` this source has no snapshot for at all -- is treated by
/// [`EgressGuard`] identically to a snapshot with an empty `egress` list:
/// every `http.send` call is refused `host_not_declared`, the fail-closed
/// posture documented on [`EgressGuard::catalog`].
pub trait EgressRuleSource: Send + Sync {
    fn resolve(&self, app_id: &str) -> Option<EgressRuleRow>;
}

/// Operator-tunable limits (spec §7.3/§8.2), sourced from each consuming
/// crate's own CLI config.
#[derive(Debug, Clone)]
pub struct EgressLimits {
    pub allow_private_hosts: bool,
    pub rate_limit_rps: u32,
    pub rate_limit_burst: u32,
    pub timeout: Duration,
    pub max_redirects: u8,
    pub max_response_bytes: usize,
    /// Port allowlist (connector spec `net.http:<host>`, Gemini condition 2:
    /// "a port allowlist (443 by default)"). Not yet CLI-tunable -- every
    /// caller sets this to `vec![443]` today; a follow-up wires an
    /// `--egress-allowed-ports` flag through each consuming crate's config
    /// the same way its other `egress_*` fields already are.
    pub allowed_ports: Vec<u16>,
    /// Upstream network-level egress gateway/proxy this guard's already-
    /// validated requests should be dialed through, instead of connecting
    /// directly to the pinned address -- see the crate module doc's
    /// "Upstream egress proxy" section. `None` (the default) is the only
    /// value either consuming crate constructs today and preserves this
    /// guard's pre-existing direct-connect behavior exactly; only
    /// [`ReqwestTransport`] reads this field.
    pub proxy_url: Option<String>,
}

/// One `http.send` request as decoded from a bundle's host-call `args`
/// (this crate's own JSON-wire convention for the WIT `http::request`
/// record of spec §6.5). `body`/response `body` are base64 rather than a
/// JSON byte array or lossy UTF-8, to stay byte-exact with the WIT
/// `list<u8>` without a JSON array of small integers.
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

/// Answers one `host-call` (`http`, `op = "send"`) against the full spec
/// §8.2 pipeline, for the manifest belonging to `scope.app_id`.
pub struct EgressGuard {
    transport: Arc<dyn HttpTransport>,
    limits: EgressLimits,
    /// Per-`app_id` egress allowlist/secret-grant source -- see the module
    /// doc for the consuming-crate seam this abstracts (`svc_action::
    /// distribution::BundleCatalog`, `svc_process`'s capability snapshot).
    /// An `app_id` this source has never heard of denies every call
    /// `host_not_declared`, the same fail-closed posture an unreachable
    /// hub-api already produced before the poll loop this replaced was
    /// retired.
    catalog: Arc<dyn EgressRuleSource>,
    /// Tenant-level global denylist (spec §8.2 step 5, "refreshed from
    /// hub-api every `EGRESS_DENYLIST_REFRESH_S`"). No hub-api endpoint for
    /// this refresh exists yet -- documented seam: always empty today, so
    /// this step never denies in this build (fail-open on this axis only;
    /// every other step, including the SSRF address check, is fully
    /// enforced regardless).
    denylist: Arc<RwLock<HashSet<String>>>,
    buckets: Mutex<HashMap<String, TokenBucket>>,
    /// Host-side DNS resolution seam (connector spec: "DNS resolution done
    /// host-side ... then connecting ONLY to the validated IP, no
    /// re-resolution"). Defaults to [`TokioResolver`] in [`EgressGuard::new`];
    /// swappable in tests only ([`EgressGuard::with_resolver`]) to prove the
    /// single-resolution/no-rebind property without a live resolver.
    resolver: Arc<dyn Resolver>,
    /// Secret-handle substitution seam (connector spec condition 8: "opaque
    /// handles in the guest request, tokens never in guest memory").
    /// Defaults to [`EnvCredentialBroker`]; swappable in tests only
    /// ([`EgressGuard::with_credential_broker`]).
    credential_broker: Arc<dyn CredentialBroker>,
    denied_total: prometheus::IntCounterVec,
    /// Spec §13.5's `waddles.core.bundle-egress` flag: OFF ⇒ every call
    /// denied `feature_disabled`, checked before anything else (scheme,
    /// allowlist, SSRF -- none of it runs at all while this capability is
    /// disabled). `min_tier: free`, so [`FeatureFlag::enabled`] alone gates
    /// it -- no license-tier `check_feature` needed.
    bundle_egress: Arc<dyn FeatureFlag>,
}

impl EgressGuard {
    pub fn new(
        transport: Arc<dyn HttpTransport>,
        limits: EgressLimits,
        catalog: Arc<dyn EgressRuleSource>,
        denied_total: prometheus::IntCounterVec,
        bundle_egress: Arc<dyn FeatureFlag>,
    ) -> Self {
        Self {
            transport,
            limits,
            catalog,
            denylist: Arc::new(RwLock::new(HashSet::new())),
            buckets: Mutex::new(HashMap::new()),
            resolver: Arc::new(TokioResolver),
            credential_broker: Arc::new(EnvCredentialBroker),
            denied_total,
            bundle_egress,
        }
    }

    /// Test-only override of the DNS resolution seam -- production always
    /// uses [`TokioResolver`], constructed by [`EgressGuard::new`].
    #[cfg(test)]
    fn with_resolver(mut self, resolver: Arc<dyn Resolver>) -> Self {
        self.resolver = resolver;
        self
    }

    /// Test-only override of the credential-substitution seam -- production
    /// always uses [`EnvCredentialBroker`], constructed by
    /// [`EgressGuard::new`].
    #[cfg(test)]
    fn with_credential_broker(mut self, broker: Arc<dyn CredentialBroker>) -> Self {
        self.credential_broker = broker;
        self
    }

    /// Services one `http`/`send` host-call for `app_id` (spec §7.4's
    /// `http` row). Every rejection is counted against
    /// `waddles_egress_denied_total{app_id,reason}` (spec §8.2) before
    /// returning -- including the spec §13.5 flag check, which runs before
    /// any of it and short-circuits with `feature_disabled` when off.
    pub async fn send(
        &self,
        app_id: &str,
        args: &serde_json::Value,
    ) -> Result<serde_json::Value, HostResultError> {
        if !self.bundle_egress.enabled().await {
            let err = denied("feature_disabled", "waddles.core.bundle-egress is disabled");
            self.denied_total
                .with_label_values(&[app_id, &err.code])
                .inc();
            return Err(err);
        }
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
        // The bundle's own trusted activation config (spec §8.3: "an
        // environment-variable *name* held in the activation config")
        // fetched once, up front -- the same row every hop's egress-rule
        // check below reuses, and the *only* source `granted_secret_refs`
        // resolution below is allowed to consult.
        let row = self.catalog.resolve(app_id);

        let mut headers: Vec<(String, String)> =
            req.headers.drain(..).map(|h| (h.name, h.value)).collect();
        // Security review finding (post-M3-capabilities landing): a bundle
        // names a *symbolic* secret reference per call (spec §6.5's
        // `secret-refs: list<tuple<string, string>>`), but that name must
        // never be handed directly to `std::env::var` -- a bundle fully
        // controls this string at runtime, so doing so lets it read *any*
        // process environment variable (AWS/DB/license credentials, not
        // just its own platform token) by simply naming it. Resolution is
        // therefore two hops, both required: (1) the bundle's symbolic
        // name must be a key in *this bundle's own* `granted_secret_refs`
        // (hub-api/admin-controlled activation config, spec §8.3's actual
        // model); (2) only the *granted* env var name that maps to is ever
        // read from the process environment. A symbolic name outside the
        // granted set is refused before any environment lookup happens at
        // all.
        let granted = row.as_ref().map(|r| &r.granted_secret_refs);
        for (header_name, secret_ref) in &req.secret_refs {
            let env_var_name = granted
                .and_then(|g| g.get(secret_ref))
                .ok_or_else(|| {
                    denied(
                        "secret_not_granted",
                        format!(
                            "secret reference {secret_ref:?} is not granted to this bundle's activation config"
                        ),
                    )
                })?;
            // Connector spec condition 8: the guest only ever names a
            // symbolic `secret_ref`; the opaque `SecretHandle` carrying the
            // actual resolvable credential name is constructed here, host
            // side, after the grant-map lookup succeeds, and substituted via
            // `CredentialBroker` -- the real value never passes back through
            // any guest-visible state.
            let handle = SecretHandle::from_granted_env_var(env_var_name);
            let value = self.credential_broker.resolve(&handle)?;
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
            // `Url::host_str()` brackets an IPv6-literal host (`"[::1]"`),
            // unlike `std::net::Ipv6Addr`'s own `Display`. Every host-based
            // comparison below (manifest allowlist, denylist, DNS/SSRF
            // resolution) needs the bracket-free form -- stripped once,
            // here -- or an IPv6-literal URL never matches its own
            // manifest entry and (more importantly for the SSRF property)
            // `tokio::net::lookup_host` fails to parse it as a literal
            // address at all, rather than being a no-op for the
            // hostname/IPv4 cases where no brackets are ever present.
            let host = url
                .host_str()
                .ok_or_else(|| denied("malformed_url", "url has no host"))?
                .trim_start_matches('[')
                .trim_end_matches(']')
                .to_ascii_lowercase();

            let egress_rules = row.as_ref().map(|r| r.egress.as_slice()).unwrap_or(&[]);
            let rule = egress_rules
                .iter()
                .find(|(pattern, _)| host_matches(pattern, &host));
            let Some((matched_pattern, methods)) = rule else {
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
            if !self.limits.allowed_ports.contains(&port) {
                return Err(denied(
                    "port_not_allowed",
                    format!("port {port} is not on the egress port allowlist"),
                ));
            }
            // Declared-IP-literal-with-port support (connector spec
            // extension): a manifest entry may pin a specific port
            // (`"1.2.3.4:9443"` or `"[::1]:9443"`) in addition to the
            // host -- when it does, the connect port must match exactly,
            // on top of (not instead of) the port allowlist check above.
            // An entry with no declared port (the pre-existing shape)
            // matches any allowlisted port, unchanged.
            if let Some(required_port) = parse_pattern(matched_pattern).1 {
                if required_port != port {
                    return Err(denied(
                        "host_not_declared",
                        format!("{host}:{port} does not match the declared {matched_pattern}"),
                    ));
                }
            }
            let addrs = self
                .resolver
                .lookup(host.clone(), port)
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

/// Splits one manifest egress-rule pattern into its host/IP component and
/// an optional declared port (connector spec extension: a declared IP
/// literal -- or hostname -- "optionally with a port"). Bracket syntax
/// (`[::1]:8443`) disambiguates an IPv6 literal from a trailing `:port`,
/// mirroring URL authority syntax; a bare pattern containing more than one
/// `:` (an unbracketed IPv6 literal) is never split. A pattern with no
/// declared port matches any port on [`EgressLimits::allowed_ports`],
/// unchanged from this guard's original behavior.
fn parse_pattern(pattern: &str) -> (&str, Option<u16>) {
    if let Some(rest) = pattern.strip_prefix('[') {
        if let Some((addr, tail)) = rest.split_once(']') {
            if let Some(port_str) = tail.strip_prefix(':') {
                if let Ok(port) = port_str.parse() {
                    return (addr, Some(port));
                }
            }
            return (addr, None);
        }
        return (pattern, None);
    }
    if pattern.matches(':').count() == 1 {
        if let Some((host, port_str)) = pattern.rsplit_once(':') {
            if let Ok(port) = port_str.parse() {
                return (host, Some(port));
            }
        }
    }
    (pattern, None)
}

/// Exact host match only (connector spec, Gemini condition 2: "one
/// permission id per exact host ... never a subdomain/wildcard pattern" --
/// same `net.http:<host>` shape hub-api's manifest validator already
/// enforces via `_EGRESS_HOST_RE`). Deliberately **not** a wildcard match:
/// an operator-declared `*.example.com` manifest entry is treated as a
/// literal string and will never match any concrete host, falling through
/// to `host_not_declared` -- wildcard rejection belongs at manifest
/// validation time, but this function must never silently honor one that
/// slips through regardless. Case-insensitive, matching DNS's own
/// convention. A `pattern` carrying a `:port` suffix ([`parse_pattern`])
/// still compares by host/IP alone here -- the port match, when declared,
/// is enforced separately in [`EgressGuard::send_checked`].
fn host_matches(pattern: &str, host: &str) -> bool {
    parse_pattern(pattern).0.eq_ignore_ascii_case(host)
}

/// AWS's IPv6 metadata address, `fd00:ec2::254` (spec §8.2 step 6).
const CLOUD_METADATA_V6: Ipv6Addr = Ipv6Addr::new(0xfd00, 0x0ec2, 0, 0, 0, 0, 0, 0x0254);

/// Extracts an embedded IPv4 address from an IPv6 address carrying one, in
/// any of the three forms a resolver can hand back (security review, HIGH
/// finding: none of these were canonicalized before the SSRF check, so
/// `::ffff:169.254.169.254`/`::ffff:127.0.0.1`/`::ffff:10.0.0.1` -- and
/// their NAT64 equivalents -- all resolved as "not forbidden" despite
/// carrying a metadata/loopback/private v4 address underneath):
///
/// - **IPv4-mapped** (`::ffff:a.b.c.d`, `::ffff:0:0/96`) -- the form a dual-
///   stack resolver most commonly returns for an A-record-only host.
/// - **NAT64-synthesized** (`64:ff9b::a.b.c.d`, `64:ff9b::/96`, RFC 6052) --
///   what a NAT64 gateway's synthesized AAAA answer looks like.
/// - **IPv4-compatible** (`::a.b.c.d`, deprecated, RFC 4291 §2.5.5.1) --
///   excluding `::` (unspecified) and `::1` (loopback), which stay
///   classified as those specific native-v6 addresses instead.
///
/// Returns `None` for a native (non-embedding) IPv6 address, in which case
/// [`is_forbidden_address`] falls through to its ordinary v6-specific
/// checks -- an embedded address is judged by the *same* rules as the v4
/// address it carries, never by the (differently-shaped) native-v6 rules.
fn embedded_ipv4(v6: Ipv6Addr) -> Option<Ipv4Addr> {
    let seg = v6.segments();
    let o = v6.octets();
    let last_32 = || Ipv4Addr::new(o[12], o[13], o[14], o[15]);
    // IPv4-mapped: `::ffff:a.b.c.d`.
    if seg[0..5] == [0, 0, 0, 0, 0] && seg[5] == 0xffff {
        return Some(last_32());
    }
    // NAT64-synthesized: `64:ff9b::a.b.c.d`.
    if seg[0] == 0x0064 && seg[1] == 0xff9b && seg[2..6] == [0, 0, 0, 0] {
        return Some(last_32());
    }
    // IPv4-compatible: `::a.b.c.d`, excluding `::` and `::1`.
    if seg[0..6] == [0, 0, 0, 0, 0, 0] && (seg[6] != 0 || seg[7] > 1) {
        return Some(last_32());
    }
    None
}

fn is_private_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    o[0] == 10 || (o[0] == 172 && (16..=31).contains(&o[1])) || (o[0] == 192 && o[1] == 168)
}

fn is_link_local_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    o[0] == 169 && o[1] == 254
}

/// Carrier-grade NAT range, RFC 6598 (`100.64.0.0/10`) -- shared address
/// space a residential/mobile ISP routes between subscribers and its own
/// NAT layer, never publicly reachable. Also where several cloud
/// providers' non-AWS metadata endpoints live (e.g. Alibaba Cloud's
/// `100.100.100.200`), so this single range check covers those alongside
/// the RFC1918/link-local ranges already checked. Never lifted by
/// `allow_private`, same tier as loopback/link-local/cloud-metadata.
fn is_cgnat_v4(ip: Ipv4Addr) -> bool {
    let o = ip.octets();
    o[0] == 100 && (64..=127).contains(&o[1])
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
/// link-local, unspecified, multicast, broadcast, CGNAT and the cloud-
/// metadata addresses are **never** lifted by any setting.
///
/// `pub`: reused by each consuming crate's own built-in host-initiated
/// sends (e.g. `svc_action`'s Discord relay) -- DNS-rebinding defense is
/// worth applying regardless of whether the destination host is
/// bundle-supplied or compiled-in.
pub fn is_forbidden_address(ip: IpAddr, allow_private: bool) -> Option<&'static str> {
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
            if v4 == Ipv4Addr::BROADCAST {
                return Some("broadcast");
            }
            if v4 == Ipv4Addr::new(169, 254, 169, 254) {
                return Some("cloud_metadata");
            }
            if is_link_local_v4(v4) {
                return Some("link_local");
            }
            if is_cgnat_v4(v4) {
                return Some("cgnat");
            }
            if !allow_private && is_private_v4(v4) {
                return Some("private");
            }
            None
        }
        IpAddr::V6(v6) => {
            // Security review, HIGH finding: canonicalize an embedded IPv4
            // address (mapped/NAT64/compatible) to its v4 form and judge
            // it by the v4 rules *before* any native-v6 check runs --
            // otherwise `::ffff:169.254.169.254` etc. never match any of
            // the checks below and are wrongly permitted.
            if let Some(v4) = embedded_ipv4(v6) {
                return is_forbidden_address(IpAddr::V4(v4), allow_private);
            }
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

/// An opaque reference to a bundle's granted secret (connector spec
/// condition 8: "opaque handles in the guest request, tokens never in guest
/// memory"). The bundle's own request JSON only ever carries a *symbolic*
/// `secret_ref` name (`HttpSendArgs::secret_refs`); this handle wraps the
/// already-validated, host-resolved credential locator produced *after*
/// that symbolic name is confirmed present in the bundle's own
/// `granted_secret_refs` grant map -- it is constructed host-side only and
/// never round-trips back into guest-controlled state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecretHandle(String);

impl SecretHandle {
    /// Wraps an already-granted environment variable name. Private
    /// constructor: a [`SecretHandle`] must only ever be built from a value
    /// that has already passed the grant-map check in
    /// [`EgressGuard::send_checked`], never directly from bundle input.
    fn from_granted_env_var(env_var_name: &str) -> Self {
        Self(env_var_name.to_string())
    }
}

/// Resolves a [`SecretHandle`] to its live credential value. Split out as a
/// trait -- mirrors this crate's `HttpTransport`/`Resolver` per-dependency
/// seam pattern -- so the credential source is swappable (process-env
/// today; a vault/KMS-backed broker later) without
/// [`EgressGuard::send_checked`] changing at all, and so a symbolic
/// bundle-supplied name is never one step away from an arbitrary
/// `std::env::var` call outside this seam.
pub trait CredentialBroker: Send + Sync {
    fn resolve(&self, handle: &SecretHandle) -> Result<String, HostResultError>;
}

/// The only [`CredentialBroker`] wired today: the granted secret's value is
/// read from the process environment (spec §8.3's model -- an
/// activation-config *name*, not a stored token, is what's granted).
pub struct EnvCredentialBroker;

impl CredentialBroker for EnvCredentialBroker {
    fn resolve(&self, handle: &SecretHandle) -> Result<String, HostResultError> {
        std::env::var(&handle.0).map_err(|_| {
            denied(
                "secret_unresolved",
                format!("granted env var {:?} is not configured", handle.0),
            )
        })
    }
}

/// Host-side DNS resolution (connector spec: "DNS resolution done
/// host-side, rejecting private, loopback, link-local and metadata IPs ...
/// then connecting ONLY to the validated IP (no re-resolution, to prevent
/// DNS rebinding)"). Split out purely for testability -- see [`EgressGuard::
/// with_resolver`] -- production always uses [`TokioResolver`].
trait Resolver: Send + Sync {
    fn lookup<'a>(
        &'a self,
        host: String,
        port: u16,
    ) -> Pin<Box<dyn Future<Output = std::io::Result<Vec<SocketAddr>>> + Send + 'a>>;
}

/// The real [`Resolver`]: `tokio::net::lookup_host`, collected once. This
/// single collection point is the entire anti-rebinding property --
/// [`EgressGuard::send_checked`] calls this exactly once per hop and pins
/// the chosen address into [`TransportRequest::pinned_addr`], which
/// [`HttpTransport`] impls must connect to verbatim, never re-resolving.
struct TokioResolver;

impl Resolver for TokioResolver {
    fn lookup<'a>(
        &'a self,
        host: String,
        port: u16,
    ) -> Pin<Box<dyn Future<Output = std::io::Result<Vec<SocketAddr>>> + Send + 'a>> {
        Box::pin(async move {
            let addrs = tokio::net::lookup_host((host.as_str(), port)).await?;
            Ok(addrs.collect())
        })
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
/// makes that overhead material. Reads no [`EgressLimits`] field except
/// via its constructor argument -- callers that want the upstream-proxy
/// seam pass `proxy_url` through [`ReqwestTransport::new`].
pub struct ReqwestTransport {
    proxy_url: Option<String>,
}

impl ReqwestTransport {
    /// Direct-connect transport -- no proxy configured. Behaviorally
    /// identical to this type before the proxy seam was added.
    pub fn new() -> Self {
        Self { proxy_url: None }
    }

    /// Dials every request through `proxy_url` instead of connecting
    /// directly to the guard's already-pinned address -- see the crate
    /// module doc's "Upstream egress proxy" section.
    pub fn with_proxy(proxy_url: String) -> Self {
        Self {
            proxy_url: Some(proxy_url),
        }
    }
}

impl Default for ReqwestTransport {
    fn default() -> Self {
        Self::new()
    }
}

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
            // Bracket-strip for consistency with `EgressGuard::send_checked`
            // (same `host_str()` quirk for IPv6-literal hosts, see its
            // comment) -- a no-op for hostname/IPv4 targets.
            let host = url
                .host_str()
                .ok_or_else(|| denied("malformed_url", "url has no host"))?
                .trim_start_matches('[')
                .trim_end_matches(']')
                .to_string();
            let method = reqwest::Method::from_bytes(req.method.as_bytes())
                .map_err(|_| denied("invalid_args", "invalid HTTP method"))?;

            let mut builder = reqwest::Client::builder()
                .resolve(&host, req.pinned_addr)
                .redirect(reqwest::redirect::Policy::none())
                .timeout(timeout);
            if let Some(proxy_url) = &self.proxy_url {
                let proxy = reqwest::Proxy::all(proxy_url)
                    .map_err(|e| denied("transport", format!("invalid proxy_url: {e}")))?;
                builder = builder.proxy(proxy);
            }
            let client = builder
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

    /// Minimal in-memory [`EgressRuleSource`] fixture -- this crate's own
    /// stand-in for each consuming crate's real catalog type
    /// (`svc_action::distribution::BundleCatalog`, `svc_process`'s
    /// capability snapshot), which are exercised by their own crates'
    /// tests against this exact trait instead.
    #[derive(Default)]
    struct TestCatalog(RwLock<HashMap<String, EgressRuleRow>>);

    impl TestCatalog {
        fn new() -> Arc<Self> {
            Arc::new(Self::default())
        }

        fn insert(&self, app_id: &str, row: EgressRuleRow) {
            self.0
                .write()
                .unwrap_or_else(|e| e.into_inner())
                .insert(app_id.to_string(), row);
        }
    }

    impl EgressRuleSource for TestCatalog {
        fn resolve(&self, app_id: &str) -> Option<EgressRuleRow> {
            self.0
                .read()
                .unwrap_or_else(|e| e.into_inner())
                .get(app_id)
                .cloned()
        }
    }

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
            allowed_ports: vec![443],
            proxy_url: None,
        }
    }

    fn catalog_with_row(app_id: &str, egress: Vec<(String, Vec<String>)>) -> Arc<TestCatalog> {
        catalog_with_row_and_secrets(app_id, egress, HashMap::new())
    }

    fn catalog_with_row_and_secrets(
        app_id: &str,
        egress: Vec<(String, Vec<String>)>,
        granted_secret_refs: HashMap<String, String>,
    ) -> Arc<TestCatalog> {
        let catalog = TestCatalog::new();
        catalog.insert(
            app_id,
            EgressRuleRow {
                egress,
                egress_rps: None,
                granted_secret_refs,
            },
        );
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

    /// Every existing test in this module exercises *other* properties of
    /// the guard (allowlist, SSRF, secrets, redirects, ...), not the spec
    /// §13.5 flag gate itself -- so the shared fixture holds it ON via
    /// [`StaticFlag`], no live license/PostHog server needed. The
    /// flag-gate tests further down construct an `EgressGuard` directly
    /// with `StaticFlag(false)` instead.
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
            boxed(StaticFlag(true)),
        )
    }

    // -- Host-pattern matching: exact only (connector spec, Gemini condition 2) --

    #[test]
    fn host_matches_exact_pattern() {
        assert!(host_matches("api.spotify.com", "api.spotify.com"));
        assert!(!host_matches("api.spotify.com", "other.spotify.com"));
    }

    #[test]
    fn host_matches_is_case_insensitive() {
        assert!(host_matches("API.Spotify.com", "api.spotify.com"));
    }

    /// A wildcard pattern is never honored, even if one slips past manifest
    /// validation (hub-api's `_EGRESS_HOST_RE` is meant to reject it there
    /// already -- this is the second, host-side line of defense): it's
    /// compared as a literal string and never matches any concrete host.
    #[test]
    fn wildcard_pattern_is_never_honored_as_a_wildcard() {
        assert!(!host_matches("*.googleapis.com", "storage.googleapis.com"));
        assert!(!host_matches("*.googleapis.com", "a.b.googleapis.com"));
        assert!(!host_matches("*.googleapis.com", "googleapis.com"));
    }

    /// End-to-end proof that a manifest wildcard entry denies every concrete
    /// host as `host_not_declared`, same as an absent entry -- a wildcard
    /// egress declaration grants access to nothing.
    #[tokio::test]
    async fn manifest_wildcard_entry_denies_every_concrete_host() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("*.googleapis.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://storage.googleapis.com/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// Mandatory negative test: an IP-literal host, not on the manifest
    /// allowlist, is denied on the same `host_not_declared` path as any
    /// other undeclared host -- literal syntax buys no special treatment.
    #[tokio::test]
    async fn ip_literal_host_not_on_allowlist_is_denied() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://93.184.216.34/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A declared public IPv4 literal is permitted end-to-end, exactly
    /// like a declared hostname -- declaring an IP buys no special
    /// treatment either direction.
    #[tokio::test]
    async fn declared_public_ip_literal_is_permitted() {
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("93.184.216.34".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://93.184.216.34/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    /// A declared IP literal with an explicit port only matches that exact
    /// port -- the same host at a different (even allowlisted) port is
    /// `host_not_declared`, not merely `port_not_allowed`.
    #[tokio::test]
    async fn declared_ip_literal_with_port_requires_that_exact_port() {
        let mut limits = default_limits();
        limits.allowed_ports = vec![443, 8443];
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            limits,
            catalog_with_row(
                "waddles.a.b.c",
                vec![("93.184.216.34:8443".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://93.184.216.34/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    // -- Port allowlist (connector spec, Gemini condition 2: "443 by default") --

    #[tokio::test]
    async fn non_default_port_is_denied_even_for_an_allowlisted_host() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://api.spotify.com:8443/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "port_not_allowed");
    }

    #[tokio::test]
    async fn allowlisted_port_is_permitted() {
        let mut limits = default_limits();
        limits.allowed_ports = vec![443, 8443];
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            limits,
            catalog_with_row(
                "waddles.a.b.c",
                vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://api.spotify.com:8443/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    // -- DNS rebinding (connector spec: "no re-resolution, to prevent DNS
    // rebinding") --

    /// A resolver stub that counts calls and always answers with the same
    /// fixed, permitted address -- standing in for an attacker's DNS server
    /// that would answer *differently* on a second lookup (rebinding to a
    /// forbidden address) if the guard were ever foolish enough to ask
    /// twice. Asserting exactly one call, and that the transport receives
    /// precisely that resolved address, proves the guard never gives a
    /// rebinding attacker the second lookup it needs.
    struct CountingResolver {
        calls: std::sync::atomic::AtomicUsize,
        addr: SocketAddr,
    }

    impl Resolver for CountingResolver {
        fn lookup<'a>(
            &'a self,
            _host: String,
            _port: u16,
        ) -> Pin<Box<dyn Future<Output = std::io::Result<Vec<SocketAddr>>> + Send + 'a>> {
            self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let addr = self.addr;
            Box::pin(async move { Ok(vec![addr]) })
        }
    }

    #[tokio::test]
    async fn dns_rebinding_simulation_resolves_exactly_once_and_pins_that_address() {
        let checked_addr: SocketAddr = "93.184.216.34:443".parse().unwrap();
        let resolver = Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: checked_addr,
        });
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_resolver(Arc::clone(&resolver) as Arc<dyn Resolver>);

        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://api.spotify.com/"}),
            )
            .await
            .expect("send succeeds");

        assert_eq!(resolver.calls.load(std::sync::atomic::Ordering::SeqCst), 1);
        let sent = transport.requests.lock().unwrap();
        assert_eq!(sent[0].pinned_addr, checked_addr);
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
    fn cgnat_v4_is_always_forbidden_even_with_allow_private() {
        let ip = IpAddr::V4(Ipv4Addr::new(100, 100, 100, 200));
        assert_eq!(is_forbidden_address(ip, true), Some("cgnat"));
    }

    #[test]
    fn broadcast_v4_is_always_forbidden() {
        assert_eq!(
            is_forbidden_address(IpAddr::V4(Ipv4Addr::BROADCAST), true),
            Some("broadcast")
        );
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

    // -- Mandatory regression tests: IPv4-mapped/NAT64/compatible IPv6 SSRF
    // bypass (security review, HIGH finding). Before the fix, every one of
    // these addresses returned `None` (permitted) because `is_loopback()`/
    // `is_unspecified()`/the manual private/link-local/metadata checks
    // never canonicalize an embedded v4 address.

    #[test]
    fn ipv4_mapped_cloud_metadata_is_forbidden() {
        let ip: IpAddr = "::ffff:169.254.169.254".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, true), Some("cloud_metadata"));
    }

    #[test]
    fn ipv4_mapped_loopback_is_forbidden() {
        let ip: IpAddr = "::ffff:127.0.0.1".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, true), Some("loopback"));
    }

    #[test]
    fn ipv4_mapped_private_is_forbidden_unless_allow_private() {
        let ip: IpAddr = "::ffff:10.0.0.1".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, false), Some("private"));
        assert_eq!(is_forbidden_address(ip, true), None);
    }

    #[test]
    fn ipv4_mapped_public_address_is_permitted() {
        let ip: IpAddr = "::ffff:93.184.216.34".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, false), None);
    }

    #[test]
    fn nat64_synthesized_cloud_metadata_is_forbidden() {
        // 64:ff9b::a9fe:a9fe == 64:ff9b::169.254.169.254.
        let ip: IpAddr = "64:ff9b::a9fe:a9fe".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, true), Some("cloud_metadata"));
    }

    #[test]
    fn nat64_synthesized_private_is_forbidden_unless_allow_private() {
        // 64:ff9b::a00:1 == 64:ff9b::10.0.0.1.
        let ip: IpAddr = "64:ff9b::a00:1".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, false), Some("private"));
        assert_eq!(is_forbidden_address(ip, true), None);
    }

    #[test]
    fn ipv4_compatible_private_is_forbidden_unless_allow_private() {
        let ip: IpAddr = "::10.0.0.1".parse().unwrap();
        assert_eq!(is_forbidden_address(ip, false), Some("private"));
        assert_eq!(is_forbidden_address(ip, true), None);
    }

    #[test]
    fn native_unspecified_and_loopback_v6_are_unaffected_by_embedded_v4_detection() {
        // `::` and `::1` must still classify as unspecified/loopback via
        // the native-v6 checks, never be mistaken for IPv4-compatible
        // `::0.0.0.0`/`::0.0.0.1`.
        assert_eq!(
            is_forbidden_address(IpAddr::V6(Ipv6Addr::UNSPECIFIED), true),
            Some("unspecified")
        );
        assert_eq!(
            is_forbidden_address(IpAddr::V6(Ipv6Addr::LOCALHOST), true),
            Some("loopback")
        );
    }

    #[tokio::test]
    async fn ssrf_to_an_ipv4_mapped_cloud_metadata_literal_is_blocked_end_to_end() {
        // Hermetic: an IP-literal host in brackets never queries a real
        // resolver (`tokio::net::lookup_host` resolves it directly). The
        // `url` crate normalizes an IPv6-literal host to fully-expanded
        // hex segments (`::ffff:a9fe:a9fe`), not the dotted-quad mixed
        // notation (`::ffff:169.254.169.254`) -- the manifest entry below
        // must match that normalized form, same as any other host
        // comparison this guard does. `a9fe:a9fe` == `169.254.169.254`.
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("::ffff:a9fe:a9fe".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://[::ffff:169.254.169.254]/latest/meta-data"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
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
            boxed(StaticFlag(true)),
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
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

    /// Mandatory oversize-response test at the [`EgressGuard`] pipeline
    /// level (spec: "request and response size caps"): a truncated
    /// [`TransportResponse`] from the transport (its own size-cap
    /// enforcement, exercised directly against a real server further down)
    /// is surfaced through the guard's response shape unchanged -- the guard
    /// never re-buffers or hides truncation, so a caller can always tell a
    /// response was capped rather than complete.
    #[tokio::test]
    async fn oversize_response_is_reported_truncated_through_the_full_guard_pipeline() {
        let capped_body = vec![b'x'; 1_048_576];
        let transport = FakeTransport::default().queue(Ok(TransportResponse {
            status: 200,
            headers: vec![],
            body: capped_body.clone(),
            truncated: true,
        }));
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("discord.com".to_string(), vec!["GET".to_string()])],
            transport,
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/big"}),
            )
            .await
            .expect("oversize response is still a successful send, just marked truncated");
        assert_eq!(result["truncated"], true);
        let body_b64 = result["body_base64"].as_str().unwrap();
        let decoded = base64::engine::general_purpose::STANDARD
            .decode(body_b64)
            .unwrap();
        assert_eq!(decoded.len(), capped_body.len());
    }

    // -- Secret-handle substitution (connector spec condition 8) --

    /// A [`CredentialBroker`] stub that records every [`SecretHandle`] it
    /// was asked to resolve, standing in for a future vault/KMS-backed
    /// broker -- proves the substitution goes through the trait seam, not a
    /// direct `std::env::var` call baked into the send path.
    struct FakeCredentialBroker {
        value: String,
        resolved_handles: Mutex<Vec<SecretHandle>>,
    }

    impl CredentialBroker for FakeCredentialBroker {
        fn resolve(&self, handle: &SecretHandle) -> Result<String, HostResultError> {
            self.resolved_handles.lock().unwrap().push(handle.clone());
            Ok(self.value.clone())
        }
    }

    /// Mandatory handle-substitution test (connector spec condition 8): the
    /// guest-supplied request JSON (`HttpSendArgs`, standing in for the
    /// bundle's own memory) carries only the symbolic `secret_ref` name --
    /// never the real token -- and the real token is substituted by the
    /// host, via [`CredentialBroker`], only into the outbound
    /// [`TransportRequest`] the guest never observes.
    #[tokio::test]
    async fn secret_handle_substitution_never_exposes_the_token_to_the_guest_request() {
        let broker = Arc::new(FakeCredentialBroker {
            value: "sk-live-should-never-appear-in-guest-state".to_string(),
            resolved_handles: Mutex::new(Vec::new()),
        });
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row_and_secrets(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
                HashMap::from([(
                    "DISCORD_TOKEN_REF".to_string(),
                    "EGRESS_TEST_HANDLE_BROKER".to_string(),
                )]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::clone(&broker) as Arc<dyn CredentialBroker>);

        // The guest's own request args -- the only thing a bundle ever
        // constructs -- carries the symbolic name only.
        let guest_args = serde_json::json!({
            "method": "POST",
            "url": "https://discord.com/api/webhooks/1",
            "secret_refs": {"Authorization": "DISCORD_TOKEN_REF"}
        });
        let guest_args_serialized = guest_args.to_string();
        assert!(!guest_args_serialized.contains("sk-live"));

        guard
            .send("waddles.a.b.c", &guest_args)
            .await
            .expect("send succeeds");

        // The broker was asked to resolve the host-constructed handle, not
        // a bundle-supplied string.
        let resolved = broker.resolved_handles.lock().unwrap();
        assert_eq!(resolved.len(), 1);
        assert_eq!(
            resolved[0],
            SecretHandle::from_granted_env_var("EGRESS_TEST_HANDLE_BROKER")
        );

        // The real token reaches only the outbound transport request, never
        // back into anything shaped like the guest's own request args.
        let sent = transport.requests.lock().unwrap();
        assert!(sent[0]
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == &broker.value));
    }

    /// Happy path for the two-hop resolution (security review fix): the
    /// bundle's symbolic name (`DISCORD_TOKEN_REF`, deliberately different
    /// from the real env var name) is granted in this bundle's own
    /// activation config, mapping to the real env var
    /// `EGRESS_TEST_DISCORD_TOKEN` -- only *that* granted name is ever read
    /// from the process environment.
    #[tokio::test]
    async fn granted_secret_ref_resolves_via_activation_config_and_is_injected_as_a_header() {
        // SAFETY: test-process-local env var, unique name avoids
        // cross-test collisions under parallel `cargo test` execution.
        unsafe { std::env::set_var("EGRESS_TEST_DISCORD_TOKEN", "s3cr3t") };
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row_and_secrets(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
                HashMap::from([(
                    "DISCORD_TOKEN_REF".to_string(),
                    "EGRESS_TEST_DISCORD_TOKEN".to_string(),
                )]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "POST",
                    "url": "https://discord.com/api/webhooks/1",
                    "secret_refs": {"Authorization": "DISCORD_TOKEN_REF"}
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

    /// The granted symbolic name maps to an env var the process never
    /// actually set -- a configuration error, distinct from naming an
    /// ungranted reference at all.
    #[tokio::test]
    async fn granted_secret_ref_whose_env_var_is_unset_is_denied_secret_unresolved() {
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog_with_row_and_secrets(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
                HashMap::from([(
                    "DISCORD_TOKEN_REF".to_string(),
                    "EGRESS_TEST_NEVER_SET_XYZ".to_string(),
                )]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "POST",
                    "url": "https://discord.com/api/webhooks/1",
                    "secret_refs": {"Authorization": "DISCORD_TOKEN_REF"}
                }),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "secret_unresolved");
    }

    /// **Mandatory regression test (security review, CRITICAL finding):**
    /// arbitrary env-var read via bundle-controlled `secret_refs`. Before
    /// the fix, `send_checked` called `std::env::var(secret_ref)` directly
    /// on the bundle-supplied string -- a bundle naming a process secret
    /// like `DATABASE_URL` would have it read and injected as a header,
    /// reachable at any host the bundle's own manifest allowlists. This
    /// asserts a non-granted name is refused `secret_not_granted` **even
    /// when that exact env var is set in the process**, and that its value
    /// never reaches the transport request at all.
    #[tokio::test]
    async fn ungranted_secret_ref_is_denied_even_when_the_named_env_var_is_set() {
        // SAFETY: test-process-local env var, unique name avoids
        // cross-test collisions under parallel `cargo test` execution --
        // deliberately shaped like a real credential name to mirror the
        // finding's exact scenario.
        unsafe {
            std::env::set_var(
                "DATABASE_URL",
                "postgres://exfiltrated-should-never-be-read",
            )
        };
        let transport = Arc::new(FakeTransport::default());
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            // No grants at all -- the bundle's activation config never
            // mentions `DATABASE_URL` (or anything else) as a secret ref.
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "POST",
                    "url": "https://discord.com/api/webhooks/1",
                    "secret_refs": {"X-Exfil": "DATABASE_URL"}
                }),
            )
            .await
            .unwrap_err();
        unsafe { std::env::remove_var("DATABASE_URL") };

        assert_eq!(err.code, "secret_not_granted");
        // The transport must never have been reached at all -- the denial
        // happens before any request is built, so no header (and
        // certainly not the secret value) can have leaked into it.
        assert!(transport.requests.lock().unwrap().is_empty());
    }

    /// Same finding, second angle: even with a grant map present for other
    /// refs, a symbolic name outside that specific bundle's own granted
    /// set is still refused -- a grant is per-name, not "any name once one
    /// grant exists".
    #[tokio::test]
    async fn secret_ref_outside_this_bundles_granted_set_is_denied() {
        unsafe { std::env::set_var("EGRESS_TEST_OTHER_SECRET", "should-not-leak") };
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog_with_row_and_secrets(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
                HashMap::from([(
                    "DISCORD_TOKEN_REF".to_string(),
                    "EGRESS_TEST_DISCORD_TOKEN".to_string(),
                )]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "POST",
                    "url": "https://discord.com/api/webhooks/1",
                    // Not the granted "DISCORD_TOKEN_REF" -- naming the
                    // *target* env var directly must still be refused.
                    "secret_refs": {"Authorization": "EGRESS_TEST_OTHER_SECRET"}
                }),
            )
            .await
            .unwrap_err();
        unsafe { std::env::remove_var("EGRESS_TEST_OTHER_SECRET") };
        assert_eq!(err.code, "secret_not_granted");
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
            boxed(StaticFlag(true)),
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
            boxed(StaticFlag(true)),
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

    // -- spec §13.5 `waddles.core.bundle-egress` flag gate --

    /// OFF ⇒ every egress call is denied `feature_disabled`, before any
    /// other check runs (even an otherwise-fully-valid, allowlisted
    /// request) -- and the denial is counted in the same metric as every
    /// other rejection reason.
    #[tokio::test]
    async fn bundle_egress_flag_off_denies_every_call_as_feature_disabled() {
        let transport = Arc::new(FakeTransport::default());
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(false)),
        );
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "POST", "url": "https://discord.com/api/webhooks/1"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "feature_disabled");
        // Never even reached the transport -- the flag check is the very
        // first thing `send` does.
        assert!(transport.requests.lock().unwrap().is_empty());
        let value = guard
            .denied_total
            .with_label_values(&["waddles.a.b.c", "feature_disabled"])
            .get();
        assert_eq!(value, 1);
    }

    #[tokio::test]
    async fn bundle_egress_flag_on_permits_an_otherwise_valid_call() {
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["POST".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "POST", "url": "https://discord.com/api/webhooks/1"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
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

        let transport = ReqwestTransport::new();
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

        let transport = ReqwestTransport::new();
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
        let transport = ReqwestTransport::new();
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

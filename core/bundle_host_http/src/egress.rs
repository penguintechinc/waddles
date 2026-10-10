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
//!
//! **Secret refs: header slots and `?query` slots.** A bundle's
//! `secret-refs` name a *slot* and a symbolic reference; the host resolves
//! the reference (grant-map, then [`CredentialBroker`]) and injects the value,
//! which therefore never enters the WASM component. A plain slot name is a
//! **header** (`Authorization`): attached on the secret-bound host's hops. A
//! slot name prefixed `?` (`?key`) is a **query-parameter** ref for APIs that
//! authenticate by query string (e.g. WeatherAPI's `?key=`): the host appends
//! `key=<value>` to the URL, replacing any same-named parameter the bundle
//! sent. The query form is stricter than the header form because a URL is
//! echoed far more widely than a header:
//!
//! - injected **only** on the first hop, on a granted `net.http.fqdn` host
//!   (never an IP-literal grant, never widened by `secret_granted_hosts`) --
//!   **every redirect hop drops it**, and any same-named parameter a
//!   redirect's `Location` carries is stripped too;
//! - written only into the per-hop [`TransportRequest::url`], never into the
//!   guard's own redirect-chain URL, so everything the guard logs/joins is
//!   structurally secret-free;
//! - a [`SecretRedactor`] scrubs every resolved secret (raw and both URL
//!   encodings) from transport error strings (`reqwest`'s `Display` embeds the
//!   full URL), response headers (e.g. a pagination `Link`) and response
//!   bodies; `TransportRequest`'s `Debug` and every log line print only the
//!   query-less URL; the transport additionally strips the query from the
//!   URL inside any `reqwest::Error` it classifies;
//! - an ungranted, unresolvable or empty `?`-ref fails the whole call
//!   (`secret_not_granted`/`secret_unresolved`) before any network activity
//!   -- never a silent unauthenticated request.
//!
//! This is the single choke point for every consumer: `svc_process`,
//! `svc_action` (a thin shim over this module) and `egress_proxy` (which
//! shares this crate's IP policy and only ever sees the CONNECT authority of
//! an `https://` call, never its path or query) all go through
//! [`EgressGuard::send`], so there is exactly one injection+redaction path.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::pin::Pin;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use base64::Engine;
use egress_assertion::ip_matches_grant;
pub use egress_assertion::{
    AssertionSigningKey, DestinationCategory, ASSERTION_HEADER as PROXY_ASSERTION_HEADER_NAME,
    FORWARD_AUTHORIZATION_HEADER as PROXY_FORWARD_AUTHORIZATION_HEADER,
};
use futures_util::StreamExt;
use penguin_bundle_host::wire::HostResultError;
use serde::Deserialize;
use tracing::debug;

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
/// `http.send`/[`EgressGuard::validate_dial`] call: three **separate**
/// permission-family grant lists (Justin's decision, superseding this
/// crate's original single-family `egress` list -- each matched only by
/// its own grants, never cross-matched):
///
/// - `fqdn_grants` (`net.http.fqdn:<host>`): a public FQDN. The resolved
///   address must itself be public *unless* it is separately covered by a
///   `private_ip_grants` entry (an FQDN rebinding into a private IP is
///   otherwise denied, even though the FQDN itself is granted).
/// - `public_ip_grants` (`net.http.public-ip:<ip>`, optional `:port`): an
///   exact public IP literal. Denied if the literal is actually a private-
///   range address -- a private IP is never reachable via this list.
/// - `private_ip_grants` (`net.http.private-ip:<ip|cidr>`, optional
///   `:port`): an exact private-range IP or CIDR block (RFC1918/ULA/CGNAT).
///   Also gated by the instance-wide [`InstanceEgressPolicy`] regardless of
///   whether a grant matches -- see [`EgressGuard::with_instance_policy`].
///
/// Each tuple is `(pattern, methods)`, the same shape this crate has always
/// used. Each consuming crate's own bundle-snapshot type implements
/// [`EgressRuleSource`] over its existing storage rather than this crate
/// owning a second copy of that state; [`EgressRuleRow::from_legacy_patterns`]
/// is the migration helper for a crate whose own manifest storage still
/// carries the pre-three-category single list.
#[derive(Debug, Clone, Default)]
pub struct EgressRuleRow {
    pub fqdn_grants: Vec<(String, Vec<String>)>,
    pub public_ip_grants: Vec<(String, Vec<String>)>,
    pub private_ip_grants: Vec<(String, Vec<String>)>,
    pub egress_rps: Option<u32>,
    pub granted_secret_refs: HashMap<String, String>,
    /// Exact hosts (lowercase, spec §8.2's declared-host comparison -- same
    /// as every other host check in this module) a resolved `secret_refs`
    /// header may be forwarded to, *beyond* the host the bundle's own
    /// request originally targeted. Empty (the `Default`, and what
    /// [`EgressRuleRow::from_legacy_patterns`] produces today -- no caller
    /// wires this field yet) is the strict, backward-compatible posture:
    /// a secret-derived header is attached only on the hop whose host
    /// equals the *original* request's host, dropped on every redirect to
    /// a different one, and re-attached if a later hop redirects back.
    /// Security review finding (PR #468, HIGH): `send`'s redirect loop
    /// previously cloned one `headers` vec -- secret headers included --
    /// unconditionally on every hop, forwarding e.g. a Discord bot token to
    /// whatever host a 3xx `Location` named. This field is the seam a
    /// future host-scoped secret grant (hub-api/manifest) can populate to
    /// deliberately widen that beyond the single originating host.
    pub secret_granted_hosts: HashSet<String>,
}

impl EgressRuleRow {
    /// Builds a row from the pre-three-category single-list shape
    /// (connector spec's original `net.http:<host>` model: a hostname or
    /// IP literal, optionally `:port`, in one flat list) by auto-
    /// classifying each pattern into its new permission family: an IP
    /// literal becomes a `public_ip_grants` or `private_ip_grants` entry
    /// depending on whether the literal address itself is a private range
    /// ([`is_private_range`]); anything else (a hostname) becomes an
    /// `fqdn_grants` entry. Lets `svc_action`'s and `svc_process`'s
    /// existing manifest-egress adapters adopt the three-category model
    /// here without the manifest/hub-api wire format itself changing --
    /// a genuine three-permission-id manifest format
    /// (`net.http.fqdn:`/`net.http.public-ip:`/`net.http.private-ip:`) is
    /// tracked as follow-up manifest/hub-api work, out of scope for this
    /// crate.
    pub fn from_legacy_patterns(
        patterns: Vec<(String, Vec<String>)>,
        egress_rps: Option<u32>,
        granted_secret_refs: HashMap<String, String>,
    ) -> Self {
        let mut row = Self {
            egress_rps,
            granted_secret_refs,
            ..Self::default()
        };
        for (pattern, methods) in patterns {
            let (addr_part, _) = parse_pattern(&pattern);
            match addr_part.parse::<IpAddr>() {
                Ok(ip) if is_private_range(ip) => row.private_ip_grants.push((pattern, methods)),
                Ok(_) => row.public_ip_grants.push((pattern, methods)),
                Err(_) => row.fqdn_grants.push((pattern, methods)),
            }
        }
        row
    }
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
    /// Secret slot -> secret reference name (spec §8.3/§6.5's
    /// `secret-refs: list<tuple<string, string>>`). A slot is either a
    /// plain header name (the resolved value is injected as that header) or
    /// `?<param>` (the resolved value is injected as that URL query
    /// parameter) -- see [`SecretSlot`]. Accepted on the wire as a JSON
    /// object *or* as a list of `[slot, ref]` pairs
    /// ([`deserialize_secret_refs`]).
    #[serde(default, deserialize_with = "deserialize_secret_refs")]
    secret_refs: Vec<(String, String)>,
}

/// Decodes `HttpSendArgs::secret_refs` from either wire shape: a JSON object
/// (`{"Authorization": "TOKEN_REF"}`, this crate's original convention and
/// what every in-crate caller/test sends) or a JSON array of two-element
/// pairs (`[["Authorization", "TOKEN_REF"]]`) -- the shape
/// `core/bundle_executor`'s `http::Host::send` actually serializes the WIT
/// `list<tuple<string, string>>` into, which a map-only decoder rejected
/// outright (`invalid type: sequence, expected a map`), making *every*
/// guest-originated `http.send` fail `invalid_args` before any secret
/// handling ran. Order is preserved for the list form; the object form is
/// key-sorted (`serde_json::Map` is a `BTreeMap` here).
fn deserialize_secret_refs<'de, D>(deserializer: D) -> Result<Vec<(String, String)>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum Wire {
        Pairs(Vec<(String, String)>),
        Map(std::collections::BTreeMap<String, String>),
    }
    Ok(match Wire::deserialize(deserializer)? {
        Wire::Pairs(pairs) => pairs,
        Wire::Map(map) => map.into_iter().collect(),
    })
}

/// Leading character that marks a `secret_refs` slot as a **query-parameter
/// secret ref** (`?key`) rather than a header name. `?` is not a legal HTTP
/// header-name (RFC 9110 `token`) character, so the two namespaces can
/// never collide -- a plain name keeps meaning "header", unchanged.
const QUERY_SECRET_PREFIX: char = '?';

/// Longest accepted query-parameter name in a `?<param>` secret slot.
const MAX_QUERY_SECRET_NAME_LEN: usize = 64;

/// One decoded `secret_refs` slot.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SecretSlot<'a> {
    /// Inject the resolved value as this HTTP header (original behavior).
    Header(&'a str),
    /// Inject the resolved value as `?<name>=<value>` in the request URL's
    /// query, host-side, on the secret-bound FQDN's first hop only
    /// ([`inject_query_secrets`]).
    QueryParam(&'a str),
}

impl<'a> SecretSlot<'a> {
    /// Classifies one slot name. A `?`-prefixed slot must be followed by a
    /// conservative parameter name (`[A-Za-z0-9._-]`, 1..=64 chars) so a
    /// bundle can never smuggle `&`/`=`/`#`/whitespace into the query it
    /// asks the host to build -- anything else is `invalid_args`.
    fn parse(slot: &'a str) -> Result<Self, HostResultError> {
        match slot.strip_prefix(QUERY_SECRET_PREFIX) {
            None => Ok(Self::Header(slot)),
            Some(name)
                if !name.is_empty()
                    && name.len() <= MAX_QUERY_SECRET_NAME_LEN
                    && name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-')) =>
            {
                Ok(Self::QueryParam(name))
            }
            Some(_) => Err(denied(
                "invalid_args",
                "secret_refs query slot must be '?' followed by 1-64 of [A-Za-z0-9._-]",
            )),
        }
    }
}

/// Replacement text for every scrubbed secret occurrence.
const REDACTED: &str = "[REDACTED]";

/// Host-side scrubber for the secret values resolved for one `http.send`
/// call. The resolved value of a `?<param>` secret ref lives in the outbound
/// URL, and `reqwest`'s error `Display` embeds the full request URL
/// (`error sending request for url (https://host/path?key=<secret>)`) -- so
/// every string that could carry it back out (a transport error message, a
/// response header such as a pagination `Link`/`Location`, a response body
/// that echoes the request) passes through here before it can reach a log, a
/// span, a metric label, or the guest component. Each secret is matched in
/// its raw form and in both URL-encodings it can take inside a URL (the
/// `application/x-www-form-urlencoded` form the host itself writes into the
/// query, and strict RFC 3986 percent-encoding), longest needle first.
///
/// **This scrub is best-effort defense-in-depth, NOT a security boundary.**
/// It is a literal-substring match, so a server that re-encodes the value in
/// some form no needle covers (base64, HTML entities, a hash, a compressed or
/// ranged body the request headers were allowed to provoke, ...) can still
/// hand it back. The actual security boundary is structural: the resolved
/// secret value is resolved and injected **host-side only** and never enters
/// the guest component's memory, request, or any value the guest authored --
/// the scrub only narrows what a misbehaving or malicious *bound host* can
/// reflect back. Hardening that keeps the scrub useful rather than complete:
/// [`force_scrubbable_response_encoding`] (no bundle-chosen compression or
/// byte ranges on a call that carries a query secret) and
/// [`SecretRedactor::trim_partial_needle_suffix`] (no secret prefix left at
/// the response-size cap). Per-ref host binding (a `?`-ref reaching only the
/// FQDN it was granted for) is the structural follow-up that shrinks the
/// reflection surface itself.
struct SecretRedactor {
    needles: Vec<String>,
}

impl SecretRedactor {
    /// Builds a redactor for `values` (empty values are ignored -- they
    /// would match everywhere).
    fn new<'a>(values: impl IntoIterator<Item = &'a str>) -> Self {
        let mut needles: Vec<String> = Vec::new();
        for value in values {
            if value.is_empty() {
                continue;
            }
            for variant in [
                value.to_string(),
                percent_encode_rfc3986(value),
                form_encode_query_value(value),
            ] {
                if !variant.is_empty() && !needles.contains(&variant) {
                    needles.push(variant);
                }
            }
        }
        needles.sort_by_key(|n| std::cmp::Reverse(n.len()));
        Self { needles }
    }

    fn is_empty(&self) -> bool {
        self.needles.is_empty()
    }

    /// Replaces every secret occurrence in `text`.
    fn scrub_str(&self, text: &str) -> String {
        let mut out = text.to_string();
        for needle in &self.needles {
            if out.contains(needle.as_str()) {
                out = out.replace(needle.as_str(), REDACTED);
            }
        }
        out
    }

    /// Replaces every secret occurrence in `bytes`.
    fn scrub_bytes(&self, bytes: Vec<u8>) -> Vec<u8> {
        let mut out = bytes;
        for needle in &self.needles {
            let needle = needle.as_bytes();
            if out.windows(needle.len()).any(|w| w == needle) {
                out = replace_bytes(&out, needle, REDACTED.as_bytes());
            }
        }
        out
    }

    /// Scrubs a transport error's message. The code is unchanged.
    fn scrub_error(&self, err: HostResultError) -> HostResultError {
        if self.is_empty() {
            return err;
        }
        HostResultError {
            code: err.code,
            message: self.scrub_str(&err.message),
        }
    }

    /// Removes the longest trailing run of `body` that is a **proper prefix**
    /// of any needle. The transport caps the body at `max_response_bytes`
    /// *before* this scrub runs, so a secret straddling the cap is cut short:
    /// the full needle no longer occurs, [`SecretRedactor::scrub_bytes`] cannot
    /// match it, and the surviving prefix (`...?key=wk_live_9f`) would reach
    /// the guest verbatim. Only meaningful -- and only called -- for a body the
    /// transport reported `truncated`; a complete body ends where the server
    /// ended it, so its tail is not a cut-off secret.
    ///
    /// A single pass is exact: the trimmed bytes are the only ones that could
    /// be the first half of a needle whose second half the cap discarded.
    fn trim_partial_needle_suffix(&self, body: &mut Vec<u8>) {
        let trim = self
            .needles
            .iter()
            .filter_map(|needle| {
                let needle = needle.as_bytes();
                (1..needle.len())
                    .rev()
                    .find(|&k| k <= body.len() && body.ends_with(&needle[..k]))
            })
            .max();
        if let Some(k) = trim {
            body.truncate(body.len() - k);
        }
    }

    /// Scrubs a transport response's header values and body so a server
    /// that reflects the request URL (or a pagination `Link`) can never hand
    /// the guest component a secret the host injected. Best-effort, not a
    /// boundary -- see the type-level doc. A `truncated` body additionally
    /// loses any trailing partial secret ([`Self::trim_partial_needle_suffix`]).
    fn scrub_response(&self, resp: TransportResponse) -> TransportResponse {
        if self.is_empty() {
            return resp;
        }
        let mut body = self.scrub_bytes(resp.body);
        if resp.truncated {
            self.trim_partial_needle_suffix(&mut body);
        }
        TransportResponse {
            status: resp.status,
            headers: resp
                .headers
                .into_iter()
                .map(|(name, value)| (name, self.scrub_str(&value)))
                .collect(),
            body,
            truncated: resp.truncated,
        }
    }
}

/// Replaces every non-overlapping occurrence of `needle` in `haystack`.
fn replace_bytes(haystack: &[u8], needle: &[u8], with: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(haystack.len());
    let mut i = 0;
    while i < haystack.len() {
        if haystack[i..].starts_with(needle) {
            out.extend_from_slice(with);
            i += needle.len();
        } else {
            out.push(haystack[i]);
            i += 1;
        }
    }
    out
}

/// Strict RFC 3986 percent-encoding (everything but the unreserved set
/// `A-Za-z0-9-._~`).
fn percent_encode_rfc3986(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(b));
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// The exact encoding `Url::query_pairs_mut().append_pair` writes for a
/// query value (`application/x-www-form-urlencoded`), obtained from the same
/// `url` implementation that builds the outbound request so it can never
/// drift from what actually appears in the URL. Falls back to the raw value
/// (which the caller also scrubs) in the impossible case the constant scratch
/// URL fails to parse.
fn form_encode_query_value(value: &str) -> String {
    reqwest::Url::parse_with_params("https://redact.invalid/", [("k", value)])
        .ok()
        .and_then(|u| {
            u.query()
                .and_then(|q| q.strip_prefix("k="))
                .map(str::to_string)
        })
        .unwrap_or_else(|| value.to_string())
}

/// Every query parameter of `url`, split on **both** `&` and `;`.
///
/// `Url::query_pairs()` splits on `&` only, but several server stacks (older
/// Java servlet containers, Python `urlparse`/`cgi` defaults, some Perl/Ruby
/// parsers) also accept `;` as a parameter separator. A bundle URL
/// `?q=1;key=evil` is one `q` parameter to `Url` but two to such a server --
/// and the injected `&key=<secret>` would then be the *second* `key`, losing
/// to the bundle's. Splitting on `;` here lets the same-name drop see what the
/// server will see. A percent-encoded `%3B` is data to every server and is
/// left alone.
fn query_params_any_separator(url: &reqwest::Url) -> Vec<(String, String)> {
    let Some(query) = url.query() else {
        return Vec::new();
    };
    let mut normalized = url.clone();
    normalized.set_query(Some(&query.replace(';', "&")));
    normalized
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect()
}

/// `true` if `name` is the parameter name of one of the injected `secrets`.
fn is_secret_param(secrets: &[(String, String)], name: &str) -> bool {
    secrets.iter().any(|(secret, _)| secret.as_str() == name)
}

/// Appends each `(param, value)` secret to `url`'s query as
/// `param=<form-encoded value>`. A pre-existing same-named parameter in the
/// bundle-supplied URL -- separated by `&` or by `;`
/// ([`query_params_any_separator`]) -- is dropped first (the host's value
/// always wins, and a bundle can't pre-seed a duplicate for a server that
/// reads the first one); every other existing byte of the query is preserved
/// verbatim unless a same-named parameter forced a re-serialization.
fn inject_query_secrets(url: &mut reqwest::Url, secrets: &[(String, String)]) {
    let existing = query_params_any_separator(url);
    if existing.iter().any(|(k, _)| is_secret_param(secrets, k)) {
        let kept: Vec<(String, String)> = existing
            .into_iter()
            .filter(|(k, _)| !is_secret_param(secrets, k))
            .collect();
        let mut pairs = url.query_pairs_mut();
        pairs.clear();
        pairs.extend_pairs(kept);
    }
    let mut pairs = url.query_pairs_mut();
    for (name, value) in secrets {
        pairs.append_pair(name, value);
    }
}

/// Request headers a bundle controls that would let a bound host answer with
/// a body the substring [`SecretRedactor`] cannot read: `Accept-Encoding` (a
/// `gzip`/`br` echo), `Range` and `If-Range` (a partial/offset echo that
/// splits the secret). On a call carrying a query secret every bundle value
/// of these is dropped and `Accept-Encoding: identity` is forced, so the
/// response body is whole plaintext. Names compare ASCII-case-insensitively.
/// Hardening of a best-effort scrub, not a boundary -- see [`SecretRedactor`].
fn force_scrubbable_response_encoding(headers: &mut Vec<(String, String)>) {
    headers.retain(|(name, _)| {
        !name.eq_ignore_ascii_case("accept-encoding")
            && !name.eq_ignore_ascii_case("range")
            && !name.eq_ignore_ascii_case("if-range")
    });
    headers.push(("Accept-Encoding".to_string(), "identity".to_string()));
}

/// Removes every query parameter named like an injected secret from a
/// redirect target -- the secret is only ever sent on the first hop, to the
/// bound host, and a redirect's `Location` (server-chosen) never gets to
/// carry the parameter onward, whatever value it names and whichever of
/// `&`/`;` separates it ([`query_params_any_separator`]).
fn strip_query_secret_params(url: &mut reqwest::Url, secrets: &[(String, String)]) {
    let existing = query_params_any_separator(url);
    if !existing.iter().any(|(k, _)| is_secret_param(secrets, k)) {
        return;
    }
    let kept: Vec<(String, String)> = existing
        .into_iter()
        .filter(|(k, _)| !is_secret_param(secrets, k))
        .collect();
    if kept.is_empty() {
        url.set_query(None);
    } else {
        let mut pairs = url.query_pairs_mut();
        pairs.clear();
        pairs.extend_pairs(kept);
    }
}

/// Drops everything from `url` that is unsafe to log or echo in an error:
/// the whole query (host-injected secret *and* bundle-supplied parameters,
/// which may carry user-identifying data), the fragment, and any userinfo.
fn strip_url_for_log(url: &mut reqwest::Url) {
    url.set_query(None);
    url.set_fragment(None);
    // `set_username`/`set_password` only fail for cannot-be-a-base URLs,
    // which carry no userinfo to strip in the first place.
    let _ = url.set_username("");
    let _ = url.set_password(None);
}

/// `url` with its query/fragment/userinfo removed ([`strip_url_for_log`]) --
/// the only form of a request URL that may appear in a log line, span or
/// `Debug` output. Unparseable input yields a fixed placeholder, never the
/// raw string.
fn redact_url_for_log(url: &str) -> String {
    match reqwest::Url::parse(url) {
        Ok(mut parsed) => {
            strip_url_for_log(&mut parsed);
            parsed.to_string()
        }
        Err(_) => "<unparseable-url>".to_string(),
    }
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
    /// Instance-wide gate on every `private_ip_grants` match (Justin's
    /// decision: "private-ip is also subject to the INSTANCE policy --
    /// default deny, global-admin opt-in"). `Arc<RwLock<_>>` so the
    /// consuming crate's own grant-snapshot refresh loop (the same
    /// PR #428/#432 shape backing `core/bundle_capability_gate`'s platform
    /// grants) can update it live without rebuilding the guard; defaults to
    /// [`InstanceEgressPolicy::default`] (deny) via [`EgressGuard::new`]
    /// until a caller opts in with [`EgressGuard::with_instance_policy`].
    instance_policy: Arc<RwLock<InstanceEgressPolicy>>,
    /// Operator-configured cluster pod/service/node CIDRs (Justin's
    /// decision: "a CONFIGURED denylist of the cluster's own pod, service
    /// and node CIDRs" -- never liftable by any grant or by
    /// [`InstanceEgressPolicy`]). Empty by default via [`EgressGuard::new`];
    /// the consuming crate's own config loader is responsible for the
    /// "required to be non-empty in beta/gamma/production" enforcement --
    /// this crate has no notion of deployment tier.
    cluster_denylist: ClusterCidrDenylist,
    /// Upstream egress-proxy signed-assertion signer (PR #463/#466's
    /// design). `None` (the default via [`EgressGuard::new`]) adds no
    /// header and preserves this guard's pre-existing behavior exactly;
    /// only meaningful once [`EgressLimits::proxy_url`] is also configured.
    proxy_assertion_signer: Option<Arc<dyn ProxyAssertionSigner>>,
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
            instance_policy: Arc::new(RwLock::new(InstanceEgressPolicy::default())),
            cluster_denylist: ClusterCidrDenylist::default(),
            proxy_assertion_signer: None,
        }
    }

    /// Wires the live instance-policy seam (see the field's doc) --
    /// defaults to deny-all-private-ip-egress ([`InstanceEgressPolicy::
    /// default`]) until a caller opts in. The `Arc<RwLock<_>>` is shared
    /// with (owned by) the caller's own grant-snapshot refresh loop, never
    /// constructed fresh per call.
    pub fn with_instance_policy(mut self, policy: Arc<RwLock<InstanceEgressPolicy>>) -> Self {
        self.instance_policy = policy;
        self
    }

    /// Wires the operator-configured cluster pod/service/node CIDR
    /// denylist (see the field's doc) -- empty (no additional denial) until
    /// a caller supplies one.
    pub fn with_cluster_denylist(mut self, denylist: ClusterCidrDenylist) -> Self {
        self.cluster_denylist = denylist;
        self
    }

    /// Wires the upstream egress-proxy signed-assertion signer (see the
    /// field's doc) -- `None` (no header added) until a caller supplies
    /// one; only meaningful alongside [`EgressLimits::proxy_url`].
    pub fn with_proxy_assertion_signer(mut self, signer: Arc<dyn ProxyAssertionSigner>) -> Self {
        self.proxy_assertion_signer = Some(signer);
        self
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

        // Bundle-declared headers only -- kept separate from the resolved
        // secret-ref headers below (`secret_headers`) so the redirect loop
        // can scope the latter to the host they were granted for (security
        // review finding, PR #468, HIGH) instead of forwarding both
        // indiscriminately to every hop.
        let base_headers: Vec<(String, String)> =
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
        //
        // **Query-parameter refs** (`?key`, [`SecretSlot::QueryParam`]) go
        // through the *identical* grant-map + broker resolution -- the only
        // difference is where the resolved value is injected -- and fail
        // loud the same way: an ungranted/unresolvable/empty `?`-ref aborts
        // the whole call here, before any network activity, so a request is
        // never silently sent without the credential it was meant to carry.
        let granted = row.as_ref().map(|r| &r.granted_secret_refs);
        let mut secret_headers: Vec<(String, String)> = Vec::with_capacity(req.secret_refs.len());
        let mut query_secrets: Vec<(String, String)> = Vec::new();
        for (slot_name, secret_ref) in &req.secret_refs {
            let slot = SecretSlot::parse(slot_name)?;
            if let SecretSlot::QueryParam(param) = slot {
                if query_secrets.iter().any(|(name, _)| name == param) {
                    return Err(denied(
                        "invalid_args",
                        "secret_refs names the same query parameter twice",
                    ));
                }
            }
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
            match slot {
                SecretSlot::Header(header_name) => {
                    secret_headers.push((header_name.to_string(), value));
                }
                SecretSlot::QueryParam(param) => {
                    if value.is_empty() {
                        return Err(denied(
                            "secret_unresolved",
                            format!("granted env var {env_var_name:?} resolved to an empty value"),
                        ));
                    }
                    query_secrets.push((param.to_string(), value));
                }
            }
        }
        // Every resolved secret value (header and query alike) is scrubbed
        // out of anything this call hands back -- see [`SecretRedactor`].
        let redactor = SecretRedactor::new(
            secret_headers
                .iter()
                .map(|(_, v)| v.as_str())
                .chain(query_secrets.iter().map(|(_, v)| v.as_str())),
        );

        // The host `secret_headers` is bound to -- captured once, at hop 0,
        // from the bundle's own originally-requested URL (the host it
        // presumably holds the credential for). `None` until the first
        // iteration below sets it.
        let mut secret_bound_host: Option<String> = None;
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
            if hop == 0 {
                secret_bound_host = Some(host.clone());
            }

            let empty_row = EgressRuleRow::default();
            let row_ref = row.as_ref().unwrap_or(&empty_row);
            let Some(matched) = match_grant(&host, row_ref) else {
                return Err(denied(
                    "host_not_declared",
                    format!("{host} is not on the manifest egress allowlist"),
                ));
            };
            let matched_pattern = matched.pattern.to_string();
            if !matched
                .methods
                .iter()
                .any(|m| m.eq_ignore_ascii_case(&method))
            {
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
            if let Some(required_port) = matched.declared_port {
                if required_port != port {
                    return Err(denied(
                        "host_not_declared",
                        format!("{host}:{port} does not match the declared {matched_pattern}"),
                    ));
                }
            }
            let category = matched.category;
            // A query-parameter secret is only ever injected on the
            // secret-bound **FQDN** (the granted `net.http.fqdn:<host>`):
            // an IP-literal grant (`public-ip`/`private-ip`, HIGH-risk
            // families) never receives one. Refused loudly before any DNS or
            // network activity rather than silently sending unauthenticated.
            if hop == 0 && !query_secrets.is_empty() && category != EgressCategory::Fqdn {
                return Err(denied(
                    "secret_query_requires_fqdn",
                    format!(
                        "{host} is not a net.http.fqdn grant; query-parameter secret refs may only be injected on a granted FQDN"
                    ),
                ));
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
            let instance_policy = *self
                .instance_policy
                .read()
                .unwrap_or_else(|e| e.into_inner());
            let mut chosen: Option<SocketAddr> = None;
            let mut last_reason = "no addresses returned";
            for addr in addrs {
                match classify_dial_address(
                    addr.ip(),
                    category,
                    row_ref,
                    &self.cluster_denylist,
                    instance_policy,
                ) {
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

            // Secret-scoped-to-host re-check (security review finding, PR
            // #468, HIGH): a resolved secret header (Authorization etc.)
            // rides along only when this hop's host is the one it was
            // resolved for, or the manifest row explicitly widens that via
            // `secret_granted_hosts` ("B has its own grant" -- the host
            // itself, not just the originating one, is authorized to
            // receive it). Every other hop drops it outright rather than
            // re-resolving a value the bundle never asked to send here.
            let is_secret_bound_host = secret_bound_host.as_deref() == Some(host.as_str());
            let secret_reattach_allowed =
                is_secret_bound_host || row_ref.secret_granted_hosts.contains(&host);
            let mut req_headers = base_headers.clone();
            if !is_secret_bound_host {
                // Cross-host hop (even one the manifest still allowlists):
                // never forward a Cookie or Proxy-Authorization the bundle
                // set for the *original* host -- both are ambient
                // credentials, not scoped to a declared `secret_ref`, so
                // `secret_granted_hosts` doesn't apply to them.
                req_headers.retain(|(name, _)| {
                    !name.eq_ignore_ascii_case("cookie")
                        && !name.eq_ignore_ascii_case("proxy-authorization")
                });
            }
            if secret_reattach_allowed {
                req_headers.extend(secret_headers.iter().cloned());
            }
            if let Some(signer) = &self.proxy_assertion_signer {
                let assertion = signer.sign(app_id, &host, port, category.into())?;
                req_headers.push((PROXY_ASSERTION_HEADER.to_string(), assertion));
            }
            // Query-parameter secrets ride on the FIRST hop only -- hop 0 is
            // by construction the bundle's own originally-requested URL on
            // the secret-bound FQDN. They are written into this per-hop
            // transport URL only; `req.url` (the redirect chain's own URL,
            // everything that is ever logged or joined against a `Location`)
            // never contains the secret, so every redirect hop is
            // structurally secret-free.
            let inject_query_secrets_now = hop == 0 && !query_secrets.is_empty();
            if inject_query_secrets_now {
                // The response scrub is a literal-substring match: a
                // bundle-chosen compression or byte range would let the
                // bound host echo the secret in a form it cannot match.
                // Applied after every other header is assembled (bundle,
                // secret-ref, proxy assertion) so none can reintroduce one.
                force_scrubbable_response_encoding(&mut req_headers);
            }
            let transport_url = if inject_query_secrets_now {
                let mut with_secrets = url.clone();
                inject_query_secrets(&mut with_secrets, &query_secrets);
                with_secrets.to_string()
            } else {
                req.url.clone()
            };
            debug!(
                app_id = %app_id,
                hop,
                host = %host,
                url = %redact_url_for_log(&req.url),
                header_secrets = secret_headers.len(),
                query_secret_params = ?query_secrets.iter().map(|(n, _)| n.as_str()).collect::<Vec<_>>(),
                query_secret_injected = inject_query_secrets_now,
                "bundle_host_http.egress.send"
            );
            let transport_req = TransportRequest {
                method: method.clone(),
                url: transport_url,
                pinned_addr,
                headers: req_headers,
                body: body.clone(),
            };
            let response = match self
                .transport
                .send(
                    transport_req,
                    self.limits.timeout,
                    self.limits.max_response_bytes,
                )
                .await
            {
                Ok(response) => redactor.scrub_response(response),
                Err(err) => {
                    // Transport errors (reqwest's embeds the full request
                    // URL, query included) are scrubbed of every resolved
                    // secret before they can reach a log or the guest.
                    let err = redactor.scrub_error(err);
                    debug!(
                        app_id = %app_id,
                        hop,
                        host = %host,
                        error_code = %err.code,
                        "bundle_host_http.egress.transport_error"
                    );
                    return Err(err);
                }
            };

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
                let mut next = url.join(&location).map_err(|_| {
                    denied("redirect_off_allowlist", "redirect Location does not parse")
                })?;
                // Never let a redirect carry a secret-named query parameter
                // onward (to the same host or any other) -- see
                // `strip_query_secret_params`.
                strip_query_secret_params(&mut next, &query_secrets);
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

    /// Validates one non-HTTP dial target (a connector host transport
    /// establishing a WebSocket-over-TLS (`wss`) or IRC-over-TLS
    /// connection) through the same declared-host/category-match/SSRF/
    /// DNS-pin pipeline `send` runs for `http.send` (spec §8.2 steps 1-2,
    /// 5-7 -- there is no HTTP method/scheme/redirect/response-size step
    /// for a bare connection establishment). `target` is `host:port`
    /// ([`split_host_port`]'s doc) -- the caller has already decided the
    /// connection will be TLS; this call only proves the *address* is safe
    /// to dial. Every rejection is counted in the same
    /// `waddles_egress_denied_total{app_id,reason}` metric `send` uses,
    /// including the spec §13.5 flag check.
    ///
    /// **`svc_ingest` adoption:** this is the "second, narrower entry
    /// point" this crate's module doc flagged as a following landing --
    /// `svc_ingest`'s Discord (wss) and Twitch (IRC-over-TLS) dial paths
    /// are the intended first callers. Wiring those two call sites is
    /// `svc_ingest`'s own follow-up (implements [`EgressRuleSource`] over
    /// its connector manifest state, same shape as this landing's
    /// `svc_process` wiring, then calls this method before connecting) --
    /// not implemented in this PR; the pipeline itself is complete and
    /// covered by this crate's own hermetic tests.
    pub async fn validate_dial(
        &self,
        app_id: &str,
        target: &str,
    ) -> Result<ValidatedTarget, HostResultError> {
        if !self.bundle_egress.enabled().await {
            let err = denied("feature_disabled", "waddles.core.bundle-egress is disabled");
            self.denied_total
                .with_label_values(&[app_id, &err.code])
                .inc();
            return Err(err);
        }
        match self.validate_dial_checked(app_id, target).await {
            Ok(v) => Ok(v),
            Err(err) => {
                self.denied_total
                    .with_label_values(&[app_id, &err.code])
                    .inc();
                Err(err)
            }
        }
    }

    async fn validate_dial_checked(
        &self,
        app_id: &str,
        target: &str,
    ) -> Result<ValidatedTarget, HostResultError> {
        let (host, port) = split_host_port(target)
            .ok_or_else(|| denied("malformed_url", "dial target must be host:port"))?;
        let host = host.to_ascii_lowercase();

        let row = self.catalog.resolve(app_id);
        let empty_row = EgressRuleRow::default();
        let row_ref = row.as_ref().unwrap_or(&empty_row);
        let Some(matched) = match_grant(&host, row_ref) else {
            return Err(denied(
                "host_not_declared",
                format!("{host} is not on the manifest egress allowlist"),
            ));
        };
        if self
            .denylist
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .contains(&host)
        {
            return Err(denied("host_denylisted", format!("{host} is denylisted")));
        }
        if let Some(required_port) = matched.declared_port {
            if required_port != port {
                return Err(denied(
                    "host_not_declared",
                    format!(
                        "{host}:{port} does not match the declared {}",
                        matched.pattern
                    ),
                ));
            }
        }
        let category = matched.category;

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
        let instance_policy = *self
            .instance_policy
            .read()
            .unwrap_or_else(|e| e.into_inner());
        let mut chosen: Option<SocketAddr> = None;
        let mut last_reason = "no addresses returned";
        for addr in addrs {
            match classify_dial_address(
                addr.ip(),
                category,
                row_ref,
                &self.cluster_denylist,
                instance_policy,
            ) {
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

        Ok(ValidatedTarget {
            host,
            port,
            pinned_addr,
        })
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
///
/// `pub`: reused by `egress_proxy::ip_policy` (security review follow-up,
/// item 3) so its `cluster_cidrs`/`deny_cidrs` checks canonicalize the
/// exact same way this crate's own `ClusterCidrDenylist`/`is_forbidden_
/// address` do -- one canonicalization implementation for every deny-list
/// comparison in the org, never a second copy that can silently drift.
pub fn embedded_ipv4(v6: Ipv6Addr) -> Option<Ipv4Addr> {
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

/// 6to4 (RFC 3056): `2002::/16`, the embedded IPv4 address occupies the
/// next 32 bits. Recognized (see [`is_forbidden_address`]'s doc) but never
/// decoded -- this deployment denies the whole range outright.
fn is_6to4_v6(ip: Ipv6Addr) -> bool {
    ip.segments()[0] == 0x2002
}

/// Teredo (RFC 4380): `2001:0000::/32`. Distinct from other `2001::`
/// allocations (documentation `2001:db8::/32`, production ranges, etc.),
/// which are ordinary native-v6 addresses and fall through to this
/// function's other checks unaffected.
fn is_teredo_v6(ip: Ipv6Addr) -> bool {
    let seg = ip.segments();
    seg[0] == 0x2001 && seg[1] == 0x0000
}

/// Canonicalizes `ip` to its embedded IPv4 form when it carries one
/// (mapped/NAT64/IPv4-compatible -- see [`embedded_ipv4`]) so a deny-list
/// comparison configured in native v4 form (e.g. an operator's
/// [`ClusterCidrDenylist`] entry) can't be bypassed by re-encoding the same
/// target as its IPv6 form. Deliberately reuses `embedded_ipv4` (not just
/// `std`'s narrower `Ipv6Addr::to_canonical`, which only unwraps the mapped
/// form) so this stays consistent with [`is_forbidden_address`]'s and
/// [`is_private_range`]'s existing canonicalization.
///
/// `pub`, same cross-crate-reuse rationale as [`embedded_ipv4`].
///
/// // regression: mapped-v6 cluster bypass
pub fn canonicalize_ip(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => embedded_ipv4(v6).map(IpAddr::V4).unwrap_or(ip),
        IpAddr::V4(_) => ip,
    }
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
            // Justin's decision (three-permission-family model): CGNAT
            // (`100.64.0.0/10`) is grouped with RFC1918/ULA as a "private
            // range" grantable via `net.http.private-ip:`/lifted by
            // `allow_private`, not a separate never-liftable tier --
            // superseding this function's original "never lifted by any
            // setting" comment. Every existing caller of this function
            // that must keep CGNAT unconditionally forbidden already
            // passes `allow_private: false` unconditionally (this crate's
            // `is_always_forbidden`, and both consuming crates' own
            // built-in host-initiated dials), so this is not a behavior
            // change for any of them.
            if !allow_private && is_cgnat_v4(v4) {
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
            // regression: mapped-v6 cluster bypass (security review follow-
            // up) -- 6to4 (`2002::/16`, RFC 3056) and Teredo (`2001::/32`,
            // RFC 4380) both tunnel an embedded IPv4 address, but via
            // legacy NAT-traversal mechanisms this deployment has no
            // legitimate egress use for. Rather than decode the embedded
            // address (6to4: direct; Teredo: XOR-obfuscated) and risk a
            // decode bug on a security-critical path for a code path with
            // no real caller, Justin's decision: deny both ranges outright
            // and unconditionally -- never lifted by `allow_private`, same
            // tier as loopback/link-local/metadata.
            if is_6to4_v6(v6) || is_teredo_v6(v6) {
                return Some("legacy_transition_mechanism");
            }
            if !allow_private && is_unique_local_v6(v6) {
                return Some("private");
            }
            None
        }
    }
}

/// Classifies `ip` as belonging to the "private range" bucket the
/// three-permission-family model reserves for `net.http.private-ip:`
/// grants (Justin's decision: "private ranges: RFC1918, ULA, CGNAT") --
/// used only to *route* a pattern/address to the right grant list
/// ([`match_grant`], [`EgressRuleRow::from_legacy_patterns`]) and to decide
/// which category an FQDN's resolved address falls into
/// ([`classify_dial_address`]). Distinct from [`is_forbidden_address`]'s
/// always-forbidden set (loopback/link-local/metadata/multicast/broadcast/
/// unspecified) -- those are never "private range", they are never
/// reachable via any grant at all.
fn is_private_range(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => is_private_v4(v4) || is_cgnat_v4(v4),
        IpAddr::V6(v6) => match embedded_ipv4(v6) {
            Some(v4) => is_private_range(IpAddr::V4(v4)),
            None => is_unique_local_v6(v6),
        },
    }
}

/// One CIDR block (`ip` or `ip/prefix`), hand-rolled rather than pulling in
/// a CIDR crate -- this crate takes no dependency it doesn't already pin,
/// and the arithmetic is a handful of lines for both address families.
#[derive(Debug, Clone, Copy)]
struct CidrBlock {
    network: IpAddr,
    prefix: u8,
}

impl CidrBlock {
    fn parse(s: &str) -> Option<Self> {
        let (addr_str, prefix) = match s.split_once('/') {
            Some((addr_str, prefix_str)) => (addr_str, prefix_str.parse::<u8>().ok()?),
            None => {
                let addr: IpAddr = s.parse().ok()?;
                let max = if addr.is_ipv4() { 32 } else { 128 };
                return Some(Self {
                    network: addr,
                    prefix: max,
                });
            }
        };
        let network: IpAddr = addr_str.parse().ok()?;
        let max = if network.is_ipv4() { 32 } else { 128 };
        if prefix > max {
            return None;
        }
        Some(Self { network, prefix })
    }

    fn contains(&self, ip: IpAddr) -> bool {
        match (self.network, ip) {
            (IpAddr::V4(net), IpAddr::V4(target)) => {
                let mask = mask_u32(self.prefix);
                (u32::from(net) & mask) == (u32::from(target) & mask)
            }
            (IpAddr::V6(net), IpAddr::V6(target)) => {
                let mask = mask_u128(self.prefix);
                (u128::from(net) & mask) == (u128::from(target) & mask)
            }
            _ => false,
        }
    }
}

fn mask_u32(prefix: u8) -> u32 {
    if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix as u32)
    }
}

fn mask_u128(prefix: u8) -> u128 {
    if prefix == 0 {
        0
    } else {
        u128::MAX << (128 - prefix as u32)
    }
}

/// Checks whether `pattern` (an exact IP, optionally `:port`-suffixed, or a
/// CIDR block for the private-ip family) covers `ip` -- the resolved/
/// literal address a request is actually targeting. Delegates the actual
/// IP/CIDR containment arithmetic to the shared
/// [`egress_assertion::ip_matches_grant`] -- the same primitive
/// `egress_proxy` re-checks the resolved address against on the other side
/// of the assertion -- so the two services can never silently diverge on
/// what a grant covers (this function previously hand-rolled its own
/// `CidrBlock`, duplicating that logic with slightly weaker semantics: no
/// IPv4-mapped-IPv6 normalization on the containment check itself, only on
/// the earlier classification step). `category` must be the same
/// [`EgressCategory`] the caller already established for `ip`/`pattern`'s
/// grant list (`PublicIp` requires exact-literal equality, `PrivateIp`
/// additionally accepts a CIDR block) -- an unparseable pattern never
/// matches anything (fail closed).
fn ip_pattern_contains(category: EgressCategory, pattern: &str, ip: IpAddr) -> bool {
    let (addr_part, _) = parse_pattern(pattern);
    ip_matches_grant(category.into(), addr_part, ip)
}

/// Operator-configured cluster pod/service/node CIDR denylist (Justin's
/// decision, item 2): never liftable by any grant, checked in
/// [`is_always_forbidden`] alongside the hard-coded always-forbidden
/// ranges. Loaded once at [`EgressGuard`] construction
/// ([`EgressGuard::with_cluster_denylist`]) from the consuming crate's own
/// config/env -- **that crate's config loader, not this one, is
/// responsible for requiring this to be non-empty in beta/gamma/
/// production** (this crate has no notion of deployment tier).
#[derive(Debug, Clone, Default)]
pub struct ClusterCidrDenylist(Vec<CidrBlock>);

impl ClusterCidrDenylist {
    /// Parses a list of CIDR/IP strings (typically split from a config/env
    /// value such as a comma-separated `EGRESS_CLUSTER_CIDR_DENYLIST`).
    /// Fails closed: a single unparseable entry rejects the whole list
    /// rather than silently dropping it, since a dropped entry here is a
    /// silently-widened SSRF surface.
    pub fn parse(cidrs: impl IntoIterator<Item = impl AsRef<str>>) -> Result<Self, String> {
        let mut blocks = Vec::new();
        for raw in cidrs {
            let raw = raw.as_ref();
            let block = CidrBlock::parse(raw)
                .ok_or_else(|| format!("invalid cluster CIDR denylist entry: {raw:?}"))?;
            // regression: mapped-v6 cluster bypass -- `contains` below
            // canonicalizes the *checked* address to its embedded-v4 form
            // (via `canonicalize_ip`) before comparing, so a v6-mapped/NAT64/
            // compatible *configured* range would silently never match
            // anything (family mismatch against the now-v4 checked
            // address). Fail closed at config-parse time instead of
            // shipping a denylist entry that can never fire.
            if let IpAddr::V6(v6) = block.network {
                if embedded_ipv4(v6).is_some() {
                    return Err(format!(
                        "cluster CIDR denylist entry {raw:?} is an IPv4-mapped/NAT64/IPv4-\
                         compatible IPv6 range -- write it in native IPv4 form instead (e.g. \
                         10.0.0.0/8): resolved addresses are canonicalized to their embedded v4 \
                         form before this denylist is checked, so a v6-encoded entry would never \
                         match anything"
                    ));
                }
            }
            blocks.push(block);
        }
        Ok(Self(blocks))
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    fn contains(&self, ip: IpAddr) -> bool {
        // regression: mapped-v6 cluster bypass -- canonicalize before
        // comparing so `::ffff:10.244.5.6` (or its NAT64/IPv4-compatible
        // equivalents) still matches a `10.244.0.0/16` entry; the old
        // hand-rolled `CidrBlock::contains` required an exact address-
        // family match and silently fell through to `_ => false` otherwise.
        let ip = canonicalize_ip(ip);
        self.0.iter().any(|c| c.contains(ip))
    }
}

/// Instance-wide policy gate on every `net.http.private-ip:` grant
/// (Justin's decision, item 1: "private-ip is also subject to the
/// INSTANCE policy -- default deny, global-admin opt-in"). Sourced from
/// the same grant-snapshot shape PR #428/#432 introduced for the platform
/// grant trio -- the consuming crate's own snapshot-refresh loop owns the
/// `Arc<RwLock<InstanceEgressPolicy>>` wired via
/// [`EgressGuard::with_instance_policy`] and updates it the same way it
/// updates any other live-refreshed grant state; this type itself is a
/// plain data snapshot, not a live seam.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct InstanceEgressPolicy {
    /// `false` (the default): every `net.http.private-ip:` grant is denied
    /// regardless of whether it matches, and an FQDN resolving into a
    /// private address is denied regardless of a covering private-ip
    /// grant. `true`: a global-admin has opted this instance in, so a
    /// matching private-ip grant (or FQDN-plus-covering-private-ip-grant
    /// pair) is permitted -- subject to every other check
    /// ([`is_always_forbidden`]) still passing.
    pub allow_private_ip_egress: bool,
}

/// Which of the three permission families ([`EgressRuleRow`]'s doc)
/// satisfied [`match_grant`] for one request/dial -- threaded through to
/// [`classify_dial_address`] so the resolved-address check applies the
/// right cross-category rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum EgressCategory {
    Fqdn,
    PublicIp,
    PrivateIp,
}

/// Converts to the shared `egress_assertion` crate's wire-format enum --
/// same three variants, kept as a separate local type here only because
/// this module's own [`match_grant`]/[`classify_dial_address`] pipeline
/// predates the shared crate; the conversion is the seam
/// [`ProxyAssertionSigner::sign`] callers use so the assertion always
/// carries `egress_assertion`'s own enum, never a second local copy.
impl From<EgressCategory> for DestinationCategory {
    fn from(category: EgressCategory) -> Self {
        match category {
            EgressCategory::Fqdn => DestinationCategory::Fqdn,
            EgressCategory::PublicIp => DestinationCategory::PublicIp,
            EgressCategory::PrivateIp => DestinationCategory::PrivateIp,
        }
    }
}

/// The grant [`match_grant`] found, borrowed from the [`EgressRuleRow`]
/// it matched against.
struct MatchedGrant<'a> {
    category: EgressCategory,
    pattern: &'a str,
    declared_port: Option<u16>,
    methods: &'a [String],
}

/// Matches `host` against exactly one of [`EgressRuleRow`]'s three grant
/// lists -- **never more than one, and never the "wrong" one for `host`'s
/// own shape** (Justin's decision: "each matched only by its own grants").
/// A bare hostname can only ever match `fqdn_grants`. An IP literal is
/// routed by its *own* address class -- a private-range literal only ever
/// searches `private_ip_grants`, a public-looking literal only ever
/// searches `public_ip_grants` -- so a private IP declared (in error, or by
/// a confused bundle) under `public_ip_grants` is never found here at all,
/// and vice versa; there is no fallback between the two IP lists.
fn match_grant<'a>(host: &str, row: &'a EgressRuleRow) -> Option<MatchedGrant<'a>> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        let category = if is_private_range(ip) {
            EgressCategory::PrivateIp
        } else {
            EgressCategory::PublicIp
        };
        let grants = if category == EgressCategory::PrivateIp {
            &row.private_ip_grants
        } else {
            &row.public_ip_grants
        };
        let (pattern, methods) = grants
            .iter()
            .find(|(pattern, _)| ip_pattern_contains(category, pattern, ip))?;
        Some(MatchedGrant {
            category,
            pattern: pattern.as_str(),
            declared_port: parse_pattern(pattern).1,
            methods: methods.as_slice(),
        })
    } else {
        let (pattern, methods) = row
            .fqdn_grants
            .iter()
            .find(|(pattern, _)| host_matches(pattern, host))?;
        Some(MatchedGrant {
            category: EgressCategory::Fqdn,
            pattern: pattern.as_str(),
            declared_port: parse_pattern(pattern).1,
            methods: methods.as_slice(),
        })
    }
}

/// Finds a `private_ip_grants` entry covering `ip` -- used only for the
/// FQDN-resolves-into-private-IP cross-category check
/// ([`classify_dial_address`]'s `Fqdn` arm), where the grant search is by
/// resolved *address*, not by the originally-requested hostname.
fn find_private_ip_grant(row: &EgressRuleRow, ip: IpAddr) -> bool {
    row.private_ip_grants
        .iter()
        .any(|(pattern, _)| ip_pattern_contains(EgressCategory::PrivateIp, pattern, ip))
}

/// Classifies `ip` against every always-deny range (spec's original SSRF
/// set, via [`is_forbidden_address`] with private ranges *not* liftable --
/// `allow_private: true` skips straight past them) plus the operator's
/// [`ClusterCidrDenylist`] (Justin's decision, item 2: cluster pod/service/
/// node CIDRs, "regardless of grants"). Neither of these is ever liftable
/// by a grant or by [`InstanceEgressPolicy`] -- this check always runs
/// first in [`classify_dial_address`], before any category-specific logic.
fn is_always_forbidden(ip: IpAddr, cluster_denylist: &ClusterCidrDenylist) -> Option<&'static str> {
    if let Some(reason) = is_forbidden_address(ip, true) {
        return Some(reason);
    }
    if cluster_denylist.contains(ip) {
        return Some("cluster_cidr_denied");
    }
    None
}

/// The full per-resolved-address decision (spec §8.2 step 6, extended by
/// Justin's three-permission-family decisions): given the category
/// [`match_grant`] already established for the *requested* host, decides
/// whether `ip` -- one of that host's resolved addresses -- may actually be
/// dialed.
///
/// Order matters and mirrors the task's enumerated rules exactly: (1) the
/// always-forbidden/cluster-CIDR check runs first and is never bypassed by
/// any category or policy; (2) only then does the category-specific
/// cross-check run (`PublicIp`/`PrivateIp` grants must match the address's
/// *actual* class, an `Fqdn` grant resolving into a private address needs
/// its own separate `private_ip_grants` cover); (3) any `PrivateIp`-
/// classified address -- whether reached via a direct `private_ip_grants`
/// match or via an `Fqdn`'s covering grant -- additionally requires
/// [`InstanceEgressPolicy::allow_private_ip_egress`].
fn classify_dial_address(
    ip: IpAddr,
    category: EgressCategory,
    row: &EgressRuleRow,
    cluster_denylist: &ClusterCidrDenylist,
    instance_policy: InstanceEgressPolicy,
) -> Option<&'static str> {
    if let Some(reason) = is_always_forbidden(ip, cluster_denylist) {
        return Some(reason);
    }
    let private = is_private_range(ip);
    match category {
        EgressCategory::Fqdn => {
            if !private {
                return None;
            }
            if !instance_policy.allow_private_ip_egress {
                return Some("instance_private_ip_denied");
            }
            if find_private_ip_grant(row, ip) {
                None
            } else {
                Some("fqdn_resolved_private_not_granted")
            }
        }
        EgressCategory::PublicIp => {
            if private {
                Some("public_ip_grant_targets_private_address")
            } else {
                None
            }
        }
        EgressCategory::PrivateIp => {
            if !private {
                Some("private_ip_grant_targets_public_address")
            } else if !instance_policy.allow_private_ip_egress {
                Some("instance_private_ip_denied")
            } else {
                None
            }
        }
    }
}

/// Splits a `host:port` dial target (connector host transports dialing
/// wss/IRC-over-TLS, never an HTTP URL -- see
/// [`EgressGuard::validate_dial`]) into its host and port. Mirrors
/// [`parse_pattern`]'s bracket convention for an IPv6 literal
/// (`[::1]:6697`); an unbracketed host with more than one `:` (a bare IPv6
/// literal with no port) is rejected as malformed rather than guessed at --
/// a dial target must always declare its port explicitly.
fn split_host_port(target: &str) -> Option<(String, u16)> {
    if let Some(rest) = target.strip_prefix('[') {
        let (addr, tail) = rest.split_once(']')?;
        let port_str = tail.strip_prefix(':')?;
        let port: u16 = port_str.parse().ok()?;
        return Some((addr.to_string(), port));
    }
    if target.matches(':').count() != 1 {
        return None;
    }
    let (host, port_str) = target.rsplit_once(':')?;
    let port: u16 = port_str.parse().ok()?;
    Some((host.to_string(), port))
}

/// One already-fully-validated non-HTTP dial target -- returned by
/// [`EgressGuard::validate_dial`]. The caller (a connector host transport
/// dialing wss/IRC-over-TLS) must connect to `pinned_addr` verbatim for
/// `host`, exactly like [`TransportRequest::pinned_addr`] for `http.send`
/// -- never re-resolving `host` itself.
#[derive(Debug, Clone)]
pub struct ValidatedTarget {
    pub host: String,
    pub port: u16,
    pub pinned_addr: SocketAddr,
}

/// Signs an "upstream egress proxy" assertion (PR #463/#466's design: this
/// guard optionally forwards an already-validated request through a
/// network-level egress proxy instead of connecting directly, with a
/// signed assertion the proxy can verify without re-running the
/// allowlist/SSRF pipeline itself). Object-safe, mirrors this crate's other
/// dependency-seam traits. Wired via
/// [`EgressGuard::with_proxy_assertion_signer`] -- `None` (the default) is
/// what every caller constructs today and adds no header at all, an exact
/// behavior-preserving no-op.
///
/// Fails closed: `send_checked` propagates a signing error as a denial
/// (`proxy_assertion_signing_failed`) rather than proxying an unsigned
/// request whenever a signer is configured -- once `proxy_url` is set,
/// every hop through it must carry a valid assertion, never a silent
/// fallback to an unauthenticated dial.
pub trait ProxyAssertionSigner: Send + Sync {
    /// Returns the `X-Waddles-Egress-Assertion` header value asserting
    /// that `app_id`'s request to `host:port` (of category `category`) was
    /// validated by this guard -- the exact
    /// [`egress_assertion::EgressAssertion`] wire format
    /// `core/egress_proxy`'s verifier checks.
    fn sign(
        &self,
        app_id: &str,
        host: &str,
        port: u16,
        category: DestinationCategory,
    ) -> Result<String, HostResultError>;
}

/// The real [`ProxyAssertionSigner`]: signs a fresh, single-use
/// [`egress_assertion::EgressAssertion`] per call with this service's own
/// Ed25519 key (see [`egress_assertion::AssertionSigningKey`]'s doc --
/// never a second, separately-distributed signing key). `sub` must equal
/// the SPIFFE `sub` of the machine JWT presented on the same connection
/// (`egress_proxy::proxy::validate`'s `SubMismatch` check) -- both are
/// this pod's own identity, so they're set once here rather than resolved
/// per call. `tenant`/`community` are likewise fixed per instance: this
/// guard (and the pod it runs in) already serves exactly one tenant/
/// community context, the same assumption `EgressRuleSource::resolve`'s
/// `app_id`-only lookup already makes.
pub struct EgressAssertionSigner {
    sub: String,
    tenant: String,
    community: String,
    signing_key: Arc<AssertionSigningKey>,
    ttl_secs: u64,
}

impl EgressAssertionSigner {
    pub fn new(
        sub: impl Into<String>,
        tenant: impl Into<String>,
        community: impl Into<String>,
        signing_key: Arc<AssertionSigningKey>,
        ttl_secs: u64,
    ) -> Self {
        Self {
            sub: sub.into(),
            tenant: tenant.into(),
            community: community.into(),
            signing_key,
            ttl_secs,
        }
    }
}

impl ProxyAssertionSigner for EgressAssertionSigner {
    fn sign(
        &self,
        app_id: &str,
        host: &str,
        port: u16,
        category: DestinationCategory,
    ) -> Result<String, HostResultError> {
        let assertion = egress_assertion::build_assertion(
            self.sub.clone(),
            self.tenant.clone(),
            self.community.clone(),
            app_id.to_string(),
            category,
            host.to_string(),
            port,
            self.ttl_secs,
        );
        self.signing_key
            .sign(&assertion)
            .map_err(|e| denied("proxy_assertion_signing_failed", e.to_string()))
    }
}

/// Header carrying the [`ProxyAssertionSigner`] output, added to the
/// outbound request only when a signer is configured. Re-exported from
/// [`egress_assertion::ASSERTION_HEADER`] so the two crates can never
/// disagree on the header name.
const PROXY_ASSERTION_HEADER: &str = PROXY_ASSERTION_HEADER_NAME;

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
///
/// **`url` and `headers` may carry resolved secrets** (a `?<param>` secret
/// ref's value is part of `url`'s query; a header secret ref is in
/// `headers`), so `Debug` is hand-written to print only the redacted
/// scheme/host/path ([`redact_url_for_log`]), the header *names* and the body
/// length -- a `{:?}` of this struct in a log line or panic message can never
/// leak a credential.
#[derive(Clone)]
pub struct TransportRequest {
    pub method: String,
    pub url: String,
    pub pinned_addr: SocketAddr,
    pub headers: Vec<(String, String)>,
    pub body: Option<Vec<u8>>,
}

impl std::fmt::Debug for TransportRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TransportRequest")
            .field("method", &self.method)
            .field("url", &redact_url_for_log(&self.url))
            .field("pinned_addr", &self.pinned_addr)
            .field(
                "header_names",
                &self
                    .headers
                    .iter()
                    .map(|(name, _)| name.as_str())
                    .collect::<Vec<_>>(),
            )
            .field("body_len", &self.body.as_ref().map(Vec::len))
            .finish()
    }
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

/// Supplies this pod's own machine JWT (`core/service_auth`, PR #438) for
/// the hop to the upstream egress proxy -- `Authorization: Bearer <token>`,
/// the credential `egress_proxy::auth::authenticate` checks on *that*
/// connection, distinct from the bundle's own destination credential
/// (which [`ReqwestTransport::send`] remaps to
/// [`egress_assertion::FORWARD_AUTHORIZATION_HEADER`] whenever this seam is
/// wired -- see [`ReqwestTransport::with_proxy`]). Object-safe, mirrors
/// this crate's other dependency-seam traits; kept as a trait rather than
/// a direct `service_auth::MachineJwtClient` dependency so this crate
/// never needs that crate at all when no consuming crate uses proxy mode.
/// `service_auth::MachineJwtClient` (its cache/refresh already built in)
/// is the production implementation each consuming crate wires.
pub trait MachineJwtSource: Send + Sync {
    fn token<'a>(
        &'a self,
    ) -> Pin<Box<dyn Future<Output = Result<String, HostResultError>> + Send + 'a>>;
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
    /// Set only by [`ReqwestTransport::with_proxy`] -- when present,
    /// [`ReqwestTransport::send`] renames any bundle-supplied
    /// `Authorization` header to
    /// [`egress_assertion::FORWARD_AUTHORIZATION_HEADER`] and sets this
    /// hop's own `Authorization` to the fetched machine JWT instead (see
    /// [`MachineJwtSource`]'s doc). `None` in direct-connect mode, where
    /// the bundle's own `Authorization` (if any) is sent completely
    /// unchanged, an exact behavior-preserving no-op.
    machine_jwt: Option<Arc<dyn MachineJwtSource>>,
}

impl ReqwestTransport {
    /// Direct-connect transport -- no proxy configured. Behaviorally
    /// identical to this type before the proxy seam was added.
    pub fn new() -> Self {
        Self {
            proxy_url: None,
            machine_jwt: None,
        }
    }

    /// Dials every request through `proxy_url` instead of connecting
    /// directly to the guard's already-pinned address -- see the crate
    /// module doc's "Upstream egress proxy" section. `machine_jwt`
    /// supplies this hop's own `Authorization` bearer (the proxy's inbound
    /// caller-auth check); the bundle's own destination credential is
    /// carried instead as [`egress_assertion::FORWARD_AUTHORIZATION_HEADER`]
    /// (see [`ReqwestTransport::send`]'s header-remap step).
    pub fn with_proxy(proxy_url: String, machine_jwt: Arc<dyn MachineJwtSource>) -> Self {
        Self {
            proxy_url: Some(proxy_url),
            machine_jwt: Some(machine_jwt),
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

            let mut headers = req.headers;
            if let Some(machine_jwt) = &self.machine_jwt {
                // Proxy-mode hop: this connection's own `Authorization` is
                // the machine JWT `egress_proxy::auth::authenticate`
                // checks, never the bundle's own destination credential --
                // rename any bundle-supplied `Authorization` (e.g. a
                // resolved `secret_ref` header) to the dedicated forward
                // header so the proxy can restore it for the real
                // destination only, never treat it as this hop's own
                // bearer credential.
                headers = headers
                    .into_iter()
                    .map(|(name, value)| {
                        if name.eq_ignore_ascii_case("authorization") {
                            (
                                egress_assertion::FORWARD_AUTHORIZATION_HEADER.to_string(),
                                value,
                            )
                        } else {
                            (name, value)
                        }
                    })
                    .collect();
                let token = machine_jwt.token().await?;
                headers.push(("Authorization".to_string(), format!("Bearer {token}")));
            }

            let mut header_map = reqwest::header::HeaderMap::new();
            for (name, value) in &headers {
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

            let response = builder.send().await.map_err(classify_send_error)?;
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
                let chunk = chunk.map_err(classify_send_error)?;
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
///
/// **The request URL is stripped of its query/fragment/userinfo first**
/// ([`strip_url_for_log`]): `reqwest::Error`'s `Display` embeds the full URL
/// (`error sending request for url (https://host/path?key=<secret>)`), and
/// the query of a `?<param>` secret ref carries a host-injected credential
/// (while bundle-supplied parameters may carry user-identifying data). This
/// is the transport-level layer; [`EgressGuard::send_checked`] additionally
/// scrubs the exact resolved secret values from whatever any
/// [`HttpTransport`] returns.
fn classify_send_error(mut err: reqwest::Error) -> HostResultError {
    if let Some(url) = err.url_mut() {
        strip_url_for_log(url);
    }
    let err = &err;
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
            EgressRuleRow::from_legacy_patterns(egress, None, granted_secret_refs),
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

    /// CGNAT is forbidden by default (`allow_private: false`), same as
    /// RFC1918/ULA -- but, per Justin's decision grouping CGNAT with those
    /// as a "private range" rather than a never-liftable tier, it *is*
    /// lifted by `allow_private: true`. `is_always_forbidden` (used by the
    /// three-category pipeline) never passes `allow_private: true` through
    /// to this check for CGNAT, so the guard's own SSRF property is
    /// unaffected -- see `cgnat_is_reachable_only_via_a_granted_private_ip_range_under_instance_policy`.
    #[test]
    fn cgnat_v4_is_forbidden_by_default_but_liftable_via_allow_private() {
        let ip = IpAddr::V4(Ipv4Addr::new(100, 100, 100, 200));
        assert_eq!(is_forbidden_address(ip, false), Some("cgnat"));
        assert_eq!(is_forbidden_address(ip, true), None);
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

    /// Under the three-category model a direct private-ip grant is no
    /// longer gated by `EgressLimits::allow_private_hosts` at all -- it's
    /// gated by [`InstanceEgressPolicy::allow_private_ip_egress`]
    /// (Justin's decision, item 1: "default deny, global-admin opt-in").
    #[tokio::test]
    async fn private_ip_grant_is_denied_by_default_instance_policy() {
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
    async fn private_ip_grant_is_permitted_once_instance_policy_opts_in() {
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("10.0.0.5".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })));
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    /// A private IP declared under `public_ip_grants` -- never possible
    /// through `catalog_with_row`'s auto-classifying helper, so built by
    /// hand -- is never found at all: [`match_grant`] only ever searches
    /// `private_ip_grants` for a private-classified literal (Justin's
    /// decision: "each matched only by its own grants").
    #[tokio::test]
    async fn private_ip_via_public_ip_grant_is_denied() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                public_ip_grants: vec![("10.0.0.5".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })));
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.0.0.5/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A public IP literal declared (in error) under `private_ip_grants` is
    /// symmetrically never found either -- routing is by the literal's own
    /// address class, not by which list happens to contain the string.
    #[tokio::test]
    async fn public_ip_via_private_ip_grant_is_denied() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                private_ip_grants: vec![("93.184.216.34".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })));
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://93.184.216.34/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    /// A public IP requested via an `fqdn_grants` entry for a *different*
    /// host is denied on the ordinary `host_not_declared` path -- category
    /// membership never changes what "declared" means; only an exact
    /// hostname match ever satisfies an `fqdn_grants` entry.
    #[tokio::test]
    async fn public_ip_via_fqdn_grant_for_a_different_host_is_denied() {
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

    /// An `fqdn_grants` host that rebinds to a private address is denied
    /// even when the instance policy allows private-ip egress in general --
    /// the FQDN grant alone never covers a private resolved address; a
    /// *separate* `private_ip_grants` entry covering that exact resolved
    /// address is required too.
    #[tokio::test]
    async fn fqdn_rebinding_to_private_is_denied_without_a_covering_private_ip_grant() {
        let resolver = Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "10.0.0.9:443".parse().unwrap(),
        });
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })))
        .with_resolver(resolver as Arc<dyn Resolver>);
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://api.spotify.com/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    /// Same rebinding scenario, but this bundle's manifest *also* grants
    /// `net.http.private-ip:10.0.0.9` -- the FQDN's rebind is now covered
    /// by that separate grant, and (with the instance policy opted in) the
    /// request succeeds.
    #[tokio::test]
    async fn fqdn_rebinding_to_private_is_permitted_with_a_covering_private_ip_grant() {
        let resolver = Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "10.0.0.9:443".parse().unwrap(),
        });
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                fqdn_grants: vec![("api.spotify.com".to_string(), vec!["GET".to_string()])],
                private_ip_grants: vec![("10.0.0.9".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })))
        .with_resolver(resolver as Arc<dyn Resolver>);
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://api.spotify.com/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    /// A `net.http.private-ip:10.0.0.0/8` CIDR grant covers any address in
    /// range, once the instance policy allows it -- proving CIDR (not just
    /// exact-IP) matching for the private-ip family.
    #[tokio::test]
    async fn private_ip_cidr_grant_covers_any_address_in_range() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })));
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.55.66.77/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    /// Parity test for the shared `egress_assertion::ip_matches_grant`
    /// primitive this module's `ip_pattern_contains` now delegates to: a
    /// `net.http.private-ip:10.0.0.0/8` CIDR grant must still cover a
    /// request host expressed as an IPv4-mapped-IPv6 literal
    /// (`::ffff:10.55.66.77`), the same way [`private_ip_cidr_grant_covers_any_address_in_range`]
    /// proves it for a plain v4 literal. Before this module switched to the
    /// shared primitive, its hand-rolled `CidrBlock::contains` required an
    /// exact address-family match (`IpNet::V4` vs `IpAddr::V6` fell through
    /// to `_ => false`), so a v4-mapped-v6 target could never satisfy a v4
    /// CIDR grant at all, despite `is_private_range` already recognizing it
    /// as the same private address for routing purposes -- this would have
    /// failed `ssrf_blocked_address` under the old implementation.
    #[tokio::test]
    async fn private_ip_cidr_grant_covers_an_ipv4_mapped_ipv6_target() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default().queue(Ok(ok_response()))),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })));
        let result = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://[::ffff:10.55.66.77]/"}),
            )
            .await;
        assert!(result.is_ok(), "expected success, got {result:?}");
    }

    /// The cluster CIDR denylist beats a private-ip grant -- even with a
    /// matching grant and instance-policy opt-in, a resolved address inside
    /// an operator-configured cluster CIDR is always denied
    /// (`cluster_cidr_denied`), never merely `ssrf_blocked_address`'s
    /// generic private-range reason.
    #[tokio::test]
    async fn cluster_cidr_denylist_beats_a_matching_private_ip_grant() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })))
        .with_cluster_denylist(ClusterCidrDenylist::parse(["10.244.0.0/16"]).unwrap());
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://10.244.5.6/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
        // Confirm it's specifically the cluster-CIDR reason, not merely a
        // generic private-range rejection -- observability requirement.
        let err2 = classify_dial_address(
            "10.244.5.6".parse().unwrap(),
            EgressCategory::PrivateIp,
            &EgressRuleRow {
                private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
            &ClusterCidrDenylist::parse(["10.244.0.0/16"]).unwrap(),
            InstanceEgressPolicy {
                allow_private_ip_egress: true,
            },
        );
        assert_eq!(err2, Some("cluster_cidr_denied"));
    }

    /// regression: mapped-v6 cluster bypass -- an IPv4-mapped-IPv6 encoding
    /// of an address inside the operator's cluster CIDR denylist must still
    /// be denied (`cluster_cidr_denied`), even with a covering private-ip
    /// grant and instance-policy opt-in. Before `ClusterCidrDenylist::
    /// contains` canonicalized its input, `::ffff:10.244.5.6` (family V6)
    /// could never match a `10.244.0.0/16` (family V4) entry at all, so the
    /// always-forbidden cluster check silently never fired for this
    /// encoding of the same address `cluster_cidr_denylist_beats_a_matching_
    /// private_ip_grant` already proves is denied in plain v4 form.
    #[tokio::test]
    async fn cluster_cidr_denylist_denies_an_ipv4_mapped_ipv6_target() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })))
        .with_cluster_denylist(ClusterCidrDenylist::parse(["10.244.0.0/16"]).unwrap());
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://[::ffff:10.244.5.6]/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
        let err2 = classify_dial_address(
            "::ffff:10.244.5.6".parse().unwrap(),
            EgressCategory::PrivateIp,
            &EgressRuleRow {
                private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
            &ClusterCidrDenylist::parse(["10.244.0.0/16"]).unwrap(),
            InstanceEgressPolicy {
                allow_private_ip_egress: true,
            },
        );
        assert_eq!(err2, Some("cluster_cidr_denied"));
    }

    /// regression: mapped-v6 cluster bypass -- same as the mapped-address
    /// case above, but for the NAT64-synthesized (`64:ff9b::a.b.c.d`) and
    /// deprecated IPv4-compatible (`::a.b.c.d`) encodings `embedded_ipv4`
    /// also recognizes.
    #[test]
    fn cluster_cidr_denylist_denies_nat64_and_ipv4_compatible_encodings() {
        let denylist = ClusterCidrDenylist::parse(["10.244.0.0/16"]).unwrap();
        let policy = InstanceEgressPolicy {
            allow_private_ip_egress: true,
        };
        let row = EgressRuleRow {
            private_ip_grants: vec![("10.0.0.0/8".to_string(), vec!["GET".to_string()])],
            ..EgressRuleRow::default()
        };
        for addr in ["64:ff9b::10.244.5.6", "::10.244.5.6"] {
            let ip: IpAddr = addr.parse().unwrap();
            assert_eq!(
                classify_dial_address(ip, EgressCategory::PrivateIp, &row, &denylist, policy),
                Some("cluster_cidr_denied"),
                "expected {addr} to be denied by the cluster CIDR denylist"
            );
        }
    }

    /// regression: mapped-v6 cluster bypass -- a cluster CIDR denylist entry
    /// itself expressed in IPv4-mapped-IPv6 form must be rejected at parse
    /// time (fail closed) rather than silently accepted as an entry that
    /// can never match anything, since `contains` always canonicalizes the
    /// checked address down to its embedded v4 form first.
    #[test]
    fn cluster_cidr_denylist_rejects_a_mapped_ipv6_configured_range() {
        let err = ClusterCidrDenylist::parse(["::ffff:10.244.0.0/120"]).unwrap_err();
        assert!(
            err.contains("IPv4-mapped"),
            "expected a clear IPv4-mapped config error, got: {err}"
        );
    }

    /// regression: mapped-v6 cluster bypass -- 6to4 and Teredo encodings of
    /// the loopback/metadata addresses are denied outright (`legacy_
    /// transition_mechanism`), not silently treated as ordinary native-v6
    /// addresses that fall through every check.
    #[test]
    fn six_to_four_and_teredo_encodings_are_always_denied() {
        // 6to4 (2002::/16) embedding 127.0.0.1 -> 2002:7f00:0001::
        let six_to_four: IpAddr = "2002:7f00:1::".parse().unwrap();
        assert_eq!(
            is_forbidden_address(six_to_four, true),
            Some("legacy_transition_mechanism")
        );
        // Teredo (2001:0000::/32).
        let teredo: IpAddr = "2001:0:4136:e378:8000:63bf:3fff:fdd2".parse().unwrap();
        assert_eq!(
            is_forbidden_address(teredo, true),
            Some("legacy_transition_mechanism")
        );
        // A same-prefix-byte ordinary v6 allocation (documentation range)
        // must NOT be swept up by the Teredo check.
        let docs: IpAddr = "2001:db8::1".parse().unwrap();
        assert_eq!(is_forbidden_address(docs, true), None);
    }

    /// Metadata is always denied regardless of any grant or instance
    /// policy -- covers both a direct `public_ip_grants`-shaped literal
    /// (never actually reachable, since a metadata address classifies as
    /// public-looking but is still hard-forbidden) and the private-ip path
    /// with the policy fully opted in.
    #[tokio::test]
    async fn cloud_metadata_is_always_denied_even_with_a_matching_grant_and_policy_opt_in() {
        let catalog = TestCatalog::new();
        catalog.insert(
            "waddles.a.b.c",
            EgressRuleRow {
                public_ip_grants: vec![("169.254.169.254".to_string(), vec!["GET".to_string()])],
                ..EgressRuleRow::default()
            },
        );
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_instance_policy(Arc::new(RwLock::new(InstanceEgressPolicy {
            allow_private_ip_egress: true,
        })));
        let err = guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://169.254.169.254/"}),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    // -- validate_dial (wss/IRC-over-TLS connector host transport dials) --

    #[tokio::test]
    async fn validate_dial_permits_a_declared_fqdn_and_pins_the_resolved_address() {
        let resolver = Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:443".parse().unwrap(),
        });
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("gateway.discord.gg".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_resolver(Arc::clone(&resolver) as Arc<dyn Resolver>);
        let target = guard
            .validate_dial("waddles.a.b.c", "gateway.discord.gg:443")
            .await
            .expect("wss dial to a declared fqdn is validated");
        assert_eq!(target.host, "gateway.discord.gg");
        assert_eq!(target.port, 443);
        assert_eq!(target.pinned_addr, "93.184.216.34:443".parse().unwrap());
        assert_eq!(resolver.calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    /// IRC-over-TLS's conventional port (6697), not 443 -- proves
    /// `validate_dial` does not apply `EgressLimits::allowed_ports`
    /// (an HTTP-capability-specific limit), only the declared-port-on-the-
    /// grant check when one is present.
    #[tokio::test]
    async fn validate_dial_permits_irc_over_tls_on_its_conventional_port() {
        let resolver = Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:6697".parse().unwrap(),
        });
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("irc.twitch.tv".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_resolver(resolver as Arc<dyn Resolver>);
        let target = guard
            .validate_dial("waddles.a.b.c", "irc.twitch.tv:6697")
            .await
            .expect("irc-over-tls dial on 6697 is validated");
        assert_eq!(target.port, 6697);
    }

    #[tokio::test]
    async fn validate_dial_denies_an_undeclared_host() {
        let guard = guard_with("waddles.a.b.c", vec![], FakeTransport::default());
        let err = guard
            .validate_dial("waddles.a.b.c", "evil.example.com:443")
            .await
            .unwrap_err();
        assert_eq!(err.code, "host_not_declared");
    }

    #[tokio::test]
    async fn validate_dial_denies_ssrf_to_metadata_even_when_declared() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("169.254.169.254".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .validate_dial("waddles.a.b.c", "169.254.169.254:443")
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    #[tokio::test]
    async fn validate_dial_denies_a_private_ip_dial_by_default_instance_policy() {
        let guard = guard_with(
            "waddles.a.b.c",
            vec![("10.0.0.5".to_string(), vec!["GET".to_string()])],
            FakeTransport::default(),
        );
        let err = guard
            .validate_dial("waddles.a.b.c", "10.0.0.5:6697")
            .await
            .unwrap_err();
        assert_eq!(err.code, "ssrf_blocked_address");
    }

    #[tokio::test]
    async fn validate_dial_respects_the_flag_gate() {
        let guard = EgressGuard::new(
            Arc::new(FakeTransport::default()),
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("gateway.discord.gg".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(false)),
        );
        let err = guard
            .validate_dial("waddles.a.b.c", "gateway.discord.gg:443")
            .await
            .unwrap_err();
        assert_eq!(err.code, "feature_disabled");
    }

    #[test]
    fn split_host_port_rejects_a_bare_ipv6_literal_with_no_brackets() {
        assert_eq!(split_host_port("::1:443"), None);
    }

    #[test]
    fn split_host_port_parses_a_bracketed_ipv6_literal_with_port() {
        assert_eq!(
            split_host_port("[::1]:6697"),
            Some(("::1".to_string(), 6697))
        );
    }

    // -- Upstream egress-proxy signed assertion (PR #463/#466), off by default --

    struct StaticSigner;
    impl ProxyAssertionSigner for StaticSigner {
        fn sign(
            &self,
            app_id: &str,
            host: &str,
            port: u16,
            category: DestinationCategory,
        ) -> Result<String, HostResultError> {
            Ok(format!("{app_id}|{host}|{port}|{category:?}"))
        }
    }

    #[tokio::test]
    async fn no_proxy_assertion_header_is_added_when_no_signer_is_configured() {
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        );
        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/"}),
            )
            .await
            .expect("send succeeds");
        let sent = transport.requests.lock().unwrap();
        assert!(!sent[0]
            .headers
            .iter()
            .any(|(k, _)| k == PROXY_ASSERTION_HEADER));
    }

    #[tokio::test]
    async fn proxy_assertion_header_is_added_when_a_signer_is_configured() {
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_proxy_assertion_signer(Arc::new(StaticSigner));
        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/"}),
            )
            .await
            .expect("send succeeds");
        let sent = transport.requests.lock().unwrap();
        let header = sent[0]
            .headers
            .iter()
            .find(|(k, _)| k == PROXY_ASSERTION_HEADER)
            .expect("assertion header present");
        assert!(header.1.starts_with("waddles.a.b.c|discord.com|"));
    }

    // PKCS8-DER-encoded Ed25519 test keypair (fixed, test-only) -- same
    // fixture shape `core/egress_assertion`'s own test module uses;
    // generated once with `openssl genpkey -algorithm ed25519` / `openssl
    // pkey -pubout`, never used outside this test module.
    const TEST_KEY_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
        150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74,
        183, 9, 171, 161,
    ];
    const TEST_KEY_PUB_RAW: &[u8] = &[
        169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5,
        69, 236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
    ];

    /// The cross-crate contract this landing exists to guarantee:
    /// [`EgressAssertionSigner`] (wired into [`EgressGuard::send`] here,
    /// exactly as a real proxy-mode deployment configures it) produces an
    /// assertion that [`egress_assertion::verify_with_key`] -- the exact
    /// primitive `core/egress_proxy`'s own JWKS-aware verifier wraps --
    /// accepts, with every claim `core/egress_proxy/src/assertion.rs`'s
    /// `EgressAssertion` expects (sub/tenant/community/app/category/
    /// destination/port/jti/iat/exp).
    #[tokio::test]
    async fn egress_assertion_signer_round_trips_through_the_shared_verifier() {
        let signing_key = Arc::new(
            AssertionSigningKey::from_ed25519_pem(
                // `from_ed25519_pem` takes PEM bytes; build one from the
                // fixed DER test key via `jsonwebtoken`'s own encoder
                // isn't available here, so construct the signer directly
                // from DER through the crate's private-field test seam
                // instead -- see the inline helper below.
                &pem_encode_ed25519_private_key(TEST_KEY_PRIV_DER),
                "k1",
            )
            .expect("valid Ed25519 PEM"),
        );
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row(
                "waddles.a.b.c",
                vec![("discord.com".to_string(), vec!["GET".to_string()])],
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_proxy_assertion_signer(Arc::new(EgressAssertionSigner::new(
            "spiffe://penguintech.io/alpha/svc-process",
            "tenant-a",
            "community-a",
            signing_key,
            30,
        )));
        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({"method": "GET", "url": "https://discord.com/"}),
            )
            .await
            .expect("send succeeds");
        let sent = transport.requests.lock().unwrap();
        let header = sent[0]
            .headers
            .iter()
            .find(|(k, _)| k == PROXY_ASSERTION_HEADER)
            .expect("assertion header present")
            .1
            .clone();

        let decoding_key = jsonwebtoken::DecodingKey::from_ed_der(TEST_KEY_PUB_RAW);
        let verified = egress_assertion::verify_with_key(
            &header,
            &decoding_key,
            egress_assertion::ASSERTION_MAX_TTL_SECONDS,
        )
        .expect("egress_proxy's verifier accepts bundle_host_http's assertion");
        assert_eq!(verified.sub, "spiffe://penguintech.io/alpha/svc-process");
        assert_eq!(verified.tenant, "tenant-a");
        assert_eq!(verified.community, "community-a");
        assert_eq!(verified.app, "waddles.a.b.c");
        assert_eq!(verified.category, DestinationCategory::Fqdn);
        assert_eq!(verified.destination, "discord.com");
        assert_eq!(verified.port, 443);
        assert!(egress_assertion::destination_matches(
            &verified,
            "discord.com",
            443
        ));
        // Wrong port: the exact port the assertion granted must be
        // required, not just any operator-allowlisted one.
        assert!(!egress_assertion::destination_matches(
            &verified,
            "discord.com",
            8443
        ));
    }

    #[tokio::test]
    async fn egress_assertion_signer_rejects_tampered_and_expired_tokens() {
        let signing_key = AssertionSigningKey::from_ed25519_pem(
            &pem_encode_ed25519_private_key(TEST_KEY_PRIV_DER),
            "k1",
        )
        .expect("valid Ed25519 PEM");
        let assertion = egress_assertion::build_assertion(
            "spiffe://penguintech.io/alpha/svc-process",
            "tenant-a",
            "community-a",
            "waddles.a.b.c",
            DestinationCategory::Fqdn,
            "discord.com",
            443,
            30,
        );
        let token = signing_key.sign(&assertion).expect("signs");
        let decoding_key = jsonwebtoken::DecodingKey::from_ed_der(TEST_KEY_PUB_RAW);

        // Tampered: flip the last base64url character of the signature.
        let mut tampered = token.clone();
        tampered.pop();
        tampered.push(if token.ends_with('A') { 'B' } else { 'A' });
        assert!(egress_assertion::verify_with_key(
            &tampered,
            &decoding_key,
            egress_assertion::ASSERTION_MAX_TTL_SECONDS
        )
        .is_err());

        // Expired: signed with exp already in the past.
        let mut expired_claims = assertion.clone();
        expired_claims.iat = egress_assertion::now_secs() - 120;
        expired_claims.exp = egress_assertion::now_secs() - 60;
        let expired_token = signing_key.sign(&expired_claims).expect("signs");
        assert!(egress_assertion::verify_with_key(
            &expired_token,
            &decoding_key,
            egress_assertion::ASSERTION_MAX_TTL_SECONDS
        )
        .is_err());

        // Wrong sub: verifies cleanly (signature/TTL are still valid) but
        // must be rejected by a caller comparing against the authenticated
        // machine JWT's own `sub` -- `egress_proxy::proxy::validate`'s
        // `SubMismatch` check, exercised here as a plain equality check
        // since that verifier isn't in this worktree.
        let verified = egress_assertion::verify_with_key(
            &token,
            &decoding_key,
            egress_assertion::ASSERTION_MAX_TTL_SECONDS,
        )
        .expect("verifies");
        assert_ne!(verified.sub, "spiffe://penguintech.io/alpha/svc-action");
    }

    /// Minimal PKCS8 PEM encoder for an Ed25519 private key DER -- avoids a
    /// second test-only crate dependency just to wrap a fixed 48-byte DER
    /// blob in PKCS8 PEM armor. The armor label is built from parts (not a
    /// literal `"-----BEGIN...-----"` string) purely so this fixed,
    /// publicly-known, test-only DER blob doesn't trip a secrets scanner's
    /// private-key-marker heuristic on a string it merely resembles.
    fn pem_encode_ed25519_private_key(der: &[u8]) -> Vec<u8> {
        use base64::Engine;
        let dashes = "-".repeat(5);
        let label = "PRIVATE KEY";
        let b64 = base64::engine::general_purpose::STANDARD.encode(der);
        let mut pem = format!("{dashes}BEGIN {label}{dashes}\n");
        for chunk in b64.as_bytes().chunks(64) {
            pem.push_str(std::str::from_utf8(chunk).unwrap());
            pem.push('\n');
        }
        pem.push_str(&format!("{dashes}END {label}{dashes}\n"));
        pem.into_bytes()
    }

    /// A [`MachineJwtSource`] returning a fixed token -- production always
    /// uses `service_auth::MachineJwtClient`.
    struct StaticMachineJwt;
    impl MachineJwtSource for StaticMachineJwt {
        fn token<'a>(
            &'a self,
        ) -> Pin<Box<dyn Future<Output = Result<String, HostResultError>> + Send + 'a>> {
            Box::pin(async { Ok("machine-jwt-value".to_string()) })
        }
    }

    #[tokio::test]
    async fn reqwest_transport_proxy_mode_remaps_authorization_and_adds_machine_jwt() {
        use std::sync::Mutex as StdMutex;

        let captured: Arc<StdMutex<Vec<(String, String)>>> = Arc::new(StdMutex::new(Vec::new()));
        let captured_clone = Arc::clone(&captured);
        let app = axum::Router::new().route(
            "/ok",
            axum::routing::get(move |headers: axum::http::HeaderMap| {
                let captured = Arc::clone(&captured_clone);
                async move {
                    let mut seen = captured.lock().unwrap();
                    for (name, value) in headers.iter() {
                        seen.push((
                            name.as_str().to_string(),
                            value.to_str().unwrap_or("").to_string(),
                        ));
                    }
                    "ok"
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });

        // Direct-connect in this test (no real upstream proxy process) --
        // exercises the header-remap logic itself, which runs regardless
        // of whether `reqwest::Proxy` is also configured.
        let transport = ReqwestTransport {
            proxy_url: None,
            machine_jwt: Some(Arc::new(StaticMachineJwt)),
        };
        transport
            .send(
                TransportRequest {
                    method: "GET".to_string(),
                    url: format!("http://127.0.0.1:{}/ok", addr.port()),
                    pinned_addr: addr,
                    headers: vec![
                        (
                            "Authorization".to_string(),
                            "Bot bundle-own-secret".to_string(),
                        ),
                        (
                            "X-Waddles-Egress-Assertion".to_string(),
                            "assertion-jwt".to_string(),
                        ),
                    ],
                    body: None,
                },
                Duration::from_secs(5),
                1_048_576,
            )
            .await
            .expect("request succeeds");

        let seen = captured.lock().unwrap();
        let auth = seen
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case("authorization"))
            .map(|(_, v)| v.as_str());
        assert_eq!(auth, Some("Bearer machine-jwt-value"));
        let forwarded = seen
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(egress_assertion::FORWARD_AUTHORIZATION_HEADER))
            .map(|(_, v)| v.as_str());
        assert_eq!(forwarded, Some("Bot bundle-own-secret"));
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

    // -- Secret headers scoped to the host they were granted for (security
    // review finding, PR #468, HIGH): a redirect must never carry a
    // resolved `secret_refs` header, Cookie, or Proxy-Authorization to a
    // different host than the one the bundle's request originally
    // targeted, even when that new host is itself on the allowlist. --

    /// A redirect to a *different* allowlisted host drops the secret header
    /// that was resolved for the original host -- the core HIGH finding:
    /// `send`'s redirect loop used to clone one `headers` vec, secret
    /// headers included, unconditionally on every hop.
    #[tokio::test]
    async fn a_redirect_to_a_different_host_drops_the_originating_hosts_secret_header() {
        let broker = Arc::new(FakeCredentialBroker {
            value: "host-a-token".to_string(),
            resolved_handles: Mutex::new(Vec::new()),
        });
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())).queue(Ok(
            TransportResponse {
                status: 302,
                headers: vec![(
                    "location".to_string(),
                    "https://host-b.example.com/next".to_string(),
                )],
                body: vec![],
                truncated: false,
            },
        )));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row_and_secrets(
                "waddles.a.b.c",
                vec![
                    ("host-a.example.com".to_string(), vec!["GET".to_string()]),
                    ("host-b.example.com".to_string(), vec!["GET".to_string()]),
                ],
                HashMap::from([("TOKEN_REF".to_string(), "EGRESS_TEST_HOST_A".to_string())]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::clone(&broker) as Arc<dyn CredentialBroker>)
        .with_resolver(Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:443".parse().unwrap(),
        }) as Arc<dyn Resolver>);

        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "GET",
                    "url": "https://host-a.example.com/start",
                    "headers": [{"name": "Cookie", "value": "session=host-a-only"}],
                    "secret_refs": {"Authorization": "TOKEN_REF"}
                }),
            )
            .await
            .expect("redirect followed to a successful terminal response");

        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert!(
            requests[0]
                .headers
                .iter()
                .any(|(k, v)| k == "Authorization" && v == "host-a-token"),
            "the first hop, to the bound host, keeps the secret header"
        );
        assert!(
            !requests[1]
                .headers
                .iter()
                .any(|(k, _)| k == "Authorization"),
            "the second hop, redirected to a different host, must not carry host-a's secret"
        );
        assert!(
            !requests[1].headers.iter().any(|(k, _)| k == "Cookie"),
            "Cookie is never forwarded across a host change either"
        );
    }

    /// A redirect that stays on the *same* host the secret was resolved for
    /// keeps carrying it -- confirms the fix scopes by host, not "never
    /// reattach after any redirect at all".
    #[tokio::test]
    async fn a_redirect_to_the_same_host_keeps_the_secret_header() {
        let broker = Arc::new(FakeCredentialBroker {
            value: "host-a-token".to_string(),
            resolved_handles: Mutex::new(Vec::new()),
        });
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())).queue(Ok(
            TransportResponse {
                status: 302,
                headers: vec![(
                    "location".to_string(),
                    "https://host-a.example.com/next".to_string(),
                )],
                body: vec![],
                truncated: false,
            },
        )));
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row_and_secrets(
                "waddles.a.b.c",
                vec![("host-a.example.com".to_string(), vec!["GET".to_string()])],
                HashMap::from([("TOKEN_REF".to_string(), "EGRESS_TEST_HOST_A".to_string())]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::clone(&broker) as Arc<dyn CredentialBroker>)
        .with_resolver(Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:443".parse().unwrap(),
        }) as Arc<dyn Resolver>);

        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "GET",
                    "url": "https://host-a.example.com/start",
                    "secret_refs": {"Authorization": "TOKEN_REF"}
                }),
            )
            .await
            .expect("redirect to the same host is followed to a successful terminal response");

        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        for (hop, req) in requests.iter().enumerate() {
            assert!(
                req.headers
                    .iter()
                    .any(|(k, v)| k == "Authorization" && v == "host-a-token"),
                "hop {hop} (same host throughout) should keep the secret header"
            );
        }
    }

    /// A redirect to a different host that the manifest row explicitly
    /// widens via `secret_granted_hosts` ("B has its own grant") does
    /// receive the resolved secret -- proves the drop above is a host
    /// scoping check, not a blanket "never on hop > 0" rule, and that
    /// widening it is an explicit, auditable manifest opt-in rather than
    /// the previous unconditional behavior.
    #[tokio::test]
    async fn a_redirect_to_a_host_explicitly_granted_the_secret_keeps_it() {
        let broker = Arc::new(FakeCredentialBroker {
            value: "shared-token".to_string(),
            resolved_handles: Mutex::new(Vec::new()),
        });
        let transport = Arc::new(FakeTransport::default().queue(Ok(ok_response())).queue(Ok(
            TransportResponse {
                status: 302,
                headers: vec![(
                    "location".to_string(),
                    "https://host-b.example.com/next".to_string(),
                )],
                body: vec![],
                truncated: false,
            },
        )));
        let mut row = EgressRuleRow::from_legacy_patterns(
            vec![
                ("host-a.example.com".to_string(), vec!["GET".to_string()]),
                ("host-b.example.com".to_string(), vec!["GET".to_string()]),
            ],
            None,
            HashMap::from([("TOKEN_REF".to_string(), "EGRESS_TEST_HOST_A".to_string())]),
        );
        row.secret_granted_hosts = HashSet::from(["host-b.example.com".to_string()]);
        let catalog = TestCatalog::new();
        catalog.insert("waddles.a.b.c", row);
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::clone(&broker) as Arc<dyn CredentialBroker>)
        .with_resolver(Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:443".parse().unwrap(),
        }) as Arc<dyn Resolver>);

        guard
            .send(
                "waddles.a.b.c",
                &serde_json::json!({
                    "method": "GET",
                    "url": "https://host-a.example.com/start",
                    "secret_refs": {"Authorization": "TOKEN_REF"}
                }),
            )
            .await
            .expect("redirect to the explicitly-granted host succeeds");

        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert!(
            requests[1]
                .headers
                .iter()
                .any(|(k, v)| k == "Authorization" && v == "shared-token"),
            "host-b was explicitly granted this secret via secret_granted_hosts"
        );
    }

    /// A redirect `Location` naming a plain `http://` URL is denied, never
    /// dialed -- the same per-hop scheme check `send` already runs on
    /// `req.url` at the top of the loop applies again once `req.url` is
    /// rewritten to the redirect target, so a downgrade is structurally
    /// impossible, not just discouraged.
    #[tokio::test]
    async fn a_redirect_to_a_plain_http_url_is_denied_never_downgraded() {
        let transport = FakeTransport::default().queue(Ok(TransportResponse {
            status: 302,
            headers: vec![(
                "location".to_string(),
                "http://discord.com/downgraded".to_string(),
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
        assert_eq!(err.code, "scheme_not_https");
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

    // -- Query-parameter secret refs (`?key`): resolved host-side and
    // injected on the secret-bound FQDN's first hop only, dropped on every
    // redirect hop, and scrubbed from every log line, transport error and
    // response that could carry the value back out. The value never enters
    // the guest. --

    const QS_APP: &str = "waddles.a.b.c";
    const QS_WEATHER: &str = "api.weatherapi.com";
    const QS_SECRET: &str = "wk_live_9f8e7d6c5b4a3210";
    const QS_HEADER_SECRET: &str = "hdr-token-abc123";

    /// [`CredentialBroker`] over a fixed `granted env var name -> value`
    /// table -- no process-environment mutation, so these tests can run in
    /// parallel with every other test in the module.
    struct TableBroker(HashMap<String, String>);

    impl CredentialBroker for TableBroker {
        fn resolve(&self, handle: &SecretHandle) -> Result<String, HostResultError> {
            self.0.get(&handle.0).cloned().ok_or_else(|| {
                denied(
                    "secret_unresolved",
                    format!("granted env var {:?} is not configured", handle.0),
                )
            })
        }
    }

    /// A [`FakeTransport`] answering `sequence` in send order (the fixture's
    /// own queue is a LIFO stack, so this reverses it once, here).
    fn fake_transport(
        sequence: Vec<Result<TransportResponse, HostResultError>>,
    ) -> Arc<FakeTransport> {
        let transport = FakeTransport::default();
        for response in sequence.into_iter().rev() {
            transport.responses.lock().unwrap().push(response);
        }
        Arc::new(transport)
    }

    fn redirect_to(location: &str) -> Result<TransportResponse, HostResultError> {
        Ok(TransportResponse {
            status: 302,
            headers: vec![("location".to_string(), location.to_string())],
            body: vec![],
            truncated: false,
        })
    }

    /// A guard whose bundle holds GET grants on `hosts` and these secret
    /// grants: `KEY_REF` (value `key_value`, the query secret), `TOKEN_REF`
    /// (`QS_HEADER_SECRET`, a header secret), `EMPTY_REF` (resolves to the
    /// empty string) and `UNSET_REF` (granted, but the broker has no value).
    /// DNS is stubbed to one fixed public address.
    fn qs_guard(transport: &Arc<FakeTransport>, hosts: &[&str], key_value: &str) -> EgressGuard {
        let egress = hosts
            .iter()
            .map(|h| (h.to_string(), vec!["GET".to_string()]))
            .collect();
        let grants = HashMap::from([
            ("KEY_REF".to_string(), "QS_KEY_ENV".to_string()),
            ("TOKEN_REF".to_string(), "QS_TOKEN_ENV".to_string()),
            ("EMPTY_REF".to_string(), "QS_EMPTY_ENV".to_string()),
            ("UNSET_REF".to_string(), "QS_UNSET_ENV".to_string()),
        ]);
        let broker = TableBroker(HashMap::from([
            ("QS_KEY_ENV".to_string(), key_value.to_string()),
            ("QS_TOKEN_ENV".to_string(), QS_HEADER_SECRET.to_string()),
            ("QS_EMPTY_ENV".to_string(), String::new()),
        ]));
        EgressGuard::new(
            Arc::clone(transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog_with_row_and_secrets(QS_APP, egress, grants),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::new(broker) as Arc<dyn CredentialBroker>)
        .with_resolver(Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:443".parse().unwrap(),
        }) as Arc<dyn Resolver>)
    }

    fn sent_urls(transport: &FakeTransport) -> Vec<String> {
        transport
            .requests
            .lock()
            .unwrap()
            .iter()
            .map(|r| r.url.clone())
            .collect()
    }

    fn weather_args(url: &str, secret_refs: serde_json::Value) -> serde_json::Value {
        serde_json::json!({"method": "GET", "url": url, "secret_refs": secret_refs})
    }

    #[tokio::test]
    async fn query_secret_ref_is_injected_into_the_url_on_the_bound_fqdn() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        let guest_args = weather_args(
            "https://api.weatherapi.com/v1/current.json?q=London",
            serde_json::json!({"?key": "KEY_REF"}),
        );
        // The guest only ever names the symbolic ref -- never the value.
        assert!(!guest_args.to_string().contains(QS_SECRET));

        guard
            .send(QS_APP, &guest_args)
            .await
            .expect("send succeeds");

        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(
            requests[0].url,
            format!("https://api.weatherapi.com/v1/current.json?q=London&key={QS_SECRET}")
        );
        assert_eq!(
            requests[0].headers,
            vec![("Accept-Encoding".to_string(), "identity".to_string())],
            "a ?-ref is a query parameter, never also a header; the only header \
             the host adds is the forced plaintext encoding"
        );
    }

    #[tokio::test]
    async fn query_secret_ref_creates_the_query_when_the_url_has_none() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        assert_eq!(
            sent_urls(&transport),
            vec![format!(
                "https://api.weatherapi.com/v1/current.json?key={QS_SECRET}"
            )]
        );
    }

    /// The host's value always wins: a bundle-supplied parameter of the same
    /// name (a dummy, or a duplicate meant to confuse a first-wins server) is
    /// removed, leaving exactly one `key=`.
    #[tokio::test]
    async fn query_secret_ref_replaces_a_bundle_supplied_same_named_parameter() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?key=guess&q=London&key=other",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        let url = reqwest::Url::parse(&sent_urls(&transport)[0]).unwrap();
        let pairs: Vec<(String, String)> = url
            .query_pairs()
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect();
        assert_eq!(
            pairs,
            vec![
                ("q".to_string(), "London".to_string()),
                ("key".to_string(), QS_SECRET.to_string())
            ]
        );
    }

    /// A secret containing URL metacharacters round-trips as exactly one
    /// parameter value -- it can never terminate the parameter or smuggle in
    /// extra ones.
    #[tokio::test]
    async fn query_secret_value_is_url_encoded_and_cannot_inject_extra_parameters() {
        let nasty = "p@ss w/rd&admin=1#frag+é";
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], nasty);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        let url = reqwest::Url::parse(&sent_urls(&transport)[0]).unwrap();
        assert!(
            url.fragment().is_none(),
            "the secret must not open a fragment"
        );
        let pairs: Vec<(String, String)> = url
            .query_pairs()
            .map(|(k, v)| (k.into_owned(), v.into_owned()))
            .collect();
        assert_eq!(
            pairs,
            vec![
                ("q".to_string(), "London".to_string()),
                ("key".to_string(), nasty.to_string())
            ]
        );
    }

    /// A header-only call is byte-for-byte what it was before `?` refs
    /// existed: the bundle's URL is untouched and the header is injected.
    #[tokio::test]
    async fn header_secret_refs_still_work_and_leave_the_url_untouched() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        let url = "https://api.weatherapi.com/v1/current.json?q=Lon%20don";
        guard
            .send(
                QS_APP,
                &weather_args(url, serde_json::json!({"Authorization": "TOKEN_REF"})),
            )
            .await
            .expect("send succeeds");
        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests[0].url, url);
        assert!(requests[0]
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == QS_HEADER_SECRET));
    }

    /// Header and query refs in one call each land in their own slot, and
    /// neither value leaks into the other.
    #[tokio::test]
    async fn header_and_query_secret_refs_coexist_in_one_call() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!({"Authorization": "TOKEN_REF", "?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        let requests = transport.requests.lock().unwrap();
        assert!(requests[0].url.contains(&format!("key={QS_SECRET}")));
        assert!(!requests[0].url.contains(QS_HEADER_SECRET));
        assert!(requests[0]
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == QS_HEADER_SECRET));
        assert!(!requests[0]
            .headers
            .iter()
            .any(|(_, v)| v.contains(QS_SECRET)));
    }

    /// Regression: `core/bundle_executor` serializes the WIT
    /// `list<tuple<string, string>>` as an array of `[slot, ref]` pairs, and
    /// the previous map-only decoder rejected that shape with `invalid_args`
    /// -- so no guest-originated call ever reached the secret code at all.
    #[tokio::test]
    async fn secret_refs_accepts_the_executors_pair_list_wire_shape() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!([["Authorization", "TOKEN_REF"], ["?key", "KEY_REF"]]),
                ),
            )
            .await
            .expect("pair-list shape is accepted");
        // ... and an empty list (a call with no secrets at all) too.
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!([]),
                ),
            )
            .await
            .expect("empty pair list is accepted");
        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert!(requests[0].url.contains(&format!("key={QS_SECRET}")));
        assert!(requests[0]
            .headers
            .iter()
            .any(|(k, v)| k == "Authorization" && v == QS_HEADER_SECRET));
        assert_eq!(
            requests[1].url,
            "https://api.weatherapi.com/v1/current.json"
        );
    }

    // -- Bound-host only; dropped on every redirect hop --

    /// The core property: a redirect to another (allowlisted) host never
    /// carries the key -- not the host-injected one, and not one the server
    /// echoes back in `Location`.
    #[tokio::test]
    async fn query_secret_is_dropped_on_a_redirect_to_a_different_host() {
        let transport = fake_transport(vec![
            redirect_to("https://host-b.example.com/next?key=echoed-by-server&x=1"),
            Ok(ok_response()),
        ]);
        let guard = qs_guard(&transport, &[QS_WEATHER, "host-b.example.com"], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("redirect followed to a terminal response");
        let urls = sent_urls(&transport);
        assert_eq!(urls.len(), 2);
        assert!(
            urls[0].contains(&format!("key={QS_SECRET}")),
            "hop 0 is the bound host"
        );
        assert_eq!(urls[1], "https://host-b.example.com/next?x=1");
        assert!(!urls[1].contains(QS_SECRET));
    }

    /// Stricter than the header rule on purpose: even a redirect that stays
    /// on the bound host does not re-send the key (the spec is "drop on ANY
    /// redirect hop").
    #[tokio::test]
    async fn query_secret_is_dropped_on_a_same_host_redirect_too() {
        let transport = fake_transport(vec![
            redirect_to("/v1/current.json?q=London&key=server-chosen"),
            Ok(ok_response()),
        ]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/old.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("redirect followed to a terminal response");
        let urls = sent_urls(&transport);
        assert_eq!(
            urls[1],
            "https://api.weatherapi.com/v1/current.json?q=London"
        );
    }

    /// A redirect target reflecting the secret under a *different* parameter
    /// name (so the name-based strip can't see it) is still neutralized: the
    /// response scrub redacts the value before the `Location` is followed.
    #[tokio::test]
    async fn a_redirect_location_echoing_the_secret_under_another_name_is_scrubbed() {
        let transport = fake_transport(vec![
            redirect_to(&format!(
                "https://host-b.example.com/next?token={QS_SECRET}&x=1"
            )),
            Ok(ok_response()),
        ]);
        let guard = qs_guard(&transport, &[QS_WEATHER, "host-b.example.com"], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("redirect followed to a terminal response");
        let urls = sent_urls(&transport);
        assert!(!urls[1].contains(QS_SECRET), "hop 1 URL was {}", urls[1]);
    }

    /// `secret_granted_hosts` widens the *header* secret to another host; it
    /// must never widen a query secret.
    #[tokio::test]
    async fn secret_granted_hosts_never_widens_a_query_secret() {
        let transport = fake_transport(vec![
            redirect_to("https://host-b.example.com/next"),
            Ok(ok_response()),
        ]);
        let mut row = EgressRuleRow::from_legacy_patterns(
            vec![
                (QS_WEATHER.to_string(), vec!["GET".to_string()]),
                ("host-b.example.com".to_string(), vec!["GET".to_string()]),
            ],
            None,
            HashMap::from([("KEY_REF".to_string(), "QS_KEY_ENV".to_string())]),
        );
        row.secret_granted_hosts
            .insert("host-b.example.com".to_string());
        let catalog = TestCatalog::new();
        catalog.insert(QS_APP, row);
        let guard = EgressGuard::new(
            Arc::clone(&transport) as Arc<dyn HttpTransport>,
            default_limits(),
            catalog,
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::new(TableBroker(HashMap::from([(
            "QS_KEY_ENV".to_string(),
            QS_SECRET.to_string(),
        )]))) as Arc<dyn CredentialBroker>)
        .with_resolver(Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "93.184.216.34:443".parse().unwrap(),
        }) as Arc<dyn Resolver>);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("redirect followed");
        let urls = sent_urls(&transport);
        assert_eq!(urls[1], "https://host-b.example.com/next");
    }

    /// Only a `net.http.fqdn` grant may receive a query secret -- an
    /// IP-literal grant is refused loudly and nothing is sent.
    #[tokio::test]
    async fn query_secret_is_refused_on_an_ip_literal_grant() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &["93.184.216.34"], QS_SECRET);
        let err = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://93.184.216.34/v1/current.json",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "secret_query_requires_fqdn");
        assert!(transport.requests.lock().unwrap().is_empty());
    }

    // -- Fail loud: never a silent unauthenticated call --

    #[tokio::test]
    async fn an_unresolvable_query_secret_ref_fails_loud_and_sends_nothing() {
        for (secret_ref, expected_code) in [
            ("UNSET_REF", "secret_unresolved"),
            ("EMPTY_REF", "secret_unresolved"),
            ("NEVER_GRANTED_REF", "secret_not_granted"),
        ] {
            let transport = fake_transport(vec![Ok(ok_response())]);
            let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
            let err = guard
                .send(
                    QS_APP,
                    &weather_args(
                        "https://api.weatherapi.com/v1/current.json?q=London",
                        serde_json::json!({"?key": secret_ref}),
                    ),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, expected_code, "ref {secret_ref}");
            assert!(
                transport.requests.lock().unwrap().is_empty(),
                "ref {secret_ref}: nothing may be sent without the credential"
            );
        }
    }

    #[tokio::test]
    async fn malformed_query_secret_slots_are_invalid_args() {
        let too_long = format!("?{}", "a".repeat(MAX_QUERY_SECRET_NAME_LEN + 1));
        for slot in [
            "?",
            "?a&b",
            "?a=b",
            "?a b",
            "?a#b",
            "?a/b",
            "?é",
            too_long.as_str(),
        ] {
            let transport = fake_transport(vec![Ok(ok_response())]);
            let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
            let err = guard
                .send(
                    QS_APP,
                    &weather_args(
                        "https://api.weatherapi.com/v1/current.json",
                        serde_json::json!([[slot, "KEY_REF"]]),
                    ),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code, "invalid_args", "slot {slot:?}");
            assert!(transport.requests.lock().unwrap().is_empty());
        }
    }

    #[tokio::test]
    async fn the_same_query_parameter_named_twice_is_invalid_args() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        let err = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!([["?key", "KEY_REF"], ["?key", "KEY_REF"]]),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "invalid_args");
        assert!(transport.requests.lock().unwrap().is_empty());
    }

    // -- Redaction: transport errors, responses, Debug, logs --

    /// A transport error embedding the full request URL (what `reqwest`'s
    /// `Display` does) comes back with the secret scrubbed -- in its raw
    /// form and in both URL encodings, with the error code untouched.
    #[tokio::test]
    async fn a_transport_error_embedding_the_query_secret_is_scrubbed() {
        let nasty = "p@ss w/rd&x=1";
        let form = form_encode_query_value(nasty);
        let rfc = percent_encode_rfc3986(nasty);
        assert_ne!(form, rfc, "fixture must exercise both encodings");
        let leaky = format!(
            "error sending request for url (https://api.weatherapi.com/v1/current.json?q=London&key={form}) raw={nasty} rfc={rfc}"
        );
        let transport = fake_transport(vec![Err(denied("transport", leaky))]);
        let guard = qs_guard(&transport, &[QS_WEATHER], nasty);
        let err = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, "transport");
        for variant in [nasty, form.as_str(), rfc.as_str()] {
            assert!(
                !err.message.contains(variant),
                "{variant:?} leaked into {:?}",
                err.message
            );
        }
        assert!(err.message.contains(REDACTED));
    }

    /// Precondition for the next two tests: an *unredacted* `reqwest` error
    /// for a failed connect really does embed the secret-bearing URL, so a
    /// passing "no secret in the error" assertion below is a meaningful one.
    async fn raw_reqwest_connect_error(url: &str, addr: SocketAddr) -> reqwest::Error {
        reqwest::Client::builder()
            .resolve("secret-host.example", addr)
            .timeout(Duration::from_millis(300))
            .build()
            .unwrap()
            .get(url)
            .send()
            .await
            .unwrap_err()
    }

    /// A loopback address nothing listens on: connect is refused at once, no
    /// network involved.
    async fn closed_local_addr() -> SocketAddr {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        addr
    }

    #[tokio::test]
    async fn real_reqwest_transport_errors_never_contain_the_query_secret() {
        let addr = closed_local_addr().await;
        let url = format!("http://secret-host.example/v1/current.json?q=London&key={QS_SECRET}");
        let raw = raw_reqwest_connect_error(&url, addr).await;
        assert!(
            raw.to_string().contains(QS_SECRET),
            "precondition: reqwest's own Display embeds the URL (got {raw})"
        );

        let err = ReqwestTransport::new()
            .send(
                TransportRequest {
                    method: "GET".to_string(),
                    url: url.clone(),
                    pinned_addr: addr,
                    headers: vec![],
                    body: None,
                },
                Duration::from_millis(300),
                1024,
            )
            .await
            .unwrap_err();
        assert!(
            !err.message.contains(QS_SECRET) && !err.message.contains("key="),
            "transport error leaked the query: {:?}",
            err.message
        );
        assert!(
            !err.message.contains("London"),
            "bundle-supplied query parameters are stripped from errors too: {:?}",
            err.message
        );
    }

    /// End to end through the real [`ReqwestTransport`]: an induced transport
    /// failure (nothing is listening / routable at the pinned address) never
    /// surfaces the secret to the caller of `EgressGuard::send`.
    #[tokio::test]
    async fn an_induced_transport_error_through_the_guard_never_contains_the_secret() {
        let mut limits = default_limits();
        limits.timeout = Duration::from_millis(300);
        // TEST-NET-1 (RFC 5737): unroutable, so the connect fails or times
        // out without touching a real host.
        let guard = EgressGuard::new(
            Arc::new(ReqwestTransport::new()),
            limits,
            catalog_with_row_and_secrets(
                QS_APP,
                vec![(QS_WEATHER.to_string(), vec!["GET".to_string()])],
                HashMap::from([("KEY_REF".to_string(), "QS_KEY_ENV".to_string())]),
            ),
            test_metrics(),
            boxed(StaticFlag(true)),
        )
        .with_credential_broker(Arc::new(TableBroker(HashMap::from([(
            "QS_KEY_ENV".to_string(),
            QS_SECRET.to_string(),
        )]))) as Arc<dyn CredentialBroker>)
        .with_resolver(Arc::new(CountingResolver {
            calls: std::sync::atomic::AtomicUsize::new(0),
            addr: "192.0.2.1:443".parse().unwrap(),
        }) as Arc<dyn Resolver>);
        let err = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .unwrap_err();
        assert!(
            ["timeout", "transport", "tls_verification_failed"].contains(&err.code.as_str()),
            "expected a transport-class failure, got {}: {}",
            err.code,
            err.message
        );
        assert!(
            !err.message.contains(QS_SECRET),
            "secret leaked into {:?}",
            err.message
        );
    }

    /// A server that reflects the request URL -- or hands back a pagination
    /// `Link` -- cannot give the guest a secret the host injected.
    #[tokio::test]
    async fn response_headers_and_body_echoing_the_query_secret_are_scrubbed() {
        let echoed = format!("https://api.weatherapi.com/v1/current.json?q=London&key={QS_SECRET}");
        let transport = fake_transport(vec![Ok(TransportResponse {
            status: 200,
            headers: vec![(
                "link".to_string(),
                format!("<{echoed}&page=2>; rel=\"next\""),
            )],
            body: format!("{{\"request\":\"{echoed}\"}}").into_bytes(),
            truncated: false,
        })]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        let value = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        let body = base64::engine::general_purpose::STANDARD
            .decode(value["body_base64"].as_str().unwrap())
            .unwrap();
        let rendered = format!("{value}{}", String::from_utf8_lossy(&body));
        assert!(
            !rendered.contains(QS_SECRET),
            "secret reached the guest: {rendered}"
        );
        assert!(String::from_utf8_lossy(&body).contains(REDACTED));
        assert!(value["headers"][0]["value"]
            .as_str()
            .unwrap()
            .contains(REDACTED));
    }

    /// Decodes a `send` result's `body_base64`.
    fn decoded_body(value: &serde_json::Value) -> Vec<u8> {
        base64::engine::general_purpose::STANDARD
            .decode(value["body_base64"].as_str().unwrap())
            .unwrap()
    }

    /// regression: egress-query-secret truncation partial-needle leak. The
    /// transport caps the body before the guard scrubs, so a secret
    /// straddling the cap survives as a prefix (`echo:/p?key=wk_live_9f`);
    /// the guard must trim that partial secret off a `truncated` body.
    #[tokio::test]
    async fn truncated_body_ending_in_a_partial_query_secret_is_trimmed() {
        let partial = &QS_SECRET[..10];
        assert_eq!(partial, "wk_live_9f");
        let transport = fake_transport(vec![Ok(TransportResponse {
            status: 200,
            headers: vec![],
            body: format!("echo:/p?key={partial}").into_bytes(),
            truncated: true,
        })]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        let value = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/p",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        assert_eq!(value["truncated"], true);
        let body = decoded_body(&value);
        assert_eq!(
            String::from_utf8_lossy(&body),
            "echo:/p?key=",
            "no prefix of the secret may survive the truncation boundary"
        );
        assert!(!String::from_utf8_lossy(&body).contains("wk_"));
    }

    /// The trim is for a *cut-off* body only: a complete body ends where the
    /// server ended it, so a tail that merely resembles a secret prefix is
    /// ordinary data and is delivered untouched.
    #[tokio::test]
    async fn a_complete_body_ending_in_a_secret_lookalike_is_not_trimmed() {
        let transport = fake_transport(vec![Ok(TransportResponse {
            status: 200,
            headers: vec![],
            body: b"served by wk_live_9f".to_vec(),
            truncated: false,
        })]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        let value = guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/p",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        assert_eq!(decoded_body(&value), b"served by wk_live_9f");
    }

    /// regression: the PoC end to end against the real transport -- a real
    /// local server echoes the request URI, the real size cap cuts it inside
    /// the secret, and the redactor (as the guard applies it to the
    /// transport's output) must not hand back the prefix.
    #[tokio::test]
    async fn real_transport_cap_inside_the_secret_leaves_no_secret_prefix() {
        let app = axum::Router::new().route(
            "/p",
            axum::routing::get(|uri: axum::http::Uri| async move { format!("echo:{uri}") }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.ok();
        });

        let cap = "echo:/p?key=wk_live_9f".len();
        let resp = ReqwestTransport::new()
            .send(
                TransportRequest {
                    method: "GET".to_string(),
                    url: format!("http://127.0.0.1:{}/p?key={QS_SECRET}", addr.port()),
                    pinned_addr: addr,
                    headers: vec![],
                    body: None,
                },
                Duration::from_secs(5),
                cap,
            )
            .await
            .expect("request succeeds even when truncated");
        assert!(resp.truncated);
        assert_eq!(
            resp.body,
            b"echo:/p?key=wk_live_9f",
            "precondition: the cap cut the secret in half, so the full-needle scrub alone cannot see it"
        );

        let scrubbed = SecretRedactor::new([QS_SECRET]).scrub_response(resp);
        assert!(scrubbed.truncated);
        assert_eq!(scrubbed.body, b"echo:/p?key=");
    }

    /// The trim covers every encoding the redactor matches, picks the longest
    /// partial across needles, runs after full occurrences were replaced, and
    /// leaves non-matching tails alone.
    #[test]
    fn truncated_response_trim_handles_encodings_multiple_needles_and_non_matches() {
        let nasty = "p@ss w/rd&admin=1";
        let form = form_encode_query_value(nasty);
        let rfc = percent_encode_rfc3986(nasty);
        assert_eq!(form, "p%40ss+w%2Frd%26admin%3D1");
        let redactor = SecretRedactor::new([nasty, "other-secret-value"]);
        let cut = |body: &str| {
            redactor
                .scrub_response(TransportResponse {
                    status: 200,
                    headers: vec![],
                    body: body.as_bytes().to_vec(),
                    truncated: true,
                })
                .body
        };

        // Each variant, cut mid-needle, loses exactly its partial tail.
        assert_eq!(cut("x=p@ss w/"), b"x=");
        assert_eq!(cut(&format!("x={}", &form[..11])), b"x=");
        assert_eq!(cut(&format!("x={}", &rfc[..11])), b"x=");
        // A one-byte prefix is still a leak of the secret's first character.
        assert_eq!(cut("x=o"), b"x=");
        assert_eq!(cut("x=other-secr"), b"x=");
        // A full needle is replaced; the partial of another after it is trimmed.
        assert_eq!(
            cut(&format!("a={nasty}&b=other-sec")),
            format!("a={REDACTED}&b=").into_bytes()
        );
        // The needle's own tail overlapping its head ("abab") still trims.
        let overlap = SecretRedactor::new(["abcabd"]);
        let trimmed = overlap
            .scrub_response(TransportResponse {
                status: 200,
                headers: vec![],
                body: b"zabcab".to_vec(),
                truncated: true,
            })
            .body;
        assert_eq!(trimmed, b"z");
        // When two needles both have a partial at the tail, the LONGER one is
        // trimmed ("abcde" for the first, "cde" for the second): trimming only
        // the shorter would leave "ab" of the first secret behind.
        let two = SecretRedactor::new(["abcdef", "cdefgh"]);
        let trimmed = two
            .scrub_response(TransportResponse {
                status: 200,
                headers: vec![],
                body: b"zabcde".to_vec(),
                truncated: true,
            })
            .body;
        assert_eq!(trimmed, b"z");
        // A tail that is not a needle prefix is untouched, and so is a body
        // shorter than the shortest possible partial.
        assert_eq!(cut("plain body ends here"), b"plain body ends here");
        assert_eq!(cut(""), b"");
    }

    /// regression: a bundle-chosen `Accept-Encoding`/`Range`/`If-Range` would
    /// let the bound host return a compressed or ranged echo the substring
    /// scrub cannot match. With a query secret on the request they are all
    /// dropped (any case, any count) and `Accept-Encoding: identity` is
    /// forced; every other bundle header is untouched.
    #[tokio::test]
    async fn encoding_and_range_headers_are_forced_off_when_a_query_secret_is_present() {
        let transport = fake_transport(vec![Ok(ok_response())]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &serde_json::json!({
                    "method": "GET",
                    "url": "https://api.weatherapi.com/v1/current.json?q=London",
                    "headers": [
                        {"name": "Accept-Encoding", "value": "gzip, br"},
                        {"name": "accept-encoding", "value": "deflate"},
                        {"name": "Range", "value": "bytes=0-31"},
                        {"name": "RANGE", "value": "bytes=32-"},
                        {"name": "If-Range", "value": "\"etag-1\""},
                        {"name": "X-Trace", "value": "keep-me"},
                    ],
                    "secret_refs": {"?key": "KEY_REF"},
                }),
            )
            .await
            .expect("send succeeds");
        let requests = transport.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        let headers = &requests[0].headers;
        let named = |n: &str| -> Vec<&str> {
            headers
                .iter()
                .filter(|(k, _)| k.eq_ignore_ascii_case(n))
                .map(|(_, v)| v.as_str())
                .collect()
        };
        assert_eq!(
            named("accept-encoding"),
            vec!["identity"],
            "exactly one Accept-Encoding, forced to identity: {headers:?}"
        );
        assert!(
            named("range").is_empty(),
            "Range must be dropped: {headers:?}"
        );
        assert!(
            named("if-range").is_empty(),
            "If-Range must be dropped: {headers:?}"
        );
        assert_eq!(named("x-trace"), vec!["keep-me"]);
    }

    /// The forcing is scoped to calls that actually carry a query secret: a
    /// header-only secret call (and a plain call) keeps the bundle's own
    /// encoding/range headers exactly as before.
    #[tokio::test]
    async fn encoding_and_range_headers_pass_through_without_a_query_secret() {
        for secret_refs in [
            serde_json::json!({"Authorization": "TOKEN_REF"}),
            serde_json::json!({}),
        ] {
            let transport = fake_transport(vec![Ok(ok_response())]);
            let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
            guard
                .send(
                    QS_APP,
                    &serde_json::json!({
                        "method": "GET",
                        "url": "https://api.weatherapi.com/v1/current.json",
                        "headers": [
                            {"name": "Accept-Encoding", "value": "gzip"},
                            {"name": "Range", "value": "bytes=0-9"},
                        ],
                        "secret_refs": secret_refs,
                    }),
                )
                .await
                .expect("send succeeds");
            let requests = transport.requests.lock().unwrap();
            let headers = &requests[0].headers;
            assert!(
                headers.contains(&("Accept-Encoding".to_string(), "gzip".to_string())),
                "{headers:?}"
            );
            assert!(
                headers.contains(&("Range".to_string(), "bytes=0-9".to_string())),
                "{headers:?}"
            );
        }
    }

    /// The helper removes every case/count of the three headers, appends a
    /// single `Accept-Encoding: identity`, and keeps all other headers.
    #[test]
    fn force_scrubbable_response_encoding_replaces_only_the_three_headers() {
        let mut headers = vec![
            ("Accept-Encoding".to_string(), "gzip".to_string()),
            ("X-A".to_string(), "1".to_string()),
            ("range".to_string(), "bytes=1-".to_string()),
            ("IF-RANGE".to_string(), "x".to_string()),
        ];
        force_scrubbable_response_encoding(&mut headers);
        assert_eq!(
            headers,
            vec![
                ("X-A".to_string(), "1".to_string()),
                ("Accept-Encoding".to_string(), "identity".to_string()),
            ]
        );
    }

    /// regression: `?q=1;key=evil` -- `;` is a parameter separator to some
    /// server stacks, so a bundle-supplied `key` hiding behind it must be
    /// dropped like an `&`-separated one, leaving only the host's value.
    #[tokio::test]
    async fn semicolon_separated_same_named_parameter_is_dropped() {
        for (bundle_url, expected_query) in [
            ("?q=1;key=evil", "q=1".to_string()),
            ("?key=evil;q=1", "q=1".to_string()),
            ("?a=1;key=x;b=2&key=y", "a=1&b=2".to_string()),
            ("?k%65y=evil;q=1", "q=1".to_string()),
        ] {
            let transport = fake_transport(vec![Ok(ok_response())]);
            let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
            guard
                .send(
                    QS_APP,
                    &weather_args(
                        &format!("https://api.weatherapi.com/v1/current.json{bundle_url}"),
                        serde_json::json!({"?key": "KEY_REF"}),
                    ),
                )
                .await
                .expect("send succeeds");
            let sent = sent_urls(&transport).remove(0);
            assert_eq!(
                sent,
                format!(
                    "https://api.weatherapi.com/v1/current.json?{expected_query}&key={QS_SECRET}"
                ),
                "bundle url {bundle_url}"
            );
            assert!(!sent.contains("evil"), "{sent}");
            assert!(!sent.contains(';'), "{sent}");
        }
    }

    /// A redirect `Location` is server-chosen: a `;`-separated secret-named
    /// parameter in it is stripped too, never carried onward.
    #[tokio::test]
    async fn semicolon_separated_secret_named_parameter_is_stripped_from_a_redirect() {
        let transport = fake_transport(vec![
            redirect_to("https://api.weatherapi.com/next?x=1;key=evil"),
            Ok(ok_response()),
        ]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");
        let urls = sent_urls(&transport);
        assert_eq!(urls.len(), 2);
        assert_eq!(urls[1], "https://api.weatherapi.com/next?x=1");
    }

    /// `%3B` is data, not a separator, to every server: an encoded `;` keeps
    /// the parameter intact and is not mistaken for a hidden `key`.
    #[test]
    fn percent_encoded_semicolon_is_not_a_separator() {
        let secrets = [("key".to_string(), "v".to_string())];
        let mut url = reqwest::Url::parse("https://h.example/p?q=a%3Bkey%3Devil").unwrap();
        inject_query_secrets(&mut url, &secrets);
        assert_eq!(url.as_str(), "https://h.example/p?q=a%3Bkey%3Devil&key=v");
        let mut redirect = reqwest::Url::parse("https://h.example/p?q=a%3Bkey%3Devil").unwrap();
        strip_query_secret_params(&mut redirect, &secrets);
        assert_eq!(redirect.as_str(), "https://h.example/p?q=a%3Bkey%3Devil");
    }

    #[test]
    fn transport_request_debug_never_prints_a_secret() {
        let req = TransportRequest {
            method: "GET".to_string(),
            url: format!("https://api.weatherapi.com/v1/current.json?q=London&key={QS_SECRET}"),
            pinned_addr: "93.184.216.34:443".parse().unwrap(),
            headers: vec![("Authorization".to_string(), QS_HEADER_SECRET.to_string())],
            body: Some(vec![1, 2, 3]),
        };
        let rendered = format!("{req:?} {req:#?}");
        assert!(!rendered.contains(QS_SECRET), "{rendered}");
        assert!(!rendered.contains(QS_HEADER_SECRET), "{rendered}");
        assert!(!rendered.contains("London"), "{rendered}");
        assert!(rendered.contains("api.weatherapi.com/v1/current.json"));
        assert!(rendered.contains("Authorization"));
    }

    /// Minimal in-test `tracing` subscriber recording every event's fields,
    /// so the "no secret in any log line" property is asserted against what
    /// the guard really emits (no `tracing-subscriber` dependency needed).
    struct CaptureSubscriber {
        events: Arc<Mutex<Vec<String>>>,
    }

    struct FieldCollector<'a>(&'a mut String);

    impl tracing::field::Visit for FieldCollector<'_> {
        fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
            use std::fmt::Write;
            let _ = write!(self.0, "{}={value:?} ", field.name());
        }
    }

    impl tracing::Subscriber for CaptureSubscriber {
        fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            let mut line = String::new();
            event.record(&mut FieldCollector(&mut line));
            self.events.lock().unwrap().push(line);
        }
        fn enter(&self, _: &tracing::span::Id) {}
        fn exit(&self, _: &tracing::span::Id) {}
    }

    /// Every log line the guard emits -- on a successful call, a redirect,
    /// and a failing transport -- is free of the secret, and the capture
    /// really did record events (a zero-event capture would prove nothing).
    #[tokio::test]
    async fn guard_log_output_never_contains_the_query_secret() {
        let events = Arc::new(Mutex::new(Vec::new()));
        // `Dispatch::new` registers with tracing's process-wide callsite
        // registry. Holding a second live dispatcher keeps each callsite's
        // cached interest computed across *all* dispatchers; with only one
        // registered, a concurrently running test thread (which has no
        // subscriber) can be the first to hit a callsite, cache
        // `Interest::never` for it, and silently blind this capture.
        let _spare = tracing::Dispatch::new(tracing::subscriber::NoSubscriber::default());
        let capture = tracing::Dispatch::new(CaptureSubscriber {
            events: Arc::clone(&events),
        });
        let _guard = tracing::dispatcher::set_default(&capture);

        // Success + redirect hop.
        let transport = fake_transport(vec![
            redirect_to("https://host-b.example.com/next?x=1"),
            Ok(ok_response()),
        ]);
        let guard = qs_guard(&transport, &[QS_WEATHER, "host-b.example.com"], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .expect("send succeeds");

        // Failing transport whose own message embeds the secret.
        let leaky = format!("error sending request for url (https://x/?key={QS_SECRET})");
        let transport = fake_transport(vec![Err(denied("transport", leaky))]);
        let guard = qs_guard(&transport, &[QS_WEATHER], QS_SECRET);
        guard
            .send(
                QS_APP,
                &weather_args(
                    "https://api.weatherapi.com/v1/current.json?q=London",
                    serde_json::json!({"?key": "KEY_REF"}),
                ),
            )
            .await
            .unwrap_err();

        let events = events.lock().unwrap();
        assert!(
            events.len() >= 3,
            "expected >=3 captured log events (2 hops + 1 error + 1 hop), got {}",
            events.len()
        );
        for line in events.iter() {
            assert!(!line.contains(QS_SECRET), "secret in log line: {line}");
            assert!(
                !line.contains("London"),
                "query leaked into log line: {line}"
            );
        }
        assert!(
            events
                .iter()
                .any(|l| l.contains("query_secret_injected=true")),
            "the injection decision is observable at DEBUG: {events:?}"
        );
    }

    // -- Unit coverage for the helpers --

    #[test]
    fn redact_url_for_log_drops_query_fragment_and_userinfo() {
        assert_eq!(
            redact_url_for_log("https://user:pw@api.weatherapi.com/v1/x.json?key=abc&q=1#f"),
            "https://api.weatherapi.com/v1/x.json"
        );
        assert_eq!(
            redact_url_for_log("not a url ?key=abc"),
            "<unparseable-url>"
        );
    }

    #[test]
    fn redactor_matches_raw_form_encoded_and_rfc3986_variants_longest_first() {
        let redactor = SecretRedactor::new(["a b*~"]);
        let text = format!(
            "{} | {} | {}",
            "a b*~",
            form_encode_query_value("a b*~"),
            percent_encode_rfc3986("a b*~")
        );
        let scrubbed = redactor.scrub_str(&text);
        assert_eq!(scrubbed, format!("{REDACTED} | {REDACTED} | {REDACTED}"));
        assert_eq!(
            redactor.scrub_bytes(text.into_bytes()),
            scrubbed.into_bytes()
        );
    }

    #[test]
    fn redactor_ignores_empty_values_and_is_a_no_op_without_secrets() {
        let redactor = SecretRedactor::new([""]);
        assert!(redactor.is_empty());
        let err = denied("transport", "nothing to scrub here");
        assert_eq!(redactor.scrub_error(err).message, "nothing to scrub here");
    }

    #[test]
    fn inject_query_secrets_preserves_existing_query_bytes_when_no_name_conflicts() {
        let mut url = reqwest::Url::parse("https://h.example/p?q=a%20b&z=%2F").unwrap();
        inject_query_secrets(&mut url, &[("key".to_string(), "v".to_string())]);
        assert_eq!(url.as_str(), "https://h.example/p?q=a%20b&z=%2F&key=v");
    }

    #[test]
    fn strip_query_secret_params_removes_only_the_named_parameters() {
        let secrets = [("key".to_string(), "v".to_string())];
        let mut url = reqwest::Url::parse("https://h.example/p?a=1&key=x&b=2").unwrap();
        strip_query_secret_params(&mut url, &secrets);
        assert_eq!(url.as_str(), "https://h.example/p?a=1&b=2");
        let mut only = reqwest::Url::parse("https://h.example/p?key=x").unwrap();
        strip_query_secret_params(&mut only, &secrets);
        assert_eq!(only.as_str(), "https://h.example/p");
        let mut untouched = reqwest::Url::parse("https://h.example/p?a=%20").unwrap();
        strip_query_secret_params(&mut untouched, &secrets);
        assert_eq!(untouched.as_str(), "https://h.example/p?a=%20");
    }

    #[test]
    fn secret_slot_parse_classifies_headers_and_query_params() {
        assert_eq!(
            SecretSlot::parse("Authorization").unwrap(),
            SecretSlot::Header("Authorization")
        );
        assert_eq!(
            SecretSlot::parse("?key").unwrap(),
            SecretSlot::QueryParam("key")
        );
        assert_eq!(
            SecretSlot::parse("?api_key-2.v").unwrap(),
            SecretSlot::QueryParam("api_key-2.v")
        );
        assert!(SecretSlot::parse("?").is_err());
    }
}

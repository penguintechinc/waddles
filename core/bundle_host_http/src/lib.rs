//! Shared bundle `http` host-capability egress guard (connector spec
//! §8.2), extracted from `core/svc_action::egress` (PR #459) so
//! `core/svc_process` can enforce the identical SSRF-hardened
//! allowlist/DNS-pinning/redirect-recheck/rate-limit pipeline for its own
//! process-stage bundles' `http` capability, rather than duplicating
//! ~2000 lines of security-critical logic. `svc_action`'s own
//! `src/egress.rs` is now a thin re-export shim over this crate -- see
//! that file's doc for the zero-behavior-change guarantee.
//!
//! **Consuming-crate seam:** this crate owns no bundle-snapshot storage of
//! its own. Each service implements [`egress::EgressRuleSource`] over its
//! own existing per-`app_id` state (`svc_action::distribution::
//! BundleCatalog`, `svc_process`'s own capability snapshot) so
//! [`egress::EgressGuard`] never depends on either service's concrete
//! catalog type.
//!
//! **`egress::EgressGuard::validate_dial` (landed).** The "second, narrower
//! entry point" this doc used to flag as future work: validates a bare
//! `host:port` dial target (a connector host transport establishing a
//! WebSocket-over-TLS or IRC-over-TLS connection) through the same
//! declared-host/category-match/SSRF/DNS-pinning steps `send` runs for
//! `http.send` (spec §8.2 steps 1-2, 5-7 -- no HTTP method/scheme/redirect/
//! response-size step applies to a bare connection establishment), and
//! returns the [`egress::ValidatedTarget`] to dial. **`core/svc_ingest`
//! adoption is still a following landing, not part of this one**:
//! `svc_ingest` implements its own [`egress::EgressRuleSource`] over its
//! connector manifest state (mirroring this crate's `svc_process` wiring)
//! and calls `validate_dial` before connecting its Discord (wss) and
//! Twitch (IRC-over-TLS) dial paths -- wiring those two call sites is
//! tracked as `svc_ingest`'s own follow-up; the guard-side pipeline itself
//! is complete and covered by this crate's own hermetic tests.
//!
//! **Query-parameter secret refs (landed).** A `secret_refs` slot named
//! `?<param>` (e.g. `?key` for WeatherAPI-style `?key=` auth) is resolved
//! host-side exactly like a header ref and appended to the URL's query, so
//! the value never enters the component -- injected only on the first hop to
//! a granted `net.http.fqdn` host, dropped on every redirect, and scrubbed
//! from logs, `Debug` output, transport errors and responses. Full contract:
//! `egress`'s module doc ("Secret refs: header slots and `?query` slots").
//!
//! **Upstream egress proxy (not part of this landing).** A
//! network-level egress gateway is planned separately. [`egress::
//! EgressLimits::proxy_url`] is the config seam reserved for it: `None`
//! today (every constructed `EgressLimits` in both consuming crates)
//! preserves the exact current direct-connect-to-pinned-address behavior;
//! `Some(url)` is read by [`egress::ReqwestTransport`] and handed to
//! `reqwest::Client::builder().proxy(...)`, so pointing a deployment at
//! the gateway is a config change, not a code change, once it exists. The
//! interaction between DNS-rebind pinning and a proxy that performs its
//! own resolution is intentionally out of scope here -- the gateway is
//! expected to enforce an equivalent policy on its own path; this field
//! only threads the address through.
//!
//! **Three-permission-family model (landed).** [`egress::EgressRuleRow`]
//! now carries three separate grant lists -- `fqdn_grants`
//! (`net.http.fqdn:<host>`), `public_ip_grants` (`net.http.public-ip:<ip>`),
//! `private_ip_grants` (`net.http.private-ip:<ip|cidr>`) -- each matched
//! only by its own grants, with category-crossing enforcement (an FQDN
//! resolving into a private IP requires a *separate* `private_ip_grants`
//! entry covering the resolved address; a private literal declared under
//! `public_ip_grants`, or vice versa, is never found by either list). A
//! `private_ip_grants` match is additionally gated by
//! [`egress::InstanceEgressPolicy`] (default deny, opt-in via
//! [`egress::EgressGuard::with_instance_policy`]), and every resolved
//! address is checked against an operator-configured
//! [`egress::ClusterCidrDenylist`] ([`egress::EgressGuard::
//! with_cluster_denylist`]) that beats any grant. The manifest/hub-api wire
//! format itself is unchanged (still the flat `net.http:<host>` list);
//! [`egress::EgressRuleRow::from_legacy_patterns`] auto-classifies each
//! pattern into its new category so `svc_action`/`svc_process`'s existing
//! adapters need no manifest-format migration -- a genuine three-
//! permission-id manifest format is tracked as follow-up hub-api work.
//! [`egress::is_forbidden_address`]'s always-enforced hard checks
//! (loopback, link-local, unspecified, multicast, broadcast, cloud
//! metadata) are never liftable by any of this; RFC1918/ULA/CGNAT are now
//! uniformly the "private range" bucket the three-category model gates
//! (superseding CGNAT's prior always-forbidden treatment).

pub mod egress;

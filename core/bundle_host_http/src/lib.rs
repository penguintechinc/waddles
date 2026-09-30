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
//! **Future adoption (`core/svc_ingest`, not part of this landing).**
//! `svc_ingest`'s connector host transports -- webhook callbacks, source
//! polling, sender requests, and non-HTTP dial targets (IRC/WebSocket
//! connection establishment) -- are the same category of "a stage-side
//! client reaches an operator/bundle-declared network endpoint" this
//! guard already governs for `http.send`. The intended shape for that
//! follow-up: `svc_ingest` implements its own [`egress::EgressRuleSource`]
//! over its connector manifest state (mirroring this landing's
//! `svc_process` wiring) and calls [`egress::EgressGuard::send`] for its
//! outbound HTTP calls unchanged. A non-HTTP dial target (IRC/WebSocket)
//! cannot reuse `send` itself (no HTTP request/response to shape), so that
//! adoption needs a second, narrower entry point performing only the
//! allowlist/SSRF/DNS-pinning steps (spec §8.2 steps 1-2, 5-7) and
//! returning the validated [`std::net::SocketAddr`] to dial -- **not
//! implemented in this landing** (scope explicitly deferred by the task
//! that added this doc paragraph); track as a follow-up before
//! `svc_ingest` adopts this crate.
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
//! **Explicitly deferred (raised after this crate's initial extraction,
//! out of scope for this PR -- track as follow-up work, not implemented
//! here):** a three-permission-family model
//! (`net.http.fqdn:<host>`/`net.http.public-ip:<ip>`/
//! `net.http.private-ip:<ip|cidr>`) with category-crossing enforcement
//! (an FQDN resolving into a private IP requires a separate private-ip
//! grant) and a configured cluster pod/service/node CIDR denylist. Today's
//! model is the connector spec's original single exact-host-allowlist
//! permission (`net.http:<host>`, declared as a plain hostname or IP
//! literal, optionally with a port) plus the always-enforced SSRF address
//! checks in [`egress::is_forbidden_address`] (loopback, link-local,
//! unspecified, multicast, broadcast, cloud metadata, RFC1918/ULA private
//! ranges, and CGNAT `100.64.0.0/10`) -- private ranges are gated by
//! [`egress::EgressLimits::allow_private_hosts`] exactly as before, never
//! by a separate permission family.

pub mod egress;

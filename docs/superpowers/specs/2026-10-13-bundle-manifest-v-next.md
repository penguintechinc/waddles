# Bundle Manifest v-next — Network Policy, Requires, Transport Capability, Kind — Design

**Status:** Design — decisions LOCKED by the product owner 2026-10-13 (do not re-litigate; no open items remain, §9).
**Date:** 2026-10-13
**Scope:** the security/dependency foundation that precedes the #840 platform connectors. Touches `libs/flask_core` (`AppManifest`), `hub_api` (`BundleManifestV2`, `bundle_approval_service`, seeder, consent summary), `core/bundle_host_http` (egress engine), `core/svc_action` + `core/svc_process` (egress catalogs), `core/svc_ingest` (ingress), `core/bundle_executor` + `wit/` (transport capability), the external `penguin-libs` `rust-bundle-host` crate (Rust `Manifest`, `CapabilityKind`) plus the penguin-libs **connection library** (company-wide network filter, §4.6), and all 79 `bundles/**` manifests.
**Ground truth:** anchors read at `origin/release/v3.0.X` @ `80bb66b7`; the branch has since advanced to `fcaffbbc`, which includes the merged connector host plumbing (B0, PR #844) — `core/svc_ingest/src/connector/` (`Transport` trait, registry, driver, sink) now exists on the release branch. Line numbers in §10 are from `80bb66b7` and may have drifted.
**Related:** `2026-09-28-connector-bundles.md` (B0 / transport families / hybrid model), `2026-09-28-bundle-permissions-and-capability-gate.md` (permission catalog, consent), `2026-09-28-connections-credentials-design.md`.

---

## 1. Overview & goals

Today a bundle manifest declares *what it does* (stages, consumes, permissions) but not *who it may talk to* or *what it depends on* in a form the platform enforces. Network egress is partly declared (`egress:` list, `net.http.*` permissions) but the runtime catalog that would enforce it is never populated; ingress is not declared at all; app→connector dependencies do not exist; and nothing distinguishes an app bundle from a platform-connector ("service") bundle.

| # | Goal | Mechanism |
|---|---|---|
| G1 | Every bundle declares its complete network surface, fail-closed | `network: {ingress, egress}` REQUIRED (§4) |
| G2 | App bundles state which service bundles they need; platform refuses to run them without | `requires: [{bundle, min_version?}]` (app bundles only), activation-time + uninstall guard (§5) |
| G3 | Service bundles get a declarative, host-checked network seam | `transport` capability (§6) |
| G4 | The two bundle classes are distinguishable and policy can differ | `kind: app \| service` (§7) |
| G5 | No field ships without teeth | Parser + enforcement land in the SAME change per field (§8) |

**Terminology (locked):** *service bundles* = platform connectors (Discord, Twitch, Slack, Kick, YouTube…); *app bundles* = everything tenants/communities install (commands, games, overlays…). Both carry `kind` and `network`; only app bundles carry `requires`.

**Locked resolutions (2026-10-13):**

| # | Resolution | Section |
|---|---|---|
| 1 | `network` is the hard, fail-closed ceiling enforced by the host (no/empty `network` = no network); `net.http` is a consent + methods layer on top, exact-host only; legacy flat `egress:` DEPRECATED, superseded by `network.egress` | §4.3 |
| 2 | `schema_version` bumps 2 → 3 (new `kind`/`network`/`requires`) | §2 |
| 3 | CIDR max block `/24`, universal, no core/non-core exception; IPv6 `/120` max block (confirmed) | §2.1 |
| 4 | Service bundles seed per-tenant rows on activation (tenant-wide activation = a row per tenant, never one global row) | §5.2 |
| 5 | Wildcards live ONLY in `network.{ingress,egress}`; `net.http.fqdn` stays exact-host | §2.1, §4.3 |
| a | Network filter extracted to the penguin-libs connection library for ALL apps company-wide; lands in F1 | §4.6, §8.1 |
| b | Final wildcard grammar (leftmost-label, PSL-validated, partial-label globs allowed) | §2.1 |

---

## 2. Manifest v-next schema

Annotated `bundle.yaml` (build manifest). The same three fields appear in `hub-manifest.yaml` (seeder / hub-api manifest) — **two manifest files per bundle, both must carry them** and are checked for equality by the seeder (§8.3).

```yaml
schema_version: 3            # BUMPED 2 → 3 (locked): v3 = REQUIRED kind + network, optional requires
app_id: waddles.core.connector.discord
name: Discord Connector
version: 1.0.0
kind: service                # NEW. app | service. REQUIRED. (§7)
feature: waddles.core.connector
module: core
provider: builtin
language: rust
artifact: component
execution_model: wasi-component
stages: { ... }              # unchanged

network:                     # NEW. REQUIRED on EVERY bundle (app + service). Hard ceiling, fail-closed. (§4)
  egress:                    # destinations the bundle may dial / call
    - discord.com            #   exact FQDN
    - "*.discord.com"        #   leftmost-label wildcard (NOT the apex)
    - "cdn-*.discord.com"    #   partial-label glob, leftmost label only
    - gateway.discord.gg
    - 162.159.128.0/24       #   IP CIDR — public OR private; max block /24 (§2.1)
  ingress:                   # sources the host will accept inbound traffic FROM for this bundle
    - 52.88.10.0/24          #   (same entry grammar)
    # [] = no inbound; block missing = validation ERROR

requires:                    # NEW. optional. APP bundles only (kind: app). (§5)
  - bundle: waddles.core.connector.discord   # a service bundle
    min_version: 1.2.0       #   SemVer 2.0.0, optional

permissions:                 # existing — structured catalog entries
  - id: connector.receive:discord
    justification: "..."
  - id: transport             # NEW capability id label (§6); service bundles only
    justification: "..."
```

An app with no network need (no/empty `network` = the host grants NO network):

```yaml
kind: app
network:
  egress: []                 # explicit "no network" — an empty list is valid, an absent block is not
  ingress: []
```

### 2.1 `network` entry grammar (locked)

**Wildcard grammar (final, locked):** the wildcard may appear only in the **leftmost label**, and its suffix must be a PSL-validated registrable domain (eTLD+1 or deeper). Two leftmost-label shapes with deliberately different reach:

| Shape | Example | Reach |
|---|---|---|
| Bare `*` label | `*.discord.com` | 1+ subdomain labels — **ANY depth**: `gateway.discord.com` and `a.b.discord.com`; never the apex `discord.com` (list it separately); never `notdiscord.com` / `discord.com.evil.com` |
| Partial-label glob | `cdn-*.discord.com`, `*-cdn.discord.com` | **EXACTLY ONE label**: `cdn-1.discord.com` yes, `cdn-1.x.discord.com` no |

No multi-star anywhere (`*.*.x.com`, `a*b*.x.com`). Resolving the apparent tension: `*` inside a *partial* label never spans a `.`; a *bare* `*` label is the explicit subdomain wildcard and is the one form that spans multiple labels.

| Entry | Verdict | Why |
|---|---|---|
| `discord.com` | VALID | exact FQDN |
| `*.discord.com` | VALID | bare leftmost `*` under eTLD+1 — matches subdomains at ANY depth |
| `cdn-*.discord.com` | VALID | partial-label glob, leftmost label — matches EXACTLY ONE label |
| `*-cdn.discord.com` | VALID | partial-label glob, leftmost label — matches EXACTLY ONE label |
| `discord` | INVALID | bare token — substring/non-FQDN, not a registrable domain |
| `*.com` | INVALID | wildcard at a TLD / public suffix |
| `*.co.uk` | INVALID | wildcard at a public suffix |
| `*.github.io` | INVALID | wildcard at a public suffix (private-section PSL entry) |
| `a.*.discord.com` | INVALID | wildcard in a non-leftmost label |
| `*.*.discord.com` | INVALID | multi-star |

Match semantics:

| Form | Example | Matches | Does NOT match |
|---|---|---|---|
| Exact FQDN | `discord.com` | `discord.com` | `a.discord.com` |
| Wildcard subdomain | `*.discord.com` | `a.discord.com`, `a.b.discord.com` (any depth) | `discord.com` (apex — list it separately), `notdiscord.com`, `discord.com.evil.com` |
| Intra-label glob | `cdn-*.discord.com` | `cdn-1.discord.com` (exactly one label deeper) | `cdn-.discord.com` (`*` needs 1+ chars), `cdn-1.x.discord.com`, `discord.com` |
| IP CIDR (public or private) | `1.2.3.0/24`, `fd00::/120` | any address in the block | anything outside |

Rules:
- **Label-boundary match, never substring.** Host is split on `.`; the entry's labels right of the leftmost label must equal the host's trailing labels. A `*` inside a partial label never spans a `.`; only the bare `*` label spans multiple labels. Two leftmost-label shapes: a bare `*` label (`*.X`) is the subdomain wildcard — matches any host with strictly more labels than X ending in X, at any depth, never the apex; an partial-label glob (`cdn-*.X`, `*-cdn.X`) matches exactly one label directly under X.
- **No wildcard at apex/TLD/public suffix.** The wildcard's suffix must be eTLD+1 or deeper per the Public Suffix List. Rejected: `*.com`, `*.co.uk`, `*.github.io`. Valid: `*.discord.com`, `*.foo.github.io`.
- **Wildcard only in the leftmost label** — none in any other label (`a.*.discord.com`), no multi-star, no lone `*` entry, no bare single-label token (`discord`).
- **F1 alignment:** penguin-libs F1 (draft PR #136) already implements all of §2.1 as confirmed — `psl` crate for PSL validation, `MIN_IPV4_PREFIX=24` / `MIN_IPV6_PREFIX=120`, bare-`*` = any depth, partial-label glob = exactly one label. Spec and F1 are aligned.
- **Wildcards live ONLY in `network.{ingress,egress}`.** `net.http.fqdn` permission targets stay exact-host (§4.3).
- Lowercase-normalized; ASCII/punycode hosts only (IDN must be supplied as punycode); no scheme, port, path, or userinfo in an entry (port policy stays with the existing allowed-ports list, §4.3).
- **PSL validation uses a pinned, offline copy** — never a runtime fetch. Rust: `psl` crate (F1 uses it); Python: `tldextract` (offline snapshot mode) or `publicsuffix2` — Western/non-PRC per `general.md` Supply Chain. PSL snapshot version is pinned and bumped deliberately (a bump can invalidate a previously-valid wildcard; re-validation of stored manifests at bump time is part of the bump procedure).
- **CIDR max block size — ALWAYS and UNIVERSAL, no core/non-core exception (locked, confirmed).** IPv4 CIDR entries accept `/24`–`/32` (reject broader: `/0`–`/23`, e.g. `/16`, `/8`); IPv6 CIDR entries accept `/120`–`/128` (reject broader: `/0`–`/119`). `/120` is the v6 analogue of `/24` — one entry grants at most 256 addresses. Rationale: forbids broad provider-range allowlisting and forces least-privilege destination lists. Enforced identically in BOTH validators (hub-api Python + penguin-libs Rust) and in the portable matcher (§4.6) — a wider CIDR can never reach the runtime catalog. Error reason `network_cidr_too_wide`. Supersedes the earlier private-only `/16`·`/64` floor for `network` entries (the catalog's `net.http.private-ip` bound is unchanged but is always ⊆ a `network` entry, hence within the cap).
- **CIDR:** both public and private are legal. Always-denied ranges (loopback, link-local, cloud metadata, cluster CIDRs) stay denied at runtime regardless of any `network` entry (§4.3). A CIDR that *overlaps* an always-denied range is a validation error where statically knowable (loopback/link-local/metadata); cluster-CIDR overlap is checked at install against the deployment's configured denylist.
- **Missing block = ERROR; empty list = "no network".** Same for `ingress` and `egress` independently — both keys required inside the block.

---

## 3. Cross-cutting constraints (apply to every subsystem below)

- **`schema_version: 3`.** The new required fields gate on the version bump: parsers accept v2 (legacy, no `kind`/`network`) only until F3 completes the migration, then refuse `< 3`. v3 manifests missing `kind`/`network` ⇒ validation error (the v2-era `egress:` list is deprecated, §4.3).
- **Unknown manifest keys are silently ignored today** (hub-api `parse_bundle_manifest_v2` reads named keys only; flask_core `parse_manifest` likewise; the penguin-libs Rust `RawManifest` is `#[serde(default)]` without `deny_unknown_fields`). Therefore each new field ships WITH its parser in all three parsers AND its enforcement in the same change — a parsed-but-unenforced field is a vulnerability dressed as a feature (it looks declared; nothing is checked).
- **Fail-closed / fail-loud:** missing `network` ⇒ validation error; unresolvable catalog row ⇒ deny (`host_not_declared`); unsatisfied `requires` ⇒ 422; PSL load failure ⇒ refuse to validate (never skip the PSL check). No silent fallback to "allow" anywhere (`critical-rules.md` Fail-Loud, Verification Integrity).
- **Three parsers + one re-parse path** must all learn each field:

| Parser | File | Notes |
|---|---|---|
| hub-api `BundleManifestV2` | `hub_api/services/bundle_manifest_v2.py` (`parse_bundle_manifest_v2` ~399) | Authoritative upload/seed gate. Also `bundle_approval_service._reparse_trusted` (~117) rebuilds the dataclass from stored `app_version_uploads.manifest_json` and MUST be taught the new fields or activation reads empty defaults. |
| flask_core `AppManifest` | `libs/flask_core/flask_core/app_manifest.py` (`parse_manifest` ~395) | Python-side shared model (reads `bundle.yaml`-shaped dicts, `KNOWN_MODULES` etc.). |
| penguin-libs Rust `Manifest` | external: `penguin-libs/packages/rust-bundle-host/src/manifest/{schema,rules}.rs` | Consumed via git `rev` pin (waddles `bundle_executor`/`bundle_host_http`/`bundle_compiler` pin `b063b7ba…`, crate `=0.2.0`; `bundle_compiler` pins an older `69f77b1a…`). Needs a penguin-libs PR + release + waddles rev bump (all three Cargo.tomls — and the divergent `bundle_compiler` pin reconciled). |

- **PII-free:** manifests carry hostnames/CIDRs/app ids only. Denial logs/metrics may include `app_id`, rule family, and reason code; never request URLs with query strings, headers, bodies, or user identifiers (the existing `EgressGuard` redaction posture is the model — `egress.rs` module doc). Metric labels stay low-cardinality (`app_id`, `reason`, `direction`); never host or CIDR as a label.
- **Vendor-tier is stricter than first-party** (§8.4).
- **Docs & tests in the same PR** for every field (bundle README, unit + regression + integration tests; real-path, not mocked, per `testing.md`/`enforcing-no-stubs`).

---

## 4. Subsystem: Network policy (`network.egress` / `network.ingress`)

### 4.1 Current state (verified)

**Egress engine — exists, solid, per-bundle by design, fail-closed** (`core/bundle_host_http/src/egress.rs`, 6370 lines):

| Piece | Where | Behaviour today |
|---|---|---|
| `EgressGuard::send` | `egress.rs` ~795 (denial `host_not_declared` ~967; port allowlist ~993) | https-only, no embedded creds, declared-host match, method check, denylist, port allowlist, DNS resolve + **pin**, SSRF address classification, redirect re-validation, response redaction. |
| `EgressRuleRow` | ~145 | Three **separate** grant lists: `fqdn_grants`, `public_ip_grants`, `private_ip_grants`, each `(pattern, methods)`; never cross-matched. |
| `match_grant` / `host_matches` | ~1828 / ~1388 | `host_matches` is **exact** (case-insensitive) match only — doc comment: a `*.` pattern "is treated as a literal string and will never match any concrete host". |
| `validate_dial` | ~1238 (`validate_dial_checked` ~1262) | Non-HTTP dial validation (WSS/IRC-TLS): declared-host, denylist, declared-port, DNS pin, SSRF classification, `waddles.core.bundle-egress` flag. **No callers outside the crate** (`lib.rs` docs say `svc_ingest` adoption is a follow-up). **Missing the `limits.allowed_ports` check** that `send` applies at ~993 — a dial to `host:6379` of a declared host passes `validate_dial` but would be refused by `send`. |
| DNS resolve + pin | `TokioResolver` ~2146, pinned-address use in `send`/`validate_dial` | Resolve once, classify every returned address, dial the pinned `SocketAddr` (anti-rebinding). |
| `is_forbidden_address` | ~1515 | Loopback/unspecified/multicast/broadcast/metadata/link-local/private-unless-allowed; IPv4-mapped/NAT64/compat v6 canonicalized (`embedded_ipv4` ~1415) first. |
| Cluster CIDR denylist + instance policy | `ClusterCidrDenylist` ~1710, `InstanceEgressPolicy` | Never liftable by any grant; private-IP egress additionally opt-in at instance level. |
| IP/CIDR match | `core/egress_assertion/src/lib.rs` `ip_matches_grant` ~437 | **Public-IP grants are exact-address only** (no CIDR); private-IP grants accept literal or CIDR. |

**Permission-catalog layer** (`core/bundle_capability_gate/src/permission.rs`, mirrored by `hub_api/services/bundle_permission_catalog.py`): `net.http.fqdn:<host>` (wildcard **rejected**, `validate_host` ~880), `net.http.public-ip:<ip>` (CIDR **rejected**, ~903), `net.http.private-ip:<ip|cidr>` (**private only**, prefix bounded `/16` v4 · `/64` v6, `MAX_PRIVATE_PREFIX_*` ~14). These are the consent-flow grants (risk-classified, per-tenant/community re-consent for IP families).

**Manifest layer today:** a top-level `egress: [{host, methods}]` list (hub-api `_EGRESS_HOST_RE` accepts a leading `*.`; penguin-libs rule V19/V20 accepts "a single-label `*.` wildcard") — so wildcards are *accepted at manifest validation* yet *never match at runtime* (above). `_derive_capabilities` (`bundle_approval_service.py` ~195) derives the `http` capability from `manifest.egress` being non-empty.

**DEAD WIRING (the headline gap):** the per-`app_id` catalog the guard consults is never populated in production.
- `svc_action`: `BundleCatalog` constructed empty at `core/svc_action/src/lib.rs:265`; `distribution.rs` doc: the legacy poll writer was **retired 2026-09-27**, "neither path writes into this catalog today".
- `svc_process`: `HttpEgressCatalog::new()` at `core/svc_process/src/lib.rs:1089` — comment: "starts empty: no writer populates it yet".
- Effect: `catalog.resolve(app_id)` → `None` → empty row → **every `http.send` is denied `host_not_declared`**. This is fail-closed (safe) but means `http` is functionally unusable and the egress engine has never run against a real manifest in prod. The intended writer is the DB-driven loader (`core/bundle_active_set` — `ActiveBundleRow`, `query.rs` ~26, today carries **no** egress/network columns; only `declared_capabilities`).
- `wasi:sockets` is denied natively in the executor (`core/bundle_executor/src/host/mod.rs` `build_wasi_ctx` ~242: `allow_tcp(false)`, `allow_udp(false)`, `allow_ip_name_lookup(false)`) — bundles can only reach the network through host capabilities.

**Ingress — ABSENT per-bundle.** The only inbound controls are platform-specific webhook hardening in `svc_ingest`: Twitch EventSub HMAC-SHA256 over `message_id+timestamp+body` + 10-minute replay window (`core/svc_ingest/src/ingest/twitch_eventsub.rs` `REPLAY_WINDOW` = 600s ~64; handler `core/svc_ingest/src/http/eventsub.rs`). No source-IP or source-domain check exists anywhere; no per-bundle ingress declaration.

### 4.2 Target

- `network.egress` is the bundle's **complete, declared destination ceiling** for every host-mediated outbound path (`http.send`, `transport` dials, any future outbound capability). Nothing outbound is reachable that is not covered by it.
- `network.ingress` is the **declared set of source addresses/domains the host will accept inbound traffic from on behalf of this bundle** (webhook receivers, interaction endpoints). Host-side check runs **before** any bundle is resolved/invoked and before body parsing (consistent with connector spec §2 webhook ordering: raw-bytes verification first).
- Entry grammar §2.1. `[]` = no network in that direction.
- Runtime matching semantics identical for http and transport: wildcard and CIDR matching are implemented **once**, in the portable penguin-libs connection library (§4.6), and consumed by both `send` and `validate_dial` (`egress_assertion::ip_matches_grant`'s own doc already states the principle — "one CIDR/IP-parsing implementation both call so the two can never silently diverge").
- **Non-negotiable layering (existing invariants unchanged):** a `network.egress` match is necessary, never sufficient. Still applied after a match: scheme/method/port allowlist, DNS resolve + pin, SSRF address classification (an FQDN resolving into private space is still denied unless separately covered by a private-IP grant and instance policy), always-denied ranges, cluster-CIDR denylist, redirect re-validation, credential-header scoping (`secret_granted_hosts`). Wildcard entries make the *name* set larger; they never relax the *address* checks.
- A wildcard-matched host is resolved and pinned exactly like an exact host; the pinned-address SSRF check is the backstop against a wildcard covering an attacker-controlled subdomain that points at internal space.

### 4.3 Relationship to existing egress surfaces (locked)

| Surface | v-next role |
|---|---|
| `network.egress` | **Hard ceiling** — a manifest-declared, fail-closed host allowlist enforced by the host. A bundle with no/empty `network` gets NO network. Source of truth the runtime catalog is built from. |
| `permissions: net.http.*` | **Consent + allowed HTTP methods layer ON TOP of `network`** — the user-facing permission summary plus per-host methods. **Exact-host only** (no wildcard in `net.http.fqdn`; wildcards live only in `network`). Each `net.http.fqdn/public-ip/private-ip` target MUST be covered by `network.egress` (new validation error `permission_outside_network`). Catalog id grammar unchanged. |
| legacy top-level `egress: [{host, methods}]` | **DEPRECATED — superseded by `network.egress`.** Migration: each legacy `host` moves to `network.egress` (a legacy `*.` host becomes a `network` wildcard under §2.1 rules; a host that fails §2.1 must be fixed by the author), each legacy `methods` set moves to the matching `net.http.*` grant. During F2 validation requires each legacy entry be covered by `network.egress`; the runtime ignores it once the catalog is built from `network` + granted `net.http.*`. Removed from all manifests in F3. |

Runtime catalog row for an `app_id` = `network.egress` entries classified into the three families (`EgressRuleRow::from_legacy_patterns`' classifier is the existing migration seam, extended with wildcard-FQDN and public-CIDR entries), with `methods` taken from the matching granted `net.http.*` permission (no matching grant ⇒ no HTTP methods ⇒ http denied, transport still dial-checked).

### 4.4 Enforcement points

| # | Point | Change |
|---|---|---|
| E1 | hub-api manifest gate (`parse_bundle_manifest_v2`) | Require `schema_version: 3` + `network.{ingress,egress}`; grammar §2.1; PSL check; CIDR bounds (`/24` max, `/120` v6 — reject wider); `permission_outside_network`; error reasons `missing_network`, `invalid_network_entry`, `network_wildcard_public_suffix`, `network_cidr_too_wide`, `network_cidr_denied_range`. |
| E2 | flask_core `parse_manifest` + penguin-libs `Manifest` | Same grammar + PSL + CIDR cap (Python `tldextract`/`publicsuffix2`; Rust `psl` — Rust side is the penguin-libs connection library, §4.6). Shared conformance fixtures (valid/invalid corpus) consumed by BOTH test suites so the parsers cannot drift (existing `tests/fixtures/manifests/equivalence/` pattern in penguin-libs is the precedent). |
| E3 | Consent summary (`build_permission_summary`, `classify_diff` ~335) | Add `network` to the summary and to `classify_diff`'s `_flatten` set — a widened network block is `widened` ⇒ re-consent; included in `permission_hash`. Wildcard and CIDR entries are visually flagged in the consent UI. |
| E4 | Stored manifest round trip (`_reparse_trusted`) | Re-hydrate `kind`, `network`, `requires`. |
| E5 | Active-set loader (`core/bundle_active_set` `ActiveBundleRow`/query) | Carry `network` (parsed from the version's stored manifest_json, or a dedicated column — implementation choice in F2) alongside `declared_capabilities`. |
| E6 | **Wire the dead catalogs** | `svc_action` `BundleCatalog` (`lib.rs:265`) and `svc_process` `HttpEgressCatalog` (`lib.rs:1089`) populated by each service's `bundle_loader` on `load`, removed on `unload`, driven by the active-set diff — same lifecycle as existing `CapabilitySnapshot`. Test must prove a seeded bundle's declared host is **allowed** and an undeclared one **denied** end-to-end (the guard has never run against a real row in prod). |
| E7 | Egress matcher | `EgressGuard` consumes the portable penguin-libs matcher (§4.6) instead of growing its own: leftmost-label wildcard + partial-label glob, public CIDR (`PublicIp` arm in `ip_matches_grant` currently exact-only; `/24` cap enforced). Negative tests: `notdiscord.com`, `discord.com.evil.com`, apex-vs-wildcard, `a.b.discord.com`, `a.*.discord.com`, `*.co.uk`, `/16` and `/23` CIDRs rejected, v4-mapped-v6 against a v4 CIDR. |
| E8 | `validate_dial` | Add the missing `limits.allowed_ports` check (parity with `send` ~993); regression test: declared host + undeclared port ⇒ `port_not_allowed`. Gains its first real caller via §6. |
| E9 | Ingress gate (`svc_ingest/src/http/*` webhook handlers) | Before bundle resolution/body parse: resolve the receiving bundle's `network.ingress`; source IP (from the trusted-proxy-aware peer address — XFF only from configured trusted proxies, never blindly) must match an ingress CIDR, or reverse-verified domain entries (domain entries resolved on a bounded TTL and matched by address; never trust a client-supplied `Host`/`Origin` as a source identity). Empty `ingress` ⇒ the bundle receives no inbound. Denial counter `…ingress_denied_total{app_id,reason}`. Existing HMAC/replay checks remain (defence in depth, not replaced). |
| E10 | Seeder / installer | Reject at seed/upload any manifest missing the block; seeder must fail the batch loud (never skip-and-continue) on a malformed network block. |

### 4.5 Build scope (exists vs build)

| Capability | Status |
|---|---|
| FQDN/IP exact matching, DNS pin, SSRF classes, redirects, denylists, metrics | EXISTS (`egress.rs`) |
| Cluster CIDR denylist, instance private-IP policy | EXISTS |
| Private CIDR matching (`/16`·`/64` floor — tightened to `/24`·`/120` for `network` entries, §2.1) | EXISTS (private only) |
| Manifest `egress:` list parse (with `*.` accepted but inert) | EXISTS — becomes deprecated |
| `network` block (3 parsers + `_reparse_trusted`) | BUILD |
| Label-boundary wildcard matcher | BUILD |
| Public-CIDR matcher + grammar (`/24` universal cap, §2.1) | BUILD |
| Portable network filter extraction to penguin-libs connection library (§4.6) | BUILD (F1) |
| PSL validation (pinned offline) in Rust + Python | BUILD |
| Per-app catalog writer from active set (E5/E6) | BUILD |
| Consent summary + diff for `network` | BUILD |
| `validate_dial` port check | BUILD (small fix) |
| Per-bundle ingress (declaration, gate, metrics) | BUILD |
| `network` block on all 79 manifests (+77 hub-manifests) | BUILD (F3 migration) |

### 4.6 Network filter extraction — penguin-libs connection library (company-wide)

The network filter is **not Waddles-specific**. It is extracted to the penguin-libs **connection library** so every PenguinTech app gets the same fail-closed `network` enforcement (locked). Lands in **F1**.

| Moves to penguin-libs (portable core) | Stays in Waddles (bundle-specific wiring) |
|---|---|
| FQDN / leftmost-label-wildcard / partial-label-glob / IP-CIDR matcher (the §2.1 grammar, incl. the `/24` v4 · `/120` v6 cap) | Manifest → per-`app_id` catalog row population (E5/E6) |
| Public-Suffix-List apex validation (pinned offline snapshot) | Cluster-CIDR denylist + instance egress policy configuration |
| DNS resolve-then-pin / SSRF address-classification guard | Capability-gate integration (`net.http.*` ⊆ `network`, `transport` kind gate) |
| `network: {ingress, egress}` policy types (+ serde, validation errors) | Consent summary / `classify_diff` / `permission_hash` |

- **Source of the extraction:** the matcher and DNS-pin logic in `EgressGuard` (`core/bundle_host_http/src/egress.rs`) is already pure and portable; the Waddles-specific part is the manifest/row plumbing around it. Extraction is a lift-and-parameterise, not a rewrite — existing `egress.rs` tests move with the code and keep passing against the library.
- **One implementation, every consumer:** Waddles `EgressGuard`, the Rust `Manifest` validator, and other apps consume the same crate; the Python validators (hub-api, flask_core) are held to it by the shared conformance corpus (§3, E2) since they cannot link the Rust crate.
- Crate/package layout inside penguin-libs is an F1 implementation decision; the contract above is the locked part.

---

## 5. Subsystem: `requires` (app→service dependency)

### 5.1 Current state (verified)

- **ABSENT.** No `requires` key in any parser or manifest. The nearest mechanism is `routes_to` (app→app routing targets), validated at activation by `_validate_routes_to` (`bundle_approval_service.py` ~260): target must exist in `app_catalog` (`routes_to_target_not_found`, 422) and be available in the tenant (`routes_to_cross_tenant`, 422), audited on refusal. Called from `activate_tenant_wide` (~879; call ~949) and `activate_for_community` (~1013; call ~1076).
- **SemVer is already stored and validated** (`_SEMVER_RE` ~44 in `bundle_manifest_v2.py`); the comparator exists in `libs/flask_core/flask_core/platform_version.py` (`_semver_core` ~60 — returns `(major, minor, patch)`, **ignores pre-release/build metadata** by design for platform min/max gates).
- **Registry tables:** `app_catalog`, `app_versions`, `app_version_uploads` (holds `manifest_json`), `app_global_installs`, `bundle_tenant_availability` (per tenant `available`, `pinned_version_id`), `app_active_versions` (PK `app_id, tenant_id, community_id`; `community_id = 0` is the **tenant-wide sentinel**), `app_install_approvals`.
- **Uninstall:** `uninstall_globally` (~525) revokes the global install and **cascades down** (`tenant_app_availability_service.unset_available` per tenant, best-effort, which deactivates per community). **No reverse-dependency guard.** `deactivate_for_community` (~1156) / `deactivate_tenant_wide` (~1210) likewise unguarded.
- **Seeder:** `hub_api/cli/seed_core_bundles.py` — `seed_one` (~729) publishes then activates per target; the batch loop iterates catalog entries in **file order** (~1249) with per-bundle failure isolation (a failing bundle is logged and the loop continues). No dependency ordering.

### 5.2 Target (locked)

- `requires: [{bundle: <app_id>, min_version?: <SemVer>}]` — a **list of service-bundle dependencies, each with an OPTIONAL minimum version**. **App bundles only** (`kind: app`); a `kind: service` manifest declaring `requires` is refused (`requires_not_allowed_for_service`). `bundle` is a full `app_id` (`waddles.<module>.<feature>.<name>`); `min_version` is SemVer 2.0.0, optional (absent = any active version).
- **Enforced at activation time** (the moment a bundle would start running for a tenant/community), not install time — availability/install of an app whose service is missing is allowed; *activation* is refused.
- **Satisfaction rule:** a dependency on service `S` is satisfied for a dependent activating in `(tenant T, community C)` iff `app_active_versions` has a row `(S, T, community_id ∈ {C, 0})` whose version ≥ `min_version`. I.e. **tenant-wide (`community_id = 0`) satisfies community-level deps via `max(scope, tenant)`** (consistent with the tier cascade, `critical-rules.md`). Corollary: a **tenant-wide dependent** (`community_id = 0`) requires the service active **tenant-wide** — a community-scoped service activation does not satisfy a tenant-wide app.
- **Missing or below-min ⇒ activation BLOCKED: HTTP 422, code `requires_unsatisfied`**, body lists each unmet `{bundle, min_version, found_version|null}`; audited like `routes_to` refusals.
- **Target kind:** a `requires.bundle` MUST resolve to a `kind: service` bundle (checked at activation against the target's stored manifest; at validation time only `app_id` shape is checked — the target may not be seeded yet). Because only app bundles carry `requires` and targets must be service bundles, the dependency graph is bipartite (app → service) and **cannot contain cycles**; the seeder still orders services before dependents (below).
- **Service bundles seed per-tenant rows on activation (locked):** tenant-wide activation of a service bundle seeds an `app_active_versions` row **per tenant** (`community_id = 0`), never a single global row — so the satisfaction rule above is uniform, and per-tenant availability/pinning/uninstall guards work unchanged. (Resolves the former OPEN-4.)
- **Seed ordering:** the seeder builds the dependency edges over the catalog's manifests and seeds **service bundles before their dependents** (ties by existing file order for determinism). A `requires` target absent from the catalog **and** not already active aborts the batch with a loud `CoreBundleSeederError` naming the target — never skip-and-continue for a dependency error. A defensive cycle check still runs in the seeder and hub-api (a malformed `kind` flip must not create one), but it is a tripwire, not an expected path.
- **Reverse-dependency guard — block, never cascade:** uninstall of a service bundle with active dependents is **refused with HTTP 409** (`has_live_dependents`) and the response **lists the dependents** `{app_id, tenant_id, community_id, required_min}`. The same guard covers tenant hide (`unset_available`), `deactivate_for_community`, `deactivate_tenant_wide` (uninstall = `uninstall_globally`), and any active-version pointer change that would drop a live dependent below its `min_version` (downgrade/rollback). An operator must deactivate the dependents first. This **replaces** the current cascade-down behavior *for dependents of a service*; the existing cascade for ordinary apps (hide ⇒ deactivate in communities) is unchanged. "Live dependent" = an `app_active_versions` row whose active version's manifest `requires` the target and which would become unsatisfied (a community-level service deactivation is not blocked by a dependent that remains satisfied by a tenant-wide activation).

### 5.3 Enforcement points

| # | Point | Change |
|---|---|---|
| R1 | `parse_bundle_manifest_v2`, `AppManifest`, Rust `Manifest` | Parse + validate `requires` (app_id shape, SemVer, no self-require, no duplicates; only valid when `kind: app`). |
| R2 | `_reparse_trusted` | Rehydrate `requires`. |
| R3 | `activate_tenant_wide` (~879) and `activate_for_community` (~1013), beside `_validate_routes_to` call sites (~949 / ~1076) | New `_validate_requires(...)`: query `app_active_versions` ⋈ `app_versions` for each dependency at `community_id ∈ {C, 0}`; compare with the SemVer comparator; raise 422 `requires_unsatisfied`; audit refusal. |
| R4 | `uninstall_globally` (~525), `tenant_app_availability_service.unset_available`, `deactivate_for_community` (~1156), `deactivate_tenant_wide` (~1210), version-pointer swap | New `_assert_no_live_dependents(...)` ⇒ 409 `has_live_dependents` with list. Runs **before** any state mutation (guard first, mutate second — never mutate-then-roll-back). |
| R5 | Seeder `seed_one` loop (~1249) | Services-before-dependents ordering + missing-target abort (+ defensive cycle tripwire) before the first write; per-tenant row seeding for service bundles. |
| R6 | SemVer comparator | Extend/extract a **full** SemVer 2.0.0 comparator (pre-release ordering) for `min_version`; `_semver_core` ignores pre-release by design for platform gates — a dependency `min_version: 1.2.0` must not be satisfied by `1.2.0-rc.1`. Place beside `platform_version.py`; shared by hub-api. |
| R7 | Data plane (defence in depth) | Active-set loader excludes/unloads a dependent whose service has become inactive (covers manual DB edits and races); logs loudly, counts `…requires_unsatisfied_total`. |
| R8 | Consent summary | Show required services + min versions in the install/activate consent screen and include in `permission_hash`. |

### 5.4 Build scope

| Capability | Status |
|---|---|
| SemVer grammar/regex; platform-version comparator (core only) | EXISTS |
| Activation choke points + `routes_to` precedent (audit, 422) | EXISTS |
| `app_active_versions` tenant-wide sentinel + availability table | EXISTS |
| `requires` parse (3 parsers + `_reparse_trusted`) | BUILD |
| Full SemVer comparator (pre-release) | BUILD |
| `_validate_requires` (activation) | BUILD |
| Reverse-dependency guard (5 mutation paths) | BUILD |
| Seeder service-first ordering + missing-target abort + per-tenant service rows | BUILD |
| Consent summary / data-plane defence-in-depth | BUILD |

---

## 6. Subsystem: `transport` capability (service-bundle network seam)

### 6.1 Current state

- **Bundles have no sockets.** `wasi:sockets` is denied in the executor (`bundle_executor/src/host/mod.rs` ~242). The only network-shaped capability bundles hold today is the guarded `http` import (egress engine above).
- **B0 (#844, MERGED — on `release/v3.0.X` @ `fcaffbbc`):** connector host plumbing — executor `ConnectorHost` invoking `waddle:connector@1.0.0` exports (`on-connect`/`on-frame`/`on-heartbeat-due`/`on-disconnect`), `core/svc_ingest/src/connector/` (`Transport` trait in `transport.rs`, always-pinned `ConnectorRegistry`, `ConnectorDriver`, `EventSink`), `core/connector_e2e/`. Its `Transport` trait is **host-owned** (connect / send_handshake / next_frame / on_frame_outcome; fatal vs retryable errors; secret-handle substitution in the outbound send path). B0 is the transport capability's base and is no longer a blocker.
- `EgressGuard::validate_dial` exists with no callers (§4.1). `wit/waddle-connector/connector.wit` exists on release; `wit/waddle-bundle/stage.wit` carries the app-bundle `CapabilityKind` binding.
- **Two `CapabilityKind` enums today:** (a) `penguin_bundle_host::wire::CapabilityKind` in penguin-libs — `Http, Kv, Db, Relay, Flags, Log, Clock, Context` (the linker-level wire type); (b) `bundle_capability_gate::CapabilityKind` (`permission.rs` ~137) — a deliberately independent, audit-metadata superset (`Objects, Overlay, Ai, Users, Telemetry, Reputation, Economy, Identity, Streaming, Interaction, …`). Reconciling them is the already-planned linker-wiring task (permission.rs doc ~130).

### 6.2 Target (locked)

- **penguin-libs `CapabilityKind::Transport`** (wire enum) + WIT verbs `open` / `send` / `subscribe` / `close` / `listen` over B0's existing host `Transport` impls; mirror variant added to `bundle_capability_gate::CapabilityKind` and the permission catalog (a `transport` catalog id, **dangerous**, service bundles only, global-approval-only per connector spec §3.2).
- **Every dial is checked against the bundle's `network.egress`** — `open`/`subscribe` call `EgressGuard::validate_dial(app_id, "host:port")` (the first real caller); the returned `ValidatedTarget.pinned_addr` is what the host actually connects to (never a re-resolve). `listen` (inbound sockets) is checked against `network.ingress`.
- **Hybrid model** (connector spec, locked): the **host keeps the inbound frame pump** (connect/reconnect/backoff/heartbeat isolation/resume/credential substitution stay 100% host code — bundles still never hold a socket or a resolved secret); the **capability is the declarative grant** that says which dials/listens the host may perform *on this bundle's behalf* and lets a bundle initiate `send`-class operations through the same checked seam. A bundle never gets a raw file descriptor, resolved IP, or resolved credential.
- Without the `transport` grant a bundle cannot cause any non-HTTP dial; with it, only to `network.egress` entries (minus always-denied ranges, minus cluster CIDRs, subject to the allowed-ports list and instance policy).

### 6.3 Enforcement points & build scope

| # | Item | Status |
|---|---|---|
| T1 | penguin-libs: `CapabilityKind::Transport` + WIT verbs + wire message handling; release; waddles rev bump | BUILD (F1 for the enum/structs; verbs host impl in F2) |
| T2 | `bundle_capability_gate`: `Transport` kind + catalog id (`dangerous`), consent-summary rendering | BUILD |
| T3 | Host import impl in `bundle_executor` (`host/…_imports.rs` family, beside `connector_imports.rs`) routing verbs to the B0 `Transport` impls | BUILD (B0 merged) |
| T4 | Every `open`/`subscribe` ⇒ `validate_dial`; every `listen` ⇒ ingress check; all denials counted in the existing `…egress_denied_total{app_id,reason}` (reason codes unchanged) | BUILD (wires E8) |
| T5 | `kind: service` gate — `transport` (and `connector.*`) grants refused for `kind: app` at manifest validation AND at grant time | BUILD |
| T6 | `_derive_capabilities` (`bundle_approval_service.py` ~195): derive `transport` from permission, not from `egress` non-empty | BUILD |
| T7 | Tests: real-path e2e — service bundle dials a declared host through the real guard against a local TLS server; undeclared host/port/wildcard-evasion denied; reconnect re-validates (re-resolve each dial) | BUILD |
| — | Host `Transport` trait, `ConnectorDriver`, registry, executor `ConnectorHost` | EXISTS (B0, PR #844, merged) |

Ordering note: transport verbs depend on (a) B0 (merged), (b) `network` parse + catalog wiring (F2), (c) the penguin-libs enum + connection-library matcher (F1).

---

## 7. Subsystem: `kind: app | service`

### 7.1 Current state
No discriminator exists in any parser or manifest (verified: no `kind` key in `bundle_manifest_v2.py`, `app_manifest.py`, or any `bundle.yaml`/`hub-manifest.yaml`; `module`/`provider`/`is_default` are unrelated). "Service-like" behavior is inferred ad hoc (`waddles.core.*` namespace, `connector` WIT world).

### 7.2 Target
- `kind` is **REQUIRED** on every manifest (schema v3); values exactly `app` or `service`; anything else ⇒ `invalid_kind`. All 79 existing bundles migrate to `kind: app` in F3.
- **Both kinds carry `kind` and `network`; only app bundles carry `requires`.**
- Policy keyed on `kind`:

| Policy | `app` | `service` |
|---|---|---|
| Carries `kind` + `network` (required) | Yes | Yes |
| May declare `transport` / `connector.*` permissions | No (refused) | Yes |
| May declare `requires` | Yes (targets must be `service`) | No (refused — `requires_not_allowed_for_service`) |
| Valid `requires` target | No | Yes |
| Approval path | Normal 3-tier consent | Global-approval-only, first-party `waddles.core.*` for v3.0 (connector spec §3.2); vendor services deferred |
| WIT world | `waddle:bundle` stage worlds | `waddle:connector` world(s) |
| `network.ingress` expected non-empty | rarely | typically (webhook-family services) |

- `kind` is immutable per `app_id` across versions (a version cannot flip an existing app to a service); enforced at publish.
- Stored in `manifest_json`; an optional denormalized `app_catalog.kind` column (alembic, hub-api-owned — **not** a `config/postgres` SQL migration, which only replays on fresh DBs) is a listing/query convenience, not a source of truth.

### 7.3 Enforcement points / build scope
Parsers ×3 + `_reparse_trusted` (BUILD); publish-time immutability check (BUILD); catalog-id/kind gate (T5); `requires` target-kind check (R3); consent summary shows kind (BUILD); marketplace/UI separation of service bundles from app listings (BUILD, F2, minimal).

---

## 8. Phased build plan

```
B0 (#844, MERGED @ fcaffbbc) ─ transport base, no longer blocking
F1 penguin-libs ──► F2 waddles enforcement ──► F3 manifest migration ──► #840 platform connectors
   (root)             (parsers + teeth)          (79 manifests)             (Discord/Twitch/…)
```

### 8.1 F1 — penguin-libs (root, external)
**Connection library (company-wide, §4.6):** extract the portable network filter — FQDN / leftmost-label-wildcard / IP-CIDR matcher (with the `/24` v4 · `/120` v6 cap), PSL apex validation, DNS resolve-then-pin / SSRF guard, and the `network: {ingress, egress}` policy types — from `EgressGuard` into the penguin-libs connection library for all apps. Waddles keeps only bundle-specific wiring.
`rust-bundle-host`: structs for `kind`, `network {ingress,egress}`, `requires` (`schema_version: 3`); `CapabilityKind::Transport`; PSL validation (pinned offline `psl` crate); full SemVer comparison helper; shared conformance fixtures (incl. the §2.1 valid/invalid grammar table and CIDR-cap cases); CHANGELOG/version bump; release; then waddles bumps the `rev` in `bundle_executor`, `bundle_host_http`, `bundle_compiler` (reconcile the divergent `bundle_compiler` pin). **No waddles behaviour changes in F1** beyond the rev bump compiling cleanly (fields exist and validate in the Rust parser, which is the one parser that only the executor/compiler paths use — its enforcement hookup is F2). Per §3 the Rust parser's fields are enforced in the same release train as F2 — F1 and F2's Rust-consumer change must not be separated by a release that exposes parse-without-enforce in production.

### 8.2 F2 — waddles parsers + enforcement (needs F1)
Split into disjoint, parallelizable slices (own worktrees, disjoint files):

| Slice | Content | Depends |
|---|---|---|
| F2a | hub-api + flask_core parsers (`kind`, `network`, `requires`), `_reparse_trusted`, PSL (Python), shared conformance fixtures, consent summary + `classify_diff` + `permission_hash` | F1 fixtures |
| F2b | Egress: adopt the F1 connection-library matcher in `EgressGuard` (wildcard, public-CIDR, `/24` cap), `permission_outside_network`; **wire the dead catalogs** (active-set loader → `BundleCatalog`/`HttpEgressCatalog`); `validate_dial` port fix; e2e real-path test (declared host allowed / undeclared denied) | F2a manifest shape, F1 |
| F2c | `requires`: `_validate_requires` at both activation paths, reverse-dependency guard (5 paths, 409), full SemVer comparator, seeder service-first ordering + per-tenant service rows, data-plane defence in depth | F2a |
| F2d | Ingress gating in `svc_ingest` (declaration consumption, source-address check, metrics) | F2a, F2b catalog plumbing |
| F2e | Transport capability host impl (verbs → B0 `Transport`, `validate_dial` per dial, `listen` ⇒ ingress), `bundle_capability_gate` `Transport` kind + catalog id, `kind: service` gate | F1, F2a, F2b (B0 merged) |

Each slice ships parser + enforcement + docs + unit/regression/integration tests in the same PR (§3). The `network`-required rule is flipped **on** only together with F3 (see below) — until all manifests carry the block, the validation error would refuse every existing bundle; so F2 lands the *code* with the strict rule enabled in the same release train as F3, not behind a long-lived toggle.

### 8.3 F3 — manifest migration (needs F2a/F2b)
- Bump `schema_version` 2 → 3; add `kind: app`, `network: {egress: […], ingress: []}` (+ `requires` where applicable) to **all 79 `bundle.yaml`** and **77 `hub-manifest.yaml`**. Two bundles currently have no `hub-manifest.yaml`: `bundles/csharp/superpenguin-roll` and `bundles/javascript/example` — decide per bundle whether the second file is created or the bundle is excluded from seeding (they are not in the seeded set today).
- `network.egress` per bundle derived from its *actual* current surface: bundles with `egress: []` and no `net.http.*` ⇒ `egress: []`; bundles declaring hosts ⇒ those hosts. Migrate legacy `egress:` out of manifests (§4.3).
- Every manifest version bumps (the seeder is immutable per version and skips a same-version re-seed — a manifest change without a version bump is silently not applied; `seed_core_bundles.py` ~610 already names this). Both files stay byte-consistent for the new fields; the seeder asserts `bundle.yaml`/`hub-manifest.yaml` agree on `kind`/`network`/`requires` and **fails loud** on mismatch.
- A scripted migrator + a CI gate: `check-bundle-manifests` fails any `bundles/**` manifest missing `kind`/`network`, with a non-zero examined count (zero examined = failure, `critical-rules.md` Verification Integrity).
- Bundle READMEs updated per `docs-and-tests-every-dispatch`.

### 8.4 After F3: platform connectors (#840)
Discord/Twitch/Slack/Kick/YouTube service bundles (connector spec §5/§6 order) are authored natively against v-next: `kind: service`, `network.{egress,ingress}` populated, `transport` grant, apps `requires` them. Out of scope here.

### 8.5 Fail-closed / fail-loud / PII-free / vendor-tier summary

- **Fail-closed:** missing `network` or `kind` ⇒ refuse; no catalog row ⇒ deny; unsatisfied `requires` ⇒ 422; PSL unavailable ⇒ refuse to validate; ingress with no declared source match ⇒ reject before any parse; a service-bundle guard that cannot determine dependents ⇒ block the destructive action (409/500), never proceed.
- **Fail-loud:** seeder aborts on missing dependency/malformed network (and defensive cycle tripwire); dual-manifest mismatch is a hard error; denials are counted with bounded labels and logged at WARN/ERROR with type+message (never swallowed into `extra`-only fields).
- **PII-free:** manifests and denial telemetry carry app ids, rule families and reason codes only — no user identifiers, request URLs with queries, headers or bodies; raw source IPs appear only in the ingress denial *debug* log, never as metric labels or in spans' attributes beyond the bounded reason.
- **Vendor-tier stricter than first-party:** for non-`waddles.core.*` (vendor) bundles — wildcard egress entries and public CIDR entries require explicit admin re-consent shown as dangerous (the `/24` CIDR cap applies to vendors exactly as to first-party — no exception either way); **`kind: service` and `transport`/`connector.*` are not available to vendors in v3.0** (connector spec §3.2); `network.ingress` for a vendor app is limited to an operator-defined allowlist; a vendor `network.egress` may not be broader than a tenant-level egress denylist allows (existing tenant denylist, penguin-libs rule V21, applies to wildcard coverage too — a wildcard that *covers* a denylisted host is rejected, not just an exact match).

---

## 9. Open risks & questions

**Resolved (locked 2026-10-13):** former OPEN-1 (egress surfaces → §4.3), OPEN-2 (`schema_version` 2 → 3 → §2), OPEN-3 (CIDR cap `/24` universal → §2.1), OPEN-4 (service bundles seed per-tenant rows → §5.2), OPEN-5 (wildcards only in `network`; `net.http.fqdn` exact-host → §2.1/§4.3), OPEN-6 (IPv6 max block `/120`, confirmed → §2.1). Note: the locked wildcard decision supersedes connector spec §3.1's "no wildcards" for `network` entries only.

**Open:** none.

**Risks:**

| Risk | Note / mitigation |
|---|---|
| Egress engine has **never run against a real catalog row in prod** | Wiring the dead catalogs (E6) is the first real exercise — expect latent bugs. Gate F2b on a kind-e2e that proves allow + deny paths with real manifests, not mocks. |
| Parser drift across the 3 parsers | Shared conformance corpus (valid + invalid), run in all three test suites; any new field requires a fixture. |
| penguin-libs release train | F1 is an external repo cycle (PR → merge → rev). Pin rev everywhere in one waddles PR; `bundle_compiler`'s older rev must be reconciled or the compiler validates a different schema than the executor. |
| PSL snapshot drift | A PSL update can retroactively invalidate a stored wildcard. Pin the snapshot; on bump, re-validate all stored manifests and fail loud. Never fetch at runtime. |
| Ingress source identity is spoofable behind proxies | Peer address trusted only through configured proxy hops (Cloudflare/LB ranges); never trust raw `X-Forwarded-For`. Domain ingress entries matched by resolved address, bounded TTL. |
| Reverse-dependency guard races | Guard and mutation must be in one transaction (hub-api already uses `engine.begin()` for activation atomicity); a concurrent dependent activation during a service deactivate must not slip through. |
| `app_versions` immutability vs. manifest fixes | Immutable seeder skips same-version re-seed; every manifest edit in F3 needs a version bump or the new fields are silently not applied. |
| Alembic vs `config/postgres` migrations | Any new column (`app_catalog.kind`, `ActiveBundleRow` network) must be an alembic revision (≤32-char ids, single head); `config/postgres` SQL migrations only replay on fresh DBs and would never reach alpha/beta. |
| Connection-library extraction scope | Extracting the matcher/DNS-pin out of `egress.rs` risks behaviour drift in the one engine that has never run on a real row; move the existing `egress.rs` tests with the code and keep them green against the library before any new grammar is added. |
| `/24` cap breaks broad existing allowlists | Any manifest currently relying on a wider CIDR (or a private `/16`) must be re-expressed as ≤256-address blocks in F3; the validator rejects, never truncates. Audit existing manifests/grants before F3 flips the strict rule. |
| Consent churn | Adding `network` to the summary/hash makes every existing approval look "widened" if the summary shape changes unexpectedly — F3's migration must produce a `network` block equal to the already-consented egress so `classify_diff` returns `unchanged` for unchanged surfaces (test it). |

---

## 10. Appendix — verified anchors (release/v3.0.X @ 80bb66b7)

| Anchor | Location | Verified |
|---|---|---|
| `EgressGuard::send` / `host_not_declared` | `core/bundle_host_http/src/egress.rs` 795 / 967 | yes |
| `validate_dial` (0 external callers; no `allowed_ports` check) | `egress.rs` 1238 (checked impl 1262) | yes |
| `host_matches` (exact only) | `egress.rs` 1388 | yes |
| DNS resolver | `TokioResolver` `egress.rs` 2146 | yes |
| `is_forbidden_address` | `egress.rs` 1515 | yes |
| Public-IP exact-only / private CIDR | `core/egress_assertion/src/lib.rs` 437 | yes |
| Wildcard + public-CIDR rejection (catalog), private floor | `core/bundle_capability_gate/src/permission.rs` ~14, ~880, ~903 | yes |
| Dead catalogs | `core/svc_action/src/lib.rs:265`, `core/svc_process/src/lib.rs:1089` | yes |
| `wasi:sockets` denied | `core/bundle_executor/src/host/mod.rs` ~242 | yes |
| EventSub HMAC + 600s replay | `core/svc_ingest/src/ingest/twitch_eventsub.rs` ~64 | yes |
| `routes_to` precedent | `hub_api/services/bundle_approval_service.py` 260 (calls 949, 1076) | yes |
| Activation functions | same file 879 (`activate_tenant_wide`), 1013 (`activate_for_community`) | yes |
| Uninstall / deactivate | same file 525, 1156, 1210 | yes |
| SemVer regex / comparator | `bundle_manifest_v2.py` 44 / `flask_core/platform_version.py` 60 | yes |
| Seeder | `hub_api/cli/seed_core_bundles.py` 729 (`seed_one`), 1249 (loop) | yes |
| Rust `Manifest` + wire `CapabilityKind` | penguin-libs `packages/rust-bundle-host` @ `b063b7ba` | yes |
| Manifest counts | 79 `bundle.yaml`, 77 `hub-manifest.yaml` | yes |

# Bundle Permissions & Capability Gate — Design

**Status:** Proposed design (no code in this doc) — the umbrella other capability specs plug into.
**Date:** 2026-09-28
**Scope:** `wit/waddle-bundle/stage.wit`, `core/svc_process`, `core/svc_action`, new `core/bundle_capability_gate` crate, `core/svc_ingest` (actor/mention tokenization), `core/browser_source_core_module`, `hub_api/services/bundle_manifest_v2.py`, `hub_api/services/bundle_approval_service.py`, `hub_api/services/permission_summary_service.py`, `hub_api/services/marketplace_lifecycle_service.py`, `hub_api/services/vendor_bundle_authz.py`, `hub_api/services/ai_routing/` (PII redaction reuse), `action/interactive/ai_interaction_module` (prompt-safety reuse), new hub-webui consent screens, `core/reputation_module`.
**Driver:** Justin's requirement — bundles get their own tables/object storage and the ability to move community/tenant reputation, but only after an Android-style permission request reviewed at each of the 3 lifecycle tiers, with one standard gate every host import calls to check/allow/validate that the call is app-bundle-scoped or reputation-scoped and that the bundle actually holds the grant. Extended mid-design with the `!vso`/`!aiso` shoutout requirement (`overlay.media`, `ai.generate`) and a hard PII-tokenization invariant covering every direction data crosses the bundle boundary.
**Builds on (read, reconciled, not re-litigated):**
- `wit/waddle-bundle/stage.wit` (v1.0, live) — 8 `CapabilityKind` variants (`Context, Http, Kv, Db, Relay, Flags, Log, Clock`), each "always granted" or gated on a manifest shape signal (e.g. `db` on non-empty `data.tables`). **`db`/`kv` are hardcoded `denied` today, unconditionally** (`core/svc_process/src/capabilities.rs:221-228`, `core/svc_action/src/capabilities.rs:582-589`) — no enforcement gate exists to wire them into. The existing `http::request.secret-refs: list<tuple<string,string>>` field (stage.wit:89, "the stage resolves the reference and injects the header; the secret value never enters the component") is reused as-is by this spec's `net.http.*` permission families — no new mechanism needed for static-key injection.
- `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` (APPROVED-WITH-CONDITIONS) — adds manifest `capabilities: [scheduled, moderation, db-batch]`, per-component `Linker` registering only a component's declared imports (§5), and `moderation`'s action-stage-only + relay-authz-gated + rate-limit-before-call + destructive-ops-bypass-cache pattern (§2, §7). **This spec's permission catalog subsumes `capabilities:`** — see §2.4.
- `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md` (PR #415, `docs/bundle-db-capability-design`, commit `e35c2db0` + round-3 conditions) — wires `db.execute`/`execute-batch`, per-app Postgres schema (`app_core.<app_id>`/`app_community.<app_id>` in the shared `waddles` database) + schema-scoped runtime role + RLS, a manifest DSL→DDL schema compiler already emitting exactly one typed table per bundle at onboarding, quotas, migrations, uninstall deletion, backups. **This spec's `storage.tables` permission is the grant that authorizes exactly what PR #415 provisions** — not a competing design; §1.4 below references PR #415's already-finalized one-table/schema-naming convention rather than redefining it.
- `docs/superpowers/specs/2026-09-28-superpenguin-fish-game-port.md` (PR #414) — a real bundle needing `storage.kv`+`storage.tables`; explicitly flags that no capability exists for a bundle to move platform points, recommending a future capability "mirroring how `moderation` was added — new WIT interface, new `CapabilityKind` variant, action-stage-only, rate-limited, audited." **This spec's `reputation.*` follows that exact template.** Its table design (`user_ref uuid, fish_caught text, score integer, caught_at timestamptz`) is this spec's first real consumer of both §1.4's single-table rule and §10.5's `user_ref` erasure-cascade rule.
- `feature/bundle-kv-capability` — fetched; **zero commits ahead of `release/v3.0.X` today** (tip is an unrelated merge). No implementation to reconcile yet; §12 hands it concrete tasks against this design.
- Existing hub-api consent machinery, already live: `hub_api/services/permission_summary_service.py` (`build_permission_summary`/`permission_hash`/`canonical_json`) and `bundle_approval_service.classify_diff` (`initial|widened|narrowed|unchanged`) already do install-time summarization and upgrade diffing — **today only over the 8 derived `CapabilityKind` names, egress hosts, tables, and `routes_to`.** This spec extends that same machinery to the full permission catalog (§2) rather than inventing a parallel one.
- The real 3-tier scope gates, confirmed in code: `platform:admin` on `app_catalog` install/uninstall + vendor-version approve/deny (`hub_api/blueprints/v1/marketplace_lifecycle.py:370,387`, `bundle_approvals.py:81,99,140`); `tenant:admin` on `app_tenant_availability` make/unavailable (`marketplace_lifecycle.py:440,464`); community-scoped activate/deactivate on `app_activations` (`marketplace_lifecycle.py:522,556`). Invariant: `activated <= available <= installed`.
- `hub_api/cli/seed_core_bundles.py` — the existing system-actor pattern (`approved_by=None`, `approval_source="system:core-seeder"`, hard-guarded to `waddles.core.*` via `services/vendor_bundle_authz.py::CORE_NAMESPACE_PREFIX`) this spec reuses verbatim for core-bundle permission pre-grant (§3.6) **and** for the `storage.tables` schema derivation (§1.4) — a vendor bundle can never reach the seeder path with a `waddles.core.*` app_id, so it can never earn an `app_core` schema.
- Existing reputation system: `core/reputation_module` (gRPC `ReputationService.RecordEvent`/`GetScore`, `reputation_service.py::adjust()` the sole write path, score band 300-850 default 600, `REPUTATION_AUTO_BAN_THRESHOLD=450`), `hub_api/services/community_reputation_service.py` (read-only, two scores: `community_members.reputation` and `reputation_global.score`), `core/svc_process/builtin_handlers/community_reputation_process.py` (the legacy native `!rep` bundle, untouched by this design). **No capability today lets a WASM bundle read or write either score.**
- Existing overlay transport: `core/browser_source_core_module`'s `BrowserSourceService.SendOverlayEvent(token, community_id, event_type, event_data)` gRPC RPC (`libs/grpc_protos/browser_source.proto:21`) — the substrate `overlay.media` (§8) is built on, no new transport.
- Existing WaddleAI integration pattern: `action/interactive/ai_interaction_module/services/waddleai_provider.py` + `services/prompt_safety.py` (`UNTRUSTED_DATA_NOTICE`/`wrap_untrusted` prompt-injection delimiting) and `hub_api/services/ai_routing/pii_redaction.py` (pattern-based PII rejection for BYOK traffic) — `ai.generate` (§9) generalizes this Python-service-local pattern into a host capability rather than duplicating it.
- `docs/bundle-telemetry-capability` (branch) — owns the mechanics behind `telemetry.logs`/`telemetry.metrics` (§1): declared instrument set, label-cardinality caps, PII scrubbing, rate limits, OTel conversion. This spec only adds the two catalog entries and routes their host calls through the same `authorize()` gate (§5) — not a competing design.
- **A live PII gap this spec closes, not just documents:** `core/svc_ingest/src/normalize.rs:150,185,263` populates `platform-event.actor` with the **raw platform username/login** (`msg.sender`, `msg.author_username`, `user_login`) today — every existing process-stage bundle already receives PII in a field this design assumes is a UUID. §10 makes fixing this a prerequisite, not a follow-up.

---

## 0. Gemini review resolution (PASS-WITH-CONDITIONS)

Gemini reviewed PR #419 and returned PASS-WITH-CONDITIONS. Every condition below is folded into this spec as a **normative requirement**, not a suggestion — none are optional follow-ups.

| # | Condition | Resolved in |
|---|---|---|
| 1 | Confused deputy: `InvokeScope` must be host-constructed only, from the immutable execution context, never guest-influenced — the guest API must have no scope parameters at all. Requires a test. | §5.1 (new subsection); test requirement in §5.1 |
| 2 | Revocation TOCTOU: a 300s poll alone leaves too wide a window. Push-based invalidation (Valkey stream/pub-sub) required, <1s; poll stays only as fallback. In-flight calls finish, but a revoke blocks further host calls within the same invocation. | §4 (grant storage, rewritten), §5.3 (defense in depth), §5.5 (performance budget) |
| 3 | PII: `core/svc_ingest/src/normalize.rs` puts raw usernames/logins in `platform-event.actor` today — a live leak, HARD BLOCKER. Must be Phase 1, Task 1. | §10.1 (hard invariant, strengthened), §12 Phase 1 Task 1 |
| 4 | Reputation: a global per-bundle and per-publisher daily cap across ALL communities, plus distribution/entropy anomaly detection, with an auto-suspend threshold notifying the global admin. | §7.3 (anti-abuse controls, expanded) |
| 5 | Detokenizer: single-pass, non-recursive; strict placeholder grammar; brace-sequence escaping before tokenizing; per-sink HTML escaping; overlay renders in a sandboxed iframe (`sandbox="allow-scripts"`, no `allow-same-origin`) under a strict CSP. | §10.4 (output detokenization, hardened) |
| 6 | Upgrades: auto-upgrade hard-blocked on ANY permission delta (added or broadened); re-consent can't be bypassed, not even by a tenant default. | §3.4 (version upgrade, strengthened) |
| 7 | `storage.objects`: a per-tenant bucket for Enterprise; shared bucket + prefixes for other tiers; per-tenant rate/IOPS limits; per-tenant SSE keys. | §6 (object storage, tiered) |
| 8 | WASM limits are already implemented (epoch deadline + memory limiter via `new_bounded_store`, PR #406) — reference it, don't redesign it. | §5.3 (defense in depth, referenced) |
| 9 | Artifact signing: hub-api signs the approved component digest (Ed25519, platform KMS key) at approval; the executor verifies against the platform public key before instantiating; fail closed. Vendor-side signing is optional and additional. | §5.6 (new subsection) |

---

## 1. Permission catalog

Android-style: every permission has a stable id, a risk level, and a default quota. `normal` permissions are shown but not blocked on; `dangerous` permissions require explicit reviewer action at every tier that sees them for the first time (§3).

| id | Risk | Default quota | `CapabilityKind` | Notes |
|---|---|---|---|---|
| `storage.kv` | normal | 64 KiB/value, 10k keys/app, `KV_MAX_TTL_S` | `Kv` | Bundle's own `...:state` hash — always-available shape, but the *grant* is now explicit, not implicit ("always granted" retired, see §2.4) |
| `storage.tables` | normal | 100k rows / 50 MB per (tenant, app) — PR #415 §6 | `Db` | **Exactly one table, in a server-derived schema (§1.4) — never bundle-chosen.** Requires `data.schema` (PR #415's DSL); table validated by existing `_TABLE_RE`/`_RESERVED_TABLES`; schema is `app_core.<app_id>`/`app_community.<app_id>` per PR #415 (commit `e35c2db0`, round-3) |
| `storage.objects` | normal | 1k objects / 500 MB per (tenant, app) | `Objects` *(new)* | §6 |
| `net.http.fqdn:<host>` | normal | 10 rps, 1 MB response | `Http` | **Preferred form.** One permission id per allowlisted hostname, e.g. `net.http.fqdn:api.example.com` — no wildcards, no IP literals (those go through the two families below instead). Static-key auth uses the existing `secret-refs` header-injection field (stage.wit:89); OAuth client-credentials (e.g. Kick) go through the connections credential broker instead (§2.2.1) |
| `net.http.public-ip:<ip>` | dangerous | 10 rps, 1 MB response | `Http` | A bare, globally-routable IP with no FQDN — never a CIDR. Higher risk than `net.http.fqdn`: opaque, rebinding/cache-poisoning-prone, harder to audit. See §1.2 |
| `net.http.private-ip:<ip\|cidr>` | dangerous | 10 rps, 1 MB response | `Http` | A private IP or CIDR, prefix no coarser than /16 (v4)/64 (v6). **Deny-by-default at the instance-policy layer** (§1.3) — opt-in only, exceptional self-hosted/on-prem case. See §1.2 |
| `chat.send:<platform>` | normal | existing `relay` rate limits (`UsageBatcher`) | `Relay` | `<platform>` ∈ compiled-in providers (`twitch`, `discord`, ...); action-stage only, unchanged from today. Outbound text is placeholder-only — see §10.4 |
| `moderation.<platform>` | dangerous | existing relay-authz + `UsageBatcher` (wit-v1.1 §2) | `Moderation` *(v1.1)* | Subsumes v1.1's bare `moderation` capability flag — same enforcement, now catalog-typed per platform |
| `overlay.media` | dangerous | 1 item/10s/app/community, ≤30s duration | `Overlay` *(new)* | §8 — answers the `!vso` shoutout requirement. Content is placeholder-only where it carries user-facing text — see §10.4 |
| `ai.generate` | dangerous, **Enterprise-gated** | 2k prompt tokens / 512 completion tokens / 10 calls-min | `Ai` *(new)* | §9 — answers the `!aiso` shoutout requirement |
| `users.profile.read` | normal | unlimited reads | `Users` *(new)* | §10.2 — non-identifying attributes only; **ReputationScoped-style** membership check (target UUID must belong to the invocation's tenant/community) |
| `telemetry.logs` | normal | declared instruments only, per-instrument rate limit | `Telemetry` *(new)* | Declared in the manifest, **granted by default** (normal risk, like `flags.read`) — instrument set, label-cardinality caps, PII scrubbing, rate limits, and OTel conversion are designed in `docs/bundle-telemetry-capability` (branch), not duplicated here. Every telemetry host call still goes through this spec's `authorize()` gate (§5) like any other capability |
| `telemetry.metrics` | normal | declared instruments only, per-instrument rate limit | `Telemetry` *(new)* | Same as `telemetry.logs` — same branch owns the mechanics, this catalog entry is only the grant |
| `reputation.read` | normal | unlimited reads | `Reputation` *(new)* | Own-scope + community/tenant leaderboard reads only — never cross-tenant |
| `reputation.community.write` | dangerous | ±5/user/day, ±50/community/day aggregate | `Reputation` *(new)* | §7 |
| `reputation.tenant.write` | dangerous | ±5/user/day, ±200/tenant/day aggregate | `Reputation` *(new)* | §7; strictly a superset grant of `reputation.community.write`'s bound, never looser per-call |
| `flags.read` | normal | unlimited | `Flags` | Was "always granted" — now explicit, still auto-approved (§3.5) |
| `platform.scheduled` | normal | 60s floor interval, existing tenant quota (wit-v1.1 §1) | *(none — hub-api CRUD only)* | Subsumes v1.1's bare `scheduled` capability flag |
| `platform.context` / `platform.clock` / `platform.log` | normal | n/a | `Context`/`Clock`/`Log` | Always-granted, zero-config; listed for completeness of the enforcement gate's exhaustive match, never shown on a consent screen (§11) |
| `interaction.pii.receive` | dangerous | n/a | `Interaction` *(new)* | Bundles can receive form/modal/interaction inputs, but a raw PII value inside one of those inputs (e.g. a free-text field a user typed a name/email/phone into) is delivered to the bundle **only** if it requested and was granted this permission — DEFAULT NO, same instance-wide deny-policy applicability as any other family (§1.3). Without the grant, the host filters PII out of interaction inputs before delivery; that filter is **best-effort**, not a guaranteed scrub, and its implementation is separate work from this catalog entry. Distinct from §10's actor/target UUID tokenization — this covers PII a *user themselves types into an input*, not the platform-supplied identity already covered by §10.1 |

**Risk-level rule:** `dangerous` = crosses a trust boundary this platform otherwise protects by construction (talks to the open internet, renders on a public stream overlay, calls a licensed AI service, mutates another user's platform-visible reputation/moderation state, or bundle-approval-scoped destructive schema changes per PR #415 §7). Everything AppScoped and contained to the bundle's own data is `normal`. **Actor/target user identity is not a permission at all** — every delivered event already carries the acting and (where applicable) targeted user as a tenant-tokenized UUID, at zero risk level, since a UUID alone is not identifying (§10.1).

### 1.1 Outbound HTTP: three separate families, and what's always denied (2026-09-28 decision)

**Bundles should never see or target private IP addresses.** Outbound HTTP is split into three separately-approvable permission families, never one bare `net.http:<host>`, so a reviewer/admin sees exactly what shape of network access a bundle is asking for:

| Family | Risk | Use |
|---|---|---|
| `net.http.fqdn:<host>` | normal | **The preferred form, always.** A bundle should name a stable public hostname wherever one exists. No wildcards, no IP literals — either of those routes to one of the two families below instead, at `dangerous` risk. |
| `net.http.public-ip:<ip>` | dangerous | A single, globally-routable IP with no FQDN. High risk — opaque, rebinding/cache-poisoning-prone, harder to audit than a hostname — even though the address itself is publicly routable. |
| `net.http.private-ip:<ip\|cidr>` | dangerous | Exists **only** for the exceptional self-hosted/on-prem case where a bundle must reach a private network the tenant itself operates. Requires its own separate, explicit approval at every tier (§3) and is **deny-by-default at the instance-policy layer** (§1.3) — a global admin must opt in before any tenant/community can even request it. **Reviewers and admins should treat any `net.http.private-ip` request as a red flag** and confirm the justification names a real, specific, tenant-operated destination before approving. |

**Always denied, unconditionally, regardless of family or any grant/approval** (checked at manifest-parse time, grant time, and again at `authorize()` on the hot path, §5.3):

- Loopback (`127.0.0.0/8`, `::1/128`)
- Link-local and cloud-metadata addresses (`169.254.0.0/16`, `fe80::/10`, `169.254.169.254`, AWS IMDSv2's `fd00:ec2::254`)
- This cluster's own pod, service, and node CIDRs (operator-configured per deployment — never hardcoded, never a manifest-supplied override)

`net.http.private-ip` additionally bounds a CIDR grant to no coarser than `/16` (v4) or `/64` (v6) — an operator should never need to hand a single bundle a whole `/8`.

### 1.2 `net.http.public-ip`/`net.http.private-ip` are checked in the same places `net.http.fqdn` is

Both `params.methods` validation (§2.2) and the `secret-refs`/credential-broker mechanics (§2.2.1) apply identically across all three families — the split is a risk/consent distinction, not a different wire mechanism. A manifest declaring `net.http.public-ip`/`net.http.private-ip` gets an advisory warning logged at parse time recommending `net.http.fqdn` instead, even when the request is otherwise valid and ultimately approved.

### 1.3 Instance policy: a fourth layer, above the 3 consent tiers (2026-09-28 decision)

A **global admin** (`platform:admin`) can allow/deny an entire permission **type** — a catalog id or family, e.g. `net.http.private-ip`, `storage.objects`, `reputation.tenant.write` — **instance-wide**, applying to every bundle, tenant, and community on this deployment regardless of its own per-app approval. This sits *above* the existing 3 tiers, not alongside them:

```
INSTANCE POLICY  (global admin, platform:admin — applies to every tenant/community)
      │
      ▼
GLOBAL catalog approval  (§3.1)
      │
      ▼
TENANT restriction  (§3.2)
      │
      ▼
COMMUNITY grant  (§3.3)
```

- **`net.http.private-ip` is deny-by-default** — seeded at migration time, opt-in only. Every other permission type defaults to `allow` at this layer (the existing 3 tiers still gate them normally).
- While a permission type is instance-denied: a manifest requesting it is rejected at onboarding (or flagged unapprovable) before it ever reaches GLOBAL catalog approval; a TENANT/COMMUNITY grant for it is impossible to create.
- **Enabling a deny on a type that was previously allowed cascades**: every existing active COMMUNITY grant matching that type is revoked, in the same transaction as the policy change itself — a grant-version bump plus a push-invalidation publish, identical in shape to a manual per-permission revoke (§3.7).
- `core/bundle_capability_gate`'s `authorize()` also checks the instance policy directly, on every call, **even when an active grant row still exists** — defense in depth against the cascade revoke not having landed yet (the same fail-closed posture as a `GrantSnapshot` cache miss, §5.3).
- An optional `param_scope` can narrow a policy to one parameter value (e.g. a single CIDR) — a secondary, best-effort refinement; the primary and only mandatory key is the permission **type** itself, never a per-bundle or per-admission decision.

### 1.4 `storage.tables`: one table per bundle, in a server-derived schema

**Resolved by PR #415 directly (commit `e35c2db0`, round-3 conditions) — not redesigned here, only referenced.** A bundle gets **exactly one** Postgres table, with hub-api-compiled DDL (typed columns from the manifest DSL) applied during onboarding, against a schema-scoped runtime role — see PR #415 for the DDL/role mechanics. The table lives in a Postgres **schema** in the shared `waddles` database, named server-side — never taken from the manifest's own claim:

```
schema = "app_core"      if app_id starts with vendor_bundle_authz.CORE_NAMESPACE_PREFIX ("waddles.core.")
       = "app_community" otherwise
table  = f"{schema}.{sanitized_app_id}"   # e.g. app_core.waddles_core_example_echo
```

`CORE_NAMESPACE_PREFIX` (`hub_api/services/vendor_bundle_authz.py:56`) is already enforced at submission — `bundle_approval_service.py:464` refuses any app_id outside `waddles.core.*` on the core-seeder path, and the inverse (a vendor claiming a `waddles.core.*` id) is refused on the vendor-submission path. A vendor bundle can therefore never reach the `app_core` schema; this spec adds no new namespace check, only a new *consumer* of the existing one. `sanitized_app_id` follows PR #415's own identifier-sanitization rule (dots/other non-identifier characters normalized, truncated with a hash suffix past Postgres's 63-byte `NAMEDATALEN` cap where needed) — not redefined here.

No revision to PR #415 is needed for this spec's one-table rule: its round-3 conditions already specify exactly one table per bundle, typed columns, hub-api DDL at onboarding, and a schema-scoped runtime role. PR #414's fishing bundle (`user_ref uuid, fish_caught text, score integer, caught_at timestamptz`) is this rule's first real consumer — its `user_ref` column is validated per §10.5.

---

## 2. Manifest declaration

### 2.1 Format

New manifest v2 top-level block, replacing the dead free-form `permissions: []` (§2.4) and `data.tables`'s implicit `db` grant:

```yaml
permissions:
  - id: storage.tables
    justification: "Stores each cast's fish, weight, and gold for the player's inventory."
    params:
      schema: data/schema.yaml          # PR #415 DSL file, relative to bundle root
  - id: storage.objects
    justification: "Stores the seasonal leaderboard banner image players can upload."
    params:
      max_object_bytes: 2097152
      max_total_bytes: 52428800
      content_types: ["image/png", "image/jpeg"]
  - id: net.http.fqdn:api.weatherapi.example.com
    justification: "Looks up real-world weather to theme the fishing spot."
    params:
      methods: ["GET"]
  - id: chat.send:discord
    justification: "Announces rare catches in the channel."
  - id: overlay.media
    justification: "Plays a short celebration clip when a viewer catches a rare fish (!vso)."
    params:
      allowed_hosts: ["www.youtube.com", "kick.com"]
      max_duration_seconds: 30
  - id: ai.generate
    justification: "Generates a personalized AI shoutout line (!aiso)."
  - id: users.profile.read
    justification: "Checks whether the caster is a subscriber to boost catch odds."
  - id: reputation.community.write
    justification: "Rewards consistent players with +1 community reputation per legendary catch, capped daily."
    params:
      delta_min: -1
      delta_max: 1
      reason_codes: ["fishing.legendary_catch"]
```

### 2.2 Per-permission `params`

| Permission | Required `params` | Validated by |
|---|---|---|
| `storage.tables` | `schema` (path to PR #415 DSL doc) | hub-api schema compiler (PR #415 §4), now one-table-only (§1.4) |
| `storage.objects` | `max_object_bytes`, `max_total_bytes`, `content_types[]` | New `_ALLOWED_CONTENT_TYPES` allowlist, both bytes fields ≤ catalog default ceiling (§1) |
| `net.http.fqdn:<host>` / `net.http.public-ip:<ip>` / `net.http.private-ip:<ip\|cidr>` | `methods[]` | Existing `_ALLOWED_METHODS`; per-family host/IP/CIDR validation in `bundle_permission_catalog.py` (§1.1) |
| `chat.send:<platform>` | none | Existing `RELAY_PROVIDERS` allowlist |
| `overlay.media` | `allowed_hosts[]`, `max_duration_seconds` | New `_EGRESS_HOST_RE` reuse (§2.2.1) + catalog ceiling (30s) |
| `ai.generate` | none | Enterprise-tier check at tenant-marketplace time (§3.2), not manifest-parse time |
| `users.profile.read` | none | — |
| `reputation.community.write` / `reputation.tenant.write` | `delta_min`, `delta_max` (integers, `|delta| <= 5`), `reason_codes[]` (non-empty) | New validator: bounds inside the catalog ceiling (§1), every `reason_codes` entry `[a-z][a-z0-9_.]*` |
| `reputation.read` | none | — |
| `flags.read` | none | — |

`justification` is mandatory on every entry, 1-280 chars, plain text (no markdown/HTML) — the string a human reviewer and the community-admin consent screen both render verbatim (§11).

#### 2.2.1 Credential injection for `net.http.*` / `overlay.media`

A platform API needing a **static key** (e.g. the YouTube Data API's `X-Goog-Api-Key` header, for `!vso`'s clip lookup) is injected via the existing `secret-refs` field (`http::request.secret-refs: list<tuple<string,string>>`, stage.wit:89) — identical across all three `net.http.*` families — the manifest names the header, hub-api's secret store holds the value, the guest never sees it. A platform API needing **OAuth client-credentials** (e.g. Kick) instead resolves through the `2026-09-28-connections-credentials-design.md` credential broker — a `net.http.*` grant covers static header injection only, never a bundle-visible OAuth token.

### 2.3 Validation additions to `bundle_manifest_v2.py`

- New `_PERMISSION_ID_RE` accepting the catalog's static ids plus the parameterized families (`net.http.fqdn:<host>` reuses `_EGRESS_HOST_RE` minus wildcards; `net.http.public-ip:<ip>`/`net.http.private-ip:<ip|cidr>` use dedicated IP/CIDR validators, §1.1; `chat.send:<platform>`/`moderation.<platform>` reuse `RELAY_PROVIDERS`).
- Logs an advisory warning (never a hard rejection) recommending `net.http.fqdn` whenever a manifest declares `net.http.public-ip`/`net.http.private-ip` (§1.2).
- Reject an unknown permission id outright (`unknown_permission`) — the catalog is closed, not extensible per-bundle.
- Reject `storage.tables` with no `data.schema` and vice versa (mutual requirement, one source of truth — `data.tables` as a bare table-name list is deprecated, see §2.4).
- Reject `overlay.media` with an empty `allowed_hosts` or a `max_duration_seconds` above the catalog ceiling.
- Reject any `dangerous`-risk permission with an empty `justification`.

### 2.4 Reconciling three existing manifest fields into one

| Existing field | Status | Disposition |
|---|---|---|
| `permissions: []` (`bundle_manifest_v2.py:102`) | Parsed, never validated, never enforced (dead) | **Becomes this spec's structured block** (§2.1) — same field name, real schema |
| `capabilities: [scheduled, moderation, db-batch]` (wit-v1.1 §4, not yet merged) | Proposed, not implemented | **Folded into `permissions:`** as `platform.scheduled` / `moderation.<platform>` / an implicit quota bump on `storage.tables` (batch is a quota dimension, not a separate grant) — land as part of the same PR that merges wit-v1.1, never ship both fields |
| `data.tables: []` (live) | Table-name allowlist only, no schema | **Retained as the DSL's own table-name list** (PR #415 §4.3 already reuses `_TABLE_RE`/`_RESERVED_TABLES`) — `storage.tables`'s grant is what makes `data.tables` reachable at all now that `db` is no longer implicitly granted |

`_derive_capabilities()` (`bundle_approval_service.py:92-101`) — which infers `http`/`db`/`relay` from manifest shape — is retired once permissions are explicit; the consent summary reads `permissions:` directly instead of re-deriving it.

---

## 3. Consent flow across the 3 tiers

```
vendor SUBMITS ──▶ GLOBAL admin (platform:admin)   reviews permission catalog at catalog approval
                          │  app_catalog + app_install_approvals
                          ▼
                   TENANT admin (tenant:admin)      sees requested permissions in the marketplace,
                          │  app_tenant_availability  MAY RESTRICT (narrow, never widen) before making available
                          ▼
                   COMMUNITY admin                  grants at activation (the install prompt) —
                          │  app_activations           Android-style permission list, dangerous ones highlighted
                          ▼
                     bundle runs, gate enforces the community's actual grant set
```

### 3.1 Global admin — catalog approval

- `bundle_approvals.py::post_approve` (`platform:admin`) already computes `get_permission_summary()`/`permission_hash()`. Extend `build_permission_summary()` (§4 of `permission_summary_service.py`) to render the full catalog (§1) instead of the 8 derived capability names.
- New required review step: every `dangerous` permission must be individually acknowledged (`approved_permissions: ["net.http.public-ip:...", "overlay.media", "ai.generate", "reputation.community.write"]` on the approve request) — approving the version without listing every dangerous id it requests is refused (`incomplete_dangerous_ack`, 422), mirroring PR #415's `destructive_schema_change_requires_ack` posture.
- Checked first, before the ack requirement above: any permission whose *type* is instance-denied (§1.3, e.g. `net.http.private-ip` on an instance that hasn't opted in) is rejected outright (`instance_denied_permission`, 403) — a global admin cannot approve past an instance-wide deny from this endpoint; the instance policy must be changed first.
- Approval writes the **maximal grant set** for the app version into a new `app_permission_requests` table (§4) — this is the ceiling every lower tier can only narrow.

### 3.2 Tenant admin — marketplace restriction

- `make_available()` (`tenant:admin`) gains an optional `restricted_permissions: []` — any subset of the catalog-approved set the tenant admin chooses to **exclude** tenant-wide. Cannot add a permission the global admin didn't approve (rejected `permission_not_in_catalog_grant`).
- `ai.generate` gets an additional, automatic restriction: `make_available()` denies it outright unless `get_tier(tenant) == "enterprise"` (§9) — a Professional tenant never sees it offered, independent of any manual toggle.
- Marketplace listing (hub-webui) shows the full requested list with the tenant's current restrictions pre-checked-off, same Android "toggle off what you don't want your users to have" framing, but restriction is opt-out per-permission, not per-install.
- Writes `app_tenant_availability`'s new `restricted_permission_ids` column (§4).

### 3.3 Community admin — activation (install prompt)

- `activate_bundle()` gains a mandatory `granted_permissions: []` — the community admin's actual consent, a subset of (catalog-approved minus tenant-restricted). Missing any permission the bundle's manifest lists as required (i.e. not opportunistically-used) blocks activation with the specific missing ids (`consent_required`, 422) — never a silent partial-grant activation.
- Every `dangerous` permission renders on its own line with its `justification`, exactly the Android permission-request screen shape (§11) — no "select all" for the dangerous tier.
- Writes `community_permission_grants` (§4) — the row the data plane actually reads at runtime.

### 3.4 Version upgrade requiring new/broader permissions

- `classify_diff()` (already live, `bundle_approval_service.py:253-278`) is extended to diff the full permission catalog, not just the 8 capability names + egress/tables/routes.
- `widened` (new permission id, or a `params` change that raises a bound — e.g. `delta_max` increases, a quota increases, a new `net.http.fqdn`/`net.http.public-ip`/`net.http.private-ip`/`overlay.media` host) **requires the same 3-tier re-consent as a first install**: the global admin re-approves the new/changed dangerous permissions, and every tenant/community that had the prior version active **stays pinned to the prior version** until each tier re-consents at its own level — never auto-upgraded.
- **Hard-blocked on ANY permission delta, `normal` or `dangerous` (Gemini condition 6).** Auto-upgrade is refused the moment `classify_diff()` reports anything other than `narrowed`/`unchanged` — an *added* permission blocks exactly like a *broadened* one, and the block **cannot be bypassed by a tenant-level default or blanket-approval setting** (e.g. a tenant admin's "auto-approve minor updates" convenience toggle, if one ever exists for non-permission-affecting changes, has no effect here). Re-consent is always an explicit, per-version, per-tier action.
- `narrowed`/`unchanged` upgrades auto-apply at the community's next poll cycle (§4) — no re-consent needed, consistent with today's non-permission upgrade behavior.
- A community that never re-consents simply never receives the new version — `app_active_versions` keeps pointing at the last-consented `version_id`, exactly the existing FK-pointer mechanism, no new state machine.

### 3.5 Auto-approved (non-`dangerous`) permissions

`normal`-risk permissions (`storage.kv`, `storage.tables`, `storage.objects` within default quota, `chat.send:<platform>`, `users.profile.read`, `telemetry.logs`, `telemetry.metrics`, `reputation.read`, `flags.read`, `platform.*`) still appear on every consent screen for transparency (Android shows all permissions, not just dangerous ones) but never block approval/activation on an explicit per-id ack — only the aggregate "I've seen this list" click each tier already performs today.

### 3.6 Core `waddles.core.*` bundles

**Pre-granted, not exempted.** `seed_core_bundles.py` calls `approve_version(approved_by=None, approval_source="system:core-seeder")` — extended to also write `app_permission_requests` with `approved_by=None`/`approval_source="system:core-seeder"` for every permission the core bundle's manifest declares, still hard-guarded to `CORE_NAMESPACE_PREFIX` before any DB write. Tenant/community tiers still see the same permission list on their own consent screens (a core bundle is still individually activated per community, same as today) — only the **global catalog-approval step** is system-automated; tenant restriction and community activation consent are unchanged human steps. This keeps one code path for "does this app_id's permission set have a global-tier record," while flagging system-approved rows distinctly in the audit trail (`approval_source`) for periodic security review, since no human reviewed the dangerous asks.

### 3.7 Revocation

- Any tier can revoke a previously-granted permission at any time (tenant admin adds a restriction; community admin deactivates a specific permission without deactivating the whole bundle — new `deactivate_permission(app_id, permission_id)` alongside the existing `deactivate_bundle`).
- Revocation takes effect within ~1s via the push-invalidation path (§4, Gemini condition 2) — not the old `BUNDLE_CONFIG_POLL_SECONDS`-bounded staleness window; that poll remains only as a fallback for a dropped notification. An invocation already in flight when the revocation lands finishes its current host call, but the gate blocks every subsequent host call in that same invocation (§5.3).
- A revoked `dangerous` permission that a bundle's declared manifest requires (not opportunistic) auto-deactivates the whole bundle for that community, logged `WARN`, rather than running the bundle in a state its own manifest says it can't function in.

---

## 4. Grant storage

**hub-api is the only RW path; the data plane (`svc_process`/`svc_action`) holds a read-only role against a read replica**, identical pattern to PR #415 §2's control-plane/`bundle-data` split — grants are control-plane metadata, not bundle data, so they live in the existing control-plane DB, not `bundle-data`.

| Table | Written by | Key columns |
|---|---|---|
| `app_permission_requests` | `bundle_approval_service.approve_version()` (global tier) | `(app_id, version)` PK, `permission_id`, `risk`, `params_json`, `approved_by`, `approval_source`, `approved_at` |
| `app_tenant_permission_restrictions` | `marketplace_lifecycle_service.make_available()` (tenant tier) | `(tenant_id, app_id, permission_id)` PK — presence = restricted (excluded) |
| `community_permission_grants` | `marketplace_lifecycle_service.activate_bundle()` (community tier) | `(community_id, app_id, permission_id)` PK, `granted_by`, `granted_at`, `params_json` (community's own bound within the approved ceiling, e.g. a tighter `delta_max`) |
| `app_permission_grant_versions` | Any of the above, append-only | `(app_id, version, permission_snapshot_hash, effective_at)` — the version-pinning row §3.4 reads to decide "is this community re-consented for this version" |
| `bundle_reputation_adjustments` | Data plane (svc_action), via hub-api's existing audit path — see §7 | `(id, app_id, tenant_id, community_id, target_user_uuid, scope, delta, reason_code, occurred_at, reversal_of)` |

**Versioning:** a grant is scoped to `(app_id, version)`, not just `app_id` — re-using `app_permission_grant_versions.permission_snapshot_hash` (same `canonical_json`/sha256 pattern as `permission_hash()`) as the single value the data plane compares to know whether its cached grant set for a given active version is current, without re-fetching the full row set on every poll tick.

**Data-plane read path — push-invalidated, poll as fallback only (Gemini condition 2, resolving the TOCTOU gap a pure 300s poll leaves).** Every write to `app_permission_requests`/`app_tenant_permission_restrictions`/`community_permission_grants` publishes a change notification (`(app_id, tenant_id, community_id, permission_id)`) onto a Valkey stream/pub-sub channel (`waddles:permission-grants:changed`) in the same hub-api transaction that writes the row. `svc_process`/`svc_action` subscribe and, on receipt, re-fetch just that `(tenant, community, app_id)`'s grant set and refresh the in-memory `GrantSnapshot` — **p99 < 1s** from write to the data plane observing it. `svc_process`/`svc_action`'s existing `BUNDLE_CONFIG_POLL_SECONDS` poll (default 300, confirmed live at `core/svc_action/src/config.rs:293`, `core/svc_process/src/config.rs:264`) is **retained only as a fallback** — a full reconciliation pass covering any dropped/missed pub-sub message, never the primary invalidation path. `GrantSnapshot` is keyed `(tenant, community, app_id) -> {permission_id -> params}`, sourced from `community_permission_grants` JOIN `app_permission_requests` (never trusting a request-time claim) either way. This is the snapshot §5's gate reads — zero per-invoke DB round trips regardless of which path refreshed it.

---

## 5. The standard enforcement gate — `core/bundle_capability_gate`

**One crate, one function, every host import calls it first:**

```rust
pub fn authorize(
    scope: &InvokeScope,                 // (tenant, community, app_id) -- never bundle input
    permission: PermissionId,            // e.g. Permission::StorageTables, Permission::ReputationCommunityWrite
    resource: ResourceRef,               // AppScoped | ReputationScoped(target_user_uuid)
) -> Result<AuthorizedCall, Denied>;
```

- `CapabilityHandler::handle` in both `svc_process/src/capabilities.rs` and `svc_action/src/capabilities.rs` calls `authorize()` as the very first statement in every match arm, replacing today's ad hoc "check `data.tables` non-empty" style gating and the hardcoded `db`/`kv` denials.
- `AuthorizedCall` carries the resolved, server-derived resource (schema name, kv key prefix, object prefix, overlay token, or verified target-user) so the capability implementation never re-derives it from bundle args — the gate is the only place resource derivation happens.

### 5.1 `InvokeScope` construction (confused-deputy prevention) — Gemini condition 1

**`InvokeScope` is constructed ONLY by the host, from the immutable execution context — never from guest arguments, and never influenceable by them.** Concretely: the executor connection's pinned stage identity (which manifest/component this connection was instantiated for) plus the invocation's server-side envelope (the delivered event's own `tenant`/`community`/`app_id`, resolved before the guest is ever invoked) are the sole two inputs. The guest-facing WIT API **has no scope parameters at all** — no capability function in any interface (`kv`, `db`, `objects`, `http`, `overlay`, `ai`, `reputation`, `users`, `telemetry`) accepts a tenant, community, app_id, or connection-identity argument of any kind; `target_user` (`ReputationScoped`) is the only guest-suppliable identity-shaped argument anywhere in the catalog, and it names a *target*, never the scope the call executes under. This is what makes the `AppScoped`/`ReputationScoped` split in §5.2 sound: a bundle cannot become a confused deputy for another app/tenant/community because there is no argument through which it could try.

**Test requirement:** (1) a static/type-level test enumerates every WIT interface's every function signature and asserts none contains a tenant/community/app_id-shaped parameter (a schema-shape check against `wit/waddle-bundle/stage.wit`, run in CI, not just at spec-review time); (2) a runtime test drives a `host-call` frame carrying a forged or mismatched scope-shaped value inside its `args` JSON (e.g. an extraneous `"tenant": "other-tenant"` key) and asserts `authorize()`'s decision is identical to the same call without it — the connection's own pinned `InvokeScope` is authoritative regardless of `args` content, confirmed by observation, not by absence of a parameter alone.

### 5.2 Two scope types

| Scope | Resource derivation | Examples | Guarantee |
|---|---|---|---|
| **`AppScoped`** | Server-computed from `(tenant, community, app_id)` alone — **never accepts a bundle-supplied resource identifier for the scoping part** | `storage.kv` key prefix (`waddles:app:{tenant}:{community}:{app_id}:state`), `storage.tables` schema-qualified table (`app_core.<app_id>`/`app_community.<app_id>`, §1.4, per PR #415), `storage.objects` bucket prefix (§6), `overlay.media`'s community/token resolution (§8), `ai.generate`'s tenant-tier gate (§9) | A bundle can never name another app's schema/prefix/overlay token — the gate rejects before the capability implementation even runs a query or RPC |
| **`ReputationScoped`** | Bundle names a `target_user` (a UUID); the gate independently verifies that user belongs to the invocation's community (`reputation.community.write`, `users.profile.read` with `scope=community`) or tenant (`reputation.tenant.write`, `users.profile.read` with `scope=tenant`) via a membership check against the same RO snapshot | `reputation.adjust(target_user, delta, reason_code)`, `users.read(target_user)` | Membership is checked **at call time**, not just at grant time — a user who left the community since the bundle was activated cannot be adjusted or profile-read |

`users.profile.read` (§10.2) is deliberately modeled as `ReputationScoped`-style rather than a third scope type — same membership check, same snapshot, a read instead of a write.

### 5.3 Defense in depth

| Layer | Mechanism |
|---|---|
| Compile/link time | Per-component `Linker` (wit-v1.1 §5, reused verbatim) registers only host functions for permissions the manifest declares **and** the community has actually granted (not just requested) — an ungranted function is never linked; a call to it fails at instantiation, before any guest code runs |
| Instantiation time | Artifact integrity check (§5.6, Gemini condition 9) — the executor refuses to instantiate a component whose Ed25519 signature over the approved digest doesn't verify against the platform public key, before the linker step above even runs |
| Resource bounds (existing, referenced not redesigned) | Per-invoke epoch deadline + `new_bounded_store` memory limiter (PR #406) — already implemented, independent of `authorize()`; bounds runaway guest compute/memory regardless of what capabilities are granted (Gemini condition 8) |
| Runtime (every call) | `core/bundle_capability_gate::authorize()` re-checks the grant against the current `GrantSnapshot` (§4) — a push-invalidated cache (§4, Gemini condition 2), not just the 300s poll, so a mid-connection revocation is caught within ~1s, not up to 300s |
| Post-authorize | Capability-specific validation continues exactly as designed elsewhere (sqlparser-rs AST gate for `db`, content-type/size checks for `storage.objects`, host/duration checks for `overlay.media`, prompt/PII checks for `ai.generate`, delta-bound/rate-limit for `reputation`) — the gate answers "is this call allowed at all," not "is this specific SQL/object/delta/prompt valid" |

**No kill-switch.** Per `critical-rules.md`'s security-sensitive-mechanism rule, there is no env var, CLI flag, or config setting that disables `authorize()` — the only sanctioned bypass is the core-bundle system-approval path (§3.6), which still goes through the same table and the same gate, just with a distinguishable `approval_source`.

**Revocation mid-invocation (Gemini condition 2):** an in-flight host call that has already started is allowed to finish — `authorize()` doesn't preempt a call in progress. But a revocation that lands mid-invocation blocks every *subsequent* host call within that same invocation: the gate re-checks the grant version on every call, not once per invocation, so a guest making several `db.execute` calls in one execution gets cut off after the revoked permission's next use, never rides out the whole invocation on a stale grant.

### 5.4 Denials

- Typed WIT error variant per capability interface (`denied(string)` already the pattern for `http`/`db`/`relay`; extended with a stable `reason` string: `not_granted`, `resource_scope_mismatch`, `quota_exceeded`, `rate_limited`, `user_not_in_scope`, `delta_out_of_bounds`, `unsupported_platform`, `contains_pii`).
- Every denial: (1) audit-logged (`tracing` + OTel, sanitized per existing `capabilities.rs` pattern), (2) counted (`waddles_bundle_capability_denied_total{app_id, permission, reason}`), (3) returned to the guest as `access-denied`, never a fabricated success — same posture PR #415 and wit-v1.1 already commit to for their own denial paths.

### 5.5 Performance budget

- Grant lookup: one hashmap read against the in-memory `GrantSnapshot` (§4), O(1), no lock contention beyond a `RwLock` read guard already used for the existing bundle-config hot-swap — sub-microsecond, no allocation on the hot path.
- Reputation/profile membership check: one additional hashmap read against a `(community_id) -> HashSet<user_uuid>` membership snapshot, refreshed on the same push-invalidated cadence as the grant snapshot (§4).
- Target: `authorize()` adds ≤ 5µs p99 to a host-call round trip — validated by a criterion benchmark in the gate crate's own test suite (`writing-rust-tests` skill pattern), gating merge the same way wit-v1.1's other performance-sensitive paths do.
- Revocation propagation target: p99 < 1s from hub-api's write to the data plane's `GrantSnapshot` reflecting it (§4's push channel), with the existing `BUNDLE_CONFIG_POLL_SECONDS` (300s) retained only as a fallback for a missed/dropped push message — never the primary mechanism.

### 5.6 Artifact integrity (Ed25519 signing) — Gemini condition 9

hub-api signs the **approved component digest** at global-tier approval (§3.1) — Ed25519, signing key held in the platform KMS, never in application config. The executor verifies that signature against the platform's public key **before instantiating** any component, for every load — a missing or invalid signature fails closed (the component is never instantiated, not degraded to a warning). This closes the gap between "hub-api approved this exact digest" and "the executor is actually running that exact digest," independent of transport integrity (TLS) or storage integrity (bucket checksums) alone. Vendor-side signing (a vendor's own key, checked additionally) is optional and additive — it never substitutes for the platform signature, which is the mandatory gate.

---

## 6. Object storage (`storage.objects`)

| Aspect | Design |
|---|---|
| Bucket/prefix, **tiered (Gemini condition 7)** | **Enterprise tenants:** a dedicated per-tenant bucket (`waddles-bundle-objects-tenant-{tenant_id}`), matching the per-tenant isolation posture `critical-rules.md`'s KMS upsell already establishes for Enterprise. **Free/Professional tenants:** the single shared bucket `waddles-bundle-objects`, prefixed `tenant/{tenant_id}/community/{community_id}/app/{app_id}/`. Either way, the prefix/bucket is server-derived (`AppScoped`, §5.2) — a bundle-supplied `key` is appended only after rejecting `..`/leading `/`/absolute paths |
| Quotas | `max_object_bytes`, `max_total_bytes`, object-count ceiling — manifest-declared within the catalog default ceiling (§1), enforced pre-write via a periodic size/count scan job, same "cached last-known, fail closed on exceed" pattern as PR #415 §6's row/byte quota gate. **Per-tenant rate and IOPS limits** (Gemini condition 7) apply independent of bucket topology — a shared-bucket tenant's burst never starves another tenant's sequential throughput |
| Content-type allowlist | Manifest `content_types[]`, validated against a small platform allowlist (`image/png`, `image/jpeg`, `image/webp`, `application/json`, `text/plain` — no `application/octet-stream`/executable types without an explicit, dangerous-risk override) |
| Max object size | Catalog default 5 MB per object, manifest may request lower, never higher, without a dangerous-risk re-ack |
| Encryption | **Per-tenant SSE key** (Gemini condition 7) — a tenant-specific server-side encryption key, not a platform-shared one, regardless of bucket topology; same MinIO SSE baseline `security.md` already mandates, scoped per tenant rather than per-deployment. Customer-managed/external KMS remains the separate Enterprise upsell on top of this baseline per `critical-rules.md` |
| Deletion on uninstall | Same lifecycle as PR #415 §8's table deletion: per-tenant uninstall deletes that tenant's prefix (or, for Enterprise, empties/retires that tenant's dedicated bucket) immediately (audited); last-tenant-uninstalled retains the app's objects for the same 30-day grace window, then a background sweep deletes the whole `app/{app_id}/` prefix (or bucket) |
| Guest WIT interface | `put`/`get`/`list`/`delete`, scoped exactly as sketched below |

```wit
/// Bundle-scoped object storage under the app's own tenant/community
/// prefix or dedicated Enterprise bucket (server-derived, never
/// bundle-supplied — spec SS5.2 AppScoped).
/// Capability: granted only when `storage.objects` is declared and granted.
interface objects {
  record object-meta {
    key: string,
    size: u64,
    content-type: string,
    etag: string,
    last-modified: string,
  }

  variant error {
    denied(string),
    not-found,
    too-large(u64),
    invalid-content-type(string),
    quota-exceeded(string),
    backend(string),
  }

  put: func(key: string, content-type: string, body: list<u8>) -> result<object-meta, error>;
  get: func(key: string) -> result<option<tuple<object-meta, list<u8>>>, error>;
  list: func(prefix: string, cursor: option<string>, limit: u32)
    -> result<tuple<list<object-meta>, option<string>>, error>;
  delete: func(key: string) -> result<_, error>;
}
```

---

## 7. Reputation (`reputation.*`)

**No existing capability lets a WASM bundle touch either reputation score** (`community_members.reputation` or `reputation_global.score`). This spec routes bundle-originated changes through the **existing** `core/reputation_module` gRPC service rather than a raw table write, reusing its clamping/weighting/audit logic instead of duplicating it. `target-user` is always the tenant-tokenized UUID (§10.1) — reputation and `users.profile.read` (§10.2) share the identical `ReputationScoped` membership mechanism (§5.2).

### 7.1 WIT interface

```wit
/// Capability: action-stage-only (spec SS6.5's "granted only to
/// action-stage bundles" rule, same as `relay`/`moderation`). `read` needs
/// `reputation.read`; `adjust`'s `scope` must be covered by the caller's
/// actual grant (`reputation.community.write` covers `community`;
/// `reputation.tenant.write` covers both).
interface reputation {
  enum scope-kind { community, tenant }

  variant error {
    denied(string),
    user-not-in-scope,
    delta-out-of-bounds(s32),
    rate-limited(u32),
    backend(string),
  }

  /// `target-user` is always the canonical Waddles user UUID -- never a
  /// platform-native id or username (S10.1 PII-tokenization invariant).
  read: func(target-user: string, scope: scope-kind) -> result<s32, error>;

  /// Returns the new score. `reason-code` must be one of the manifest's
  /// declared `reason_codes` for the granted permission.
  adjust: func(target-user: string, scope: scope-kind, delta: s32, reason-code: string)
    -> result<s32, error>;
}
```

### 7.2 Call path

1. `core/bundle_capability_gate::authorize()` — `ReputationScoped(target_user)`: verify `target_user` belongs to the invocation's community (or tenant, for `tenant` scope) via the membership snapshot (§5.5). Not in scope → `user-not-in-scope`, no RPC attempted.
2. Delta bound check against the manifest's granted `delta_min`/`delta_max` (community admin's own bound, §3.3, never looser than the global-approved ceiling). Out of bounds → `delta-out-of-bounds`.
3. Rate-limit check — per-app-per-user-per-day and per-app-per-community/tenant-per-day aggregate token buckets (`UsageBatcher`, same primitive `relay`/`moderation` already use). Exceeded → `rate-limited`, **before** any RPC, matching moderation's "rate-limit strictly before the external call" merge gate (wit-v1.1 §7.1).
4. `reason_code` allowlist check against the manifest's declared `reason_codes`.
5. gRPC call to `core/reputation_module`'s `ReputationService` — **needs a new RPC**, `AdjustScore(tenant_id, community_id, user_id, scope, delta, reason_code, actor_app_id) -> SuccessResponse`, since the existing `RecordEvent` is weight-table-driven (`chat_message`/`command_usage`), not an arbitrary signed-delta API. Flagged as an open question (§13) — not designed further here.
6. Unconditional audit row: `bundle_reputation_adjustments` (§4), regardless of RPC success/failure.

### 7.3 Anti-abuse controls

| Control | Mechanism |
|---|---|
| Per-user cap | `|delta| <= delta_max` per call, plus a per-app-per-user-per-day aggregate cap (catalog default ±5, §1) |
| Per-community/tenant cap | Aggregate daily cap across all users (catalog default ±50 community / ±200 tenant, §1) — bounds a bundle distributing many small adjustments across many users to avoid the per-user cap |
| **Global per-bundle cap** (Gemini condition 4) | Aggregate daily delta cap for a given `(app_id, version)` **summed across every community and tenant it's activated in**, independent of the per-community/per-tenant caps above — bounds a single bundle install-base-wide farming pattern that stays under every individual community's cap by spreading across many communities |
| **Global per-publisher cap** (Gemini condition 4) | Same aggregation, one level up: a daily delta cap summed across **every app a single vendor/publisher has activated anywhere on the platform** — bounds a publisher operating several bundles each individually under the per-bundle cap |
| Mandatory reason code | Every `adjust()` requires a manifest-declared `reason_code` — no free-text, no blank reason; makes bulk-reversal and audit trivial |
| Reversibility | Every adjustment is a ledger row (`bundle_reputation_adjustments`), never an in-place score edit outside `reputation_module`'s own clamp logic; a reversal is a new `adjust()` call with the negated delta and `reason_code = "reversal:<original_id>"`, itself audited — never a destructive delete of the original row |
| Statistical anomaly flag | A background hub-api job flags an app whose daily adjustment **distribution and entropy** (Gemini condition 4 — not magnitude alone: a low-entropy pattern, e.g. always exactly `+1` to a rotating small set of accounts, is exactly as suspicious as a large-magnitude outlier) deviates from its own trailing 30-day baseline, for tenant-admin review (dashboard warning, human decides) |
| **Auto-suspend threshold** (Gemini condition 4) | A **harder**, automatic tier above the statistical flag: an app crossing a fixed, platform-wide anomaly threshold (e.g. an absolute global-per-bundle-cap breach, or an entropy/distribution deviation beyond the flag's own 3σ-equivalent ceiling) has its `reputation.*` grants **automatically suspended platform-wide** (not just flagged) and the **global admin is notified** immediately — distinct from the softer statistical flag, which stays a human-reviewed dashboard warning; reinstatement is a global-admin action, not automatic |
| Scope containment | `community.write` can never touch `reputation_global.score` — only `tenant.write` can, and only for users within that tenant — enforced by the gate's membership check, not by convention |
| Auto-ban interaction | A bundle-driven adjustment that would cross `REPUTATION_AUTO_BAN_THRESHOLD` (450) is **not** treated as a bundle-triggerable side effect — `reputation_module`'s own auto-ban logic runs downstream of any write, unchanged; bundles never get a distinct "ban" verb here (that's `moderation.ban`, wit-v1.1 §2) |

---

## 8. Overlay media (`overlay.media`)

Built on the existing `core/browser_source_core_module` gRPC surface — `BrowserSourceService.SendOverlayEvent(token, community_id, event_type, event_data)` (`libs/grpc_protos/browser_source.proto:21`) — no new transport, just a new `event_type`. Answers the `!vso` (play a ≤30s clip) shoutout requirement; `!aiso` additionally needs `ai.generate` (§9) to produce the line, then this capability to play it.

| Aspect | Design |
|---|---|
| Token/community resolution | Server-derived (`AppScoped`, §5.2) from `(tenant, community, app_id)` — the bundle never sees or supplies the overlay token |
| Host allowlist | Manifest `allowed_hosts[]` (e.g. `www.youtube.com`, `kick.com`) — `play-media`'s `url` host must match; reuses the same `_EGRESS_HOST_RE` validator `net.http.fqdn` already has (§2.3), applied to a second manifest field rather than a new regex |
| Duration cap | Manifest `max_duration_seconds`, catalog ceiling 30s; the host clamps, never trusts the guest's own claim |
| Rate limit | 1 item/10s/app/community, `UsageBatcher` token bucket (same primitive as `relay`/`moderation`) |
| User-facing text | Out of scope for `play-media` itself (video/image only); a future `overlay.text`/`overlay.leaderboard` capability rendering usernames is subject to §10.4's output-detokenization rule and needs its own catalog entry — not designed further here |

```wit
/// Capability: action-stage-only, granted only when `overlay.media` is
/// declared and granted. `url`/`kind`/`duration-seconds` are validated
/// against the manifest's own allowed_hosts/max_duration_seconds -- the
/// gate resolves community_id/token server-side, never from guest input.
interface overlay {
  enum media-kind { video, image }
  variant error { denied(string), invalid-url(string), too-long(u32), rate-limited(u32), backend(string) }

  play-media: func(url: string, kind: media-kind, duration-seconds: u32) -> result<_, error>;
}
```

---

## 9. AI generation (`ai.generate`)

Host-mediated only — **never a vendor AI SDK inside the WASM component**, generalizing the existing `action/interactive/ai_interaction_module`'s `WaddleAIProvider` pattern (today a Python-service-local import) into a WIT host capability. Enterprise-gated: `critical-rules.md`'s Feature Flags & License Tiers table already lists WaddleAI as Enterprise; the tenant marketplace check (§3.2) additionally requires `get_tier() == "enterprise"` before `ai.generate` is even offered to a tenant, per the `integrating-waddleai` skill's existing pattern — a Professional tenant never sees the permission, regardless of what a bundle's manifest requests. Prompt safety reuses `prompt_safety.py`'s `UNTRUSTED_DATA_NOTICE`/`wrap_untrusted` delimiting for any platform-sourced text folded into a prompt, and **rejects (never redacts)** a prompt matching the PII patterns `hub_api/services/ai_routing/pii_redaction.py` already flags for BYOK traffic — the host assembles/validates the final prompt; the bundle never talks to WaddleAI directly and never sees anything beyond the completion text.

| Aspect | Design |
|---|---|
| Quotas | 2,000 prompt tokens / 512 completion tokens / 10 calls-minute (catalog default) |
| PII posture | Host rejects (`contains-pii`) rather than silently redacts — a bundle should never have had PII in scope to put in a prompt (§10.1's hard invariant) |
| License gate | Enterprise-only, checked at tenant-marketplace time (§3.2), not manifest-parse time |
| Answers | `!aiso`'s AI-generated shoutout requirement |

```wit
/// Capability: action-stage-only, Enterprise-tier gated (S9). The host, not
/// the bundle, resolves and calls WaddleAI -- no vendor SDK ships inside
/// the component.
interface ai {
  variant error { denied(string), quota-exceeded, prompt-too-large(u32), contains-pii, backend(string) }

  generate: func(prompt: string, max-tokens: u32) -> result<string, error>;
}
```

---

## 10. User identity, PII boundary & mention tokenization

### 10.1 Hard invariant

**A bundle only ever receives tokenized identity (UUIDs), never PII.** Usernames and display names ARE PII under this platform's rules (`critical-rules.md` PII Tokenization: "Reference users by UUID, not username, wherever possible and *always* outside the boundary") — a WASM bundle is squarely "outside the boundary." This governs every direction:

- **Inbound:** every delivered event's actor and any target user are UUIDs (§10.3); no permission is needed to read them — they're just fields on data the bundle is already subscribed to receive (§1's "actor/target identity is not a permission" note).
- **Sideways (host-mediated lookups):** `users.profile.read` (§10.2) returns only non-identifying attributes; a platform-identity-requiring call (e.g. a Helix clip lookup) never round-trips a platform handle through the guest (§10.3, last bullet).
- **Outbound:** a bundle emits `{user:<uuid>}` placeholders only; rendering to a display name happens exactly once, host-side, at the sink (§10.4).

**HARD BLOCKER (Gemini condition 3), not a follow-up:** `core/svc_ingest/src/normalize.rs:150,185,263` currently populates `platform-event.actor` with the raw platform username/login (`msg.sender`, `msg.author_username`, `user_login`) — a live PII leak today, not a theoretical gap. **No permission in this catalog — `normal` or `dangerous` — ships to any bundle until this is fixed**, per §12 Phase 1, Task 1. The fix: `actor` and any target user are tokenized to a canonical UUID, or an ephemeral pseudonym for an unlinked identity (§10.3), before the event ever reaches the process-stage or action-stage bundle; the raw platform username/login is retained only inside the PII boundary (hub-api's `hub_users` mapping and the sink-side detokenizer, §10.4), never forwarded past `svc_ingest`/`svc_process`'s pre-dispatch pass into guest-visible data.

### 10.2 `users.profile.read`: non-identifying attributes only

Returns **only** the following, and nothing else — no display name, username, avatar/profile image, email, bio, or any platform-native id that is itself identifying:

| Field | Type | Notes |
|---|---|---|
| `role_flags` | `list<string>` | subset of `moderator`/`vip`/`subscriber`/`broadcaster` — platform-reported role state only |
| `subscription_tier` | `option<string>` | e.g. Twitch's tier1/tier2/tier3; platforms without tiers return `none` |
| `is_follower` | `option<bool>` | platforms without a follow concept return `none` |
| `first_seen_date` | date (day precision) | this tenant's first recorded event from the user — day precision only, never a full timestamp |
| `account_age_bucket` | enum: `new`, `<1mo`, `1-6mo`, `6-12mo`, `1-2y`, `2y+` | coarse bucket, never the exact account-creation date |

If a platform genuinely has no non-identifying attribute to offer (e.g. a bare webhook source with no role/subscription/follow concept), the call returns `unsupported-platform` rather than degrading toward identifying fields — **the permission is cut for that platform, not weakened**.

```wit
/// Capability: granted only when `users.profile.read` is declared and
/// granted. Returns non-identifying attributes only (S10.1) -- never a
/// name, avatar, email, bio, or identifying platform id.
interface users {
  enum account-age-bucket { new, under-1-month, from-1-to-6-months, from-6-to-12-months, from-1-to-2-years, over-2-years }

  record public-profile {
    role-flags: list<string>,
    subscription-tier: option<string>,
    is-follower: option<bool>,
    first-seen-date: string,        // RFC 3339 date, day precision
    account-age-bucket: account-age-bucket,
  }

  variant error { denied(string), unsupported-platform, not-found, backend(string) }

  /// `user` is the tenant-scoped UUID already on bundle-context/
  /// platform-event -- never a platform handle.
  read: func(user: string) -> result<public-profile, error>;
}
```

### 10.3 Inbound: actor/target tokenization & mention resolution

| Step | Where | What |
|---|---|---|
| 1 | `core/svc_ingest::normalize.rs` | Resolves `actor` (and any platform-**structured** target reference already to hand, e.g. a Discord `<@id>` mention or a raid's `from_broadcaster_user_id`) to a canonical Waddles user UUID via the existing single `hub_users` identity table, upserting a new identity on first sight — the same choke point that already builds `PlatformEvent`, before `publish.rs` ships it onward. `platform-event.actor`'s WIT type is unchanged (`option<string>`); the string it carries changes from a login to a UUID (or an ephemeral pseudonym, step 3) |
| 2 | `core/svc_process`, new pre-dispatch pass | **Unstructured** mentions/command arguments naming a user (Twitch/IRC `@someuser`, a bare `!so someuser` argument) cannot be resolved at ingest (no per-command argument grammar there). A new host-side pass scans `payload-json` text fields for platform-specific mention grammar **after** `consumes`/`command_prefix` filter matching (which still needs the raw text) but **strictly before `process-stage.transform`/`action-stage.dispatch` is ever invoked into the guest**, replacing each recognized mention with `{user:<uuid>}` |
| 3 | Same passes | An unknown/unlinked user (mentioned by someone else, never itself seen by this integration) gets a **deterministic ephemeral pseudonym** — `UUIDv5(WADDLES_MENTION_NAMESPACE, "{platform}:{platform_user_id}")` — consistent across repeated mentions but **never** a real `hub_users` row and **never** resolvable by `users.profile.read` (`not-found`). No real identity record is auto-created just because someone else named a platform handle — deliberate, avoids a DSAR/erasure surface for people who never themselves interacted |

**Free-text PII beyond a recognized mention** (e.g. a user typing "my email is x@y.com" into chat) is **not** scrubbed by this pipeline — flagged as an explicit open question (§13); structured mention tokenization and general free-text PII detection are different-shaped problems and this spec only solves the former.

**Platform-identity-requiring host calls never round-trip a handle through the guest.** A call needing a platform id (e.g. Twitch Helix's clips-lookup for `!vso`) is modeled as a typed host function taking a UUID — the same `moderation` pattern (wit-v1.1 §2: platform-agnostic args, host resolves the platform mapping) — never a raw `net.http.*` passthrough carrying a platform handle. The host resolves UUID→platform id via the existing identity mapping, makes the call, and returns only the operationally-needed, non-identifying result (e.g. a clip URL) — any PII in the platform API's own response (display name, avatar) is stripped host-side and never reaches the guest.

### 10.4 Outbound: placeholder rendering & output detokenization (every sink)

A bundle only ever emits `{user:<uuid>}` placeholders — never a rendered name — in any text/data handed to a host capability (`relay.push`, a future `overlay` text/leaderboard capability, a future webhook-out or Discord-embed sink). **Detokenization happens exactly once, inside the trusted component that owns the sink, immediately before the payload leaves that component — never earlier, never inside the bundle.**

**Detokenizer hardening (Gemini condition 5).** The renderer is a **single-pass, non-recursive** tokenizer — it scans the payload exactly once and never re-scans its own substitution output (a resolved display name is never fed back through the placeholder grammar, closing off a self-referential injection where a crafted "display name" itself contains `{user:...}`-shaped text). Placeholders are recognized **only** against a strict grammar (`{user:<uuid-v4-or-v5-shape>}`, nothing looser) — any bundle-supplied text containing a brace sequence that merely *resembles* the grammar (e.g. literal `{user:not-a-uuid}` typed by a guest, or an attacker-influenced string smuggled through a manifest justification field) is **escaped before tokenizing** (braces entity-escaped) so it can never be misidentified as a real placeholder or trigger a second substitution pass. The rendered output is then **HTML/markup-escaped per sink** — each sink's own escaping rules (IRC-safe for chat, HTML-entity for overlay/DOM), not one shared escaper assumed safe everywhere.

| Sink | Renders in | Detail |
|---|---|---|
| Chat (`relay.push`) | `core/svc_action` (`handle_relay`/`handle_discord_relay`) | The existing outbound trust boundary (`sanitize_irc_component`'s pattern) gains a detokenize-then-sanitize step before `LPUSH`/REST send |
| Overlay (`overlay.play-media`, future overlay text/leaderboard capabilities) | The component actually feeding the browser source over its websocket/SSE connection (`core/browser_source_core_module`) | Renders at the point of streaming the frame to the browser — a raw UUID must never appear in the DOM, a URL, or a socket frame. The `overlay` WIT capability in `svc_action` passes `{user:<uuid>}` through **unresolved**; resolution is `browser_source_core_module`'s job, since it is the component actually adjacent to the untrusted browser context, not `svc_action`. The browser source itself renders inside a **sandboxed `<iframe sandbox="allow-scripts">`** (deliberately **without** `allow-same-origin`) under a **strict CSP** (no inline script execution beyond what the sandbox already isolates, no arbitrary external resource loads) — defense in depth against the rendered content itself, independent of the detokenizer's own escaping (Gemini condition 5) |
| Future sinks (webhooks out, Discord embeds) | Whichever component makes the final outbound call | Same rule by construction — a new sink is not compliant until it detokenizes at its own egress point |

- **Batched, cached resolution.** Each rendering component resolves UUIDs via a small per-tenant `{uuid -> display_name}` cache (one batched lookup, not one query per mention), TTL 5 minutes default, invalidated immediately on a rename event rather than waiting out the TTL.
- **Erased or unknown users** render as a fixed neutral label (`"a former viewer"`) — never the UUID, never a blank string, never a visible error.
- **HTML/markup escaping is mandatory on every rendered name before it reaches an overlay** (XSS-sensitive: an attacker-chosen display name is exactly the injection vector this closes) — per-sink escaping (above), not one shared escaper assumed safe everywhere, plus the overlay's own sandboxed-iframe/CSP layer as a second, independent control.
- **Test:** a data-plane regression test asserts (1) no UUID-shaped string appears in any overlay `event_data` payload or any chat `relay.push` payload, (2) a crafted brace-sequence in guest-supplied text (`{user:not-a-real-uuid}`, or a nested/self-referential placeholder) is escaped rather than substituted or re-scanned, and (3) the renderer never runs a second substitution pass over its own output — across a fixed corpus of synthetic bundle outputs, reported with the corpus size (`critical-rules.md` Verification Integrity: a zero-payload test proves nothing).

Bundles stay PII-free in both directions: inbound tokenized at ingest/pre-dispatch (§10.3), outbound detokenized at the sink (§10.4) — never inside the WASM boundary either way.

### 10.5 `user_ref` columns & erasure cascade

PR #415's manifest DSL (already one-table-only per §1.4, commit `e35c2db0` round-3) owns column typing; this spec adds one DSL annotation: a `uuid`-typed column may be flagged `user_ref: true` (e.g. PR #414's fishing table's `user_ref` column). hub-api's schema compiler validates that a `user_ref`-flagged column only ever receives UUIDs resolving within the bundle's own invocation tenant — the same tenant-membership check `authorize()` already performs for `ReputationScoped`/`users.profile.read` calls (§5.2) — a bundle cannot smuggle a foreign tenant's UUID into its own table. On a DSAR/erasure request, hub-api's existing erasure job (the single place that already walks every PII-adjacent table for a `hub_users` row, per `critical-rules.md` PII Tokenization) is extended to also walk every bundle's single table for `user_ref`-flagged columns matching the erased UUID, deleting/nulling those rows in the same transaction — automatic and bundle-agnostic, since there is always exactly one table and the annotation is declarative.

### 10.6 Enforcement & tests

- `authorize()` denies a `users.profile.read` call whose target UUID isn't tenant/community-scoped, identical mechanism to `reputation.read` (§5.2).
- Linker isolation (§5.3) applies identically — a component without `users.profile.read` granted never gets `users.read` linked at all.
- Two count-asserted regression gates are required, not optional (`critical-rules.md` Verification Integrity):
  1. **Inbound:** no field in any WIT record delivered to a guest matches the PII sanitizer's `SENSITIVE_KEYS` denylist or an explicit username/display-name/email/avatar pattern, across a fixed corpus of synthetic ingest events.
  2. **Outbound:** §10.4's no-UUID-in-egress-payload test.

---

## 11. hub-webui consent screens

| Screen | Tier | Content |
|---|---|---|
| Catalog approval | Global admin | Full permission list, `dangerous` ones visually separated ("Dangerous permissions" section, red/amber accent) with per-id checkbox ack; cannot submit approval with an unchecked dangerous permission |
| Marketplace listing | Tenant admin | Same list, each permission toggleable **off only** (opt-out), dangerous ones pre-expanded by default; a tooltip shows the bundle's own `justification` string verbatim; `ai.generate` shows an "Enterprise" badge and is un-toggleable (hidden entirely, not just disabled) below Enterprise tier |
| Install/activation prompt | Community admin | Android-style single scrollable list grouped `Dangerous` (top, expanded, each with an icon + justification) then `Standard` (collapsed by default, "View all N permissions" disclosure) — big "Allow" / "Don't install" buttons, no partial-grant default (every listed permission must be explicitly allowed or the specific missing ones are called out, per §3.3) |
| Re-consent banner | Community admin | Shown when a pinned-old-version community's app has a newer version awaiting re-consent (§3.4) — diffs old vs. new permission set inline (`+ reputation.tenant.write`, styled like a diff) |
| Revoke | Tenant/Community admin | Per-permission toggle from the app's settings page, calling `deactivate_permission` (§3.7); dangerous permissions get a confirmation step naming what breaks (from the manifest's own "required vs. opportunistic" flag) |

---

## 12. Phased implementation plan (agent-sized, ≤30 min each)

**Renumbered against Gemini's PASS-WITH-CONDITIONS review (§0): the PII fix is now Phase 1, Task 1 — the single highest-priority item in this entire plan, per condition 3.**

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | Define `Permission`/`PermissionId`/`Risk` enums + catalog table (§1) as a shared Rust module in the new `core/bundle_capability_gate` crate; unit tests for id parsing (`net.http.fqdn:<host>`, `net.http.public-ip:<ip>`, `net.http.private-ip:<ip|cidr>`, `chat.send:<platform>`) | Rust | none |
| 0 | Add `_PERMISSION_ID_RE` + catalog validation to `bundle_manifest_v2.py`, replacing dead `permissions: []` parsing (§2.3) | hub-api | Phase 0 (catalog agreed) |
| 0 | Migration: `app_permission_requests`, `app_tenant_permission_restrictions`, `community_permission_grants`, `app_permission_grant_versions`, `bundle_reputation_adjustments` (§4) | hub-api (raw migration, control-plane DB) | none |
| **1 (Task 1 — HARD BLOCKER, Gemini condition 3)** | Tokenize `actor` in `core/svc_ingest::normalize.rs` (UUID via `hub_users`, replacing the raw-username assignment at lines 150/185/263) + the svc-process pre-dispatch mention-resolution pass + ephemeral-pseudonym minting (§10.3); raw username/login is retained only inside the PII boundary (`hub_users` mapping, sink-side detokenizer) | Rust (svc_ingest, svc_process) | none — highest priority in this entire plan, blocks every phase below that touches a Dangerous permission |
| 1 | Inbound PII regression test (§10.6): fixed corpus of synthetic ingest events, assert zero PII-shaped fields reach any WIT record | Rust (svc_ingest/svc_process tests) | previous task |
| 2 | Extend `permission_summary_service.build_permission_summary()` to render the full catalog instead of `_derive_capabilities()`'s 8 names; update `classify_diff()`'s flatten set to include permission ids + params | hub-api | Phase 0 |
| 2 | `bundle_approvals.py::post_approve` gains `approved_permissions[]` ack requirement + `incomplete_dangerous_ack` refusal (§3.1) | hub-api | previous task |
| 3 | `marketplace_lifecycle_service.make_available()` gains `restricted_permissions[]` + the `ai.generate` Enterprise auto-restriction (§3.2); `activate_bundle()` gains mandatory `granted_permissions[]` + `consent_required` refusal (§3.3); both enforce the hard, non-bypassable upgrade block on any permission delta (§3.4, Gemini condition 6) | hub-api | Phase 2 |
| 3 | `deactivate_permission(app_id, permission_id)` endpoint + service function (§3.7) | hub-api | previous task |
| 3 | `seed_core_bundles.py` extended to also write `app_permission_requests` rows with `approval_source="system:core-seeder"` (§3.6) | hub-api | Phase 0 |
| 4 | `core/bundle_capability_gate` crate: `authorize()`, `InvokeScope` constructed only from the host's pinned connection identity + server-side envelope (§5.1, Gemini condition 1 — never from guest args), `AppScoped`/`ReputationScoped` resource derivation, unit tests against fixed grant fixtures | Rust | Phase 0 |
| 4 | Confused-deputy test suite (§5.1, Gemini condition 1): static WIT-schema check that no interface function accepts a scope-shaped parameter, plus a runtime test that a forged scope-shaped value inside `args` has no effect on `authorize()`'s decision | Rust | previous task |
| 4 | hub-api: publish grant-change notifications onto `waddles:permission-grants:changed` (Valkey stream/pub-sub) on every write to the permission tables (§4, Gemini condition 2) | hub-api | Phase 0 |
| 4 | `GrantSnapshot` push-subscriber wired into `svc_process`/`svc_action`, refreshing just the changed `(tenant, community, app_id)` on notification, p99 < 1s; existing `BUNDLE_CONFIG_POLL_SECONDS` poll retained as fallback-only reconciliation (§4, §5.5) | Rust (both stages) | previous task |
| 4 | `svc_process`/`svc_action`'s `CapabilityHandler::handle` calls `authorize()` first in every match arm, replacing today's hardcoded `db`/`kv` `not_implemented` denials with `not_granted` where applicable; per-call version re-check so a revocation blocks subsequent calls within an already-in-flight invocation (§5.3) | Rust (both stages) | previous task |
| 5 | Artifact signing (§5.6, Gemini condition 9): hub-api signs the approved component digest (Ed25519, platform KMS key) at global-tier approval (§3.1) | hub-api | Phase 0 |
| 5 | Executor verifies the signature against the platform public key before instantiating any component — fail closed on missing/invalid signature, before the linker step (§5.3) runs | Rust (`core/bundle_executor`) | previous task |
| 6 | Wire `storage.kv` end-to-end through the gate (`feature/bundle-kv-capability` — currently zero commits; this phase **is** that branch's scope): `AppScoped` key-prefix derivation, quota enforcement, real Valkey hash get/set/delete/increment | Rust (both stages) | Phase 4, Phase 1 |
| 6 | Wire `storage.tables` through the gate atop PR #415's `db.execute` implementation (already one-table-per-bundle, `app_core`/`app_community` schema naming — commit `e35c2db0`, round-3) — the gate supplies the `AppScoped` schema-qualified table resolution (§1.4) | Rust (both stages) | Phase 4, Phase 1, PR #415's Phase 3 |
| 6 | `user_ref` DSL annotation + erasure-cascade extension to the existing hub-api erasure job (§10.5) | hub-api | previous task |
| 7 | `storage.objects`: shared-bucket provisioning + prefix scheme for Free/Professional tenants, WIT interface (§6) wired into `bundle_executor`'s linker + both stages' `CapabilityHandler` | Rust + hub-api | Phase 4, Phase 1 |
| 7 | `storage.objects` Enterprise tier: dedicated per-tenant bucket provisioning, per-tenant SSE key, per-tenant rate/IOPS limits (§6, Gemini condition 7) | Rust + hub-api | previous task |
| 8 | `users.profile.read`: WIT interface (§10.2), per-platform field-availability table, gate wiring reusing the `ReputationScoped` membership check | Rust (svc_action) | Phase 4, Phase 1 |
| 8 | `overlay.media`: WIT interface (§8) wired to `browser_source_core_module`'s existing `SendOverlayEvent` RPC, host-allowlist/duration-clamp/rate-limit; browser source rendered in a sandboxed `<iframe sandbox="allow-scripts">` (no `allow-same-origin`) under a strict CSP (§10.4, Gemini condition 5) | Rust (svc_action) + browser_source | Phase 4, Phase 1 |
| 8 | Output-detokenization renderer in `browser_source_core_module` and `svc_action`'s `handle_relay`/`handle_discord_relay`: single-pass non-recursive tokenizer, strict placeholder grammar, brace-sequence escaping before tokenizing, per-sink HTML/markup escaping, batched/cached name resolution with rename invalidation, neutral label for erased/unknown users (§10.4, Gemini condition 5) | Python (browser_source) + Rust (svc_action) | Phase 1 |
| 8 | Output-detokenization regression test (§10.6): no UUID-shaped string in any overlay/chat egress payload; a crafted brace-sequence is escaped, not substituted or re-scanned; fixed corpus, count-asserted | Rust + Python tests | previous task |
| 9 | `ai.generate`: WIT interface (§9), host client reusing `prompt_safety.py`/`pii_redaction.py`, Enterprise-tier gate wired into `make_available()` (§3.2) | Rust (svc_action) + hub-api | Phase 3, Phase 4, Phase 1 |
| 10 | `reputation.proto`: add `AdjustScore` RPC (§7.2 step 5, open question §13) | Python (`core/reputation_module`) | none — can start in parallel with Phases 0-4 |
| 10 | `reputation` WIT interface (§7.1) + gate wiring (membership snapshot, delta/rate-limit checks) in `svc_action` only (action-stage-only) | Rust (svc_action) | Phase 4, Phase 1, previous task |
| 10 | `bundle_reputation_adjustments` audit write path + global per-bundle/per-publisher cap enforcement + distribution/entropy anomaly job + automatic platform-wide suspend-and-notify-global-admin threshold (§7.3, Gemini condition 4) | hub-api | Phase 10 (schema exists) |
| 10 | `telemetry.logs`/`telemetry.metrics`: wire the catalog grant into `authorize()` (§4); instrument declaration, cardinality caps, scrubbing, rate limits, and OTel conversion land on `docs/bundle-telemetry-capability`'s own schedule, not this plan | Rust (both stages) | Phase 4, `docs/bundle-telemetry-capability` |
| 11 | hub-webui: catalog-approval dangerous-permission ack UI (§11 row 1) | React | Phase 2 |
| 11 | hub-webui: marketplace restriction toggles + Enterprise badge for `ai.generate` (§11 row 2) | React | Phase 3, Phase 9 |
| 11 | hub-webui: community activation Android-style consent screen + re-consent banner, reflecting the hard any-delta upgrade block (§11 rows 3-4, §3.4) | React | Phase 3 |
| 11 | hub-webui: per-permission revoke UI (§11 row 5) | React | Phase 3 |
| 12 | OTel metrics/logs/traces for `authorize()` denials and grant-snapshot refresh, including push-invalidation latency (§5.4, §5.5) | Rust (both stages) | Phase 4 |
| 13 | Fishing bundle (PR #414) re-requests permissions against the final catalog, including `storage.tables`'s schema naming and `user_ref` annotation, once Phases 0-6 land | fishing spec owner | Phases 0-6 |

Phases 0-3 (hub-api schema + consent flow) and the `reputation.proto` RPC addition (Phase 10's first task) have no Rust dependency and can proceed fully in parallel with Phase 4 (the gate crate itself) — but **Phase 1 (PII tokenization) is a hard prerequisite for every phase from 6 onward**, not just a subset: no permission that exposes or touches user identity ships before it, per Gemini condition 3.

---

## 13. Open questions (not blockers, flagged for follow-up)

- **`reputation_module`'s `AdjustScore` RPC** (§7.2 step 5) is named but not designed here — needs its own short addendum covering how it interacts with `reputation_service.py::adjust()`'s existing weight/clamp pipeline (does a bundle-driven delta bypass weighting entirely, or get treated as a new weighted `event_type` per app?).
- **Cross-bundle permission sharing** (e.g. fishing-shop's tables read by fishing-core, PR #415 §10) is out of scope for the permission *catalog* — it's an existing open question in PR #415, sharpened by §1.4's one-table-per-bundle rule (a cross-bundle FK is now structurally impossible, not just discouraged), unaffected otherwise by this design.
- **`moderation.<platform>` catalog entry vs. wit-v1.1's bare `moderation` capability flag** — this doc assumes the fold happens in the same PR that lands wit-v1.1 (§2.4); if wit-v1.1 merges first with the bare flag, a follow-up migration renames it without changing enforcement semantics.
- **Free-text PII scrubbing beyond recognized mentions** (§10.3) — general NLP-shaped detection of PII embedded in arbitrary chat text (an email address, a phone number typed into a message body) is explicitly not solved by the mention-tokenization pipeline; needs its own design if required.
- **Ephemeral pseudonym linking** (§10.3) — if a mentioned-but-unlinked user later links their platform identity, their ephemeral UUIDv5 pseudonym and their new real `hub_users` UUID are permanently distinct; a bundle that cached the ephemeral id (e.g. in its `storage.kv`) will silently see "two different users." Acceptable for this landing (no correctness requirement to retroactively unify), but worth flagging to bundle authors in SDK docs.
- **Output-detokenization cache ownership** (§10.4) — whether the per-tenant `{uuid -> display_name}` cache is a new shared component or duplicated per-sink (svc_action, browser_source_core_module, future sinks) is left to implementation; duplication is acceptable at this scale but a shared cache service is the likely eventual consolidation once a third sink exists.
- **`overlay.text`/`overlay.leaderboard`** (§8, §10.4) — a future capability rendering usernames on an overlay needs its own catalog entry and manifest params; sketched as a direction, not designed here.

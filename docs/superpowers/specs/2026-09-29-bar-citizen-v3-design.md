# Bar Citizen v3.0 Design — Greenfield Bundle Build

**Status:** Draft — objectives need owner confirmation before implementation starts.
**Date:** 2026-09-29
**Scope:** New app bundles (`waddles.core.barcitizen.*`, `waddles.core.starcitizen.*`), cross-server role management (#497), event sync (#96), plus the hub-api/data-plane pieces they depend on.
**Driver:** 2026-09-29 release-plan review — Bar Citizen (2nd-biggest customer) has never received any features, v2 included. Per owner scope notes on #484/#485/#486/#497/#96 (all 2026-09-29): **this is a greenfield v3 build against Bar Citizen's objectives, not a parity port.** There is no legacy behavior to preserve; acceptance criteria in the issues themselves are placeholders pending this spec.
**Builds on (read, reconciled, not re-litigated):** `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md` (#419, MERGED — permission catalog, capability gate, `storage.tables` schema derivation), `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md` (#415/#430/#498 — one-table-per-bundle DDL), `docs/superpowers/specs/2026-09-28-connector-bundles.md` (#444 — `waddle:connector@1.0.0` WIT world, `connector.receive:<platform>`/`connector.send:<platform>`/`connector.pii.read`, `waddles_connector_pii_reader` RO role), `docs/reference/bundle-network-permissions.md` (#466 — three-family egress risk model), `docs/superpowers/specs/2026-09-28-v3.0-python-removal-plan.md` (wave/slice format this doc follows).
**Review:** Gemini second-opinion pass (critical perspective) against this doc's own draft and the platform's PII/capability-gate/connector invariants. Findings folded in: removed a raw Odoo-partner-id column that leaked PII into bundle storage (§2.2), corrected the Spectrum connector's transport model to match #444's host-owns-all-sockets design instead of requesting `net.http.fqdn` on a receiver-only bundle (§2.3), eliminated a duplicate event-data table between the read-only bundle and the §4.1 canonical events table (§2.1), clarified where RSI-handle tokenization actually happens relative to connector `on-frame` delivery (§5.1), made bulk role-op rate limiting host-enforced instead of bundle-trusted (§3.4), added a tag re-injection loop-prevention rule (§4.2), and split Discord E2E into mocked-PR-gate + nightly-live-scheduled-job to avoid the fork-PR-secret and shared-test-guild-concurrency problems (§7).

---

## Open questions for Bar Citizen (owner takes these to the customer)

1. **Event/data source for the read-only bundle (#484):** does Bar Citizen publish events via a public API (Discord, a community calendar, a website with structured data), or would ingestion require scraping a page with no API? Scraping carries ToS/fragility risk (§2.1) — need the actual source(s) before `net.http.fqdn` targets can be finalized.
2. **Odoo access (#485):** does Bar Citizen's Odoo instance expose XML-RPC/REST (standard Odoo capability) to an external integration, and can they issue a scoped API key/OAuth app instead of shared admin credentials?
3. **Spectrum organization details (#486):** confirm the RSI org handle/URL and whether Bar Citizen accepts the one-way-sync-only limitation (no posting back to Spectrum — RSI ToS risk, per #101).
4. **Role management scope (#497):** how many Discord guilds/servers does Bar Citizen operate across, and is cross-platform (not just cross-server) role sync actually needed in v3.0, or is cross-Discord-guild sufficient for launch?
5. **Event sync (#96):** which platforms beyond Discord does Bar Citizen need events synced to (a public calendar? Spectrum inbound only?), and what community tag alias do they want (defaults to display name)?
6. **License tier:** confirm whether Bar Citizen's contract already entitles them to Professional/Enterprise-gated capabilities (cross-server role sync, audit logging) or whether this work should ship Free-tier to match "no features ever delivered" — see §3 tier table, all marked ASSUMPTION.

---

## 1. Objectives

| # | Objective | Source | Notes |
|---|---|---|---|
| 1 | Ship a read-only Bar Citizen bundle surfacing community events and public informative data into connected platforms | #484 (restores closed #14) | Read-only enforced via the capability gate (#428/#433), not bundle-local logic |
| 2 | Ship a full Bar Citizen bundle: read/write Discord integration + Odoo (ERP) sync | #485 (restores closed #15) | **ASSUMPTION — confirm with owner:** #15's original "different external WaddleDB connection for Odoo" is architecturally incompatible with the v3 bundle model (no raw external-DB connections); this spec routes Odoo access through Odoo's own API instead (§2.2) |
| 3 | Ship a Star Citizen Spectrum connector: one-way (Spectrum→WaddleBot) message/event/roster sync | #486 (re-specs open #101) | No outbound posting, ever — RSI ToS risk. #101 itself stays untouched/unmilestoned as an architecture-superseded reference |
| 4 | Cross-server/cross-platform role management: sync/assign roles driven by membership, reputation, or identity verification, with bulk apply and audit logging | #497 | **ASSUMPTION — confirm with owner:** issue text says "across multiple servers on a platform and, where the platform supports it, across platforms" — this spec treats cross-*guild* (same platform) as the v3.0 committed scope and cross-*platform* role sync as a stretch goal (§4.6), pending Bar Citizen's actual guild count (open question 4) |
| 5 | Cross-community event sync via community tags, filtering which communities see which events on a shared platform instance | #96 | Moved up from v3.1 to v3.0 per 2026-09-29 owner comment (customer-critical for Bar Citizen) |
| 6 | Tenant isolation: no bundle or role-sync operation may cross a tenant boundary | #497 (explicit AC), `critical-rules.md` PII Tokenization | Hard constraint, not a preference — enforced at the capability gate, not just the UI |
| 7 | PII tokenization: no raw Star Citizen/RSI handle or Discord username crosses the hub-api boundary | #485, #486, `critical-rules.md` PII Tokenization | RSI handles are explicitly PII for this design (§5) |
| 8 | Every bundle/feature ships behind a defaulted-OFF PostHog flag and passes 90%+ coverage with OTel logs/metrics/traces | `critical-rules.md`, `general.md` | Standard gate, restated here because all four issues' ACs already require it |

---

## 2. Bundle decomposition

**Naming decision:** the issues as filed use `waddles.community.barcitizen.*` / `waddles.integrations.spectrum` (implying `app_community` schema per #419 §1.4's derivation rule: `app_core` only if the app_id starts with `waddles.core.`). **This spec renames all three to the `waddles.core.*` namespace** — this is first-party PenguinTech engineering work built for a named customer, not a community-contributed or vendor bundle, so it belongs in `app_core` alongside the platform's own seeded bundles (`hub_api/cli/seed_core_bundles.py`'s system-actor pattern, #419 §1.4/§3.6). **ASSUMPTION — confirm with owner:** the issues will need their `app_id`/flag references updated to match (#484/#485/#486 bodies currently say `waddles.community.*`/`waddles.integrations.*`).

| Bundle | app_id | Schema (derived, #419 §1.4) |
|---|---|---|
| Bar Citizen read-only | `waddles.core.barcitizen.readonly` | `app_core.waddles_core_barcitizen_readonly` |
| Bar Citizen full | `waddles.core.barcitizen.full` | `app_core.waddles_core_barcitizen_full` |
| Star Citizen Spectrum connector | `waddles.core.starcitizen.spectrum` | connector — no per-app table; org/roster mapping table below lives in `app_core.waddles_core_starcitizen_spectrum` |

### 2.1 `waddles.core.barcitizen.readonly` (#484)

| Aspect | Detail |
|---|---|
| WIT world | `stage` (v1.0) — process-stage only, no action-stage; nothing is written back to the source |
| `storage.tables` columns | **Sync-cursor state only, not a duplicate event copy:** `id uuid pk`, `community_id uuid`, `source text`, `last_external_event_id text`, `last_polled_at timestamptz`, `last_error text nullable`. The actual event rows are written into the §4.1 canonical hub-api events table via a granted service call, not into this bundle's own table — keeping exactly one source of truth for event data instead of a bundle-local copy that can drift from the cross-community sync table |
| `storage.kv` | Not requested — no bundle-local ephemeral state needed |
| `net.http.fqdn` egress | `net.http.fqdn:<bar-citizen-source-host>` — **exact host TBD, open question 1.** If no API exists and scraping is the only path, flag as a `dangerous`-adjacent ToS risk (§8) and prefer a scheduled `platform.scheduled` poll with conservative rate limits over aggressive polling |
| Permissions | `storage.tables` (normal), `net.http.fqdn:<host>` (normal), `platform.scheduled` (normal) — explicitly **no** `chat.send:*`, no write/admin scope (matches #484 AC) |
| Flags | `waddles.bundle-barcitizen-readonly` (default OFF) |
| License tier | **ASSUMPTION:** Free — core read-only community-info surfacing, no gated capability per `critical-rules.md`'s tier table |

### 2.2 `waddles.core.barcitizen.full` (#485)

| Aspect | Detail |
|---|---|
| WIT world | `stage` (v1.0) — process-stage + action-stage (read/write) |
| `storage.tables` columns | `id uuid pk`, `community_id uuid`, `member_ref uuid` (tokenized, never raw PII — §5), `sync_status text`, `last_synced_at timestamptz`, `last_error text nullable` — **no Odoo identifier column.** The `odoo_partner_id ↔ member_ref` mapping lives only inside hub-api's identity service (§5.2); the bundle table never stores the raw partner id, since it's a direct key into Odoo's own PII-bearing customer record |
| `storage.kv` | Small cache for Odoo API rate-limit/backoff state, optional |
| `net.http.fqdn` egress | `net.http.fqdn:<odoo-host>` — **exact host TBD, open question 2.** Odoo's XML-RPC/JSON-RPC endpoints are typically same-host paths (`/xmlrpc/2/common`, `/xmlrpc/2/object`), so one FQDN entry covers all Odoo calls |
| Discord | Full read/write via the existing/soon-migrated Discord connector (#444) — `chat.send:discord` today, `connector.send:discord` once Discord's sender cuts over (#444 §3.1, "subsumes `chat.send` — not run in parallel") |
| Permissions | `storage.tables` rw, `net.http.fqdn:<odoo-host>` (normal), `chat.send:discord`/`connector.send:discord` |
| PII handling | The bundle only ever holds `member_ref`. Every Odoo call resolves `member_ref → odoo_partner_id` via a hub-api identity lookup at call time (§5.2) — the mapping is never cached in bundle storage or logged |
| Flags | `waddles.bundle-barcitizen-full` (default OFF) |
| License tier | **ASSUMPTION:** Professional — full read/write plus an external ERP integration exceeds the Free tier's "core product" scope per `critical-rules.md` |

### 2.3 `waddles.core.starcitizen.spectrum` (#486, re-specs #101)

| Aspect | Detail |
|---|---|
| WIT world | `waddle:connector@1.0.0` (#444) — `receiver` interface only (`on-connect`/`on-frame`/`on-heartbeat-due`/`on-disconnect`). **No `sender` interface** — one-way inbound only, per #101's explicit no-outbound-posting decision (RSI ToS/ban risk) |
| Transport | **Host-owned, not a bundle capability** — per #444, "bundles hold no sockets, the host owns every transport (websocket, IRC, webhook, polling, Pusher)." REST polling against RSI's community-documented (undocumented-by-RSI) Spectrum endpoints is configured on the connector's host-side transport registration, not requested via `net.http.fqdn` (that permission is for `stage`/`stage-v1_1` bundles calling out directly; a connector receiver never dials out itself — it only receives frames the host hands it via `on-frame`). Websocket for lobby streaming is an open question if the host's connector transport model doesn't support persistent connections yet — REST-polling fallback covers the gap either way (per #101). The polling target host is still declared in the connector's manifest for review/allowlisting purposes, exactly as a `net.http.fqdn` entry would be — it's the *permission mechanism* that differs, not the review rigor |
| `storage.tables` columns | `id uuid pk`, `community_id uuid`, `spectrum_org_id text`, `lobby_id text`, `channel_ref text` (WaddleBot interaction-channel mapping), `rsi_handle_ref uuid` (tokenized — §5), `last_event_at timestamptz` |
| RSI host status | **Public API status: none officially exists (#101) — this is reverse-engineered/community-documented, not a sanctioned API. Flag as ToS risk** (§8); Cloudflare/anti-bot protection on RSI's side is a likely operational failure mode, not just a legal one — health-check probe + fast kill-switch are mandatory per #101 AC, not optional hardening |
| Permissions | `connector.receive:starcitizen-spectrum` (dangerous, global-tier-only, core-only — same trust tier as Discord/Twitch connector receivers per #444 §3.1; **no** `connector.send:*` for this platform, matching the one-way design), `storage.tables` (normal) |
| Auth | RSI session token stored via the bundle's own encrypted credential capability (`docs/superpowers/specs/2026-09-28-connections-credentials-design.md`), per-community connection, never a shared credential, never logged |
| Flags | `waddles.bundle-spectrum` (default OFF, fast kill-switch per #101 AC) |
| License tier | **ASSUMPTION:** Professional — third-party platform integration beyond core, same reasoning as §2.2 |

---

## 3. Role management across servers/platforms (#497)

### 3.1 Role mapping model

New hub-api-owned table (cross-community config, not bundle-local — a role mapping spans multiple guilds/communities within one tenant, so it doesn't fit the one-table-per-bundle AppScoped model cleanly):

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `tenant_id` | `uuid` | Hard tenant boundary (§3.4) |
| `community_id` | `uuid` | Owning community |
| `condition_type` | `text check in ('membership','reputation_threshold','identity_verified')` | Grant trigger |
| `condition_value` | `jsonb` | e.g. `{"min_score": 700}` for reputation |
| `platform` | `text` | `discord`, future platforms |
| `target_guild_ref` | `text` | External guild/server id |
| `target_role_ref` | `text` | External role id |
| `created_by` | `uuid` | Admin who configured the mapping |
| `created_at` / `updated_at` | `timestamptz` | |

### 3.2 Sync direction and conflict rules

- **One-way, internal-truth model:** WaddleBot's own condition state (membership, reputation score, verification status) is authoritative; platform role state is always driven to match it, never read back to change internal state.
- **Conflict rule:** last-internal-write-wins per `(member, platform, target_role_ref)`. Before issuing a grant/revoke call, the host consults its own structural guild-state cache (#444's "host maintains a non-PII structural guild-state cache" for permission checks) to skip redundant API calls when the member already holds/lacks the role.
- **Idempotency:** each sync operation carries a dedupe key `(member_ref, target_role_ref, action)`; the host's own cache plus Discord's own PATCH-role-list semantics make repeated syncs safe to retry.

### 3.3 Discord connector permissions needed

Built on #444's connector layer (per the issue's own instruction — "not a bundle-local Discord API client"):

- `connector.send:discord` for role grant/revoke — #444's `sender` spec enumerates `role.create`/`role.edit`/`role.delete` (guild-level role CRUD) but **not** a per-member grant/revoke action kind. **ASSUMPTION — confirm with #444's owners:** this spec proposes two new sender action kinds, `role.member.grant` and `role.member.revoke`, following the same `build-request` pattern as the existing role CRUD kinds.
- Permission-check reads use #444's host-maintained structural guild-state cache (roles/channels/permission overwrites — explicitly non-PII, §"Permissions checks" in #444, exact query API still an open item there).
- No new WIT interface needed beyond the two action kinds above — this rides entirely on `connector@1.0.0`'s existing `sender` shape.

### 3.4 Rate limits

- Per-call: existing relay `UsageBatcher` limits (#444/#419 wit-v1.1 §2) apply to every `role.member.grant`/`revoke` call, same as any other relay action.
- Bulk apply/remove: capped at 50 members per operation. **Enforcement is host-side, not bundle-trusted** — the bundle may batch its own calls, but the host's `UsageBatcher` rate limiter is the actual gate on every individual `role.member.grant`/`revoke` call regardless of how the bundle paces them; a bundle cannot bypass the cap by issuing calls faster than its own batching implies. Defense in depth against a compromised or buggy bundle fanning out into a Discord rate-limit storm.

### 3.5 Audit logging

One audit event per role change: `(member_ref, target_role_ref, platform, trigger condition_type, actor, timestamp, result)`. Reuses the existing hub-api audit-log sink (same one `bundle_capability_gate::authorize()` already writes denial/grant events to, #419 §5). Satisfies #497's AC ("bulk apply/remove ... completes with an audit log entry per change").

### 3.6 Tenant boundary

**Hard constraint, not UI-only** (#497 AC is explicit on this): `target_guild_ref` is resolved server-side against the invoking community's own tenant — never taken from bundle input. The capability gate's `ResourceRef` derivation (#419 §5.2, `AppScoped`, server-side derivation) rejects any role-mapping row or sync operation whose resolved guild doesn't belong to the same tenant as the community that owns the mapping. **Regression test required** asserting cross-tenant role sync is rejected at the gate layer, not just filtered in the UI — this is called out explicitly in #497's own acceptance criteria.

### 3.7 Scope checks via the capability gate (#428)

Every `role.member.grant`/`revoke` invocation goes through `CapabilityGate::authorize()` (#428) with a host-constructed `InvokeScope` (never guest-influenced, #419 §5.1 Gemini condition 1) carrying the resolved tenant/community/guild triple. `dangerous`-risk classification applies (guild role management crosses a trust boundary per #419's risk-level rule) — reviewed at all three consent tiers (§3 of #419) on first install, not auto-approved.

### 3.8 Cross-platform stretch goal

If Bar Citizen confirms a genuine cross-*platform* need (open question 4), the same `role_mappings` table already supports multiple `platform` values per community — the only new work is a second platform's connector gaining equivalent `role.member.grant`/`revoke` sender action kinds. No schema change needed to extend beyond Discord.

---

## 4. Event sync (#96)

### 4.1 Event model

Canonical events live in **hub-api**, not bundle-local `storage.tables` — an event synced across multiple communities on a shared platform instance needs one shared source of truth for dedup, not N private per-bundle copies that drift. New/extended hub-api table:

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | Canonical event id |
| `tenant_id` | `uuid` | |
| `source_platform` | `text` | `discord`, `spectrum`, `calendar`, etc. |
| `external_event_id` | `text` | Natural key with `source_platform` for dedup/idempotency |
| `title` / `description` | `text` | |
| `starts_at` / `ends_at` | `timestamptz` | **Always stored UTC**; rendered in the requesting community's configured timezone at read time (§4.4) |
| `community_tags` | `text[]` | #96's `[Community_Name]` tags, parsed/stored structured — not re-parsed from description text on every read |
| `tag_scope` | `text check in ('tagged','unfiltered')` | Untagged-event behavior, configurable per community per #96 AC |
| `created_by_community_id` | `uuid nullable` | Owning community if WaddleBot-originated |
| `updated_at` | `timestamptz` | |

### 4.2 Tags

- Format unchanged from #96: `[Community_Name]` appended to the platform-facing description; multiple tags supported for joint events.
- **Parsing is host/hub-api-side, not per-bundle** — matches #96's own architecture note ("sync filter applies at the router/processor layer before events reach individual community handlers"). Tag injection/extraction lives in a shared library, called once per sync, not duplicated per platform integration.
- Tags are stripped when rendering inside WaddleBot's own UI (community context already known) and re-injected only on outbound platform writes.
- **Loop prevention:** every write to a canonical event row records its `updated_at` and the writing source. An inbound sync from a platform is compared against the event's own last-known outbound-write fingerprint before re-parsing tags — if the inbound payload is WaddleBot's own most recent outbound write echoing back, it's a no-op, not a new upsert. This is required to prevent a re-tag → re-sync → re-tag cycle (duplicate `[Community_Alpha] [Community_Alpha]` tags) between §4.2's strip/re-inject step and inbound polling.

### 4.3 Platforms covered

| Platform | Direction | Mechanism |
|---|---|---|
| Discord scheduled events | Inbound + outbound | Connector `sender` action kinds `scheduled_event.create`/`edit`/`delete` (#444 §"Roles, channels, scheduled events") for outbound; `receiver.on-frame` for inbound gateway events |
| Calendar (Google/ICS) | **ASSUMPTION — confirm with owner:** which calendar provider(s) Bar Citizen actually needs (open question 5) | New connector or existing calendar integration, TBD pending answer |
| Star Citizen Spectrum | Inbound only | §2.3's connector — org events flow in, never out (matches #486's one-way design) |

### 4.4 Timezone handling

All storage is UTC (`timestamptz`). Each community has a configured display timezone (existing community settings); rendering happens at the API/webui boundary, never baked into stored values — avoids the classic DST-drift bug from storing localized times.

### 4.5 Dedupe and idempotency

Natural key `(source_platform, external_event_id)` maps to one canonical `id`. Sync is an upsert on that key; field-level conflicts (e.g. two platforms disagree on title) resolve last-write-wins per field with the writing source recorded, not silently dropped — an audit trail of which sync touched which field last is required for troubleshooting cross-platform drift.

---

## 5. PII

**Invariant (restated from `critical-rules.md` PII Tokenization and #419 §10.1):** bundles and connectors receive only UUIDs for actor/target identity — never raw usernames, handles, or emails. Star Citizen/RSI handles are PII under this rule, exactly like a Discord username.

### 5.1 RSI handle lookup/verification flow

1. A user runs a link command (`/link-spectrum <handle>`, exact UX TBD) or an admin configures it.
2. The bundle/connector calls hub-api's internal gRPC identity service (extends the `hub_user_identities` mapping already used for platform identities, per the #449 fold-in) — `IdentityService.LinkExternalIdentity(platform="starcitizen_spectrum", external_id=<rsi_handle>, hub_user_uuid=<uuid>)`.
3. Hub-api persists the mapping and returns the UUID. The Spectrum connector bundle (§2.3) is the one component in this design that legitimately sees the raw RSI handle — it arrives via the connector's own `on-frame` host delivery, inside the same privileged, first-party, signed trust tier #444 §3.3 already grants Discord/Twitch connectors for raw actor data. The connector's normalization step calls `identity.lookup`/`LinkExternalIdentity` immediately, **before** the event is re-emitted downstream to any `stage`/`stage-v1_1` process/action-stage bundle — those bundles (including §2.2's full bundle) only ever see the resolved UUID, never the raw handle. This mirrors the fix #419 §10.1 mandates for `svc_ingest::normalize.rs` on existing platforms.
4. An **unlinked** handle (seen in roster/activity data before a user links it) gets a stable, non-identifying placeholder UUID scoped to the org — never the raw handle — until claimed via step 1. This lets roster sync and activity tracking function pre-verification without ever storing a raw handle outside hub-api.
5. **Detokenization** (e.g. showing a handle back to a community admin in webui) happens only inside hub-api's PII boundary, or via the connector's `identity.lookup` host import gated by `connector.pii.read` (#444 §3.3 — dedicated `waddles_connector_pii_reader` RO-replica role, global-approved, core-only, never vendor-grantable).

### 5.2 Odoo member records (#485)

Same pattern: an Odoo `partner_id` maps to a `member_ref` UUID via the same identity service, resolved once at sync time; `storage.tables` and logs carry only `member_ref`, never the Odoo partner's name/email.

---

## 6. Dependencies and sequencing

| Dependency | Issue | Gates |
|---|---|---|
| Bundle db capability + DDL generator | #430, #498 | Any `storage.tables` bundle (§2.1–2.3, §3.1's role_mappings, §4.1's events table) |
| Capability gate crate + wiring | #428, #433 | Every permission check in §2–§4 |
| Grant storage / 3-tier consent flow | #432 | Install-time consent for `dangerous` permissions (`connector.receive:*`, `net.http.fqdn`, Discord role management) |
| Egress hardening (guard, three-category perms, egress-proxy, CNI) | #459, #463, #465, #468, #469 | Every `net.http.fqdn` egress target (§2.1's source, §2.2's Odoo, §2.3's Spectrum) |
| Connector world + per-component linker | #461 | Spectrum connector (§2.3), Discord role/event sender kinds (§3.3, §4.3) |
| Connector PII reader role | #464 | RSI handle detokenization (§5.1 step 5) |
| Connector bundle design + migration tracking | #444, #451 | Foundation for Spectrum receiver and Discord sender role/event action kinds |
| Identity fold into hub-api | #449 | RSI handle linking, Odoo member tokenization (§5) |

### Critical path

```
#430/#498 (db)  ─┐
#428/#433 (gate) ─┼─► §2.1 readonly bundle (simplest — no egress-dangerous perms, no connector)
#459/#465/#468/#469 (egress) ─┘
       │
       ▼
#444 (connector base, already merged as design) + #449 (identity fold)
       │
       ├─► §2.3 Spectrum connector (#486) ──► needs #461 (connector world), #464 (PII reader)
       │
       └─► §3 Role management (#497) ──► needs #444's new sender action kinds (§3.3, ASSUMPTION)
       │
       ▼
§2.2 Bar Citizen full bundle (#485) — depends on Odoo egress (#459/#465) + role/event patterns proven above
       │
       ▼
§4 Event sync (#96) — cross-cutting; db/gate-ready work can start in parallel, but Discord scheduled-event
   sender kinds depend on #444's connector sender cutover
```

### Slice plan (agent-sized, ≤30 min each)

| Slice | Work | Depends on |
|---|---|---|
| A1 | `waddles.core.barcitizen.readonly` manifest + table DDL + poll loop against the confirmed source (open question 1) | #430/#498, #459/#465 |
| A2 | Role mapping table + hub-api CRUD (membership/reputation/verification conditions, no sync yet) | #430/#498 |
| A3 | Event canonical table + tag parse/inject shared library (no platform wiring yet) | #430/#498 |
| B1 | Discord sender `role.member.grant`/`revoke` action kinds (pending #444 owner confirmation, §3.3) | #444, #461 |
| B2 | Discord sender `scheduled_event.create`/`edit`/`delete` wiring for event sync outbound | #444, #461 |
| B3 | Spectrum connector receiver: org linking + roster/event inbound parsing | #444, #461, #449 |
| B4 | RSI handle link/verify flow + unlinked-handle placeholder UUIDs | #449, #464 |
| C1 | Role sync engine: condition evaluation → grant/revoke dispatch, dedupe cache, audit log | A2, B1 |
| C2 | Event sync engine: tag filtering, dedupe/idempotency, timezone rendering | A3, B2, B3 |
| D1 | Bar Citizen full bundle: Odoo XML-RPC client + member tokenization | #449, egress deps |
| D2 | Bar Citizen full bundle: Discord read/write wiring reusing B1/C1 patterns | C1, D1 |
| E1 | Cross-tenant role-sync regression test + capability-gate denial test (§3.6) | C1 |
| E2 | Spectrum health-check probe + kill-switch flag wiring (#101 AC) | B3 |

---

## 7. Testing

| Layer | Coverage |
|---|---|
| Unit | Tag parse/inject library; role condition evaluation; timezone conversion; RSI handle tokenization/placeholder logic; Odoo response mapping — 90%+ per `critical-rules.md` |
| Integration | Capability-gate denial paths (cross-tenant role sync, cross-tenant event read); db capability writes against real Postgres; connector `identity.lookup` against a seeded RO replica |
| E2E | **Every standard PR-gate run mocks Discord** (gateway frames + REST responses) — masked CI secrets are unavailable to fork-originated PR runs in most CI setups, so a live-bot test cannot be a hard PR gate. The **live Discord round trip runs on a scheduled/nightly job against the release branch only** (never fork PRs), using the **separate CI-only bot** (dedicated disposable test guild, bot token as a masked CI secret per `critical-rules.md` Token & Secret Hygiene — never the production bot, never a synthetic-forged envelope), serialized (mutex/lock, one run at a time) against that guild to avoid concurrent-run role/event collisions: role grant/revoke visible in the test guild, scheduled event create/update relayed. Spectrum connector is tested against a fixture/mock only, in both PR and nightly runs — no live RSI calls anywhere in CI (undocumented API, ToS/Cloudflare-block risk) |
| Coverage | 90%+ lines/branches/functions/statements, gate blocks below threshold |
| OTel | `role_sync.grant`/`role_sync.revoke`/`role_sync.denied` counters; `event_sync.upsert`/`event_sync.conflict` counters; histogram on Spectrum poll latency and Odoo call latency; spans across the identity-lookup and capability-gate calls; structured logs (penguin logging, not hand-rolled) for every grant/deny/sync decision at INFO, verbose branch detail at DEBUG |

---

## 8. Security review notes

| Threat | Mitigation |
|---|---|
| Role-escalation via sync | Role grants are `dangerous`-risk, reviewed at all 3 consent tiers (#419 §3); `InvokeScope` host-constructed only (no guest-supplied scope); cross-tenant guild resolution rejected at the gate (§3.6) with a regression test |
| Cross-tenant leakage | Every `storage.tables` row and role-mapping row is tenant-scoped at the schema/gate layer, never bundle-trusted; event canonical table filters by `tenant_id` on every read |
| Scraping abuse / ToS violation | Spectrum connector is read-only by design (no `connector.send`), rate-limited, health-checked, and behind a fast kill-switch (#101 AC); Bar Citizen read-only source (§2.1) flagged for ToS confirmation before any scraping fallback is built (open question 1) |
| Egress abuse | All three bundles' `net.http.fqdn` targets are explicit allowlist entries reviewed at install (#419 §1), never wildcards or IP literals; private-IP egress is not requested by any bundle in this design |
| PII leakage via connector | RSI handles and Odoo member data tokenize to UUID before crossing into `storage.tables`/logs/telemetry; `connector.pii.read`/`identity.lookup` stays global-approved, core-only, RO-replica-scoped (#444 §3.3) |
| Credential exposure (Odoo, RSI session token) | Stored via the connections/credentials broker (`docs/superpowers/specs/2026-09-28-connections-credentials-design.md`), never logged, never bundle-visible as plaintext |

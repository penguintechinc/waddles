# Bar Citizen v3.0 Design — Greenfield Bundle Build

**Status:** Draft — objectives need owner confirmation before implementation starts.
**Date:** 2026-09-29
**Scope:** New app bundles (`waddles.core.barcitizen.*`, `waddles.core.starcitizen.*`), cross-server role management (#497), event sync (#96), plus the hub-api/data-plane pieces they depend on.
**Driver:** 2026-09-29 release-plan review — Bar Citizen (2nd-biggest customer) has never received any features, v2 included. Per owner scope notes on #484/#485/#486/#497/#96 (all 2026-09-29): **this is a greenfield v3 build against Bar Citizen's objectives, not a parity port.** There is no legacy behavior to preserve; acceptance criteria in the issues themselves are placeholders pending this spec.
**Builds on (read, reconciled, not re-litigated):** `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md` (#419, MERGED — permission catalog, capability gate, `storage.tables` schema derivation), `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md` (#415/#430/#498 — one-table-per-bundle DDL), `docs/superpowers/specs/2026-09-28-connector-bundles.md` (#444 — `waddle:connector@1.0.0` WIT world, `connector.receive:<platform>`/`connector.send:<platform>`/`connector.pii.read`, `waddles_connector_pii_reader` RO role), `docs/reference/bundle-network-permissions.md` (#466 — three-family egress risk model), `docs/superpowers/specs/2026-09-28-v3.0-python-removal-plan.md` (wave/slice format this doc follows).
**Review:** Gemini second-opinion pass (critical perspective) against this doc's own draft and the platform's PII/capability-gate/connector invariants. Findings folded in: removed a raw Odoo-partner-id column that leaked PII into bundle storage (§2.2, since superseded — see Update below), corrected the Spectrum connector's transport model to match #444's host-owns-all-sockets design instead of requesting `net.http.fqdn` on a receiver-only bundle (§2.3), eliminated a duplicate event-data table between the read-only bundle and the §4.1 canonical events table (§2.1), clarified where RSI-handle tokenization actually happens relative to connector `on-frame` delivery (§5.1), made bulk role-op rate limiting host-enforced instead of bundle-trusted (§3.4), added a tag re-injection loop-prevention rule (§4.2), and split Discord E2E into mocked-PR-gate + nightly-live-scheduled-job to avoid the fork-PR-secret and shared-test-guild-concurrency problems (§7).

**Update (2026-09-29, owner decisions this session):**
1. **No Odoo — Bar Citizen replaced Odoo with Wix.** §2.2 is rewritten around the Wix platform (REST/Headless APIs, Members, Events, Pricing Plans, webhooks). #95 (Wix platform integration) is re-milestoned to v3.0.x and folded into this design; #485/Odoo is superseded.
2. **v3.0 role sync is Twitch↔Discord (cross-platform), narrowed scope:** subscriber tiers (T1/T2/T3, Twitch→Discord only — subs are read-only on Twitch) and moderators (default Twitch→Discord; Discord→Twitch mod assignment is an **ASSUMPTION** pending owner confirmation, flagged as a privilege-escalation vector — **superseded by Update 2 below: direction is now a normal per-mapping opt-in, not an approval gate**). VIP and follower sync are explicitly **removed** from v3.0 scope. §3 is rewritten; cross-guild sync is retained as in-scope (same mechanism), no longer framed as a stretch goal.
3. **Shared guilds:** an owner requirement surfaced that more than one Bar Citizen community may map to the same Discord guild. §3.0 (new) covers routing, role ownership, and the tenant boundary for this case; a platform gap was found (hub-api's `ingest_sources` table enforces one community per guild today) and filed as a new dependency (see §6).
4. **Second Gemini second-opinion pass** (critical perspective, run against §2.2/§3/§5.2/§8's Wix, Twitch↔Discord, and shared-guild changes): 5 findings returned, folded in below — (a) native/manual Discord role assignment can put a user into another community's role state without ever going through `managed_roles`'s write-path check, so any **read** of role state used to drive a downstream sync (e.g. Twitch mod status) must re-verify ownership, not just trust the role's presence (§3.0, new caveat); (b) a **race condition**: registering a pre-existing (not bot-created) Discord role into `managed_roles` needs stronger provenance than "first admin to ask" — an admin from community A could harvest and pre-claim community B's role ids (§3.0, new caveat, mitigation open as a build-time item); (c) the Discord→Twitch moderator confirmation step is now **mandatory, not optional** (§3.2, fixed directly); (d) Wix webhook replay protection is now stated explicitly as part of the same host-side, pre-parse verification step, not a separate later check (§2.2, fixed directly); (e) rendering a Wix/Twitch/Discord member's name anywhere downstream (e.g. a Discord welcome message) must go through the existing `connector.pii.read`/`identity.lookup` detokenization gate (§5.1 step 5) — never a value cached in `storage.tables` — with the existing 5-minute per-replica cache (#444 §3.4) as the load-bounding mitigation for the lookup-per-render pattern this implies (§5.2, fixed directly). (a) and (b) are flagged as **build-time hardening items**, not resolved by a doc change alone — see §3.0 and §8.

**Update 2 (2026-09-29, second follow-up — owner decisions):**
1. **Multi-tenant guild pairing, replacing "one guild : one tenant":** a Discord guild is a shared external resource and may be paired with more than one tenant. §3.0 is rewritten as a consent-based pairing model — a guild authority (Discord user verified via OAuth to hold Manage Server) must explicitly consent to each tenant pairing, revocable by either side; exclusive channel binding, per-tenant `managed_roles` ownership, Discord role-hierarchy enforcement, and guild-authority-only pairing/ownership visibility (never member data) replace the earlier single-tenant assumption. #500 is rescoped accordingly (commented).
2. **Every sync is opt-in, no default-active direction:** §3.1/§3.2 add explicit `sync_direction`/`enabled` columns to `role_mappings` — an admin must pick a direction and turn a mapping on. Subscriber tiers remain Twitch→Discord only (physically constrained; the UI must say so). Moderator sync can run in any direction, including Discord→Twitch, which is no longer "pending owner confirmation" — it's a normal per-mapping choice, still carrying the mandatory identity-link confirmation and audit as safety controls. Bidirectional mappings get an explicit loop-prevention (origin-marker) and conflict (last-write-wins) rule, §3.2. Event sync (§4) follows the same opt-in-per-mapping rule.
3. **Scale — 14 guilds, ~12 communities, N:M mapping:** new §3.10 covers Bar Citizen's real topology (11 regional + 3 international guilds; communities that span multiple guilds and guilds that host multiple communities), per-guild rate-limit isolation, a per-`(tenant,guild)` queue-lane batch design, resumable bulk backfill with progress, and per-guild OTel metric labels.
4. **Third Gemini second-opinion pass** (critical, against the multi-tenant pairing + directional-sync changes specifically): 5 findings, all folded in — (a) authorization must be **state-based, not actor-attribution-based**, since Discord's Audit Log can't reliably or promptly identify who made a native role change (§3.0, fixed — also fully closes the 2nd review's role-harvesting-race item via the guild-authority approval gate); (b) a **global bot-token rate limit** sits above the per-guild lanes, since Discord's real limit is per-token, not per-guild, and one guild's bulk traffic could otherwise starve the other 13 (§3.6/§3.10, fixed); (c) bidirectional loop-prevention now matches on a **monotonic per-mapping sequence number**, not a wall-clock window, since delivery jitter/clock drift could otherwise miss an echo or misorder rapid transitions (§3.2, fixed); (d) a guild authority's consent must be **periodically re-verified**, not trusted indefinitely, since a demoted/departed/compromised authority is invisible to the bot without a check (§3.0, new build-time item); (e) pairing **revocation must actively unwind already-granted roles**, not just stop future syncing, to avoid orphaned grants outliving the pairing that authorized them (§3.0, new build-time item).

---

## Open questions for Bar Citizen (owner takes these to the customer)

1. **Event/data source for the read-only bundle (#484):** does Bar Citizen publish events via a public API (Discord, a community calendar, a website with structured data), or would ingestion require scraping a page with no API? Scraping carries ToS/fragility risk (§2.1) — need the actual source(s) before `net.http.fqdn` targets can be finalized.
2. **Wix app scope (#95, replaces the former Odoo question):** can Bar Citizen create a scoped Wix OAuth app / API key for their site, and which Wix apps do they actually use — Members, Events, Stores, Pricing Plans (memberships/paid tiers), some combination? This decides which webhook subscriptions and outbound scopes §2.2 actually needs.
3. **Spectrum organization details (#486):** confirm the RSI org handle/URL and whether Bar Citizen accepts the one-way-sync-only limitation (no posting back to Spectrum — RSI ToS risk, per #101).
4. **Exact community↔guild map (#497, new — §3.10):** Bar Citizen has ~14 Discord guilds (11 regional + 3 international) and ~12 communities in an N:M relationship. Need the actual map: which communities own which regional guild(s), which communities have a presence in which international guild(s), and — for role/event mirroring — a **hub-and-spoke diagram**: which servers mirror which (e.g. do the 3 international guilds mirror a subset of regional roles/events, and do regional guilds ever mirror from each other, or is it strictly hub→spoke from the international guilds)? This sizes the real fan-out (§3.10) and which pairings/mappings actually need building first.
5. ~~Discord→Twitch moderator assignment~~ — **resolved:** this is now a normal per-mapping opt-in an admin can enable in any direction (§3.2), not a scope question; the mandatory identity-link confirmation and audit remain as safety controls regardless.
6. **Event sync (#96):** which platforms beyond Discord does Bar Citizen need events synced to (a public calendar? Spectrum inbound only?), and what community tag alias do they want (defaults to display name)?
7. **License tier:** confirm whether Bar Citizen's contract already entitles them to Professional/Enterprise-gated capabilities (cross-server role sync, audit logging) or whether this work should ship Free-tier to match "no features ever delivered" — see §3 tier table, all marked ASSUMPTION.

---

## 1. Objectives

| # | Objective | Source | Notes |
|---|---|---|---|
| 1 | Ship a read-only Bar Citizen bundle surfacing community events and public informative data into connected platforms | #484 (restores closed #14) | Read-only enforced via the capability gate (#428/#433), not bundle-local logic |
| 2 | Ship a full Bar Citizen bundle: read/write Discord integration + Wix sync (members, events, pricing plans) | #485 (restores closed #15), #95 | **Owner decision 2026-09-29: Bar Citizen replaced Odoo with Wix — no Odoo design in this spec.** #15's original "different external WaddleDB connection for Odoo" is moot; this bundle instead composes #95's Wix integration scope with #485's Discord read/write, routed through Wix's own REST/webhook APIs (§2.2), never a raw external-DB connection |
| 3 | Ship a Star Citizen Spectrum connector: one-way (Spectrum→WaddleBot) message/event/roster sync | #486 (re-specs open #101) | No outbound posting, ever — RSI ToS risk. #101 itself stays untouched/unmilestoned as an architecture-superseded reference |
| 4 | Cross-*platform* (Twitch↔Discord) role management: sync/assign roles driven by subscriber tier and moderator status, with bulk apply and audit logging, including cross-guild and shared-guild cases | #497 | **Owner decision 2026-09-29: v3.0 scope is Twitch↔Discord role sync**, not cross-guild-only — see §3 (rewritten). Scope is narrowed to subscriber tiers (T1/T2/T3) and moderators only; VIP and follower sync are out of scope. Cross-Discord-guild sync (including shared guilds, §3.0) is included as the same mechanism, not a stretch goal |
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

### 2.2 `waddles.core.barcitizen.full` (#485, folds in #95 — Wix, replaces Odoo)

**Owner decision 2026-09-29: no Odoo. Bar Citizen replaced Odoo with Wix.** #95's scope (Wix platform integration) is what this bundle actually needs; #15/#485's original Odoo/ERP framing is dropped entirely — there is no Odoo design anywhere in this spec.

**Which Wix data the bundle needs, and why (scoped down from #95's full bi-directional catalog):**

| Wix area | Used for | Direction | Notes |
|---|---|---|---|
| Members (Wix REST Members API) | Map a Wix site member to a `member_ref` UUID; surface membership status to Discord (e.g. a "Member" role) | Inbound (webhook) + outbound lookup | Only status/role-relevant fields are read — no need for full profile sync |
| Events (Wix Events API) | Feed Bar Citizen's Wix-hosted events into the §4 canonical event-sync table, same as Discord/Spectrum sources | Inbound only for v3.0 | Outbound (WaddleBot-authored events → Wix) is **not** in v3.0 scope — #95's bi-directional Events scope is deferred; this bundle only consumes |
| Pricing Plans (memberships/paid tiers) | Optional: map a paid Wix plan to a Discord role, same pattern as Twitch sub-tier sync (§3) | Inbound (webhook) | Only pursued if Bar Citizen actually sells Wix membership tiers — open question 2 |
| Stores / orders | **Out of scope for v3.0.** #95 lists Stores; no Bar Citizen use case identified yet, and orders carry payment-adjacent PII this design has no reason to take on | — | Revisit only if Bar Citizen names a concrete need |
| Groups, Blog, Bookings | **Out of scope for v3.0** — #95 lists these for the general Wix integration issue, but nothing in Bar Citizen's stated objectives needs them | — | Left for #95 itself if/when re-scoped beyond Bar Citizen |

**Auth:** a scoped Wix OAuth app (preferred) or a scoped Wix API key, held **server-side only**, in the platform-credentials Secret (the same broker as every other connection credential, `docs/superpowers/specs/2026-09-28-connections-credentials-design.md`) — **never embedded in a bundle**, matching `client.md`'s "never call third-party APIs directly from client" and this platform's existing credential-broker pattern (§2.3's RSI session token, §5.2's former Odoo key). Per Wix's own auth model (`dev.wix.com/docs/overview/auth-permissions`), the app/key is granted only the scopes it uses (Members read, Events read, Pricing Plans read) — never broad "manage site" scope. Scopes are declared at app setup and granted by the Bar Citizen site owner at install (OAuth) or configured at key creation (API key) — resolved by open question 2.

**Inbound (Wix → WaddleBot):** Wix delivers platform events as **webhooks** — a JWT-encoded payload (`eventType`, `instanceId`, `data`, `identity`) per `dev.wix.com/docs/api-reference/articles/work-with-wix-apis/platform/about-the-structure-of-webhooks`. This lands as a **new webhook `Transport` family member** on the ingest path, per #444 §2's existing Webhook row (Twitch EventSub / Slack Events API / generic `/hooks/v1/{id}`): **signature verification happens host-side, on the raw request bytes, before any JSON parse and before any connector bundle is invoked** — identical ordering to EventSub/Slack, using Wix's own JWT-signature verification (the webhook JWT is signed by Wix, verified against Wix's public key/the app's webhook secret, per the same article). **Replay protection is part of the same pre-parse verification step, not a later check:** the JWT's own `iat`/`exp` claims are validated against an explicit replay window (exact window TBD against Wix's current webhook documentation at implementation time, matching the discipline #444 §2 already applies to EventSub's 10-minute and Slack's 5-minute windows) before the payload is handed to the connector at all — a signature-valid-but-expired/replayed webhook is rejected at the same host boundary, never passed through on signature validity alone (second Gemini review finding). A `waddles.core.starcitizen.spectrum`-style connector receiver (`waddle:connector@1.0.0`, `receiver` interface only for this bundle's inbound path) normalizes `member`/`event`/`pricing-plan` webhook payloads into `normalized-event`s; the bundle's own `member_ref`/tokenization step (§5.2) runs inside `on-frame`, before anything reaches process-stage.

**Outbound (WaddleBot → Wix, where needed at all):** any outbound Wix REST call (e.g. looking up a member's current plan) goes through the **egress proxy**, with an explicit `net.http.fqdn:<bar-citizen-wix-site-host or www.wixapis.com>` grant — exact host(s) TBD pending open question 2 (a Wix Headless/REST integration typically calls `www.wixapis.com` with the site's `instanceId`, not a customer-specific host, so the egress grant is likely a fixed PenguinTech-side allowlist entry rather than a per-customer FQDN — confirmed once the OAuth app is stood up).

| Aspect | Detail |
|---|---|
| WIT world | `waddle:connector@1.0.0` for the inbound webhook path (receiver only, per #444 §2's Webhook transport family) + `stage` (v1.0) process-stage/action-stage for Discord read/write and any outbound Wix lookups |
| `storage.tables` columns | `id uuid pk`, `community_id uuid`, `member_ref uuid` (tokenized, never raw PII — §5.2), `wix_site_id text` (the Wix `instanceId`/site id — not PII), `sync_status text`, `last_synced_at timestamptz`, `last_error text nullable` — **no Wix member name/email column, ever** |
| `storage.kv` | Small cache for Wix API rate-limit/backoff state, optional |
| `net.http.fqdn` egress | `net.http.fqdn:<wix-api-host>` (normal) — see Outbound note above for exact host(s), open question 2 |
| Discord | Full read/write via the existing/soon-migrated Discord connector (#444) — `chat.send:discord` today, `connector.send:discord` once Discord's sender cuts over (#444 §3.1) |
| Permissions | `connector.receive:wix` (dangerous, global-tier-only, core-only — same trust tier as the Spectrum/Discord connector receivers, §2.3) for inbound webhooks; `storage.tables` rw; `net.http.fqdn:<wix-api-host>` (normal) for outbound lookups; `chat.send:discord`/`connector.send:discord` |
| PII handling | The bundle only ever holds `member_ref`. Wix member email/name arrives in the raw webhook payload delivered to the connector's `on-frame` (same privileged, first-party trust tier #444 §3.3 grants Discord/Twitch/Spectrum receivers, §5.1); the normalization step resolves/creates `member_ref` via hub-api's identity service **before** re-emitting downstream — no `stage`/`stage-v1_1` bundle, including this one's own action-stage half, ever sees a raw Wix name or email |
| Webhook signature verification | Host-side, on raw bytes, before parse — Wix's JWT webhook signature checked against the app's registered public key/secret, same ordering and replay-window discipline as #444 §2's EventSub/Slack rows |
| Flags | `waddles.bundle-barcitizen-full` (default OFF) |
| License tier | **ASSUMPTION:** Professional — full read/write plus an external platform (Wix) integration exceeds the Free tier's "core product" scope per `critical-rules.md` |

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

## 3. Role management: Twitch↔Discord (#497)

**Owner decision 2026-09-29: v3.0's role sync is Twitch↔Discord (cross-platform), not cross-guild-only** — cross-guild (multiple Discord servers) remains included as the same mechanism, not a separate stretch goal.

**Owner correction 2026-09-29 (scope narrowing):** the roles modeled are **only** (a) subscriber tiers (T1/T2/T3) and (b) moderators. **VIP and follower sync are removed from v3.0 scope entirely** — no `channel:manage:vips` scope, no VIP slot-limit handling, no follower condition type.

**Owner decision 2026-09-29 (second follow-up): a Discord guild may be paired with more than one tenant.** This supersedes the earlier "one guild : one tenant" ASSUMPTION — §3.0 below is rewritten as a consent-based pairing model instead. **Owner decision: every sync is opt-in, no default-active direction** — §3.2 is rewritten accordingly.

### 3.0 Multi-tenant guild pairing (a shared external resource, not owned by any one tenant)

**Owner requirement 2026-09-29 (revised):** a Discord guild is a **shared external resource** that may be paired with more than one tenant, each running one or more communities in it — not just multiple communities within a single tenant as first scoped.

**Current-model gap (found by inspection, not assumed):** hub-api's `ingest_sources` table — the row that maps a platform+external id (a Discord guild, a Twitch channel) to a community — has `UNIQUE (tenant_id, platform, source_id)` (`alembic/versions/0020_ingest_sources_rbac.py:84`), and `ingest_source_service.create_source()`'s pre-insert existence check (`hub_api/services/ingest_source_service.py`, the `existing = await install_dal(...)` query) filters only on `(tenant_id, platform, source_id)` — **not** `community_id` — so a second community (same tenant *or* a different one) cannot register the same guild today; the second `create_source()` call 409s. **The platform does not support N communities — same-tenant or cross-tenant — per guild yet.** This is new work, not a reinterpretation of existing capability — filed as #500 (rescoped below), not designed further in this document beyond the schema/routing shape it implies.

**Pairing model — consent-based, not admin-fiat:**

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `guild_ref` | `text` | The Discord guild id — this row is keyed by guild, **not** by tenant, since one guild can have many pairings |
| `tenant_id` | `uuid` | The tenant this pairing grants guild access to |
| `guild_authority_discord_user_id` | `text` | The Discord user who approved the pairing, verified via Discord OAuth to hold **Manage Server** in this guild at approval time |
| `status` | `text check in ('pending','active','revoked')` | |
| `consented_at` / `revoked_at` | `timestamptz nullable` | |
| `revoked_by` | `text nullable` | Either a guild-authority Discord user id or a tenant admin's `hub_user_uuid` — see revocation below |
| `created_by` (`hub_user_uuid`) / `created_at` | | The tenant admin who proposed the pairing |

**Lifecycle:** a tenant admin *proposes* a pairing (`status='pending'`) naming the guild; it only becomes `active` once a **guild authority** — a Discord user verified via Discord OAuth to currently hold Manage Server in that guild — explicitly consents, out-of-band from the proposing tenant (a Discord-side approval flow, exact UX TBD). **Revocable by either side:** the guild authority can revoke a pairing at any time (removing that tenant's access to the guild entirely), and the tenant can revoke its own pairing (opting out). Revocation is immediate and audited; it stops all further sync for that tenant's `role_mappings`/`managed_roles` rows scoped to that guild (existing rows are disabled, not silently orphaned — an admin-visible "pairing revoked" state, not a crash or a stuck sync).

**Routing (message/command/interaction → community), now resolved across all paired tenants, not just one:**

1. **Channel-level binding first** — if the interaction's channel (or its parent category) is explicitly bound to one community, use it. **Channel binding is exclusive across every tenant paired with the guild** — one channel maps to at most one community, full stop; a second tenant's bind request for an already-bound channel is rejected at bind time, not just at routing time.
2. **Explicit community tag/prefix** — a command argument or a per-channel default naming convention resolves next.
3. **Guild-level default community** — if the guild has exactly one community configured with no channel-level override, use it. **The guild default, like a channel bind, can belong to only one community across all paired tenants** — this is the degenerate single-tenant case, not a per-tenant default.
4. **Ambiguous — fail closed.** If none of the above resolves to exactly one community, the interaction is rejected with a user-visible hint ("this channel isn't linked to a specific community — ask an admin to bind it") — **never** broadcast or applied to all candidate communities/tenants. Same fail-closed posture as every other capability-gate denial in this design (§3.8, §8).

**Role ownership (tenant/community A must never touch tenant/community B's roles, even in the same guild):** the `managed_roles` registry (hub-api-owned, alongside `role_mappings` in §3.1):

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `tenant_id` | `uuid` | **Hard scope** — a tenant may only see/manage rows where this matches its own tenant; enforced at the gate, not just filtered in a query |
| `pairing_id` | `uuid` | FK to the pairing above — a `managed_roles` row can only exist for an `active` pairing; a revoked pairing's rows stop being usable immediately (§3.8) |
| `platform` | `text` | `discord`, `twitch` |
| `guild_or_channel_ref` | `text` | External Discord guild id or Twitch channel id |
| `external_role_ref` | `text` | Discord role id, or a synthetic ref for Twitch moderator status (Twitch has no per-status "role id" — the ref is `(channel_ref, 'moderator')`) |
| `owning_community_id` | `uuid` | The one community (within the owning tenant) allowed to grant/revoke this role |
| `role_kind` | `text check in ('sub_tier1','sub_tier2','sub_tier3','moderator')` | |
| `name_prefix_template` | `text nullable` | e.g. `"[{community_tag}] {role}"` — see naming convention below |
| `created_by` / `created_at` | | |

Every `role.member.grant`/`revoke` (Discord) or moderator-add/remove (Twitch) call resolves its target role/status against `managed_roles` **by external ref, never by name** (see spoofing note below), and is rejected at the capability gate if the invoking tenant/community doesn't own that row **and** the row's pairing is `active`. **Conflict detection:** attempting to register a `managed_roles` row for an `external_role_ref` already owned by a different community (same tenant or a different one) is rejected at creation time with an explicit error (not a silent overwrite). Removals (§3.2) only ever touch roles the invoking community owns — the sync engine's revoke path filters candidate roles through `managed_roles` before issuing any call, same as grants.

**Registering a pre-existing role requires guild-authority approval (closes the registration race, see below):** a bot-*created* role is unambiguously owned by its creating community at creation time and needs no extra approval. Registering an **already-existing** Discord role into `managed_roles`, however, routes through the same guild-authority approval the pairing itself required — the guild authority (or a delegate they name) confirms which community a given pre-existing role id belongs to before it's claimable. This directly replaces the earlier, weaker "cross-check the registering admin's own channel/community routing binding" mitigation with an actual independent-party approval, since routing bindings are exactly what a same-guild adversarial tenant could also manipulate.

**Discord role hierarchy (guild authority must configure this):** Discord only lets a bot grant/revoke roles positioned **below** the bot's own role in the guild's role list. The guild authority must position the bot's role above every role any paired tenant registers into `managed_roles` — documented as an explicit pairing-activation prerequisite, checked at pairing-activation time (and re-checked before each grant/revoke call, since a guild authority can reorder roles later) with a clear, audited failure mode (`role-hierarchy-violation`, not a silent no-op) if the bot's role has since been moved below a managed role.

**Role naming convention (owner suggestion, human-readable ownership signal — not the authorization mechanism):** roles the bot *creates and manages* in a shared/multi-tenant guild get a per-community prefix, default template `"[<community tag alias>] <role>"` (e.g. `[BCSEA] Sub T2`, `[BCSEA] Mod`), configurable per community. Constraints: Discord role names are capped at 100 characters — the prefix + role name must fit, truncating the role name (never the prefix) if needed; the tag alias is sanitized (no `@`, no markdown, emoji stripped or normalized) before use. **Ownership and authorization are tracked by Discord role ID in `managed_roles` above, never inferred from the name** — a guild admin can rename any role, and anyone with Manage Roles can create a look-alike `[BCSEA] Mod`; name-based ownership is spoofable and is called out as such in §8. If a managed role is renamed in Discord, tracking continues by ID; re-applying the configured prefix on detected drift is a per-community, off-by-default setting (audited either way). Adopting an existing, unprefixed role into `managed_roles` requires the guild-authority approval above, never auto-adoption.

**Data isolation:** tenants never see each other's members, events, audit rows, or role-mapping config for a shared guild — every query is scoped by `(tenant_id, pairing_id)` at the DAL/gate layer, never filtered client-side. **The guild authority sees only a pairing/ownership overview** — which tenants/communities are paired, and which roles each owns (role name/id + owning community label) — **never member data, never event content, never another tenant's audit log**. This is a distinct, narrower view from any tenant's own admin UI, surfaced through a dedicated guild-authority-scoped read path (new work, §6).

**Events in a shared/multi-tenant guild (#96):** a Discord scheduled event can belong to several communities across several tenants sharing that guild — one underlying Discord event, many `community_tags` (§4.2) on the canonical row; §4.2's dedupe/loop-prevention logic already operates on the canonical event id, so a shared-guild event is a single row with a multi-value `community_tags` array, never N duplicate canonical rows. **Tenant isolation still applies to the canonical row's tenant-scoped fields** (§4.1's `tenant_id` column) — a multi-tenant shared guild means the *tags* array can span tenants' communities, but each tag's owning community is still resolved against its own tenant for every read/write, same as roles above.

**Read-state authorization gap (second Gemini review finding, build-time hardening item):** `managed_roles` gates the bot's own **write** path (grant/revoke), but a guild admin can assign or remove a role natively in Discord, outside the bot entirely. If any downstream logic (e.g. deciding whether to sync a Discord role into Twitch moderator status) treats "the user currently holds role X" as sufficient authorization without re-checking `managed_roles.owning_community_id`/`tenant_id` for that specific read, a native, bot-bypassing role assignment in a multi-tenant guild becomes a path to cross-community **or cross-tenant** (or Discord→Twitch privilege-escalation, §3.2) state changes. **Mitigation:** every sync decision that reads a role's presence, not just every grant/revoke that writes one, must resolve the role through `managed_roles` first (including its `pairing_id`/`tenant_id`) and treat an unregistered or wrongly-owned role as absent for that community's purposes — a sync-engine implementation requirement, not just a gate-time check, flagged for the C1 slice (§6).

**Tenant boundary — now explicitly allowed, secured by pairing consent instead of a one-tenant-per-guild rule:** the earlier "one guild = one tenant" default is **replaced**. A guild may be paired with any number of tenants; the security boundary is the **pairing's `active` status plus per-row tenant/community ownership checks** (`managed_roles.tenant_id`, `role_mappings.tenant_id`, channel-bind exclusivity), not the guild itself being single-tenant. **Residual risk (documented, not eliminated):** the bot's own Discord permissions are still granted guild-wide via one OAuth install — there's no way to give the bot Discord-native permissions scoped to just one tenant's roles/channels — so a compromised or malicious tenant admin could still attempt to read guild-wide structural state (channels/role list) via the host's non-PII structural guild-state cache (#444 §3.3) even though every *write* and every *managed_roles read used for a sync decision* is tenant-scoped and gated. This residual gap is why role-hierarchy enforcement, the guild-authority approval gate for pre-existing roles, and the guild-authority's own pairing/ownership visibility (not a blank check) all exist — see §8's updated threat row.

**Authorization is state-based, not actor-attribution-based (third Gemini review finding):** a manual, native Discord role change never carries a reliable actor identity in the gateway event itself, and Discord's Audit Log API is both eventually-consistent (an entry can lag the gateway event) and separately rate-limited — relying on it to attribute *who* made a change before deciding whether to sync it would be fragile and would either stall syncs or fail open under load. This design deliberately does **not** attempt actor attribution. Instead, the authorization boundary is entirely **state-based**: a role's current registration in `managed_roles` (owning `tenant_id`/`community_id`, `active` pairing) is the only thing that decides whether *any* observed state for that role — however it changed — is eligible to sync anywhere. A cross-tenant hijack attempt (tenant B's admin manually granting tenant A's role to force a sync) is blocked not by detecting *who* granted it, but because the sync engine only ever acts on a role it resolves through `managed_roles` as owned by the community/tenant it's syncing on behalf of — an unregistered or wrongly-owned role is simply never read as a trigger, regardless of source.

**Pairing/consent is not "install once, trust forever" (third Gemini review finding, build-time item):** a guild authority who consented can later be demoted, leave the guild, or have their account compromised, and the bot has no push notification for another user's Discord permission changes. **Mitigation, required at build time:** re-verify the guild authority's current Manage Server permission periodically (not just at consent time) and before any sensitive pairing-affecting action (e.g. approving a new pre-existing-role registration); a guild authority who no longer holds Manage Server has their outstanding approval authority revoked, though the pairing itself stays active until someone with current authority revokes it or a replacement guild authority re-consents. This is a periodic re-check, not a per-message check — it bounds the staleness window without adding per-event Discord API load.

**Revocation must actively unwind synced state, not just stop future syncing (third Gemini review finding, build-time item):** disabling a tenant's `role_mappings`/`managed_roles` rows on pairing revocation (as stated above) stops *future* syncing, but any Discord roles the bot already granted stay assigned to members — an orphaned, no-longer-governed grant. **Mitigation, required at build time:** revocation triggers a best-effort cleanup pass that revokes every bot-granted role tied to the revoked pairing's `managed_roles` rows (subject to the same rate limits as any other bulk operation, §3.6/§3.10, and audited); bot-*created* roles the pairing owned are left in place (deleting a role is more disruptive than revoking grants, and a future re-pairing may want to reclaim it under the same guild-authority-approval gate as any other pre-existing role) but are no longer synced.

### 3.1 Platforms and roles modeled

**Owner decision: every sync direction is a per-mapping, admin-chosen opt-in — nothing is active by default.**

| Platform | Role/status | Twitch API assignability | Configurable direction(s) |
|---|---|---|---|
| Twitch | Subscriber tier (T1/T2/T3) | **Read-only** — subscriptions cannot be granted via API, only observed | **Twitch→Discord only** — the only valid choice, since Twitch has no "grant a subscription" call; the admin UI must not offer the other directions for this role kind, and the API rejects any other `sync_direction` for a `sub_tier*` mapping |
| Twitch | Moderator | **Assignable** via Helix with `channel:manage:moderators` | Twitch→Discord, Discord→Twitch, or bidirectional — admin's choice per mapping, no default direction. Discord→Twitch and bidirectional both require the mandatory identity-link confirmation (§3.3) and audit (§3.7) regardless of who enables them — these are safety controls on the mechanism, not an owner-approval gate |
| ~~Twitch~~ | ~~VIP~~ | ~~Assignable via `channel:manage:vips`~~ | **Removed from v3.0 scope** |
| ~~Twitch~~ | ~~Follower~~ | ~~Read-only~~ | **Removed from v3.0 scope** |

Role mapping table (hub-api-owned, cross-community config):

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `tenant_id` | `uuid` | Tenant scope, enforced via the owning pairing (§3.0, §3.8) |
| `community_id` | `uuid` | Owning community — must match `managed_roles.owning_community_id` for the resolved target (§3.0) |
| `condition_type` | `text check in ('twitch_sub_tier1','twitch_sub_tier2','twitch_sub_tier3','twitch_moderator','discord_moderator')` | Grant trigger — narrowed from the original generic `membership`/`reputation_threshold`/`identity_verified` set to the actual v3.0 conditions; reputation/verification-driven role sync (#497's original broader framing) is deferred, not built in this slice |
| `platform` | `text` | Target platform: `discord`, `twitch` |
| `target_guild_ref` / `target_channel_ref` | `text` | External Discord guild id or Twitch channel id, resolved server-side (§3.8) |
| `target_role_ref` | `text` | Discord role id, or the synthetic Twitch-moderator ref (§3.0) |
| `sync_direction` | `text check in ('twitch_to_discord','discord_to_twitch','bidirectional')` | **New column — the admin's explicit choice.** No default value; a mapping cannot be activated without one. `sub_tier*` condition types only accept `twitch_to_discord` |
| `enabled` | `boolean not null default false` | **New column — every mapping is created disabled.** The sync engine (C1, §6) never processes a mapping with `enabled=false`; flipping it on is a separate, explicit admin action from creating the mapping |
| `created_by` | `uuid` | Admin who configured the mapping |
| `created_at` / `updated_at` | `timestamptz` | |

### 3.2 Sync direction and conflict rules

- **Nothing syncs until an admin explicitly enables a mapping with an explicit direction.** Creating a `role_mappings` row does not start syncing anything — `enabled` defaults to `false` and `sync_direction` has no default (§3.1). This applies uniformly to Twitch↔Discord role sync, cross-guild role mirroring (§3.10), and event sync (§4).
- **Subscriber tiers: Twitch → Discord only, always** — enforced at the schema/API level (§3.1), not just a UI default, since Twitch has no "grant a subscription" call.
- **Moderator: any direction, including Discord → Twitch, is a normal configurable choice — no longer pending owner confirmation.** The safety controls exist regardless of which direction(s) an admin picks: (1) **audited** (§3.7), every grant/revoke recording which direction's mapping triggered it; (2) **mandatory identity-link confirmation** (§3.3) — the target must already have a confirmed Twitch↔Discord link before any cross-platform moderator call executes, whichever direction; a role/status change for an unlinked user never triggers a call on the other platform. Discord→Twitch specifically remains flagged as a **privilege-escalation vector** in §8 given a Discord role grant can make someone a real Twitch moderator — the mitigation is the confirmation + audit above, not a ship/don't-ship gate.
- **Bidirectional loop prevention (new, required for any `bidirectional` mapping):** every grant/revoke call the sync engine issues is tagged with an **origin marker** — `(origin_platform, origin_write_id, origin_seq)`, where `origin_seq` is a **monotonic per-`(member_ref, target_role_ref)` sequence number the sync engine assigns itself**, not a wall-clock timestamp — recorded alongside the resulting state. When the *other* platform's own EventSub/gateway frame reports that same state change back, the engine recognizes its own echo by matching the incoming state against the most recent origin marker for that pair and treats it as a no-op — never re-forwarding it back to the platform it came from. **Third Gemini review finding — clock drift and event-delivery latency:** matching on a bounded wall-clock window alone is fragile under Discord/Twitch delivery jitter (a delayed gateway frame can arrive after the window closes and be misread as a fresh external change) and under clock drift between services. The `origin_seq` counter sidesteps both — it's compared for exact identity, not recency, so a late-arriving echo is still recognized whenever it does arrive, no window to miss. A rapid sequence of real transitions (e.g. Tier 3→Tier 2→Tier 3 in quick succession) is handled by incrementing `origin_seq` per transition rather than per wall-clock tick, so an echo of transition N never falsely matches transition N+1. **Residual trade-off, documented not eliminated:** if an echo is ever missed anyway (e.g. the origin marker row itself was evicted before the echo arrived), a spurious one-hop re-forward is a data-consistency nuisance (an extra, ultimately idempotent API call), never a security issue — the role-ownership/pairing checks (§3.0) still gate every call regardless of whether it originated from a genuine change or a missed-echo re-forward.
- **Bidirectional conflict rule:** when both platforms report a genuine (non-echo) state change for the same `(member_ref, target_role_ref)` within the same sync window — e.g. a moderator manually demoted on Twitch and independently promoted on Discord — **last-write-wins by the sync engine's own received-order, not by comparing the two platforms' wall-clock timestamps** (per the clock-drift note above), with the winning platform and its write recorded in the audit row (§3.7); this mirrors §4.5's field-level last-write-wins rule for bidirectional event sync, adapted to avoid cross-platform clock comparison. A losing platform's state is corrected to match on the next sync pass, itself tagged with an origin marker per the loop-prevention rule above.
- **One-way mappings keep the original internal-truth model:** for a `twitch_to_discord` or `discord_to_twitch` mapping, the source platform's observed state is authoritative; the target is driven to match it, never read back to change the source.
- **Conflict rule (one-way):** last-internal-write-wins per `(member, platform, target_role_ref)`, scoped to roles the invoking community owns (§3.0). Before issuing a grant/revoke call, the host consults its own structural guild-state cache (#444) to skip redundant API calls when the member already holds/lacks the role.
- **Idempotency:** each sync operation carries a dedupe key `(member_ref, target_role_ref, action)`; the host's own cache plus Discord's PATCH-role-list semantics and Twitch Helix's idempotent moderator add/remove calls make repeated syncs safe to retry.

### 3.3 Account linking (Twitch ↔ Discord identity)

A Twitch user and a Discord user are linked only as UUIDs in bundles, via hub-api's identity service — the same pattern as §5.1's RSI-handle linking:

1. A user (or admin, for bulk cases) initiates linking via a Discord command or a webui flow.
2. hub-api runs **OAuth on both platforms** (Twitch OAuth for the Twitch identity, Discord OAuth — already established for existing Discord identity — for the Discord side) and calls `IdentityService.LinkExternalIdentity` twice (once per platform) against the same `hub_user_uuid`, extending the existing `hub_user_identities` mapping (#449).
3. Neither platform's raw handle/username is stored in bundle `storage.tables` — only the resulting `member_ref`/`hub_user_uuid`, per the standing PII invariant (§5).
4. Twitch and Discord OAuth tokens themselves are stored via the credential broker, never in bundle storage, and scoped per §3.4's minimal-scope list.

### 3.4 Twitch Helix API: scopes and rate/slot limits

- **Minimal token scopes:** `channel:read:subscriptions` (subscriber tier read), `moderation:read` (current moderator list read) — always required. `channel:manage:moderators` is requested **only if** at least one `enabled` mapping on that Twitch channel has `sync_direction` `discord_to_twitch` or `bidirectional` for the moderator role kind (§3.1/§3.2); it is never requested by default, and is dropped again if every such mapping is later disabled.
- **Inbound transport reuses the already-planned Twitch EventSub connector** (§2's connector roadmap already lists "Twitch EventSub (receiver)" as v3.0 work) — subscriber-tier and moderator-status changes arrive via the matching EventSub subscription types (e.g. subscribe/subscription-message/subscription-end for tiers, moderator-add/moderator-remove for mod status), not a separate poll loop. **ASSUMPTION:** the exact EventSub subscription-type-to-scope pairing needs confirming against Twitch's current EventSub reference at implementation time — not fabricated here.
- **Twitch API rate limits:** Helix enforces its own per-app/per-token rate-limit bucket, independent of Discord's; every moderator grant/revoke call is subject to it in addition to any host-side pacing this design adds. Exact bucket sizing is an implementation-time lookup against Twitch's published Helix rate-limit documentation, not asserted here.
- **Slot limits:** Twitch does not impose a documented hard cap on moderator count (unlike the now-removed VIP slot limit) — no slot-limit handling is required for this design's moderator sync.

### 3.5 Discord connector permissions needed

Built on #444's connector layer (per the issue's own instruction — "not a bundle-local Discord API client"):

- `connector.send:discord` for role grant/revoke — #444's `sender` spec enumerates `role.create`/`role.edit`/`role.delete` (guild-level role CRUD) but **not** a per-member grant/revoke action kind. **ASSUMPTION — confirm with #444's owners:** this spec proposes two new sender action kinds, `role.member.grant` and `role.member.revoke`, following the same `build-request` pattern as the existing role CRUD kinds.
- Permission-check reads use #444's host-maintained structural guild-state cache (roles/channels/permission overwrites — explicitly non-PII, §"Permissions checks" in #444, exact query API still an open item there).
- No new WIT interface needed beyond the two action kinds above — this rides entirely on `connector@1.0.0`'s existing `sender` shape.

### 3.6 Rate limits (both APIs, bulk apply, many guilds)

- **Hierarchical, not flat, rate limiting (third Gemini review finding):** Discord's per-bot-token limit is global — a single bot process shares one budget across every guild it's in — so a naive independent per-guild bucket can still let one guild's traffic exhaust the shared global budget underneath the per-guild accounting, starving every other guild even though each guild's own counter looks fine. The design is therefore **two-level**: a **global token-bucket governor** for the bot's own Discord API budget sits above **per-guild child lanes** (existing relay `UsageBatcher` limits, #444/#419 wit-v1.1 §2); a guild's lane can only draw from the global budget when it has headroom, and **real-time, user-triggered sync calls are prioritized over bulk-backfill traffic** in the scheduler (§3.10) so an in-progress backfill in one guild never delays a live role/event change in another. Twitch Helix's own token-bucket (§3.4) applies independently to every Twitch moderator call, keyed per channel, under the same two-level shape if Bar Citizen ever operates multiple Twitch channels.
- Bulk apply/remove: capped at 50 members per operation, per platform, **per guild/channel**. **Enforcement is host-side, not bundle-trusted** — the bundle may batch its own calls, but the host's rate limiter is the actual gate on every individual call regardless of how the bundle paces them. Defense in depth against a compromised or buggy bundle fanning out into a platform-side rate-limit storm.
- **Many-guild fan-out** (Bar Citizen's ~14 guilds): see §3.10 for the batch/queue design this implies.

### 3.7 Audit logging

One audit event per role change: `(member_ref, target_role_ref, platform, tenant_id, pairing_id, owning_community_id, condition_type, sync_direction, actor, timestamp, result)`. For a `discord_to_twitch` or `bidirectional` moderator mapping (§3.2), the audit row additionally records the triggering role/status change on the origin platform that caused the call on the other. **Pairing lifecycle events** (propose/consent/revoke, §3.0) are their own audit rows, visible to both the proposing tenant and the guild authority (the guild authority's view is limited to pairing/ownership metadata, never member data — §3.0). Reuses the existing hub-api audit-log sink (same one `bundle_capability_gate::authorize()` already writes denial/grant events to, #419 §5). Satisfies #497's AC ("bulk apply/remove ... completes with an audit log entry per change").

### 3.8 Tenant boundary (pairing-scoped, not guild-scoped)

**Hard constraint, not UI-only** (#497 AC is explicit on this), **rewritten for multi-tenant guild pairing (§3.0):** `target_guild_ref`/`target_channel_ref` is resolved server-side against the invoking community's own tenant — never taken from bundle input — **and** the capability gate additionally requires an `active` `guild_tenant_pairings` row for that `(tenant_id, guild_ref)` pair before authorizing anything. The gate's `ResourceRef` derivation (#419 §5.2, `AppScoped`, server-side derivation) rejects any role-mapping row or sync operation whose resolved guild/channel doesn't belong to a pairing the invoking tenant actually holds, whose pairing is `revoked`, or whose target role isn't owned by the invoking community (§3.0). A revoked pairing takes effect immediately — no grace period for in-flight syncs. **Regression tests required:** cross-tenant role sync rejected at the gate layer (existing #497 AC); tenant A cannot modify tenant B's roles, events, or channel binds in a shared guild (new, per owner decision); an ambiguous shared-guild routing case fails closed rather than broadcasting; a role-mapping/managed-roles operation against a `revoked` or never-`active` pairing is rejected. None of these are UI-only checks.

### 3.9 Scope checks via the capability gate (#428)

Every `role.member.grant`/`revoke` (Discord) and moderator-add/remove (Twitch) invocation goes through `CapabilityGate::authorize()` (#428) with a host-constructed `InvokeScope` (never guest-influenced, #419 §5.1 Gemini condition 1) carrying the resolved tenant/community/guild-or-channel triple **and the pairing id** (§3.0/§3.8). `dangerous`-risk classification applies to both directions (guild role management and Twitch moderator management both cross a trust boundary per #419's risk-level rule) — reviewed at all three consent tiers (§3 of #419) on first install, not auto-approved. Any `discord_to_twitch`/`bidirectional` moderator mapping additionally requires its own explicit per-community consent grant, separate from the base Twitch↔Discord sync install, per §3.2.

### 3.10 Scale: many guilds, many communities (N:M, not 1:1)

**Owner-supplied sizing:** Bar Citizen operates **~14 Discord guilds** (11 regional + 3 international) and **~12 communities** — and the mapping between them is **N:M**, not one guild per community: some communities span multiple guilds (e.g. one international community present across all 3 international servers, or a community that owns a regional server and also has a presence in an international one); some guilds host multiple communities (§3.0's shared-guild case). This is the general case the pairing/routing model (§3.0) and the `role_mappings`/`managed_roles` schema (§3.1) are already shaped for — a community is never assumed to have exactly one guild, and a guild is never assumed to have exactly one community; no additional schema change is needed beyond what §3.0/§3.1 already define, since both tables key off `(community_id, guild_ref/channel_ref)` pairs rather than a single guild-per-community column.

**Sizing example (illustrative, not the confirmed real map — open question below):**

| Community | Guild(s) | Notes |
|---|---|---|
| Regional community "BCSEA" | 1 regional guild | The common case — 1:1 |
| Regional community "BCUS-East" | 1 regional guild + a channel in the international hub guild | N communities can each have a presence in a shared international guild — this is §3.0's shared-guild routing, applied per-region |
| International community "BC-Global" | All 3 international guilds | 1 community : N guilds — the routing/ownership model resolves each guild independently; `managed_roles`/`role_mappings` rows exist once per `(community, guild)` pair, not once per community |

**Routing examples:**

- **A community present in 2+ guilds** (e.g. "BC-Global" above): an interaction in international guild #2 resolves to "BC-Global" via that guild's own channel-binding/default-community rules (§3.0) — the routing decision is always local to the guild the interaction happened in; a community having a presence elsewhere doesn't change how *this* guild's ambiguity is resolved.
- **A guild hosting 2+ communities** (§3.0's shared-guild case, e.g. a regional guild also hosting a sub-community): unchanged from §3.0 — channel-level binding, then tag/prefix, then guild default (which itself can only belong to one community), then fail-closed.

**Role/event sync fan-out math (12 communities × 14 guilds, under per-guild rate limits):** the pairing/mapping model means the *number of active sync operations* is driven by how many `role_mappings`/event-source rows are actually `enabled` (opt-in, §3.2), not by the full 12×14 cross-product — but the **worst case** (every community mirroring roles/events into every guild it has a presence in, across all paired guilds) still bounds design decisions:

- Each guild's Discord role-modify calls draw from their own **per-guild lane** under the **global bot-token governor** (§3.6) — a burst of role changes in one busy regional guild is bounded by its own lane, but the governor still protects the other 13 guilds from the case where one guild's lane alone would otherwise be large enough to exhaust Discord's shared global rate limit.
- **Batch/queue design:** bulk operations and cross-guild mirroring runs are dispatched onto a **per-`(tenant, guild)` queue lane** (the existing relay/job infrastructure, partitioned by that key) — a worker pool drains lanes in parallel, bounded by the platform's existing global relay concurrency budget **and** the global rate-limit governor (§3.6), so 14 guilds' worth of work proceeds concurrently up to that budget rather than serially through one shared queue. **Real-time sync calls preempt bulk-backfill traffic** in the scheduler — a large backfill running in one guild's lane never delays a live role/event change anywhere, including in that same guild.
- **Bulk initial backfill** (activating a new pairing, or enabling a new mapping against an existing large membership): runs as a **resumable job**, checkpointed per guild (a cursor over the last-processed member/role id), so an interruption resumes from the checkpoint rather than restarting the whole guild's backfill; progress is exposed per guild (e.g. "1,200 / 3,400 members processed") through the same audit/status surface as other long-running sync state. Idempotent grant/revoke semantics (§3.2's dedupe key) make a resumed backfill safe to re-run over already-processed members.
- **OTel metrics per guild** (§7): every `role_sync.*`/`event_sync.*` counter and histogram is labeled with `guild_ref` in addition to platform/`role_kind` — 14 guilds is well within safe label cardinality; a tenant with a much larger guild count would need cardinality bounding (e.g. bucketing or a cap with an "other" label), flagged as a future-scale follow-up, not a v3.0 requirement at Bar Citizen's size.

---

## 4. Event sync (#96)

**Owner decision: event sync is opt-in per mapping, like role sync (§3.2)** — an event source (Discord, Wix, Spectrum, a cross-guild mirror per §3.10) is not synced into a community's feed until an admin explicitly configures and enables that mapping with a chosen direction (inbound-only, outbound-only, or bidirectional where the platform allows it). Nothing is on by default.

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
- **Shared guilds (§3.0):** when a Discord guild hosts multiple communities, one underlying Discord scheduled event can be relevant to several of them — this is modeled as **one canonical event row with a multi-value `community_tags` array**, never as N duplicate rows for the same external event. Dedupe keys off `(source_platform, external_event_id)` as usual (§4.5); a shared-guild event simply accumulates more than one tag on that single row as each community's tag-scope filter (§4.1's `tag_scope`) opts it in.

### 4.3 Platforms covered

| Platform | Direction | Mechanism |
|---|---|---|
| Discord scheduled events | Inbound + outbound | Connector `sender` action kinds `scheduled_event.create`/`edit`/`delete` (#444 §"Roles, channels, scheduled events") for outbound; `receiver.on-frame` for inbound gateway events |
| Wix Events (§2.2, #95) | Inbound only for v3.0 | §2.2's Wix connector receiver normalizes Wix Events webhook payloads into the canonical table; outbound (WaddleBot → Wix) is deferred, matching §2.2's scope note |
| Calendar (Google/ICS) | **ASSUMPTION — confirm with owner:** which calendar provider(s) Bar Citizen actually needs (open question 6) | New connector or existing calendar integration, TBD pending answer |
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

### 5.2 Wix member records (#485/#95, replaces Odoo)

Same pattern: a Wix member id (the `contactId`/member id in a Wix webhook payload) maps to a `member_ref` UUID via the same identity service, resolved **inside the Wix connector's `on-frame`**, before the normalized event is re-emitted downstream — identical timing to §5.1's RSI-handle flow. `storage.tables` and logs carry only `member_ref`, never the Wix member's name/email. Wix member emails and names stay inside hub-api's PII boundary (or the connector's momentary `on-frame` handling, itself inside the same privileged trust tier as any other connector receiver, §5.1 step 3) — every client and every `stage`/`stage-v1_1` bundle, including §2.2's own action-stage half, sees only the token.

**Rendering a name downstream (second Gemini review finding):** a use case like a Discord "Welcome, `<name>`!" message needs the raw name at render time — this must go through the same `connector.pii.read`/`identity.lookup` detokenization gate as §5.1 step 5, resolved on demand, **never** by caching the name in `storage.tables` or a bundle-visible field. The existing per-replica 5-minute `identity.lookup` cache (#444 §3.4) is the load-bounding mitigation this implies — a render-time lookup hits that cache in the steady state, not a live Wix API call per message, bounding both Wix rate-limit exposure and identity-service load.

---

## 6. Dependencies and sequencing

| Dependency | Issue | Gates |
|---|---|---|
| Bundle db capability + DDL generator | #430, #498 | Any `storage.tables` bundle (§2.1–2.3, §3.1's role_mappings/managed_roles, §4.1's events table) |
| Capability gate crate + wiring | #428, #433 | Every permission check in §2–§4 |
| Grant storage / 3-tier consent flow | #432 | Install-time consent for `dangerous` permissions (`connector.receive:*`, `net.http.fqdn`, Discord/Twitch role management) |
| Egress hardening (guard, three-category perms, egress-proxy, CNI) | #459, #463, #465, #468, #469 | Every `net.http.fqdn` egress target (§2.1's source, §2.2's Wix API host, §2.3's Spectrum) |
| Connector world + per-component linker | #461 | Spectrum connector (§2.3), Wix webhook connector (§2.2), Discord/Twitch role/event sender kinds (§3.5, §4.3) |
| Connector PII reader role | #464 | RSI handle detokenization (§5.1 step 5), Wix member detokenization (§5.2) |
| Connector bundle design + migration tracking | #444, #451 | Foundation for Spectrum/Wix receivers and Discord/Twitch sender role/event action kinds |
| Identity fold into hub-api | #449 | RSI handle linking, Wix member tokenization (§5), Twitch↔Discord account linking (§3.3) |
| **hub-api multi-tenant guild pairing + N:M binding (rescoped)** | **#500** | **Any shared/multi-tenant-guild role sync (§3.0) or event tagging (§4.2) — blocks §3/§4 wherever a guild hosts more than one community or tenant. Rescoped 2026-09-29: `ingest_sources` uniqueness must allow the same guild across tenants and communities with exclusive channel-level binding, plus the new `guild_tenant_pairings` consent table (§3.0)** |
| **Guild-authority pairing/consent UI + OAuth verification flow (new)** | *(new work, no issue filed yet — part of #500's scope)* | Pairing proposal/consent/revocation (§3.0); Discord OAuth Manage-Server verification at consent time and at pre-existing-role registration time |
| **Bulk backfill queue infrastructure (new)** | *(new work, part of #500's scope)* | Resumable per-guild backfill with progress (§3.10) — reuses existing relay/job queue infra, partitioned per `(tenant, guild)` |

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
       ├─► §3 Role management, Twitch↔Discord (#497) ──► needs #444's new Discord sender action kinds
       │      (§3.5, ASSUMPTION) + Twitch EventSub connector (§3.4) ──► shared/multi-tenant guild cases
       │      also need #500 (rescoped: pairing/consent + N:M binding, §3.0)
       │
       └─► §4 Event sync (#96) ──► shared/multi-tenant guild tagging also needs #500
       │
       ▼
§2.2 Bar Citizen full bundle (#485, folds in #95 Wix) — depends on Wix webhook connector (needs #461)
   + Wix egress (#459/#465) + role/event patterns proven above
       │
       ▼
Cross-cutting: db/gate-ready work in §3/§4 can start in parallel with the above, but Discord/Twitch
   sender kinds depend on #444's connector sender cutover, and any shared-guild slice is blocked on #500
```

### Slice plan (agent-sized, ≤30 min each)

| Slice | Work | Depends on |
|---|---|---|
| A1 | `waddles.core.barcitizen.readonly` manifest + table DDL + poll loop against the confirmed source (open question 1) | #430/#498, #459/#465 |
| A2 | Role mapping table (§3.1) + `managed_roles` ownership registry (§3.0) + hub-api CRUD, no sync yet | #430/#498 |
| A3 | Event canonical table + tag parse/inject shared library (no platform wiring yet) | #430/#498 |
| A4 | hub-api multi-tenant guild pairing table + N:M community/tenant binding + deterministic routing (#500, rescoped) | #430/#498 |
| A5 | Guild-authority OAuth consent/revocation flow (Manage-Server verification), pairing/ownership-overview read path (§3.0) | A4 |
| A6 | Resumable per-guild bulk-backfill queue + progress tracking (§3.10) | A4 |
| B1 | Discord sender `role.member.grant`/`revoke` action kinds (pending #444 owner confirmation, §3.5) | #444, #461 |
| B2 | Discord sender `scheduled_event.create`/`edit`/`delete` wiring for event sync outbound | #444, #461 |
| B3 | Spectrum connector receiver: org linking + roster/event inbound parsing | #444, #461, #449 |
| B4 | RSI handle link/verify flow + unlinked-handle placeholder UUIDs | #449, #464 |
| B5 | Twitch EventSub connector receiver: subscriber-tier + moderator-status inbound (§3.4) | #444, #461, #449 |
| B6 | Wix webhook connector receiver: Members/Events/Pricing Plans inbound + `member_ref` tokenization (§2.2, §5.2) | #444, #461, #449 |
| C1 | Role sync engine: condition evaluation → grant/revoke dispatch, dedupe cache, audit log, role-ownership + active-pairing check (§3.0), opt-in `enabled`/`sync_direction` gating (§3.2) | A2, A4, B1, B5 |
| C2 | Event sync engine: tag filtering incl. shared-guild multi-tag (§4.2), dedupe/idempotency, timezone rendering | A3, B2, B3, B6 |
| C3 | Twitch↔Discord account linking flow (§3.3) | #449 |
| C4 | Discord→Twitch (and bidirectional) moderator mapping support — a normal per-mapping opt-in, no owner gate (§3.2) | C1, C3 |
| D2 | Bar Citizen full bundle: Discord read/write wiring reusing B1/C1 patterns, Wix wiring reusing B6 | C1, B6 |
| E1 | Cross-tenant role-sync regression test + capability-gate denial test (§3.8) | C1 |
| E2 | Spectrum health-check probe + kill-switch flag wiring (#101 AC) | B3 |
| E3 | Multi-tenant guild regression tests: cross-community role-ownership denial, ambiguous-routing fail-closed, **tenant A cannot modify tenant B's roles/events/channel binds**, operation against a revoked/never-active pairing rejected, **read-path `managed_roles` re-check (native-assignment bypass), pre-existing-role registration requires guild-authority approval** (§3.0, §3.8, 2nd + 3rd Gemini review) | A4, C1 |
| E4 | Bidirectional sync regression tests: origin-marker loop prevention (no re-forward of an echoed write), last-write-wins conflict resolution recorded with winning platform (§3.2) | C1 |
| E5 | Scale tests: global-governor-vs-per-guild-lane rate-limit isolation (one guild's bulk traffic can't starve another), real-time-preempts-backfill scheduling, resumable bulk backfill (interrupt-and-resume checkpoint correctness), per-guild OTel label cardinality (§3.6, §3.10) | A4, C1 |
| E6 | Pairing lifecycle tests: role-hierarchy violation detected and audited (not a silent no-op), stale guild-authority re-verification revokes approval authority, pairing revocation cleanup revokes bot-granted roles tied to it | A5, C1 |

---

## 7. Testing

| Layer | Coverage |
|---|---|
| Unit | Tag parse/inject library; role condition evaluation; timezone conversion; RSI handle tokenization/placeholder logic; Wix webhook payload → `member_ref` mapping; role-ownership resolution (§3.0); shared-guild routing precedence (§3.0) — 90%+ per `critical-rules.md` |
| Integration | Capability-gate denial paths (cross-tenant role sync, cross-tenant event read, **cross-community role-ownership denial in a shared guild**, **operation against a revoked/never-active guild pairing rejected**, **tenant A modifying tenant B's roles/events/channel binds rejected**); db capability writes against real Postgres; connector `identity.lookup` against a seeded RO replica; **ambiguous shared-guild routing resolves to a fail-closed rejection, never a broadcast**; **a mapping created but not `enabled` never syncs (opt-in-by-default-off verified)**; **bidirectional loop-prevention drops an echoed write instead of re-forwarding it** |
| E2E | **Every standard PR-gate run mocks Discord and Twitch** (gateway/EventSub frames + REST responses) — masked CI secrets are unavailable to fork-originated PR runs in most CI setups, so a live-bot test cannot be a hard PR gate. The **live Discord round trip runs on a scheduled/nightly job against the release branch only** (never fork PRs), using the **separate CI-only bot** (dedicated disposable test guild, bot token as a masked CI secret per `critical-rules.md` Token & Secret Hygiene — never the production bot, never a synthetic-forged envelope), serialized (mutex/lock, one run at a time) against that guild to avoid concurrent-run role/event collisions: role grant/revoke visible in the test guild, scheduled event create/update relayed, **two-community shared-guild fixture exercising role-ownership denial**. Twitch moderator/sub-tier sync is tested against EventSub fixtures only in PR-gate runs; a live Twitch round trip (test channel, disposable mod token) follows the same nightly-only pattern as Discord. Spectrum connector is tested against a fixture/mock only, in both PR and nightly runs — no live RSI calls anywhere in CI (undocumented API, ToS/Cloudflare-block risk). Wix webhook signature verification is tested against fixture-signed payloads, no live Wix site required |
| Coverage | 90%+ lines/branches/functions/statements, gate blocks below threshold |
| OTel | `role_sync.grant`/`role_sync.revoke`/`role_sync.denied` counters (labeled by platform, `role_kind`, **`sync_direction`, and `guild_ref`** per §3.10); `event_sync.upsert`/`event_sync.conflict` counters (also `guild_ref`-labeled); histogram on Spectrum poll latency, Wix/Twitch API call latency, and **bulk backfill batch duration per guild**; spans across the identity-lookup and capability-gate calls; structured logs (penguin logging, not hand-rolled) for every grant/deny/sync decision at INFO, verbose branch detail at DEBUG — including every shared-guild routing decision, pairing consent/revocation event, and its resolution path (§3.0) |

---

## 8. Security review notes

| Threat | Mitigation |
|---|---|
| Role-escalation via sync | Role grants are `dangerous`-risk, reviewed at all 3 consent tiers (#419 §3); `InvokeScope` host-constructed only (no guest-supplied scope); cross-tenant guild resolution rejected at the gate (§3.8) with a regression test |
| **Discord→Twitch moderator privilege escalation (new)** | A Discord role grant could make someone a Twitch moderator (`channel:manage:moderators`) — a real-world privilege gain, not just an internal-state change. This direction is now a normal per-mapping admin opt-in (§3.2, no longer owner-gated), so the mitigation is entirely in the mechanism: every mapping is `enabled=false` until explicitly turned on (§3.1); the target must have a **mandatory**, pre-existing Twitch↔Discord identity link (§3.3) before any call executes — an unlinked user's Discord role change never reaches Twitch; every grant/revoke is audited with the triggering change recorded (§3.7) |
| **Multi-tenant Discord guild sharing (revised — now explicitly allowed, not disallowed)** | The bot's Discord permissions are still guild-wide via a single OAuth grant — a malicious/compromised tenant admin in a multi-tenant-paired guild could still enumerate guild-wide structural state (channels/role list) via the host's structural guild-state cache, even though every write and every `managed_roles`-backed read is tenant-scoped and gated (§3.0/§3.8). Mitigated by: pairing requires explicit guild-authority consent (Discord OAuth-verified Manage Server), revocable by either side; exclusive channel/guild-default binding across all paired tenants; `managed_roles`/`role_mappings` rows are hard-scoped by `tenant_id` and a row's pairing must be `active`; registering a **pre-existing** role requires guild-authority approval (not just the registering admin's own claim); Discord role-hierarchy is enforced (bot's role must sit above every managed role) with an audited failure mode if violated; the guild authority's own visibility is limited to a pairing/ownership overview, never member data. **Regression test required:** tenant A cannot modify tenant B's roles, events, or channel binds in a shared/multi-tenant guild |
| **Pairing consent bypass / guild-authority impersonation (new)** | A pairing that activates without a genuine Manage-Server-holding Discord user's consent would let any tenant claim any guild. Mitigated by requiring Discord OAuth verification of the consenting user's current Manage Server permission at consent time (not a self-attested checkbox), and by making every pairing revocable by the guild authority at any time if consent was given in error or under duress |
| **Bidirectional role sync feedback loop (new)** | A `bidirectional` moderator mapping without loop prevention could ping-pong a status change back and forth between Twitch and Discord. Mitigated by the origin-marker mechanism (§3.2) — an echo of the sync engine's own most recent write is recognized and dropped as a no-op, never re-forwarded |
| **Ambiguous shared-guild routing (new)** | A message/command/interaction in a shared guild that doesn't resolve to exactly one community must never be applied to all candidate communities by accident. Mitigated by the deterministic precedence + fail-closed rule in §3.0, with a regression test (§3.8, §7) |
| **Role-name spoofing in shared guilds (new)** | A community-prefix naming convention (e.g. `[BCSEA] Mod`, §3.0) is a human-readable ownership signal only — a guild admin can rename any role, and anyone with Manage Roles can create a look-alike prefixed role. Mitigated by tracking ownership and authorization exclusively by Discord role ID in the `managed_roles` registry (§3.0), never by name; adopting an existing unprefixed role into the registry requires an explicit admin action, never auto-adoption |
| Cross-tenant leakage | Every `storage.tables` row and role-mapping row is tenant-scoped at the schema/gate layer, never bundle-trusted; event canonical table filters by `tenant_id` on every read |
| **Cross-community leakage within a shared guild (new)** | Community A must never grant, revoke, or read the ownership state of a role owned by community B in the same guild. Mitigated by `managed_roles`' `owning_community_id` check at the capability gate (§3.0, §3.8) on every grant/revoke/removal path, with a dedicated regression test |
| **Native Discord role assignment bypassing `managed_roles` (2nd Gemini review, build-time item; clarified by 3rd review)** | A guild admin (or anyone with Manage Roles, from any tenant sharing the guild) assigning a role natively in Discord never touches the bot's write-path gate. Mitigated by making authorization **state-based, not actor-attribution-based** (§3.0, 3rd Gemini review): the sync engine never tries to determine *who* changed a role (Discord's Audit Log API is both eventually-consistent and separately rate-limited, making attribution unreliable at sync time) — instead, *every* read used for a sync decision, not just every write, resolves the role through `managed_roles` first (including its `tenant_id`/`pairing_id`), and an unregistered or wrongly-owned role is treated as absent regardless of how or by whom it changed |
| **`managed_roles` registration race — role-id harvesting (2nd Gemini review, now closed by design, not just a build-time item)** | A community/tenant admin could pre-claim a pre-existing Discord role id actually intended for a different community or tenant (role ids are visible to anyone in the guild) by registering it first. **Closed** by requiring guild-authority approval (an independent party, not the registering admin) for any pre-existing-role registration (§3.0) — this replaces the weaker "cross-check the registering admin's own routing binding" mitigation from the 2nd review round, which was itself manipulable by the same adversarial tenant |
| **Global bot-token rate-limit exhaustion / cross-tenant starvation (new, 3rd Gemini review)** | A naive independent per-guild rate-limit bucket doesn't protect against Discord's *global*, per-bot-token rate limit being exhausted by one guild's traffic (e.g. a large bulk backfill), starving real-time sync in the other 13 guilds even though each guild's own counter looks healthy. Mitigated by the hierarchical global-governor-plus-per-guild-lanes rate limiter (§3.6/§3.10) and by prioritizing real-time sync calls over bulk-backfill traffic in the scheduler |
| **Stale guild-authority consent (new, 3rd Gemini review, build-time item)** | A guild authority who consented to a pairing can later be demoted, leave the guild, or have their account compromised, with no push notification to the bot. Mitigated by periodically re-verifying the guild authority's current Manage Server permission (not just at consent time) and before any sensitive pairing-affecting action such as approving a new pre-existing-role registration (§3.0) |
| **Orphaned role grants after pairing revocation (new, 3rd Gemini review, build-time item)** | Revoking a pairing disables future syncing but, without further action, leaves already-granted Discord roles assigned to members — an ungoverned, no-longer-tracked grant. Mitigated by a best-effort cleanup pass on revocation that revokes every bot-granted role tied to the revoked pairing's `managed_roles` rows (rate-limited and audited like any other bulk operation, §3.6/§3.10); bot-created roles are left in place but stop syncing |
| Scraping abuse / ToS violation | Spectrum connector is read-only by design (no `connector.send`), rate-limited, health-checked, and behind a fast kill-switch (#101 AC); Bar Citizen read-only source (§2.1) flagged for ToS confirmation before any scraping fallback is built (open question 1) |
| Egress abuse | Every bundle's `net.http.fqdn` targets (including §2.2's Wix API host) are explicit allowlist entries reviewed at install (#419 §1), never wildcards or IP literals; private-IP egress is not requested by any bundle in this design |
| PII leakage via connector | RSI handles and Wix member data (name, email) tokenize to `member_ref`/UUID before crossing into `storage.tables`/logs/telemetry (§5.1, §5.2); `connector.pii.read`/`identity.lookup` stays global-approved, core-only, RO-replica-scoped (#444 §3.3) |
| **Wix webhook forgery (new)** | An unauthenticated or replayed webhook claiming to be from Wix could inject fabricated member/event data. Mitigated by host-side JWT signature verification on the raw request bytes, before any parse or connector invocation — the same ordering #444 §2 already mandates for EventSub/Slack — plus a replay window, matching the existing webhook `Transport` family's discipline (§2.2) |
| Credential exposure (Wix OAuth app/API key, RSI session token) | Stored via the connections/credentials broker (`docs/superpowers/specs/2026-09-28-connections-credentials-design.md`), server-side only, never embedded in a bundle or distributed build (`client.md`), never logged, never bundle-visible as plaintext |

# Bar Citizen v3.0 Design — Greenfield Bundle Build

**Status:** Draft — objectives need owner confirmation before implementation starts.
**Date:** 2026-09-29
**Scope:** New app bundles (`waddles.core.barcitizen.*`, `waddles.core.starcitizen.*`), cross-server role management (#497), event sync (#96), plus the hub-api/data-plane pieces they depend on.
**Driver:** 2026-09-29 release-plan review — Bar Citizen (2nd-biggest customer) has never received any features, v2 included. Per owner scope notes on #484/#485/#486/#497/#96 (all 2026-09-29): **this is a greenfield v3 build against Bar Citizen's objectives, not a parity port.** There is no legacy behavior to preserve; acceptance criteria in the issues themselves are placeholders pending this spec.
**Builds on (read, reconciled, not re-litigated):** `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md` (#419, MERGED — permission catalog, capability gate, `storage.tables` schema derivation), `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md` (#415/#430/#498 — one-table-per-bundle DDL), `docs/superpowers/specs/2026-09-28-connector-bundles.md` (#444 — `waddle:connector@1.0.0` WIT world, `connector.receive:<platform>`/`connector.send:<platform>`/`connector.pii.read`, `waddles_connector_pii_reader` RO role), `docs/reference/bundle-network-permissions.md` (#466 — three-family egress risk model), `docs/superpowers/specs/2026-09-28-v3.0-python-removal-plan.md` (wave/slice format this doc follows).
**Review:** Gemini second-opinion pass (critical perspective) against this doc's own draft and the platform's PII/capability-gate/connector invariants. Findings folded in: removed a raw Odoo-partner-id column that leaked PII into bundle storage (§2.2, since superseded — see Update below), corrected the Spectrum connector's transport model to match #444's host-owns-all-sockets design instead of requesting `net.http.fqdn` on a receiver-only bundle (§2.3), eliminated a duplicate event-data table between the read-only bundle and the §4.1 canonical events table (§2.1), clarified where RSI-handle tokenization actually happens relative to connector `on-frame` delivery (§5.1), made bulk role-op rate limiting host-enforced instead of bundle-trusted (§3.4), added a tag re-injection loop-prevention rule (§4.2), and split Discord E2E into mocked-PR-gate + nightly-live-scheduled-job to avoid the fork-PR-secret and shared-test-guild-concurrency problems (§7).

**Update (2026-09-29, owner decisions this session):**
1. **No Odoo — Bar Citizen replaced Odoo with Wix.** §2.2 is rewritten around the Wix platform (REST/Headless APIs, Members, Events, Pricing Plans, webhooks). #95 (Wix platform integration) is re-milestoned to v3.0.x and folded into this design; #485/Odoo is superseded.
2. **v3.0 role sync is Twitch↔Discord (cross-platform), narrowed scope:** subscriber tiers (T1/T2/T3, Twitch→Discord only — subs are read-only on Twitch) and moderators (default Twitch→Discord; Discord→Twitch mod assignment is an **ASSUMPTION** pending owner confirmation, flagged as a privilege-escalation vector). VIP and follower sync are explicitly **removed** from v3.0 scope. §3 is rewritten; cross-guild sync is retained as in-scope (same mechanism), no longer framed as a stretch goal.
3. **Shared guilds:** an owner requirement surfaced that more than one Bar Citizen community may map to the same Discord guild. §3.0 (new) covers routing, role ownership, and the tenant boundary for this case; a platform gap was found (hub-api's `ingest_sources` table enforces one community per guild today) and filed as a new dependency (see §6).
4. **Second Gemini second-opinion pass** (critical perspective, run against §2.2/§3/§5.2/§8's Wix, Twitch↔Discord, and shared-guild changes): 5 findings returned, folded in below — (a) native/manual Discord role assignment can put a user into another community's role state without ever going through `managed_roles`'s write-path check, so any **read** of role state used to drive a downstream sync (e.g. Twitch mod status) must re-verify ownership, not just trust the role's presence (§3.0, new caveat); (b) a **race condition**: registering a pre-existing (not bot-created) Discord role into `managed_roles` needs stronger provenance than "first admin to ask" — an admin from community A could harvest and pre-claim community B's role ids (§3.0, new caveat, mitigation open as a build-time item); (c) the Discord→Twitch moderator confirmation step is now **mandatory, not optional** (§3.2, fixed directly); (d) Wix webhook replay protection is now stated explicitly as part of the same host-side, pre-parse verification step, not a separate later check (§2.2, fixed directly); (e) rendering a Wix/Twitch/Discord member's name anywhere downstream (e.g. a Discord welcome message) must go through the existing `connector.pii.read`/`identity.lookup` detokenization gate (§5.1 step 5) — never a value cached in `storage.tables` — with the existing 5-minute per-replica cache (#444 §3.4) as the load-bounding mitigation for the lookup-per-render pattern this implies (§5.2, fixed directly). (a) and (b) are flagged as **build-time hardening items**, not resolved by a doc change alone — see §3.0 and §8.

---

## Open questions for Bar Citizen (owner takes these to the customer)

1. **Event/data source for the read-only bundle (#484):** does Bar Citizen publish events via a public API (Discord, a community calendar, a website with structured data), or would ingestion require scraping a page with no API? Scraping carries ToS/fragility risk (§2.1) — need the actual source(s) before `net.http.fqdn` targets can be finalized.
2. **Wix app scope (#95, replaces the former Odoo question):** can Bar Citizen create a scoped Wix OAuth app / API key for their site, and which Wix apps do they actually use — Members, Events, Stores, Pricing Plans (memberships/paid tiers), some combination? This decides which webhook subscriptions and outbound scopes §2.2 actually needs.
3. **Spectrum organization details (#486):** confirm the RSI org handle/URL and whether Bar Citizen accepts the one-way-sync-only limitation (no posting back to Spectrum — RSI ToS risk, per #101).
4. **Guild/community topology (#497):** confirm Bar Citizen's total Discord guild count, and whether any of those guilds already host (or are expected to host) more than one Bar Citizen community (§3.0's shared-guild case) — needed to size the routing/ownership work in §3.0 and §6.
5. **Discord→Twitch moderator assignment (#497, new):** does Bar Citizen actually want a Discord role grant to be able to make someone a Twitch moderator? Default in this spec is **off** (Twitch→Discord only) — see §3.2's ASSUMPTION and §8's privilege-escalation entry.
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

### 3.0 Shared guilds (N communities : 1 Discord server)

**Owner requirement 2026-09-29:** more than one Bar Citizen community may be assigned to the same Discord guild.

**Current-model gap (found by inspection, not assumed):** hub-api's `ingest_sources` table — the row that maps a platform+external id (a Discord guild, a Twitch channel) to a community — has `UNIQUE (tenant_id, platform, source_id)` (`alembic/versions/0020_ingest_sources_rbac.py:84`), and `ingest_source_service.create_source()`'s pre-insert existence check (`hub_api/services/ingest_source_service.py`, the `existing = await install_dal(...)` query) filters only on `(tenant_id, platform, source_id)` — **not** `community_id` — so a second community cannot register the same guild today; the second `create_source()` call 409s. **The platform does not support N communities per guild yet.** This is new work, not a reinterpretation of existing capability — filed as a dependency (§6) and a new issue (below), not designed further in this document beyond the schema/routing shape it implies.

**Routing (message/command/interaction → community):** once the schema gap above is closed, resolution must be deterministic:

1. **Channel-level binding first** — if the interaction's channel (or its parent category) is explicitly bound to one community, use it.
2. **Explicit community tag/prefix** — a command argument or a per-channel default naming convention (e.g. a bot-configured "default community" per channel) resolves next.
3. **Guild-level default community** — if the guild has exactly one community with no channel-level override, use it (this is today's implicit 1:1 case, preserved as the degenerate case of the new model).
4. **Ambiguous — fail closed.** If none of the above resolves to exactly one community, the interaction is rejected with a user-visible hint ("this channel isn't linked to a specific community — ask an admin to bind it") — **never** broadcast or applied to all candidate communities. This is the same fail-closed posture as every other capability-gate denial in this design (§3.8, §8).

**Role ownership (community A must never touch community B's roles):** a new **role-ownership registry**, `managed_roles` (hub-api-owned, alongside `role_mappings` in §3.1):

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `tenant_id` | `uuid` | |
| `platform` | `text` | `discord`, `twitch` |
| `guild_or_channel_ref` | `text` | External Discord guild id or Twitch channel id |
| `external_role_ref` | `text` | Discord role id, or a synthetic ref for Twitch moderator status (Twitch has no per-status "role id" — the ref is `(channel_ref, 'moderator')`) |
| `owning_community_id` | `uuid` | The one community allowed to grant/revoke this role |
| `role_kind` | `text check in ('sub_tier1','sub_tier2','sub_tier3','moderator')` | |
| `name_prefix_template` | `text nullable` | e.g. `"[{community_tag}] {role}"` — see naming convention below |
| `created_by` / `created_at` | | |

Every `role.member.grant`/`revoke` (Discord) or moderator-add/remove (Twitch) call resolves its target role/status against `managed_roles` **by external ref, never by name** (see spoofing note below), and is rejected at the capability gate if the invoking community doesn't own that row. **Conflict detection:** attempting to register a `managed_roles` row for an `external_role_ref` already owned by a different community in the same tenant is rejected at creation time with an explicit error (not a silent overwrite); the same rule applies to a Twitch channel↔community binding via `ingest_sources`. Removals (§3.2) only ever touch roles the invoking community owns — the sync engine's revoke path filters candidate roles through `managed_roles` before issuing any call, same as grants.

**Role naming convention (owner suggestion, human-readable ownership signal — not the authorization mechanism):** roles the bot *creates and manages* in a shared guild get a per-community prefix, default template `"[<community tag alias>] <role>"` (e.g. `[BCSEA] Sub T2`, `[BCSEA] Mod`), configurable per community. Constraints: Discord role names are capped at 100 characters — the prefix + role name must fit, truncating the role name (never the prefix) if needed; the tag alias is sanitized (no `@`, no markdown, emoji stripped or normalized) before use. **Ownership and authorization are tracked by Discord role ID in `managed_roles` above, never inferred from the name** — a guild admin can rename any role, and anyone with Manage Roles can create a look-alike `[BCSEA] Mod`; name-based ownership is spoofable and is called out as such in §8. If a managed role is renamed in Discord, tracking continues by ID; re-applying the configured prefix on detected drift is a per-community, off-by-default setting (audited either way). Adopting an existing, unprefixed role into `managed_roles` requires an explicit admin action — the sync engine never auto-adopts a role it didn't create.

**Events in a shared guild (#96):** a Discord scheduled event can belong to several communities sharing that guild — one underlying Discord event, many `community_tags` (§4.2) on the canonical row; §4.2's dedupe/loop-prevention logic already operates on the canonical event id, so a shared-guild event is a single row with a multi-value `community_tags` array, never N duplicate canonical rows.

**Read-state authorization gap (second Gemini review finding, build-time hardening item):** `managed_roles` gates the bot's own **write** path (grant/revoke), but a guild admin can assign or remove a role natively in Discord, outside the bot entirely. If any downstream logic (e.g. deciding whether to sync a Discord role into Twitch moderator status) treats "the user currently holds role X" as sufficient authorization without re-checking `managed_roles.owning_community_id` for that specific read, a native, bot-bypassing role assignment in a shared guild becomes a path to cross-community (or Discord→Twitch privilege-escalation, §3.2) state changes. **Mitigation:** every sync decision that reads a role's presence, not just every grant/revoke that writes one, must resolve the role through `managed_roles` first and treat an unregistered or wrongly-owned role as absent for that community's purposes — this is a sync-engine implementation requirement, not just a gate-time check, flagged for the C1 slice (§6).

**Role-registry race condition (second Gemini review finding, build-time hardening item):** `managed_roles`' "reject on conflicting registration" rule (above) only helps once a role is registered — it does not stop a community A admin from harvesting a public, pre-existing Discord role id belonging to community B's intended role and registering it as A's first, before B ever configures the bot. **Mitigation, required at build time:** registering a **pre-existing** (not bot-created) role into `managed_roles` must require verifying the registering admin's provenance beyond "has Manage Roles in the guild" — at minimum, cross-checking against the channel/community routing binding (§3.0's routing rules) that the registering admin is acting within their own community's bound channels/category, and surfacing every new registration of a pre-existing role to all communities sharing that guild (an admin-visible audit event, not just a log line) so a wrongful claim is discoverable quickly. Bot-*created* roles are not exposed to this race — the creating community is unambiguous at creation time.

**Tenant boundary for shared guilds — ASSUMPTION, confirm with owner:** default proposal is **a Discord guild binds to exactly one tenant**, with multiple communities allowed only *within* that tenant; a guild bind request naming a tenant that conflicts with the guild's existing binding is rejected at bind time, not just at sync time. **Why not allow cross-tenant guild sharing:** the bot's Discord permissions are granted at the guild level (a single bot install, single OAuth grant) — there is no per-community scoping of the bot's own Discord permissions, so a tenant admin configuring a role mapping in a cross-tenant-shared guild could, at minimum, enumerate or interfere with another tenant's roles/channels through the host's structural guild-state cache (#444's "host maintains a non-PII structural guild-state cache", §3.3) even if `managed_roles` ownership checks block the write path — a high-risk widening of the tenant boundary for no confirmed customer need. This is flagged in §8 and gated behind an explicit owner confirmation before any cross-tenant guild binding is ever allowed.

### 3.1 Platforms and roles modeled

| Platform | Role/status | Twitch API assignability | v3.0 direction |
|---|---|---|---|
| Twitch | Subscriber tier (T1/T2/T3) | **Read-only** — subscriptions cannot be granted via API, only observed | Twitch → Discord only |
| Twitch | Moderator | **Assignable** via Helix with `channel:manage:moderators` | Twitch → Discord (default); Discord → Twitch is an **ASSUMPTION**, §3.2 |
| ~~Twitch~~ | ~~VIP~~ | ~~Assignable via `channel:manage:vips`~~ | **Removed from v3.0 scope** |
| ~~Twitch~~ | ~~Follower~~ | ~~Read-only~~ | **Removed from v3.0 scope** |

Role mapping table (hub-api-owned, cross-community config — a role mapping spans multiple guilds/communities within one tenant, so it doesn't fit the one-table-per-bundle AppScoped model cleanly):

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid pk` | |
| `tenant_id` | `uuid` | Hard tenant boundary (§3.8) |
| `community_id` | `uuid` | Owning community — must match `managed_roles.owning_community_id` for the resolved target (§3.0) |
| `condition_type` | `text check in ('twitch_sub_tier1','twitch_sub_tier2','twitch_sub_tier3','twitch_moderator','discord_moderator')` | Grant trigger — narrowed from the original generic `membership`/`reputation_threshold`/`identity_verified` set to the actual v3.0 conditions; reputation/verification-driven role sync (#497's original broader framing) is deferred, not built in this slice |
| `platform` | `text` | Target platform: `discord`, `twitch` |
| `target_guild_ref` / `target_channel_ref` | `text` | External Discord guild id or Twitch channel id, resolved server-side (§3.8) |
| `target_role_ref` | `text` | Discord role id, or the synthetic Twitch-moderator ref (§3.0) |
| `created_by` | `uuid` | Admin who configured the mapping |
| `created_at` / `updated_at` | `timestamptz` | |

### 3.2 Sync direction and conflict rules

- **Subscriber tiers: Twitch → Discord only.** Twitch subscriptions are read-only via Helix/EventSub — there is no "grant a subscription" call — so this direction is fixed, not configurable.
- **Moderator: Twitch → Discord is the default direction.** A Discord role change never attempts to grant/revoke Twitch moderator status unless the per-community opt-in below is explicitly enabled.
- **Discord → Twitch moderator assignment — ASSUMPTION, confirm with owner (open question 5):** if enabled, this requires `channel:manage:moderators` and is treated as a **privilege-escalation vector** (§8): a Discord role grant would make someone a Twitch moderator. Mitigations, all required together if this direction ships: (1) **off by default**; (2) an **explicit per-community opt-in**, configured by a tenant admin, not a global default; (3) **audited** (§3.7) with the triggering Discord role change recorded alongside the resulting Twitch API call; (4) **mandatory** (not optional, per second Gemini review) — the target must already have a confirmed Twitch↔Discord identity link (§3.3) before the grant executes; a Discord role change for an unlinked user must never trigger a Twitch API call.
- **One-way, internal-truth model per direction:** for whichever direction a mapping runs, WaddleBot's own observed condition state (Twitch sub tier, Twitch/Discord moderator status) is authoritative for that direction; the target platform's role state is driven to match it, never read back to change the source platform's state.
- **Conflict rule:** last-internal-write-wins per `(member, platform, target_role_ref)`, scoped to roles the invoking community owns (§3.0). Before issuing a grant/revoke call, the host consults its own structural guild-state cache (#444) to skip redundant API calls when the member already holds/lacks the role.
- **Idempotency:** each sync operation carries a dedupe key `(member_ref, target_role_ref, action)`; the host's own cache plus Discord's PATCH-role-list semantics and Twitch Helix's idempotent moderator add/remove calls make repeated syncs safe to retry.

### 3.3 Account linking (Twitch ↔ Discord identity)

A Twitch user and a Discord user are linked only as UUIDs in bundles, via hub-api's identity service — the same pattern as §5.1's RSI-handle linking:

1. A user (or admin, for bulk cases) initiates linking via a Discord command or a webui flow.
2. hub-api runs **OAuth on both platforms** (Twitch OAuth for the Twitch identity, Discord OAuth — already established for existing Discord identity — for the Discord side) and calls `IdentityService.LinkExternalIdentity` twice (once per platform) against the same `hub_user_uuid`, extending the existing `hub_user_identities` mapping (#449).
3. Neither platform's raw handle/username is stored in bundle `storage.tables` — only the resulting `member_ref`/`hub_user_uuid`, per the standing PII invariant (§5).
4. Twitch and Discord OAuth tokens themselves are stored via the credential broker, never in bundle storage, and scoped per §3.4's minimal-scope list.

### 3.4 Twitch Helix API: scopes and rate/slot limits

- **Minimal token scopes:** `channel:read:subscriptions` (subscriber tier read), `moderation:read` (current moderator list read) — always required. `channel:manage:moderators` is requested **only if** the Discord→Twitch moderator opt-in (§3.2) is enabled for at least one community on that Twitch channel; it is never requested by default.
- **Inbound transport reuses the already-planned Twitch EventSub connector** (§2's connector roadmap already lists "Twitch EventSub (receiver)" as v3.0 work) — subscriber-tier and moderator-status changes arrive via the matching EventSub subscription types (e.g. subscribe/subscription-message/subscription-end for tiers, moderator-add/moderator-remove for mod status), not a separate poll loop. **ASSUMPTION:** the exact EventSub subscription-type-to-scope pairing needs confirming against Twitch's current EventSub reference at implementation time — not fabricated here.
- **Twitch API rate limits:** Helix enforces its own per-app/per-token rate-limit bucket, independent of Discord's; every moderator grant/revoke call is subject to it in addition to any host-side pacing this design adds. Exact bucket sizing is an implementation-time lookup against Twitch's published Helix rate-limit documentation, not asserted here.
- **Slot limits:** Twitch does not impose a documented hard cap on moderator count (unlike the now-removed VIP slot limit) — no slot-limit handling is required for this design's moderator sync.

### 3.5 Discord connector permissions needed

Built on #444's connector layer (per the issue's own instruction — "not a bundle-local Discord API client"):

- `connector.send:discord` for role grant/revoke — #444's `sender` spec enumerates `role.create`/`role.edit`/`role.delete` (guild-level role CRUD) but **not** a per-member grant/revoke action kind. **ASSUMPTION — confirm with #444's owners:** this spec proposes two new sender action kinds, `role.member.grant` and `role.member.revoke`, following the same `build-request` pattern as the existing role CRUD kinds.
- Permission-check reads use #444's host-maintained structural guild-state cache (roles/channels/permission overwrites — explicitly non-PII, §"Permissions checks" in #444, exact query API still an open item there).
- No new WIT interface needed beyond the two action kinds above — this rides entirely on `connector@1.0.0`'s existing `sender` shape.

### 3.6 Rate limits (both APIs, bulk apply)

- Per-call: existing relay `UsageBatcher` limits (#444/#419 wit-v1.1 §2) apply to every `role.member.grant`/`revoke` call on Discord; Twitch Helix's own token-bucket (§3.4) applies independently to every Twitch moderator call. The two are never conflated into one shared budget.
- Bulk apply/remove: capped at 50 members per operation, per platform. **Enforcement is host-side, not bundle-trusted** — the bundle may batch its own calls, but the host's rate limiter (Discord's `UsageBatcher`, Twitch's own Helix-facing limiter) is the actual gate on every individual call regardless of how the bundle paces them. Defense in depth against a compromised or buggy bundle fanning out into a platform-side rate-limit storm.

### 3.7 Audit logging

One audit event per role change: `(member_ref, target_role_ref, platform, condition_type, owning_community_id, actor, timestamp, result)`. For the Discord→Twitch moderator opt-in (§3.2), the audit row additionally records the triggering Discord role change that caused the Twitch call. Reuses the existing hub-api audit-log sink (same one `bundle_capability_gate::authorize()` already writes denial/grant events to, #419 §5). Satisfies #497's AC ("bulk apply/remove ... completes with an audit log entry per change").

### 3.8 Tenant boundary

**Hard constraint, not UI-only** (#497 AC is explicit on this): `target_guild_ref`/`target_channel_ref` is resolved server-side against the invoking community's own tenant — never taken from bundle input. The capability gate's `ResourceRef` derivation (#419 §5.2, `AppScoped`, server-side derivation) rejects any role-mapping row or sync operation whose resolved guild/channel doesn't belong to the same tenant as the community that owns the mapping, and (§3.0) whose target role isn't owned by the invoking community. **Regression tests required:** cross-tenant role sync rejected at the gate layer (existing #497 AC); community A cannot modify a role owned by community B in a shared guild; an ambiguous shared-guild routing case fails closed rather than broadcasting; a cross-tenant guild-bind attempt is rejected at bind time (§3.0). None of these are UI-only checks.

### 3.9 Scope checks via the capability gate (#428)

Every `role.member.grant`/`revoke` (Discord) and moderator-add/remove (Twitch) invocation goes through `CapabilityGate::authorize()` (#428) with a host-constructed `InvokeScope` (never guest-influenced, #419 §5.1 Gemini condition 1) carrying the resolved tenant/community/guild-or-channel triple. `dangerous`-risk classification applies to both directions (guild role management and Twitch moderator management both cross a trust boundary per #419's risk-level rule) — reviewed at all three consent tiers (§3 of #419) on first install, not auto-approved. The Discord→Twitch moderator opt-in (§3.2) additionally requires its own explicit per-community consent grant, separate from the base Twitch↔Discord sync install.

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
| **hub-api N:1 guild-community binding (new)** | **#500** | **Any shared-guild role sync (§3.0) or shared-guild event tagging (§4.2) — blocks §3/§4 wherever a guild hosts more than one Bar Citizen community** |

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
       │      (§3.5, ASSUMPTION) + Twitch EventSub connector (§3.4) ──► shared-guild cases also need
       │      #500 (hub-api N:1 guild-community binding)
       │
       └─► §4 Event sync (#96) ──► shared-guild tagging also needs #500
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
| A4 | hub-api N:1 guild-community binding + deterministic routing (#500) | #430/#498 |
| B1 | Discord sender `role.member.grant`/`revoke` action kinds (pending #444 owner confirmation, §3.5) | #444, #461 |
| B2 | Discord sender `scheduled_event.create`/`edit`/`delete` wiring for event sync outbound | #444, #461 |
| B3 | Spectrum connector receiver: org linking + roster/event inbound parsing | #444, #461, #449 |
| B4 | RSI handle link/verify flow + unlinked-handle placeholder UUIDs | #449, #464 |
| B5 | Twitch EventSub connector receiver: subscriber-tier + moderator-status inbound (§3.4) | #444, #461, #449 |
| B6 | Wix webhook connector receiver: Members/Events/Pricing Plans inbound + `member_ref` tokenization (§2.2, §5.2) | #444, #461, #449 |
| C1 | Role sync engine: condition evaluation → grant/revoke dispatch, dedupe cache, audit log, role-ownership check (§3.0) | A2, B1, B5 |
| C2 | Event sync engine: tag filtering incl. shared-guild multi-tag (§4.2), dedupe/idempotency, timezone rendering | A3, B2, B3, B6 |
| C3 | Twitch↔Discord account linking flow (§3.3) | #449 |
| C4 | Discord→Twitch moderator opt-in (§3.2) — only if owner confirms (open question 5) | C1, C3 |
| D2 | Bar Citizen full bundle: Discord read/write wiring reusing B1/C1 patterns, Wix wiring reusing B6 | C1, B6 |
| E1 | Cross-tenant role-sync regression test + capability-gate denial test (§3.8) | C1 |
| E2 | Spectrum health-check probe + kill-switch flag wiring (#101 AC) | B3 |
| E3 | Shared-guild regression tests: cross-community role-ownership denial, ambiguous-routing fail-closed, cross-tenant guild-bind rejection, **read-path `managed_roles` re-check (native-assignment bypass), pre-existing-role registration provenance check** (§3.0, §3.8, 2nd Gemini review) | A4, C1 |

---

## 7. Testing

| Layer | Coverage |
|---|---|
| Unit | Tag parse/inject library; role condition evaluation; timezone conversion; RSI handle tokenization/placeholder logic; Wix webhook payload → `member_ref` mapping; role-ownership resolution (§3.0); shared-guild routing precedence (§3.0) — 90%+ per `critical-rules.md` |
| Integration | Capability-gate denial paths (cross-tenant role sync, cross-tenant event read, **cross-community role-ownership denial in a shared guild**, **cross-tenant guild-bind rejection**); db capability writes against real Postgres; connector `identity.lookup` against a seeded RO replica; **ambiguous shared-guild routing resolves to a fail-closed rejection, never a broadcast** |
| E2E | **Every standard PR-gate run mocks Discord and Twitch** (gateway/EventSub frames + REST responses) — masked CI secrets are unavailable to fork-originated PR runs in most CI setups, so a live-bot test cannot be a hard PR gate. The **live Discord round trip runs on a scheduled/nightly job against the release branch only** (never fork PRs), using the **separate CI-only bot** (dedicated disposable test guild, bot token as a masked CI secret per `critical-rules.md` Token & Secret Hygiene — never the production bot, never a synthetic-forged envelope), serialized (mutex/lock, one run at a time) against that guild to avoid concurrent-run role/event collisions: role grant/revoke visible in the test guild, scheduled event create/update relayed, **two-community shared-guild fixture exercising role-ownership denial**. Twitch moderator/sub-tier sync is tested against EventSub fixtures only in PR-gate runs; a live Twitch round trip (test channel, disposable mod token) follows the same nightly-only pattern as Discord. Spectrum connector is tested against a fixture/mock only, in both PR and nightly runs — no live RSI calls anywhere in CI (undocumented API, ToS/Cloudflare-block risk). Wix webhook signature verification is tested against fixture-signed payloads, no live Wix site required |
| Coverage | 90%+ lines/branches/functions/statements, gate blocks below threshold |
| OTel | `role_sync.grant`/`role_sync.revoke`/`role_sync.denied` counters (labeled by platform and `role_kind`); `event_sync.upsert`/`event_sync.conflict` counters; histogram on Spectrum poll latency and Wix/Twitch API call latency; spans across the identity-lookup and capability-gate calls; structured logs (penguin logging, not hand-rolled) for every grant/deny/sync decision at INFO, verbose branch detail at DEBUG — including every shared-guild routing decision and its resolution path (§3.0) |

---

## 8. Security review notes

| Threat | Mitigation |
|---|---|
| Role-escalation via sync | Role grants are `dangerous`-risk, reviewed at all 3 consent tiers (#419 §3); `InvokeScope` host-constructed only (no guest-supplied scope); cross-tenant guild resolution rejected at the gate (§3.8) with a regression test |
| **Discord→Twitch moderator privilege escalation (new)** | A Discord role grant could make someone a Twitch moderator (`channel:manage:moderators`) — a real-world privilege gain, not just an internal-state change. Mitigated by: off by default; requires an explicit per-community tenant-admin opt-in (§3.2); every grant audited with the triggering Discord change recorded (§3.7); optional confirmation step; flagged **ASSUMPTION, confirm with owner (open question 5)** — the base design ships with this direction disabled |
| **Cross-tenant Discord guild sharing (new)** | The bot's Discord permissions are guild-wide with a single OAuth grant — a tenant admin in a cross-tenant-shared guild could enumerate or interfere with another tenant's role/channel structure via the host's structural guild-state cache even where write-path ownership checks (§3.0) hold. Mitigated by treating "one guild = one tenant" as the hard default, rejected at bind time; cross-tenant sharing is **ASSUMPTION, confirm with owner** and not built without it (§3.0) |
| **Ambiguous shared-guild routing (new)** | A message/command/interaction in a shared guild that doesn't resolve to exactly one community must never be applied to all candidate communities by accident. Mitigated by the deterministic precedence + fail-closed rule in §3.0, with a regression test (§3.8, §7) |
| **Role-name spoofing in shared guilds (new)** | A community-prefix naming convention (e.g. `[BCSEA] Mod`, §3.0) is a human-readable ownership signal only — a guild admin can rename any role, and anyone with Manage Roles can create a look-alike prefixed role. Mitigated by tracking ownership and authorization exclusively by Discord role ID in the `managed_roles` registry (§3.0), never by name; adopting an existing unprefixed role into the registry requires an explicit admin action, never auto-adoption |
| Cross-tenant leakage | Every `storage.tables` row and role-mapping row is tenant-scoped at the schema/gate layer, never bundle-trusted; event canonical table filters by `tenant_id` on every read |
| **Cross-community leakage within a shared guild (new)** | Community A must never grant, revoke, or read the ownership state of a role owned by community B in the same guild. Mitigated by `managed_roles`' `owning_community_id` check at the capability gate (§3.0, §3.8) on every grant/revoke/removal path, with a dedicated regression test |
| **Native Discord role assignment bypassing `managed_roles` (new, 2nd Gemini review, build-time item)** | A guild admin assigning a role natively in Discord (not via the bot) never touches the write-path gate — any sync logic that reads "does this user hold role X" without re-checking `managed_roles` ownership for that read is a bypass. Mitigated by requiring every read used for a sync decision, not just every write, to resolve through `managed_roles` first (§3.0); unregistered/wrongly-owned roles are treated as absent |
| **`managed_roles` registration race — role-id harvesting (new, 2nd Gemini review, build-time item)** | A community-A admin could pre-claim a pre-existing Discord role id actually intended for community B (role ids are visible to anyone in the guild) by registering it first. Mitigated by requiring registration of a pre-existing role to cross-check the registering admin's own channel/community routing binding (§3.0) and by surfacing every new pre-existing-role registration as an admin-visible audit event to all communities sharing the guild, not just a log line; bot-created roles are unaffected (unambiguous owner at creation) |
| Scraping abuse / ToS violation | Spectrum connector is read-only by design (no `connector.send`), rate-limited, health-checked, and behind a fast kill-switch (#101 AC); Bar Citizen read-only source (§2.1) flagged for ToS confirmation before any scraping fallback is built (open question 1) |
| Egress abuse | Every bundle's `net.http.fqdn` targets (including §2.2's Wix API host) are explicit allowlist entries reviewed at install (#419 §1), never wildcards or IP literals; private-IP egress is not requested by any bundle in this design |
| PII leakage via connector | RSI handles and Wix member data (name, email) tokenize to `member_ref`/UUID before crossing into `storage.tables`/logs/telemetry (§5.1, §5.2); `connector.pii.read`/`identity.lookup` stays global-approved, core-only, RO-replica-scoped (#444 §3.3) |
| **Wix webhook forgery (new)** | An unauthenticated or replayed webhook claiming to be from Wix could inject fabricated member/event data. Mitigated by host-side JWT signature verification on the raw request bytes, before any parse or connector invocation — the same ordering #444 §2 already mandates for EventSub/Slack — plus a replay window, matching the existing webhook `Transport` family's discipline (§2.2) |
| Credential exposure (Wix OAuth app/API key, RSI session token) | Stored via the connections/credentials broker (`docs/superpowers/specs/2026-09-28-connections-credentials-design.md`), server-side only, never embedded in a bundle or distributed build (`client.md`), never logged, never bundle-visible as plaintext |

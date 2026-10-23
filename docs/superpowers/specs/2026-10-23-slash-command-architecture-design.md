# Slash-Command-First Command Architecture — Design

**Date:** 2026-10-23
**Status:** Final draft — awaiting human approval. Nothing is built until approved.
**Target:** Waddles v3 data plane (release/v3.0.X). **Phase 1 = Discord**; Slack/Mattermost designed-for, not built.

## Intent & Why

Today Waddles commands are chat/prefix-triggered (`!ping`): svc-ingest publishes every event on a source stream, every bound bundle's `transform` runs, and each bundle string-matches itself. We want **slash commands as the primary command UX for Discord** (later Slack/Mattermost) — discoverable, native — while keeping **app bundles as the brains** (all command logic stays in the bundle). svc-ingest/svc-action only learn *that a command is a `webhook` type and where to send the reply*. The same change replaces fan-out-to-every-bundle with registry + active-context routing, cutting wasted executor runs.

## Goals
- Slash commands on Discord, registered **per-guild**, driven entirely by **bundle activation** — no per-command code in ingest/action.
- A single shared **commands registry** (DB table) as the one contract between bundles and the data plane: drives (1) slash registration, (2) command→bundle routing, (3) delivery-type/response-transport. Ingest and bundles depend on the registry, never on each other.
- **Per-user active community context** resolves which community processes an event when a server/source is paired to multiple communities — killing the cross-community broadcast ripple.
- Bundles declare commands in their manifest → hub-api writes the registry → svc-ingest reads a **read-replica, ~1-min cache**.
- Backward compatible: existing prefix bundles work unchanged (type defaults to `standard`; registry auto-inferred from their existing `consumes`).
- Cross-platform-ready: normalized envelope + registry + active-context accommodate Slack/MM later with no bundle changes.

## Non-Goals
- Typed slash options in v1 — minimal `/verb args:<text>` → free-text through existing `parse_command`. Typed options = per-command opt-in later.
- Slack/Mattermost implementation now (design accommodates; Discord first).
- Replacing the Valkey spine or ingest→process→executor→action topology (unchanged).
- Per-tenant bot-token model (pre-existing gap; v1 uses the single shared bot token, bounding registration to that bot's guilds).
- Mapping Discord's `default_member_permissions`/command permissions to bundle scopes (v1: the bundle's own grant/scope checks still apply inside `transform`, same as message commands). Later.

## Anti-Goals (failure even if it technically works)
- Bundles knowing slash-vs-prefix or platform — they stay trigger/platform-agnostic.
- **Interaction tokens ever reaching bundle code** — host-held only (see token store).
- Breaking content-reactive bundles (moderation/counters/keyword) — they still see the full stream via `*`.
- Hitting the DB on the command hot path.
- Silently dropping events during cutover (fail-loud).

## Constraints (verified vs release/v3.0.X @881ac8dd)
- **No command registry today.** Routing = source-stream grant (`consumes`) + in-bundle string-match; `command_prefix` filter "not yet wired." Net-new.
- **StageEnvelope strict + MAC-bound** (penguin-spine; unknown top-level fields rejected; MAC covers ids/trace, not `event.payload`).
- **Discord 3-second ACK:** ingest sends the type-5 deferred-ack **itself** before the spine hop.
- **No slash handling today:** py-cord receiver explicitly skips slash registration; Rust path only MESSAGE_CREATE. Whole slash path greenfield. `core/svc_ingest/discord_rest.rs` = existing rate-limited bot REST client to extend.
- **svc-action always posts to a channel**; interaction followup is a new RelayOp.
- **One shared bot token** today.
- Existing ingest `ScopeResolver` resolves events → community by numeric ids (reused below).

## The Design (decided)

### 1. Commands registry (new table, `app_commands`)
Written by hub-api, read by svc-ingest via read-replica. Per row: `command` (verb, or **`*`** = "every event" for content-reactive bundles); `app_id` (FK app_catalog); `type` set (`standard` default / `webhook`); `platforms` set (discord/slack/mm); scope (tenant/community_id, 0 = tenant-wide); **registration order** (per community+command activation timestamp — drives ownership). Many-to-many: a command may map to multiple bundles. Declared via a new manifest `commands:` field; written on activation/approval (extends `bundle_approval_service`/`seed_core_bundles`).

### 2. User active community context (new)
Per **(user, source/server)** → community. Default = the **first-registered** community for that source. User switches with the built-in `!context` / `/context <community-or-id>` (bare = show current + the server's available communities). Stored in a new hub-api-owned table, read by the router under the same ~1-min cache. Platform-agnostic (Discord guild, Twitch channel, etc.). Persisted across sessions.

### 3. Routing (unified; replaces fan-out)
Per inbound event: (a) resolve the **community = invoking user's active context** for that source (reusing/extending `ScopeResolver`); (b) within that community, resolve the routing set = **union(bundles for the exact command, bundles for `*`)**; (c) route the normalized envelope to each via `target_app_id`. Command handlers wake only for their command (+ `*` observers); `*` is explicit firehose opt-in. Routing + context maps cached **~1 min** (read-replica) — no per-event DB hit. **Interaction normalization:** ingest renders the interaction into the **existing event/payload shape** (text reconstructed as `{prefix}{verb} {args}`, actor = the interaction invoker tokenized identically to the message path), plus a non-secret `trigger=webhook` marker — so existing bundles + `parse_command` are unchanged.

### 4. Slash registration & reconcile (svc-ingest Discord connector)
New supervised task polls the registry (~1 min) and reconciles **per guild**: desired set = the **union** of all communities paired to that guild, their `webhook`-type platform=discord commands; register missing / update changed / remove stale via guild-scoped `PUT /applications/{app}/commands` (instant; avoids global ~1h lag + leakage). (Slash is guild-wide, can't be per-user; the active context picks the processing community at invoke time. A command not active in the user's current context → a "not available in your current context — switch with /context" reply.) Connector begins handling `INTERACTION_CREATE`.

### 5. Delivery types & response path (host-side token store)
- `standard` → chat; svc-action replies via existing channel post (multiple bundles each posting is fine).
- `webhook` → slash; on receipt ingest **fires the type-5 defer immediately** (beats 3s) and **stores the interaction token in Valkey keyed by interaction id, ~15-min TTL** (matches interaction lifetime). The envelope carries only a **non-secret reference** (interaction/event id) + `trigger=webhook` — **the token is never in the envelope/payload, so bundles physically can't see it.** svc-action's new RelayOp looks the token up from the store and posts the real reply as the interaction **followup webhook** (`POST /webhooks/{app_id}/{token}`). Bundles stay oblivious: emit a logical response; action picks followup vs channel-post from the envelope marker. The logical-response contract gains an **ephemeral/visibility flag**.

### 6. Response ownership (multi-bundle on one slash command)
One interaction = one reply token. **First-registered non-wildcard bundle for that command+community owns the interaction response.** Additional non-wildcard + all `*` bundles receive the event and may act, but are **observers** for the interaction reply (their interaction replies are dropped by svc-action; they may still use other capabilities). The **router marks the owner's copy** in the envelope so svc-action enforces it without a second lookup. Standard/chat commands keep the free-for-all. Ownership order = per-(community,command) activation timestamp; deactivate removes from the set; reactivation re-queues at the end (can lose ownership).

### 7. Failure / timeout semantics
Ingest defers before any bundle runs, so a reply is owed. A **deadline watchdog** (host-side; keyed by interaction id, well inside the 15-min token window) sends a generic fallback followup if the owner errors, times out, is deactivated mid-flight, or produces no reply (triggers: executor failure, DLQ, or the deadline timer). Deactivated-but-still-registered command (reconcile lag) → the "not available in your current context"/"command unavailable" fallback. Multi-message/file replies deferred (v1 = one followup + the ephemeral flag).

### 8. Idempotency
Ingest **dedupes interactions by interaction id** (short-TTL store; reuse the `event_dedupe.claim` atomic-increment-TTL pattern) before publishing — Discord redelivery / gateway resume can't cause double-execution or double-followup. The stored token is consumed once for the initial response; followups are keyed so redelivery doesn't double-post.

### 9. Cutover / migration
The initial registry is **auto-inferred from existing bundles' `consumes`**: a `command_prefix` filter → a `standard` command row; a bare message subscription → a `*` row — so existing prefix bundles migrate **without manifest edits**. New `commands:` manifest field is for slash declarations + overrides. The registry-driven router ships **flag-gated**, validated for routing parity against today's fan-out before the flip; a bundle that ends up with no command and no `*` is detected and warned at activation (fail-loud, never a silent drop).

### 10. Cross-platform shape (designed-for, not built)
Slack/MM slash = HTTP-inbound (a webhook endpoint Waddles hosts; needs request-signature verification — Discord's gateway is socket-trusted) + `response_url` (~30 min). Same registry (`platforms`), same normalized envelope, same active-context resolution, same `webhook` type + host-side token store (store the `response_url` instead of the interaction token); only the receiver + response transport differ.

## What's left to the builder
1. Exact `app_commands` (+ user-context) table schemas, migrations, and whether user-context lives in its own table or extends an existing membership table — builder's call; must key on numeric ids.
2. Reconcile edge cases: safe "remove stale" (never delete a command another app legitimately owns in the guild), and `PUT …/commands` rate-limit batching across many guilds.
3. Per-tenant bot token — dependency/follow-up that generalizes registration beyond the shared bot's guilds. Not built here.
4. Typed slash options — deferred; per-command opt-in later.
5. Whether the deadline watchdog is a new supervised task in svc-action or a spine-side timer — builder's call.

## Calls the author made (check these while reading)
- **Token: host-side Valkey store** (not envelope/payload, not a schema bump) — chosen by the human.
- **Active-context model** for N:M community resolution, default first-registered, `/context` built-in — from the human.
- Interaction normalized into the **existing event shape** (synthesized `{prefix}{verb} {args}` + invoker actor) so bundles/`parse_command` are unchanged.
- **Union** slash registration per guild; active context picks the processing community; "not available in your context" when it isn't active there.
- **Owner marked by the router**; non-owner interaction replies dropped; ownership = activation order, reactivation re-queues.
- **Deadline watchdog + generic fallback followup**; ephemeral flag added to the response contract.
- **Ingest dedupe by interaction id** (reuse event_dedupe pattern).
- **Auto-infer initial registry from existing `consumes`**; **flag-gated, parity-validated cutover**; no-command+no-`*` = activation warning.
- **Accept the ~1-min cache lag** (per the chosen caching); graceful fallback for the deactivated-but-registered window.

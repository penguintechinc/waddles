# Calendar app bundle (Rust) + Discord scheduled-event sync

Status: DRAFT spec (2026-11-07), docs only. Supersedes the legacy `calendar_interaction_module` + hub-api event-sync engine.

## 1. Principles (locked)

- hub-api = gateway ONLY (external integrations, admin/management, config, scaling) -- no consumer features. Calendar leaves hub-api.
- Calendar = generic, configurable APP bundle, **Rust** (new bundles are Rust). No customer names anywhere.
- Service bundles own tools; app bundles invoke. Discord already exports `discord.scheduled_event.{create,update,cancel,delete}` (`bundles/rust/discord/src/tools.rs:89-94`, schemas `bundles/rust/discord/bundle.yaml:77-170`). App->service only, never app->app.
- App bundles are not tier-licensed. State is community-scoped. Lifecycle INSTALLED -> ENABLED -> ACTIVATED.

## 2. Goals / non-goals

| Goals | Non-goals |
|---|---|
| Events CRUD, approval workflow, upcoming/search, timezone, optional recurrence | Rebuilding Google/Microsoft OAuth calendar sync, booking pages, availability, tournaments (defer, see Q6) |
| Chat commands via shared `waddle-sdk` verb parser | New platform tools in hub-api |
| Discord scheduled-event sync as bundle logic via `discord.scheduled_event.*` | Other platforms' event targets (Twitch schedule etc.) -- later, same pattern |
| Admin/webui management through hub-api generic bundle-admin API | Calendar-specific hub-api routes |
| One-shot reversible data migration, then delete legacy | Ticketing parity in v3.0 |

## 3. Legacy inventory (to replace)

| Piece | Path | Fate |
|---|---|---|
| Module routes (~70: events, approve/reject/cancel, rsvp, search/upcoming/trending, sync, config, categories, tickets, admins, oauth, availability, booking, tournaments) | `action/interactive/calendar_interaction_module/app.py` (2010 LOC) + 15 services | events/rsvp/approval/config -> bundle; rest see Q6; module deleted |
| Discord push from module | `services/calendar_service.py:852` `_maybe_sync_to_discord`, `services/event_discord_sync_client.py` | Deleted |
| Sync engine | `hub_api/services/event_discord_sync_service.py` (`sync_event` :743, `DiscordScheduledEventTarget` :515, `run_event_sync_reconcile_batch` :898) | Deleted (logic re-homed in bundle) |
| Routes | `hub_api/blueprints/v1/event_discord_sync.py` | Deleted |
| Valkey connector queue | `hub_api/services/event_sync_connector_client.py` (`waddles:event-sync:discord:requests` / `:result:<id>`) | Deleted |
| svc-action capability | `core/svc_action/src/capabilities/event_sync.rs` (656 LOC) | Deleted |
| CronJob + secret | `k8s/helm/waddlebot/templates/event-sync-reconcile-cronjob.yaml`, `event-sync-request-secret.yaml` | Deleted |
| Tables | `calendar_events` (024 + ticketing cols, 0039), `calendar_event_discord_syncs` (098), `calendar_rsvps`, `guild_tenant_pairings.event_sync_enabled` | Migrated, then dropped |
| Flags | `FEATURE_EVENT_DISCORD_SYNC`, `DISABLE_CONNECTOR_EVENT_SYNC_FLAG` | Removed; activation is the gate |
| Webui | `admin/hub_module/frontend/src/pages/admin/AdminCalendarEvents.jsx` (349 LOC) | Replaced by generic bundle-admin view |
| `!event` kv bundle | `bundles/python/event` (kv: events + RSVPs) | Decision s8 |

Security finding closed by deletion: the Valkey result channel `waddles:event-sync:discord:result:<request_id>` is unauthenticated (anyone with Valkey access can forge a "confirmed" result). Removing the queue removes it; tool calls are host-mediated and grant-checked instead.

## 4. Architecture

```
 chat (!calendar ...)         webui (thin)             hub-api (gateway)
      |                           |                          |
      v                           v                          v
 ingest -> process-stage   /api/v1/.../bundles/{app}/*    generic bundle-admin API
      |        |                  |  (config schema, actions, views)
      |        v                  v                          |
      |   +-----------------------------------------+        |
      +-->|  calendar app bundle (Rust, WASM)       |<-------+ admin action
          |  commands | approvals | sync | reconcile|
          +--+---------+---------+--------+---------+
             |         |         |        |
        bundle table  kv       object    tool.call
        (data.tables) (cfg,    store     (host-mediated, grant-checked)
         per-community idem,   (opt:           |
         scope)        cursor) ICS export)     v
                                       discord SERVICE bundle
                                       discord.scheduled_event.{create,update,cancel,delete}
   periodic reconcile hook (core, PR feature/platform-op-host-seams) --> bundle reconcile()
```

## 5. Data model and where it lives

Decision: **bundle-owned table(s) via manifest `data.tables`**, partitioned per community by the host. `table_access` is NOT used: it is for platform tables, app-only, allowlist-narrow (`2026-11-06-bundle-table-access-grants.md`); calendar needs none -- target guild is bundle config, not a read of `guild_tenant_pairings`. kv holds config, idempotency keys, reconcile cursor only. Q1: confirm `data.tables` (bundle-owned SQL) is implemented in the host; if not, v3.0 falls back to kv (like `bundles/python/event`, whose manifest declares `data.tables: []`).

| Table (bundle-owned) | Columns (all carry numeric `community_id`; refs by UUID/numeric, never slug) |
|---|---|
| `events` | id BIGINT, public_id UUID, community_id, title, description, starts_at TIMESTAMPTZ (UTC), ends_at, tz TEXT (IANA), location, status SMALLINT enum (draft/pending/scheduled/cancelled/rejected), created_by_user_uuid, approved_by_user_uuid, recurrence_id NULL, version INT, legacy_id NULL |
| `recurrences` (v3.1) | id, community_id, rrule TEXT (RFC 5545 subset: FREQ/INTERVAL/BYDAY/COUNT/UNTIL), tz, exdates; instances materialised in a rolling window -- never unbounded |
| `rsvps` | event_id, community_id, user_uuid (NOT platform_user_id), status SMALLINT, guest_count, waitlist fields |
| `event_sync` | event_id, community_id, target SMALLINT (1=discord), target_ref, remote_id (discord event id), status SMALLINT (pending/synced/error), attempts, last_error_code, last_synced_hash, updated_at; UNIQUE(event_id,target,target_ref) |

- Timestamps UTC; `tz` is the author's IANA zone for rendering + recurrence expansion (DST-correct, `chrono-tz`).
- Status/target as int enum codes.
- PII: RSVP keyed by user UUID. Legacy `calendar_rsvps.platform_user_id` is mapped to UUID at migration; unmappable rows go to a reject report, not copied.
- Config (kv, community-scoped): `sync.discord.enabled`, `sync.discord.guild_ref`, `approval.required`, `default_tz`, `reminders`.

## 6. Commands + admin path

Chat verbs via shared `waddle-sdk` parser (set/add/sub/enable/disable/remove/list/reset; bare = read):

| Command | Meaning | Gate |
|---|---|---|
| `!calendar` / `!calendar list` | upcoming | everyone |
| `!calendar add <title> @ <when> [tz=] [loc=]` | create (pending if approval on) | scope `calendar:write` |
| `!calendar set <id> <field> <value>` / `remove <id>` | edit / cancel | owner or `calendar:approve` |
| `!calendar enable\|disable sync discord` | per-community toggle | community admin |
| `!rsvp <id> [yes\|no\|maybe]` | RSVP | everyone |

Admin/webui: **no calendar routes in hub-api.** ONE generic gateway surface reused by every app bundle: `GET/PUT /api/v1/communities/{id}/bundles/{app}/config` (JSON-Schema from manifest `config_schema`, validated by hub-api, stored community-scoped) and `POST .../bundles/{app}/admin/{action}` + `GET .../admin/{view}`, which hub-api authorizes (OIDC scope + tenant + community) and forwards to the bundle's declared admin actions/views. Webui renders list/form generically from the manifest-declared view schema -- zero calendar logic in webui. Q2: does a generic bundle-admin/config API exist yet? If not, it is PR 1 and the largest risk.

## 7. Event sync (bundle logic)

Flow: create/update/cancel/delete in bundle -> `event_sync` upsert (pending, same transaction) -> `tool.call discord.scheduled_event.<op>` -> store `remote_id`, status=synced; failure -> status=error, attempts++.

| Concern | Design |
|---|---|
| Idempotency | key = `sha256(community_id : event.public_id : op : event.version)`, kv-stored; replay of an applied key is a no-op. Create guarded by `remote_id IS NULL` + row lock so two workers never double-create |
| Mapping | `event_sync.remote_id` in bundle table (replaces `calendar_event_discord_syncs`); never Valkey |
| Payload | tool schema requires name<=100, description<=1000, start+end RFC3339, location 1-100: bundle defaults end=start+1h, location="Online" when absent (Q7); `last_synced_hash` skips no-op updates |
| Retry | inline 2 attempts w/ jitter inside the ~8s tool budget; then left pending/error for reconcile. Retry only transient (429/5xx/timeout); remote 404 => recreate once; 403 => status=error shown in admin view |
| Reconcile | bundle periodic reconcile hook (core, PR `feature/platform-op-host-seams`): bounded batch of `event_sync` where status!=synced, kv cursor, drift (remote 404) + orphan cancel. Replaces CronJob |
| Budget | one tool call per invocation; <=10s WASM budget |
| Grants | manifest `requested_tools` for the 4 `discord.scheduled_event.*` tools (all `risk: dangerous` => admin approval + activation consent; re-consent on permission_hash change) |
| Multi-guild | v3.0: one guild per community. Legacy multi-pairing fan-out -> Q4 |
| Kill-switch | per-community `sync.discord.enabled`; no env flag |

## 8. `bundles/python/event` (!event kv bundle)

Recommend **merge into calendar, retire `bundles/python/event`**: same domain (events + RSVPs), it is Python (new work must be Rust), and its kv RSVPs diverge from calendar RSVPs. Keep `!event`/`!rsvp` as aliases; kv events migrated in the migration PR. Not on the v3.0 critical path: keep the Python bundle until calendar is ACTIVATED on alpha. Q3: any real communities using kv events?

## 9. Approvals

- `approval.required` (config, default off). Create -> pending; approvers hold scope `calendar:approve` (checks on scopes, never role names).
- Verbs approve/reject/cancel (+reason); audit log with numeric community_id + event public_id only.
- Sync fires only on transition to scheduled; cancel/reject of a synced event => `discord.scheduled_event.cancel`.
- Requester notified via `chat.send`.

## 10. Ticketing

**Defer to v3.1+.** Legacy ticketing (`024`: ticketing_enabled, ticket types, check-in, transfer, refund_policy; `ticket_service.py` 1471 LOC) touches payments and needs economy-style service tools. Migration archives ticketing columns (read-only, not dropped). Ticket routes disappear with the module only after confirming zero live rows in `calendar_tickets` on alpha/beta (Q5).

## 11. Data migration (one-shot, reversible)

1. Admin job (one-off, not a recurring feature) reads `calendar_events` + `calendar_event_discord_syncs` + `calendar_rsvps`, writes bundle tables per community that has ACTIVATED the bundle; idempotent upsert on `legacy_id`.
2. `discord_event_id` -> `event_sync.remote_id`: existing Discord events are adopted, never duplicated. String statuses -> int enums. `platform_user_id` -> user UUID; unmappable -> reject report.
3. Dry-run prints per-community read/written/rejected counts; zero rows read = FAIL, not pass.
4. Reversible: legacy tables NOT dropped in the migration PR (drop is a later PR after one release of soak); reverse script replays bundle rows via `legacy_id`.
5. Legacy tables are dual-chain (SQL 098 + Alembic 0039): the later drop must hit both (fresh-replay guard).

## 12. Deletions (after soak)

| Area | Remove |
|---|---|
| hub-api | `blueprints/v1/event_discord_sync.py`, `services/event_discord_sync_service.py`, `services/event_sync_connector_client.py`, tests, flags, `guild_tenant_pairings.event_sync_enabled` |
| Legacy | `action/interactive/calendar_interaction_module/` (OAuth/booking/tournaments need disposition, Q6) |
| Core | `core/svc_action/src/capabilities/event_sync.rs` + registration |
| Helm | CronJob, request secret, values |
| Webui | `AdminCalendarEvents.jsx` + route |
| DB | `calendar_events`, `calendar_event_discord_syncs`, `calendar_rsvps` (ticketing archive retained) |
| Obs | dashboards/alerts referencing event-sync metrics (`k8s/observability`) |

## 13. Security

- Community scoping by host (tables partitioned on `community_id`); bundle never trusts community id from payload.
- Grants: only the four `discord.scheduled_event.*` tools; no `table_access`, no `connector.pii.read`, no broad egress; approval UI shows dangerous notice.
- Target guild comes from community-admin config, resolved by the discord service under the community's own connection -- cannot point sync at another tenant's guild.
- Logs/traces: community_id, event public_id, op, counts only; never titles/descriptions/handles. Metric labels bounded (`op`, `outcome`).
- Validation: lengths, RFC3339, IANA tz allowlist, RRULE subset + max instances; responses via explicit schema.
- Removes the unauthenticated Valkey result channel.

## 14. Tests

- Unit (Rust): verb parsing, tz/DST + recurrence, payload clamp, idempotency, state machine.
- Real e2e, flag-on (ACTIVATED), real WASM + real Postgres + real executor with a Discord service bundle exporting the 4 tools: create calls tool once; replay no dup; update only on hash change; cancel; retry on 5xx; reconcile heals drift; community A cannot read/sync community B; ungranted tool call denied.
- Migration: seeded legacy rows -> counts in/out, adoption of existing discord ids, reverse round-trip.
- Telemetry counts asserted (logs/metrics/spans); coverage 90%+.
- Live Discord smoke on alpha with the CI bot only, never the alpha token.

## 15. Rollout / rollback

1. Ship bundle INSTALLED, legacy untouched.
2. Alpha: ENABLE + ACTIVATE for a test community, sync OFF, verify; then sync ON against a test guild.
3. Migrator dry-run then live per community; legacy sync disabled for that community in the same step (adoption by `remote_id` prevents dupes).
4. Soak one release, then deletion PRs.
- Rollback: deactivate bundle, re-enable legacy flag, reverse-migrate via `legacy_id`. Legacy code stays until soak ends.

## 16. PR breakdown (ordered, independently mergeable)

| # | PR | Depends on |
|---|---|---|
| 1 | Generic bundle-admin/config API in hub-api + manifest `config_schema`/view schema (only if absent, Q2) | -- |
| 2 | `bundles/rust/calendar`: manifest, tables, events CRUD, commands, RSVP, approvals, tests | 1, Q1 |
| 3 | Sync: `requested_tools`, idempotency, mapping, retry | 2 (discord tools exist) |
| 4 | Reconcile via periodic hook | 3, `feature/platform-op-host-seams` |
| 5 | Generic webui bundle-admin view | 1 |
| 6 | Data migrator + dry-run + reverse script | 2, 3 |
| 7 | Alpha cutover (config only) + docs | 4-6 |
| 8 | Recurrence | 2 |
| 9 | Retire `bundles/python/event` | 7 |
| 10 | Deletions (hub-api sync, `event_sync.rs`, CronJob, legacy module, flags) | 7 + soak |
| 11 | Drop legacy tables (SQL + Alembic) | 10 + soak |

## 17. v3.0 (beta Wed 2026-11-11) vs v3.1

Today is 2026-11-07; alpha preview Mon 11-09, beta in 4 days.

| v3.0 | v3.1 |
|---|---|
| Realistically only this spec, plus PR 1 if Q2 = no. A minimal PR 2 (events + RSVP + commands, no approvals/recurrence/sync) fits ONLY if Q1 and Q2 are already yes (~3-4 agent-days + e2e) -- tight | PRs 3-11: sync, reconcile, webui view, migrator, cutover, recurrence, python merge, deletions; ~2-3 weeks incl. soak |
| **Keep legacy calendar + sync running for beta. Do not half-migrate or delete.** If the unauthenticated Valkey result channel is exposed in beta, harden it in place (sign/auth the result key) as a small hotfix | Full cutover |

Estimate (agent-days, parallel after PR 1/2): 1: 2-3; 2: 3-4; 3: 2; 4: 1-2; 5: 2; 6: 2; 7-11: 3 + soak.

## 18. Open questions

| # | Question |
|---|---|
| Q1 | Is bundle-owned SQL (`data.tables`) implemented in the host, or kv-only today? |
| Q2 | Does a generic bundle config/admin-action API exist in hub-api? If not, PR 1 blocks the webui path |
| Q3 | Merge `bundles/python/event` into calendar (recommended) -- any real data in its kv? |
| Q4 | Multi-guild fan-out (legacy pairings, N guilds per community): keep or one guild per community in v3.0? |
| Q5 | Ticketing: any live paid/ticketed events on alpha/beta? Confirm defer |
| Q6 | OAuth calendar sync, booking pages, availability, tournaments: delete, defer, or own bundles? |
| Q7 | Should the discord tool contract make `location`/end-time optional (schema requires both today)? |

# calendar (Rust APP bundle)

`waddles.core.app.calendar` -- community events, RSVP, approvals, chat commands, admin actions,
and push-only event sync to Discord scheduled events (plus config-gated Twitch schedule / Wix
events adapters). WASM component on the `stage-next-tools` world; it CALLS service-bundle tools
(app -> service only, never app -> app). Spec: PR #1099 (calendar) + #1101 (catalog).

## Commands (`!calendar`; shared verbs, bare = read)

| Command | Who |
|---|---|
| `!calendar` / `list` / `view <id>` | everyone |
| `add <title> @ <when> [tz=Zone] [end=<when>] [loc="Place"]` | mod/broadcaster; any linked member when `approval_required` (lands `pending`) |
| `set <id> <title\|description\|start\|end\|tz\|location> <value>` / `remove <id>` | owner or mod/broadcaster |
| `approve <id>` / `reject <id> [reason]` | mod/broadcaster |
| `rsvp <id> [yes\|no\|maybe]` | linked members |

`<when>` is RFC 3339 or `YYYY-MM-DD HH:MM` in the event timezone (DST gaps/ambiguity rejected).
Values with spaces must be double-quoted. The parser also accepts the legacy `!event ...` and
`!rsvp <id> yes|no|maybe` heads, but the manifest only consumes `!calendar` for now: the Python
`bundles/python/event` still owns `!event`/`!rsvp` and two bundles must not answer one command
(`test_bundle_command_collisions`). Widen `command_prefix` in the PR that deletes that bundle.

## Storage: community kv (not `data.tables`)

The host `db` capability is ONE table per bundle with insert/get/`query(limit, offset, order)`/
update/delete and no filter, so upcoming/approval-queue/per-event-RSVP queries (the spec's
events/rsvps/event_sync tables) would need full scans. kv keys (all `.`-separated, community
scoped by the host): `cal.seq`, `cal.index`, `cal.ev.<id>`, `cal.rsvp.<id>`,
`cal.sync.<id>.<target>`, `cal.pendq`, `cal.idem.<hash>`. kv has no list/CAS: index and
RSVP updates are read-modify-write and a concurrent writer in one community can lose an update
(accepted at chat scale, not hidden). Cap 400 events (oldest terminal event is purged to make
room) and 500 RSVPs/event. Revisit when `db.query` gains filters.

## Sync

`create/update/cancel/delete` on a scheduled event queues an op per enabled target
(`cal.sync.*`: remote id, status, attempts, last error code, last payload hash) and flushes it
inline. Idempotency key = `sha256(community:event public_id:target:op:version)`; an applied key is
a no-op, unchanged payloads (hash) skip the call, creates are guarded by the mapping so a retry
never double-creates. 2 inline attempts on transient errors (timeout/pool/target unavailable);
remaining work stays queued and a bounded reconcile pass runs after every event touch (no
periodic host hook exists yet). Remote 404 on update recreates once; on cancel/delete it is
satisfied. `not_granted`, `forbidden`, `invalid_args`, a create result without a remote id
(`remote_id_missing`, never blind-retried) and exhausted retries park as `status=error`.
Discord payload clamps: name<=100, description<=1000, location 1-100 (default `Online`),
end defaults to start+1h.

| Target | Tools consumed (target app) | Status |
|---|---|---|
| discord | `discord.scheduled_event.{create,update,cancel,delete}` (`waddles.core.service.discord`) | requested in manifest |
| twitch | `twitch.schedule.{create,update,delete}` (`waddles.core.service.twitch`) | adapter + contract fixtures; NOT yet requested (tools not built) |
| wix | `wix.event.{create,update,cancel}` (`waddles.core.service.wix`) | adapter + contract fixtures; NOT yet requested |

Enabling twitch/wix requires a manifest version bump adding their `requested_tools` once the
service tools exist (least privilege: no grant is requested for a tool that does not exist).
Contract fixtures: `contracts/<tool>.schema.json`; `tests/contract.rs` validates every adapter
payload against them and cross-checks the Discord ones with `bundles/rust/discord/bundle.yaml`.

## Admin actions (hub-api bundle-admin gateway)

`list`/`get`/`create`/`update`/`cancel`/`approve`/`reject`/`delete`/`backfill` (queue upcoming events for a hub-enabled target) arrive as a
`platform=hub`, `event_type=bundle.admin` event with payload `{"action", "args"}` (already
authorized by OIDC scope + tenant + community in hub-api); the JSON result is returned in
`TransportResult.detail`. Responses are explicit DTOs (`app::event_json`), never raw records.
No generic bundle-admin API exists in hub-api yet (spec Q2) and the manifest has no `hub`
consumes rule yet, so this path is code-complete and tested but not reachable until the gateway
lands.

## Config (`config_json`, hub-resolved; the ONLY source of security toggles)

`approval_required`, `default_tz`, `default_duration_minutes`, `default_location`,
`sync_discord`, `sync_twitch`, `sync_wix` (see `config_schema`). Sync defaults OFF. Chat can never change `approval_required` or `sync_*`; they are set via the hub config / bundle-admin gateway. With `approval_required`, a non-approver's edit of an approved event returns it to `pending` (remote copy unchanged, no sync) until re-approved.

## Security / privacy

Community scoping is the host's; the bundle never reads a community/tenant id from a payload.
RSVPs and ownership are keyed by user UUID only (a non-UUID actor is refused loudly). Echoed
user text is neutralised against `@everyone`/`@here`. Logs carry op/outcome/ids/counts only --
never titles, descriptions or handles (asserted in tests). No secrets exist in the guest; the
Discord guild/credential is resolved host-side from the community's own connection.

## Build / test

```bash
cargo test                                         # 32 tests (flow + contract)
cargo component build --release --target wasm32-wasip2 --locked
wasm-tools component wit target/wasm32-wasip2/release/waddle_bundle_calendar_rust.wasm
```

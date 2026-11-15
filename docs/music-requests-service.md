# Music requests service (`waddles.core.service.music-requests`)

ONE song-request queue per community, owned by a `kind: service` bundle (Rust, world `stage-tools`).
`waddles.core.example.music` (Python app) is the user-facing core: `!sr`/`!songrequest`/`!music`/`!queue`/`!songqueue`.
`waddles.core.example.dj` (Rust app) is an OPTIONAL layer on top: it never owns a queue or the request commands;
it reads/adds through this service's tools. Music works fully with DJ absent or off.

## Storage (db-v2 tables declared in the manifest `data.schema`; community-scoped by the host)

| table | columns | indexes |
|---|---|---|
| `requests` | `title text(200)`, `artist text(200)`, `source_url text(500)` (https only, may be ""), `duration_ms int4`, `requester user_ref` (nullable), `kind int4` (1 viewer, 2 dj, 3 imported), `status int4` (1 queued, 2 playing), `queued_at timestamptz`, `started_at timestamptz` (nullable) | `[status, queued_at]` |
| `history` | `title text(200)`, `artist text(200)`, `source_url text(500)`, `duration_ms int4`, `played_at timestamptz`, `requester user_ref` (nullable, NEVER exposed), `kind int4` | `[played_at]` |
| `settings` | `skey text(32)` (unique), `ival int4` | unique `[skey]` |
| `artist_plays` | `artist_key text(200)` (unique, lower-cased artist), `artist text(200)`, `plays int4` | unique `[artist_key]`, `[plays]` |
| `song_plays` | `song_key text(64)` (unique, first 32 hex chars of sha256 over lower(title), a pipe, lower(artist)), `title text(200)`, `artist text(200)`, `plays int4` | unique `[song_key]`, `[plays]` |

`requests` and `history` also carry `provider text(16)` (host-derived, see DMCA-safe mode) and `requests` a
`seq int4` (the public item `id`, allocated from `settings.seq`; row ids stay internal). `requester` is a `user_ref`
declared `on_erasure: anonymize` (nullable) in both tables. Play counters have no requester columns and are
incremented in the SAME transaction that writes `history`.

`settings` keys: `ai_dj_on` (0/1, default 0), `max_per_user` (default 3), `max_queue` (default 100), `imported` (0/1 one-shot), `dmca_safe` (0/1, default 0), `seq` (id counter). `max_per_user` counts the user's QUEUED requests (not the one playing).
No slugs as keys; requester is the host-verified community user UUID only. kv keys never contain `:`.
hub-api reads these physical tables from the replica (`bundle_data_v2.derive_v2_table_identity(app_id, logical, provider="builtin")`).
No platform alembic table is needed (bundle tables are provisioned by the install lifecycle); 0117 stays unused.

## Scopes (host-enforced `required_scope`, per-action; moderator/admin are scope BUNDLES)

| scope | tools | role bundles |
|---|---|---|
| `music:history.read` | `history.list` | member, vip, moderator, admin, owner |
| `music:requests.read` | `requests.list`, `requests.advance_due`, `config.get`, `dj.get`, `page.link` | member, vip, moderator, admin, owner |
| `music:requests.write` | `requests.add`, `requests.cancel_own` | member+ |
| `music:requests.manage` | `requests.remove`, `requests.advance`, `requests.import`, `config.set` | moderator, admin, owner |
| `music:dj.manage` | `dj.set` | moderator, admin, owner |
| `music:stats.read` | `music.stats.top` | member, vip, moderator, admin, owner |
| `music:settings.write` | `settings.set` | moderator, admin, owner |

Role -> scopes lives in `core/bundle_tool_call::scopes_for_role` (+ allow-list in `hub_api/services/bundle_manifest_v2.py`).
`requests.add` with `kind: "dj"` additionally requires `music:requests.manage` in `__ctx.scopes` (requester stored NULL).
The actor is read ONLY from the host-injected `__ctx` (never from args).

## Tools (JSON in/out). Item = `{id:int, title, artist, source_url, duration_ms, kind:"viewer"|"dj"|"imported", queued_at, dmca_safe_item:bool}` — never a requester. `history.list` items also carry `dmca_safe_item`.

| tool | input | output |
|---|---|---|
| `requests.list` | `{limit?: 1..100 (50)}` | `{now_playing: Item|null, queue: [Item], total_queued, ai_dj_on, dmca_safe}` |
| `requests.add` | `{title, artist?, source_url?, duration_ms?, kind?: "viewer"|"dj"}` (write tool: idempotency key) | `{item: Item, position}`; errors: `queue_full`, `per_user_limit`, `duplicate`, `invalid_url` |
| `requests.cancel_own` | `{id}` | `{removed:bool}` (only rows whose requester == actor) |
| `requests.remove` | `{id}` | `{removed:bool}` |
| `requests.advance` | `{}` | `{finished: Item|null, now_playing: Item|null}`; finishing writes a `history` row |
| `requests.advance_due` | `{}` | same shape; advances ONLY if now_playing's `duration_ms` has elapsed or nothing is playing and the queue is non-empty (no privilege needed, idempotent) |
| `requests.import` | `{items:[{title,artist,source_url,duration_ms}]<=100}` | `{imported:int}`; one-shot per community (`settings.imported`), only when queue+history empty, requester NULL, kind=3 |
| `history.list` | `{limit?: 1..50 (5)}` | `{items:[{title,artist,source_url,duration_ms,played_at}]}` newest first |
| `config.get` / `config.set` | `{}` / `{max_per_user?: 1..50, max_queue?: 1..500}` | `{max_per_user,max_queue,ai_dj_on,dmca_safe}` |
| `dj.get` / `dj.set` | `{}` / `{enabled:bool}` | `{enabled}` |
| `music.stats.top` | `{kind:"artist" or "song", limit?: 1..25 (10)}` | `{items:[{title?,artist,plays}]}` (`title` only for songs), most played first |
| `settings.set` | `{dmca_safe:bool}` (write tool) | `{dmca_safe, purged:int}`; enabling purges QUEUED non-safe rows (never the playing one) |
| `page.link` | `{}` | `{url}` = `{PUBLIC_WEBUI_URL}/community/{community_id}/music`; error `link_not_configured` when the platform base URL is not configured |

## Public API (hub-api) and UI

`GET /api/v1/communities/{id}/music` (community members; replica read; flag `waddles.music.queue-page` default OFF):
`{now_playing, queue:[{title,artist,source_url,duration_ms,kind}], history:[last 5 {title,artist,source_url,duration_ms,played_at}], ai_dj:{enabled}}`
where `ai_dj.enabled` = DJ bundle (`waddles.core.example.dj`) active for the community AND `settings.ai_dj_on = 1`.
NO requester identity anywhere. Webui page `/community/:communityId/music`.

## Platform context

Both stages add `web_base_url` (from env `PUBLIC_WEBUI_URL`, omitted when unset) to the bundle context JSON.

## DMCA-safe mode

`settings.dmca_safe = 1` makes `requests.add` (viewer AND `kind: "dj"`) and `requests.import` refuse any non-safe source
with `not_dmca_safe: ...`. The `provider` stored on every row is DERIVED by the service from the `source_url` host
against the `SAFE_SOURCES` config table in `src/logic.rs` -- no tool accepts a provider/tag argument (unknown args are
rejected; title/artist text is never read for provider). Anything not on the table, or with an empty URL, is `other`
(not safe).

| provider | identified by |
|---|---|
| `ncs` (NoCopyrightSounds) | host `ncs.io`, host `ncs.lnk.to`, or `soundcloud.com` with path prefix `/nocopyrightsounds`. The NCS YouTube channel cannot be told apart from a URL, so YouTube links are never safe. |
| `rfm` (royalty-free music) | hosts `pixabay.com` (path `/music`), `pretzel.rocks`, `streambeats.com`, `uppbeat.io`, `incompetech.com`, `bensound.com` -- a clearly-commented, review-gated config table. |

Subdomains match (`www.ncs.io`); look-alikes (`ncs.io.evil.com`, `https://ncs.io@evil.com`) do not (userinfo URLs are rejected).
Enabling safe mode purges queued `other` rows and reports `purged`; the now-playing row is left alone.

## Platform context (`web_base_url`)

The stage adds `web_base_url` (env `PUBLIC_WEBUI_URL`; trimmed; `https://`, or `http://` for dev; omitted otherwise) to the
`context` host-call JSON (`svc_process` and `svc_action`). The WIT `bundle-context` record has no field for it, so the
executor surfaces it to guests as the host-reserved `web_base_url` key of `config-json` (a community-set key of that name is
dropped). `page.link` returns `{base}/community/{numeric community_id}/music`, or `link_not_configured` when absent.

## Notes

- Write tools require the host idempotency key; per-user/queue caps and the DMCA-safe setting are guarded inside the same
  `transact` as the insert (a race fails the batch and the tool re-reads). Duplicate detection (same title+artist, or same
  non-empty URL, among queued + playing) is a pre-check, not a unique index.
- `history` is bounded to the newest 500 rows (oldest pruned best-effort after each advance) so the community row quota is not exhausted.
- `requests.import` needs `music:requests.manage` (a moderator-level actor must trigger the legacy-queue migration), is refused with `queue_full` when the batch exceeds `max_queue` or 100 (before any write), and claims the one-shot marker first (guards: queue and history empty), then inserts in chunks of 16; a failure after the claim returns `import_partial`.

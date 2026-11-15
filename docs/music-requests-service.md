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

`settings` keys: `ai_dj_on` (0/1, default 0), `max_per_user` (default 3), `max_queue` (default 100), `imported` (0/1 one-shot).
No slugs as keys; requester is the host-verified community user UUID only. kv keys never contain `:`.
hub-api reads these physical tables from the replica (`bundle_data_v2.derive_v2_table_identity(app_id, logical, provider="builtin")`).
No platform alembic table is needed (bundle tables are provisioned by the install lifecycle); 0117 stays unused.

## Scopes (host-enforced `required_scope`, per-action; moderator/admin are scope BUNDLES)

| scope | tools | role bundles |
|---|---|---|
| `music:requests.read` | `requests.list`, `requests.advance_due`, `history.list`, `config.get`, `dj.get`, `page.link` | member, vip, moderator, admin, owner |
| `music:requests.write` | `requests.add`, `requests.cancel_own`, `requests.import` | member+ |
| `music:requests.manage` | `requests.remove`, `requests.advance`, `config.set` | moderator, admin, owner |
| `music:dj.manage` | `dj.set` | moderator, admin, owner |

Role -> scopes lives in `core/bundle_tool_call::scopes_for_role` (+ allow-list in `hub_api/services/bundle_manifest_v2.py`).
`requests.add` with `kind: "dj"` additionally requires `music:requests.manage` in `__ctx.scopes` (requester stored NULL).
The actor is read ONLY from the host-injected `__ctx` (never from args).

## Tools (JSON in/out). Item = `{id:int, title, artist, source_url, duration_ms, kind:"viewer"|"dj"|"imported", queued_at}` — never a requester.

| tool | input | output |
|---|---|---|
| `requests.list` | `{limit?: 1..100 (50)}` | `{now_playing: Item|null, queue: [Item], total_queued, ai_dj_on}` |
| `requests.add` | `{title, artist?, source_url?, duration_ms?, kind?: "viewer"|"dj"}` (write tool: idempotency key) | `{item: Item, position}`; errors: `queue_full`, `per_user_limit`, `duplicate`, `invalid_url` |
| `requests.cancel_own` | `{id}` | `{removed:bool}` (only rows whose requester == actor) |
| `requests.remove` | `{id}` | `{removed:bool}` |
| `requests.advance` | `{}` | `{finished: Item|null, now_playing: Item|null}`; finishing writes a `history` row |
| `requests.advance_due` | `{}` | same shape; advances ONLY if now_playing's `duration_ms` has elapsed or nothing is playing and the queue is non-empty (no privilege needed, idempotent) |
| `requests.import` | `{items:[{title,artist,source_url,duration_ms}]<=100}` | `{imported:int}`; one-shot per community (`settings.imported`), only when queue+history empty, requester NULL, kind=3 |
| `history.list` | `{limit?: 1..50 (5)}` | `{items:[{title,artist,source_url,duration_ms,played_at}]}` newest first |
| `config.get` / `config.set` | `{}` / `{max_per_user?, max_queue?}` | `{max_per_user,max_queue,ai_dj_on}` |
| `dj.get` / `dj.set` | `{}` / `{enabled:bool}` | `{enabled}` |
| `page.link` | `{}` | `{url}` = `{PUBLIC_WEBUI_URL}/community/{community_id}/music`; error `link_not_configured` when the platform base URL is not configured |

## Public API (hub-api) and UI

`GET /api/v1/communities/{id}/music` (community members; replica read; flag `waddles.music.queue-page` default OFF):
`{now_playing, queue:[{title,artist,source_url,duration_ms,kind}], history:[last 5 {title,artist,source_url,duration_ms,played_at}], ai_dj:{enabled}}`
where `ai_dj.enabled` = DJ bundle (`waddles.core.example.dj`) active for the community AND `settings.ai_dj_on = 1`.
NO requester identity anywhere. Webui page `/community/:communityId/music`.

## Platform context

Both stages add `web_base_url` (from env `PUBLIC_WEBUI_URL`, omitted when unset) to the bundle context JSON.

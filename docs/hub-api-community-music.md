# hub-api: `GET /api/v1/communities/{community_id}/music`

Member-facing read of a community's music queue page. Reads the `waddles.core.service.music-requests`
bundle's db-v2 tables (`requests`, `history`, `settings`, `artist_plays`, `song_plays`) from the
**read replica** (primary only when no replica is configured). Code:
`hub_api/blueprints/v1/community_music.py`, `hub_api/services/community_music_service.py`.

| Aspect | Behaviour |
|---|---|
| Auth | Bearer JWT; tenant re-validated, caller must be an active community member (403 otherwise, incl. other-tenant communities) |
| Flag | `waddles.music.queue-page`, default OFF -> 404 |
| Data scope | Every query is filtered by bound `tenant_id` AND `community_id`; physical names only from `derive_v2_table_identity` |
| Missing tables | Bundle not installed -> empty lists, `dmca_safe=false`, `ai_dj.enabled=false` (not a 500) |
| Privacy | Requester columns are never selected; no requester/user field exists in the DTOs |

Response (`MusicPageDTO`):

```json
{
  "now_playing": {"title": "", "artist": "", "source_url": "", "duration_ms": 0, "kind": "viewer|dj|imported"},
  "queue": [ "<=50 items shaped like now_playing" ],
  "history": [ {"title": "", "artist": "", "source_url": "", "duration_ms": 0, "played_at": "ISO-8601"} ],
  "ai_dj": {"enabled": false},
  "dmca_safe": false,
  "top_artists": [ {"artist": "", "plays": 0} ],
  "top_songs": [ {"title": "", "artist": "", "plays": 0} ]
}
```

`history` = last 5, `top_*` = top 5 by plays. `ai_dj.enabled` = DJ bundle `waddles.core.example.dj`
active for the community (`app_active_versions`, community or tenant-wide) AND `settings.ai_dj_on = 1`.
`dmca_safe` = `settings.dmca_safe = 1`.

Known blocker: `derive_v2_table_identity` rejects `-`, so the contract app id
`waddles.core.service.music-requests` cannot get physical tables until the id (or the derive
sanitiser) changes; `test_contract_app_id_derives` is a strict xfail that flips when fixed.

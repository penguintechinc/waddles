# music (Python)

`!sr`/`!songrequest` + `!music`/`!queue` -- a per-community song-request QUEUE. Community-scoped
fun/utility content, first-party (`provider: builtin`, `app_id: waddles.core.example.music`),
**inspired by** superpenguintv (Psychoboy)'s
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) song-request feature -- not a
literal port. No original source code or text is reused; the command grammar, queue data shape,
and all logic are written fresh for this bundle. See `bundle.yaml`'s `author`/`notice` fields for
the credit, and contrast `bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and
carries the full verbatim MIT notice because it reuses original code.

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`) for the
`!music`/`!queue` verb shapes. `!sr`/`!songrequest` are pure free-text "add" commands and never go
through the grammar -- same documented extension as `shoutout`'s own bare-positional-target case
(the request text may start with any token, including one that looks like a VERB).

**Chat front end only (1.1.0+).** The queue, now-playing state, history and stats live in the shared
service `waddles.core.service.music-requests` (see `docs/music-requests-service.md`); this bundle
turns chat commands into its tool calls (process stage) and relays the reply + publishes the overlay
surfaces (action stage). It works fully with the optional AI DJ absent and never references it.
Privilege is decided by the service's host-verified scopes, never chat badges: a typed `forbidden`
becomes "only moderators can do that".

## Commands

| Command | Tool(s) | Behavior |
|---|---|---|
| `!sr <title - artist [https-link]>` / `!songrequest` | `requests.add` | Adds a request (idempotent per chat message). Service errors (queue full, per-user limit, duplicate, bad URL, DMCA-safe refusal) get friendly replies. |
| `!music` / `!queue` / `!music list` | `requests.advance_due`, `requests.list` | Now playing + next 5. |
| `!music now` | same | Now playing only. |
| `!music history` | `history.list` | Last 5 played. |
| `!music top [artists\|songs]` | `music.stats.top` | Top 5 (default songs). |
| `!music remove <id>` | `requests.remove`, falls back to `requests.cancel_own` on `forbidden` | Moderators remove any; viewers their own. |
| `!music next` / `!music skip` | `requests.advance` | Moderator (service-enforced). |
| `!music set max-per-user <n>` | `config.set` | Moderator, 1-10. |
| `!music set dmca-safe on\|off`, `!music dmca` | `settings.set`, `config.get` | Moderator set; anyone reads status. |
| `!songqueue` / `!sq` | `page.link` | Link `{base}/community/{community_id}/music`; `song queue page is not configured` when unset. Behind PostHog flag `waddles.music.queue-page` (default OFF; silent when off). |

Replies never include who requested a song. Logs carry command/tool/code/counts only.

## Migration from 1.0.x

On the first command per community, a legacy kv queue (`music.queue`) is handed to
`requests.import` (title/artist/url only, requester ids dropped), then `music.queue`, `music.seq` and
`music.config.max-per-user` are deleted and `music.migrated` is set. A transient failure retries on
the next command; a service refusal (already imported / queue not empty) clears the stale legacy data.

## Overlay

`now_playing` and `queue` surfaces publish from the service's `requests.list` result after each
command (text only: title, artist, duration; no requester, no URLs).

## Permissions

`storage.kv` (migration only), `overlay.media`, `chat.send:discord|twitch`, and `tool.call:waddles.core.service.music-requests:<tool>`
for each tool above (`requested_tools` in `hub-manifest.yaml`). Built against WIT world `stage-next-tools`
(`bundles/core-bundles.yaml`). Tests: `python3 -m pytest bundles/python/music/tests`.

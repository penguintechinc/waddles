# DJ app bundle (`waddles.core.example.dj`)

Rust WASM **app** bundle (`stage-next-tools` world). An **optional AI DJ layer** over the shared
request queue service `waddles.core.service.music-requests` (contract:
`docs/music-requests-service.md`). The music bundle + that service are the core (queue, now
playing, history, `!sr`/`!queue`); music works fully with the DJ absent or off. The DJ owns **no
request queue and no request commands**: it keeps only its policy (bans, allow/deny lists, AI
judge), playlists, config and audit, and *invokes* `music-{youtube,spotify,soundcloud}`, `ai` and the
queue service through gated `tool.invoke` calls (app to app is forbidden). Gated by flag
`waddles.command-dj` (default OFF).

## Architecture

```
!dj ai on|off ---> dj.set  (music:dj.manage, host-enforced)   the service is the ONE stored switch
!dj fill|next ---> dj.get -> requests.advance_due -> requests.list -> plan -> requests.add kind=dj
                        |                                   (picks: AI hint -> music.<p>.search -> policy)
                        +-- off (or not granted) => NO picks, and the reply says so
```

Planning (every invocation re-reads the queue first; there is still **no periodic hook**, see host gaps):

1. `dj.get` / `requests.list.ai_dj_on` must both say on.
2. `viewer` = queued items that are not `kind: dj`. The DJ acts only while `viewer < dj_min_queue` (default 3).
3. Interleave: the DJ may hold `max(1, viewer / dj_requests_per_pick)` queued picks (default 2 viewer
   requests per pick; one pick is always allowed so an idle queue is never silent). Picks are only
   ever appended, so viewer requests play first.
4. Picks come from the active playlist (`!dj mode playlist`) then the AI (hourly cap, batch), resolved via a real
   `music.<p>.search`, run through the policy, added with `requests.add {kind:"dj"}` (idempotency key
   `dj.add.<message_id>.<n>`), and announced in chat. `queue_full` stops the run; `duplicate`/`invalid_url` skip.

## Commands (only `!dj`; moderator / broadcaster only)

`!sr`, `!songrequest`, `!music`, `!queue`, `!songqueue` are NOT handled here (removed in 1.1.0).

| Command | Effect |
|---|---|
| `!dj ai on\|off\|status` | flips / reads the service's shared switch (`dj.set` / `dj.get`) |
| `!dj fill` / `!dj next` | plan + add picks now (see Architecture) |
| `!dj ai genre <g>\|instructions <text>` | AI taste; instructions compile once into a bounded policy |
| `!dj policy show\|confirm\|discard\|clear` | confirm / drop the compiled policy |
| `!dj set <explicit\|dmca_safe\|max_duration_s\|replay_cooldown_s\|dj_min_queue\|dj_requests_per_pick\|providers\|ai_hourly_cap\|ai_batch> <v>` | settings |
| `!dj list <allow-channel\|allow-artist\|deny-artist\|deny-term\|deny-track> add\|remove <v>` | allow/deny lists |
| `!dj ban <provider/id>` / `!dj unban <provider/id>` | DJ ban list |
| `!dj playlist list\|create\|delete\|show\|use\|add\|remove\|move\|import <name> ...`, `!dj mode requests\|playlist\|ai` | playlists + pick source |
| `!dj audit [n]`, `!dj config` | audit / settings |

Removed settings: `requests`, `max_per_user`, `cooldown_s`, `max_queue`, `approval`, `auto_remove`, `cost`
(request-era; the service owns limits). The economy cost-per-request was dropped with `!sr` (it only
served requests) and the `economy` grant is gone. The DJ no longer plays media (no `player.play`
grant) and no longer publishes overlay surfaces: music is the single source for now-playing/queue.

## Policy (order of evaluation)

1. deterministic (no AI): ban list, provider enabled, allowlists (`dmca_safe` = allowlist-only,
   fail-closed when empty), deny artist/title/track, duration, explicit, replay cooldown, duplicate.
   Duplicates are checked against the shared queue (title + artist), replay cooldown against the DJ's own recent picks.
2. optional AI judge (`ai.classify`, fixed labels `allowed|violates`, track metadata only) for
   fuzzy rules. A violation is rejected; low confidence, an unavailable service or an out-of-set
   label **holds** (the pick is simply not made), never auto-adds. Every decision is audited
   (`{req_id, rule, decided_by, outcome}`; PII-free) in `dj.audit`.
3. Plain-language instructions are compiled **once** by `ai.complete` (JSON) into a strict,
   bounded `Rules` struct (`deny_unknown_fields`), shown to the admin, and only enforced after
   `!dj policy confirm`.

Prompt-injection guard: viewer text never reaches `ai.complete`; titles reach `ai.classify`
only as the data field of a fixed-label classification; model output is schema-validated.
The AI DJ resolves every suggestion through a real `music.<p>.search` (title must match) and
pushes it through the same policy; unresolved tracks are never added. Hourly cap enforced.

## State (kv, community-scoped, keys use `.`)

`dj.config`, `dj.state` (bans, recent DJ picks for replay cooldown, playlist cursor, AI hourly window),
`dj.audit`, `dj.pl_index`, `dj.pl.<name>`, `dj.migrated`. No requester id is stored or emitted anywhere.
**Migration (1.0.x to 1.1.0):** on the first `!dj` invocation per community, if `dj.migrated` is unset, the
old `dj.state.queue` (queued items only; held ones are dropped) is sent once via `requests.import` (an
"already imported" refusal is tolerated), then `queue/now/rejected/next_id` and `dj.public_state` are removed
and `dj.migrated` is set. If the DJ has no grant or the service is down the old queue is kept and retried.

## Consumed tool contract

Queue service (`waddles.core.service.music-requests`, scopes enforced by the host from the invoking actor; the DJ never
sends an actor or community): `requests.list {limit}` -> `{now_playing, queue, total_queued, ai_dj_on}`,
`requests.advance_due {}`, `requests.add {title, artist, source_url?, duration_ms, kind:"dj"}` -> `{item, position}`
(errors `queue_full`, `duplicate`, `invalid_url`; needs `music:requests.manage`), `requests.import {items<=100}`,
`dj.get {}` / `dj.set {enabled}` -> `{enabled}`. Items never carry a requester.
Without the grant every call is `NotGranted` and the DJ replies that it is not allowed to use the music queue.

Providers (derived from `bundles/contracts/*.v1.json`; tests `include_str!` them, so drift fails the build):
`music.<p>.search {query,limit<=25}`, `music.<p>.resolve {url}`, `music.<p>.playlist.get {playlist_id,limit,page_token}`
(youtube, spotify, soundcloud; `explicit` null = unknown, which fails closed when explicit songs are disallowed).
`ai.complete {system<=1200,prompt<=3500,max_tokens,temperature}` and `ai.classify {text,labels,instructions}` per `ai-tools.v1`.
DMCA allowlists match `artist` (or the optional extra provider field `channel_id` when present).

## Build / test

```
cargo component build --release --target wasm32-wasip2 --locked
cargo test --locked && cargo clippy --locked --all-targets -- -D warnings && cargo deny check
```

## Needs from host seams (S0)

- Periodic / stream-lifecycle hook so the AI DJ can top up the queue without a chat command
  (`streaming-lifecycle` hooks return `unsupported-stage`). Until then the DJ "reacts on change" only
  by re-listing on every `!dj` invocation.
- Generic admin-action path (webui to app bundle) for settings now done by `!dj` commands.
- Live-stream state to default `dmca_safe` ON while streaming (today an explicit setting).

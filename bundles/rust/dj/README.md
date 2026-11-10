# DJ app bundle (`waddles.core.example.dj`)

Rust WASM **app** bundle (`stage-next-tools` world). Community playlists, viewer song requests,
mod queue controls, a deterministic song policy and an optional AI DJ. It owns no tools: it
*invokes* `music-{youtube,spotify,soundcloud}`, `ai` and (optionally) `economy` service bundles
through gated `tool.invoke` calls (app to app is forbidden). Gated by flag `waddles.command-dj`
(default OFF). Replaces hub_api music routes + `bundles/python/music` (not deleted in this PR).

## Commands (aliases `!sr`, `!songrequest`, `!music`, `!queue` kept)

| Who | Command |
|---|---|
| viewer | `!sr <query\|link>`, `!music` (queue), `!music now`, `!music remove <id>` (own) |
| mod / broadcaster | `!music skip\|next`, `approve <id>`, `ban <id>`, `restore <id>`, `move <id> <pos>`, `clear` |
| mod | `!dj set <requests\|explicit\|dmca_safe\|approval\|auto_remove\|max_per_user\|cooldown_s\|max_duration_s\|max_queue\|replay_cooldown_s\|cost\|providers\|ai_hourly_cap\|ai_batch> <v>` |
| mod | `!dj list <allow-channel\|allow-artist\|deny-artist\|deny-term\|deny-track> add\|remove <v>`, `!dj unban <provider/id>` |
| mod | `!dj playlist list\|create\|delete\|show\|use\|add\|remove\|move\|import <name> ...`, `!dj mode requests\|playlist\|ai` |
| mod | `!dj ai on\|off\|genre <g>\|instructions <plain text>`, `!dj policy show\|confirm\|discard\|clear`, `!dj fill`, `!dj audit [n]`, `!dj config` |

Moderator = the platform's `is_mod` / `is_broadcaster` flag; a missing flag is NOT a moderator.

## Policy (order of evaluation)

1. deterministic (no AI): ban list, provider enabled, allowlists (`dmca_safe` = allowlist-only,
   fail-closed when empty), deny artist/title/track, duration, explicit, replay cooldown, duplicate.
   Per user: cooldown (kv TTL), max pending, queue cap. Optional approval gate.
2. optional AI judge (`ai.classify`, fixed labels `allowed|violates`, track metadata only) for
   fuzzy rules. Low confidence, unavailable service or an out-of-set label **holds for a mod**,
   never auto-plays. `auto_remove` on: violation is removed with the reason in chat; off: held.
   `!music restore` is a mod override that skips the AI judge. Every decision is audited
   (`{req_id, rule, decided_by, outcome}`; PII-free) in `dj.audit`.
3. Plain-language instructions are compiled **once** by `ai.complete` (JSON) into a strict,
   bounded `Rules` struct (`deny_unknown_fields`), shown to the admin, and only enforced after
   `!dj policy confirm`.

Prompt-injection guard: viewer text never reaches `ai.complete`; titles reach `ai.classify`
only as the data field of a fixed-label classification; model output is schema-validated.
The AI DJ resolves every suggestion through a real `music.<p>.search` (title must match) and
pushes it through the same policy; unresolved tracks are never played. Hourly cap enforced.

## State (kv, community-scoped, keys use `.`)

`dj.config`, `dj.state` (queue, now, history, bans, rejected ring), `dj.audit`, `dj.pl_index`,
`dj.pl.<name>`, `dj.cd.<user_uuid>` (cooldown), `dj.public_state` (overlay snapshot: now-playing +
next 25 queued, no requester ids). Requesters are stored as community `user_uuid` only.
kv has no compare-and-swap: concurrent writers in one community are last-write-wins.

## Consumed tool contract (derived from `bundles/contracts/*.v1.json`; tests `include_str!` them, so drift fails the build)

`music.<p>.search {query,limit<=25}`, `music.<p>.resolve {url}`, `music.<p>.playlist.get {playlist_id,limit,page_token}`
(providers: youtube, spotify, soundcloud; track shape per `music-tool-contract.v1`, `explicit` null = unknown, which
fails closed when explicit songs are disallowed). There is no generic playback tool: YouTube/SoundCloud play through
the Music Station; Spotify hands off with `music.spotify.player.play {uris}` (moderator scope, idempotency key
`dj.play.<item_id>`, failure reported in chat). `ai.complete {system<=1200,prompt<=3500,max_tokens,temperature}` and
`ai.classify {text,labels,instructions}` -> `{label,confidence,reason}` per `ai-tools.v1`. Optional cost/refund:
`economy.adjust_balance {user,delta}` (delta<0 debits; idempotency keys `dj.sr.<message_id>` / `dj.refund.<item_id>`).
Targets: `waddles.core.service.music-<p>`, `waddles.core.service.ai`, `waddles.core.service.economy`.
DMCA allowlists match `artist` (or the optional extra provider field `channel_id` when present).

## Build / test

```
cargo component build --release --target wasm32-wasip2 --locked
cargo test --locked && cargo clippy --locked --all-targets -- -D warnings && cargo deny check
```

## Needs from host seams (S0)

- A way for svc-presentation's Music Station to read bundle state (`dj.public_state`); the
  `music` overlay surface only accepts `clear` today and app bundles cannot export tools.
- Periodic / stream-lifecycle hook so the AI DJ can fill gaps without a chat command
  (`streaming-lifecycle` hooks currently return `unsupported-stage`).
- Generic admin-action path (webui to app bundle) for settings now done by `!dj` commands.
- Live-stream state to default `dmca_safe` ON while streaming (today an explicit setting).

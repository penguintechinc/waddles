# live-now (app bundle, Rust)

Community "who's live" across platforms. An APP bundle on `world stage-next-tools`: it **consumes**
each platform service bundle's `<platform>.live.channels` tool (it exports none; app->app is
forbidden, service bundles own the tool definitions).

| Feature | How |
|---|---|
| `!live` / `!live <platform>` | process stage -> tool calls -> reply relayed to the origin channel. Flag `live-now.command` (default OFF) |
| Go-live announcements | on `stream.online`/`stream.offline`/`live.refresh` (and alongside `!live`): templated message to `announce_targets`. Flag `live-now.announce` (default OFF) |
| Dedupe | atomic kv claim per stream **session** (`platform + channel_ref + started_at`), 12 h TTL |
| Cooldown | atomic kv claim per channel, `cooldown_seconds` (default 1800) |
| Stale guard | streams older than `max_age_seconds` (default 900) are never announced, so enabling the feature cannot flood chat |
| Snapshot | latest `{live, platform, channel, since, viewer_count, streams[], errors[]}` in kv key `snapshot` (30 s freshness cache; same shape as the old `live_status` community summary) |

## Contract

`bundles/contracts/live-channels-contract.v1.json` (generate a platform manifest entry with
`scripts/gen-live-manifest-tools.py <platform>`; `--check <manifest>` fails on drift). Failures are
loud: the provider errors with `invalid-args` whose message starts with `platform_unavailable`,
`rate_limited` or `not_connected`. One failing platform is reported in the reply/snapshot
(`errors[]`); all failing is an error. No empty-success fallback.

## Config (resolved 3-tier JSON; unknown keys rejected)

`platforms` (default twitch,youtube,discord), `include_linked` (also call `scope: "linked"`),
`announce_targets: [{platform, channel_id}]` (discord/twitch/youtube, max 10), `template`
(`{name} {platform} {title} {category} {viewers}`), `cooldown_seconds` (60-86400),
`max_age_seconds` (60-3600).

## Security

Titles/names from platforms are stripped of `@ < > \`` , control and bidi characters before they reach chat.
No connection secrets in the guest (platform bundles resolve them host-side). Logs carry platform /
counts / error codes only. The invoking actor is dropped from the outbound event.

## Offline / degraded behavior

Platform bundle missing -> `not_installed`; shown as `[unavailable: ...]` in `!live`. No scheduler exists
yet: refresh happens on `stream.online/offline`, `live.refresh` and `!live` (see PR "Needs from S0").

## Parity with the Python it replaces

`hub_api blueprints/v1/live_status.py` + `services/live_status_service.py` -> snapshot shape and live-first,
viewers-desc ordering (offline connected channels included). `live_activity.py` (bot interaction event
feed + SSE) is **not** reproduced: it is a chat-interaction log, not live status; the SSE push is the S0
overlay seam (catalog Q3).

## Build / test

```
cargo test && cargo clippy --all-targets -- -D warnings && cargo deny check
cargo component build --release --target wasm32-wasip2 --locked
```

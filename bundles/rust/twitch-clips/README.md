# Twitch clips (app bundle)

`waddles.core.example.twitch-clips` - Rust APP bundle on `world stage-next-tools`. It owns no
Twitch credentials and no Helix code: clips are created, polled and listed only through the Twitch
platform service bundle's tools (`waddles.core.service.twitch`: `twitch.clip.{create,get,list}`),
whose broadcaster is pinned host-side. App bundles invoke, service bundles own tools.

## Chat commands (Twitch)

| Command | Behaviour |
|---|---|
| `!clip [title]` | Create a clip (only while the channel is live - Twitch refuses otherwise and the viewer gets a notice). Gated by `who_can_clip`, per-user and per-channel cooldowns. Polls `twitch.clip.get` with backoff (1s,1.5s,2s,...) within `poll_budget_ms`; posts the URL when ready, else "still processing" and finishes later (see Pending). |
| `!clips [n]` | Recent clips (1-10) from `twitch.clip.list`, minus clips an admin hid. Gated by `who_can_list`, per-channel list cooldown. |

Titles are sanitised (control chars stripped, 100 chars, host mention tokens `{user:..}` refused).
There is no shared Rust verb parser in `waddle-sdk-rs` yet, so a tiny exact-match parser lives in
`logic.rs::parse_command` (swap to the SDK parser when it lands).

## Config (community scope, `config_schema` in the manifest)

`who_can_clip` (default `mods`), `who_can_list` (`everyone`), `user_cooldown_secs` (60),
`channel_cooldown_secs` (30), `list_cooldown_secs` (10), `default_title`, `announce_template`
(`Clip ready: {url}`; `{title}` allowed), `discord_share_enabled` + `discord_share_channel_id`,
`list_limit` (5), `poll_budget_ms` (6000, max 7000). Invalid config fails loud: viewers see
"misconfigured", admins see the exact violations via the `config` admin action.

## Async clip creation and the 10s invocation cap

Helix clips finish asynchronously, but the host caps one invocation at 10s
(`EXECUTOR_MAX_CALL_TIMEOUT_MS`, manifest max). So: create + bounded poll (<=7s) inside the command;
if still not ready the clip is parked in a self-expiring kv slot (`clip.pend.N`, 120s TTL). Pending
clips are resolved by (a) the next `!clip`/`!clips` (cap 2), (b) the `sweep` admin action, (c) a
`hub`/`bundle.reconcile` event once the host periodic hook delivers it. A pending clip older than
15s is reported with an explicit timeout message, never dropped silently.

## Admin actions (bundle-admin gateway)

The gateway delivers a process event `platform=hub, event_type=bundle.admin,
payload={action,args}`; the result comes back as event `bundle.admin.result`
`{admin_result:{ok,action,data|error,message}}`. Authorization (OIDC scope + tenant + community) is
the gateway's job; the bundle never trusts chat badges for admin work. Actions: `list` (recent
clips + pending count), `delete` (remove from index + hide from `!clips`; does NOT delete on
Twitch - Helix has no such call), `config` (effective config; validates an optional candidate
without storing), `sweep`. Declared in `hub-manifest.yaml` `admin_actions`.

## Discord share

When enabled, a finished clip is also relayed to the one configured Discord channel
(`chat.send:discord` via the relay import - the discord bundle exports no chat-send tool).

## State (kv, community-scoped, no ':' in keys)

`clip.item.N`/`clip.item.seq` (25-slot ring index, atomic increment, no read-modify-write),
`clip.pend.N`/`clip.pending.seq`, `clip.hidden.<id>`, `clip.cd.user.<uuid>`, `clip.cd.channel`,
`clip.cd.list`, `clip.lock` (30s in-flight lock serialising create), `clip.dedupe.<message_id>`
(redelivery guard, 10min).

## Contract with the Twitch bundle

`contract/twitch-clip-tools.json` is the JSON-Schema fixture of the three consumed tools. The test
host validates every tool call the bundle makes and every canned reply against it.

## Security / privacy

No credentials, no raw HTTP (no `egress`), cooldowns keyed by community user UUID (never a handle),
logs carry `op`/`outcome` only (never titles, URLs or ids), clip URLs from tools are host-allowlisted
(`clips.twitch.tv`, `twitch.tv`) before relaying, relay providers limited to twitch/discord.

## Build / test

```
cargo component build --release --target wasm32-wasip2 --locked
cargo test --locked          # unit + behaviour (contract-checked) + real component build/WIT test
cargo clippy --all-targets --locked -- -D warnings && cargo fmt --check && cargo deny check
```

Legacy parity: `action/interactive/clip_interaction_module` Twitch parts (`twitch_clip_service`
create, clip list). Stream-video clips/reels are the separate v3.1 `clip`/`clip-manager` bundles.

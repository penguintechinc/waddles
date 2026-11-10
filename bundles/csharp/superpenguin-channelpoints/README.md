# superpenguin-channelpoints (C#)

Channel-point **redemption-to-action policy** ported from [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
(MIT, used with permission; author `github/psychoboy`, collection `superpenguintv`, notice verbatim in `bundle.yaml`). App bundle,
not a service: it owns *when/what* a redemption does; the economy service owns balances, caps and idempotency, and the
Twitch platform service/host own every Twitch API call.

| | |
|---|---|
| App id | `waddles.integrations.superpenguin.channelpoints` |
| World | `stage-next-tools` (WASI component, `process-stage` + `action-stage` + `streaming-lifecycle`) |
| Flag | `waddles.command-superpenguin-channelpoints` (default OFF) |
| Consumes | Twitch `channel.channel_points_custom_reward_redemption.add`; `chat.message` with prefix `!redeem` (Twitch, Discord) |
| Tools consumed | `waddles.core.service.economy`: `points_view` (read), `adjust_balance` (write) |
| Host imports used | `kv`, `identity.resolve_actor`, `relay` (chat reply), `overlay.push` (alert-box), `log`, `flags` |
| State | `kv`, community-scoped: `cp.map` (mapping table), `cp.out.<hash>` (per-redemption outcome, 24 h), `cp.cnt.<reason>` (counters) |

## Commands (moderator / broadcaster only; badges are strict-boolean and fail-closed)

| Command | Effect |
|---|---|
| `!redeem map <reward-id\|"title"\|title> <steps>` | Add or replace the mapping for a reward (new mappings start enabled) |
| `!redeem unmap <ref>` | Remove a mapping |
| `!redeem enable <ref>` / `disable <ref>` | Per-mapping switch (a disabled mapping is ignored, counted) |
| `!redeem list` | First 8 mappings, `(+n more)` after that |

`<ref>` is the reward UUID, a quoted title (spaces allowed) or a one-word title. Titles match case-insensitively; **the reward id wins
over the title** when both are mapped. Table: at most 100 rows and 60 000 bytes (refused loudly, never truncated).

### Steps (joined by `&&`, at most 3, at most one `award`)

| Step | Behaviour |
|---|---|
| `award <1..1000>` | Credit the redeemer via economy `adjust_balance` (stable per-redemption retry key). A mint cap or departed member is logged and skipped; any other failure propagates |
| `say <template>` | Chat message to the broadcaster's channel (several `say` steps are joined with a space, cut at 400 chars) |
| `alert <text>` | Push a `superpenguin.channelpoints.redeem` alert-box message (see overlay below) |

Placeholders (case-insensitive): `{user}`/`{displayname}` (the host `{user:uuid}` placeholder, rendered to the name at egress),
`{reward}`/`{rewardtitle}`, `{cost}`/`{rewardcost}`, `{input}`/`{userinput}`/`{message}`, `{amount}` (the award, 0 if none),
`{currency}` (economy alias; fetched only if used). `{user}` is empty inside `alert` text: the overlay shows the redeemer separately.

## Redemption contract

The event payload is Twitch's EventSub redemption event JSON. Read fields: `id` (required), `status` (must be `unfulfilled`),
`user_input`, `broadcaster_user_login`, `broadcaster_user_id`, `reward.{id,title,cost}`.

| Case | Result |
|---|---|
| status other than `unfulfilled` | ignored, `cp.ignored reason=status count=n` |
| no mapping / mapping disabled | ignored, `reason=unmapped` / `disabled` (counted, not an error) |
| redeemer not linked / not a member | ignored, `reason=not_linked` (never invents a user) |
| malformed event / blank id | warning `cp.ignored reason=bad_event`, no output |
| replay of the same redemption id | same reply, credit moves once (outcome recorded **before** the credit; steps replayed from the record even if the mapping changed) |
| `say` step but no channel in the event | `cp.say_skipped reason=no_channel` warning (award/alert still run) |
| overlay push refused | exception propagates (fail loud); the award is deduplicated on redelivery |
| corrupt `cp.map` | exception (operator recovery); never replaced by an empty table |

Viewer input and reward titles are sanitised before echo: control characters, `{ } @` removed, leading `! / . \ ~ $ %` stripped,
100 chars max, so a redemption cannot forge a `{user:..}` placeholder, mass-mention or make the bot emit a command. Logs hold ids,
counts and reasons only.

## Browser-source setup

The overlay component lives in `overlay/` (declared in the manifest `overlay_assets` with sha256; provenance in `overlay/_ASSETS.md`).
It needs the presentation service's alert-box live channel (`GET /{overlay_code}/alert_box/live?key=...`, server-sent events).

1. Serve the three files (`index.html`, `redeem.css`, `redeem.js`) from any static host, or open them from disk in OBS.
2. In OBS add a **Browser** source with the URL
   `https://<your-static-host>/index.html?origin=https://<presentation-origin>&code=<16-hex overlay code>&key=<overlay view key>`
   (optional `&duration=6`, 2..15 s per card). Size it to the canvas; the background is transparent.
3. A green "Connected" pill appears for 3 s; a red pill names a bad `origin`/`code`/`key` parameter or a lost connection (EventSource
   reconnects by itself). Only alert type `superpenguin.channelpoints.redeem` is shown; redemptions queue (max 50) and play in order.

The page uses a strict Content-Security-Policy, system fonts, `textContent` only, and no third-party runtime resources.

## Host needs (this bundle is not live-complete until they exist)

1. **Ingest does not forward the Twitch redemption event today** (no EventSub subscription / event type mapping for
   `channel.channel_points_custom_reward_redemption.add`, which also needs the broadcaster's `channel:read:redemptions` scope held host-side).
2. **Twitch relay channel convention**: the chat reply uses `broadcaster_user_login` (fallback `broadcaster_user_id`) as the relay `channel`;
   confirm that is what the Twitch `chat.send` relay accepts (the chat-command path uses the inbound `channel_id`).
3. **`overlay.push` has no stage-side handler yet** (`core/bundle_executor` answers `not-implemented`), so any mapping with an `alert` step
   fails loud until the host lands it; award/say-only mappings are unaffected. The alert carries the user UUID in `alert.user` so the
   presentation service resolves the display name.
4. The host context does not forward app config JSON, so mappings are chat-managed (no config schema).

## Deferred (platform operations, not ported on purpose)

Helix reward create/update/delete, `ChannelPointSetEnabledState` / `SetPausedState` sub-actions, and fulfilling/cancelling a redemption are
Twitch API calls owned by the Twitch platform service; no tool exists in the catalog, so none is invented here. Watch-streak variables
(`{WatchStreak.*}`) and `GiftPoints`/`Fireworks`/`Sound`/music-queue actions of upstream recipes belong to other app/service bundles.

## Deviations from upstream

- Mappings are explicit rows managed by chat commands instead of Actions-engine recipes; title matching is case-insensitive (upstream: exact title).
- The redeemer is the Waddles community user uuid, not a Twitch login; names appear through the host placeholder.
- Upstream could run arbitrary sub-action lists; here only `award`, `say`, `alert` exist (no scripts, no queues).
- Overlay: only the generic alert idea of `alerts.html` is reused; clips/gifs/audio hooks are out of scope (see `overlay/_ASSETS.md`).

## Build and test

```bash
python3 -I scripts/gen-superpenguin-manifests.py channelpoints   # regenerate bundle.yaml + hub-manifest.yaml from manifest.src.yaml
bash scripts/test-superpenguin-bundle.sh channelpoints           # xUnit + 90% line-coverage floor (containerised .NET SDK)
bash scripts/build-superpenguin-bundle.sh channelpoints          # real WASM component, exports checked with wasm-tools
```

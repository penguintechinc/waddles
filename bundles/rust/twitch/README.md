# Twitch platform connector (`waddles.core.service.twitch`)

P6b Task 1. A `kind: service` Rust WASM component on `world connector-tools`
that moves Twitch specifics out of `core/svc_ingest/src/ingest/twitch*.rs`.
It holds **no secret, no raw target and no transport code**: the host owns the
sockets, HMAC verification, secret substitution and channel pinning.

```
EventSub webhook (host HMAC route) ─┐
IRC over host WsTransport ──────────┼─> receiver.on-frame ─> normalized-event
                                    │   (JSON object = EventSub, else IRC text)
on-connect ─> handshake template (slots only)      on-heartbeat-due ─> PING
sender.build-request(chat.send|chat.delete|dm.send) ─> http-request-tpl (slots only)
tool-provider: get_chatters | get_followers | get_subscriptions
```

## Cutover

PostHog flag `waddles.connector-twitch`, default OFF. The host selects the path;
the guest additionally refuses every entry point (`frame-error::unsupported`)
while the flag is OFF. While OFF the hardcoded path runs unchanged. Nothing
hardcoded is deleted here (that is T4).

## Parity

`tests/parity.rs` runs all 23 `tests/data_plane_parity/corpus/twitch/*.json`
vectors through the connector logic and compares event type, actor and payload
with the hardcoded golden (`source` is host-derived from the pinned connection;
`occurred_at` is the host-stamped `received_at`). Run `cargo test --locked`.

## Host configuration the integrator must provide

| Item | Value |
|---|---|
| IRC `WsConfig.slot_prefixes` | `PASS oauth:`, `NICK `, `JOIN #` |
| IRC `WsConfig.handles` | `pass` (oauth token), `nick`, `channel` -> the community's pinned connection |
| Handshake slots | `{{TWITCH_PASS}}`, `{{TWITCH_NICK}}`, `{{TWITCH_CHANNEL}}` |
| Webhook route | `Scheme::twitch_eventsub()` + `secret_ref`, owner `twitch` |
| Outbound (T6d) slots | exact set below |

### Slot set for T6d (substituted by the host, never by the connector)

| Slot | Handle | Where it appears | Host binds from |
|---|---|---|---|
| `{{TWITCH_TOKEN}}` | `token` | `Authorization: Bearer ` header | community connection secret |
| `{{TWITCH_CLIENT_ID}}` | `client_id` | `Client-Id` header | bot application id |
| `{{TWITCH_BROADCASTER_ID}}` | `broadcaster_id` | chat.send body, chat.delete + read-tool URL query | pinned connection / `action.target-source-id` |
| `{{TWITCH_SENDER_ID}}` | `sender_id` | chat.send body, chat.delete `moderator_id`, whisper `from_user_id`, `get_chatters` `moderator_id` | pinned bot user id |

Read tools (`get_chatters`, `get_followers`, `get_subscriptions`) send these slots in the URL query
through the `http` import; the host must substitute them on tool requests exactly as on sender
templates. Until T6d lands a read tool sends the literal slot and Twitch answers 400
(`invalid-args`), a loud failure. Tools take NO destination arguments (`broadcaster_id`,
`moderator_id`, `channel`, `room_id`, `sender_id`, `target`, ... are rejected). Header refs for the
read tools stay `Authorization` -> `twitch_bearer`, `Client-Id` -> `twitch_client_id`.

## Fail-loud parsing

A missing/non-object `event`, a missing/empty broadcaster id, or a present field of the wrong type is a
typed `frame-error::malformed` (never a defaulted/fabricated event). Only documented optional metadata
(`tier`=1000, `total`=1, `viewers`/`bits`=0, ...) defaults, and only when ABSENT. Declared divergence: a raid
keyed only by `to_broadcaster_user_id` yields that id (the hardcoded path published an empty one).

## Security properties (each has a test)

- Connector-supplied channel/broadcaster/sender/moderator fields in an action payload are never read; the template is identical with or without them.
- Every guest/user-derived string that reaches a template goes through `sender::reject_unsafe`: control and invisible format characters (NUL, U+200B-200F, U+2028-202E, U+2060-206F, U+FEFF, tag chars) are REJECTED (never stripped), then `{{` and `}}` are rejected, so no transformation can assemble a live slot. `verify_slots` re-checks the finished template: the only brace pairs present must be declared slots.
- Reconnect re-sends one `JOIN` and one `CAP REQ` regardless of session token (no stacked subscriptions); EventSub dedup stays host-side.
- Read tools return canonical UUIDs only (`identity.lookup`); no handle or numeric Twitch id leaves the connector.
- Logs carry `op` + counts only. No kv is used.

## Known gaps (need host/WIT work, outside this bundle's allowed edit set)

1. **IRC `PING` -> `PONG`**: `on-frame` can only return events, so the guest cannot answer a server `PING`. `ws.rs` (or a WIT reply channel) must auto-`PONG`. The connector sends a client `PING` on every heartbeat to keep the idle timer fed.
2. **Self-echo filter (enabling the connector REQUIRES the host self-filter or a `self-nick` in `connection-ctx`)**: the guest has no self identity (`irc::GUEST_SELF_NICK = None`), so it cannot drop the bot's own `PRIVMSG` (vector `irc_self_message_dropped`). Pinned by `guest_path_without_self_nick_differs_only_on_self_echo` and `self_echo_gap_is_pinned_until_host_filter_lands`, which fail on purpose once the gap closes. Until then `waddles.connector-twitch` stays OFF (default; `common::gate_decision` refuses every entry point while OFF, tested).
3. **`RECONNECT`/login failure** surface as `frame-error` (counted, connection stays); the host should treat `backend("irc login rejected")` as park-worthy.
4. **Actor = `identity.lookup` UUID, no raw-handle fallback.** Key = platform user id (IRC `user-id` tag; EventSub `user_id`, else `broadcaster_id`), else the login when the wire carries no id. A failed lookup drops the event and is counted (`actor_lookup_dropped`, no key logged); a frame with nothing left returns `frame-error::backend`. Requires the `connector.pii.read` grant, which is declared in `hub-manifest.yaml` (catalog id: dangerous, core-only; pre-granted by the core seeder). Without the grant lookups are denied and every actor-bearing event fails loud. Payload fields (`author`, `user_login`, ...) still match the hardcoded golden and are tokenized downstream by `pii_tokenize`. Declared divergence: `tests/data_plane_parity/expected/twitch/divergences.json` (self-echo).
5. **Outbound slot semantics (Part B)** follow the handshake-slot convention; reconcile with T6d once specified. Read tools use the host-pinned slots above.
6. The connector needs the connector `identity.lookup` import for read tools.

## Moderation (v1.1.0)

Tools `twitch.moderation.{ban,timeout,unban,delete_message}` (manifest `required_scope: community:moderate`, `risk: dangerous`; the host verifies the invoking actor). The guest has no mutating egress: the host serves an authorized call by queueing the pinned outbound op `mod.ban` / `mod.timeout` / `mod.unban` / `chat.delete`, whose Helix template `moderation.rs` / `sender.rs` build. `broadcaster_id` / `moderator_id` are host slots; payload keys naming a destination are ignored. `tools::moderation_op` validates tool args (UUID `user`, `duration` 1..=1209600 s, `reason` <= 500 chars) and yields the producer-envelope fields; for ban/timeout the params ride in `text` as a JSON object `{duration?, reason?}`. Permissions: `moderation.twitch`, `chat.delete:twitch`.

Not implemented (fail loud): `clear_chat` (policy has no kind for `DELETE /moderation/chat` without `message_id`) and whispers (`dm.send` needs a host duty). Parity vs legacy `twitch_service.py`: `tests/moderation_parity.rs` (moderator is the bot connection, not the broadcaster).

## Channel extras (v1.2.0)

| tool | kind | where it runs | Helix |
|---|---|---|---|
| `twitch.clip.create` | `clip.create` | host op (`clips:edit`) | `POST /clips?broadcaster_id=` |
| `twitch.clip.get` / `.list` | - | guest read | `GET /clips` |
| `twitch.schedule.create/update/delete` | `schedule.*` | host op (`channel:manage:schedule`) | `POST/PATCH/DELETE /schedule/segment` |
| `twitch.schedule.list` | - | guest read | `GET /schedule` |
| `twitch.live.channels` | - | guest read | `GET /users` + `GET /streams` |
| (reconcile) | `eventsub.create/delete/list` | host op, APP token | `/eventsub/subscriptions` |

Schemas: clips = twitch-clips `contract/twitch-clip-tools.json`; schedule create/update/delete = calendar `contracts/twitch.schedule.*.schema.json`; live = `bundles/contracts/live-channels-contract.v1.json` (manifest entry verified by `scripts/gen-live-manifest-tools.py twitch --check`). Failure codes (`InvalidArgs("<code>: ...")`): `not_live|rate_limited|not_connected|clips_disabled|platform_unavailable`; `clip.create` status->code mapping is the host executor's (404 not live, 403 clips disabled, 401 not connected, 429 rate limited).

- Host ops follow the moderation pattern: the guest refuses, `tools::host_op` validates, the producer envelope carries parameters as JSON-object `text`, `broadcaster_id` is the host slot. Schedule update/delete: the segment id rides in `text`; the HOST strips it and fills the `id` query (`TWITCH_SEGMENT_ID` duty slot) because segment ids are base64 with `=` padding.
- Reads run in the guest through the guarded `http` import like `get_followers` (same host-substitution precondition for `{{TWITCH_BROADCASTER_ID}}`). `clip.get` needs no broadcaster, so it works without it.
- **EventSub** (`eventsub_sub.rs`): `create` carries only `{type, version}`; `transport.callback` and `transport.secret` are HOST-DUTY slots (`TwitchDuties`, `core/svc_ingest/.../twitch_slots.rs`) built from the work item's opaque `callbackKey` (the secret is fetched from hub-api per call and never reaches the guest or a template). The condition names only `app.broadcaster_id`. `reconcile()` implements the hub-api pass (reconcile -> execute -> results with nonces); orphan GC runs only when `enabledIdsComplete`, the Twitch listing is one complete page, and only for our webhook path (cap 25/pass).
- **Cross-tenant guard.** The app token can act on every community's subscriptions, so `eventsub.delete` is ownership-bound by the host (`TwitchDuties::check_owned`: it resolves the subscription and refuses unless its condition names the invoking community's own broadcaster) and is not claimed at all without a `TwitchAppSource`; `eventsub.list` is claimed by no hook and fails closed (`HostDutyMissing`), so the reconcile GC cannot run until a per-broadcaster filter exists (it then deletes nothing). Regression tests: `twitch_slots.rs` + `outbound_exec.rs::twitch_eventsub_delete_is_bound...`.
- Unsupported, fail loud: clip `title` / `duration_s` (query values the host verifier cannot carry), `twitch.live.channels` `scope: linked`, whispers (`dm.send`, needs the host DM duty).

## Build / test

```
cargo test --locked && cargo clippy --all-targets --locked -- -D warnings
cargo fmt --check && cargo deny check
cargo llvm-cov --locked --summary-only
cargo component build --release --target wasm32-wasip2 --locked
```

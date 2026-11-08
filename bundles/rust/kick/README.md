# Kick platform service bundle (`waddles.core.service.kick`)

A `kind: service` Rust WASM component on `world connector-tools`. It holds **no secret, no raw
target and no transport code**: the host owns the sockets, webhook signature verification, secret
substitution and channel pinning. Platform logic lives here; the data plane stays Rust.

```
Pusher WS (host WsTransport, profiles/kick.rs) ─┐
signed webhook (host verifies, wraps envelope) ─┼─> receiver.on-frame ─> normalized-event
                                                │   ({"event":..} = Pusher, else {"type","body"} envelope)
on-connect ─> one pusher:subscribe (slot only)       on-heartbeat-due ─> pusher:ping
sender.build-request(chat.send | chat.delete | mod.ban | mod.timeout | mod.unban) ─> http-request-tpl (slots only)
tool-provider: kick.live.channels
```

## Cutover

PostHog flag `waddles.connector-kick`, default OFF; the guest refuses every entry point while OFF.
The legacy Python path (`core/svc_ingest/receivers/kick_pusher.py`, `builtin_handlers/kick_*.py`)
keeps running unchanged; nothing legacy is deleted here (`.ci/builtin-handler-map.yaml` stays
`pending` until the seams below land).

## Inbound

| Source | Events |
|---|---|
| Pusher `App\Events\ChatMessageEvent` | `message` (parity with the Python receiver) |
| Webhook `chat.message.sent` | `message` |
| Webhook `channel.followed` | `channel.follow` |
| Webhook `channel.subscription.new` / `.renewal` | `channel.subscribe` (`metadata.is_renewal`, `.duration`) |
| Webhook `channel.subscription.gifts` | `channel.subscription.gift` (`metadata.total`, `.is_anonymous`; giftee identities never published) |
| Webhook `livestream.status.updated` | `stream.online` / `stream.offline` |

Other webhook types (rewards, metadata, bans, kicks) are ignored. Event kinds and payload shape
match the Twitch normalizers; every payload field is in the host PII classifier's closed schema
(`core/svc_process/src/pii_tokenize.rs`, which gained the metadata keys `duration` and
`is_renewal`). Actors are `identity.lookup` UUIDs, never raw handles; a failed lookup drops the
event (fail closed).

### Webhook signature (official)

<https://docs.kick.com/events/webhook-security>: RSA PKCS#1 v1.5 / SHA-256 over
`"{Kick-Event-Message-Id}.{Kick-Event-Message-Timestamp}.{raw body}"`, base64 in
`Kick-Event-Signature`, key fetched from `GET https://api.kick.com/public/v1/public-key` (never
hardcoded). The host side is `core/svc_ingest/src/connector/kick_webhook.rs` (typed errors,
replay window, constant-time verify, envelope builder); the event type is only in the
`Kick-Event-Type` header, so the host wraps each verified delivery as
`{"type","version","message_id","timestamp","body"}` for this bundle.

### Declared divergences from the Python path

Pinned by `tests/parity.rs` (corpus `tests/data_plane_parity/corpus/kick/*.json`):

- `channel_slug` dropped (the channel is the host-pinned source); `created_at` dropped
  (`occurred_at` is the host-stamped arrival time).
- `chatroom_id` (int) -> `room_id` (string); `is_owner` -> `is_broadcaster` (Twitch-shaped,
  PII-classified names); added `author`, `user_id`, `is_vip`.
- A chat message with no sender id is refused (`frame-error::malformed`); Python published it with a
  null actor.
- Webhook scheme: official RSA signature replaces the legacy `X-Kick-Signature` HMAC, which is not
  part of Kick's documented API. Chat send uses the official `POST /public/v1/chat`, not the
  unofficial `kick.com/api/v2/messages/send/<chatroom>`.

## Outbound

Templates only (the host substitutes `{{KICK_TOKEN}}` -> handle `token`, per send, from the
community's Kick OAuth connection; OAuth 2.1 + PKCE is the hub-api gateway flow, tokens are
host-side only). API: <https://docs.kick.com/apis/chat>.

| Op | Request |
|---|---|
| `chat.send` | `POST /public/v1/chat` `{"content","type":"bot"[,"reply_to_message_id"]}` — Kick delivers a bot post to the channel attached to the token |
| `chat.delete` | `DELETE /public/v1/chat/{message_id}`; the host requires a proven channel (stamp pin) before sending |
| `mod.ban` | `POST /public/v1/moderation/bans` `{"broadcaster_user_id":"{{KICK_BROADCASTER_ID}}"→int,"user_id":<int>[,"reason"]}`; `text` = optional plain reason (≤100 chars) |
| `mod.timeout` | same endpoint plus `"duration"` (1..=10080 minutes): `text` = `{"duration":N[,"reason":".."]}` (required) |
| `mod.unban` | `DELETE /public/v1/moderation/bans` `{"broadcaster_user_id":..,"user_id":..}`; no `text` |

The broadcaster is the host slot `{{KICK_BROADCASTER_ID}}` (the host writes a JSON integer from the community connection's own
`broadcaster_user_id` binding metadata; a bundle can never name another channel). `user_id` must be a non-zero digit string and is
emitted as a JSON integer. The host refuses a request whose target equals the broadcaster (`protected_target`). `reason`/`text` pass the
same unsafe-character and brace gate as chat text.

`kick.live.channels` (catalog S8 `*.live.channels` contract): `GET /public/v1/channels` with no
query returns the authenticated user's channel, i.e. exactly the community's own connection. Only an
allowlist of fields is copied (the response also carries the stream KEY and ingest URL; tests prove
they never leak). `channel_ref` is the broadcaster's `identity.lookup` UUID. `scope: linked` is
refused (the host exposes one pinned channel). Contract fixture:
`tests/fixtures/live_channels_contract.json` (swap for `bundles/contracts/live-channels-contract.v1.json`
when it lands).

## Known gaps (documented, not hidden)

- **Self-echo**: the guest has no self identity and the host `SelfEchoFilter` grammar is IRC-only,
  so the bot's own chat posts are ingested like any message (the legacy Python receiver does the
  same). A bundle that replies with command-looking text can therefore re-trigger itself; keep
  `waddles.connector-kick` OFF for a community until a host-side self-id filter exists.
- **Server `pusher:ping`**: `on-frame` can only return events, so the guest cannot answer a server
  ping; the 25 s client `pusher:ping` (below Pusher's activity timeout) keeps the server from ever
  needing to ping.

## Host seams

Landed in svc_ingest / svc_action: the Kick channel pin, the integer body slot (`SlotShape::Int`), `KickSlots`, the webhook scheme +
envelope, the executor queue (`SenderStatus::ExecutorQueue`, `kick` in the relay providers), the registered `chat.delete` / `mod.*` ops
(`policy/kick.rs`, `platform_ops.rs`, fixture, `chat.delete:kick` + `moderation.kick` permissions), the webhook route plan
(`Scheme::kick(<broadcaster_user_id>)` on a second supervised lane) and the events subscription (`connector/kick_subscribe.rs`).

Still open:

4. **Read-tool slots** — `kick.live.channels` needs only the bearer (header ref `kick_bearer`); the
   host `http` import must resolve it for kick tool calls.

## Build / test

```
cargo test --locked && cargo clippy --all-targets --locked -- -D warnings
cargo fmt --check && cargo deny check
cargo component build --release --target wasm32-wasip2 --locked
```

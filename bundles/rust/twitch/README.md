# Twitch platform overlay (`waddles.core.service.twitch`)

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

PostHog flag `waddles.overlay-twitch`, default OFF. The host selects the path;
the guest additionally refuses every entry point (`frame-error::unsupported`)
while the flag is OFF. While OFF the hardcoded path runs unchanged. Nothing
hardcoded is deleted here (that is T4).

## Parity

`tests/parity.rs` runs all 23 `tests/data_plane_parity/corpus/twitch/*.json`
vectors through the overlay logic and compares event type, actor and payload
with the hardcoded golden (`source` is host-derived from the pinned connection;
`occurred_at` is the host-stamped `received_at`). Run `cargo test --locked`.

## Host configuration the integrator must provide

| Item | Value |
|---|---|
| IRC `WsConfig.slot_prefixes` | `PASS oauth:`, `NICK `, `JOIN #` |
| IRC `WsConfig.handles` | `pass` (oauth token), `nick`, `channel` -> the community's pinned connection |
| Handshake slots | `{{TWITCH_PASS}}`, `{{TWITCH_NICK}}`, `{{TWITCH_CHANNEL}}` |
| Webhook route | `Scheme::twitch_eventsub()` + `secret_ref`, owner `twitch` |
| Outbound (T6d) slots | `{{TWITCH_TOKEN}}`, `{{TWITCH_CLIENT_ID}}`, `{{TWITCH_BROADCASTER_ID}}`, `{{TWITCH_SENDER_ID}}` with handles `token`, `client_id`, `broadcaster_id`, `sender_id`; broadcaster/sender ids bound from `action.target-source-id` |
| Read-tool header refs (`http.request.secret-refs`) | `Authorization` -> `twitch_bearer`, `Client-Id` -> `twitch_client_id` |

## Security properties (each has a test)

- Overlay-supplied channel/broadcaster/sender/moderator fields in an action payload are never read; the template is identical with or without them.
- User text containing `{{` is rejected, so text can never smuggle a slot the host would substitute.
- Reconnect re-sends one `JOIN` and one `CAP REQ` regardless of session token (no stacked subscriptions); EventSub dedup stays host-side.
- Read tools return canonical UUIDs only (`identity.lookup`); no handle or numeric Twitch id leaves the overlay.
- Logs carry `op` + counts only. No kv is used.

## Known gaps (need host/WIT work, outside this bundle's allowed edit set)

1. **IRC `PING` -> `PONG`**: `on-frame` can only return events, so the guest cannot answer a server `PING`. `ws.rs` (or a WIT reply channel) must auto-`PONG`. The overlay sends a client `PING` on every heartbeat to keep the idle timer fed.
2. **Self-echo filter**: the guest has no self identity, so it cannot drop the bot's own `PRIVMSG` (vector `irc_self_message_dropped`). Passes with the nick supplied (host-side filter or a `self-nick` in `connection-ctx`); `guest_path_without_self_nick_differs_only_on_self_echo` pins the gap. Twitch does not echo own messages, so risk is low.
3. **`RECONNECT`/login failure** surface as `frame-error` (counted, connection stays); the host should treat `backend("irc login rejected")` as park-worthy.
4. **Actor is the raw login** (parity with the hardcoded path; tokenization is downstream `pii_tokenize`), unlike the connector WIT comment that says on-frame actors are already UUIDs. Needs an owner decision before the hardcoded path is deleted.
5. **Outbound slot semantics (Part B)** follow the handshake-slot convention; reconcile with T6d once specified. Read tools take `broadcaster_id` as an argument until T6d pins it host-side.
6. The overlay needs the connector `identity.lookup` import for read tools; the permission catalog has no id for it yet.

## Build / test

```
cargo test --locked && cargo clippy --all-targets --locked -- -D warnings
cargo fmt --check && cargo deny check
cargo llvm-cov --locked --summary-only
cargo component build --release --target wasm32-wasip2 --locked
```

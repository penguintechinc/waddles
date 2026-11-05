# waddles.core.service.youtube -- YouTube platform overlay (Rust, `connector-tools`)

P6b Task 3. A `kind: service` connector overlay: the **host** owns the poll transport, SSRF pinning and
credentials; this component owns the **YouTube knowledge** -- frame parsing, normalization, page-token /
interval / quota handling, request templates, chat send. YouTube is NOT greenfield: this is a port of the
Python `core/svc_ingest` path, which stays live and unchanged until PostHog flag
`waddles.overlay-youtube` (default OFF) is turned on. Nothing hardcoded is deleted here (that is T4).

```
host PollTransport (PollConfig: url, cursor_param=pageToken, cursor_pointer=/nextPageToken)
   --response body--> receiver.on-frame --> normalized-event[]  (identical to Python normalize())
host outbound (T6d) <-- sender.build-request(chat.send) -- template + secret-ref slot (no token)
app bundles --> tool-provider.call (poll_plan, classify_error, discovery_*, build_send_chat_request)
```

## Behavior inventory (Python reference -> overlay)

| Area | Python (`youtube_live_poll.py`) | Overlay |
|---|---|---|
| Discovery | `search.list?eventType=live&type=video&maxResults=5` (100 units) then `videos.list?part=liveStreamingDetails` (1 unit) -> `activeLiveChatId`; only while no chat is known | `discovery_request` / `discovery_parse` tools (same URLs, `parse_search`/`parse_videos`). **Host drives the sequence** |
| Poll | `liveChat/messages?part=id,snippet,authorDetails&liveChatId&maxResults=200` | `poll_request` tool (host builds `PollConfig.url` from it) |
| Page token | `nextPageToken` carried to next call; reset to none on chat end | host cursor: `cursor_param=pageToken`, `cursor_pointer=/nextPageToken`; `poll_plan` tool mirrors it |
| Interval | `pollingIntervalMillis` (default 5000), floored at 2 s | `poll_plan` (same default/floor). **Host `PollConfig.interval` is static** (gap below) |
| Message types | every item with `snippet.displayMessage` -> `message` (text, Super Chat, membership...); items without it skipped | identical; skipped items counted, never raised |
| Auth | `key=` API key, or OAuth refresh-token -> bearer, one refresh+retry on 401 | templates carry `Authorization: Bearer {{YT_OAUTH_TOKEN}}` slot -> handle `youtube.oauth`; host adapter resolves/refreshes. Overlay never sees a token |
| Errors | 403 `quotaExceeded/dailyLimitExceeded/rateLimitExceeded/userRateLimitExceeded` -> 30 s backoff, stop after 5 consecutive; other 403 (`liveChatEnded`...) -> reset state + rediscover, quota counter untouched; 401 refresh; 429/5xx retryable; other 4xx fatal | `classify_error` tool (same classes/actions/counters) |
| Normalize | `text.strip()`, `actor = author_id or "unknown"`, strict bool flags (only real `true`), `occurred_at = occurred_at or published_at or now` | `logic::normalize_raw` -- byte-for-byte golden parity |

## Frames accepted by `on-frame`

1. A real `liveChat/messages` list response (what the host poll transport yields). `live_chat_id` comes from
   `snippet.liveChatId`. `video_id` is **null** here (it is host discovery state, not in the response).
2. A receiver-raw event dict (`{text, live_chat_id, author_id, ...}`) -- the Python/corpus contract.

## Tools (all read-only template/classification helpers; none performs I/O)

`discovery_request`, `discovery_parse`, `poll_request`, `poll_plan`, `classify_error`, `build_send_chat_request`.

## Sender (Part B)

`build-request` supports `chat.send` -> `POST liveChat/messages?part=snippet` with `target-source-id` as the
`liveChatId` (host-resolved from the binding; the payload can supply ONLY `text`, <=200 chars, so an app cannot
retarget a message) and the credential slot above. Real execution needs T6d (generic host outbound); the
executor `ConnectorHost` does not export `sender` yet, so the template is proven at the component level only.

## Gaps for the integrator (not in this bundle's scope)

- `PollTransport` treats **every 403 as Fatal** (`poll.rs`): a quota 403 and a `liveChatEnded` 403 both kill the
  poller instead of backing off/rediscovering. Needs a host hook calling `classify_error`.
- `PollConfig.interval` is static; `pollingIntervalMillis` cannot feed back. Needs a per-frame interval hint.
- No host-side discovery loop (search -> videos -> set `liveChatId` in the URL).
- `hub_api/services/bundle_manifest_v2.py` has no connector `receiver`/`sender` stage; this manifest uses the
  `action` stage with `tool-provider#call` like economy.
- `.ci/builtin-handler-map.yaml` still lists `youtube_live_ingest` as pending; flip it once this lands + T4.

## Parity evidence

- `tests/parity.rs`: corpus `youtube/live_chat_message.json` + goldens from `test_builtin_youtube_live_ingest.py`
  and `test_receivers_youtube_live_poll.py` fixtures (strip semantics incl. 0x1c-0x1f, flags fail closed, `unknown`
  actor, occurred_at fallback, skipped items). Edge cases were run through the real Python `normalize()` once.
- `core/bundle_executor/tests/youtube_overlay.rs`: the REAL compiled component through production
  `ConnectorHost::load` (`scripts/test-overlay-youtube.sh`).

## Build / test

```bash
cd bundles/rust/youtube
cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings
cargo test --locked && cargo llvm-cov --locked --fail-under-lines 90
cargo deny check
cargo component build --release --target wasm32-wasip2 --locked
scripts/test-overlay-youtube.sh   # repo root: build + real-path executor tests
```

## Operational notes

Logs are PII-free (`op`, `events`, `skipped` counts only). Egress: `www.googleapis.com` (GET/POST). No `kv`, so
no key-charset concerns. Version bump on any change.

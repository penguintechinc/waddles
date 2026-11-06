# waddles.core.service.youtube -- YouTube platform connector (Rust, `connector-tools`)

P6b Task 3. A `kind: service` connector service bundle: the **host** owns the poll transport, SSRF pinning and
credentials; this component owns the **YouTube knowledge** -- frame parsing, normalization, page-token /
interval / quota handling, request templates, chat send. YouTube is NOT greenfield: this is a port of the
Python `core/svc_ingest` path, which stays live and unchanged until PostHog flag
`waddles.connector-youtube` (default OFF) is turned on. Nothing hardcoded is deleted here (that is T4).

```
host PollTransport (PollConfig: url, cursor_param=pageToken, cursor_pointer=/nextPageToken)
   --response body--> receiver.on-frame --> normalized-event[]  (identical to Python normalize())
host outbound (T6d) <-- sender.build-request(chat.send) -- template + secret-ref slot (no token)
app bundles --> tool-provider.call (poll_plan, classify_error, discovery_*, build_send_chat_request)
```

## Behavior inventory (Python reference -> connector)

| Area | Python (`youtube_live_poll.py`) | Connector |
|---|---|---|
| Discovery | `search.list?eventType=live&type=video&maxResults=5` (100 units) then `videos.list?part=liveStreamingDetails` (1 unit) -> `activeLiveChatId`; only while no chat is known | `discovery_request` / `discovery_parse` tools (same URLs, `parse_search`/`parse_videos`). **Host drives the sequence** |
| Poll | `liveChat/messages?part=id,snippet,authorDetails&liveChatId&maxResults=200` | `poll_request` tool (host builds `PollConfig.url` from it) |
| Page token | `nextPageToken` carried to next call; reset to none on chat end | host cursor: `cursor_param=pageToken`, `cursor_pointer=/nextPageToken`; `poll_plan` tool mirrors it |
| Interval | `pollingIntervalMillis` (default 5000), floored at 2 s | `poll_plan` (same default/floor). **Host `PollConfig.interval` is static** (gap below) |
| Message types | every item with `snippet.displayMessage` -> `message` (text, Super Chat, membership...); items without it skipped | identical; skipped items counted, never raised |
| Auth | `key=` API key, or OAuth refresh-token -> bearer, one refresh+retry on 401 | templates carry `Authorization: Bearer {{YT_OAUTH_TOKEN}}` slot -> handle `youtube.oauth`; host adapter resolves/refreshes. Connector never sees a token |
| Errors | 403 `quotaExceeded/dailyLimitExceeded/rateLimitExceeded/userRateLimitExceeded` -> 30 s backoff, stop after 5 consecutive; other 403 (`liveChatEnded`...) -> reset state + rediscover, quota counter untouched; 401 refresh; 429/5xx retryable; other 4xx fatal | `classify_error` tool (same classes/actions/counters) |
| Normalize | `text.strip()`, `actor = author_id or "unknown"`, strict bool flags (only real `true`), `occurred_at = occurred_at or published_at or now` | `logic::normalize_raw` -- byte-for-byte golden parity |

## Frames accepted by `on-frame`

1. A real `liveChat/messages` list response (what the host poll transport yields). `live_chat_id` comes from
   `snippet.liveChatId`. `video_id` is **null** here (it is host discovery state, not in the response).
2. A receiver-raw event dict (`{text, live_chat_id, author_id, ...}`) -- the Python/corpus contract.

## Tools (all read-only template/classification helpers; none performs I/O)

`discovery_request{step}`, `discovery_parse`, `poll_request{max_results?}`, `poll_plan`, `classify_error`,
`build_send_chat_request{target_source_id, payload{text}}`. Request tools take no ids (slots, above).

## Sender (Part B)

`build-request` supports `chat.send` -> `POST liveChat/messages?part=snippet`. `target-source-id` is validated but not
embedded: the destination is the host-pinned `{{YT_LIVE_CHAT_ID}}` slot and the payload can supply ONLY `text`
(<=200 chars), so an app cannot retarget a message; credential slot as above. Real execution needs T6d (generic host outbound); the
executor `ConnectorHost` does not export `sender` yet, so the template is proven at the component level only.

## Host config (slots the host must pin and substitute)

The connector never takes or embeds caller-chosen ids or credentials. Templates carry slots; `secret_refs`
pairs each slot with a handle. The host substitutes at send time (URL-encode in URLs, JSON-escape in bodies).

| Slot | Handle | Pinned from |
|---|---|---|
| `{{YT_OAUTH_TOKEN}}` | `youtube.oauth` | per-tenant bot credential (credential adapter) |
| `{{YT_LIVE_CHAT_ID}}` | `youtube.live_chat_id` | host discovery state / outbound binding |
| `{{YT_CHANNEL_ID}}` | `youtube.channel_id` | the monitored source's config |
| `{{YT_VIDEO_ID}}` | `youtube.video_id` | the live video the host discovered |

**Host substitution contract (MUST):** `{{YT_CHANNEL_ID}}`, `{{YT_VIDEO_ID}}`, `{{YT_LIVE_CHAT_ID}}` sit in QUERY-value
position (`channelId=`, `id=`, `liveChatId=`) -- the host MUST percent-encode (RFC 3986, everything outside
`A-Za-z0-9-._~`) the substituted value, and JSON-escape it in the `chat.send` body. The bundle does not trust that:
every id/token it emits or returns is an anchored allowlist, and every literal query value it builds itself is
percent-encoded (`pct`), so a missing host encode still cannot inject a parameter from bundle-produced values.

| Value | Allowlist (anchored) |
|---|---|
| channel / video / live-chat id | `^[A-Za-z0-9_-]{1,128}$` (real: `UC`+22 base64url, 11-char video ids, `Cg0KC...`/`KicKGFVD...` chat ids) |
| `nextPageToken` | `^[A-Za-z0-9_=.-]{1,512}$` |
| `maxResults` | integer `1..=2000` built from a `u32`, never a string |

Anything else (`& = # % / ? + space`, unicode, quotes, over-length) is a typed `Malformed` error. If a legitimate
live-chat id ever contains a character outside this set, widen `is_valid_id` deliberately and add a fixture -- do not
loosen it silently.

Input hardening: every string that reaches a template (chat text, target-source-id, API-returned ids, page
tokens) is REJECTED with a typed error if it holds a control character, a zero-width/format/bidi character,
or lookalike braces, and then if it holds `{{` or `}}` -- rejected, never stripped (stripping first let
`{<ctrl>{SECRET}}` forge a slot in the Twitch connector review). Chat text with a newline is therefore refused.

Actor tokenization: `on-frame` resolves each distinct author via `identity.lookup(platform-identity)` (needs the
core `connector.pii.read` grant, which the hub permission catalog does not know -- see gaps). No `author_id`
-> the literal `unknown` is looked up, exactly as the Python path tokenized it. Erased users drop the event
(counted); rate-limited/denied/backend lookups fail the frame (host holds the cursor and retries).

## Gaps for the integrator (not in this bundle's scope)

- Host classification (`PollLifecycle`) now backs off quota 403s and rediscovers on `liveChatEnded` (host-side, via
  `liveBroadcasts`, see `docs/connector-host-capability.md`); the guest `classify_error` tool is still not called by
  the host (same classes, evaluated host-side).
- `PollConfig.interval` is static; `pollingIntervalMillis` cannot feed back. Needs a per-frame interval hint.
- Rediscovery is host-side and only runs after a `liveChatEnded`; a binding with no `live_chat_id` yet is still
  skipped (initial discovery is hub-api's job).
- `hub_api/services/bundle_manifest_v2.py` has no connector `receiver`/`sender` stage; this manifest uses the
  `action` stage with `tool-provider#call` like economy.
- `connector.pii.read` is declared in `hub-manifest.yaml` (catalog id exists; dangerous, core-only) and is
  pre-granted by the core seeder (P6b integration).
- `.ci/builtin-handler-map.yaml` still lists `youtube_live_ingest` as pending; flip it once this lands + T4.

## Parity evidence

- `tests/parity.rs`: corpus `youtube/live_chat_message.json` + goldens from `test_builtin_youtube_live_ingest.py`
  and `test_receivers_youtube_live_poll.py` fixtures (strip semantics incl. 0x1c-0x1f, flags fail closed, `unknown`
  actor, occurred_at fallback, skipped items). Edge cases were run through the real Python `normalize()` once.
- Shared harness: `PARITY_CONNECTOR_WASM=<built wasm> PLATFORM=youtube make test-connector-parity` -> 8 vectors
  (corpus `live_chat_message` + 7 added: moderator/owner+sponsor flags, truthy non-bool flags, missing optionals,
  unicode text, super-chat text, explicit occurred_at; goldens blessed from the real Python path).
- `core/bundle_executor/tests/youtube_connector.rs`: the REAL compiled component through production
  `ConnectorHost::load` (`scripts/test-connector-youtube.sh`).
- Security regressions (`tests/tools_sender.rs`, `tests/forbidden_chars.rs`, `tests/url_safety*.rs` -- 12/12 URL mutants killed): slot smuggling, control/zero-width/
  bidi/lookalike chars, mutation-checked (9/10 mutants killed; the 10th is the redundant raw-vs-cleaned guard).

## Build / test

```bash
cd bundles/rust/youtube
cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings
cargo test --locked && cargo llvm-cov --locked --fail-under-lines 90
cargo deny check
cargo component build --release --target wasm32-wasip2 --locked
scripts/test-connector-youtube.sh   # repo root: build + real-path executor tests
```

## Operational notes

Logs are PII-free (`op`, `events`, `skipped` counts only). Egress: `www.googleapis.com` (GET/POST). No `kv`, so
no key-charset concerns. Version bump on any change.

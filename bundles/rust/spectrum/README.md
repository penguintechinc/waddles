# waddles.core.service.spectrum -- RSI Spectrum connector (Rust, `connector` world, inbound-only)

gh #486 (re-specs #101). Slice 1 of the Spectrum v3 port: the **component + pure logic + golden parity**.
The legacy Python path (`core/svc_ingest/receivers/spectrum_{poll,org}.py`) stays live and unchanged.

```
host (poll transport, RSI egress allowlist, session credential)
   --RSI {success,data} response or raw change frame--> receiver.on-frame
   --> normalized-event[]  (actor = UUID via identity.lookup; no raw handle / display name in payload)
sender.build-request --> ALWAYS Unsupported (one-way inbound by design, #101)
```

## Parity (Python `normalize()` + `_item_from_mapping`)

| Area | Python | Connector |
|---|---|---|
| lobby msg -> `message`; forum thread/reply -> `thread`/`reply` | yes | identical (`is_reply` now strict: only `true`/`1`, Python used truthy `bool()`) |
| text `.strip()`, id/`plaintext\|body\|subject`/`member.id\|member_id`, epoch -> `isoformat()` | yes | identical (byte-for-byte iso incl. microseconds) |
| oldest-first ordering, skip-and-count items without id/text | yes | identical |
| roster `member_joined/left/roles_changed`, org `org_event_*` | via host diff | normalizer ready; frames arrive as raw change dicts |
| `author_id`/`member_id`/`display_name`/`organizer_id` in payload | present | **removed** (PII boundary); actor is the tokenized UUID |
| anonymous author | actor `"unknown"` | actor `None` |

## Remaining work (NOT in this crate -- host/runtime, see PR description)

1. svc_ingest `PollTransport` is GET + cursor only; Spectrum needs **POST + JSON body + `Cookie`/`X-Rsi-Token` header** (profile-derived, host-constant).
2. **Seen-id dedupe + priming** (no backlog flood) and the **roster/events snapshot store + diff** (Valkey) are stateful and must live host-side (guests are stateless; pinned connectors have no host-call bridge).
3. `SpectrumProfile` + `connector_e2e` against a fake Spectrum API (host-side). DONE in the wiring slice: selector entry (`CONNECTOR_SPECTRUM_ENABLED`, default OFF, suppresses the legacy `spectrum_{poll,org}` receivers) + parity fixtures, helm `connectors.spectrum.*`, hub-api provider `spectrum` (RSI session token sealed at rest, `chat_ingest` only), `core-bundles.yaml` row. Still open: executor `CONNECTOR_PIN_DIGEST_SPECTRUM` field (`core/bundle_executor/src/config.rs`).
4. Identity: `identity.lookup(("spectrum", member_id))` needs the `spectrum` platform registered with the identity backend.

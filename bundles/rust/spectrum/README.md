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

## Host side (slice 2b, `core/svc_ingest`)

| Piece | Where |
|---|---|
| POST + JSON body + `Cookie`/`X-Rsi-Token` | `connector/poll.rs` `PollRequest` (slice 2a) |
| Host-owned page cursor (host writes the integer `page` field; never guest/metadata controlled) | `PollRequest::page_field`, `PollTransport::fetch_page` |
| Seen-id dedupe + priming (no history flood), roster/org-event snapshot store + diff (Valkey, community-scoped numeric keys, ids/fingerprints only) | `connector/spectrum.rs`, `connector/spectrum_org.rs` |
| `SpectrumProfile` (RSI host pinned; tenant = feed's server-derived tenant) | `connector_runtime/profiles/spectrum.rs`, `connector/secrets.rs` `approved_hosts("spectrum")` |
| `identity.lookup` for platform `spectrum` | `connector/host_calls.rs` `CONNECTORS` |
| Real-path proof (real wasm, fake RSI API, runtime tenant park) | `core/connector_e2e/tests/spectrum_profile.rs` |

## Remaining work (NOT in this slice)

1. Selector entry (`CONNECTOR_SPECTRUM_ENABLED`) + boot wiring of the Valkey store (`RuntimeDeps::spectrum`) + legacy-suppression parity fixtures.
2. helm `connectors.spectrum.*`, hub-api provider (`spectrum`, RSI session token credential), `core-bundles.yaml` entry.

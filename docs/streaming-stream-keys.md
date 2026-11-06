# Stream keys and restream destinations (hub-api)

`/api/v1/admin/{community_id}/streams/*` (community admin; tenant middleware first).

| Route | Purpose |
|---|---|
| `GET .../streams` | config (never the key); `config: null` until created (broadcast flag) |
| `POST .../streams` | create config; **one-time** plaintext key in `streamKey` |
| `POST .../streams/key/regenerate` | rotate; old key stops authorizing at once; new key in `streamKey` |
| `GET/POST .../streams/destinations` | list / add restream destination (`waddles.streaming.broadcast` flag) |
| `DELETE .../streams/destinations/{id}` | remove (id is a UUID; foreign id = 404) |
| `PUT .../streams/destinations/{id}/force-cut` | set (`force_cut` bool) or toggle (no body) |
| `GET .../streams/status` | live status from `streaming_sessions` |

## Secrets

- Inbound key: `wbsk_` + 256-bit CSPRNG; only `sha256(key)` is stored
  (`streaming_configs.stream_key_hash`, UNIQUE) plus an 8-char display fingerprint. Create/rotate
  responses carry `Cache-Control: no-store`; no GET returns it; it is never logged.
- Destination URL + key: AES-256-GCM (`CREDENTIAL_ENCRYPTION_KEY`), AAD = `streamdest:{config_id}:{public_id}`.
  Lists show only `scheme://host[:port]`. Destination count capped at 10 per community.
- RTMP destinations accept H.264 only (FLV cannot carry HEVC/AV1); SSRF guard on the URL.
- `STREAMING_INGEST_RTMP_URL` (env) is the public ingest base URL shown to admins.

## svc-streaming contract

- Ingest auth (RTMP, SRT, WHIP and `/internal/streaming/ingest-auth`) hashes the presented key and
  matches `streaming_configs.stream_key_hash` only; `source_url` is never consulted. Keys shorter
  than 16 chars (incl. empty) are refused before any lookup (`core/svc_streaming/src/stream_key.rs`).
  Existing rows without a hash cannot authenticate until a key is minted/rotated via this API.
- Relay egress (`src/egress/relay/guard.rs`) re-resolves each destination at dial time, refuses any
  non-public answer, and pins the validated IP into rtmp/srt URLs (rtmps keeps its hostname for TLS;
  validated, not pinned). Consuming `streaming_destinations` in the data plane is tracked in #1034.

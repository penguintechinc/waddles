# Stream keys and restream destinations (hub-api)

`/api/v1/admin/{community_id}/streams/*` (community admin; tenant middleware first).

| Route | Purpose |
|---|---|
| `GET .../streams` | config (never the key); `config: null` until created |
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

## Follow-up (svc-streaming)

The Rust data plane still authorizes ingest by matching the presented key against plaintext
`streaming_configs.source_url`. It must switch to `stream_key_hash = sha256(presented key)`
(`orchestrator::find_enabled_config`, `api::internal::ingest_auth`, SRT key refresh) before keys
minted by this API authorize a publish. Until then, hub-api-created configs carry `source_url = ''`.

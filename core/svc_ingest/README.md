# svc-ingest (Rust data-plane ingest)

Owns inbound platform connections and HTTP intake; normalizes events and appends them once to the
Valkey stream spine. No database.

## Per-community Twitch sessions (`INGEST_DYNAMIC_CONNECTIONS`)

With the flag ON, svc-ingest runs one Twitch IRC session **per connected bot/channel binding**
instead of a single static `TWITCH_IRC_*` identity.

| Env | Default | Meaning |
|---|---|---|
| `INGEST_DYNAMIC_CONNECTIONS` | `false` | Turns on per-community sessions (chart: `pipeline.rustDataPlane.svcIngest.dynamicConnections.enabled`, alpha only today) |
| `TWITCH_RECONCILE_INTERVAL_SECS` | `60` (alpha: `30`) | Reconcile cadence, bounded 10..3600; a binding change takes effect within one interval (+10% jitter) |
| `TWITCH_RESOLVE_TIMEOUT_SECS` | `10` | Per-call bound on hub-api list/resolve |

**Static `TWITCH_IRC_NICK` / `TWITCH_IRC_CHANNEL` with the flag ON is a FATAL startup error** (never a
silent override) -- the chart never renders them for alpha. Required alongside the flag:
`SERVICE_JWT_TOKEN_ENDPOINT`, a valid `ENVELOPE_BINDING_KEYS` + `ENVELOPE_BINDING_ACTIVE_KID`, a
reachable Valkey (leases + spine), and https hub-api outside alpha. The machine identity needs the
`connections:resolve` + `connections:list` scopes (chart grants them to svc-ingest only).

### Runbook

1. **Connect:** in the webui connect the Twitch bot for a community (OAuth) and name the channel -- this
   creates a provider connection in hub-api.
2. Within <= one reconcile interval a session starts: svc-ingest lists bindings, takes a per-binding
   Valkey lease (one replica owns each session), resolves a fresh token and JOINs the channel.
   Check `svc_ingest_twitch_sessions_live` (gauge) and `svc_ingest_twitch_session_events_total{event="started"}`.
3. **Disconnect/delete** the connection: the next reconcile stops the session and releases its lease
   (`event="stopped"`). A hub outage keeps running sockets open (no start/stop decisions on a failed list).
4. Token refresh/rotation is picked up on the session's next (re)connect; a revoked token ends the session
   as non-retryable until the binding is re-authorised.

Logs and metric labels never carry tokens, channel or bot logins -- only numeric community/connection ids
and counts.

### Tests

| Command | Covers |
|---|---|
| `cargo test --locked` (needs `VALKEY_URL`, a real Valkey; unset = failure, never a skip) | unit + real-socket lifecycle/reconcile tests |
| `make test-svc-ingest-twitch-live` | live e2e: real hub-api + Postgres + Valkey + IRC server; binding created/deleted through the admin API starts/stops a session; prints and gates on counts (docker + helm + cargo; `HUB_PYTHON` = python with `hub_api/requirements-dev.txt` + `libs/flask_core/requirements.txt`) |
| `helm` render: `k8s/helm/waddlebot/tests/test_svc_ingest_twitch_sessions_render.py` | alpha renders the flag + timing envs and no static pair; beta/prod unchanged; egress to hub-api |

## Outbound relay host-pin (`src/outbound_pin.rs`)

The outbound drain never trusts a destination it was handed. `svc_action` pins every
channel-targeted relay op (Twitch `chat.send`; Discord `chat.send`/`chat.delete`) to the
triggering event's own channel and stamps it as `origin_channel`; the drain then refuses any
entry whose `channel` is not exactly that stamp, is not a well-formed id (Twitch login
`^[a-z0-9_]{1,25}$`, non-numeric; Discord snowflake), or has no stamp at all (pre-upgrade
entries are refused loudly, never guessed at).

| Refusal reason | Meaning |
|---|---|
| `channel_not_pinned` | `channel` differs from `origin_channel` |
| `no_origin_pin` | entry has no `origin_channel` |
| `invalid_channel` | malformed id (incl. IRC multi-target `#a,#b`) |
| `unsupported_platform` | no pin policy -> fail closed |

Refusals surface as `SenderError::Rejected`, bump `svc_ingest_outbound_pin_refused_total{platform,op,reason}`
and log a PII-free WARN (platform/op/reason only -- never a channel, id or message text).
Bundles only ever supply message content. Tests: `cargo test --lib outbound` (regression tests named
`*unpinned*`/`*regression_p0*`).

## Per-community Twitch outbound credentials (`src/outbound_creds.rs`)

Design: `docs/superpowers/specs/2026-11-02-per-tenant-bot-credentials.md`. Replaces the single process-wide
Twitch bot identity on the outbound drain.

- `svc_action` stamps the invoke scope's NUMERIC `community_id` on each queued `chat.send` (never bundle input).
- On **every send** the drain resolves that community's own twitch connection via `connection_resolver`
  (binding list -> the community's binding that owns the pinned channel -> `resolve`), asserts the answer is the
  same community/connection/provider/OAuth kind, and uses the token once. The secret is **never cached**
  (`SecretString`, zeroizing, redacted `Debug`); keys are numeric ids, never slug/name.
- Failure is loud and typed, with NO fallback credential (`CredError` -> `SenderError::Rejected`/`Failed`,
  stable reason in the ack): `no_community_scope`, `no_connection`, `channel_not_owned` (incl. a channel
  claimed by another community), `connection_not_usable`, `credential_unavailable` (retryable, `Failed`).
- Telemetry: `svc_ingest_outbound_credential_resolve_total{platform,outcome}`, PII-free logs (community id +
  reason only; never token/login/channel/text); resolver hub-api calls keep their own spans/metrics.
- **Switch / migration:** governed by the existing `waddles.ingest-dynamic-connections` flag (env baseline
  `INGEST_DYNAMIC_CONNECTIONS`, default OFF). ON = per-community (misconfig is a fatal startup error; a set
  `TWITCH_IRC_OAUTH_TOKEN` is ignored with a WARN). OFF = the LEGACY single-identity path
  (`StaticCredentialSource`, needs `TWITCH_IRC_NICK`+`TWITCH_IRC_OAUTH_TOKEN`, else the drain does not start and
  logs why) -- single-tenant dev only; removed when the flag is retired. The two are never mixed per call.
- Known follow-up: each send pages the (non-secret) binding list; a community-filtered hub-api endpoint or a
  short-TTL cache of non-secret binding metadata would cut that cost. Secrets stay uncached regardless.
- Tests: `cargo test -p svc-ingest --lib outbound` (regressions tagged `p6a-t6b`).

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

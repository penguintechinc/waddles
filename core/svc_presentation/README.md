# svc-presentation (Rust)

The Waddles overlay/presentation service: OBS browser-source overlays
(full-screen/media/crawler/music, plus the #458 widget palette --
alert_box/chat/goals/ticker/image), per-community theme/styling config.
Replaces the Python + Quart alpha (`app.py`, `blueprints/`, `services/` in
this same directory) once P2-P9 land the render/live/push/hub routes on
top of this skeleton; both builds coexist in-tree during the transition
(see `src/lib.rs` module doc, same precedent as `core/svc_streaming`
issue #287).

## P1 Scope (this scaffold)

Config, telemetry (tracing + OTel + Prometheus), the database connection,
SeaORM entities for this service's three tables, and the `overlay_auth`
VIEW/PUSH axum guards mounted on route-less sub-routers. **No render,
live-channel, or push handlers exist yet** -- `/overlay/*` 404s today,
honestly, rather than serving a stub. See `src/overlay/router.rs`'s module
doc for the exact extension points later chunks add routes to:

| Chunk | Adds | Extension point |
|---|---|---|
| P2 | `GET /overlay/{community}/{surface}` render | `src/overlay/router.rs::view_guarded_router` |
| P3 | `GET /overlay/{community}/{surface}/live` SSE/websocket | `src/overlay/router.rs::view_guarded_router` |
| P4 | `POST /overlay/{community}/{surface}/push` | `src/overlay/router.rs::push_guarded_router` |

## Ports

| Port | Protocol | Purpose |
|---|---|---|
| 8207 | HTTP | Control plane: `/health`, `/readyz`; overlay-auth-guarded sub-routers (route-less until P2-P4) |
| 9090 | HTTP | Prometheus `/metrics` (secondary scrape surface, separate listener) |

## Environment

| Var | Default | Notes |
|---|---|---|
| `MODULE_PORT` | `8207` | HTTP control-plane port -- matches the Python alpha's existing default |
| `METRICS_PORT` | `9090` | Prometheus port |
| `BIND_ADDR` | `0.0.0.0` | Listener bind address |
| `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER` | `localhost`/`5432`/`waddlebot`/`svc-presentation-rw` | Per-service DB account |
| `DB_PASSWORD` | *(required, env-only)* | Never a CLI flag |
| `CACHE_HOST`/`CACHE_PORT` | `localhost`/`6379` | Reserved for the live overlay fan-out (P3) |
| `CACHE_PASSWORD` | *(optional, env-only)* | |
| `PUSH_JWKS_URL` | `http://hub-api/.well-known/jwks.json` | hub-api's JWKS endpoint -- verifies PUSH credentials |
| `PUSH_AUDIENCE` | `waddlebot-internal` | Expected `aud` claim on a PUSH credential |
| `PUSH_TRUSTED_ISSUER` | `hub-api` | Expected `iss` claim on a PUSH credential |
| `OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | unset | Standard OTLP env config; unset endpoint = tracing-only (no OTLP export attempted) |

All secrets are env-only, never accepted as a CLI flag (`src/config.rs`).
There is no static shared-secret bearer token (replacing the Python
alpha's `PRESENTATION_PUSH_TOKEN`) -- PUSH credentials are hub-api-issued
machine JWTs verified against `PUSH_JWKS_URL` (`overlay_auth`,
`service_auth::JwksTrustBundle`).

## Contracts This Crate Depends On (never re-implemented)

- `core/overlay_schema` -- the `Surface` enum, `OverlayPush` wire shape,
  `OverlayEnvelope` SSE/websocket frame.
- `core/overlay_auth` -- `require_view_credential`/`require_push_credential`
  axum guards; this crate supplies the concrete
  `ViewCredentialStore`/`PushTrustSource` implementations
  (`src/overlay/view_store.rs`, `src/overlay/push_trust.rs`).
- `core/service_auth` -- `JwksTrustBundle`/`TrustBundle` (consumed directly
  by `src/overlay/push_trust.rs`).
- `wit/waddle-bundle/stage.wit` -- the bundle-facing `overlay` WIT
  interface (`world stage-next`); kept in lockstep with `overlay_schema`'s
  `Surface` enum per that crate's own module doc.

## Make Targets

```
make build          # cargo build --all-targets
make test            # cargo test
make lint             # fmt-check + clippy -D warnings
make test-security  # cargo deny check
make coverage        # cargo llvm-cov --fail-under-lines 90
make docker-build    # build Dockerfile.rust (context = core/), tag for localhost:32000
```

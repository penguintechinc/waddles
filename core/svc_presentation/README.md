# svc-presentation (Rust)

The Waddles overlay/presentation service: OBS browser-source overlays
(full-screen/media/crawler/music, plus the #458 widget palette --
alert_box/chat/goals/ticker/image, and the live `caption` surface), per-community
theme/styling config.
Replaces the Python + Quart alpha (`app.py`, `blueprints/`, `services/` in
this same directory) once P2-P9 land the render/live/push/hub routes on
top of this skeleton; both builds coexist in-tree during the transition
(see `src/lib.rs` module doc, same precedent as `core/svc_streaming`
issue #287).

## Live Overlay Pipeline (push -> render -> fan-out -> browser)

The built-in, native-renderer path. (The WASM presentation-host capability
for bundle-authored overlays is a separate, future build -- nothing here
depends on it.)

```text
 action-stage adapter / bundle
        | POST /{overlay_code}/{surface}/push   (PUSH machine JWT)
        v
 PUSH guard: overlay_code --> community_id (communities.overlay_code),
             then overlay_auth::authorize_push --> credential.community_id (verified)
        |
        |  community_id --> communities.tenant_id + presentation_config theme
        |                   (CommunityContextStore, 60s TTL cache)
        v
 OverlayDetokenizer::render_with_metrics
        |   1. resolve every {user:<uuid>} / user field -> hub-api display names
        |      (one batched, tenant-scoped ResolveDisplayNames call)
        |   2. per-surface renderer: HTML-escape every free-text field,
        |      substitute names, validate image_url
        v
 RenderedFrame  (flat JSON, tagged by content_type)  -- nothing raw survives
        |
        v
 AppState::frame_hub : PresentationHub<RenderedFrame>
        |  per (community, surface) broadcast channel
        v
 GET .../live (SSE)  /  GET .../live/ws (websocket)   (VIEW key)
        |
        v
 GET /{overlay_code}/{surface}?key=...   (the OBS browser-source page)
        EventSource -> renders the frame as escaped HTML
```

- **Render before publish.** The push handler never forwards the caller's
  body. `AppState::frame_hub` only ever carries `RenderedFrame`s, and the
  `/live` routes only subscribe to it, so a browser cannot receive a raw
  `{user:<uuid>}`, a user UUID or unescaped markup through this service. The
  type system enforces it: the hub the live routes read is
  `PresentationHub<RenderedFrame>`, not `PresentationHub<OverlayPush>`.
  (`AppState::hub`, the raw `OverlayPush` hub, is owned by the caption
  pipeline alone.)
- **Tenant comes from the verified community.** A PUSH JWT proves only
  `community_id`; the tenant hub-api scopes name resolution by is derived from
  it (`communities.tenant_id`), never from a request body or path.
- **Failure modes** (all loud, none leak):

  | Condition | Result |
  |---|---|
  | Renderer rejects the push (empty, missing field, bad `image_url`) | `400` to the pusher, nothing published, `outcome="rejected"` |
  | Unknown community | `404`, nothing published |
  | hub-api down / circuit open / timeout | Push still published; every user renders `Unknown User`; `ERROR` logged; `detok ... outcome="unavailable"` |
  | Community lookup DB error | Push still published with no tenant (all `Unknown User`) and the default theme; `ERROR` logged; `pushes_total outcome="degraded"` |
  | `image` / `caption` posted to the generic route | `400` (they own dedicated routes; reaching the generic one is a routing regression) |

### Overlay URLs: the unguessable overlay code

Overlay URLs are `/{overlay_code}/{surface}[/live|/live/ws|/push]`, where
`overlay_code` is the community's **random 64-bit value as 16 lowercase hex
characters** (`communities.overlay_code`, alembic
`0056_communities_overlay_code`; CSPRNG-generated, `UNIQUE NOT NULL`, new
communities get one from the column `DEFAULT`). It replaced the sequential
integer id (`/overlay/{community_id}/...`), which made every overlay trivially
enumerable.

```text
https://<host:port>/a1b2c3d4e5f60718/chat?key=<VIEW key>
                    \______________/ \__/ \________/
                     overlay_code   surface  VIEW key (still required)
```

- **The code is only a public path handle.** The guards
  (`src/overlay/router.rs`) resolve it to the real `community_id` once, at the
  edge (`src/overlay/code.rs`, read-only `communities` lookup); every
  credential, scope, hub channel, tenant lookup and metric stays keyed by that
  id. The code never appears in logs, spans or metric labels, and the
  integer id never appears in any URL or response (`ConnectedFrame.community`
  and the push responses carry the code).
- **Defense in depth, not a replacement.** The VIEW `?key=` (browser routes) and
  the PUSH machine JWT scoped to the resolved community are enforced exactly as
  before; an unguessable URL that leaks still needs them.
- **Check order** (any failure stops the request): resolve the code (`404` if
  it is not exactly `[0-9a-f]{16}` or names no community -- an old integer URL
  such as `/42/chat` or `/overlay/42/chat` lands here, as does a reserved word
  like `/health/chat`) -> VIEW `?key=` present (`400`) -> credential valid *for
  that community* (`401`/`403`). A resolver/database failure is a `500`.
- **The route parameter cannot be regex-constrained in axum**, so the first
  segment is a plain capture and the guard is the constraint. `/health` and
  `/readyz` are literal one-segment routes that win on specificity;
  `/overlay/captions/{key}` and `/ws/captions/{id}` have literal first
  segments; none of those words is 16 hex characters, so the capture can never
  serve them (`tests/routing.rs`).
- **Cache and rotation.** Found mappings are cached `OVERLAY_CODE_CACHE_TTL_SECONDS`
  (default 30s; absent codes at most 5s; bounded, with random-code probing
  unable to evict real mappings). That TTL is also the rotation latency: when a
  code is regenerated (follow-up endpoint) the old URL stops resolving on each
  replica within one TTL; an already-open stream stays up until it reconnects.
  Metrics: `svc_presentation_overlay_code_lookups_total{outcome}` and
  `svc_presentation_overlay_code_lookup_duration_seconds`.

### Browser pages

`GET /{overlay_code}/{surface}?key=<VIEW key>` serves one static,
self-contained page (`src/http/templates/overlay.html`) that opens an
`EventSource` on the sibling `.../live` URL and renders the stream.
Optional query params: `max` (chat lines), `ttl` (chat line seconds),
`duration` (alert ms), `speed` (crawler seconds).

| Surface | Browser page | Rendering |
|---|---|---|
| `alert_box` | yes | queued cards: type, resolved name, amount, message |
| `chat` | yes | rolling lines: platform, resolved author, text |
| `goals` | yes | label, `current / target unit`, progress bar |
| `ticker` | yes | static bottom tape |
| `crawler` | yes | scrolling bottom tape |
| `full_screen` | yes | title / body / validated `image_url`; `clear` hides |
| `media` | yes | same, corner card |
| `music` | no (404) | poll-driven Music Station; no push-derived text |
| `image` | no (404) | fail-loud stub pending the P9 image surface |
| `caption` | no (404) | has its own page: `/overlay/captions/{key}` |

Page hardening: the document is static (no overlay code, key or pushed text is
interpolated); its one inline script is allowed by a **`sha256-` CSP hash**
(no `'unsafe-inline'` scripts); the script has a single `innerHTML` sink fed
only by `safeHtml()` (which re-escapes any raw `< > " '`, so a server
regression degrades to visible text, never markup); `image_url` reaches
`img.src` only after an `http(s)` check; `Cache-Control: no-store` and
`Referrer-Policy: no-referrer` because the URL carries the VIEW key.
`tests/overlay_render.rs` drives the real router + guards end to end, and
`src/http/overlay_page.rs`'s tests pin the sink discipline.

### Detokenization at startup

Detokenization is **on by default and fail-loud**: with `HUB_API_GRPC_ENDPOINT`
or `SERVICE_JWT_TOKEN_ENDPOINT` unset, or hub-api unreachable at startup, the
process exits non-zero rather than serving `Unknown User` to every viewer.
`PII_DETOKENIZATION_ENABLED=false` is the explicit, loudly logged operator
escape hatch (dev / air-gapped / alpha with no hub-api gRPC yet): no hub client
is created, no name is ever resolved, every user renders `Unknown User`, and
output stays escaped and leak-free. Only the exact value `false` disables it.

### Required database grants

Besides this service's own tables, the service reads (read-only)
`communities (id, tenant_id)` -- the community -> tenant mapping --
`communities (id, overlay_code)` -- the overlay code -> community lookup every
overlay route performs (alembic `0056`; the migration does not issue the GRANT
because per-service role names are provisioned outside Alembic) -- and
`presentation_config` (theme). A deployment that moves to a dedicated
`svc-presentation-rw` role must grant `SELECT` on `communities`
(`id, tenant_id, overlay_code`) and `presentation_config`. A role missing the
`overlay_code` grant fails every overlay request with a `500` (logged
`overlay code lookup failed`), never a silent 404.

## Scope history

P1 delivered config, telemetry, the database connection, the SeaORM
entities and the `overlay_auth` guards; P2-P4 added the per-surface
renderers, the live SSE/websocket channel and the push route; P6/P9 the image
upload/render path. The live pipeline above is what ties them together.

## Captions (port of `core/browser_source_core_module`)

The closed-caption overlay -- the Python `browser_source_core_module`'s
`/overlay/captions/<key>` page, `/ws/captions/<community_id>` websocket and
`POST /api/v1/internal/captions` ingest -- is ported here
(`src/http/captions.rs`, `src/overlay/render/caption.rs`,
`src/overlay/caption_store.rs`, migration `102_caption_events_rust_port.sql`).
It ships **dark**: every caption route is gated on the PostHog flag
`waddles.core.overlay-captions` (OFF by default), so the Python path stays
live until the parity cutover.

| Route | Auth | Notes |
|---|---|---|
| `GET /overlay/captions/{key}?community_id=N` | VIEW key (path) validated *for that community* | Same URL as Python -- existing OBS sources keep working. Static page, no data embedded |
| `GET /ws/captions/{community_id}?key=` | VIEW key (`?key=`) | Same URL as Python. Replays the last 5 min / 10 captions, then streams live |
| `POST /{overlay_code}/caption/push` | PUSH machine JWT scoped to the community | **Replaces** `POST /api/v1/internal/captions`: no static `X-Service-Key`, community from the credential never the body, body is `OverlayPush{caption: ...}` |

PII: pushes carry a tokenized `user` UUID plus an already-detokenized
`display_name`; only the UUID is stored (`caption_events.user_ref`), the
display name is forwarded to live viewers and never persisted or logged, so
replayed history has no attribution name. Retention is 7 days (hourly purge,
flag-gated).

**Cutover checklist (parity work, not done here):** repoint ingress for
`/overlay/captions/*` and `/ws/captions/*` and every caller of the legacy
`/api/v1/internal/captions` / gRPC `SendCaption` at this service (callers must
now send a PUSH JWT and the `caption` payload); flip the flag; then delete the
Python caption path and drop the deprecated `caption_events.username` column.

## Ports

| Port | Protocol | Purpose |
|---|---|---|
| 8207 | HTTP | Control plane: `/health`, `/readyz`; overlay page, `.../live`, `.../live/ws`, `.../push`; image upload; captions |
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
| `HUB_API_GRPC_ENDPOINT` | *(required unless detok disabled)* | hub-api internal gRPC, e.g. `https://waddlebot-hub-api-v3:50204` -- overlay display-name resolution |
| `SERVICE_JWT_TOKEN_ENDPOINT` | *(required unless detok disabled)* | hub-api machine-JWT bootstrap (`POST /internal/service-token`) |
| `SERVICE_JWT_SA_TOKEN_PATH` | `/var/run/secrets/kubernetes.io/serviceaccount/token` | Projected SA token the bootstrap reads |
| `HUB_API_GRPC_CA_FILE` | *(empty = system roots)* | PEM CA that signed hub-api's gRPC cert |
| `PII_DETOKENIZATION_ENABLED` | *(unset = on)* | `false` = explicit operator escape hatch (see Detokenization at startup) |
| `OVERLAY_CODE_CACHE_TTL_SECONDS` | `30` | Overlay code -> community cache TTL; also the worst-case delay before a rotated code stops resolving. `0` = no cache |
| `OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | unset | Standard OTLP env config; unset endpoint = tracing-only (no OTLP export attempted) |

All secrets are env-only, never accepted as a CLI flag (`src/config.rs`).
There is no static shared-secret bearer token (replacing the Python
alpha's `PRESENTATION_PUSH_TOKEN`) -- PUSH credentials are hub-api-issued
machine JWTs verified against `PUSH_JWKS_URL` (`overlay_auth`,
`service_auth::JwksTrustBundle`).

## Contracts This Crate Depends On (never re-implemented)

- `core/overlay_schema` -- the `Surface` enum, `OverlayPush` wire shape,
  `OverlayEnvelope` SSE/websocket frame.
- `core/overlay_auth` -- `validate_view_token`/`authorize_push` (the VIEW/PUSH
  checks `src/overlay/router.rs`'s code-resolving guards apply once the
  overlay code is resolved to a community; its numeric-path
  `require_view_credential`/`require_push_credential` guards are not used
  here); this crate supplies the concrete
  `ViewCredentialStore`/`PushTrustSource` implementations
  (`src/overlay/view_store.rs`, `src/overlay/push_trust.rs`).
- `core/service_auth` -- `JwksTrustBundle`/`TrustBundle` (consumed directly
  by `src/overlay/push_trust.rs`).
- `wit/waddle-bundle/stage.wit` -- the bundle-facing `overlay` WIT
  interface (`world stage-next`); kept in lockstep with `overlay_schema`'s
  `Surface` enum per that crate's own module doc.

## Overlay Output Safety: Detokenization + HTML Escaping

Overlay surfaces render only hub-api-resolved display names, never a raw
PII value, UUID or `{user:<token>}` placeholder, and every free-text
field is HTML-escaped exactly once (`src/overlay/detok.rs`,
`src/overlay/render/shared.rs`). Bundles reference users by tokenized UUID
only (`rules/critical-rules.md` PII Tokenization); this is the overlay
sink of the same outbound pass `core/egress_detokenizer` runs for chat.

| Step | Where | Behavior |
|---|---|---|
| Resolve (async, once per push) | `OverlayDetokenizer::resolve` | Collects every canonical-UUID user reference (`chat_message.user`, `alert.user`, `{user:<uuid>}` in text), one batched, tenant-scoped `ResolveDisplayNames` call via `hub_client` (cap 100, 3s timeout) |
| Render (sync, per surface) | `render/shared.rs::sanitize_text` | HTML-escape the bundle text, then substitute each placeholder with its resolved name (`Sink::Overlay` escapes the name) or `Unknown User` |

- **Surfaces sanitized:** `alert_box`, `chat`, `crawler`, `full_screen`,
  `goals`, `media`, `ticker`. `music` carries no push-derived text (asserted
  by test); `image` is still the fail-loud stub.
- **Fail-safe-empty:** hub-api unreachable, circuit open, timeout, empty
  tenant, or an unknown UUID all render `Unknown User` (logged `ERROR`/`WARN`,
  counts only) -- the push still renders, nothing leaks, nothing is dropped.
- **The renderer is the last line of defense:** names reach the (signature-
  frozen, synchronous) renderers through `detok::with_names`; rendered with no
  resolved-name scope, every token becomes `Unknown User` and the caller's
  own `display_name` is never trusted. `chat`/`alert_box` `display_name` is
  always the resolved name for `user`; the `user` UUID is no longer emitted.
- **`image_url`** is validated, not escaped (the client assigns it to
  `img.src`, where `&amp;` would corrupt query strings): plain `http(s)://`,
  no whitespace/quote/bracket/control characters, no embedded `{user:` token --
  otherwise a loud `InvalidField`.
- **Tenant scoping:** `DetokScope::tenant_id` must come from the validated
  credential (never a request body/path); hub-api scopes the lookup by it.
  Resolution is tenant-scoped on the wire; `community_id` is log/trace context.
- **No PII in logs/metrics:** nothing logs a UUID, token, name or push text
  (`detokenize_resolving` is deliberately not used -- it logs unresolved
  token values). `ResolvedNames`'s `Debug` prints the entry count only.
- **Metrics:** `svc_presentation_overlay_detok_resolutions_total{outcome}`
  (`ok`/`partial`/`unavailable`/`disabled`), `..._detok_resolve_duration_seconds`,
  `..._detok_unresolved_tokens_total` (`register_detok_metrics`); trace span
  `overlay.detok.resolve`. The push route adds
  `svc_presentation_overlay_pushes_total{surface,outcome}`
  (`published`/`rejected`/`degraded`), `..._push_duration_seconds{surface}`,
  and the per-surface `..._renders_total`/`..._render_duration_seconds`.
- **Clients must treat these strings as HTML**, not `textContent`: the
  legacy Python `render.py` page used `textContent` and would show `&lt;`
  literally for escaped output. The shipped browser page does (see Browser
  pages); the caption page is the one deliberate `textContent`-only client.

### Known gaps

- `caption` is not detokenization-aware: its renderer emits unescaped text for
  its `textContent`-only page and still carries the `user` UUID and the
  caller's `display_name`. It never reaches `frame_hub`, so the generic
  live routes cannot leak it, but it needs a plain-text resolve-only variant.
- Browser pages for `music` and `image`, and the overlay designer UI, are not
  built here.
- The WASM presentation host (bundle-authored overlay widgets) is a separate
  future build; this README describes the built-in native-renderer path only.

`OverlayDetokenizer::render`/`render_with_metrics` are the entry points a
push route calls instead of `render::render` directly. **Not yet wired:** the
P4 `POST .../push` handler still publishes the raw `OverlayPush` to the hub
(it never calls the renderer); hooking it up needs an `AppState` field
(`Arc<OverlayDetokenizer>`, built from a `hub_client::HubClient` -- see
`core/svc_action/src/lib.rs::build_hub_client` for the env/mTLS shape) and
is a follow-up, not part of this chunk.

## Make Targets

```
make build          # cargo build --all-targets
make test            # cargo test
make lint             # fmt-check + clippy -D warnings
make test-security  # cargo deny check
make coverage        # cargo llvm-cov --fail-under-lines 90
make docker-build    # build Dockerfile.rust (context = core/), tag for localhost:32000
```

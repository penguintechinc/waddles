# Bundle state surfaces and the composite overlay scene (bundle -> presentation)

Generic, safe path for an app bundle to get its state rendered by svc-presentation overlays and the
webui. Replaces "write overlay-facing state to kv and let something poll it".

Contract: `bundles/contracts/surfaces.v1.json` (mirrored byte-for-byte at
`core/svc_presentation/src/surfaces/surfaces.v1.json`; a unit test fails on drift).

```
bundle --overlay.publish-surface(surface_id, payload_json)--> executor
  --host-call db/"surface.publish"--> stage (derives community + app id from the invocation scope;
  bundle may publish only to surfaces under its own `<app_id>.` namespace or an explicit grant)
  --bundle_host_surface::SurfaceHost--> gRPC (the only in-cluster transport)
  waddles.presentation.internal.v1.SurfaceService/Publish
  (hub-api-minted JWT bound to that one community, verified by svc-presentation first)
      size cap -> rate limit -> parse -> sanitize -> JSON-Schema validate
      -> replay cache (replay surfaces) -> fan-out
  <-- GET /surfaces/{overlay_code}/live?surfaces=a,b&key=<VIEW key>   (SSE, read-only, browser)
  <-- GET /{overlay_code}/scene/live?key=<VIEW key>                  (SSE for the scene's widgets)
```

There is deliberately **no HTTP publish route**: in-cluster service-to-service traffic is gRPC only
(`proto/waddles/presentation/internal/v1/surface.proto`). The browser-facing viewer routes stay HTTP
behind the community's VIEW key.

## Surfaces (v1)

| id | consumer | replay | cap | rate/min |
|---|---|---|---|---|
| `waddles.core.example.music.now_playing` | Music Station card | yes | 4 KiB | 120 |
| `waddles.core.example.music.queue` (<=50 tracks) | Music Station list | yes | 16 KiB | 120 |
| `live.channels` | who's-live widget | yes | 16 KiB | 30 |
| `clip.alert` | clip popup (event) | no | 2 KiB | 20 |
| `alert` | generic alert; also bridged into `alert_box` | no | 2 KiB | 60 |

Surface ids a bundle owns are namespaced under its app id (`<app_id>.<name>`); hub-api enforces the
same rule on `stages.presentation.owned_surfaces` at upload, and the stage host authorizes a publish
on the same prefix, so a bundle can never publish into another bundle's namespace.

## Composite scene and widgets

The Overlay Designer (hub-api, alembic 0113) places widgets on one transparent canvas served at
`GET /{overlay_code}/scene` (layout: `/scene/layout`, frames: `/scene/live`). The canvas comes ONLY
from `presentation_config.canvas_resolution_code` (1 = 1080p, 2 = 2k, 3 = 4k); width/height are
derived. At most 128 components per scene (aligned with hub-api).

Widgets are keyed `(bundle_app_id, widget_id)`. `POST /{overlay_code}/widget/{bundle_app_id}/{widget_id}/push`
(PUSH JWT) is refused unless the widget's bundle is enabled for the community AND the widget is
registered (or placed) there. A push lands on the widget's OWN channel: a `surface` widget goes
through the contract pipeline to its declared surface; every other kind to the private hub key
`widget/<bundle_app_id>/<widget_id>`. It never touches a shared surface, so one widget cannot
overwrite or leak into another. The pushed `output_kind` must equal the registered one.

Sanitized-HTML widgets pass through `ammonia` (html5ever): closed tag allowlist, no scripts/iframes/
forms/handlers/`id`/`href`, `https` image sources only, allowlisted inline-style declarations only.

Scene and widget pages are served with a hash-based CSP: `default-src 'none'`, scripts by hash only,
`connect-src 'self'`, and `img-src 'self'` plus an explicit https origin allowlist (built-in artwork
CDNs, the image bucket origin, and `WIDGET_IMG_SRC_ALLOWLIST`) -- never `http:`, `data:` or a bare
`https:`.

## Security properties

* The community is the one the verified credential names (PUSH JWT scope
  `presentation.overlay:push:<id>`; VIEW key via `overlay_view_credentials`). Hub, replay cache,
  limiter and SSE are all keyed `(community_id, surface-or-widget key)`.
* Tenant is derived from the community's row for every scene query -- never from a request.
* Viewers hold a VIEW key: read-only. SSE keys are chosen server-side from verified rows.
* Structured data only: `additionalProperties: false`, https-only URLs, control characters rejected,
  strings are plain text; the pages write strings with `textContent` (one sanitized-HTML sink).
* PII-free: schemas carry no user fields; any `{user:<token>}` becomes `Unknown User`. Logs carry ids,
  codes and counts only.
* Errors are loud: `invalid` (JSON-pointer + rule, never values), `too_large`, `rate_limited`
  (+`retry-after-ms`), `not_granted`, `denied`, `unavailable`, `not_implemented`.

## Operating it

| Env | Default | Purpose |
|---|---|---|
| `SURFACE_GRPC_PORT` | `50051` | in-cluster `SurfaceService` listener (on `BIND_ADDR`) |
| `SURFACE_GRPC_TLS_CERT_FILE` / `_KEY_FILE` | empty | ECC P-256 server identity; REQUIRED unless `BIND_ADDR` is loopback (startup fails otherwise) |
| `WIDGET_IMG_SRC_ALLOWLIST` | empty | extra `https://host[:port]` image origins (invalid entries fail startup) |

Metrics: `svc_presentation_surface_publishes_total{surface,outcome}`,
`svc_presentation_surface_replays_total{surface}` (widget channels fold into the label `widget`).

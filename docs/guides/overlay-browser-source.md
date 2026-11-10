# Overlay Browser Sources (svc-presentation)

How a community's OBS (or any browser) overlay is wired, and why nothing raw
can reach it. Service details: `core/svc_presentation/README.md`.

## Add an overlay to OBS

1. Get the community's overlay code and VIEW key (hub-api overlay settings).
2. Add a **Browser Source** with the URL

   ```text
   https://<svc-presentation host:port>/<overlay_code>/<surface>?key=<VIEW key>
   ```

   `<overlay_code>` is a random 16-character hex string unique to the
   community (for example `a1b2c3d4e5f60718`) -- not the community's numeric
   id, which no longer works in overlay URLs.

3. Set width/height to the canvas; leave "Shutdown source when not visible"
   off so the live connection stays up. The page background is transparent.

| `<surface>` | Shows | Optional query params |
|---|---|---|
| `alert_box` | queued follow/sub/cheer/... cards | `duration` (ms per alert, 1000-60000) |
| `chat` | rolling chat lines | `max` (lines, 1-50), `ttl` (seconds a line stays, 3-600) |
| `goals` | goal label, `current / target`, progress bar | |
| `ticker` | static bottom tape | |
| `crawler` | scrolling bottom tape | `speed` (seconds per scroll, 5-300) |
| `full_screen` | title / body / image, full canvas | |
| `media` | title / body / image, corner card | |

`music`, `image` and `caption` have no page at this URL (`caption` keeps its
own `/overlay/captions/{key}` page). Both the overlay code and the key are in
the URL: treat the whole URL like a password. The code makes the URL
unguessable (overlays can no longer be found by counting community ids); the
key is still required on top of it. If a URL leaks, rotate the key from
hub-api; regenerating the overlay code (to invalidate the old path) is a
tracked follow-up.

## Pushing to an overlay

Action-stage adapters `POST /<overlay_code>/<surface>/push` with a
hub-api-issued PUSH machine JWT scoped to that community (the JWT is still
scoped by community id; the URL carries the community's overlay code) and an
`overlay_schema::OverlayPush` body. Reference users by tokenized UUID only:
write `{user:<uuid>}` in text, or set `chat_message.user` / `alert.user`.

**svc-action's built-in handlers** (`core/svc_action/services/overlay_push.py`, used by
`!vso`/`twitch_shoutout_action`) do this for you. Both inputs are resolved server-side, never from
chat or bundle input: the overlay code is read from `communities.overlay_code` for the envelope's
own community, and the PUSH JWT is minted by hub-api (gRPC `ServiceAuthService.MintServiceToken`, scope
`presentation.overlay:push` + `community_id`, stamped as `presentation.overlay:push:<id>`, 5 min,
cached per community) from the pod's projected ServiceAccount token. An unset `PRESENTATION_URL`,
a missing/malformed overlay code, a refused token, or a non-2xx push raises loudly -- there is no
fallback to the retired `/overlay/<id>/<surface>/push` route or the old
`PRESENTATION_PUSH_TOKEN` bearer. The all-in-one `overlayUrl` hub-api returns is
`<base>/<overlay_code>/alert_box?key=<VIEW key>` (`null` only on a pre-0057 database).

## Which runtime serves these routes

The Rust svc-presentation (`templates/svc-presentation-rust.yaml`) is the only presentation
runtime in every environment: the Python `templates/svc-presentation.yaml` was removed and
`presentation.rust.enabled` defaults to `true` in base values (inherited by beta/gamma/production).
With it on, the chart also grants svc-action the `presentation.overlay:push` scope and an egress
policy to svc-presentation (`waddlebot.svcActionOverlayPush`); setting it `false` renders no
presentation workload and none of that.

## What the viewer is guaranteed

- A user reference renders as the display name hub-api resolves for the
  community's own tenant, or `Unknown User` -- never the UUID or the
  placeholder, never a caller-supplied `display_name`.
- Every free-text field is HTML-escaped once; a bundle cannot inject markup or
  script. `image_url` must be plain `http(s)://` or the push is a `400`.
- If hub-api is down the overlay keeps working and shows `Unknown User`
  (an `ERROR` is logged and
  `svc_presentation_overlay_detok_resolutions_total{outcome="unavailable"}`
  counts it). A misconfigured service refuses to start instead.
- A rejected push (`400`) publishes nothing.

Alpha note: `pipeline.piiTokenization.enabled: false` renders
`PII_DETOKENIZATION_ENABLED=false`, so the overlay never calls hub-api and
shows `Unknown User` for every user reference (plain text is unaffected).

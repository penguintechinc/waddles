# Overlay Browser Sources (svc-presentation)

How a community's OBS (or any browser) overlay is wired, and why nothing raw
can reach it. Service details: `core/svc_presentation/README.md`.

## Add an overlay to OBS

1. Get the community's VIEW key (hub-api overlay settings).
2. Add a **Browser Source** with the URL

   ```text
   https://<svc-presentation host>/overlay/<community_id>/<surface>?key=<VIEW key>
   ```

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
own `/overlay/captions/{key}` page). The key is in the URL: treat it like a
password and rotate it from hub-api if it leaks.

## Pushing to an overlay

Action-stage adapters `POST /overlay/<community_id>/<surface>/push` with a
hub-api-issued PUSH machine JWT scoped to that community and an
`overlay_schema::OverlayPush` body. Reference users by tokenized UUID only:
write `{user:<uuid>}` in text, or set `chat_message.user` / `alert.user`.

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

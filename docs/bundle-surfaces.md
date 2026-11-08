# Bundle state surfaces (bundle -> presentation)

Generic, safe path for an app bundle to get its state rendered by svc-presentation
overlays (Music Station, who's-live widget, clip alerts) and the webui. Replaces
"write overlay-facing state to kv and let something poll it".

Contract: `bundles/contracts/surfaces.v1.json` (mirrored byte-for-byte at
`core/svc_presentation/src/surfaces/surfaces.v1.json`; a unit test fails on drift).

```
bundle --overlay.publish-surface(surface_id, payload_json)--> executor
  --host-call db/"surface.publish"--> stage (derives community from scope; checks
  manifest grant overlay.surface.<id>) --bundle_host_surface::SurfaceHost-->
  POST svc-presentation /internal/surfaces/{community}/{surface}/publish
  (hub-api-minted PUSH JWT bound to that community)
      size cap -> rate limit -> parse -> sanitize -> JSON-Schema validate
      -> replay cache (replay surfaces) -> fan-out
  <-- GET /surfaces/{overlay_code}/live?surfaces=a,b&key=<VIEW key> (SSE, read-only)
  <-- GET /surfaces/{overlay_code}/view?surfaces=...  (static page, textContent only)
```

## Surfaces (v1)

| id | consumer | replay | cap | rate/min |
|---|---|---|---|---|
| `music.now_playing` | Music Station card | yes | 4 KiB | 120 |
| `music.queue` (<=50 tracks) | Music Station list | yes | 16 KiB | 120 |
| `live.channels` | who's-live widget | yes | 16 KiB | 30 |
| `clip.alert` | clip popup (event) | no | 2 KiB | 20 |
| `alert` | generic alert; also bridged into `alert_box` | no | 2 KiB | 60 |

## Security properties

* Community comes only from the verified credential (PUSH JWT scope
  `presentation.overlay:push:<id>`; VIEW key via `overlay_view_credentials`) -- never from the body.
  Hub, replay cache, limiter and SSE are all keyed `(community_id, surface)`.
* Viewers hold a VIEW key: read-only, cannot publish. The view key is community-scoped; the
  surface set is the closed contract list (an unknown id is 404). Per-surface viewer scoping
  is not implemented -- every surface is PII-free by schema.
* Structured data only: `additionalProperties: false`, https-only URLs, no markup accepted as
  a field type; control characters rejected; strings are plain text. The page writes every
  string with `textContent` (a test bans `innerHTML`/`eval`/etc.), CSP `sha256-` hash, no inline
  script allowance. Consumers (React) must render as text too.
* PII-free: schemas carry no user fields; any `{user:<token>}` in a string becomes `Unknown User`.
* Rejected publishes (schema/size) still consume rate-limit tokens. Replay cache is bounded
  (4096 entries, 6 h TTL, evicts oldest).
* Errors are loud: `invalid` (with JSON-pointer + rule, never values), `too_large`,
  `rate_limited` (+`retry-after-ms`), `not_granted`, `denied`, `unavailable`, `not_implemented`.

## Manifest

A bundle declares each surface it publishes as a permission `overlay.surface.<surface-id>`
with a justification (e.g. dj: `overlay.surface.music.now_playing`, `overlay.surface.music.queue`).
`bundle_host_surface::SurfacePolicy::from_permissions` turns the granted set into the allow-list.

## Switching existing bundles from kv state to `publish-surface`

(Exact per-PR changes; do not edit those PRs from here.)

**dj (PR #1122, `bundles/rust/dj`)**
* `src/model.rs`/`engine.rs`: keep `PublicState`; after each state change call
  `publish-surface("music.now_playing", {"now_playing": state.now_playing, "updated_ms": ...})` and
  `publish-surface("music.queue", {"queue": state.queue (truncate to 50), "updated_ms": ...})`.
  `PublicTrack` already matches the schema (`id`, `provider`, `track_id`, `title`, `artist`,
  `duration_s`, `url` https-or-null); drop non-https `url` to `null` first.
* `src/host.rs`: add `fn publish_surface(&self, id: &str, payload: &Value) -> Result<(), String>`
  to the `Host` trait (WASM impl calls the new import); remove the `KEY_PUBLIC` (`dj.public_state`) kv write.
* `bundle.yaml` + `hub-manifest.yaml`: add the two `overlay.surface.*` permissions.
* Publish failures: log (id/code only) and continue -- never fail the chat command on overlay errors;
  `rate_limited` -> skip, the next state change republishes.

**live-now (PR #1119, `bundles/rust/live-now`)**
* `src/logic.rs`: after `snapshot_cached` refreshes, map `Snapshot` -> `live.channels`
  `{live, viewer_count, streams:[{platform, channel, live, since, viewer_count, title, category}], as_of_ms}`
  (drop `channel_ref`, `linked`, `errors`; max 25 streams) and publish; keep the `snapshot` kv key only as the
  bundle-private cache for `!live` (it is no longer overlay-facing).
* Manifests: add `overlay.surface.live.channels`.

**twitch-clips (PR #1113)**
* `src/logic.rs`: when `twitch.clip.get` returns `ready`, publish `clip.alert`
  `{clip_id, url (https twitch only), title, created_ms}` right before relaying the chat message.
* Manifests: add `overlay.surface.clip.alert`.

**ai-teammates (PR #1117)** -- no overlay state today; nothing to switch. If it later wants
an alert, publish `alert` (`alert_type: "info"`).

## Needs from host seams (S0) -- files owned elsewhere

1. `core/bundle_capability_gate/src/permission.rs`: a permission family accepting the dynamic id
   `overlay.surface.<surface-id>` (id grammar in `bundle_host_surface::valid_surface_id`), resource =
   `AppScopedResource::Overlay` (community server-derived), plus an entry in `platform_table_allowlist.json`
   equivalent / manifest-permission checker so `check-bundle-manifest-permissions` accepts it.
2. `core/svc_action/src/capabilities.rs` (and `core/svc_process/src/capabilities.rs` if a process-stage
   bundle publishes): dispatch arm
   `CapabilityKind::Db if call.op.starts_with("surface.publish")` -> authorize the granted
   `overlay.surface.<id>` for `args.surface_id`, build `SurfacePolicy::from_permissions(granted)`, call
   `SurfaceHost::publish(&policy, scope.community_id, surface_id, payload_json)`, and return the
   executor-expected error body `{code: err.code(), message: err.to_string()}`. Until this lands every
   publish fails loud with the stage's `not_implemented`.
3. Stage boot wiring: `SurfaceHost::new(PRESENTATION_URL, Arc<BoundMachineJwtClient::with_timeouts(
   SERVICE_JWT_TOKEN_ENDPOINT, sa_token_path, "presentation.overlay:push", timeouts)>)`.
4. hub-api: a webui SSE proxy (`GET /api/v1/communities/{id}/surfaces/stream`) that authenticates the
   user, looks up the community VIEW key server-side and proxies `GET /surfaces/{code}/live`
   (the browser never sees the VIEW key). Not built here.

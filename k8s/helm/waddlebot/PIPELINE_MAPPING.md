# v3.0.x Task 0.5 — 31 → 7 Container Mapping (SKELETON)

Denominator (`grep -rl "kind: Deployment" k8s/helm/waddlebot/templates/ | wc -l`): **31**.

This chart now contains **both** the 31 legacy per-module Deployments **and** the 7 new
pipeline Deployments (historical: `svc-ingest.yaml`, `svc-process.yaml`, `svc-action.yaml` -- since removed in favor of the Rust `svc-*-rust.yaml`;
`svc-core.yaml`, `hub-api.yaml`, `hub-webui.yaml`, `svc-rtc.yaml`). They coexist — the 31 are
**not deleted** by this task. Deleting them and wiring the new containers' app-level Python
Module-bundle imports is the remaining build (see bottom of this file).

## 26 app/module Deployments → 6 pipeline/support containers

| # | Legacy template | New container | Module (owner) |
|---|---|---|---|
| 1 | `collectors/discord.yaml` | `svc-ingest` | Bot |
| 2 | `collectors/googlechat.yaml` | `svc-ingest` | Bot |
| 3 | `collectors/kick.yaml` | `svc-ingest` | Bot |
| 4 | `collectors/mattermost.yaml` | `svc-ingest` | Bot |
| 5 | `collectors/slack.yaml` | `svc-ingest` | Bot |
| 6 | `collectors/teams.yaml` | `svc-ingest` | Bot |
| 7 | `collectors/twitch.yaml` | `svc-ingest` | Bot |
| 8 | `collectors/youtube-live.yaml` | `svc-ingest` | Bot |
| 9 | `pushing/trigger-webhooks.yaml` | `svc-ingest` | Bot (generic inbound webhooks) |
| 10 | `pushing/trigger-streaming.yaml` | `svc-ingest` | Bot |
| 11 | `core/router.yaml` | `svc-process` | Core (event bus) + Bot (command dispatch) |
| 12 | `core/core-community.yaml` | `svc-core` | Core (tenancy) |
| 13 | `core/core-data.yaml` | `svc-core` | Core |
| 14 | `core/core-identity.yaml` | `svc-core` | Core (identity) |
| 15 | `core/ai-researcher.yaml` | `svc-action` | Bot (standalone AI assistant) |
| 16 | `core/hub.yaml` | `hub-api` | Core (admin shell) |
| 17 | `core/marketplace.yaml` | `hub-api` | Core (marketplace) |
| 18 | `core/hub-webui.yaml` | `hub-webui` | Core (1:1, static SPA) |
| 19 | `interactive/ai.yaml` | `svc-action` | Bot (interaction) |
| 20 | `interactive/loyalty.yaml` | `svc-action` | Social (per core-boundary.md flagged call) |
| 21 | `interactive/interactive-gaming.yaml` | `svc-action` | Bot (clip/server_manager/server_status) |
| 22 | `interactive/interactive-media.yaml` | `svc-action` | Social (spotify/youtube_music) |
| 23 | `interactive/interactive-productivity.yaml` | `svc-action` | Social (calendar/lfg) |
| 24 | `interactive/interactive-social.yaml` | `svc-action` | Social (presence/shoutout/alias/quote/inventory/memories/translate) |
| 25 | `pushing/action-platforms.yaml` | `svc-action` | Bot (discord/slack/teams/mattermost/googlechat outbound) |
| 26 | `pushing/action-serverless.yaml` | `svc-action` | Bot (gcp_functions/lambda/openwhisk outbound) |

Tally: `svc-ingest` 10, `svc-process` 1, `svc-core` 3, `svc-action` 8, `hub-api` 2,
`hub-webui` 1 = 25... plus `core/hub-webui.yaml` counted at row 18 = **26**.

## 5 infrastructure Deployments — out of scope, untouched

`infrastructure/{minio,ollama,postgres,qdrant,redis}.yaml` are datastore/dependency
Deployments, not Module/Core app code. They are not part of the "~40 co-equal service
containers" the design doc collapses, and Task 0.5 does not touch them.

`26 + 5 = 31` — full denominator accounted for.

## `svc-rtc` — the 7th container has no Helm predecessor

`core/module_rtc` (Go WebRTC/SFU, Social-owned per `core-boundary.md`) has **no existing
Helm Deployment template** among the 31 — it exists only in the deprecated Kustomize tree
(`k8s/kustomize/`). `svc-rtc` is net-new in Helm, not a consolidation of an existing Helm
resource. It closes a gap rather than replacing one of the 31.

## Naming collision found and fixed during verification

Legacy `core/hub.yaml` hardcodes its Deployment/Service `metadata.name` to
`{{ fullname }}-hub-api` (its component *label* is `hub`, but the object *name* is
`hub-api` — a pre-existing quirk). Legacy `core/hub-webui.yaml` uses
`{{ fullname }}-hub-webui` for both label and name. The new `hub-api.yaml`/`hub-webui.yaml`
skeleton templates initially reused those same literal names and collided while coexisting
(`helm template` rendered two objects with the identical `metadata.name`, which
`kubectl apply`/`helm install` would reject or silently overwrite). Fixed by suffixing the
new containers' object names: `{{ fullname }}-hub-api-v3` and
`{{ fullname }}-hub-webui-v3`. Component labels were already disambiguated
(`hub-api` vs legacy's `hub`; `hub-webui-v3` vs legacy's `hub-webui`) so pod selectors never
collided — only the object names did. Confirmed no duplicate `metadata.name` remains via
`awk` scan over the full render (34 unique Deployment names, zero repeats).

## What remains (NOT done by this skeleton)

- **Per-module App-bundle wiring inside each container.** The env vars this skeleton renders
  (`MODULE_LOAD_BOT`, `MODULE_LOAD_SOCIAL`, etc.) are a values→env passthrough proving the
  toggle reaches every stage a Module touches. The Python-side conditional import of each
  Module's App bundle behind those env vars does not exist yet — that's the larger remaining
  build referenced in the task.
- **No Dockerfiles/CI images exist yet** for `svc-ingest`/`svc-process`/`svc-action`/
  `svc-core`/`hub-api`/`hub-webui`/`svc-rtc`. The skeleton pins each container's `image:` to
  the repo's existing base-image digest (`python:3.13-slim-bookworm@sha256:...` for the
  Python-based containers, `node:20-bookworm-slim@sha256:...` for `hub-webui`,
  `debian:bookworm-slim@sha256:...` for `svc-rtc`, matching `core/module_rtc/Dockerfile`'s
  runtime stage) rather than inventing a digest or using a mutable tag — grepped from
  existing Dockerfiles, not invented. These are placeholders; replace with
  `ghcr.io/waddlebot/waddlebot/{name}@sha256:<built-digest>` once each container's own
  Dockerfile and CI build exist.
- **Cutover.** Deleting the 31 legacy Deployments and repointing traffic (Services, Ingress,
  HTTPRoute, NetworkPolicy) at the 7 is not done here — the two sets coexist so this skeleton
  renders cleanly without breaking the running chart.
- **`svc-rtc` media transport** (UDP port range, `hostNetwork`/`hostPort` needs) is not
  wired — only an HTTP health/metrics port is exposed. Any NET_ADMIN/hostNetwork requirement
  needs explicit user approval before being added (ROOT EXCEPTION policy).

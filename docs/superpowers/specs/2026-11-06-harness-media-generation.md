# Harness Media Generation (image / music / video) — Design

**Status:** Proposed — v3.0 scope REDUCED to Gemini-only image + music + video (see §6 and Deferred). **Date:** 2026-11-06. **Flag:** `waddles.harness-media-generation` (default OFF).
**Scope:** `wit/waddle-bundle/stage.wit` (new `media` interface), `core/bundle_capability_gate` (`media.generate`), new `core/bundle_host_media` crate, `core/svc_process` + `core/bundle_executor` (bridge), `hub_api` (BYOK + credential broker + settings), webui settings page.

## 1. Licensing decision (2026-11-06; supersedes "BYOK = Enterprise")

| Rule | Decision |
|---|---|
| Minimum tier | **Professional** or higher. Free is refused (`tier-required`). |
| Key source | **BYOK required.** There is no platform-funded media key; a community/tenant without its own provider key is refused (`byok-missing`). |
| Effective tier | `max(tenant_tier, community_tier)`, cascading down (`critical-rules.md`). Resolved host-side; never taken from the guest or the bundle manifest. |
| Unknown license state | **Fail closed**: last-known cached tier within grace; never-seen = refuse (`tier-required`, wire code `license_unknown`). Never default to Free-allowed or Pro-allowed. |
| Where enforced | **Inside the host capability** (`bundle_host_media::gate`), never in a bundle. App bundles are free; they merely request the permission. |
| **BYOK tier (all AI)** | **BYOK = Professional or higher** for AI generally, not only media. `hub_api/services/ai_routing/router.py` `_byok_entitled` now checks Professional-or-higher (was Enterprise); premium-local-metered stays Enterprise. |
| Existing text `ai.generate` | Self-hosted Ollama, unchanged (its Enterprise enforcement is still tracked in #858). Media is a separate capability. |

Rationale: media costs real money per call, so the cost is borne by the key owner (BYOK) and the tier gates the feature, not the bundle.

## 2. Findings that shape the design

- `ai.generate` is **self-hosted Ollama** (`core/svc_process/src/ai.rs`), reached via the executor's `ai` import (`core/bundle_executor/src/host/stage_next_ai.rs`) carried as `capability=db, op="ai.generate"`; the stage (`svc_process::capabilities::handle_ai`) authorizes through the gate first. It has **no BYOK**.
- AI BYOK lives only in hub-api: `ai_byok_keys` (AES-256-GCM, `AI_BYOK_ENCRYPTION_KEY`, `hub_api/services/ai_routing/byok_crypto.py`), admin routes `PUT/GET/DELETE /api/v1/admin/<community_id>/ai/byok-keys` (key never returned, `key_last4` only), providers limited to OpenAI/Anthropic.
- **`TenantTier` is hard-coded `Free` in both stages' `gate_scope()`** (`svc_process/src/capabilities.rs`, `svc_action/src/capabilities.rs`) — no Rust tier resolver exists. Media therefore needs its own `TierResolver` seam (not a silent reuse of the scope field).
- `GuardedClient` (`core/bundle_host_http/src/guarded.rs`) provides pinned-IP, no-redirect, size-capped egress.

## 3. Architecture

```
bundle (guest) --media.submit--> executor (bridge only, no net)
   --host-call db/"media.submit"--> stage (svc_process)
        1 gate.authorize(media.generate)   grant, rate, instance policy
        2 flag waddles.harness-media-generation
        3 bundle_host_media::MediaHost.submit():
             a. validate request (kind, prompt bounds, PII reject, size/duration clamps)
             b. TierResolver.effective_tier(tenant, community) >= Professional  else tier-required
             c. CredentialResolver.resolve(tenant, community, provider)  else byok-missing
             d. rate limit + spend cap (per community, durable ledger)
             e. persist job(queued) -> return job-id
   worker: adapter.submit -> poll (Veo/Lyria long-running op) -> bytes
        -> validate content-type + size cap -> ObjectStore.put(tenant/community-scoped key, SSE)
        -> job(done, handle) | job(failed, typed error)
   guest: media.status(job-id) (poll) and/or `media-ready` event delivered through the bundle callback mechanism
   guest: media.url(job-id) -> scoped expiring URL (host-signed, tenant+community+job bound, <=15 min)
```

Credential resolution: production `CredentialResolver` calls a hub-api **internal** endpoint (service JWT, SPIFFE-ready, never a guest value) that decrypts the BYOK key hub-side; the plaintext lives only in a zeroizing buffer for one provider call, is never logged, labelled, put in a span, or returned across the guest boundary. Provider adapters use `GuardedClient` with **pinned provider hosts** (compile-time allowlist); no guest-supplied URL ever reaches the network.

### 3.1 Providers (no PRC-origin providers or models)

| Provider | Image | Music | Video | Auth | Pinned host |
|---|---|---|---|---|---|
| Google Gemini | Imagen (`imagen-*`, `:predict`) | Lyria (`lyria-*`) | Veo (`veo-*`, `:predictLongRunning` + operation poll) | `x-goog-api-key` header | `generativelanguage.googleapis.com` |
| OpenAI | `/v1/images/generations` | — | — | `Authorization: Bearer` | `api.openai.com` |
| others | pluggable via the `MediaProvider` trait; each must be added to the host allowlist in code review | | | | |

A (provider, kind) pair a provider does not offer is `unsupported-kind`. Model ids are operator config, never guest values.

### 3.2 Typed errors (WIT `media.error`)

`tier-required(tier)`, `byok-missing(provider)`, `provider-refused(reason)` (safety/policy refusal, closed vocabulary, never provider text), `quota(window)` (rate / spend cap), `timeout`, `denied(code)` (gate), `invalid(string)`, `unavailable(string)` (flag off / not wired — fail loud), `not-found` (unknown or cross-community job — indistinguishable), `backend(string)`.

### 3.3 Storage and delivery

Outputs go to SeaweedFS through the existing storage client, SSE on, key `media/{tenant}/{community}/{job}.{ext}` (numeric ids; never slug/name). Content-type is validated against the kind (`image/png|jpeg|webp`, `audio/mpeg|wav|ogg`, `video/mp4|webm`) by magic bytes, not the provider header; per-kind size caps (image 10 MiB, music 20 MiB, video 100 MiB). Job lookup, URL minting and reads re-check `(tenant, community)` against the invoke scope; a mismatch is `not-found`. Raw bytes never cross the guest boundary — the guest receives an expiring signed URL/handle only.

### 3.4 Limits and spend

Per-community rate (default 6 submits/min, 60/hour) and a **daily spend cap in micro-USD** from a per-kind cost table; the cap is checked at submit and debited from a durable ledger (a restart cannot reset it). Cost per kind is shown at activation (§4). Job TTL and a global per-community in-flight cap bound queue growth. Video/music default timeout 10 min; expiry = `timeout`.

## 4. Permission

`media.generate` — **dangerous** (spends the key owner's money, calls third parties, produces publicly-renderable content). Admin-approved at every tier like `ai.generate`; consent screen shows the notes "Spends your BYOK provider credits: est. image $0.04, music $0.10, video $1.50 per request; Professional tier required". Catalog entries: `bundle_capability_gate::PermissionId::MediaGenerate`, `hub_api/services/bundle_permission_catalog.py`. Default quota 6 calls/min. Prompts are PII-rejected (same contract as `ai.generate`).

## 5. hub-api / webui

`/api/v1/admin/<community_id>/media/byok-keys` (PUT/GET/DELETE, community-admin, DTOs, key never returned, `key_last4` only) reusing the `ai_byok_keys` encryption with a `purpose`/provider discriminator for `gemini`, `openai-images`. Internal `POST /internal/v1/media/credentials:resolve` (service JWT) for the stage. A webui settings page is part of PR3; until it lands the capability is **unserved in the UI** (API-only) and says so.

## 6. v3.0 scope (reduced) and delivery

v3.0 is **Gemini only** (single BYOK Gemini API key, pinned host `generativelanguage.googleapis.com`) covering **image (Imagen), music (Lyria), video (Veo)** behind the `MediaProvider` trait. Pro+ tier and BYOK required; flag default OFF; outputs in object storage, returned as scoped expiring URLs; manifest grant `media.generate`; basic per-community rate limit (6/min) plus a per-community in-flight job cap.

Async model (simplest correct): `media.generate-image|music|video(prompt, opts) -> result<job-id, error>` submits and returns a job id; the host runs the provider call (Imagen is a quick `:predict`, Veo/Lyria long-running operations are polled host-side) with a bounded timeout (image 60 s, music 5 min, video 10 min -> `timeout`); the guest polls `media.job-status(job-id) -> result<job-state, error>` where `job-state` = `pending | done(url) | failed(error)`. No event delivery in v3.0 (poll only). Job lookup is scoped to (tenant, community); a mismatch is `not-found`.

| PR | Content |
|---|---|
| 1 | Spec; BYOK floor -> Professional (hub-api router + tests); `media.generate` catalog (Rust gate + Python); WIT `media` interface (3 submit calls + `job-status`); `bundle_host_media` crate: types, typed errors, tier gate, validation, seams |
| 2 | Gemini adapters (Imagen/Lyria/Veo) over `GuardedClient` vs a fake API; job store, storage + scoped URLs; rate limit; stage + executor wiring; real-path e2e |
| 3 | hub-api media BYOK admin API (API only) + internal credential resolver |

Until PR2 wires the stage handler a `media.*` call answers `not_implemented` -> `unavailable` (fail-loud). The webui BYOK settings page is **unserved** in v3.0 (API only).

## Deferred to v3.1.x

OpenAI images and every other provider; routing through WaddleAI; spend caps and cost display at activation; webui BYOK settings page; advanced moderation beyond surfacing the provider's safety refusal; event/callback delivery of job completion (poll-only in v3.0).

## 7. Test matrix

Tier free/pro/enterprise/unknown × BYOK missing/present; community-tier lift (`max`); adapter exact request shapes + auth headers + operation polling against fake APIs; safety-refusal mapping; cross-community job/URL refusal; rate + spend cap; PII-prompt reject; executor e2e (fixture bundle -> fake provider -> scoped URL); cargo-mutants on the tier gate and scope check; >=90% line coverage.

## Implementation notes (Rust PR, 2026-11-06)

- **Provider models changed under us.** Imagen is shut down and Veo 3.1 preview was shut down 2026-10-22 (Gemini API docs). The adapters use Nano Banana (image), Lyria 3.5 (music) and Gemini Omni Flash (video) via the Interactions API on the one pinned host; no Vertex host is needed. Model ids are operator config.
- **Storage design superseded:** outputs go to the single bundle bucket under the media-harness prefix, tagged `app-bundle:<requesting bundle>`, and the guest receives an object handle (`media://{tenant}/{community}/{job}.{ext}`), not a URL; `object-url` presigns for the owning bundle only (interim; replaced by the bundle-object-storage capability's presign-get).
- **Polling does not spend the generation budget:** `job-status`/`object-url` use `CapabilityGate::check_granted` (grant + instance policy, no quota).
- **Hard-coded tier finding:** `gate_scope()` pins `TenantTier::Free` in svc_process and svc_action; left untouched. Media uses `TierResolver` (hub-api) instead.
- **hub-api contracts needed:** `GET /api/v1/internal/media/tier` (new, not part of the BYOK branch) and the BYOK resolver; until both exist the capability refuses (fail closed).

# Harness Media Generation (image / music / video) — Design

**Status:** Proposed. **Date:** 2026-11-06. **Flag:** `waddles.harness-media-generation` (default OFF).
**Scope:** `wit/waddle-bundle/stage.wit` (new `media` interface), `core/bundle_capability_gate` (`media.generate`), new `core/bundle_host_media` crate, `core/svc_process` + `core/bundle_executor` (bridge), `hub_api` (BYOK + credential broker + settings), webui settings page.

## 1. Licensing decision (supersedes the memory note "basic AI = Pro, BYOK = Enterprise" for media only)

| Rule | Decision |
|---|---|
| Minimum tier | **Professional** or higher. Free is refused (`tier-required`). |
| Key source | **BYOK required.** There is no platform-funded media key; a community/tenant without its own provider key is refused (`byok-missing`). |
| Effective tier | `max(tenant_tier, community_tier)`, cascading down (`critical-rules.md`). Resolved host-side; never taken from the guest or the bundle manifest. |
| Unknown license state | **Fail closed**: last-known cached tier within grace; never-seen = refuse (`tier-required`, wire code `license_unknown`). Never default to Free-allowed or Pro-allowed. |
| Where enforced | **Inside the host capability** (`bundle_host_media::gate`), never in a bundle. App bundles are free; they merely request the permission. |
| Existing text `ai.generate` | Unchanged (self-hosted Ollama; Enterprise enforcement still tracked in #858). Media is a separate capability so its tier rule cannot be confused with it. |

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

## 6. Delivery plan (stacked PRs into `release/v3.0.X`)

| PR | Content |
|---|---|
| 1 | This spec; WIT `media` interface; `media.generate` catalog entry (Rust + Python); `bundle_host_media` crate: types, errors, **tier gate**, request validation, `TierResolver`/`CredentialResolver`/`ObjectStore`/`MediaProvider` seams, tests + mutation check |
| 2 | Provider adapters (Imagen/Veo/Lyria/OpenAI) over `GuardedClient` vs fake provider APIs; storage + scoped URLs; rate/spend ledger; stage + executor wiring; real-path executor e2e with a fixture bundle |
| 3 | hub-api BYOK admin API + internal resolver + webui settings page |

Until PR2 wires the stage handler, a `media.*` host call answers `not_implemented` -> `unavailable` (fail-loud); the flag stays OFF.

## 7. Test matrix

Tier free/pro/enterprise/unknown × BYOK missing/present; community-tier lift (`max`); adapter exact request shapes + auth headers + operation polling against fake APIs; safety-refusal mapping; cross-community job/URL refusal; rate + spend cap; PII-prompt reject; executor e2e (fixture bundle -> fake provider -> scoped URL); cargo-mutants on the tier gate and scope check; >=90% line coverage.

# Premium-AI Routing + Model-Selection Design (free-local / premium-metered / BYOK)

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

**Date:** 2026-08-31
**Status:** Draft
**Branch:** feature/v3-premium-ai-spec
**Provenance:** `.PLAN:345-356` (Premium AI + BYOK; generalized metered-consumable token billing)
**Cross-refs:** `docs/plans/2026-08-31-metered-token-billing-design.md` (parallel — the token ledger/pricing/enforcement mechanism this spec consumes), `docs/plans/2026-08-31-v3-sccebm-program-plan.md` §5–§6 + P6, `docs/plans/2026-08-26-v3-scbm-apps-design.md` (Feature/flag/tier spine).

## Goal

Define a **single provider-routing / model-selection layer** the AI modules call, routing every
AI request across three model tiers — **free-local** (small Ollama model) → **premium-local-metered**
(large local MoE) → **BYOK-external** (community's own OpenAI/Anthropic/etc. key) — under four
composed constraints: community **entitlement** (tier × PostHog flag), **premium-AI token balance**,
whether a **BYOK key** is configured, and the **model_access_policy** (per-org/team/user model
allow/deny, default REJECT).

The layer consolidates the **two divergent provider abstractions that exist today** (see §0) behind
one normalized request/response contract, so model selection, metering, key handling, and policy
enforcement live in exactly one place instead of being re-implemented per module.

Premium-local **consumes premium-AI tokens** (metered per input+output token; global admin sets the
price — mechanism in the metered-token-billing spec). BYOK **consumes zero platform tokens** (the
community pays its provider directly). Free is **free**.

---

## 0. Current state (what we build on / around)

Two separate, non-unified AI provider abstractions ship today, plus a per-community AI config table
and the v3 gating spine. `model_access_policy`, a premium-AI token ledger, and a unified router are
**greenfield** (do not exist in code).

| Concern | Exists today | File:line |
|---|---|---|
| **Abstraction A** — ai_interaction | `AIService` + `AIProvider` Protocol → `OllamaProvider` \| `WaddleAIProvider`, chosen by `AI_PROVIDER` env | `action/interactive/ai_interaction_module/services/ai_service.py:19,42,52,63`; `config.py:51` |
| Free-local provider | `OllamaProvider` (host/port/TLS, reports `eval_count`) | `.../services/ollama_provider.py:20,182` |
| External proxy provider | `WaddleAIProvider` (OpenAI-compat proxy, reports `waddleai_tokens`) | `.../services/waddleai_provider.py:21,142` |
| ai_interaction surface | Quart REST `/api/v1/ai` (`/interaction`, `/chat/completions`, `/models`, `/config`), port 8005 | `.../app.py:44,47,171` |
| **Abstraction B** — ai_researcher | `AIProviderService` + `AIProvider(Enum)` = OLLAMA/OPENAI/ANTHROPIC, **direct** external calls (BYOK precedent) | `core/ai_researcher_module/services/ai_provider.py:31,75,174,201`; OpenAI url `:105`, Anthropic url `:111` |
| ai_researcher surface | Standalone Quart container, port 8070 | `core/ai_researcher_module/app.py:126`; `docker-compose.yml:1003` |
| Per-community AI config | `ai_researcher_config`: `ai_provider`, `ai_model`, `custom_ai_endpoint`, `custom_api_key_encrypted`, `use_custom_endpoint`, `is_premium` — **BYOK + premium precedent** | `config/postgres/migrations/045_add_admin_feature_tables.sql:36-57`; custom-endpoint cols `005_add_custom_ai_endpoints.sql:5-13` |
| Per-community AI enablement | `community_ai_chatter_config` (opt-in + rate limits) | `config/postgres/migrations/061_community_ai_chatter_config.sql:12` |
| Gating spine | `FeatureContract{id,version,module,requires_scopes,min_tier,flag}`; `min_tier ∈ {free,professional,enterprise}`; `flag == "waddles."+id` | `libs/flask_core/flask_core/feature_contract.py:74`, parse rules `:130` |
| Two-gate resolver | `feature_enabled(flag_key, *, tenant, community, default)` | `libs/flask_core/flask_core/feature_flags.py:37` |
| AI feature contracts | `social.welcome_ai` (enterprise), `integrations.waddleai` (enterprise) | `libs/social_module/features.py:156`; `libs/core_platform_module/features.py:177,180,182` |
| `is_premium` consulted | Node admin (pre-v3) — being ported to Python hub-api | `admin/hub_module/backend/src/controllers/adminController.js:2424`; toggle `superadminController.js:299` |
| Pre-v3 hand-rolled gating (replaced) | `license_service.py` copies | `core/workflow_core_module/services/license_service.py`; `core/video_proxy_module/services/license_service.py:29` |

**Two consistency defects to fix while unifying** (both to be resolved by the router):
1. ai_interaction and ai_researcher have **independent** provider abstractions and env schemas — the
   whole point of this layer is to collapse them into one.
2. ai_researcher's config validation allows only `ollama`/`waddleai` (`core/ai_researcher_module/config.py:439`)
   while its service enum implements `ollama`/`openai`/`anthropic` — advertised vs implemented mismatch.

**Not in tree (this spec defines them as greenfield):** `model_access_policy` table (migration 018 is
`018_add_simple_games.sql`, unrelated — there is no per-org/team/user model gating anywhere today),
premium-AI token ledger/balance, and the unified router itself.

---

## 1. Model tiers

| Tier | Model (example) | Runtime / provider | Cost model | Requires | Consumes |
|---|---|---|---|---|---|
| **FREE** | small local, e.g. `llama3.1:1b` | Ollama, ubiquitous (small node / sidecar) | free, always-on default | feature flag on | nothing |
| **PREMIUM** | large local MoE, e.g. `gemma4:26B-A4B` | Ollama on a **beefy host** (GPU/large-RAM node pool) | **per-token metered**, global admin sets price | premium-AI capability entitled (§7) **AND** token balance > 0 **AND** policy allows model (§5) | **premium-AI tokens** = input+output tokens (§4) |
| **BYOK** | community's own model (`gpt-4o`, `claude-3.7`, custom OpenAI-compatible…) | proxied **server-side** to the community's external provider | community pays its provider **directly** | BYOK capability entitled **AND** key configured (§3) **AND** policy allows model | **no platform tokens** (usage may be recorded for analytics only) |

Today's defaults (`OLLAMA_MODEL=llama3.2` at ai_interaction `config.py:60`; `tinyllama` at
ai_researcher `045:41`) become the FREE tier once normalized to one agreed small model (open decision).
The MoE model tag is an open decision (confirm a real Ollama tag; `gemma4:26B-A4B` is illustrative).

---

## 2. Provider-routing / model-selection layer

### Abstraction

A single entry point every AI-using module calls, replacing per-module `AIService.create()`
(`ai_service.py:52`) and `AIProviderService` (`ai_provider.py:75`):

```
AIRouter.route(AIRequest) -> AIResponse        # one call; router picks tier, dispatches, meters
```

Recommended home: a **shared library** libs/ai_routing (in-process, hot path) whose adapters wrap
the *existing* providers (`OllamaProvider`, `WaddleAIProvider`, direct OpenAI/Anthropic clients) — see
§8 "which container hosts routing".

### Normalized request / response (provider-agnostic)

```
AIRequest  { tenant, community_id, feature_id,        # feature_id e.g. "social.welcome_ai"
             messages|prompt, model_hint?, requested_tier?,
             max_tokens, temperature,
             on_insufficient_balance ∈ {block, fallback_free},   # per call-site, default per §2 recommendation
             invocation ∈ {interactive, ambient} }
AIResponse { text, provider, model, tier_used,
             usage{ input_tokens, output_tokens, total },
             billed_tokens,                            # 0 for free/BYOK; = usage.total for premium-local
             fallback_reason? }                        # set when the effective tier != requested tier
```

Each provider adapter implements `generate(AIRequest) -> AIResponse` and **normalizes usage** from its
native shape — Ollama `prompt_eval_count`/`eval_count` (`ollama_provider.py:182`), OpenAI/Anthropic
`usage`, WaddleAI `waddleai_tokens` (`waddleai_provider.py:142`).

### Selection precedence

```
route(req):
  1. resolve community AI config        (ai_researcher_config-style row, generalized per-community)
  2. POLICY GATE  — model_access_policy, default REJECT (§5)
        model denied for org/team/user? -> BLOCK (never silently swap model)
  3. resolve effective tier             (requested_tier | model_hint -> tier; else community default = FREE)
  4. route by tier, walking the ladder DOWN on failure:
        FREE     -> free-local Ollama (small)                     [gate: feature flag only]
        PREMIUM  -> premium-local MoE, METER tokens               [gate: entitled AND balance>0 AND policy]
        BYOK     -> external via community key, server-side proxy [gate: entitled AND key configured]
  5. return AIResponse (tier_used, usage, billed_tokens, fallback_reason)
```

Fallback ladder (explicit): **premium-local-metered → BYOK-external → free-local**. Free-local is the
floor and is always reachable (subject only to the feature flag), so ambient AI never hard-fails.

### Premium requested but no balance — RECOMMENDATION

Split by invocation, carried on `on_insufficient_balance`:

| Invocation | Behavior | Rationale |
|---|---|---|
| **interactive** (user explicitly asks for a premium generation) | **block-with-upgrade** — return an "insufficient premium-AI tokens; purchase / upgrade" result, do **not** silently downgrade | Mirrors seat-overage rule: block the deliberate action, offer an upgrade path (`critical-rules.md` Licensing Model) |
| **ambient** (proactive chatter, event welcome, auto-summaries) | **graceful fallback to free-local**, logged + `fallback_reason` set | Never break the bot on an empty balance (`client.md` graceful degradation) |

Default `on_insufficient_balance`: `block` for user-invoked endpoints, `fallback_free` for
event/automatic call-sites. Same split applies when the **balance/entitlement server is unreachable**:
use last-known cached balance; if unknown, ambient degrades to free, interactive blocks with a
transient-error message (never crash).

BYOK failures (bad key, provider quota/`429`) surface to the **community** (their cost, their problem);
fall back to free-local only for ambient invocations, never charge platform tokens.

---

## 3. BYOK (bring-your-own-key)

**Providers at launch (recommended):** OpenAI, Anthropic (both already have direct clients —
`ai_provider.py:105,111`), plus a **custom OpenAI-compatible endpoint** (the `custom_ai_endpoint`
precedent — `005:5-13`, comment cites "Azure OpenAI, private Ollama, etc."). Extensible later: Azure
OpenAI, Google Gemini. Provider set is an open decision.

**Storage (hard rules — `client.md`, `security.md`):**
- Keys are **per-community**, **server-side only**, **never** hardcoded, **never** in plaintext, and
  **never** callable from the client — all third-party AI calls are **proxied through the backend**.
- Precedent column `ai_researcher_config.custom_api_key_encrypted` (`045:43`) is the right shape but
  must be a **real secret**, not a reversible/plaintext blob: store an **envelope-encrypted ciphertext
  (KMS/DEK-wrapped)** or a **secret-ref** into Vault / External-Secrets (`security.md` Secrets), with
  at-rest encryption mandatory. Decryption happens **only** in the routing/control-plane container
  (§8); the plaintext key never leaves the server, is never logged, and is masked in any diagnostics.
- BYOK entitlement is an OIDC-scoped, tier-gated capability (§7); the key record is tenant/community
  scoped at the ORM layer (never trusted from request body).

**Rotation:**
- A per-community rotate endpoint (hub-api settings/marketplace) **validates the new key** with a cheap
  `/models` or health call before committing, re-encrypts / re-refs, **invalidates the router's cached
  binding**, and discards the old key. Optional dual-key grace window for zero-downtime rotation.

---

## 4. Billing tie-in

Mechanism (ledger store, pricing surface, purchase/refill, balance enforcement) is owned by the
**metered-token-billing spec** — this spec only defines *what* premium-AI meters and *how* per request.

| Tier | Metered? | Per-request accounting |
|---|---|---|
| FREE | no | — |
| PREMIUM-local | **yes** | `billed_tokens = usage.input_tokens + usage.output_tokens`; decrement community premium-AI balance; global-admin price per token |
| BYOK | no platform charge | record `usage` for analytics only; community billed by its own provider |

**Token counting — RECOMMENDATION:** prefer **provider-reported** usage (authoritative, cheap):
premium-local Ollama returns `prompt_eval_count`+`eval_count` (`ollama_provider.py:182`); external
providers return `usage`; WaddleAI returns `waddleai_tokens` (`waddleai_provider.py:142`). Fall back to
a **local tokenizer estimate** only when a provider does not report. Meter at response time and emit a
usage event to the billing ledger (license-server metering per the billing spec).

**Overspend guard:** pre-check `balance > 0` before dispatch (§2 step 4). For large generations,
optionally **reserve/hold** an estimated max up front and settle to actuals on completion (avoids a
single request driving the balance negative). Reserve-vs-post-charge is an open decision (§8).

Premium-AI tokens are **one of two consumables sharing one mechanism** — the other is
transcoding/encoding tokens (streaming module). This is the platform's **third billing axis**
(per-consumable-token / usage-metered), independent of per-node and per-seat licensing
(`.PLAN:352-356`, `critical-rules.md` Licensing Model).

---

## 5. model_access_policy tie-in

**Reality:** greenfield. No `model_access_policy` table or per-org/team/user model gating exists;
"migration 018" in tree is `018_add_simple_games.sql` (unrelated). This spec defines the table; the
closest precedent is `ai_researcher_config`'s per-community model choice (§0).

**Shape:** `model_access_policy` scoped at **org (tenant) / team (community/OU) / user**, **default
REJECT** — an explicit allowlist; a model is usable only if explicitly allowed at some applicable scope.

**Composition with tier/BYOK/balance — orthogonal, restrict-only:**

```
usable(model) == policy_allows(model)  AND  tier/balance/BYOK reaches(model)
```

- Policy is evaluated **first** (§2 step 2) and can **DENY** a model the tier/balance/BYOK would
  otherwise allow — e.g. block `gpt-4` for one team even though BYOK is configured, or block a specific
  premium local model for a compliance-scoped org.
- Policy **never grants** a model the tier does not entitle — it only narrows. This mirrors the scope
  layering in `security.md` (Global → Tenant → Team → User/Resource; narrower layers restrict, never
  expand).
- On deny: **block with reason**; do not silently substitute a different model. (A policy *may* name an
  allowed alternative, but substitution is explicit, never a hidden fallback.)

---

## 6. Infra

```
FREE small model      ->  Ollama, ubiquitous (svc-core / small node / sidecar)   cheap, every env
PREMIUM MoE           ->  Ollama on a BEEFY host (GPU / large-RAM node pool)      dedicated, gated envs
BYOK external         ->  server-side egress to api.openai.com / api.anthropic.com (allowlisted)
```

- **Free-local** runs everywhere. Precedent: the mem0 local-model infra (Qdrant + Ollama, fully local —
  root `CLAUDE.md`) and the `ai-ollama` compose service (`docker-compose.yml`; ai_interaction
  `depends_on: ai-ollama`). Ollama supports host/port/**TLS** already (`ollama_provider.py` SSL context;
  `config.py:57-59`).
- **Premium MoE** needs the "beefy host" (`.PLAN:348`) — GPU or large-RAM node, node affinity/taint;
  **not** deployed to every environment. Two distinct Ollama endpoints (small = ubiquitous, MoE = beefy);
  the router selects the endpoint by tier. Env matrix (open decision): local/alpha = free-local only;
  beta = free-local; premium MoE on a dedicated GPU node pool (gamma/prod or a standalone beefy node).
  Model pull precedent: `core/ai_researcher_module/scripts/pull-models.sh`.
- **BYOK egress** must be on the NetworkPolicy egress allowlist (`security.md` Egress filtering); calls
  originate from the server-side proxy only.

---

## 7. Gating

Every AI capability is gated **flag × tier** (two-gate, `critical-rules.md`) via a `FeatureContract`
(`feature_contract.py:74`; `flag == "waddles."+id`; `min_tier ∈ {free,professional,enterprise}`).

**Two levels — keep distinct:**
1. **Feature** level = *what the AI does* — existing contracts: `social.welcome_ai` (enterprise,
   `social_module/features.py:156`), `integrations.waddleai` (enterprise,
   `core_platform_module/features.py:177`), plus future `analytics.*` / `ai_researcher.*` AI features.
2. **Model-tier** level = *which model serves it* — free-local / premium-local / BYOK, chosen by the
   router **beneath** an already-entitled feature.

**New capability contracts this spec introduces** (per-capability flags, `.PLAN:468-474`):

| Contract id | Flag | min_tier (recommended) | Gates |
|---|---|---|---|
| `ai.premium_models` | `waddles.ai.premium_models` | professional* | access to premium-local metered tier (then also requires token balance) |
| `ai.byok` | `waddles.ai.byok` | professional* | ability to configure BYOK keys and route to them |

\* `min_tier` for these is an **open decision** — because premium-AI is a *purchasable metered
consumable*, it may instead be exposed to Free tier as a pay-as-you-go capability (consumable is
orthogonal to tier). Custom-endpoint BYOK was labelled "enterprise" in the `005` precedent, arguing
Enterprise for `ai.byok`. Resolve in §8.

**Graceful degradation:** flag / license / balance server unreachable → last-known cached value
(never-seen flags default OFF); ambient AI degrades to free-local; the bot never crashes
(`critical-rules.md`, `client.md`).

---

## 8. Open decisions

| # | Decision | Recommendation / note |
|---|---|---|
| 1 | **Exact model list per tier** | Normalize one FREE small model (today: `llama3.2` / `tinyllama` split); confirm a real Ollama tag for the PREMIUM MoE (`gemma4:26B-A4B` illustrative) |
| 2 | **Premium-no-balance behavior** | **Recommend the split (§2):** interactive → block-with-upgrade; ambient → fallback-to-free. Confirm defaults per call-site |
| 3 | **BYOK provider set + key backend** | Launch: OpenAI + Anthropic + custom OpenAI-compatible; later Azure/Gemini. Backend: **envelope-encrypted (KMS/DEK) or Vault/External-Secrets ref**, upgrading the plaintext-capable `custom_api_key_encrypted` precedent |
| 4 | **Token counting method** | **Provider-reported preferred**, tokenizer fallback. Reserve/hold vs post-charge for large generations — pick one |
| 5 | **Which container hosts routing** | **Recommend: routing logic = shared lib libs/ai_routing (in-process, hot path); stateful concerns — BYOK key decryption, balance check/decrement, pricing, model_access_policy — resolved through hub-api (control plane owns secrets + billing + primary writes), cached short-TTL.** A dedicated AI svc is **not** recommended (extra hop; AI modules already are the action-pipeline consumers). Rationale below |
| 6 | **WaddleAI relationship** | `integrations.waddleai` is an enterprise-gated feature (`core_platform_module/features.py:177`) and `WaddleAIProvider` is an OpenAI-compat proxy already in-tree. Options: (a) a BYOK-class external target with a platform-managed key, (b) the premium-external routing engine itself, (c) orthogonal enterprise integration. **Recommend (a)** — WaddleAI as an enterprise external provider adapter under BYOK routing. Confirm |
| 7 | **model_access_policy** | Greenfield table; confirm scope granularity (org/team/user), home (product DB vs license-server), and default REJECT vs default-allow-with-blocklist (spec assumes **default REJECT**) |
| 8 | **Consolidate the two abstractions** | Fold ai_interaction `AIService` and ai_researcher `AIProviderService` into the one router; fix the ollama/waddleai-vs-ollama/openai/anthropic mismatch (`ai_researcher/config.py:439`). Confirm lib location + migration order |
| 9 | **Is premium-AI purchasable by Free tier?** | Consumable is orthogonal to tier — recommend premium-AI purchasable as pay-as-you-go behind `ai.premium_models` flag, with tier setting only whether BYOK/premium *routing* is offered. Confirm min_tier for `ai.premium_models` / `ai.byok` (ties to #5/§7) |

### Why routing = library + hub-api state (decision #5, expanded)

- **Library for the decision:** the tier pick + policy check + provider dispatch + usage normalization
  is latency-sensitive and belongs in-process in each AI-using container, wrapping the providers that
  already live there. This directly cures today's duplication (two abstractions, §0).
- **hub-api for the state:** BYOK keys, token balance, pricing, and `model_access_policy` are secrets +
  billing + primary-DB writes — which the control plane owns (`.PLAN:358-365` marketplace-in-hub-api;
  bundle control-plane pattern). Every AI container holding provider keys and its own balance logic is
  exactly the sprawl v3 removes. AI containers read entitlement/balance/policy/key-material from hub-api
  over a short-TTL cache; hub-api holds the primary writes.
- **Premium MoE endpoint is infra, not a routing container** — an Ollama deployment on a beefy node,
  addressed by the router by tier.

---

## Appendix — request flow (end to end)

```
AI module (welcome / chatter / researcher / analytics)
   │  AIRequest{tenant, community, feature_id, invocation, ...}
   ▼
feature_enabled(flag, tenant, community)        # §7 two-gate: is the FEATURE entitled at all?
   │ yes
   ▼
AIRouter.route()                                 # libs/ai_routing (in-process)
   ├─ resolve per-community AI config     ◄── hub-api (cached)
   ├─ model_access_policy  (default REJECT)◄── hub-api  ──► deny? BLOCK (§5)
   ├─ effective tier?
   │     FREE     ─────────────────────────────► Ollama small (local)          billed_tokens=0
   │     PREMIUM  ─ balance>0? ◄── hub-api ─┬─yes► Ollama MoE (beefy)  ─ meter ─► decrement (§4)
   │                                        └─no─► §2 split: block-upgrade | fallback_free
   │     BYOK     ─ key? decrypt ◄── hub-api ───► external provider (server proxy)  billed_tokens=0
   ▼
AIResponse{ text, provider, model, tier_used, usage, billed_tokens, fallback_reason }
```

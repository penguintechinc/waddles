# Community tier gates

Decision (2026-11-18): a community is **Free** or **Premium** only. There is ONE Premium pool
per tenant. A community is Premium only through an allocation from that pool; the tenant's own
license tier (Professional/Enterprise) never uplifts a community. Allocation and pool mechanics:
`docs/community-tier-allocation.md`.

Resolver: `community_tier_service.resolve_effective_tier(async_dal, dal, tenant_id, community_id)`
returns `'premium'` or `'free'`, keyed by numeric ids, failing closed to `'free'` (ERROR log,
exception type only) on any error.

## Gates

| Gate | Where | Requires |
|------|-------|----------|
| Workflows (create/update/publish/execute/test/webhook) | `services/workflow_service.py` | Premium |
| Media generation + media BYOK keys | `services/media_byok_service.py` (`MIN_MEDIA_TIER`) | Premium |
| Premium-local AI (metered Ollama tier) | `services/ai_routing/router.py` | Premium (+ flag `waddles.ai.premium_models`) |
| Custom endpoint / BYOK AI | `services/ai_routing/router.py` | Provider rule below (+ flag `waddles.ai.byok`) |
| Operator tier override | `PUT /superadmin/communities/<id>` | `licenseTier` 1 or 2, `reasonCode`, system tenant + `users:admin` |

Every gate treats an unknown, inactive or other-tenant community exactly like Free (no existence
oracle).

## AI provider rules

| Provider | Free | Premium |
|----------|------|---------|
| `ollama` | allowed (custom endpoint) | allowed |
| `waddleai` | allowed (custom endpoint) | allowed |
| `openai` | denied | allowed (BYOK) |
| `gemini` | denied | allowed (BYOK) |
| `anthropic` | denied | allowed (BYOK) |
| anything else | denied | denied |

Constants: `FREE_CUSTOM_ENDPOINT_PROVIDERS`, `PREMIUM_BYOK_PROVIDERS`; check:
`router.provider_allowed_for_tier(provider, tier)`. A denial is a 403 `AI_TIER_NOT_ENTITLED`
(interactive) or a graceful fall back to the free-local default (ambient). The default
free-local Ollama path is unchanged and is not gated by the provider rule.

Note: today only `openai` and `anthropic` have a dispatchable BYOK client and stored key
(`config_service._VALID_PROVIDERS`); Gemini is media-only BYOK. A custom-endpoint (Ollama/WaddleAI)
dispatch path with a stored endpoint is not built yet; the gate is ready for it.

## Display and mappings

- `licenseTier` on `/auth/me` and superadmin DTOs is `1` (Free) or `2` (Premium); an unknown stored
  allocation code displays as `1`. `isPremium` is derived (`licenseTier == 2`).
- Legacy strings in `communities.license_tier` (`pro`, `professional`, `premium`, `enterprise`) read
  as Premium (2) for display only; they are never a gate.
- The media proto still names `MEDIA_TIER_PROFESSIONAL`/`ENTERPRISE`; community Premium maps to
  `MEDIA_TIER_PROFESSIONAL` (the lowest paid enum) and `ENTERPRISE` is never emitted.

## Tenant-level gates (unchanged)

`services/envelope/gate.py` (external KMS) and `services/free_tier_caps.py` (free-tier entity
caps) gate on the TENANT's license tier (`get_tier(tenant=...)`, `flask_core.tier_catalog`
Professional/Enterprise ladder), not on a community's tier, so they are untouched.
`flask_core.entitlement` likewise resolves only the tenant tier; the generic
`CommunityTierSource` uplift hook was removed.

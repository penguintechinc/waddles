# Web UI: community tiers

Frontend for the backend contract in `docs/community-tier-allocation.md`. Communities are only Free (1) or Premium (2), with ONE Premium pool per tenant; any other code reads as Free (tenants keep their own ladder, not shown here). Names, codes, badge colors and reason labels live only in `admin/hub_module/frontend/src/services/communityTierConstants.ts`.

## Screens

| Screen | Path | Who | Notes |
|--------|------|-----|-------|
| Tenant "Community tiers" | `/tenant/:tenantSlug/community-tiers` (nav: Tenant Admin group) | `tenant:admin` + slug | One Premium pool meter, per-community Free/Premium select, usage reloaded after every change (never optimistic). |
| Super admin "Manage Communities" edit dialog | `/superadmin/communities` | `communities:admin`; tier change also needs `users:admin` | License tier select (Free / Premium) replaces the old Premium checkbox; list shows a Free/Premium badge. |

## Tenant page behaviour

- Pool: one meter, "Premium communities: used of limit", available, over-limit warning in text and color, `unlimited` for -1 ("Unlimited (operator deployment)" when `source` is `bypass_default`), `not licensed` for null, a source note, a Stale badge with the observed time. Allocations are read with `?tier=2`; the PUT sends tier 1 (release) or 2 only.
- Communities come from `tenantApi.getCommunities`; only the `publicId` UUID is sent to allocation calls. Current tier is read from `communities/tier-allocations` (no row = Free).
- Release or downgrade asks for confirmation first ("this community loses Premium features").
- 402 `TEAM_POOL_EXHAUSTED` shows the upgrade panel ("Your Premium pool is full (N of N). Release a slot or upgrade your license."), not an error toast. 503 `TEAM_POOL_UNAVAILABLE` says team licensing isn't available from the license server yet (selects disabled, no fake data). 404 (flag `waddles.community-tier-allocation` OFF or not this tenant's community) shows a "not enabled" state. 403, 409 and other errors have their own messages. Results are announced in an `aria-live` region.

## Super admin dialog

- Changing the tier requires a `reasonCode` (`support_request`, `sales_agreement`, `trial_extension`, `billing_correction`, `partner_program`, `data_correction`, `other`). The request carries `licenseTier` + `reasonCode` and never `isPremium`.
- Helper text: operator override is not blocked by the tenant's pool, counts against it and is audited.
- `seatLimit`: omitted when unchanged, explicit `null` when a set value is cleared (unlimited), integer >= 1 otherwise. A cleared description is sent as `null`.
- Backend errors show inline (`role="alert"`); a 403 on a tier change falls back to "requires global admin access (users:admin)".

## Tier-gated admin pages

`user.isPremium` is never returned by any API; the pages now read `communities[].licenseTier` from `/auth/me` via `useAuth().communityTier(communityId)` (missing or unknown = Free; the gate is `>= 2`).

| Page | Needs | Mirrors server gate |
|------|-------|---------------------|
| Bot Detection (`AdminBotDetection`) | Premium | locked page shows "requires Premium" |
| Analytics (`AdminAnalytics`) | Premium (health score, bad actors, retention) | |
| AI Researcher config (`AdminAIResearcherConfig`) | Free: custom endpoint only for Ollama or WaddleAI; Premium adds BYOK for OpenAI, Gemini, Anthropic (locked "Requires Premium" on Free); bot-detection toggle Premium | `hub_api/services/ai_routing/router.py` |
| Community dashboard bot score card | Premium | |

The UI gate is a UX hint only; server gates are authoritative.

## Out of scope

The tenant license-key UI is a later piece.

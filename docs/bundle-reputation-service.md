# Reputation: service bundle + default logic bundle

Two cooperating bundles, split on purpose:

| Bundle | Kind | Owns | Does NOT own |
|---|---|---|---|
| `waddles.core.service.reputation` (v1.1.0) | `service`, always-on, ONE per platform | the store access, tools, caps/idempotency/ledger/audit (host capability), soft-ban state, FLOOR ENFORCEMENT (community soft/hard + tenant), floor/threshold config, tiers (display) | scoring weights, event->delta mapping |
| `waddles.core.app.reputation-accrual` (v1.0.0) | `app`, `provides: reputation-logic` | event->delta mapping, weights, cooldowns, low-score notice; per-community config in its own kv | the store, caps, floors, bans |

Floors are admin limits, not scoring logic, so they live with the store. Scoring is replaceable: any approved
app bundle that `provides: reputation-logic` can take the accrual bundle's place (contract below).

```
platform event -> svc-process -> reputation LOGIC app bundle (default: accrual)
                                     | tool.invoke  adjust / apply_moderation  (+ idempotency key)
                                     v
                          reputation SERVICE bundle  -- floors, soft-ban, tools
                                     | reputation host cap (apply / apply-moderation / get-tenant / history / ...)
                                     v
                          bundle_host_reputation  -- membership (live), per-class 24h caps, reputation_apply()
                                     v
                          Postgres (bundle_reputation_scores / _adjustments, reputation_soft_bans, ...)
```

## Scale and classes (decision D1)

Legacy FICO-style scale: **300..850, start 600**, clamped by `reputation_apply()`. Two reason classes, chosen by
the HOST from the op and the granted permission (never from a guest argument):

| Class | WIT op | Permission | Per-call | Per user / day | Per community / day |
|---|---|---|---|---|---|
| activity | `apply` | `reputation.community.write` | +/-5 | 5 | 50 |
| moderation | `apply-moderation` | `reputation.moderation.write` (core/approved only) | +/-200 | 400 | 4000 |

The two windows are independent (the ledger records `reason_code` as `<class>:<source>`). A moderation write from
a human-originated invocation additionally needs the verified actor to hold `community:moderate` (or admin);
only a SYSTEM-originated invocation is exempt (host-derived from an allowlist of platform event types:
`channel.follow|followed|subscribe|cheer|raid|ban|unban|moderate`, `channel.subscription.{gift,gifts,new,renewal}`,
`moderation.ban|timeout` (internal types such as `reputation.floor_check` are never system-origin; svc-ingest drops reserved `reputation.*`/`internal.*` types); `message`, `interaction.command`, `chat.*` and every unknown
type are human; the flag never survives a hop through a non-`waddles.core.` bundle).

## Service tools

| Tool | Access | Notes |
|---|---|---|
| `get_score {user}`, `get_many {users<=100}`, `rep {user}` | read | score + tier (FICO tiers: poor <580, fair 580, good 670, very_good 740, exceptional 800) |
| `leaderboard {limit?}` | read | **community-scoped only**; there is deliberately NO tenant-wide leaderboard (D3) |
| `adjust {user, delta, reason, platform?}` | write | activity class; caller names the delta, host bounds it; idempotent; then floors are enforced |
| `apply_moderation {user, delta, reason, platform?}` | write, dangerous | moderation class; needs the caller's approved dangerous-tool grant |
| `override {user, absolute_score, reason?}` | write, dangerous | admin (`required_scope: community:admin`) |
| `get_tenant_score {}` | read | the verified actor's OWN tenant aggregate (no `user` arg; no actor = forbidden) |
| `history {user, limit?, before?}` | read | own ledger, or admin |
| `check {user}` | read | score, tier, `soft_banned`, `at_risk` |
| `at_risk {limit?}` | read, admin | members below `at_risk_threshold`, lowest first |
| `is_soft_banned {user}` | read | cheap read for game/raffle/giveaway/economy bundles (see Soft ban) |
| `get_policy {}` / `set_policy {..}` | admin | floors/thresholds only; validated `300 <= hard < soft < at_risk <= 850` |

### Floors (decision D2: everything OFF by default)

Community policy (bundle kv `floors`, defaults): `soft_ban_enabled=false soft_ban_floor=550`,
`hard_ban_enabled=false hard_ban_floor=400`, `at_risk_threshold=600`. Tenant policy (hub-api-owned
`reputation_tenant_policy`, read-only here): `tenant_auto_ban_enabled=false`, `tenant_auto_ban_floor` (recommended
default **350**), `tenant_at_risk_threshold`. After every applied change, behind the PostHog flag
`waddles.reputation-auto-ban` (default OFF):

1. score `<` hard floor (enabled) -> ban in this community (`community_hard_floor`) via the platform ban tool;
2. else score `<` soft floor (enabled) -> soft ban (`community_soft_floor`);
3. else a FLOOR-TRIGGERED soft ban is lifted; a manual soft ban (other reason) is never cleared;
4. tenant: the user's tenant aggregate `<` tenant floor (policy enabled) -> ban (`tenant_floor`).

The result carries `floors:{community,tenant}` (`disabled|none|ban|soft_ban|soft_ban_lifted|ban_failed:<code>`); a
failed ban never fails the already-applied score change and is never silent. Known limits: a human-less platform
ban needs a host-asserted automated actor for the platform moderation tool (it is `required_scope:
community:moderate`) - until that lands a floor ban reports `ban_failed:forbidden`; tenant-floor ban fan-out
runs in the current community only. Kill switch: `waddles.disable-bundle-reputation`.

### Soft ban

Cannot WIN anything in that community (games, raffles, giveaways, economy payouts). State lives in the
hub-api-owned `reputation_soft_bans`. Recording a ban is never throttled (a floor ban always applies); LIFTS are
quota-limited (5/user/day, 50/community/day, typed `rate_limited`) which also bounds flapping. Winner-picking
bundles must call `is_soft_banned` (or `check`) and exclude those users; the shared helper/bundle updates are
follow-ups listed in the PR (waddle-sdk helper).

## Tenant aggregate

`get_tenant_score` reads the user's OWN aggregate over the tenant's communities. Interim implementation: rounded
mean of the user's per-community scores. **Planned v3.1.x (#1235):** a per-community net contribution cap of +/-100
to the tenant aggregate (anti-brigading), computed inside `reputation_apply()`; the ledger already attributes every
row to its community so it can be added without a data migration.

## Contract with hub-api (`feature/reputation-hub-api` owns the migration)

The host consumes, the hub-api migration creates:

* `reputation_apply(p_tenant bigint, p_community bigint, p_user_uuid uuid, p_delta int, p_reason_code smallint,
  p_actor text, p_source_event text, p_idempotency_key text) returns table (new_score int, applied bool)` -
  SECURITY DEFINER; locks, clamps to the scale, writes the ledger (`kind = 'apply'`, `reason_code` =
  `activity:<src>` / `moderation:<src>`), idempotent on the key (`applied = false` on replay) and re-verifies
  the member row `FOR SHARE` (raises `not_a_member`); the least-privilege role has no UPDATE on `community_members`.
* ledger `kind` CHECK must allow `'apply'`; role needs `EXECUTE` on the function, `SELECT` on
  `reputation_tenant_policy (tenant_id, tenant_auto_ban_enabled, tenant_auto_ban_floor, tenant_at_risk_threshold)`,
  and `SELECT, INSERT, UPDATE` on `reputation_soft_bans (id, tenant_id, community_id, user_uuid, reason_code,
  created_at, lifted_at)`.
* Policy field names are exactly: `soft_ban_enabled soft_ban_floor hard_ban_enabled hard_ban_floor
  at_risk_threshold tenant_auto_ban_enabled tenant_auto_ban_floor tenant_at_risk_threshold`.

## Third-party reputation logic bundles

A third-party bundle may replace the default logic. Contract:

* Manifest declares `provides: reputation-logic` (kind `app`). **At most ONE active `reputation-logic` bundle per
  TENANT**, shared by all its communities; activating a second returns a typed `409` (hub-api activation check).
  The logic bundle's own configuration stays per community (weights may differ per community). Exactly one
  reputation SERVICE bundle exists globally.
* It receives the events it declares in `consumes` (Twitch/Kick/Discord follow, subscribe, subscription.*, cheer,
  raid, chat.message, interaction.command; platform moderation outcomes are NOT delivered yet).
* It may call only service tools it holds `tool.call` grants for: `adjust` (activity, +/-5), `check`,
  `get_score`/`get_many`/`rep`, `is_soft_banned`, `leaderboard`. `apply_moderation` is dangerous and needs an explicit
  admin-approved grant; `override`, `at_risk`, `get_policy`, `set_policy` are admin-scoped; history/tenant ops are
  core-only.
* Every cap still applies: per-call, per user/day, per community/day (host, durable), idempotency by event id,
  membership, floors (the service enforces them after your delta), and the kill switch.

## Default logic spec (from legacy v2: `core/reputation_module`, `svc_process/services/activity_accrual.py`)

The accrual bundle reproduces the legacy BEHAVIOR (values configurable per community in the bundle's kv key
`accrual`; absent = defaults, present-but-invalid = loud skip):

### Legacy v2 defaults

| Event | Weight | Notes |
|---|---|---|
| chat_message | +1 | per-user cooldown 60 s |
| command_usage (`!...`) | +1 | cooldown 10 s |
| giveaway_entry | -1 | dissuade giveaway bots |
| follow | +1 | |
| subscription / tier2 / tier3 | +5 / +10 / +20 | **clamped to the activity cap +5 here** |
| gift_subscription | +3 per gift | multiplier `max(1, total)` |
| donation | +1 per dollar | |
| cheer | +1 per 100 bits | multiplier `bits/100`, half-away rounding once |
| raid | +2 | |
| boost | +5 | |
| warn / timeout / kick / ban | -25 / -50 / -75 / -200 | moderation class, to the moderated user |

Scale 300/850, start 600, tiers poor/fair/good/very_good/exceptional at 300/580/670/740/800. Delta =
`round_half_away_from_zero(weight x multiplier)`, rounded once. Legacy low-score rule: score `<=` 450 and above the
auto-ban threshold records a `low_score_warning` at most once per 24 h (the accrual bundle logs one PII-free WARN
per user per 24 h; `low_score_notice_at`, default 450). Legacy auto-ban threshold 450, disabled by default, is now
the two-floor model above (also disabled by default; community defaults hard 400, soft 550, at-risk 600; tenant 350).
Legacy has NO decay and NO tier-change notification: neither is implemented.

### Intentional deviations

* Activity deltas are clamped to +/-5 per call and the per-user daily activity ceiling (5) applies: legacy
  uncapped chat accrual and the +10/+20 sub tiers are bounded by the host caps (catalog quotas can be raised later).
* Auto-ban ships OFF; floors act through the service (soft ban / platform ban), not the accrual bundle.
* The per-community tenant-contribution cap (+/-100) is deferred to v3.1.x (#1235).
* Only `moderation.ban|timeout` (the host's verified system allowlist) apply moderation deltas; `warn`/`kick` have no
  verified source and are skipped (`unverified_moderation`). The moderated user resolves through the host-verified
  mention bindings, never a raw payload id; moderation weights must be negative and the delta is forced negative;
  a POSITIVE moderation delta needs a verified community admin (service tool and host). These events are not yet
  delivered inbound by svc-ingest (only outbound `mod.*` sender ops exist); EventSub `channel.ban` is not subscribed.

## Observability

Host store: `waddles_bundle_reputation_call_duration_seconds{op,outcome}` (histogram; ops include `apply`,
`get_tenant`, `history`, `below_threshold`, `is_soft_banned`, `soft_ban`, `lift_soft_ban`),
`waddles_bundle_reputation_calls_total{op,outcome}`, `waddles_bundle_reputation_delta_abs`. Logs carry tool name +
outcome only (no user input, no UUIDs). The accrual bundle reports `reputation.accrual.events{outcome,class}`.

## Tests

Host store + SQL contract (stub `reputation_apply`) `core/bundle_host_reputation/tests/postgres_integration.rs`
(Docker Postgres); stage handlers `core/svc_process/src/capabilities_reputation_tests.rs`; identity/system-origin
`core/svc_process/src/identity.rs`; service tools `bundles/rust/reputation/src/logic/tests.rs`; real-path e2e
`core/tool_invoke_e2e/tests/reputation_service_e2e.rs` (real wasm + executor + `tool.invoke` + gate). The e2e
fixture `core/tool_invoke_e2e/tests/fixtures/reputation_service.wasm` must be rebuilt when the bundle changes (see
the README beside it).

## SQL contract (v3.0.X, PR #1236)

The host never writes reputation tables: every write is the SECURITY DEFINER SQL function
`reputation_apply` (membership, caps, idempotency, clamp, ledger all enforced in SQL; breaches
map to `rate_limited`, non-members to `not_a_member`). Consequences for the bundle role:

- `override` (admin_override, class 3) and any **positive** moderation delta are denied by the
  SQL to this role, so the host answers `forbidden` for everyone, admin scope or not -- admin
  override and score restores are hub-api only.
- Soft bans: only the score-floor ban (`community_soft_floor`) can be set or cleared, via
  `reputation_set_floor_soft_ban` / `reputation_clear_floor_soft_ban`. Manual soft bans are
  hub-api only (a bundle gets `forbidden`).
- Activity-class writes (`adjust`, `adjust_many`, `apply`) are allowed only when the call
  chain's origin is the tenant's single active reputation-logic app
  (`reputation_active_logic_app`; none or ambiguous fails closed) or the core service itself.
- Unscored members read as `COALESCE(balance, community policy score_default, 600)` everywhere.
- **Automated hard ban** (`waddles.reputation-auto-ban`, default OFF): a floor ban
  (`community_hard_floor` / `tenant_floor`) through `<platform>.moderation.ban` carries the
  host-asserted automated actor `system:reputation_floor` (scope `community:moderate` only), attached by
  `svc_process` `capabilities/floor_ban.rs` ONLY when the caller is the core reputation service reached as the
  chain `[active reputation-logic app, service]`, a reputation apply already succeeded in the same invocation
  (host-tracked on the invocation identity; fresh on every hop, never inherited), and the flag is ON.
  Anything else falls through to the ordinary human-scope check (`forbidden`). Per community the host claims
  an idempotency/rate-limit slot (`reputation_floor_ban_begin`, 60/h default, `floor_ban_hourly_quota`),
  re-verifies the breach in SQL (`reputation_verify_floor_breach`: scope switch enabled AND score below
  floor), runs the ban through the normal per-community grant check, and records the outcome
  (`reputation_floor_ban_record`; table `bundle_reputation_floor_bans` is the audit trail: actor, reason,
  scope, community, target UUID, platform, status). The tenant floor fans out over every live community of the
  tenant (`reputation_tenant_communities`, 4 at a time); failures are durable retries (exponential backoff,
  8 attempts, picked up by `reputation_floor_ban_due` on the next floor call), a partial fan-out is reported
  as `target_unavailable`, never as success. Limit: the ban uses the triggering event's platform; the
  per-community platform-connection enumeration is a follow-up, so a community without that platform's ban
  grant is recorded as a failed (retried) attempt.
- Migrations: `alembic/versions/0072_reputation_host_ops.py` (score clamp CHECK + ledger columns),
  `alembic/versions/0098_reputation_floor_ban.py` + `scripts/db/bundle_reputation_floor_ban.sql`
  (automated hard ban; tests `alembic/tests/test_0098_reputation_floor_ban.py`),
  `alembic/versions/0097_reputation_unify.py` + `scripts/db/bundle_reputation_unify.sql` (PR #1236, authoritative).

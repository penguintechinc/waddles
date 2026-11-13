# waddles.core.service.reputation

Always-on `kind: service` bundle (Rust, `world stage-tools`) that will own community
reputation behind tool calls (bundle-MCP-tools P5). It exports `tool-provider` and holds the
`reputation` host capability (`reputation.read`, `reputation.community.write`,
`reputation.community.admin`) plus `storage.kv` for delta-policy/tier config.

v1.1.0: the service owns the store, tools, soft-ban state and FLOOR ENFORCEMENT (soft/hard community floors + tenant
floor, behind flag `waddles.reputation-auto-ban`, default OFF). It holds NO scoring weights: the default logic app
bundle `waddles.core.app.reputation-accrual` (or an approved replacement that `provides: reputation-logic`) names the
deltas. Tools, floors, caps and the third-party contract: `docs/bundle-reputation-service.md`.

Tools: `get_score`, `get_many`, `rep`, `leaderboard` (community only), `adjust {user, delta, reason, platform?}`,
`apply_moderation` (dangerous), `override` (admin), `get_tenant_score` (own), `history` (own/admin), `check`,
`at_risk` (admin), `is_soft_banned`, `get_policy`/`set_policy` (admin). Config (bundle kv, per community):
`floors` (soft/hard enabled+floor, at_risk_threshold; defaults all disabled, 550/400/600), `tiers`.

Build: `cargo component build --release --target wasm32-wasip2 --locked`
(the core-bundle image does this in `bundles/Dockerfile.core-bundles`).

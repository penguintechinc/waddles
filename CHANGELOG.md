# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- GDPR Art. 17 erasure now covers every user-keyed table, not ~16 of ~121 (GRC audit HIGH): a declarative erasure registry
  (`hub_api/services/erasure_registry.py`, run by `erasure_executor.py` inside the single erasure transaction) deletes the
  subject's rows in 75 further tables, scrubs their PII on rows kept for others, and records a reasoned decision for every
  remaining user-keyed table (pseudonymous actor reference, legal retention, exempt, or a listed gap). Also erased: economy
  ledgers/balances, per-user bundle KV in Valkey, the uploaded avatar/banner in SeaweedFS, and `sso_identities.subject`. External
  stores fail loud (the erasure rolls back and is retryable). The suppression-only design (#804) is unchanged and erasure
  stays ungated in every tier. A CI gate (`tests/test_erasure_registry_coverage.py`) scans the migration chain and fails when a
  user-keyed table/column has no rule, so a future table cannot silently regress erasure. See `docs/PRIVACY.md`.

### Fixed
- CI verification integrity -- gates that could not fail now can: `scripts/lib/checks.sh` counters no longer turn a crashed
  scanner into "0 findings" (`tool_capture`, non-numeric counter = FAIL); beta `rollout status` + smoke steps in `ci-cd.yml` and the
  `security-scan` job in `containers.yml` no longer masked (`|| true` / `|| echo` / `continue-on-error`); `.checks-baseline`
  ceilings re-measured and lowered; pytest suites now fail below 90% line coverage (`.coveragerc` `fail_under` + `addopts`);
  seeded-failure proof in `scripts/ci/tests/test_checks_gate_integrity.sh` (`make test-gate-integrity`, CI job `gate-integrity`)

### Added
- Ban-evasion suppression (GDPR Art. 6(1)(f), counsel to verify): an erased user with an ACTIVE ban leaves one per-tenant keyed
  one-way code (`suppression_records`, migration `0059_suppression_records`; HMAC-SHA256 under an HKDF key derived per tenant
  from the platform tenant KEK, closed `ban`/`auto_ban` enum, expiry) and a returning banned user is re-caught at join approval,
  OAuth signup and identity link. Operator tooling `make suppression-check|lift|purge|stats`; chart `suppression.maxRetentionDays`;
  draft `docs/compliance/suppression-LIA.md` / `suppression-DPIA.md`
- Standard provider/connection framework (hub-api), Twitch first: per-community connection API (`/api/v1/communities/{id}/provider-connections`, install descriptors at `/api/v1/connection-providers`), provider adapter interface + registry, Twitch adapter (bot account, channel resolution, refresh), AES-256-GCM encrypted per-community token store with refresh-under-lock, and machine-JWT data-plane routes (`/api/v1/internal/provider-connections`, `.../resolve`) so `svc-ingest` can resolve connections dynamically instead of static env. Scope `connections:admin`, flag `waddles.integrations.provider_connections` (Professional, default OFF). Migration `0058_provider_connections`. See `docs/PROVIDER_CONNECTIONS.md`.
- Real OTel metrics for the Rust data plane (replacing the `penguin_spine::NoopMetrics` stubs and the executor's
  unexported atomics): svc-process / svc-action emit stage latency, executor-call and host-call latency histograms,
  bundle load, drain batch size, consumer-group backlog, sandbox trips, dispatch attempts, DLQ / tenant-boundary counters;
  bundle-executor emits load phases (fetch / verify / precompile), invoke / guest / instantiate latency, host-call round
  trips, sandbox trips, resident-bundle and in-flight gauges and host-API session health, exported over OTLP/gRPC **only**
  when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (default off: no new executor egress). The capability gate's grant/denial
  counters (`waddles_bundle_capability_{authorized,denied}_total`), which were silently dropped by an uninstalled
  `metrics`-facade recorder, now export through an OTel-backed recorder. New dashboard `waddles-stage-pipeline`. See
  `docs/observability/data-plane-metrics.md`.
- Star Citizen Spectrum org sync (gh #101, Bar Citizen): svc-ingest now diffs a Spectrum community's member roster and event
  list into change events -- `member_joined` / `member_left` / `member_roles_changed` and `org_event_created` / `_updated` /
  `_cancelled` / `_removed` / `_rsvp_changed` -- under the new `spectrum.org` consume tag. Valkey-backed snapshots survive
  restarts (downtime changes still detected), a shrink guard holds back RSI glitches instead of emitting mass departures,
  rosters are fetched complete-or-fail, delivery is at-least-once. Behind a second flag `waddles.spectrum-org-sync`
  (default OFF, needs `waddles.spectrum-integration` too); one-way inbound only. See `docs/integrations/spectrum.md`.
- Enterprise SSO for hub-api: SAML 2.0 and OpenID Connect (Enterprise tier, `waddles.auth.sso_saml`) and Google OAuth2 (Professional tier, `waddles.auth.sso_google`). Per-tenant connections with admin API, JIT provisioning keyed on (connection, subject) with no email adoption, PKCE + nonce + local ID-token validation, signature-wrapping/replay/XXE-hardened SAML SP, SSRF-guarded IdP HTTP, AES-256-GCM secret storage, login-CSRF binder cookie, OTel metrics/traces. Migration `0049_sso_connections`; auto-provisioned `SSO_ENCRYPTION_KEY` (alpha/local) that never fails the chart elsewhere. See `docs/SSO.md`.
- Enterprise tamper-evident audit logging (GRC audit finding #3): append-only per-tenant SHA-256 hash chain
  (`audit_events`, migration `0053_audit_events_hash_chain`), `GET /api/v1/compliance/audit/{events,head,verify,export}`,
  `make verify-audit-chain` / `make verify-audit-export`, new Enterprise feature `waddles.compliance.audit_export`
  (see `docs/compliance/audit-logging.md`)
- Audit coverage for authz denials, tenant/role changes, DSAR/erasure, logins (password, OAuth/SSO, passkey, refresh),
  admin actions and license/subscription changes via an app-wide hook (`hub_api/services/audit_http.py`)
- `flask_core.authz.AuthzDecision`: `require_scope` publishes its verdict as `request.authz_decision`

### Security
- JWT verification hardening, H-2 Phase 0 + MED-5 (`docs/JWT_VERIFICATION.md`): one algorithm per verifier
  (`HS256` platform, `EdDSA` service, asymmetric allow-list for OIDC ID tokens), `alg: none` and
  `jku`/`jwk`/`x5u`/`x5c`/`crit` headers rejected, `kid` charset pinned; `iss`/`aud` now ENFORCED (previously a
  token without them still verified) and `sub iss aud iat exp scope tenant` are mandatory; the
  `TENANT_CLAIM_MIGRATION_CUTOFF` default-tenant fallback is removed (a token with no `tenant` is rejected);
  an empty signing secret is refused at mint and at verify. Minted HS256 tokens carry a `kid` header
  (`JWT_KID`, default `hs256-v1`). New metrics `waddles_jwt_verifications_total{verifier,alg,outcome}` and
  `waddles_jwt_verification_seconds`. Minting stays HS256 (asymmetric cutover is a later phase).
  `/internal/service-token` no longer decodes its own token with signature verification disabled
  (`ServiceJwtIssuer.issue_with_claims`).
- Session revocation + short-lived access tokens (`docs/TOKEN_REVOCATION.md`, SOC 2 / OWASP session
  management): a server-side revocation store (`flask_core.token_revocation`; Valkey behind a 5 s
  local cache, bounded grace then fail closed on an outage) is consulted on **every**
  `verify_jwt_token` and rejects a token before its `exp` by `jti`, by login session `sid`, or by a
  per-user `iat` cutoff ("revoke all"). Access tokens now live **15 minutes**
  (`JWT_ACCESS_TTL_SECONDS` 300-900), carry a random `jti` (+ `sid` for login sessions), nothing may
  live over 1 h (refused at mint **and** at verify, `lifetime_exceeded`), and are renewed by a
  rotating HttpOnly refresh token (`wb_refresh`) with reuse detection, a 10 s parallel-refresh
  grace, 7 d idle / 30 d absolute limits and a digest (not the bearer) in `hub_sessions`. Triggers:
  logout, `POST /auth/logout-all`, password change (all sessions), passkey removal / identity unlink
  (other sessions), user deactivate / delete / super-admin demotion / DSAR erasure, and the new
  superadmin `GET .../users/{id}/sessions`, `POST .../revoke-sessions`,
  `POST .../sessions/{sid}/revoke`. New metrics `waddles_jwt_revocation_*` and
  `waddles_hub_session_events_total`; audit action `authn.session_revoked`. **Deploy note:** 24 h
  tokens minted before this are refused - every browser session signs in once. `/auth/refresh` now
  needs the refresh cookie (an access token no longer renews itself). The Rust verifiers must check
  the same store (paired follow-up; contract in the doc).

### Changed
- **Erasure keeps nothing for a user without an active ban.** `community_members` rows (with their reputation), `reputation_events`,
  `reputation_tenant`, bundle reputation, the `ephemeral_pseudonyms` mapping and `hub_user_identities` are now DELETED instead of
  nulled-and-retained (supersedes the "retained reputation / identity anchor" posture); a returning user starts fresh. The admin
  DSAR console answers a repeat erase of an already-erased user with `not_found`
- `platform_integrations_crypto` raises `PlatformCredentialKeyError` (a subclass of `PlatformCredentialCryptoError`) for an unset/malformed key, so callers can tell a configuration fault from a tampered ciphertext
- `compliance.audit_logs` now requires scope `compliance.audit:admin` (was `compliance.audit:read`, which every session's
  `*:read` wildcard satisfied); the tenant-owner scope bundle grants it
- `bundle_audit.record` is fail-loud: a failed `audit_log` insert raises `AuditWriteError` instead of being swallowed

### Fixed
- Legacy `config/postgres` SQL migrations 097/099/100/101/102 never reached existing databases (the baseline replays
  SQL only on a fresh DB). Alembic revision `0060_port_legacy_sql_097_102` re-applies them idempotently and ledgers them
  in `schema_migrations`; `alembic/tests/test_legacy_sql_ownership.py` now fails CI for any un-bridged `NNN_*.sql`
  (see `docs/DATABASE.md` "SQL vs Alembic ownership")
- Legacy SQL `103_streaming_codec_columns` never reached existing databases either, so `streaming_configs.video_codec`/`audio_codec`
  and `streaming_targets.protocol`/`video_codec`/`audio_codec` were missing there (svc-streaming HEVC/AV1 + SRT targets broken).
  Alembic revision `0061_port_legacy_sql_103_codec` adds them idempotently and ledgers 103; `PENDING_PORT` is now empty
- `AAALogger.audit()` / `.auth()` call sites that did not match the real signature (`audit(action, user, community, result, **extras)`) raised `TypeError` on their SUCCESS path, after the work was already done: 91 `audit` calls across analytics-core (incl. `HealthService.calculate_health_score` and the `bot_score_calculator` batch job), reputation, security-core, labels, the interactive action modules, the consolidated `services/*` apps and the trigger receivers, plus two `auth` calls on the Kick webhook invalid-signature path. Visible effects included 500s after committed writes, `disable_auto_ban` / `update_weights` reporting failure after committing, an auto-ban applied on the platform but reported `success=False`, and `SpamDetector.check_spam` failing OPEN for spam it had just detected. The message text moved to `detail=`; the actor is the caller's opaque id where the handler has one, else the literal `"system"`. New `libs/flask_core/tests/test_aaa_logger_call_sites.py` executes every `logger.audit()` / `.auth()` call in the repo against the real `AAALogger`, so a mis-signatured call can no longer merge.
- Removed the `except Exception: pass` that silently dropped audit-log write failures (`bundle_audit.record`), and the two
  sibling swallows on the consent-proof trail (`cookie_consent_service.log_audit_event`) and the failed-deletion record
  (`data_privacy_service`); all now go through `audit_service.report_write_failure`
- Legacy `audit_log` bundle-lifecycle rows were never written on real Postgres: the insert passed a tz-aware `datetime`
  for a plain `TIMESTAMP` column, asyncpg rejected it, and the swallowed error hid it. The value now matches the column
- Bundle flows with required follow-on work (global uninstall cascade, tenant un-availability cascade, tenant-wide/community
  activation signing + Valkey provisioning, tenant permission restriction invalidations) finish that work before an audit
  failure is raised (`bundle_audit.DeferredAudit`), so fail-loud cannot leave the platform half-changed
- **Mod-gate bypass in 40 app bundles:** `bool(is_mod)` / `bool(payload.get("is_mod"))` treated the string badge `"false"` as truthy, so a non-moderator whose normalizer emitted a string passed every moderator gate, and `transform()` laundered the string into a real `True` for `dispatch`. All badge reads are now strict identity checks (`is True`, fail closed on anything else); versions bumped. New CI gates: `check-bundle-source-hygiene.py --check badge-truthiness` and `scripts/ci/tests/test_bundle_badge_gate.py`
- **Mod-gate bypass in the legacy ingest path:** `builtin_handlers/{kick,youtube_live,twitch}_ingest.normalize` read role flags with `bool(raw.get("is_mod"))` (also `is_subscriber`/`is_vip`/`is_broadcaster`/`is_owner`/`is_sponsor`), and the Kick/YouTube receivers did the same on the upstream payload. A raw event is decoded from Valkey JSON, so the string `"false"` is truthy and promoted a non-moderator to a moderator downstream. All role flags now go through `receivers.badge_flags.strict_flag` (real `True` only, fail closed, non-boolean values logged by platform/field/type without the value). The `badge-truthiness` CI gate (`check-bundle-source-hygiene.py`) now also scans `core/svc_ingest/**` with its own denominator, and `make check-bundle-hygiene` runs it; regression: `core/svc_ingest/tests/test_badge_flags_regression.py`
- `poll` bundle (1.0.2) was dead (`ImportError: cannot import name 'create_dal'`, so it could neither load nor build); ported onto the structured `db` + `community_kv` APIs (one `poll_records` table, atomic kv tallies and one-vote markers), create/vote/close verified end to end. Refs #675
- `loyalty` (1.0.3) integrity: atomic first-grant claim (no lost/duplicate rows under concurrency), bounded amounts and a balance cap, accurate clamped replies, no junk rows from `sub`, caller identity normalized like a typed target, orphan-row compensation
- `inventory` (1.0.3) integrity: `give` no longer destroys the item when the credit fails (refund), per-user directory lock (no lost updates), item-name / distinct-item / quantity bounds, caller identity normalized, orphan-row compensation
- `wheel` (1.0.3): the storage-failure ERROR log carried the host's free-form error text and, for a corrupt blob, text derived from user-typed options; it now logs a static `op` and the exception class name only

## [2.2.0] - 2026-04-10

### Added
- Microservice consolidation: 51 containers reduced to 22 via blueprint aggregation (Quart apps combining related modules)
- 8 new consolidated service directories under `services/` (action-platforms, action-serverless, core-community, core-data, core-identity, interactive-gaming, interactive-media, interactive-productivity, interactive-social, trigger-streaming, trigger-webhooks)
- Vendor portal enhancements: user dropdown menu, vendor modules/analytics/settings pages (#105)
- Vendor discount codes: full CRUD API with atomic redemption, management UI with filters and pagination (#102)
- Vendor analytics dashboard: sales metrics, install time-series, discount code performance, CSV export (#104)
- Custom raffle sounds: per-community sound upload (mp3/ogg/wav), message templates with variable substitution (#111)
- GitHub Issues sync: bidirectional support ticket ↔ GitHub issue sync, AES-256-GCM token encryption, webhook signature verification (#106)
- AI knowledge base: pgvector embeddings for multi-source indexing (GitHub repos, URLs, text), confidence-scored ticket suggestions via Ollama (#107)
- 4 new PostgreSQL migrations (064–067): discount codes, raffle sounds, GitHub sync, AI knowledge
- Microsoft Teams trigger module with Adaptive Card support

### Changed
- Consolidated service architecture from individual module containers to grouped service containers
- E2E test infrastructure: centralized fixtures, shared helpers, global setup with auto-seeding

### Fixed
- Multiple service startup crashes resolved during beta deployment
- DAL URL format and botbuilder import issues
- Blueprint name collisions in interactive-gaming
- CI/CD workflow path detection for services/ directory

## [1.0.0] - 2025-12-16

### Added

#### Core Module System
- **Module SDK**: Complete module development kit enabling creation of custom modules
  - Lambda adapter for AWS Lambda function integration
  - Google Cloud Platform (GCP) adapter for serverless deployment
  - Apache OpenWhisk adapter for open-source serverless environments
  - Standardized module interface and lifecycle management
  - Module discovery and registration system

#### Data Processing Pipeline
- **Redis Streams Pipeline**: High-performance asynchronous event processing
  - Stream-based event architecture for reliable message delivery
  - Consumer group management for distributed processing
  - Backpressure handling and flow control
  - Integration with command processor for workflow orchestration

#### Music System
- **Unified Music System**: Consolidated multi-source music integration
  - Spotify integration with playback control and playlist management
  - YouTube Music support with search and streaming capabilities
  - SoundCloud integration for independent artist content
  - Radio station streaming with genre and station browsing
  - Unified playback controls across all providers
  - Playlist synchronization and cross-platform library management

#### Inventory Management
- **Quartermaster Inventory System**: Comprehensive item and inventory tracking
  - Item definition and cataloging system
  - Inventory state management per user/guild
  - Item crafting and combination mechanics
  - Inventory persistence and historical tracking
  - Item rarity and attribute systems

#### Community Engagement
- **Quote Management System**: Community quote collection and retrieval
  - Quote submission and moderation workflow
  - Advanced search and filtering capabilities
  - Quote ratings and trending system
  - Author attribution and context preservation
  - Daily quote delivery features

- **Loyalty System with Gamification**: Multi-game loyalty and progression system
  - **Dice Game**: Classic dice rolling with customizable rules
  - **Rock-Paper-Scissors (RPS)**: Head-to-head competitive gameplay
  - **8-Ball**: Magic 8-ball fortune telling experience
  - **Golden Ticket**: Prize wheel lottery system with configurable rewards
  - **PvP System**: Player-versus-player competitive matches with ranking
  - Experience and level progression
  - Reward distribution and claim management
  - Leaderboard tracking and seasonal resets

#### Workflow Automation
- **Workflow System**: Flexible automation and task orchestration
  - Workflow definition with YAML/JSON configuration
  - Expression engine for dynamic condition evaluation
  - Variable binding and state management
  - Action chaining and sequential/parallel execution
  - Error handling and recovery mechanisms
  - Workflow history and audit logging

#### Marketplace Platform
- **Marketplace Module**: Complete e-commerce ecosystem
  - Backend services for product and order management
  - Payment processing integration
  - Multiple payment provider support
  - Order fulfillment workflow
  - User and vendor management
  - Product catalog and listing system
  - Transaction history and reconciliation

#### User Interface
- **Hub WebUI Enhancements**: Improved administrative interface
  - Enhanced dashboard with activity overview
  - Module management interface
  - Advanced user management tools
  - System configuration panel
  - Real-time monitoring and analytics
  - Administrative translation configuration (AdminTranslation component)

#### Inter-Service Communication
- **gRPC Communication Protocol**: Efficient service-to-service communication
  - Protocol buffer definitions for services
  - Streaming and unary RPC support
  - Service discovery integration
  - Load balancing and health checks

#### Database
- **Database Migrations**: Seven new schema updates (migrations 011-017)
  - Migration 011: Core module SDK schema
  - Migration 012: Redis Streams pipeline tables
  - Migration 013: Unified music system tables
  - Migration 014: Quartermaster inventory schema
  - Migration 015: Quote management tables
  - Migration 016: Loyalty and games schema
  - Migration 017: Translation configuration schema

### Changed
- Refactored command processor to utilize Redis Streams pipeline
- Updated admin controller to support module management endpoints
- Enhanced admin routes with new configuration and management endpoints
- Improved browser source core module with caption overlay templates
- Updated API service client with gRPC support
- Enhanced dependencies in router module with translation service support

### Technical Details
- **Language Support**: Python, JavaScript/React, SQL
- **Infrastructure**: Redis, PostgreSQL, gRPC
- **Cloud Platforms**: AWS Lambda, Google Cloud Platform, Apache OpenWhisk
- **Architecture**: Microservices with event-driven processing

### Files Modified
- `admin/hub_module/backend/src/controllers/adminController.js`
- `admin/hub_module/backend/src/routes/admin.js`
- `admin/hub_module/frontend/src/App.jsx`
- `admin/hub_module/frontend/src/layouts/AdminLayout.jsx`
- `admin/hub_module/frontend/src/services/api.js`
- `core/browser_source_core_module/app.py`
- `processing/router_module/requirements.txt`
- `processing/router_module/services/command_processor.py`

### New Files Added
- `admin/hub_module/frontend/src/pages/admin/AdminTranslation.jsx`
- `config/postgres/migrations/007_add_translation_config.sql`
- `core/browser_source_core_module/templates/caption-overlay.html`
- `processing/router_module/services/translation_service.py`
- `processing/router_module/services/translation_providers/` (directory)
- `processing/router_module/services/test_translation_service.py`

### Documentation
- Added comprehensive test guides and API documentation for caption functionality
- Included quick reference guides for caption testing
- Added delivery summary and test scenario documentation

---

## [0.9.0] - Previous Release

### Previous Features
- Core Discord bot framework
- Basic command processing
- Database integration with PostgreSQL
- Admin panel with basic management
- Community management tools
- Chat moderation system

---

For more information about Waddles, please refer to the project documentation and contribution guidelines.

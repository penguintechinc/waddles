# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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

### Changed
- `compliance.audit_logs` now requires scope `compliance.audit:admin` (was `compliance.audit:read`, which every session's
  `*:read` wildcard satisfied); the tenant-owner scope bundle grants it
- `bundle_audit.record` is fail-loud: a failed `audit_log` insert raises `AuditWriteError` instead of being swallowed

### Fixed
- Removed the `except Exception: pass` that silently dropped audit-log write failures (`bundle_audit.record`), and the two
  sibling swallows on the consent-proof trail (`cookie_consent_service.log_audit_event`) and the failed-deletion record
  (`data_privacy_service`); all now go through `audit_service.report_write_failure`
- Legacy `audit_log` bundle-lifecycle rows were never written on real Postgres: the insert passed a tz-aware `datetime`
  for a plain `TIMESTAMP` column, asyncpg rejected it, and the swallowed error hid it. The value now matches the column
- Bundle flows with required follow-on work (global uninstall cascade, tenant un-availability cascade, tenant-wide/community
  activation signing + Valkey provisioning, tenant permission restriction invalidations) finish that work before an audit
  failure is raised (`bundle_audit.DeferredAudit`), so fail-loud cannot leave the platform half-changed

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

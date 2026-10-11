# WaddleBot Database Architecture & Module Ownership

This document serves as the single source of truth for database structure, module ownership, table access patterns, and role-based security in WaddleBot.

---

## 1. Supported Databases

| Database | Version | `DB_TYPE` Value | Use Case |
|----------|---------|---|---|
| **PostgreSQL** | 16.x | `postgresql` | Production primary (default) |
| **MySQL** | 8.0+ | `mysql` | Production alternative |
| **MariaDB Galera** | 10.11+ | `mysql` | High-availability clustering (WSREP + utf8mb4 required) |
| **SQLite** | 3.x | `sqlite` | Development / lightweight only |

**Default**: PostgreSQL 16. Set `DB_TYPE` environment variable to select database type.

---

## 2. Database Initialization Strategy

### Schema Sources

WaddleBot uses a **dual approach** for schema management:

#### SQL Migrations (Source of Truth)
- **Location**: `config/postgres/migrations/` (63 files)
- **Canonical schema definition** — all CREATE TABLE, ALTER TABLE, indexes, triggers, and RLS policies
- **Execution**: Run via `config/postgres/migrations/run-migrations.sh` on fresh database
- **Tracked by**: `schema_migrations` table (version number, timestamp, checksum)

#### Alembic (Python Service Migrations)
- **Location**: `alembic/` + `alembic/versions/`
- **Purpose**: Manages schema changes initiated by Python services (`flask_core` models)
- **Baseline migration** (`0001_baseline_from_sql_migrations.py`): On new deployments, delegates to SQL migration runner; on existing databases with `schema_migrations` table, no-ops (schema already exists)
- **Execution**: Manual `alembic upgrade head` (never auto-run on application startup)
- **Maintenance**: Python developers create new Alembic revisions for schema changes to `flask_core` models

#### Hub Module (Node.js)
- **Schema**: Defined entirely in SQL migrations (no ORM)
- **Database**: Uses raw `pg` (node-postgres) client
- **Initialization**: Inherits schema from SQL migrations; no separate initialization required

#### SQL vs Alembic ownership (existing databases)

The numbered SQL files are replayed **only** by `0001_baseline` on a *fresh* database; once `schema_migrations` has rows the baseline no-ops forever. A SQL file added after a DB was baselined therefore **never reaches an existing DB (alpha, beta, prod) by itself** -- the gap that left 097/099-102 unapplied until `0060` bridged them.

- **Prefer an Alembic revision** for any new schema change: it reaches every database.
- **If you add a numbered SQL file anyway** (legacy-table changes still do), ship in the same PR an Alembic revision that (1) re-applies the DDL idempotently (`IF NOT EXISTS`, `to_regclass`, a `pg_constraint` probe -- it must be a no-op on a fresh DB, where the baseline already ran the file) and (2) lists the file's version in a module-level `LEGACY_VERSIONS` tuple and inserts it into `schema_migrations(version)` (`ON CONFLICT DO NOTHING`).
- **Enforced**: `alembic/tests/test_legacy_sql_ownership.py` (no docker) fails for any SQL file numbered 084+ with no bridge. Known follow-ups sit in its `PENDING_PORT` list and must be removed when ported.
- **Ledger rule**: a DB with a populated `schema_migrations` is a real legacy DB -- a missing pre-097 table raises, ledger rows are written. A DB without one is the minimal CI bootstrap (`alembic/tests/pg_docker.py`): only self-contained DDL applies and no ledger is invented.
- **Downgrade** of a port removes its ledger rows only; the DDL stays (on a fresh DB it belongs to the baseline replay, and dropping tables would lose data).
- **Per-service DB roles** (`0055`) skip catalog tables that do not exist yet. After `alembic upgrade head`, the deploy-time `python3 scripts/db/service_roles.py reconcile --strict` grants the tables a port creates (097 `reputation_tenant`, 100 `overlay_view_credentials`, 101 `overlay_images`, 102 `caption_events`) and fails loud if any is still missing.
- **Overlay credentials write-through**: hub-api's overlay mint/rotate/disable (`hub_api/services/overlay_service.py`) writes `community_overlay_tokens` AND `overlay_view_credentials` (SHA-256 of the key, previous hash + `rotated_at` on rotate) in one transaction, and ensures `communities.overlay_code` (0057) is set -- svc-presentation only validates against `overlay_view_credentials`, so a mint that skips it hands out a dead key.

| SQL files | Alembic bridge |
|---|---|
| 084-096 | `0014_wave1a_bundle_seeds` |
| 097, 099-102 (098: ledger row only -- DDL is `0039_event_sync_enabled`) | `0060_port_legacy_sql_097_102` |
| 103 `streaming_codec_columns` | `0061_port_legacy_sql_103_codec` (downgrade drops the 5 columns + ledger row) |

### Initialization Flow

```
Fresh Database:
  1. $ config/postgres/migrations/run-migrations.sh
     → Applies all 63 SQL migration files in order
     → Creates schema_migrations tracking table
  2. Flask app starts → Alembic baseline migration detects schema_migrations exists → no-ops
  3. Hub module starts → Queries existing tables directly

Existing Database:
  1. Flask app startup → Alembic checks schema_migrations version
  2. If version < current → $ alembic upgrade head (manual operator)
  3. Hub module queries existing schema; read-only
```

---

## 3. Per-Module Database Accounts (36 Roles)

WaddleBot enforces **per-module database accounts** with permission templates. All 36 database roles are created during schema initialization (migration 031) and provisioned via `module_db_accounts` table (migration 034).

### Permission Templates

| Template | Applies To | Permissions |
|----------|---|---|
| `trigger_readonly` | Trigger modules (5) | SELECT servers, communities, modules; SELECT (id, username, is_active) on `hub_users`; USAGE sequences |
| `action_platform` | Action modules (7) | SELECT servers, communities; SELECT platform_integrations; SELECT (id, email, username, avatar_url, is_active) on `hub_users`; USAGE sequences |
| `interactive_standard` | Interactive modules (10) | SELECT servers, communities, modules; SELECT (id, username, is_active) on `hub_users`; USAGE sequences |
| `core_broad` | Core modules (9) | SELECT servers, communities, modules, commands; SELECT (id, username, email, is_active) on `hub_users`; USAGE sequences |
| `core_admin` | Core admin modules (2: analytics, security) | ALL PRIVILEGES on all tables and sequences |
| `minimal` | Minimal access (lambda, gcp) | SELECT communities, servers; USAGE sequences |
| `custom` | Dynamic modules | Per-module custom configuration |

### All 36 Database Roles

**Hub Admin (1)**
- `hub_admin` — Full access to all tables, schema operations, user management

**Router Module (1)**
- `mod_router` — Core command routing, broad read access to commands/aliases/configs

**Trigger Modules (5)** — Read-only event collection
- `mod_trigger_twitch`, `mod_trigger_discord`, `mod_trigger_slack`, `mod_trigger_youtube`, `mod_trigger_kick`

**Action Modules (7)** — Push to platforms
- `mod_action_twitch`, `mod_action_discord`, `mod_action_slack`, `mod_action_youtube`, `mod_action_lambda`, `mod_action_gcp`, `mod_action_teams`

**Interactive Modules (10)** — Feature integrations
- `mod_interactive_ai`, `mod_interactive_alias`, `mod_interactive_shoutout`, `mod_interactive_inventory`, `mod_interactive_calendar`, `mod_interactive_memories`, `mod_interactive_ytmusic`, `mod_interactive_spotify`, `mod_interactive_loyalty`, `mod_interactive_quote`

**Core System Modules (12)**
- `mod_core_labels`, `mod_core_browser_source`, `mod_core_identity` (user mgmt), `mod_core_ai_researcher`, `mod_core_workflow`, `mod_core_community`, `mod_core_reputation`, `mod_core_analytics`, `mod_core_security`, `mod_core_video_proxy`, `mod_core_engagement`, `mod_core_rtc`

**Credential Manager (1)**
- `mod_credential_manager` — Platform integration lifecycle, credential audit logging

### Dynamic Account Provisioning

**Table**: `module_db_accounts`
- Tracks all 36 module accounts, their permission templates, custom grants, and owned/readable tables
- Supports dynamic module registration without SQL

**Table**: `db_permission_templates`
- Stores all 7 permission templates; admins can create custom templates

**Functions**:
- `provision_module_db_account(module_name, db_username, module_type, template)` — Creates new role, applies grants
- `deactivate_module_db_account(module_name)` — Revokes login privilege (audit trail preserved)
- `rotate_module_db_password(module_name)` — Rotates password securely

---

## 4. Complete Table Inventory by Module

See **[Table Ownership Reference](architecture/table-ownership.md)** for the full detailed table inventory (100+ tables organized by owning module, with access patterns and data sensitivity notes).

**Quick summary of major module areas:**

| Module Group | Owning Modules | Key Tables |
|---|---|---|
| **Identity & Hub** | Hub, Identity Core | `hub_users`, `hub_admins`, `hub_sessions`, `hub_user_identities`, `hub_user_profiles`, `cookie_consent`, `user_access_tokens`, `community_access_tokens` |
| **Community Management** | Community Core | `communities`, `community_members`, `community_servers`, `community_roles`, `tenants`, `tenant_admins` |
| **Routing & Commands** | Router, Command System | `commands`, `command_aliases`, `entities`, `command_permissions`, `command_executions`, `rate_limits`, `coordination` |
| **Features: Gaming** | Loyalty, PvP, Simple Games, Giveaway | `loyalty_points`, `pvp_match_history`, `player_game_inventory`, `loyalty_simple_game_results`, `loyalty_giveaway_winners`, `golden_ticket_holders` |
| **Features: Engagement** | Shoutout, Quotes, Memories, Engagement | `shoutout_history`, `shoutout_templates`, `quotes`, `memories`, `reminders`, `community_polls`, `community_forms` |
| **Features: Media** | Calendar, Video Proxy, Music, Clips | `calendar_events`, `event_attendees`, `video_stream_configs`, `community_call_rooms`, `music_queue`, `clip_bookmarks`, `clip_highlight_reels` |
| **Features: Marketplace** | Marketplace, Vendor | `marketplace_modules`, `marketplace_submissions`, `marketplace_sellers`, `vendor_submissions`, `approved_vendor_modules`, `marketplace_reviews` |
| **Integrations & Platforms** | Credential Manager, Trigger/Action Modules | `platform_integrations`, `twitch_actions`, `discord_actions`, `slack_actions`, `youtube_oauth_tokens`, `spotify_tokens` |
| **Admin & Ops** | Security, Analytics, Admin | `activity_watch_sessions`, `activity_message_events`, `bot_detection_results`, `community_reputation_config`, `server_ban_sync`, `server_access_policies` |
| **Infrastructure** | Service Registry, Server Manager | `services`, `service_events`, `rcon_command_log`, `server_status_configs` |
| **Workflow & Automation** | Workflow Core, Vendor | `workflow_definitions`, `workflow_executions`, `workflow_node_executions`, `workflow_schedules`, `workflow_permissions` |

---

## 5. Row-Level Security (RLS) Policies

**RLS is enabled on 2 critical tables** to enforce module isolation:

### `platform_integrations` Table

**Purpose**: Stores credentials and configuration for Twitch, Discord, Slack, YouTube, Kick, Spotify, etc.

**RLS Policy**: FORCE — each role sees only its platform

| Role | Access | Condition |
|---|---|---|
| `hub_admin` | SELECT, INSERT, UPDATE, DELETE | All rows |
| `mod_credential_manager` | SELECT, INSERT, UPDATE, DELETE | All rows |
| **Trigger Modules** | SELECT | `platform = 'twitch'` AND `is_active = TRUE` (etc. per platform) |
| **Action Modules** | SELECT, UPDATE | `platform = 'twitch'` (etc. per platform) |
| `mod_interactive_spotify` | SELECT, INSERT, UPDATE, DELETE | `platform = 'spotify'` |
| `mod_interactive_ytmusic` | SELECT, UPDATE | `platform = 'youtube'` AND `integration_type = 'community_oauth'` |
| `mod_router` | SELECT | `is_active = TRUE` |
| `mod_core_analytics` | SELECT | All rows |
| `mod_core_security` | SELECT | All rows |

**Rationale**: Prevents Twitch module from accessing Discord credentials, etc.

### `credential_access_log` Table

**Purpose**: Audit trail for all credential access (who accessed what platform credential and when).

**RLS Policy**: Each module sees only its own entries

| Role | Access | Condition |
|---|---|---|
| `hub_admin` | SELECT | All rows (audit oversight) |
| `mod_credential_manager` | SELECT, INSERT | All rows (lifecycle mgmt) |
| **All other modules** | SELECT, INSERT | `db_user = current_user` (own entries only) |

**Rationale**: Modules cannot spy on other modules' credential access patterns.

### Column-Level Grants on `hub_users`

All modules have **restricted column access** on the user identity table:

| Role Group | Granted Columns |
|---|---|
| **Trigger modules** | `id`, `username`, `is_active` (identification only) |
| **Action modules** | `id`, `email`, `username`, `avatar_url`, `is_active` (display + contact) |
| **Interactive modules** | `id`, `username`, `is_active` |
| **Core modules** | Varies by function (identity module = full, security = all, analytics = all, workflow/engagement = id/username/is_active) |

**PII Protection**: No module can see `password_hash`, `mfa_secret`, or other sensitive auth data.

---

## 6. Database Access by Service Type

### Flask Backend (`services/flask_backend`)
- **Role**: `hub_admin` (or dynamically created per-module role via Alembic)
- **Access**: All tables
- **Pattern**: SQLAlchemy for schema initialization, PyDAL for runtime queries
- **Migrations**: Managed via Alembic (`alembic upgrade head`)

### Hub Module (`admin/hub_module`)
- **Role**: `hub_admin` (default, shared with Flask)
- **Access**: All tables
- **Pattern**: Raw SQL via `pg` (node-postgres) client
- **Migrations**: None (schema read-only; changes via SQL migrations only)

### Go Backend (if present)
- **Role**: Per-module account (determined by deployment config)
- **Access**: Restricted to owning module's tables + shared read-only tables
- **Pattern**: GORM/sqlx for database operations
- **Migrations**: Changes via SQL migration files

### Feature Modules (35 other modules)
- **Role**: One of the 36 predefined roles (e.g., `mod_interactive_loyalty`, `mod_trigger_twitch`)
- **Access**: Restricted to module's tables + RLS-enforced platform_integrations rows
- **Creds**: `DB_USER` / `DB_PASS` environment variables per module
- **Startup**: Each module connects at startup; RLS policies automatically enforce access

---

## 7. PII & Data Sensitivity

### Sensitive Tables (PII / Secrets)

**Do NOT backup to untrusted locations:**
- `hub_users` — password_hash, mfa_secret, email
- `platform_integrations` — oauth_token, refresh_token, api_key, secret (encrypted)
- `hub_user_identities` — third-party provider IDs
- `credential_access_log` — audit of sensitive credential access

**Backup Strategy**:
- Encrypt backups in transit and at rest
- Restrict restore access to `hub_admin` only
- Audit all backup/restore operations

### Data Deletion (GDPR)

- **Table**: `data_deletion_requests` — audit trail for right-to-be-forgotten requests
- **Function**: `request_user_deletion(user_id)` — initiates anonymization cascade
- **Retention**: No PII retained after anonymization; `user_id` UUID only

---

## 8. Connection Pooling & Performance

All services use **persistent connection pooling** to the database:

| Service | Pool Size | Timeout | Max Retries |
|---|---|---|---|
| Flask Backend | `(2 × CPU cores) + disk_spindles` | 30s | 5 |
| Hub Module | 10 | 10s | 3 |
| Feature Modules | 2–5 | 5s | 3 |

**Thread-Local Pattern** (Flask/multi-threaded services):
```python
thread_local = threading.local()

def get_db():
    if not hasattr(thread_local, 'db'):
        thread_local.db = DAL(db_uri, pool_size=10, migrate=False)
    return thread_local.db
```

**Async Pattern** (Quart/async services):
```python
async def handle_request():
    db = DAL(db_uri, pool_size=1)  # per-task instance
    try:
        # query
        db.commit()
    finally:
        db.close()
```

---

## 9. Cross-Reference Documentation

- **Column-Level Schema Detail**: [docs/architecture/database-schema.md](architecture/database-schema.md) — Every table, column, type, constraint, trigger
- **Full Table Ownership Inventory**: [docs/architecture/table-ownership.md](architecture/table-ownership.md) — All 100+ tables, owning modules, access patterns, migration source
- **Developer Guide**: [docs/standards/DATABASE.md](../standards/DATABASE.md) — PyDAL/SQLAlchemy patterns, multi-DB support, per-service accounts, PII tokenization
- **SQL Migrations**: [config/postgres/migrations/](../config/postgres/migrations/) — Source of truth for schema (63 files)
- **Alembic Migrations**: [alembic/](../alembic/) — Python service schema changes
- **Module Registry**: [admin/hub_module/](../admin/hub_module/) — Hub module schema initialization

---

## 10. Troubleshooting

### Common Issues

**Connection Refused**
- Check `DB_HOST`, `DB_PORT` in environment
- Verify database is running: `psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "SELECT 1"`

**Permission Denied (Role)**
- Verify module is using correct `DB_USER` (check `module_db_accounts` table)
- Check `is_active = TRUE` for the module's role
- Verify RLS policies: `SELECT * FROM pg_policies WHERE tablename = 'platform_integrations'`

**Schema Mismatch**
- Run `$ config/postgres/migrations/run-migrations.sh` on fresh database
- For Flask services: `$ alembic upgrade head`
- Check `schema_migrations` table for applied migration versions

**Slow Queries**
- Check table indexes: `SELECT * FROM pg_indexes WHERE tablename = 'commands'`
- Monitor connection pool saturation (logs show pool full errors)
- Profile with `EXPLAIN ANALYZE SELECT ...`

---

**Last Updated**: 2025-12-03
**Format Version**: 1.0
**Maintainer**: WaddleBot Database Team

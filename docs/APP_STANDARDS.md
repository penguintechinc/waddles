# Waddles Application Standards

This document defines local application standards that apply to Waddles development.

## Microservice Architecture (v2.2.x)

The v2.2.x release consolidated 51 containers into 22 services by grouping related modules into consolidated Quart applications:

**Consolidated Services** (in `services/` directory, 11 services):
- `action-platforms` — Twitch/Discord/Slack action modules
- `action-serverless` — Webhook/HTTP action modules
- `core-community` — Community, membership, join-requests modules
- `core-data` — Database access, schemas, migrations
- `core-identity` — Authentication, identity, user linking
- `interactive-gaming` — Gaming, leaderboard, loyalty modules
- `interactive-media` — Media, gallery, streaming modules
- `interactive-productivity` — Forms, calendar, inventory modules
- `interactive-social` — Social, reputation, shoutout modules
- `trigger-streaming` — Twitch/YouTube event receivers
- `trigger-webhooks` — Discord/Slack/generic webhook receivers

**Admin Services** (separate, 2 services):
- `hub-api` — Admin/management API (Python/Quart)
- `hub-webui` — Admin portal frontend (ExpressScript + ReactJS)

**Infrastructure** (separate):
- `migrations` — Database schema management container

Each consolidated service runs a single Quart app combining related module blueprints on port 8080.

## Container Naming Convention

All Docker containers, Kubernetes resources, and internal DNS references MUST follow this naming pattern:

```
<category>-<name>
```

### Categories

| Category | Description | Examples |
|----------|-------------|----------|
| `trigger` | Modules that receive external events (webhooks, IRC, polling) | `trigger-twitch`, `trigger-discord`, `trigger-slack` |
| `action` | Modules that push actions to external platforms | `action-twitch`, `action-discord`, `action-slack` |
| `core` | Core platform services (router, identity, etc.) | `core-router`, `core-identity`, `core-labels` |
| `hub` | Admin/management portal components | `hub-api`, `hub-webui` |
| `infra` | Infrastructure services (databases, caches) | `infra-postgres`, `infra-redis`, `infra-seaweedfs` |
| `ai` | AI-related services | `ai-ollama`, `ai-researcher` |

### Examples

| Old Name | New Name | Description |
|----------|----------|-------------|
| `waddlebot-twitch` | `trigger-twitch` | Twitch event collector |
| `waddlebot-twitch-action` | `action-twitch` | Twitch action pusher |
| `waddlebot-router` | `core-router` | Central event router |
| `waddlebot-postgres` | `infra-postgres` | PostgreSQL primary |
| `waddlebot-postgres-replica` | `infra-postgres-replica` | PostgreSQL read replica |
| `waddlebot-hub` | `hub-api` | Hub module API |
| `waddlebot-ollama` | `ai-ollama` | Ollama AI inference |

### Docker Compose Service Names

Service names in `docker-compose.yml` should match the container naming convention:

```yaml
services:
  trigger-twitch:
    container_name: trigger-twitch
    # ...

  action-discord:
    container_name: action-discord
    # ...

  core-router:
    container_name: core-router
    # ...
```

### Internal DNS References

When services communicate internally, use the service name (which follows the same convention):

```python
# Correct
ROUTER_API_URL = "http://core-router:8000"
DATABASE_URL = "postgresql://user:pass@infra-postgres:5432/waddlebot"

# Incorrect (old style)
ROUTER_API_URL = "http://router:8000"
DATABASE_URL = "postgresql://user:pass@postgres:5432/waddlebot"
```

### Kubernetes Resource Names

Kubernetes Deployments, Services, ConfigMaps, etc. should use the same naming convention:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: trigger-twitch
  labels:
    app: trigger-twitch
    category: trigger
```

### Benefits

1. **Clear categorization**: Immediately understand a service's role from its name
2. **Consistent sorting**: Services group naturally in listings (all triggers together, etc.)
3. **Easy filtering**: `docker ps --filter "name=trigger-*"` shows all trigger modules
4. **Self-documenting**: New team members can understand architecture from names alone

## Community Types

| Type | Description |
|------|-------------|
| `shared_interest_group` | Standard interest-based community (default) |
| `gaming` | Gaming community — streamers, guilds, esports |
| `creator` | Content creator community |
| `corporate` | Corporate or professional community |
| `workforce` | Internal team or department — workforce management, HR tools |
| `support` | Help desk / customer support community with ticket system |
| `other` | General-purpose community |

`workforce` communities are suited for internal teams, departments, and organisations. `support` communities gain access to the ticket system: `support_ticket_categories`, `support_tickets`, `support_ticket_comments` tables, admin dashboard, and member-facing ticket submission pages.

## Quartermaster — Inventory System

The Quartermaster is a community inventory management system. Admins manage named items with quantities; members can claim (check out) items and return them.

**Database tables (migration 014):**
- `inventory_items` — items with name, category, type, total/available quantity, metadata
- `inventory_checkouts` — active claims with user, quantity, due date, status
- `inventory_log` — immutable audit trail for all stock changes

**Admin routes:** `/admin/:communityId/inventory` — Items tab (CRUD + stock adjustments) + Claims tab (all active checkouts)

**Member routes:** `/community/:communityId/inventory` (browse + claim) and `/community/:communityId/inventory/my-items` (my active claims + return)

## Personal & Community Access Tokens (PAT / CAT)

Two token types for programmatic API access:

| Token | Purpose | Identity | Limit |
|-------|---------|----------|-------|
| **PAT** (`wdl_u_*`) | Personal scripts, CLI tools — acts *as the user* | User principal | 1 per user |
| **CAT** (`wdl_c_*`) | Service accounts, bots, integrations — acts as *the community* | Community principal | 5 standard / 10 premium |

PATs use optional scope ceilings (restrict what the user can do via API). CATs require mandatory OAuth2-style scopes from the `permission_scopes` catalog.

Auth middleware detects token type by prefix: `wdl_u_` → load user from `user_access_tokens`, `wdl_c_` → load community from `community_access_tokens`.

**CATs fail closed (security hotfix, 2026-11-27):** a CAT is never a user — `get_current_user_id` always 401s for one. `tenant_middleware` refuses CATs (401) on every route except those explicitly opted in with `flask_core.cat_auth.allow_cat(scopes=[...], community_from=...)` placed below it. An opted-in route enforces the declared scopes (or the explicit `CAT_SELF_ONLY` for token-introspection routes) and requires the route's target community — extracted by its declared `from_view_arg(name)` / `from_json_body(field)` / `cat_bound_community` (default: reject) — to equal the CAT's bound community. Handlers read `request.cat_principal` (community id, token id, scopes; no user id). Opted-in routes are pinned by `hub_api/tests/test_cat_fail_closed.py::CAT_ROUTE_ALLOWLIST`. Today: `GET /api/v1/communities/my` only.

**Desktop OAuth (`redirect_uri=waddles://oauth/callback`) requires PKCE S256:** the client sends `code_challenge` (+ `code_challenge_method=S256`) on `GET /api/v1/auth/oauth/<platform>` and `code_verifier` on `POST /api/v1/auth/exchange`. Exchange codes are single-use with a 60 s TTL.

**Admin route:** `/admin/:communityId/tokens` | **User route:** `/account/tokens`

## Signup Controls & Authentication

Platform-level signup settings are stored in `hub_settings` and managed via the Super Admin platform config page (`/superadmin/platform-config` → Signup & Auth tab):

| Setting key | Description |
|-------------|-------------|
| `allow_public_signup` | Enable/disable new user registration |
| `captcha_provider` | `none` \| `altcha` \| `recaptcha` \| `turnstile` |
| `captcha_site_key` | Public CAPTCHA site key |
| `captcha_secret_key` | Secret CAPTCHA key for recaptcha/turnstile (server-side only). Not used by `altcha` |
| `passkey_enabled` | Allow WebAuthn passkey login for existing users |

`altcha` (self-hosted proof-of-work) is keyed by the dedicated `ALTCHA_HMAC_KEY` env var (waddlebot-secrets, auto-generated by the Helm chart; never the app secret). Unset => `GET /api/v1/captcha/challenge` and registration verification fail closed (503). Challenges are single-use (Valkey `SET NX`), the challenge endpoint is limited to 20 requests/min per client IP, and the salt format is `<24hex>?expires=<unix>&`.

## Community Join Policy

Each community has a `join_mode` field on the `communities` table:

| Value | Behaviour |
|-------|-----------|
| `open` | Anyone can join immediately (default) |
| `approval` | Join request created; admin must approve before membership granted |
| `invite` | Self-join blocked; admins must directly add members |

Join requests are stored in `community_join_requests`. Admin review page: `/admin/:communityId/join-requests`.

## Form Results Visibility

Forms support a `results_visibility` field that controls who can view submitted responses:

| Value | Who Can See Results |
|-------|-------------------|
| `community` | All community members |
| `registered` | Any registered/logged-in user |
| `submitter_and_admins` | Only the person who submitted + community admins (default) |
| `admins` | Community admins only |

## Platform Command Architecture

All bot commands (Discord slash, Slack named slash, Twitch `!` prefix) use a **generic router-forwarding model**:

1. Platform receiver bot receives a command
2. Bot calls `POST {ROUTER_URL}/events` with a normalized event payload (platform, user_id, message, etc.)
3. The router looks up `commands WHERE command = $1` to find the module
4. Router dispatches to the module's `/api/v1/execute` endpoint
5. Module response is returned to the platform bot and formatted appropriately

**Adding a new command** only requires inserting a row into the `commands` table — no bot code changes needed.
`community_id = NULL` in the commands table means the command is a global default for all communities.

### Non-Disableable Modules (is_core = TRUE)

Only two modules cannot be disabled by community admins:

| Module | Reason |
|--------|--------|
| `identity` | Auth/user linking — disabling breaks all member authentication |
| `workflow` | Internal orchestration engine |

All other modules (including `loyalty`, `leaderboard`, `shoutout`, `reputation`) are community-disableable.
Attempting to disable a core module via the API returns HTTP 403.

### Context Resolution Order (per command)

When the router processes a command, the target community is resolved in this order:

1. **Per-user override** — Redis key `ctx:{platform}:{user_id}:{channel_id}` (TTL 24h), backed by `user_platform_context` table
2. **Channel/server default** — `community_servers` row with `is_primary = true` for this entity (Redis cached)
3. **Error** — "No community configured for this channel"

Security gate: per-user context switching is limited to communities with an **approved, active** `community_servers` link.

### Server/Channel ↔ Community Linking Handshake

Either side can initiate:

- **Platform-initiated**: Platform owner runs `/join <community>` → `server_link_requests` row with `initiated_by='platform'` → community admin approves in WebUI
- **Community-initiated**: Community admin uses "Request Link" form in Admin → Linked Servers → platform owner runs `/approve <community>`

Key schema fields in `server_link_requests`:
- `initiated_by`: `'community'` or `'platform'`
- `link_type`: `'standard'`, `'read_only'`, or `'announcement_only'`
- `status`: `'pending'`, `'approved'`, `'rejected'`

### Platform Ingestion (`core/svc_ingest/`)

> Note: The legacy v2 `platform_receiver` Python library has been retired. Platform ingestion now runs in Rust via `core/svc_ingest/` and connector bundles under `bundles/`.

📚 See [docs/platform-commands.md](platform-commands.md) for the full command reference.
📚 See [docs/server-linking.md](server-linking.md) for the server linking step-by-step guide.

### Migration Notes

When renaming containers:
1. Update `docker-compose.yml` service names and container_name
2. Update all internal DNS references (DATABASE_URL, API URLs, etc.)
3. Update Kubernetes manifests in `k8s/` directory
4. Update any hardcoded references in application code
5. Update monitoring/alerting configurations

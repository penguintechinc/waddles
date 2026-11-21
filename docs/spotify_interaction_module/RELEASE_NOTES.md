# Spotify Interaction Module — Release Notes

> Version history and change log for the Spotify Interaction Module.
> New releases are prepended to the top of this file.

---

## v0.1.0 — Initial Documentation Release

*Released: 2026-02-16*

### Summary

First documentation release for the `spotify_interaction_module`. This release
establishes the full documentation suite for the module's v2.0.0 codebase,
covering the OAuth 2.0 Authorization Code flow, token lifecycle management,
configuration reference, testing guide, and operational troubleshooting.

The module provides Spotify OAuth integration for the WaddleBot platform,
enabling each community to independently authorize against Spotify's Web API.
Token management (acquisition, storage, refresh, and revocation) is fully
implemented. HTTP route registration for the OAuth flow endpoints is the next
milestone (v0.2.0).

### Module Version at This Release

```
MODULE_NAME    = spotify_interaction_module
MODULE_VERSION = 2.0.0
MODULE_PORT    = 8026
```

### Module Status at v0.1.0

The `spotify_interaction_module` at code version `2.0.0` provides:

**Runtime**:
- Quart async ASGI application served by Hypercorn with 4 workers on port 8026
- Non-root Docker container running as the `waddlebot` user
- Python 3.12 runtime on `python:3.12-slim` base image
- Log output to `/var/log/waddlebotlog/`

**OAuth Implementation** (`services/oauth_service.py`):
- `SpotifyOAuthService` class implementing the full Authorization Code OAuth 2.0 flow
- `get_authorization_url(state, scopes)` — builds Spotify authorization URL with
  `client_id`, `response_type=code`, `redirect_uri`, `scope`, `state`, `show_dialog=true`
- `exchange_code_for_token(code, community_id)` — exchanges the authorization code
  for `access_token` and `refresh_token` via POST to `https://accounts.spotify.com/api/token`
- `refresh_token(community_id)` — refreshes the access token using the stored refresh token,
  preserving the existing refresh token if Spotify omits it from the response
- `get_valid_token(community_id)` — returns a valid access token, auto-refreshing
  if within `timedelta(minutes=5)` of expiry
- `is_authenticated(community_id)` — returns True if `get_valid_token` returns a token
- `revoke_token(community_id)` — hard deletes from `music_oauth_tokens`
- `_store_token(community_id, token_data)` — upserts token into PostgreSQL with
  `ON CONFLICT (community_id, platform) DO UPDATE` and `COALESCE` on `refresh_token`

**Default OAuth Scopes** (11 total in `DEFAULT_SCOPES`):
- `user-read-playback-state`
- `user-modify-playback-state`
- `user-read-currently-playing`
- `playlist-read-private`
- `playlist-read-collaborative`
- `playlist-modify-public`
- `playlist-modify-private`
- `user-library-read`
- `user-library-modify`
- `user-top-read`
- `user-read-recently-played`

**Configuration** (`config.py`):
- `Config` class with class-level attributes, read from environment via `os.getenv()`
- `load_credentials_from_db(db_connection)` — loads credentials from `platform_integrations`
  table (active Spotify bot integration record with `is_active = TRUE`)
- `start_credential_listener(redis_client)` — daemon thread subscribing to
  Redis pub/sub channel `credentials:spotify:bot:refreshed` for live credential rotation
- `_credential_lock: threading.Lock` for thread-safe credential state management
- Fallback to `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` environment variables
  when DB credential loading fails

**HTTP Endpoints** (currently implemented):
- `GET /health` — liveness probe (status, module, version)
- `GET /healthz` — readiness probe with subsystem checks
- `GET /metrics` — Prometheus-format metrics
- `GET /api/v1/status` — module operational status

**Testing** (`test-api.sh`):
- 6 test cases: GET /health, GET /healthz, GET /metrics, GET /api/v1/status,
  GET /api/v1/nonexistent (404), DELETE /api/v1/status (405)
- Supports `--url`, `--api-key`, `--verbose`, `--skip-auth` flags
- Exit code 0 on all pass, 1 on any failure

### Documentation Files Added in v0.1.0

| File | Description | Lines |
|---|---|---|
| OVERVIEW.md | Purpose, capabilities, OAuth flow summary, module quick reference | 176 |
| USAGE.md | Docker setup, OAuth walkthrough step-by-step, workflow examples | 331 |
| API.md | Complete endpoint reference: health, status, all /auth/* endpoints | 512 |
| ARCHITECTURE.md | Components, startup sequence, token lifecycle, security notes | 349 |
| CONFIGURATION.md | All env vars, Kubernetes ConfigMap/Secret, credential rotation | 471 |
| TESTING.md | pytest fixtures, aioresponses mocking, track/playlist fixtures, CI | 552 |
| TROUBLESHOOTING.md | OAuth failures, token expiry, scope errors, rate limiting, logs | 376 |
| RELEASE_NOTES.md | Version history (this file) | — |

### Key Source Files Documented

| File | Purpose |
|---|---|
| `action/interactive/spotify_interaction_module/app.py` | Quart app, `@app.before_serving` startup, blueprint registration |
| `action/interactive/spotify_interaction_module/config.py` | `Config` class, credential loading, Redis pub/sub listener |
| `action/interactive/spotify_interaction_module/services/__init__.py` | Services package init |
| `action/interactive/spotify_interaction_module/services/oauth_service.py` | `SpotifyOAuthService` full implementation |
| `action/interactive/spotify_interaction_module/requirements.txt` | quart, hypercorn, aiohttp, httpx, python-dotenv, pytest stack |
| action/interactive/spotify_interaction_module/Dockerfile | Python 3.12-slim build, non-root `waddlebot` user, 4 workers |
| `action/interactive/spotify_interaction_module/test-api.sh` | Bash API test suite with 6 test cases |

### Dependencies Introduced

| Package | Version Constraint | Purpose |
|---|---|---|
| quart | >=0.19.0 | Async Flask-compatible web framework |
| hypercorn | >=0.16.0 | ASGI server (replaces Gunicorn/Uvicorn) |
| httpx | >=0.27.0,<0.28.0 | Async HTTP client (available, aiohttp used in oauth_service) |
| python-dotenv | >=1.0.0 | `.env` file loading via `load_dotenv()` |
| pytest | >=7.4.0 | Test framework |
| pytest-asyncio | >=0.23.0 | Async test support |
| pytest-cov | >=4.1.0 | Coverage reporting |

The `aiohttp` library (used in `oauth_service.py` for async HTTP to Spotify) is
implicitly pulled as a dependency. It may be added explicitly to `requirements.txt`
in a future patch.

### Known Limitations at v0.1.0

**1. OAuth routes not yet registered in app.py**

`SpotifyOAuthService` is fully implemented in `services/oauth_service.py`, but the
route handlers for `/auth/login`, `/auth/callback`, `/auth/status`, `/auth/refresh`,
and `/auth/revoke` are not yet wired into the Quart `api_bp` blueprint in `app.py`.
The API.md documents these endpoints from their design intent. Registration is the
first priority for v0.2.0.

**2. aiohttp ClientSession created per request**

`exchange_code_for_token` and `refresh_token` each create a new `aiohttp.ClientSession`
via `async with aiohttp.ClientSession() as session:`. This is safe for current volume
but suboptimal for high throughput. A shared session stored in `app.config` should be
introduced in a future release.

**3. Synchronous PyDAL calls in async context**

`dal.executesql()` is a synchronous call made within `async def` handlers. Under
high concurrency, this can block the asyncio event loop. Migration to `asyncpg`
or wrapping with `loop.run_in_executor` is planned for a future version.

**4. aiohttp not in requirements.txt**

`services/oauth_service.py` imports `aiohttp` but `aiohttp` is not listed in
`requirements.txt`. It is currently resolved as a transitive dependency. This should
be made explicit in a future patch.

**5. No Spotify Web API call endpoints**

The module handles OAuth token lifecycle only. Downstream Spotify operations (search,
playback control, queue management, playlist CRUD) are delegated to the
`unified_music_module` (port 8051), which calls this module to obtain valid tokens.
Direct Spotify API proxy endpoints are planned for v0.3.0.

### Upgrade Notes

This is the initial documentation release covering code at version 2.0.0.
No database migrations or configuration changes are required for this release.
The code has not changed; only documentation has been added.

---

## Future Versions (Planned)

### v0.2.0 — OAuth Route Registration

**Target**: Register OAuth endpoints in app.py so the Authorization Code flow
is fully operational end-to-end from the HTTP layer.

Planned changes:
- Register `GET /api/v1/auth/login` — initiate OAuth, redirect to Spotify
- Register `GET /api/v1/auth/callback` — handle Spotify callback, exchange code
- Register `GET /api/v1/auth/status` — check community authentication state
- Register `POST /api/v1/auth/refresh` — force token refresh
- Register `DELETE /api/v1/auth/revoke` — revoke community authorization
- Add CSRF state token generation and validation (session or Redis-backed)
- Instantiate `SpotifyOAuthService` in `startup()` using resolved credentials
- Add `aiohttp` explicitly to `requirements.txt`
- Extend `test-api.sh` with OAuth endpoint test cases

### v0.3.0 — Spotify API Proxy Endpoints

**Target**: Expose commonly-used Spotify Web API calls through the module's REST API.

Planned endpoints:
- `GET /api/v1/search?q=...&community_id=...` — Spotify track search with caching
- `GET /api/v1/now-playing?community_id=...` — Current playback state
- `POST /api/v1/playback/pause` — Pause playback for community
- `POST /api/v1/playback/resume` — Resume playback
- `POST /api/v1/playback/skip` — Skip to next track
- `POST /api/v1/queue/add` — Add track URI to playback queue
- `GET /api/v1/history?community_id=...` — Recently played tracks

### v0.4.0 — Playlist Management

- `GET /api/v1/playlists?community_id=...` — List community playlists
- `GET /api/v1/playlists/{playlist_id}/tracks` — Get playlist tracks
- `POST /api/v1/playlists` — Create a new playlist
- `POST /api/v1/playlists/{playlist_id}/tracks` — Add tracks to playlist
- `DELETE /api/v1/playlists/{playlist_id}/tracks` — Remove tracks from playlist

### v1.0.0 — Production Ready

**Target**: Full-featured, production-hardened Spotify integration.

Planned improvements:
- `asyncpg` for non-blocking PostgreSQL operations
- Shared `aiohttp.ClientSession` with connection pooling
- Spotify now-playing broadcast to browser source overlay module
- Song request queue with per-user cooldowns and community queue limits
- Top tracks and listening history endpoints
- Comprehensive pytest coverage with `aioresponses` mocking
- OpenTelemetry tracing for Spotify API calls
- Proper Spotify API error codes mapped to WaddleBot error types
- Rate limit handling with `Retry-After` header respect and exponential backoff

---

## Contribution Notes

### Documentation Standards

This documentation follows the WaddleBot module documentation standard established
in `docs/plans/2026-02-16-module-documentation-standard.md`. Each module requires
8 documentation files covering:

1. OVERVIEW.md — Purpose, capabilities, quick reference
2. USAGE.md — Getting started, operational workflows
3. API.md — Complete endpoint reference
4. ARCHITECTURE.md — Internal design and data flows
5. CONFIGURATION.md — All environment variables
6. TESTING.md — Test strategy, fixtures, how to run
7. TROUBLESHOOTING.md — Common failures and resolutions
8. RELEASE_NOTES.md — Version history (this file)

### Adding a New Release Entry

When releasing a new version of the `spotify_interaction_module`:

1. Prepend a new section at the top of this file, above the most recent entry.
2. Use the format: `## vX.Y.Z — Brief Title`
3. Include the release date in ISO format: `*Released: YYYY-MM-DD*`
4. List all changes under these headings as applicable:
   - Breaking Changes
   - New Features
   - Bug Fixes
   - Performance Improvements
   - Configuration Changes (new env vars, defaults changed)
   - Database Migrations (if any)
   - Upgrade Notes (steps required when upgrading from the previous version)

### Versioning Policy

Follows semantic versioning aligned with WaddleBot platform conventions:

| Change Type | Version Component | Example |
|---|---|---|
| Breaking API change, removed endpoint | Major | 1.0.0 -> 2.0.0 |
| New endpoint or capability added | Minor | 0.1.0 -> 0.2.0 |
| Bug fix, security patch, documentation | Patch | 0.1.0 -> 0.1.1 |

The `MODULE_VERSION` constant in `config.py` must be updated with each release:
```python
MODULE_VERSION = '0.2.0'  # Update this
```

---

## Related Modules

| Module | Port | Relationship |
|---|---|---|
| `unified_music_module` | 8051 | Primary consumer of Spotify tokens |
| `router_module` | 8000 | Routes `/spotify/*` requests inbound |
| `browser_source_core_module` | 8027 | Consumes now-playing data for overlays |
| `youtube_music_interaction_module` | 8038 | Sibling music module for YouTube |

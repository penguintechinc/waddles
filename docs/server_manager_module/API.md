# Server Manager Interaction Module - API Reference

Complete documentation of all HTTP endpoints for the Server Manager Interaction Module, covering both the Python module's direct routes and the Hub Backend proxy routes exposed through `rconController.js`.

---

## Python Module Endpoints (Port 8098)

### Health & Status

#### GET `/health`

Check module health and operational status.

```bash
curl http://localhost:8098/health
```

**Response (200 OK):**
```json
{
  "status": "healthy",
  "module": "server_manager_interaction_module",
  "version": "1.0.0",
  "timestamp": "2026-02-24T10:30:00Z",
  "database": "connected",
  "uptime_seconds": 7200
}
```

#### GET `/metrics`

Performance metrics and statistics.

```bash
curl http://localhost:8098/metrics
```

**Response (200 OK):**
```json
{
  "requests_total": 4210,
  "requests_per_second": 8.7,
  "average_response_time_ms": 62.4,
  "rcon_connections_active": 3,
  "database_connections": { "active": 4, "idle": 6, "pool_size": 10 }
}
```

#### GET `/api/v1/status`

Quick operational status check.

```bash
curl http://localhost:8098/api/v1/status
```

**Response (200 OK):**
```json
{
  "status": "operational",
  "module": "server_manager_interaction_module"
}
```

---

### Backward Compatibility Routes

All routes from the prior `server_status_interaction_module` are preserved verbatim under `/api/v1/server-status/*`. Existing integrations require no changes.

---

### Server Manager Routes — `/api/v1/server-manager/`

All routes below are scoped by `community_id`.

---

#### POST `/<community_id>/connect-test`

Test connectivity to a server using provided credentials. Does not persist credentials.

**Request:**
```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/connect-test \
  -H "Content-Type: application/json" \
  -d '{
    "server_type": "rcon",
    "game_type": "minecraft",
    "host": "mc.example.com",
    "port": 25575,
    "password": "rcon_secret"
  }'
```

**Body Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `server_type` | string | Yes | `rcon`, `mumble`, or `teamspeak` |
| `game_type` | string | No | `rust`, `minecraft`, `cs2`, `ark`, `valheim`, etc. |
| `host` | string | Yes | Server hostname or IP |
| `port` | int | Yes | Connection port |
| `password` | string | Yes | Plaintext password (not stored) |

**Response (200 OK):**
```json
{
  "success": true,
  "latency_ms": 34,
  "server_info": "Minecraft 1.21.4 — 12/100 players"
}
```

**Response (400):**
```json
{ "success": false, "error": "Connection refused: host unreachable" }
```

---

#### POST `/<community_id>/command`

Execute an RCON command on a saved server. Logs to `rcon_command_log`.

```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/command \
  -H "Content-Type: application/json" \
  -d '{
    "server_id": 7,
    "command": "say Hello from WaddleBot!",
    "executed_by_user_id": 1001
  }'
```

**Body Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `server_id` | int | Yes | ID in `server_status_configs` |
| `command` | string | Yes | Raw RCON command string |
| `executed_by_user_id` | int | Yes | Hub user performing the action |

**Response (200 OK):**
```json
{
  "success": true,
  "response": "Broadcasting: Hello from WaddleBot!",
  "log_id": 5503
}
```

---

#### GET `/<community_id>/servers/<server_id>/status`

Get current server status (online/offline, player count, map, etc.).

```bash
curl http://localhost:8098/api/v1/server-manager/42/servers/7/status
```

**Response (200 OK):**
```json
{
  "server_id": 7,
  "online": true,
  "player_count": 14,
  "max_players": 100,
  "map": "Procedural Map",
  "game_time": "Day 3",
  "version": "Rust 2463"
}
```

---

#### GET `/<community_id>/servers/<server_id>/players`

List currently connected players.

```bash
curl http://localhost:8098/api/v1/server-manager/42/servers/7/players
```

**Response (200 OK):**
```json
{
  "server_id": 7,
  "players": [
    {
      "identifier": "76561198012345678",
      "name": "CoolGamer99",
      "duration_seconds": 3600,
      "ping_ms": 42,
      "reputation_score": 720
    }
  ]
}
```

---

#### POST `/<community_id>/servers/<server_id>/kick`

Kick a player from the server and log the action.

```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/servers/7/kick \
  -H "Content-Type: application/json" \
  -d '{
    "player_identifier": "76561198012345678",
    "reason": "Disruptive behavior",
    "actor_user_id": 1001
  }'
```

**Body Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `player_identifier` | string | Yes | Steam ID, UUID, or TS client ID |
| `reason` | string | Yes | Human-readable reason |
| `actor_user_id` | int | Yes | Hub user performing the action |

**Response (200 OK):**
```json
{ "success": true, "log_id": 891 }
```

---

#### POST `/<community_id>/servers/<server_id>/ban`

Ban a player and optionally trigger cross-server sync.

```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/servers/7/ban \
  -H "Content-Type: application/json" \
  -d '{
    "player_identifier": "76561198012345678",
    "reason": "Cheating",
    "actor_user_id": 1001,
    "sync_to_all_servers": true
  }'
```

**Body Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `player_identifier` | string | Yes | Player identifier |
| `reason` | string | Yes | Ban reason |
| `actor_user_id` | int | Yes | Hub user performing the action |
| `sync_to_all_servers` | bool | No | Propagate ban to other community servers |
| `notify_security_core` | bool | No | Send to `security_core_module` (default: true) |

**Response (200 OK):**
```json
{
  "success": true,
  "log_id": 892,
  "synced_servers": [7, 9, 11]
}
```

---

#### GET `/<community_id>/servers/<server_id>/channels`

List channels on a Mumble or TeamSpeak server.

```bash
curl http://localhost:8098/api/v1/server-manager/42/servers/12/channels
```

**Response (200 OK):**
```json
{
  "server_id": 12,
  "server_type": "mumble",
  "channels": [
    { "id": 0, "name": "Root", "parent_id": null, "user_count": 0 },
    { "id": 1, "name": "General", "parent_id": 0, "user_count": 5 },
    { "id": 2, "name": "Gaming", "parent_id": 0, "user_count": 3 }
  ]
}
```

---

#### POST `/<community_id>/servers/<server_id>/move`

Move a user to a different channel on a Mumble or TeamSpeak server.

```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/servers/12/move \
  -H "Content-Type: application/json" \
  -d '{
    "user_identifier": "CoolGamer99",
    "target_channel_id": 2,
    "actor_user_id": 1001
  }'
```

**Body Parameters:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `user_identifier` | string | Yes | Mumble username or TS client ID |
| `target_channel_id` | int | Yes | Destination channel ID |
| `actor_user_id` | int | Yes | Hub user performing the action |

**Response (200 OK):**
```json
{ "success": true }
```

---

#### POST `/<community_id>/servers/<server_id>/message`

Broadcast a text message to all users on a Mumble or TeamSpeak server.

```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/servers/12/message \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Server restart in 5 minutes!",
    "actor_user_id": 1001
  }'
```

**Response (200 OK):**
```json
{ "success": true }
```

---

#### GET `/<community_id>/servers/<server_id>/policy`

Retrieve the access policy for a server.

```bash
curl http://localhost:8098/api/v1/server-manager/42/servers/7/policy
```

**Response (200 OK):**
```json
{
  "server_id": 7,
  "policy_type": "reputation_threshold",
  "reputation_kick_threshold": 400,
  "reputation_ban_threshold": 320,
  "policy_data": {}
}
```

---

#### PUT `/<community_id>/servers/<server_id>/policy`

Create or update the access policy for a server.

```bash
curl -X PUT http://localhost:8098/api/v1/server-manager/42/servers/7/policy \
  -H "Content-Type: application/json" \
  -d '{
    "policy_type": "reputation_threshold",
    "reputation_kick_threshold": 420,
    "reputation_ban_threshold": 340
  }'
```

**Response (200 OK):**
```json
{
  "success": true,
  "server_id": 7,
  "policy_type": "reputation_threshold",
  "reputation_kick_threshold": 420,
  "reputation_ban_threshold": 340
}
```

---

#### POST `/<community_id>/servers/<server_id>/enforce`

Trigger an immediate reputation enforcement pass on all connected players.

```bash
curl -X POST http://localhost:8098/api/v1/server-manager/42/servers/7/enforce \
  -H "Content-Type: application/json" \
  -d '{ "actor_user_id": 1001 }'
```

**Response (200 OK):**
```json
{
  "success": true,
  "players_evaluated": 14,
  "players_kicked": 1,
  "players_banned": 0,
  "actions": [
    {
      "player": "ToxicUser123",
      "action": "kick",
      "reputation_score": 385
    }
  ]
}
```

---

#### GET `/<community_id>/servers/<server_id>/access-log`

Retrieve the kick/ban action audit log for a server.

```bash
curl "http://localhost:8098/api/v1/server-manager/42/servers/7/access-log?limit=50"
```

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | Max records returned |
| `offset` | int | 0 | Pagination offset |
| `action` | string | — | Filter: `kick`, `ban`, `unban` |

**Response (200 OK):**
```json
{
  "server_id": 7,
  "total": 23,
  "entries": [
    {
      "id": 892,
      "actor_user_id": 1001,
      "target_player_identifier": "76561198012345678",
      "action": "ban",
      "reason": "Cheating",
      "auto_enforced": false,
      "reputation_score": null,
      "created_at": "2026-02-24T09:14:00Z"
    }
  ]
}
```

---

## Hub Backend Proxy Routes (`rcon.js`)

The hub backend (admin/hub_module/backend) exposes these routes, which proxy to the Python module after JWT auth and community scoping.

Base path: `/api/v1/:communityId/rcon`

### Admin Routes (require admin role)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/:communityId/rcon/servers` | List all configured servers |
| `POST` | `/:communityId/rcon/servers` | Add a new server (encrypts credentials) |
| `PUT` | `/:communityId/rcon/servers/:serverId` | Update server configuration |
| `DELETE` | `/:communityId/rcon/servers/:serverId` | Remove a server |
| `POST` | `/:communityId/rcon/servers/:serverId/test` | Test server connection |
| `POST` | `/:communityId/rcon/servers/:serverId/command` | Execute RCON command |
| `POST` | `/:communityId/rcon/servers/:serverId/kick` | Kick a player |
| `POST` | `/:communityId/rcon/servers/:serverId/ban` | Ban a player |
| `GET` | `/:communityId/rcon/servers/:serverId/channels` | List voice channels |
| `POST` | `/:communityId/rcon/servers/:serverId/move` | Move voice user |
| `POST` | `/:communityId/rcon/servers/:serverId/message` | Broadcast voice message |
| `GET` | `/:communityId/rcon/servers/:serverId/log` | RCON command log |
| `GET` | `/:communityId/rcon/servers/:serverId/policy` | Get access policy |
| `PUT` | `/:communityId/rcon/servers/:serverId/policy` | Set access policy |
| `POST` | `/:communityId/rcon/servers/:serverId/enforce` | Run enforcement pass |
| `GET` | `/:communityId/rcon/servers/:serverId/access-log` | Access audit log |

### Member Routes (require authenticated member)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/:communityId/rcon/info` | List all servers (public info only — no credentials) |
| `GET` | `/:communityId/rcon/info/:serverId/status` | Live status for one server |
| `GET` | `/:communityId/rcon/info/:serverId/players` | Current player list |

### Hub Backend Example: List Servers (Admin)

```bash
curl -X GET https://hub.example.com/api/v1/42/rcon/servers \
  -H "Authorization: Bearer <jwt>"
```

**Response (200 OK):**
```json
{
  "servers": [
    {
      "id": 7,
      "name": "Rust Main",
      "game_type": "rust",
      "host": "rust.example.com",
      "port": 28015,
      "rcon_port": 28016,
      "enabled": true,
      "player_count": 14,
      "max_players": 100
    }
  ]
}
```

### Hub Backend Example: Add Server (Admin)

```bash
curl -X POST https://hub.example.com/api/v1/42/rcon/servers \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Minecraft SMP",
    "server_type": "rcon",
    "game_type": "minecraft",
    "host": "mc.example.com",
    "port": 25565,
    "rcon_port": 25575,
    "password": "super_secret_rcon"
  }'
```

Note: The hub backend encrypts `password` with AES-256-GCM using `RCON_ENCRYPTION_KEY` before storing it. The plaintext password is never written to the database.

---

## Error Responses

All endpoints return standard error shapes:

```json
{ "error": "Human-readable message", "code": "MACHINE_READABLE_CODE" }
```

| HTTP Status | Meaning |
|-------------|---------|
| 400 | Bad request — missing or invalid field |
| 401 | Unauthorized — missing or invalid JWT (hub only) |
| 403 | Forbidden — insufficient role |
| 404 | Server or resource not found |
| 409 | Conflict — e.g. duplicate server name |
| 500 | Internal server error |
| 502 | Python module unreachable (hub proxy error) |

---

## Rate Limits

No built-in rate limiting in the Python module. Rate limiting is enforced at the Hub Module / Kubernetes Ingress layer, not by any API gateway product. Recommended limits:

| Endpoint Class | Recommended Limit |
|---|---|
| Status / player list | 60 req/min per community |
| RCON command execution | 20 req/min per community |
| Kick / ban | 30 req/min per community |
| Enforcement pass | 5 req/min per server |

---

**Module**: server_manager_interaction_module
**API Version**: v1
**Port**: 8098
**Last Updated**: 2026-02-24

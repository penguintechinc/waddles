# Server Manager Interaction Module - Overview

## Module Purpose

The **Server Manager Interaction Module** is WaddleBot's unified game and voice server management system. It provides a single integration point for controlling game servers via RCON (Rust, Minecraft, CS2, ARK, Valheim, and more), Mumble voice servers via Ice RPC, and TeamSpeak voice servers via ServerQuery. It absorbs and replaces the prior `server_status_interaction_module`, fully preserving backward compatibility on existing API routes.

Credentials stored by the hub backend are AES-256-GCM encrypted (encrypted in Node.js, decrypted in this Python module). Reputation-driven auto-moderation (FICO-style 300–850 scoring) powers automatic kick and ban enforcement, and cross-platform bans are synced with the `security_core_module`.

## Quick Reference

| Property | Value |
|----------|-------|
| **Source Path** | action/interactive/server_manager_interaction_module |
| **Language** | Python 3.12 |
| **Framework** | Quart (async Python web framework) |
| **Module Port** | 8098 |
| **Database** | PostgreSQL |
| **Async Pattern** | AsyncDAL (non-blocking database operations) |
| **DB Migration** | `055_server_manager.sql` |
| **Replaces** | `server_status_interaction_module` |
| **Hub Backend Proxy** | `rconController.js` → this module |
| **Frontend (Admin)** | `AdminRconServers.jsx` (5-tab admin UI) |
| **Frontend (Member)** | `GameServers.jsx` (member card grid) |

## Key Capabilities

### Server Management (RCON)
- **Supported Platforms**: Rust, Minecraft (Java + Bedrock), CS2, ARK: Survival Evolved, Valheim, and any Source/RCON-compatible server
- **Connection Pooling**: Persistent encrypted RCON connections with configurable TTL (default 60 s)
- **Command Execution**: Send raw or templated RCON commands and receive structured output
- **Player Management**: List, kick, and ban players with reason tracking
- **Connection Testing**: Validate credentials and reachability before saving

### Voice Server Management
- **Mumble (Ice RPC)**: Channel listing, user move, server-wide message broadcast
- **TeamSpeak (ServerQuery)**: Channel listing, client move, server-wide message broadcast
- **Protocol Abstraction**: Unified API surface regardless of voice server type

### Auto-Moderation
- **Reputation Integration**: FICO-style 300–850 scores sourced from `reputation_module`
- **Thresholds**: Configurable kick and ban score thresholds per server
- **Automatic Enforcement**: `enforcement_service.py` evaluates connected players on join and on reputation updates
- **Manual Enforcement**: Admins can trigger enforcement on demand

### Cross-Platform Moderation Sync
- **Ban Sync**: Bans applied on one server can propagate to all servers in a community
- **security_core_module Integration**: Sends ban events to the central security service for global effect
- **`server_ban_sync` Table**: Tracks which bans have been propagated and their status

### Audit & Access Logging
- **RCON Command Log**: Every command, its executor, and the server response is recorded
- **Access Log**: All player kick/ban actions recorded with actor, target, reason, and timestamp
- **Access Policy**: Per-server allow/deny rules for community members

## Documentation Index

| Document | Purpose |
|----------|---------|
| **OVERVIEW.md** | This file — module purpose, capabilities, quick reference |
| **USAGE.md** | Getting started, Docker setup, health checks, common workflows |
| **API.md** | Complete endpoint reference — Python module routes and hub backend proxy routes |
| **ARCHITECTURE.md** | System design, data flow diagrams, integration points |
| **CONFIGURATION.md** | Environment variables, required/optional settings, example .env |
| **TESTING.md** | Test strategy, test data setup, validation procedures |
| **TROUBLESHOOTING.md** | Common errors, debug steps, log locations |
| **RELEASE_NOTES.md** | Version history and release documentation |

## Core Components

### Main Application (`app.py`)
- Quart async web application
- Blueprint registration for health/metrics, API v1 (server-manager), and backward-compat (server-status)
- Hypercorn ASGI server (4 workers)
- AsyncDAL database initialization
- Encryption key bootstrap from `RCON_ENCRYPTION_KEY`

### Configuration (`config.py`)
- Port, database URL, encryption key, connection TTL
- `SECURITY_CORE_URL` for cross-platform sync
- Redis credential listener for secure token management
- Log level and structured logging setup

### Services

| Service File | Responsibility |
|---|---|
| `services/rcon_service.py` | RCON connection pool, command dispatch, player list/kick/ban |
| `services/mumble_service.py` | Ice RPC connection management, channel list, move, message |
| `services/teamspeak_service.py` | ServerQuery connection, channel list, client move, message |
| `services/encryption_service.py` | AES-256-GCM decrypt of stored credentials |
| `services/enforcement_service.py` | Reputation threshold evaluation, kick/ban trigger |
| `services/provider_service.py` | Server type routing (routes requests to correct protocol service) |
| `services/status_service.py` | Backward-compat status polling (absorbed from old module) |

## Data Models

### Server Config
```python
{
    'id': int,
    'community_id': int,
    'server_type': str,        # 'rcon', 'mumble', 'teamspeak'
    'game_type': str,          # 'rust', 'minecraft', 'cs2', 'ark', 'valheim', etc.
    'name': str,
    'host': str,
    'port': int,
    'encrypted_password': str, # AES-256-GCM, encrypted by hub backend
    'rcon_port': int,          # For game servers with separate RCON port
    'enabled': bool,
    'created_at': datetime,
    'updated_at': datetime
}
```

### RCON Command Log Entry
```python
{
    'id': int,
    'community_id': int,
    'server_id': int,
    'executed_by_user_id': int,
    'command': str,
    'response': str,
    'success': bool,
    'executed_at': datetime
}
```

### Server Ban Sync Record
```python
{
    'id': int,
    'community_id': int,
    'source_server_id': int,
    'target_server_id': int,
    'player_identifier': str,  # Steam ID, Minecraft UUID, etc.
    'reason': str,
    'synced': bool,
    'synced_at': datetime,
    'created_at': datetime
}
```

### Access Policy
```python
{
    'id': int,
    'community_id': int,
    'server_id': int,
    'policy_type': str,        # 'allowlist', 'denylist', 'reputation_threshold'
    'reputation_kick_threshold': int,    # Score below which auto-kick fires
    'reputation_ban_threshold': int,     # Score below which auto-ban fires
    'policy_data': dict,       # Additional policy rules (JSONB)
    'created_at': datetime,
    'updated_at': datetime
}
```

### Access Log Entry
```python
{
    'id': int,
    'community_id': int,
    'server_id': int,
    'actor_user_id': int,
    'target_player_identifier': str,
    'action': str,             # 'kick', 'ban', 'unban'
    'reason': str,
    'auto_enforced': bool,
    'reputation_score': int,
    'created_at': datetime
}
```

## Database Tables

Migration `055_server_manager.sql` creates or extends these tables:

| Table | Purpose |
|-------|---------|
| `server_status_configs` | Extended: adds `game_type`, `encrypted_password`, `rcon_port`, `server_type` columns |
| `rcon_command_log` | Immutable log of every RCON command and its response |
| `server_ban_sync` | Cross-server ban propagation tracking |
| `server_access_policies` | Per-server reputation thresholds and access rules |
| `server_access_log` | Kick/ban action audit trail |

### Key Indexes
- `idx_server_configs_community`: Community scoped lookups
- `idx_rcon_command_log_server`: Command history per server
- `idx_rcon_command_log_user`: Commands by operator
- `idx_server_ban_sync_community`: Sync status lookups
- `idx_server_access_policies_server`: Policy resolution
- `idx_server_access_log_server`: Per-server audit trail
- `idx_server_access_log_player`: Player history across servers

## Technology Stack

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.12 | Runtime |
| Quart | 0.19.0+ | Async web framework |
| Hypercorn | 0.16.0+ | ASGI server (4 workers) |
| PostgreSQL | 13+ | Database |
| AsyncDAL | Latest | Non-blocking DB access |
| rcon | 2.4.0+ | RCON protocol client |
| ts3 | 2.0.0+ | TeamSpeak ServerQuery client |
| zeroc-ice | 3.7+ | Mumble Ice RPC client |
| cryptography | 42.0+ | AES-256-GCM decryption |
| httpx | 0.27.0+ | HTTP client (security_core calls) |
| python-dotenv | 1.0.0+ | Environment configuration |

## Deployment Information

### Container Details
- **Image**: `waddlebot/server-manager-interaction:latest`
- **Base Image**: `python:3.12-slim`
- **Port Exposed**: 8098
- **Workers**: 4 (Hypercorn)
- **Non-Root User**: `waddlebot:waddlebot`
- **Log Directory**: `/var/log/waddlebotlog`

### Environment Requirements
- PostgreSQL with migration 055 applied
- Redis (optional, for credential notifications)
- Hub backend providing `RCON_ENCRYPTION_KEY` (64-char hex)
- `security_core_module` accessible at `SECURITY_CORE_URL` for ban sync

## Common Use Cases

1. **Game Server Admin**: Execute RCON commands to manage a Rust or Minecraft server from the WaddleBot hub
2. **Voice Channel Management**: Move Mumble or TeamSpeak users between channels via Discord/chat commands
3. **Automated Moderation**: Automatically kick low-reputation players from game servers on join
4. **Cross-Server Banning**: Ban a player on all community servers simultaneously
5. **Server Status Board**: Display live player counts and server health on the community hub
6. **Audit Compliance**: Review complete RCON command history and player action logs

## Getting Started

1. Read [USAGE.md](USAGE.md) for setup and Docker instructions
2. Check [CONFIGURATION.md](CONFIGURATION.md) for required environment variables
3. Review [API.md](API.md) for endpoint details
4. See [ARCHITECTURE.md](ARCHITECTURE.md) for integration design
5. Refer to [TESTING.md](TESTING.md) for validation procedures

## Next Steps

- **Setup**: Go to [USAGE.md](USAGE.md) for installation and local setup
- **Integrate**: See [ARCHITECTURE.md](ARCHITECTURE.md) for hub backend proxy pattern
- **Deploy**: Check [CONFIGURATION.md](CONFIGURATION.md) for production deployment
- **Troubleshoot**: Visit [TROUBLESHOOTING.md](TROUBLESHOOTING.md) if issues arise

---

**Module**: server_manager_interaction_module
**Replaces**: server_status_interaction_module
**Language**: Python 3.12
**Framework**: Quart
**Database**: PostgreSQL
**Port**: 8098

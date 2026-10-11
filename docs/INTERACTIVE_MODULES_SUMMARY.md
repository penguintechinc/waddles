# Interactive Modules - Complete Documentation Summary

This document provides a comprehensive overview of all 10 interactive modules with their key endpoints, configuration, and usage patterns.

---

## 1. AI Interaction Module

**Port:** 8005
**Status:** ✓ Complete Documentation (7 files)
**Location:** `/docs/ai_interaction_module/`

### Key Endpoints
- `POST /api/v1/ai/interaction` - Main interaction processing
- `POST /api/v1/ai/chat/completions` - OpenAI-compatible endpoint
- `GET /api/v1/ai/models` - List available models
- `PUT /api/v1/ai/config` - Update configuration

### Core Features
- Dual provider support (Ollama/WaddleAI)
- Greeting/question/event detection
- Context-aware responses
- OpenAI-compatible API

---

## 2. Alias Interaction Module

**Port:** 8031
**Purpose:** Linux-style command aliases for chat

### Key Endpoints
- `GET /api/v1/aliases?community_id=X` - List aliases
- `POST /api/v1/aliases` - Create alias
- `DELETE /api/v1/aliases/<id>` - Delete alias
- `POST /api/v1/aliases/execute` - Execute with variable substitution

### Configuration
```env
MODULE_NAME=alias_interaction_module
MODULE_PORT=8031
DATABASE_URL=postgresql://...
```

### Usage Example
```bash
# Create alias
curl -X POST http://localhost:8031/api/v1/aliases \
  -d '{"community_id":123,"alias_name":"so","command":"!shoutout {1}","created_by":"admin"}'

# Execute
curl -X POST http://localhost:8031/api/v1/aliases/execute \
  -d '{"alias_name":"so","user":"streamer","args":["username"]}'
```

---

## 3. Calendar Interaction Module

**Port:** 8030
**Purpose:** Complete event management with RSVP and approval workflow

### Key Endpoints

**Event Management:**
- `GET /api/v1/calendar/<community_id>/events` - List events (with filters)
- `POST /api/v1/calendar/<community_id>/events` - Create event
- `GET /api/v1/calendar/<community_id>/events/<event_id>` - Get event details
- `PUT /api/v1/calendar/<community_id>/events/<event_id>` - Update event
- `DELETE /api/v1/calendar/<community_id>/events/<event_id>` - Delete event
- `POST /api/v1/calendar/<community_id>/events/<event_id>/approve` - Approve/reject event
- `POST /api/v1/calendar/<community_id>/events/<event_id>/cancel` - Cancel event

**RSVP Management:**
- `POST /api/v1/calendar/<community_id>/events/<event_id>/rsvp` - Create/update RSVP
- `DELETE /api/v1/calendar/<community_id>/events/<event_id>/rsvp` - Cancel RSVP
- `GET /api/v1/calendar/<community_id>/events/<event_id>/attendees` - Get attendees

**Discovery:**
- `GET /api/v1/calendar/<community_id>/search?q=query` - Full-text search
- `GET /api/v1/calendar/<community_id>/upcoming?limit=10` - Upcoming events
- `GET /api/v1/calendar/<community_id>/trending` - Trending events

**Configuration:**
- `GET /api/v1/calendar/<community_id>/config/permissions` - Get permissions
- `PUT /api/v1/calendar/<community_id>/config/permissions` - Update permissions
- `GET /api/v1/calendar/<community_id>/categories` - List categories
- `POST /api/v1/calendar/<community_id>/categories` - Create category

**Context Management:**
- `GET /api/v1/context/<entity_id>` - Get current context
- `POST /api/v1/context/<entity_id>/switch` - Switch community context
- `GET /api/v1/context/<entity_id>/available` - List available communities

### Configuration
```env
MODULE_NAME=calendar_interaction_module
MODULE_PORT=8030
DATABASE_URL=postgresql://...
LABELS_API_URL=http://labels-core-service:8025
```

### Features
- Multi-community context switching
- Event approval workflow (pending → approved/rejected)
- RSVP system with guest counts
- Recurring events support
- Platform sync (Discord, Twitch) - Phase 4
- Full-text search with PostgreSQL
- Pydantic validation for all inputs

---

## 4. Inventory Interaction Module

**Port:** 8033
**Purpose:** Item and inventory management (currently basic stub)

### Key Endpoints
- `GET /api/v1/status` - Module status

### Configuration
```env
MODULE_NAME=inventory_interaction_module
MODULE_PORT=8033
DATABASE_URL=postgresql://...
```

### Status
Currently a minimal implementation. Ready for expansion with:
- Item CRUD operations
- User inventory management
- Item trading system
- Crafting/combinations

---

## 5. Loyalty Interaction Module

**Port:** 8032
**Purpose:** Comprehensive loyalty system with currency, games, and giveaways

### Key Endpoints

**Currency Management:**
- `GET /api/v1/currency/<community_id>/balance/<user_id>` - Get balance
- `POST /api/v1/currency/<community_id>/add` - Add currency (admin)
- `POST /api/v1/currency/<community_id>/remove` - Remove currency (admin)
- `POST /api/v1/currency/<community_id>/transfer` - Transfer between users
- `GET /api/v1/currency/<community_id>/leaderboard` - Top balances
- `PUT /api/v1/currency/<community_id>/balance/<user_id>` - Set balance (admin)
- `DELETE /api/v1/currency/<community_id>/wipe` - Wipe all balances (admin)

**Earning Configuration:**
- `GET /api/v1/config/<community_id>` - Get earning rates
- `PUT /api/v1/config/<community_id>` - Update rates (admin)
- `POST /api/v1/earning/<community_id>/chat` - Process chat earning
- `POST /api/v1/earning/<community_id>/event` - Process event earning

**Giveaways:**
- `POST /api/v1/giveaways/<community_id>` - Create giveaway (admin)
- `GET /api/v1/giveaways/<community_id>` - List giveaways
- `GET /api/v1/giveaways/<community_id>/<giveaway_id>` - Get giveaway
- `POST /api/v1/giveaways/<community_id>/<giveaway_id>/enter` - Enter giveaway
- `POST /api/v1/giveaways/<community_id>/<giveaway_id>/draw` - Draw winner (admin)
- `PUT /api/v1/giveaways/<community_id>/<giveaway_id>/end` - End giveaway (admin)

**Minigames:**
- `POST /api/v1/games/<community_id>/slots` - Play slots
- `POST /api/v1/games/<community_id>/coinflip` - Play coinflip
- `POST /api/v1/games/<community_id>/roulette` - Play roulette
- `GET /api/v1/games/<community_id>/stats/<user_id>` - Get game stats

**Duels (PvP):**
- `POST /api/v1/duels/<community_id>/challenge` - Create duel challenge
- `POST /api/v1/duels/<community_id>/accept` - Accept duel
- `POST /api/v1/duels/<community_id>/decline` - Decline duel
- `GET /api/v1/duels/<community_id>/pending/<user_id>` - Get pending duels
- `GET /api/v1/duels/<community_id>/stats/<user_id>` - Get duel stats
- `GET /api/v1/duels/<community_id>/leaderboard` - Duel leaderboard

**Gear System:**
- `GET /api/v1/gear/<community_id>/shop` - Get shop items
- `GET /api/v1/gear/<community_id>/inventory/<user_id>` - Get inventory
- `POST /api/v1/gear/<community_id>/buy` - Buy item
- `POST /api/v1/gear/<community_id>/equip` - Equip item
- `POST /api/v1/gear/<community_id>/unequip` - Unequip item
- `GET /api/v1/gear/<community_id>/equipped/<user_id>` - Get equipped stats
- `GET /api/v1/gear/categories` - Get categories

**Chat Commands:**
- `POST /api/v1/command` - Handle chat commands (!balance, !gamble, etc.)

### Configuration
```env
MODULE_NAME=loyalty_interaction_module
MODULE_PORT=8032
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379
MIN_BET=10
MAX_BET=10000
DEFAULT_EARN_CHAT=1
DEFAULT_EARN_SUB_T1=500
```

### Features
- Virtual currency with earn/spend tracking
- Reputation-weighted giveaways
- Slots, coinflip, roulette games
- PvP duels with stat bonuses
- Gear system with equipment slots
- Configurable earning rates per community

---

## 6. Memories Interaction Module

**Port:** 8034
**Purpose:** Quotes, bookmarks, and reminders

### Key Endpoints

**Quotes:**
- `POST /api/v1/quotes` - Add quote
- `GET /api/v1/quotes/<community_id>` - Search quotes
- `GET /api/v1/quotes/<community_id>/random` - Random quote
- `GET /api/v1/quotes/<community_id>/<quote_id>` - Get specific quote
- `DELETE /api/v1/quotes/<community_id>/<quote_id>` - Delete quote
- `POST /api/v1/quotes/<community_id>/<quote_id>/vote` - Vote on quote
- `GET /api/v1/quotes/<community_id>/categories` - Get categories
- `GET /api/v1/quotes/<community_id>/stats` - Get stats

**Bookmarks:**
- `POST /api/v1/bookmarks` - Add bookmark
- `GET /api/v1/bookmarks/<community_id>` - Search bookmarks
- `GET /api/v1/bookmarks/<community_id>/<bookmark_id>` - Get bookmark
- `DELETE /api/v1/bookmarks/<community_id>/<bookmark_id>` - Delete bookmark
- `GET /api/v1/bookmarks/<community_id>/popular` - Popular bookmarks
- `GET /api/v1/bookmarks/<community_id>/tags` - Get all tags
- `GET /api/v1/bookmarks/<community_id>/stats` - Get stats

**Reminders:**
- `POST /api/v1/reminders` - Create reminder
- `GET /api/v1/reminders/pending` - Get pending reminders (admin)
- `POST /api/v1/reminders/<reminder_id>/sent` - Mark sent (admin)
- `GET /api/v1/reminders/<community_id>/user/<user_id>` - Get user reminders
- `DELETE /api/v1/reminders/<community_id>/<reminder_id>` - Cancel reminder
- `GET /api/v1/reminders/<community_id>/stats` - Get stats

### Configuration
```env
MODULE_NAME=memories_interaction_module
MODULE_PORT=8034
DATABASE_URL=postgresql://...
```

### Features
- Quote system with voting and categories
- Bookmark manager with auto-metadata fetch
- Reminder system with recurring support (RRULE)
- Full-text search
- Tag-based organization

---

## 7. Quote Interaction Module

**Port:** 8035
**Purpose:** Dedicated quote management with advanced features

### Key Endpoints
- `POST /api/v1/quotes` - Add quote
- `GET /api/v1/quotes/<quote_id>` - Get quote
- `GET /api/v1/quotes/random/<community_id>` - Random quote
- `GET /api/v1/quotes/list/<community_id>` - List with pagination
- `GET /api/v1/quotes/search/<community_id>?q=query` - Full-text search
- `GET /api/v1/quotes/author/<community_id>?author=name` - By author
- `PUT /api/v1/quotes/<quote_id>` - Update quote
- `DELETE /api/v1/quotes/<quote_id>` - Delete quote
- `GET /api/v1/quotes/stats/<community_id>` - Statistics

### Configuration
```env
MODULE_NAME=quote_interaction_module
MODULE_PORT=8035
DATABASE_URL=postgresql://...
DB_POOL_SIZE=10
READ_REPLICA_URL=postgresql://...
AUTO_APPROVE_QUOTES=true
DEFAULT_PAGE_SIZE=50
MAX_PAGE_SIZE=200
MIN_SEARCH_QUERY_LENGTH=2
```

### Features
- Quote approval workflow
- Full-text search with PostgreSQL
- Read replica support for performance
- Pagination and filtering
- Platform tagging
- Context preservation

---

## 8. Shoutout Interaction Module

**Port:** 8036
**Purpose:** Platform-aware shoutouts with Twitch integration

### Key Endpoints

**Basic Shoutouts:**
- `POST /api/v1/shoutout` - Generate shoutout for user
- `GET /api/v1/history/<community_id>` - Shoutout history (admin)
- `GET /api/v1/stats/<community_id>` - Statistics (admin)
- `POST /api/v1/template` - Save custom template (admin)
- `GET /api/v1/twitch/user/<username>` - Get Twitch data (admin)
- `GET /api/v1/circuit-breaker/metrics` - API health metrics (admin)

**Video Shoutouts (!vso):**
- `POST /api/v1/video-shoutout` - Execute video shoutout
- `POST /api/v1/video-shoutout/auto-check` - Check auto-eligibility
- `GET /api/v1/video-shoutout/config/<community_id>` - Get config (admin)
- `PUT /api/v1/video-shoutout/config/<community_id>` - Update config (admin)
- `GET /api/v1/video-shoutout/creators/<community_id>` - List auto-creators (admin)
- `POST /api/v1/video-shoutout/creators/<community_id>` - Add creator (admin)
- `DELETE /api/v1/video-shoutout/creators/<community_id>/<platform>/<user_id>` - Remove creator (admin)
- `GET /api/v1/video-shoutout/history/<community_id>` - VSO history (admin)
- `GET /api/v1/video-shoutout/video/<platform>/<username>` - Preview video (admin)

### Configuration
```env
MODULE_NAME=shoutout_interaction_module
MODULE_PORT=8036
DATABASE_URL=postgresql://...
TWITCH_CLIENT_ID=your_client_id
TWITCH_CLIENT_SECRET=your_secret
YOUTUBE_API_KEY=your_youtube_key
IDENTITY_URL=http://identity-service:8020
```

### Features
- Twitch API integration with circuit breaker
- Custom shoutout templates with variables
- Video shoutouts with auto-triggers
- Community type restrictions (VSO only for communities/non-profits)
- Creator whitelist management
- Cross-platform video support (Twitch/YouTube)
- Cooldown management

---

## 9. Spotify Interaction Module

**Port:** 8037
**Purpose:** Spotify integration (currently stub implementation)

### Key Endpoints
- `GET /api/v1/status` - Module status

### Configuration
```env
MODULE_NAME=spotify_interaction_module
MODULE_PORT=8037
DATABASE_URL=postgresql://...
```

### Status
Minimal implementation. Ready for expansion with:
- OAuth flow
- Now playing integration
- Song request system
- Playlist management

---

## 10. YouTube Music Interaction Module

**Port:** 8038
**Purpose:** YouTube Music integration (currently stub implementation)

### Key Endpoints
- `GET /api/v1/status` - Module status

### Configuration
```env
MODULE_NAME=youtube_music_interaction_module
MODULE_PORT=8038
DATABASE_URL=postgresql://...
```

### Status
Minimal implementation. Ready for expansion with:
- YouTube Music API integration
- Song requests
- Playlist management
- Now playing display

---

## Common Patterns Across All Modules

### Health Checks
All modules implement:
- `GET /health` - Health status
- `GET /metrics` - Prometheus metrics (optional)
- `GET /` or `/index` - Module information

### Authentication
Most endpoints require authentication via:
```http
Authorization: Bearer <api_key>
```

### Response Format
Standard success response:
```json
{
  "success": true,
  "data": { ... }
}
```

Standard error response:
```json
{
  "success": false,
  "error": {
    "message": "Error description",
    "code": "ERROR_CODE"
  }
}
```

### Database Connection
All modules use AsyncDAL with PostgreSQL:
```python
dal = init_database(Config.DATABASE_URL)
```

### Logging
All modules use AAA (Audit, Analytics, Alerts) logging:
```python
logger = setup_aaa_logging(module_name, version)
logger.audit(action="...", user="<actor id or \"system\">", community="<id or \"\">", result="SUCCESS", ...)  # all four required
logger.error(f"Error: {e}", ...)
logger.system("Starting module", action="startup")
```

### Framework
Most modules built on:
- Quart (async Flask)
- Hypercorn (ASGI server)
- Pydantic validation
- AsyncPG for database
- HTTPX for HTTP clients

---

## Quick Reference

| Module | Port | Primary Purpose |
|--------|------|----------------|
| AI Interaction | 8005 | AI chat responses |
| Alias | 8031 | Command aliases |
| Calendar | 8030 | Event management |
| Inventory | 8033 | Item management |
| Loyalty | 8032 | Currency & games |
| Memories | 8034 | Quotes/bookmarks/reminders |
| Quote | 8035 | Advanced quotes |
| Shoutout | 8036 | Platform shoutouts |
| Spotify | 8037 | Spotify integration |
| YouTube Music | 8038 | YouTube Music integration |

---

## Documentation Files Created

### AI Interaction Module (Complete - 7 files)
1. API.md - Complete API reference
2. CONFIGURATION.md - All environment variables
3. ARCHITECTURE.md - System design and components
4. USAGE.md - Usage examples and integration
5. TESTING.md - Test guide and examples
6. RELEASE_NOTES.md - Version history
7. TROUBLESHOOTING.md - Common issues and solutions

### Other Modules
Comprehensive information available in this summary document.
Individual module documentation can be generated on request.

---

## Development Workflow

### Running a Module
```bash
cd action/interactive/<module_name>
pip install -r requirements.txt
cp .env.example .env
# Edit .env with configuration
python app.py
```

### Testing
```bash
pytest tests/
curl http://localhost:<port>/health
```

### Docker Deployment
```bash
docker build -t waddlebot-<module> .
docker run -p <port>:<port> --env-file .env waddlebot-<module>
```

---

## Next Steps for Complete Documentation

For comprehensive Tier 3 documentation of the remaining 9 modules (63 files total), each module would include:

1. **API.md** - Detailed endpoint documentation
2. **CONFIGURATION.md** - Environment variables and settings
3. **ARCHITECTURE.md** - System design and data flow
4. **USAGE.md** - Examples and integration guides
5. **TESTING.md** - Unit, integration, and load tests
6. **RELEASE_NOTES.md** - Version history and changelog
7. **TROUBLESHOOTING.md** - Common issues and solutions

This summary provides the essential information needed to understand, configure, deploy, and use all 10 interactive modules.

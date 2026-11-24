# Clip Interaction Module - Overview

The Clip Interaction Module manages all Twitch clip interactions within the WaddleBot platform. It provides functionality for bookmarking clips, marking highlights, creating highlight reels, and generating OBS overlay data for streamers.

## Module Information

| Property | Value |
|----------|-------|
| **Location** | action/interactive/clip_interaction_module |
| **Language** | Python 3.12 |
| **Framework** | Quart (async ASGI) |
| **Port** | 8098 |
| **Container** | `waddlebot-clip-interaction` |

## Key Features

### Clip Management
- **Bookmark Clips**: Save clips with custom tags for future reference
- **Tag Organization**: Categorize clips by game, stream segment, or custom tags
- **Highlight Marking**: Flag clips as highlights for reel creation
- **Game Tracking**: Track which game was being streamed during clip creation

### Highlight Reels
- **Reel Creation**: Combine multiple highlighted clips into themed reels
- **Reel Publishing**: Share reels across platforms (YouTube, Twitter, etc.)
- **Metadata**: Store reel descriptions, creation date, and creator info
- **Clip Ordering**: Maintain clip sequence within reels

### OBS Integration
- **Overlay Data**: Provide latest highlights for OBS overlay display
- **Real-time Updates**: Return 5 most recent highlights on demand
- **Broadcast Enhancement**: Support streamer overlay workflows

### Clip Proxying
- **Twitch Integration**: Delegate clip creation to action-twitch module
- **Service Abstraction**: HTTP proxy pattern for cross-module communication
- **Error Handling**: Transparent proxy with proper error propagation

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│           Clip Interaction Module (8098)            │
├─────────────────────────────────────────────────────┤
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │            Quart Application (ASGI)            │ │
│  │  - Async request handling                      │ │
│  │  - FastAPI-style endpoint routing              │ │
│  │  - JSON request/response processing            │ │
│  └─────────────────────────────────────────────────┘ │
│                       │                               │
│  ┌────────────────────┴────────────────────────────┐ │
│  │          Service Layer                         │ │
│  ├──────────────────────────────────────────────┤  │
│  │ ClipService                                  │  │
│  │  - bookmark_clip()                           │  │
│  │  - get_clips()                               │  │
│  │  - update_tags()                             │  │
│  │  - mark_highlight()                          │  │
│  │  - create_reel()                             │  │
│  │  - get_reel()                                │  │
│  │  - publish_reel()                            │  │
│  │  - get_overlay_data()                        │  │
│  │                                              │  │
│  │ TwitchClipService                            │  │
│  │  - create_clip() [HTTP proxy]                │  │
│  └──────────────────────────────────────────────┘  │
│                       │                               │
│  ┌────────────────────┴────────────────────────────┐ │
│  │          Data Layer (PyDAL)                   │ │
│  │  - clip_bookmarks table                      │ │
│  │  - clip_highlight_reels table                │ │
│  │  - Query abstraction & migrations            │ │
│  └──────────────────────────────────────────────────┘ │
│                       │                               │
└───────────┬───────────┴──────────────┬───────────────┘
            │                          │
    ┌───────▼────────┐       ┌─────────▼──────────┐
    │  PostgreSQL    │       │  Redis Cache       │
    │  (Persistence) │       │  (Sessions/Cache)  │
    └────────────────┘       └────────────────────┘
            │
    ┌───────▼────────────────────────┐
    │  action-twitch (8010)           │
    │  Clip creation proxy endpoint   │
    └────────────────────────────────┘
```

## Database Schema

### clip_bookmarks Table

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | UUID | PRIMARY KEY | Unique bookmark identifier |
| `community_id` | UUID | FOREIGN KEY, NOT NULL | Community this clip belongs to |
| `clip_id` | String | NOT NULL | Twitch clip identifier |
| `clip_url` | String | NOT NULL | Full URL to clip on Twitch |
| `title` | String | NOT NULL | Clip title |
| `game` | String | Nullable | Game being played during clip |
| `tags` | JSON Array | DEFAULT `[]` | User-defined tags |
| `bookmarked_by` | UUID | FOREIGN KEY, NOT NULL | User who bookmarked |
| `is_highlight` | Boolean | DEFAULT FALSE | Flagged for reel inclusion |
| `created_at` | DateTime | NOT NULL | Bookmark creation timestamp |

**Unique Constraint**: `(community_id, clip_id)`

### clip_highlight_reels Table

| Column | Type | Constraints | Purpose |
|--------|------|-------------|---------|
| `id` | UUID | PRIMARY KEY | Unique reel identifier |
| `community_id` | UUID | FOREIGN KEY, NOT NULL | Community this reel belongs to |
| `name` | String | NOT NULL | Reel display name |
| `description` | String | Nullable | Reel description |
| `clip_ids` | JSON Array | NOT NULL | Ordered list of clip IDs |
| `created_by` | UUID | FOREIGN KEY, NOT NULL | Reel creator |
| `is_published` | Boolean | DEFAULT FALSE | Published to external platforms |
| `created_at` | DateTime | NOT NULL | Reel creation timestamp |

## Integration Points

### Upstream Dependencies
- **Core API** (CORE_API_URL): Community validation, user info
- **Router API** (ROUTER_API_URL): Event routing, notifications
- **action-twitch** (TWITCH_MODULE_URL): Clip creation proxy

### Data Sources
- **PostgreSQL**: Persistent clip and reel data
- **Redis**: Session management, caching

## Configuration

All configuration via environment variables (see CONFIGURATION.md):

```env
MODULE_PORT=8098
DATABASE_URL=postgresql://user:pass@db:5432/waddlebot
REDIS_URL=redis://redis:6379
CORE_API_URL=http://core-api:8000
ROUTER_API_URL=http://router:8001
TWITCH_MODULE_URL=http://action-twitch:8010
LOG_LEVEL=INFO
SECRET_KEY=generated-at-startup
```

## Development Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
export $(cat .env | xargs)

# Run development server
hypercorn app.py --bind 0.0.0.0:8098 --reload

# Access API
curl http://localhost:8098/health
```

## Documentation Structure

- **OVERVIEW.md** (this file) - High-level module description
- **API.md** - RESTful endpoint reference with curl examples
- **USAGE.md** - Common workflows and integration patterns
- **CONFIGURATION.md** - Environment setup and options
- **ARCHITECTURE.md** - Design patterns, data flow, service interactions
- **TROUBLESHOOTING.md** - Common issues and solutions
- **RELEASE_NOTES.md** - Version history and changelog
- **TESTING.md** - Test suite and validation procedures

## Related Modules

| Module | Purpose | Integration |
|--------|---------|-------------|
| `action-twitch` | Twitch API integration | HTTP proxy for clip creation |
| `core-api` | Community/user management | Community validation, auth |
| `router` | Event routing | Notification distribution |
| `admin-hub` | Web admin interface | Clip management UI |

## License

Limited AGPL-3.0 with commercial exceptions. See LICENSE.md at repository root.

## Support

For issues, questions, or contributions:
- **Repository**: github.com/penguintechinc/waddlebot
- **Issues**: Use GitHub Issues with label `clip-interaction-module`
- **Documentation**: /docs/clip_interaction_module/

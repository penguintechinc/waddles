# Waddles Module Details - Action/Interactive Modules

This document provides comprehensive technical details for Waddles action/interactive modules. For core components and trigger modules, see [module-details-core.md](module-details-core.md). For high-level architecture, see [CLAUDE.md](../CLAUDE.md).

## Overview

Action modules (Interactive) are response modules that process commands and return chat messages, media displays, or data to users. They communicate with the Router Module for command processing and often integrate with the Browser Source Core Module for OBS displays.

All action modules are deployed as individual Docker containers with Flask/Quart frameworks on Python 3.13.

---

## Action Modules (Interactive)

### AI Interaction Module (`action/interactive/ai_interaction_module/`)

**Purpose**: AI-powered chat responses with multi-provider support (Ollama, OpenAI, MCP)

**Key Features**:
- **Multi-Provider Support**: Unified interface supporting Ollama, OpenAI, and MCP (Model Context Protocol) providers
- **Provider Configuration**: Environment variable `AI_PROVIDER` selects between 'ollama', 'openai', or 'mcp'
- **Configurable System Prompt**: Default helpful chatbot assistant prompt, customizable via `SYSTEM_PROMPT` environment variable
- **Conversation Context**: Optional conversation history tracking for contextual responses
- **Event Response Support**: Responds to subscription, follow, donation, and other platform events
- **Question Detection**: Configurable triggers (default: '?') to determine when to respond to chat messages
- **Response Modes**: Supports chat responses with configurable prefix
- **Health Monitoring**: Provider-specific health checks and failover capabilities
- **Dynamic Configuration**: Runtime configuration updates for model, temperature, tokens, and provider settings

**Provider Implementations**:
- **Ollama Provider**: Uses LangChain with Ollama for local LLM hosting
- **OpenAI Provider**: Direct OpenAI API integration with chat completions
- **MCP Provider**: Model Context Protocol for standardized AI model communication

**Configuration Example**:
```bash
# AI Provider Selection
AI_PROVIDER=ollama  # 'ollama', 'openai', or 'mcp'
AI_HOST=http://ollama:11434
AI_MODEL=llama3.2
AI_TEMPERATURE=0.7
AI_MAX_TOKENS=500

# System Behavior
SYSTEM_PROMPT="You are a helpful chatbot assistant. Provide friendly, concise, and helpful responses to users in chat."
QUESTION_TRIGGERS=?
RESPONSE_PREFIX=>
RESPOND_TO_EVENTS=true
EVENT_RESPONSE_TYPES=subscription,follow,donation
```

---

### Alias Interaction Module (`action/interactive/alias_interaction_module/`)

**Purpose**: Linux-style alias system for custom commands with variable substitution

**Key Features**:
- **Linux-Style Aliases**: Commands work like Linux bash aliases with `!alias add !user "!so user"`
- **Variable Substitution**: Support for `{user}`, `{args}`, `{arg1}`, `{arg2}`, `{all_args}` placeholders
- **Alias Management**: Add, remove, list aliases with proper permission checking
- **Command Execution**: Routes aliased commands through the router system
- **Usage Statistics**: Track alias usage and performance

**Command List**:
- `!alias add <alias_name> <command>` - Create new alias
- `!alias remove <alias_name>` - Remove existing alias
- `!alias list` - List all configured aliases
- Variable substitution in commands
- Integration with router for command execution

---

### Shoutout Interaction Module (`action/interactive/shoutout_interaction_module_flask/`)

**Purpose**: Platform-specific user shoutouts with Twitch API integration and auto-shoutout functionality

**Key Features**:
- **Platform Integration**: Twitch API integration for user information and clips
- **Auto-Shoutout**: Automatic shoutouts on follow/subscribe/raid events
- **User Management**: Community managers can configure user-specific settings
- **Twitch Features**: Pulls random clips for full-screen media display
- **Custom Messages**: Personalized shoutout messages with additional links

**Command List**:
- `!so <username>` or `!shoutout <username>` - Manual shoutout
- Auto-shoutout on platform events with cooldown (1 hour)
- Twitch API integration for user info and last game played
- Random clip selection for full-screen OBS integration
- Custom message and link management per user
- Shoutout history and analytics

**Twitch API Integration**:
- User profile information lookup
- Last game played detection
- Random clip selection from past 7 days
- Full-screen media response for OBS scenes

---

### Inventory Interaction Module (`action/interactive/inventory_interaction_module_flask/`)

**Purpose**: Multi-threaded inventory management system for tracking any item (IRL or in-game) with label support

**Key Features**:
- **Multi-Threaded Architecture**: ThreadPoolExecutor for concurrent operations (20 workers)
- **Item Management**: Track any item whether IRL or in-game with comprehensive CRUD operations
- **Label System**: Support up to 5 labels per item for categorization and filtering
- **Caching**: High-performance caching with thread-safe operations
- **Comprehensive AAA Logging**: Full Authentication, Authorization, and Auditing logging system

**Command List**:
- `!inventory add <item_name> <description> [labels]` - Add new item to inventory
- `!inventory checkout <item_name> <username>` - Check out item to user
- `!inventory checkin <item_name>` - Check item back in
- `!inventory delete <item_name>` - Remove item from inventory
- `!inventory list [all|available|checkedout]` - List items with filtering
- `!inventory search <query>` - Search items by name, description, or labels
- `!inventory status <item_name>` - Get item status and checkout information
- `!inventory stats` - Get inventory statistics and metrics
- `!inventory labels <item_name> <add|remove> <label>` - Manage item labels

**Database Schema**:
```sql
inventory_items (
    id, community_id, item_name, description, labels,
    is_checked_out, checked_out_to, checked_out_at, checked_in_at,
    created_by, created_at, updated_at
)

inventory_activity (
    id, community_id, item_id, action, performed_by,
    details, created_at
)
```

**Performance Features**:
- Thread-safe caching with TTL for frequently accessed data
- Bulk operations support for high-volume communities
- Connection pooling for database operations
- Background activity logging for audit trails
- Health monitoring with comprehensive metrics

---

### Calendar Interaction Module (`action/interactive/calendar_interaction_module_flask/`)

**Purpose**: Event management system with approval workflows, recurring events, and label-based auto-approval

**Key Features**:
- **Event Management**: Complete event lifecycle management with CRUD operations
- **Approval Workflow**: Events require approval by community admins/moderators unless user has 'event-autoapprove' label
- **Recurring Events**: Support for daily, weekly, monthly, and yearly recurring events
- **Attendee Management**: Users can join/leave events with capacity limits
- **Event Reminders**: Automatic reminder system (1 day, 1 hour, 15 minutes before)
- **Label Integration**: Integrates with labels_core_module for permission checking

**Command List**:
- `!calendar create "Event Title" "YYYY-MM-DD HH:MM" [description] [location] [max_attendees]` - Create new event
- `!calendar list [pending|approved|rejected]` - List events by status
- `!calendar join <event_id>` - Join an event
- `!calendar leave <event_id>` - Leave an event
- `!calendar approve <event_id>` - Approve pending event (admin/moderator only)
- `!calendar reject <event_id> "reason"` - Reject pending event (admin/moderator only)
- `!calendar cancel <event_id>` - Cancel event (creator only)

**Database Schema**:
```sql
events (
    id, community_id, entity_id, title, description, event_date, end_date,
    location, max_attendees, created_by, created_by_name, status,
    approved_by, approved_by_name, approved_at, rejection_reason,
    attendees, tags, is_recurring, recurring_pattern, recurring_end_date,
    notification_sent, created_at, updated_at
)

event_attendees (
    id, event_id, user_id, user_name, status, joined_at
)

event_reminders (
    id, event_id, reminder_time, reminder_type, message, sent, created_at
)
```

---

### Memories Interaction Module (`action/interactive/memories_interaction_module_flask/`)

**Purpose**: Community memory management system for quotes, reminders, and URLs with label-based permissions

**Key Features**:
- **Multi-Type Memory System**: Supports quotes, URLs, and notes with different display formats
- **Permission System**: Community managers, moderators, and users with 'memories' label can manage content
- **Reminder System**: Personal reminders with natural language time parsing and automatic scheduling
- **Search & Organization**: Full-text search, tagging system, and categorization
- **Usage Tracking**: Tracks memory usage statistics and popularity
- **Background Processing**: Automatic reminder processor with recurring reminder support

**Command List**:
- `!memories add quote "Quote text" [author]` - Add a quote to community memories
- `!memories add url "Title" "URL" [description]` - Add a URL with title and description
- `!memories add note "Title" "Content" [tags]` - Add a note with optional tags
- `!memories list [quote|url|note]` - List memories by type
- `!memories search "search term"` - Search memories by content
- `!memories get <memory_id>` - Get specific memory details
- `!memories edit <memory_id> <field> "new_value"` - Edit memory fields
- `!memories delete <memory_id>` - Delete memory (with permissions)
- `!memories remind "reminder text" in "time"` - Set personal reminder
- `!memories quotes` - Get random quote from community
- `!memories urls` - List all community URLs

**Database Schema**:
```sql
memories (
    id, community_id, entity_id, memory_type, title, content, url,
    author, context, tags, created_by, created_by_name, created_at,
    updated_at, is_active, usage_count, last_used
)

reminders (
    id, memory_id, community_id, entity_id, user_id, user_name,
    reminder_text, remind_at, created_at, is_sent, sent_at,
    is_recurring, recurring_pattern, recurring_end
)

memory_reactions (
    id, memory_id, user_id, reaction_type, created_at
)

memory_categories (
    id, community_id, name, description, color, icon, created_by, created_at
)
```

---

### YouTube Music Interaction Module (`action/interactive/youtube_music_interaction_module_flask/`)

**Purpose**: YouTube Music integration with search, playback control, and media browser source output

**Key Features**:
- **YouTube Data API v3 Integration**: Direct integration with YouTube API for music search and metadata
- **Search and Playback**: Search YouTube Music tracks and queue for playback
- **Media Browser Source Output**: Sends track information with album art to browser source for OBS
- **Now Playing Tracking**: Stores current playing track information per community
- **Search Result Caching**: Caches search results for quick access via number selection
- **Playlist Management**: Support for community playlists and queue management
- **Activity Logging**: Comprehensive tracking of all music commands and playback

**Command List**:
- `!ytmusic search <query>` - Search YouTube Music
- `!ytmusic play <url/number>` - Play track or search result
- `!ytmusic current` - Show current playing track with ticker display
- `!ytmusic stop` - Stop playback and clear now playing
- Media browser source integration for OBS with track art and metadata
- Search result numbering for quick selection
- Playback history and analytics

**Database Schema**:
```sql
youtube_now_playing (
    community_id, video_id, title, artist, album, duration,
    thumbnail_url, requested_by, started_at, updated_at
)

youtube_search_cache (
    community_id, user_id, query, results, created_at
)

youtube_activity (
    community_id, user_id, action, details, created_at
)
```

---

### Spotify Interaction Module (`action/interactive/spotify_interaction_module_flask/`)

**Purpose**: Spotify integration with OAuth authentication, search, playback control, and media browser source output

**Key Features**:
- **Spotify Web API Integration**: OAuth 2.0 authentication with full playback control
- **Real-time Playback Control**: Play, pause, skip, volume control on user's Spotify devices
- **Device Management**: List and control playback on multiple Spotify devices
- **Media Browser Source Output**: Rich media display with album art, progress bars, and track info
- **Token Management**: Automatic token refresh and secure storage
- **Search and Queue**: Advanced search with playlist integration
- **Multi-User Support**: Per-user authentication within communities

**Command List**:
- `!spotify search <query>` - Search Spotify catalog
- `!spotify play <uri/number>` - Play track on user's Spotify device
- `!spotify current` - Show current playback with progress and device info
- `!spotify pause/resume` - Control playback state
- `!spotify skip` - Skip to next track
- `!spotify devices` - List available Spotify devices
- OAuth authentication flow with secure token management
- Media browser source with real-time progress updates

**Database Schema**:
```sql
spotify_tokens (
    community_id, user_id, access_token, refresh_token,
    expires_at, scope, created_at, updated_at
)

spotify_now_playing (
    community_id, track_uri, track_name, artists, album,
    duration_ms, album_art_url, is_playing, progress_ms,
    requested_by, started_at, updated_at
)

spotify_search_cache (
    community_id, user_id, query, results, created_at
)
```

---

### Browser Source Core Module (`core/browser_source_core_module_flask/`)

**Purpose**: Multi-threaded browser source management system for OBS integration with ticker, media, and general display sources

**Key Features**:
- **Multi-threaded Architecture**: ThreadPoolExecutor for handling hundreds of concurrent browser sources
- **WebSocket Communication**: Real-time updates to browser sources via WebSocket connections
- **Three Source Types**: Ticker, Media, and General browser sources for different display needs
- **Unique Community URLs**: Each community gets unique URLs for each source type with secure tokens
- **OBS Integration**: Optimized for OBS Studio browser source plugin with auto-refresh and styling
- **Router Integration**: Receives display data from interaction modules via router
- **Portal Integration**: Community admins can view and manage browser source URLs

**Browser Source Types**:
- **Ticker Source**: Scrolling text messages at bottom of screen for notifications and alerts
- **Media Source**: Rich media display for music, videos, images with metadata and progress
- **General Source**: Custom HTML/CSS content for forms, announcements, and interactive elements

**Key Features List**:
- Unique URLs per community: `/browser/source/{token}/ticker`, `/browser/source/{token}/media`, `/browser/source/{token}/general`
- WebSocket real-time updates for immediate display changes
- Queue management for ticker messages with priority and duration
- Media attribution display with artist, song name, album art, and progress bars
- Responsive design that works across different OBS scene sizes
- Access logging and analytics for browser source usage
- Token management with regeneration capabilities

**Database Schema**:
```sql
browser_source_tokens (
    community_id, source_type, token, is_active, created_at
)

browser_source_history (
    community_id, source_type, content, session_id, created_at
)

browser_source_access_log (
    community_id, source_type, ip_address, user_agent, accessed_at
)
```

**Integration Flow**:
1. **Module Response**: Interaction module sends response with browser source data
2. **Router Processing**: Router routes browser source responses to browser source core module
3. **WebSocket Distribution**: Browser source module distributes updates via WebSocket to connected sources
4. **OBS Display**: Browser sources in OBS receive updates and display content in real-time
5. **Portal Management**: Community admins can view URLs and manage settings through portal

**Music Module Browser Source Integration**:
- YouTube Music and Spotify modules send media responses to browser source core
- Media browser source displays track information with album art and attribution
- Real-time progress updates for Spotify playback
- Automatic timeout and cleanup for media displays
- Responsive design for different OBS scene layouts

**Browser Source Implementation Details**:
- **Transparent Backgrounds**: All browser source templates use `background: transparent;` for proper OBS compositing
- **WebSocket Communication**: Real-time bidirectional communication between browser sources and core module
- **Multi-threaded Processing**: ThreadPoolExecutor handles hundreds of concurrent connections
- **Connection Management**: Automatic connection tracking, cleanup, and stale connection removal
- **Queue System**: Priority-based message queuing for ticker with overflow protection
- **Analytics Integration**: Comprehensive tracking of browser source usage and interactions
- **Template System**: Modular HTML/CSS/JS templates for each source type
- **Responsive Design**: Works across different OBS scene sizes and aspect ratios
- **Security**: Token-based authentication with unique URLs per community and source type

**Browser Source Templates**:
- **Ticker Template**: Scrolling text with animations, priority queuing, and style variants
- **Media Template**: Music display with album art, progress bars, and service indicators
- **General Template**: Flexible content display with HTML, forms, announcements, and alerts
- **CSS Framework**: Modern CSS with animations, transitions, and responsive design
- **JavaScript**: WebSocket handling, automatic reconnection, and analytics tracking

**OBS Integration Features**:
- **Transparent Backgrounds**: Proper alpha channel support for overlay compositing
- **Auto-refresh**: Automatic reconnection on connection loss
- **Performance Optimized**: Minimal resource usage with efficient DOM updates
- **Cross-browser**: Compatible with OBS browser source engine
- **Responsive Layouts**: Adapts to different scene sizes and orientations

---

### Server Manager Interaction Module (`action/interactive/server_manager_interaction_module/`)

**Purpose**: Comprehensive game server and voice server management with RCON, Mumble Ice RPC, and TeamSpeak ServerQuery support. Replaces and absorbs `server_status_interaction_module`.

**Key Features**:
- **RCON Management**: Source RCON protocol support for Rust, Minecraft, CS2, ARK, Valheim, Palworld, Factorio, and more
- **Mumble Voice Servers**: Full management via Ice RPC — users, channels, kick/ban, broadcast messages
- **TeamSpeak Voice Servers**: Full management via ServerQuery — clients, channels, kick/ban, messaging
- **Encrypted Credentials**: AES-256-GCM encrypted server passwords stored in PostgreSQL, decrypted only at connection time
- **Reputation Auto-Moderation**: FICO-style scoring (300-850) with configurable auto-kick/ban thresholds per server
- **Cross-Platform Sync**: Integrates with security_core_module to propagate bans/kicks to/from game servers
- **Connection Pooling**: TTL-based RCON connection reuse (60s default)
- **Audit Logging**: All commands logged to rcon_command_log, all automated actions to server_access_log

**Supported Server Types**:
- `rcon` — Game servers using Source RCON (Rust, Minecraft, CS2, ARK, Valheim, etc.)
- `mumble` — Mumble voice servers via Ice RPC
- `teamspeak` — TeamSpeak voice servers via ServerQuery
- `status_only` — Legacy status-only monitoring (backward compatible)

**Supported Game Types**: rust, minecraft, cs2, ark, valheim, palworld, factorio, conan_exiles, 7dtd, squad, unturned, terraria, starbound, source, mumble, teamspeak, other

**Database Tables**:
```sql
-- Extended existing table
server_status_configs (+ server_type, host, game_port, rcon_port, credential_enc, credential_iv, game_type, visibility, display_name, added_by, metadata, deleted_at)

-- New tables
rcon_command_log (id, server_config_id, user_id, command, response_summary, success, executed_at)
server_ban_sync (id, community_id, server_config_id, sync_enabled, sync_direction, last_synced_at, created_at)
server_access_policies (id, server_config_id, community_id, require_community_member, auto_kick_enabled, auto_kick_threshold, auto_ban_enabled, auto_ban_threshold, ...)
server_access_log (id, server_config_id, target_player, action, reason, reputation_score, created_at)
```

---

## Related Documentation

- [module-details-core.md](module-details-core.md) - Core components and trigger modules
- [CLAUDE.md](../CLAUDE.md) - Project overview and architecture
- [API Documentation](api-reference.md) - Complete API endpoint reference
- [Environment Variables](environment-variables.md) - Configuration reference
- [Deployment Guide](deployment.md) - Docker and Kubernetes deployment

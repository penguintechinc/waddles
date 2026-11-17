# Platform Commands Reference

> Full command reference for Waddles on Discord, Slack, and Twitch.
> All commands forward to the router and are dispatched to the appropriate module.

## Platform Capabilities

| Feature | Discord | Slack | Twitch |
|---------|---------|-------|--------|
| Slash commands | `/command` | `/command` | ❌ |
| Prefix commands | `!command` | ❌ | `!command` |
| Modals/forms | ✅ | ✅ Block Kit | ❌ |
| Ephemeral replies | ✅ | ✅ | ❌ |
| Native polls | ✅ (py-cord ≥2.6) | ❌ | ✅ Helix API |
| Buttons/selects | ✅ | ✅ Block Kit | ❌ |

## Context Resolution Order

When a command is received, Waddles resolves the target community in this order:

1. **Per-user override** — Redis key `ctx:{platform}:{user_id}:{channel_id}` (TTL 24h), backed by `user_platform_context` table
2. **Channel/server default** — `community_servers.is_primary = true` for this channel/server (cached)
3. **Error** — "No community configured for this channel" if neither applies

Security gate: users can only switch context to communities that have an **approved, active** link to the channel (`community_servers WHERE status='approved' AND is_active=true`).

---

## Linking & Context Commands

These commands manage platform ↔ community connections. They require **server admin / broadcaster** permission.

| Command | Discord | Slack | Twitch | Description |
|---------|---------|-------|--------|-------------|
| Request link | `/join <community>` | `/waddlebot join <community>` | `!join <community>` | Request this channel be linked to a community |
| Approve community request | `/approve <community>` | `/waddlebot approve <community>` | `!approve <community>` | Approve a pending community-initiated link request |
| Remove link | `/leave <community>` | `/waddlebot leave <community>` | `!leave <community>` | Remove community link |
| List linked communities | `/linked` | `/waddlebot linked` | `!linked` | Show all approved links |
| Check link status | `/link status` | `/waddlebot link status` | `!link status` | Show pending requests |
| Set default community | `/link default <community>` | `/waddlebot link default <community>` | `!link default <community>` | Set default community for this channel |

### Context Switching (all users)

| Command | Discord | Slack | Twitch | Description |
|---------|---------|-------|--------|-------------|
| Show context | `/context` | `/context` | `!context` | Show current community context |
| Switch context | `/context switch <name>` | `/context switch <name>` | `!context <name>` | Switch to a linked community (must be a member) |
| Reset context | `/context reset` | `/context reset` | `!context reset` | Reset to channel default |
| Set channel default | `/context default <name>` | `/context default <name>` | `!context default <name>` | Set channel default (admin/broadcaster only) |

---

## Module Commands

### Forms (`forms` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| List forms | `/form list` | `!form list` | Show available community forms |
| Submit a form | `/form submit <name>` | `!form <name>` | Open form (modal on Discord, text prompt on Twitch) |

### Polls (`polls` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| List polls | `/poll list` | `!poll list` | Show active polls |
| Vote on poll | `/poll vote <name>` | `!poll vote <name>` | Vote (native discord.Poll / Twitch Helix poll) |

### Support / Tickets (`support` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| New ticket | `/ticket new [category]` | `!ticket <description>` | Create a support ticket |
| Check status | `/ticket status` | `!ticket status` | View your open tickets |

### Loyalty / Currency (`loyalty` module)

| Command | Discord | Twitch | Description | Permission |
|---------|---------|--------|-------------|-----------|
| Balance | `/balance` | `!balance` | Check point balance | everyone |
| Give points | `/give @user <amount>` | `!give @user <amount>` | Transfer points to a user | registered |
| Slots | `/slots` | `!slots` | Play slots minigame | registered |
| Duel | `/duel @user <bet>` | `!duel @user <bet>` | Challenge to a duel | registered |
| Giveaway | `/giveaway` | — | Start a points giveaway (admin) | moderator |

### Quotes & Memories (`memories` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Add quote | `/quote add` | `!quote add <text>` | Save a quote |
| Random quote | `/quote random` | `!quote random` | Get a random quote |
| Search quotes | `/quote search <term>` | `!quote search <term>` | Search saved quotes |
| Bookmark | `/bookmark add <url>` | `!bookmark <url>` | Save a URL |
| Reminder | `/remind me <time> <text>` | `!remind <time> <text>` | Set a reminder |

### LFG — Looking for Group (`lfg` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Create group | `/lfg create` | `!lfg create <game>` | Create an LFG listing |
| List groups | `/lfg list [game]` | `!lfg list` | Browse open groups |
| Join group | `/lfg join <id>` | `!lfg join <id>` | Join a group |
| Leave group | `/lfg leave` | `!lfg leave` | Leave your current group |
| Cancel | `/lfg cancel` | — | Cancel your listing (creator only) |

### Calendar & Events (`calendar` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| List events | `/event list` | `!event list` | Show upcoming community events |
| View event | `/event view <id>` | `!event view <id>` | Show event details |
| RSVP | `/event rsvp <id>` | `!rsvp <id>` | RSVP to an event |

### Shoutouts (`shoutout` module)

| Command | Discord | Twitch | Description | Permission |
|---------|---------|--------|-------------|-----------|
| Shoutout | `/so @user` | `!so <username>` | Give a shoutout | moderator |

### Translation (`translate` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Translate | `/translate <lang> <text>` | `!translate <lang> <text>` | Translate text to target language |

### Server Status (`server_status` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Check status | `/status [game]` | `!status [game]` | Check game server status |

### Clips (`clip` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Bookmark clip | `/clip bookmark <url>` | `!clip` | Bookmark or create a clip |
| List clips | `/clip list` | `!clip list` | Browse saved clips |
| Highlight | `/clip highlight <id>` | — | Mark clip as highlight |

### Aliases (`alias` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Create alias | `/alias create <name> <command>` | `!alias create <name> <cmd>` | Create a command alias |
| List aliases | `/alias list` | `!alias list` | Show all aliases |
| Delete alias | `/alias delete <name>` | `!alias delete <name>` | Remove an alias |

### AI Assistant (`ai` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Ask AI | `/ask <question>` | `!ask <question>` | Ask the community AI assistant |

### Reputation (`reputation` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| View rep | `/rep @user` | `!rep @user` | View a user's reputation score |

### Labels (`labels` module)

| Command | Discord | Twitch | Description | Permission |
|---------|---------|--------|-------------|-----------|
| Add label | `/label add @user <label>` | `!label add @user <label>` | Apply a label to a user | moderator |
| List labels | `/label list` | `!label list` | Show available labels (admin) | admin |

### Leaderboard (`leaderboard` module)

| Command | Discord | Twitch | Description |
|---------|---------|--------|-------------|
| Leaderboard | `/top [category]` | `!top [category]` | View community leaderboard |

---

## Permission Levels

| Level | Description |
|-------|-------------|
| `everyone` | Any user in the channel/server |
| `registered` | Users who have linked their Waddles account |
| `moderator` | Moderators, Discord server mod role, Twitch moderator |
| `admin` | Discord server admin, Twitch broadcaster, Slack workspace admin |

---

## Adding a New Platform

To add a new platform (e.g., Guilded, Matrix, Telegram):

1. Implement a connector bundle under `bundles/` and wire its receiver in `core/svc_ingest/`
2. Register the platform in `hub_api/` (`supported_platforms`) and add OAuth2 provider support if applicable

No router or module changes are needed — the `commands` table's `community_id = NULL` rows apply globally.

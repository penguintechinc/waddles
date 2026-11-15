# Chat Commands

All bot commands are prefixed with `!`. Aliases (alternate command names) are listed in the Command column. Permissions: **everyone** can run most commands; **moderator+admin** (community owners/admins/moderators) required for write operations; **everyone** for read-only. Feature flags gate each group independently (default ON unless noted).

---

## Music & Song Requests

| Command | Aliases | Who | Purpose | Reply (example) | Flag |
|---------|---------|-----|---------|-----------------|------|
| `!sr <url\|query>` | `!songrequest` | Everyone | Enqueue a song to the Music Station | *(routed to music app)* | `waddles.social.music` |
| `!sr status` | — | Everyone | Check if song requests are enabled | `song requests: enabled` | `waddles.social.music` |
| `!sr pause` | — | Mod+ | Pause playback | *(routed to music app)* | `waddles.social.music` |
| `!sr resume` | — | Mod+ | Resume playback | *(routed to music app)* | `waddles.social.music` |
| `!sr set youtube-labels <labels>` | — | Mod+ | Set YouTube title label allowlist | *(routed to music app)* | `waddles.social.music` (youtube_labels subset) |
| `!songqueue` | `!sq` | Everyone | Link to public queue page | `Song queue: {base}/community/{community_id}/music` (or `song queue page is not configured`) | PostHog `waddles.music.queue-page` (default OFF) |

---

## Custom Aliases

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!alias <name> <cmd> […args]` | Mod+ | Create alias | `alias set: !{name} → !{target}` | `waddles.bot.command_aliases` |
| `!alias add <name> <cmd>` | Mod+ | Create (explicit form) | `alias set: !{name} → !{target}` | `waddles.bot.command_aliases` |
| `!alias list` | Everyone | List all aliases | `aliases: !xx → !hello, !yy → !ping, …` | `waddles.bot.command_aliases` |
| `!unalias <name>` | Mod+ | Delete alias | `alias removed: !{name}` | `waddles.bot.command_aliases` |
| `!alias delete <name>` | Mod+ | Delete (explicit form) | `alias removed: !{name}` | `waddles.bot.command_aliases` |

---

## Community Context Switch

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!cc` | Everyone | Show current community & linked communities | `community context: {current} (default) — available: {others}` | `waddles.community.context` |
| `!cc <name>` | Everyone | Switch to a community (24h override) | `switched to {name} for 24h` | `waddles.community.context` |
| `!cc default` | Everyone | Reset to channel's default community | `community context reset to {primary} (default)` | `waddles.community.context` |
| `!cc reset` | Everyone | Reset to channel's default community | `community context reset to {primary} (default)` | `waddles.community.context` |

---

## Shoutouts

| Command | Aliases | Who | Purpose | Reply (example) | Flag |
|---------|---------|-----|---------|-----------------|------|
| `!so <user>` | `!shoutout` | Config-gated* | Text shoutout on Twitch | *(routed to shoutout app)* | `waddles.bot.shoutout` |
| `!vso <user>` | — | Config-gated* | Video/clip shoutout on Twitch | *(routed to shoutout app)* | `waddles.bot.shoutout` |

*Permission based on `shoutout_config.so_permission` / `vso_permission` (`admin_only` / `mod` / `vip` / `subscriber` / `everyone`); default is `mod` if config unavailable.

---

## Reputation

| Command | Aliases | Who | Purpose | Reply (example) | Flag |
|---------|---------|-----|---------|-----------------|------|
| `!reputation` | `!rep` | Everyone | View own reputation (global + community) | `{display_name}: Global {tier} ({score}), {community} {tier} ({score})` | *(always on)* |

---

## Community Loyalty (Points & Rewards)

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!points` | Everyone | View own points balance | `{display_name}: {n} points` | `waddles.community.loyalty` |
| `!points <user>` | Mod+ | View another member's balance | `{user}: {n} points` | `waddles.community.loyalty` |
| `!points add <user> <n>` | Mod+ | Add points to member | *(routed to loyalty app)* | `waddles.community.loyalty` |
| `!points remove <user> <n>` | Mod+ | Remove points from member | *(routed to loyalty app)* | `waddles.community.loyalty` |
| `!top` | Everyone | Top-10 leaderboard | `**Leaderboard:**\n1. {user}: {points}\n2. …` | `waddles.community.loyalty` |
| `!shop` | Everyone | List redeemable items | *(routed to loyalty app)* | `waddles.community.loyalty` |
| `!redeem <sku>` | Everyone | Spend points on item | *(routed to loyalty app)* | `waddles.community.loyalty` |

---

## Quotes

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!quote` | Everyone | Show help | `Quote commands: \`!quote add <text>\` \| \`!quote <id>\` \| \`!quote random\`` | *(always on)* |
| `!quote add <text>` | Everyone | Add a quote (approval pending) | *(routed to quote app)* | *(always on)* |
| `!quote <id>` | Everyone | Fetch quote by ID | `#{id}: "{text}" — {author}` | *(always on)* |
| `!quote random` | Everyone | Fetch random approved quote | `#{id}: "{text}" — {author}` | *(always on)* |

---

## Welcome

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| *(automatic on first message)* | — | First-time user greeting | `Welcome to the community, {username}!` | *(always on)* |

---

## Polls

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!poll` | Everyone | Show help | `Poll commands: \`!poll create …\` \| \`!poll list\` \| …` | *(always on)* |
| `!poll create "<title>" "<opt1>" "<opt2>" …` | Everyone | Create poll | `Poll created! ID: {id}\n…\nVote: \`!poll vote {id} <option>\`` | *(always on)* |
| `!poll vote <poll_id> <option_index>` | Everyone | Vote on poll | *(reply varies)* | *(always on)* |
| `!poll close <poll_id>` | Everyone | Close poll | *(reply varies)* | *(always on)* |
| `!poll list` | Everyone | List active polls | `**Active Polls:**\n…` | *(always on)* |
| `!poll view <poll_id>` | Everyone | View specific poll | `**Poll {id}:**\n…\nOptions:\n…` | *(always on)* |

---

## Announcements

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!announce publish <id>` | Everyone | Broadcast announcement to all channels | *(routed to announcement app)* | *(gated by config)* |

---

## Forums

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!forum create <title> \| <body>` | Everyone | Create forum post | *(routed to forums app)* | *(gated by config)* |
| `!forum reply <post_id> \| <content>` | Everyone | Reply to forum post | *(routed to forums app)* | *(gated by config)* |

---

## Chat History & Channels

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!chat-history` | Everyone | View recent chat (newest first) | `**Chat History:**\n[date] {user}: {msg}\n…` | *(always on)* |
| `!channels` | Everyone | List active channels | `**Chat Channels:**\n- {name}: {count} messages\n…` | *(always on)* |

---

## Inventory (Quartermaster)

| Command | Who | Purpose | Reply (example) | Flag |
|---------|-----|---------|-----------------|------|
| `!inventory add <name> [-t tags] [-o owner]` | Mod+ | Add item to inventory | `Added {name} to inventory` | *(always on)* |
| `!inventory remove <name>` | Mod+ | Remove item from inventory | `Removed {name}` | *(always on)* |
| `!inventory list` | Everyone | List all items | `**Inventory:**\n- {name}: {qty} available\n…` | *(always on)* |
| `!inventory checkout …` | — | *(not yet implemented)* | `checkout/checkin isn't wired up yet…` | *(stub)* |

---

## Built-In Commands (Core Bot)

| Command | Who | Purpose | Reply (example) |
|---------|-----|---------|-----------------|
| `!ping` | Everyone | Latency check | `pong 🐧` |
| `!hello` / `!hi` / `!hey` | Everyone | Greet bot | `Hey {user}! 👋 waddles is online.` |
| `!help` / `!commands` | Everyone | List all commands | *(grouped command list)* |
| `!echo <text>` | Everyone | Echo text back | `{text}` |
| `!waddle` | Everyone | Penguin waddle | `🐧 *waddles across {platform}*` |
| `!roll` / `!dice` | Everyone | Roll a d6 | `🎲 {user} rolled a {1-6}` |
| `!flip` / `!coin` | Everyone | Coin flip | `🪙 Heads` or `Tails` |
| `!8ball <question>` | Everyone | Magic 8-Ball | `🎱 {answer}` |
| `!hug [user]` | Everyone | Hug someone | `{user} gives {target} a warm hug! 🤗` |
| `!love [user]` | Everyone | Love match % | `💕 {user} + {target} = {0-100}% love match!` |
| `!lurk` | Everyone | Lurk mode | `{user} slips into the shadows to lurk 👀` |
| `!followage` | Everyone | Twitch followage | `Followage tracking is coming soon! 🐧` |
| `!uptime` | Everyone | Bot uptime | `waddles has been up for {Xh Ym Zs} 🐧` |
| `!time` | Everyone | Server time (UTC) | `Server time: {YYYY-MM-DD HH:MM:SS UTC}` |
| `!rules` | Everyone | Community rules | `1) Be kind  2) No spam  3) Have fun 🐧` |
| `!bot` / `!about` | Everyone | About waddles | `I'm waddles 🐧 — a multi-platform community bot by PenguinTech.` |
| `!socials` | Everyone | Social media | `Follow waddles: twitter.com/waddlebot \| instagram.com/waddlebot` |
| `!discord` | Everyone | Discord invite | `Join our Discord: discord.gg/waddlebot` |

---

## Pages & Overlays

| Name | Path | Purpose | Access |
|------|------|---------|--------|
| **Music Queue** | `/community/<community_id>/music` | Public queue page (linked by `!sq`/`!songqueue`) | Public (web) |
| **Music Overlay** | `/overlay/<community>/music` | OBS overlay for queue display | External (OBS) |
| **Admin: Music Settings** | `https://{webui}/admin/community/{id}/music` | Configure song requests, pause/resume | Web UI (admin) |
| **Admin: Aliases** | `https://{webui}/admin/community/{id}/aliases` | Manage custom commands | Web UI (admin) |
| **Admin: Shoutouts** | `https://{webui}/admin/community/{id}/shoutouts` | Configure permission levels | Web UI (admin) |
| **Admin: Loyalty** | `https://{webui}/admin/community/{id}/loyalty` | Manage points, shop items, leaderboard | Web UI (admin) |

---

## Common Error States

| Error | Command | Cause | Action |
|-------|---------|-------|--------|
| `song requests: disabled` | `!sr status` | Feature flag `waddles.social.music` is OFF | Enable flag in PostHog |
| `song requests: error - …` | `!sr` / `!sr status` | Music Station unavailable or misconfigured | Check hub-api music service logs |
| `song queue: link not configured (PUBLIC_WEBUI_URL)` | `!sq` / `!songqueue` | `PUBLIC_WEBUI_URL` env var unset | Set PUBLIC_WEBUI_URL in svc-process config |
| `only moderators/admins can change song request settings` | `!sr set youtube-labels` | Caller not in `_ADMIN_ROLES` | Ask a moderator or admin |
| `only moderators/admins can pause song requests` | `!sr pause` / `!sr resume` | Caller not in `_ADMIN_ROLES` | Ask a moderator or admin |
| `unknown setting '...' — supported: youtube-labels` | `!sr set <key>` | `<key>` not implemented | Use `youtube-labels` or wait for other settings |
| `youtube-labels: up to 32 labels, 64 chars each` | `!sr set youtube-labels` | Label count >32 or label length >64 | Reduce label list or individual lengths |
| `alias names are letters, numbers, - and _ (max 32)` | `!alias` | Alias name contains invalid chars or >32 chars | Use only `[a-z0-9_-]`, max 32 chars |
| `!{name} is a built-in command and can't be aliased` | `!alias` | Alias name conflicts with bot command | Choose a different alias name |
| `an alias can't run !alias` | `!alias` | Alias expands to `!alias` or `!unalias` | Use a different target command |
| `unknown command: …` | `!alias` | Expansion target is not a known command | Target must be a registered bot or feature command |
| `only moderators/admins can set aliases` | `!alias` | Caller not in `_ADMIN_ROLES` | Ask a moderator or admin |
| `no aliases set — try !alias xx somecommand` | `!alias` | No aliases exist yet | Create one with `!alias <name> <cmd>` |
| `community context: this channel isn't linked to any community yet` | `!cc` | Channel has no linked communities | Contact server admins to link the channel |
| `community context lookup is unavailable right now` | `!cc` | DB error or timeout | Try again in a few seconds |
| `no community named '…' on this channel — try: …` | `!cc <name>` | Typed community name doesn't match linked ones | Use exact name or one of the suggestions |
| `that doesn't look like a Twitch username` | `!so` / `!vso` | Target doesn't match `[a-z0-9_]{3,25}` post-normalization | Use a valid Twitch login (3-25 alphanumeric/underscore) |
| `you can't shout yourself out` | `!so` / `!vso` | Target matches caller's own login | Shout out someone else instead |
| `you don't have permission to shout out` | `!so` / `!vso` | Permission level (`so_permission`/`vso_permission`) not met | Check shoutout config for your role |
| `reputation lookup is unavailable right now` | `!reputation` / `!rep` | DB error or timeout | Try again in a few seconds |
| `only moderators/admins can adjust points` | `!points add/remove` | Caller not in `_ADMIN_ROLES` | Ask a moderator or admin |
| `loyalty unavailable` | `!points` / `!top` / `!shop` / `!redeem` | Feature flag `waddles.community.loyalty` is OFF | Enable flag in PostHog |
| `Unknown quote command: …` | `!quote` | Subcommand not recognized | Use `add`, numeric ID, or `random` |
| `Quote #{id} not found` | `!quote <id>` | Quote ID doesn't exist | Try `!quote random` or list available IDs |
| `No quotes found` | `!quote random` | No approved quotes in community | Add one with `!quote add <text>` |
| `that command hit a snag 🐧` | *(any feature command)* | Feature bundle crashed (rare) | Check svc-process logs, try again |
| `Unknown command. Try !help` | `!(unknown)` | Command not recognized & no matching alias | Check `!help` or create an alias with `!alias` |

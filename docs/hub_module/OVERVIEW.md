# Hub Module

> Central web portal and API gateway providing administration, community management, real-time communication, and unified access to all Waddles microservices through an integrated web interface.

## Purpose

The Hub Module is the primary administrative portal and unified interface for Waddles. It provides a full-stack application with a Node.js/Express backend and React-based frontend SPA that enables administrators and community managers to configure, monitor, and manage communities, users, and integrated modules. The module acts as an API gateway, proxying requests to downstream Waddles microservices while providing its own management capabilities.

The Hub Module handles authentication across multiple platforms (OAuth2 with Twitch, Discord, YouTube, etc.), session management, user administration, community configuration, module discovery and management, real-time updates via WebSockets, analytics, and broadcast capabilities. It serves both public-facing pages (community directory, statistics) and admin dashboards with role-based access control.

The module is central to the Waddles architecture, sitting at the intersection of user authentication (via Identity Core Module), community management (via Community Module), and all downstream feature modules, coordinating their configuration and providing a unified management experience.

## Key Capabilities

- Multi-platform OAuth2 authentication (Twitch, Discord, YouTube, and more)
- Community creation and management interface
- User and role management with granular permissions
- Module discovery, registration, and configuration
- Real-time updates and notifications via WebSocket
- Broadcast and announcement system
- Analytics and statistics dashboard
- Admin panel for super-admins and community moderators
- Public community directory and search
- Hub Channels (chat, forum, voice) with cross-platform bridging via mirror groups
- Community Interaction page — Discord-like layout with channel sidebar, real-time chat, threaded forums, and voice/video rooms
- Ticketing and calendar integration
- Leaderboard and loyalty program configuration
- Quartermaster — community inventory management with claim/return workflow
- Personal Access Tokens (PAT) and Community Access Tokens (CAT) for API access
- CAPTCHA support (reCAPTCHA v2 / Cloudflare Turnstile) on registration
- WebAuthn passkey login for existing users
- Community join policy: open, approval-required, or invite-only
- **Module management** — Enable/disable modules per community (non-core only); core modules (identity, workflow) cannot be disabled
- **Platform command reference** — Admin Commands page shows all registered commands with platform, module, permission, and enabled/disabled status
- **Server/channel linking** — Bi-directional handshake: either community admin (WebUI) or platform owner (bot command) can initiate; both sides must approve
- **Platform settings** — Set default community per linked server/channel; grouped by platform
- **My Channels** — User personal dashboard page to manage their own platform channel connections and request community links
- **Community context switching** — Per-user community context override via `!context` / `/context` bot commands; validated against approved links

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, accessing dashboards, common workflows |
| [API.md](API.md) | Backend REST endpoints, WebSocket events, formats |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Frontend/backend design, data flows, components |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, OAuth setup, service discovery |
| [TESTING.md](TESTING.md) | Test strategy, API testing, WebUI testing |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, debug steps, FAQ |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, breaking changes, migrations |

## OAuth Credential Ownership Model

OAuth credentials are now managed at the correct ownership layer rather than centrally by superadmins:

| Credential Type | Where managed | Route prefix | Auth required |
|---|---|---|---|
| Bot credentials | Superadmin → Platform Config | `/superadmin/platform-config` | `super_admin` |
| Community OAuth | Community admin → Connected Platforms | `/admin/:communityId/oauth/credentials` | Community admin |
| User OAuth | User → Account Settings | `/user/oauth/credentials` | Authenticated user (own records only) |

**Security:** User OAuth endpoints enforce ownership via `req.user.id` from the JWT — users can never access another user's credentials. Community OAuth endpoints verify the credential's `community_id` matches the route param before any mutation.

## New in v1.3.0

- **Platform Feature Parity** — All 14 modules now accessible via Discord slash commands, Slack named slash commands, and Twitch `!` prefix commands — no WebUI required
- **Module Management UI** — `AdminModules.jsx` at `/admin/:communityId/modules` — enable/disable any non-core module with a toggle; core modules (identity, workflow) are greyed out
- **Command Reference Page** — `AdminCommands.jsx` at `/admin/:communityId/commands` — table of all registered commands with platform badge, module, permission level, and enabled/disabled status
- **Server/Channel Linking** — Bi-directional handshake via bot commands (`/join`, `/approve`, `/leave`) or WebUI "Request Link" form; `initiated_by` badge shows who requested
- **Platform Settings Page** — `AdminPlatformSettings.jsx` at `/admin/:communityId/platform-settings` — manage default community per linked server; grouped by platform
- **My Channels** — `MyChannels.jsx` at `/dashboard/my-channels` — personal page for users to manage their own platform channel connections
- **Community Context Switching** — Per-user context override via `/context switch <community>` on all platforms; validated against approved links; Redis-cached with DB fallback
- **Shared Platform Library** — libs/platform_receiver provides `PlatformReceiverBase`, event schema, and response helpers for all receiver bots; designed for future platform additions
- **Migrations 051–054** — Corrected `is_core` flags, added `initiated_by`/`link_type` to server link requests, seeded 60+ platform commands, added `user_platform_context` table

## New in v1.1.0

- **All-Platform Dashboard** — Platform Dashboard now shows all 10 supported platforms (Discord, Twitch, Slack, YouTube, KICK, Telegram, Matrix, Guilded, Revolt, Hub Chat)
- **Spotlighted Communities** — Public home page highlights the top 5 active public communities by member count (`GET /api/v1/public/communities/spotlighted`)
- **Marketing & Branding** — Hero, features, CTA, and footer text updated to "workforce & communities" language
- **Form Results Visibility** — New `results_visibility` field on forms controls who can view submissions (community, registered, submitter_and_admins, admins)
- **Calendar Module** — Full Express API for user-facing calendar (OAuth, availability, booking pages, bookings, group scheduling) and admin event management (CRUD, approval, RSVPs). New admin Calendar Events page. Routes at `/api/v1/calendar/*` and `/api/v1/admin/:communityId/calendar/*`
- **Support Community Type + Ticket System** — New `support` community type with ticket categories, auto-numbered tickets (SUP-00001), statuses (open/in_progress/waiting/resolved/closed), priorities, internal/public comments, admin dashboard + ticket detail pages, and member submission + my-tickets pages

## Quick Reference

| Item | Value |
|---|---|
| Source | `admin/hub_module/` |
| Backend Language | Node.js 20+ (Express) |
| Frontend Language | JavaScript/React 18 |
| Backend Port | 8060 |
| Real-time | WebSocket (Socket.io) |
| Database | PostgreSQL |
| Maintained by | Penguin Tech Inc |

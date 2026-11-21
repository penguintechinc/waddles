# Hub Module Release Notes

## Version 1.3.0

**Release Date:** 2026-02-24

### New Features

#### Superadmin Platform Analytics Dashboard
- New `/superadmin/analytics` page with cross-community metrics
- Summary cards: Total Users, Active Users (30d), Total Communities, Avg Platform Reputation
- Reputation tier distribution (FICO-style: Exceptional → Poor) with percentage bars
- Platform breakdown showing community counts per platform (Discord, Slack, Twitch, etc.)
- User growth chart with 30d/90d/1y period selector (CSS-only bar chart)
- Activity segments: 24h / 7d / 30d / 90d / Inactive with stacked bar visualization
- Community type distribution breakdown
- New `platform_user_reputation` SQL view: global community reputation = platform reputation (zero-migration promotion)
- New `platform_analytics_snapshots` table for future daily metric rollups
- Backend: 4 new API endpoints under `GET /api/v1/superadmin/analytics/*`

#### Full Platform Feature Parity — Discord, Slack, Twitch
- All 14 interactive modules now accessible from Discord, Slack, and Twitch without visiting the website
- Discord: Named `SlashCommandGroup`s per module (`/form`, `/poll`, `/ticket`, `/balance`, `/quote`, `/lfg`, `/event`, `/so`, `/translate`, `/clip`, `/alias`, `/ask`, `/rep`, `/top`, `/context`, `/join`)
- Slack: Named slash command handlers for each module (`/form`, `/poll`, `/ticket`, etc.) plus `/context` and server-linking commands
- Twitch: All `!` prefix commands routed through the existing chat handler (generic forwarding via `commands` table); broadcaster-only commands short-circuited locally for fast feedback
- All module commands seeded in `commands` table (migration 053) as global defaults with `community_id=NULL`

#### Community Context Switching
- Users can switch between communities linked to their channel: `!context <name>` / `/context switch <name>`
- Per-user override stored in new `user_platform_context` table (migration 054) with Redis 24h cache
- Security gate: only communities with an **approved** `community_servers` link to the channel are eligible
- Reset to channel default: `!context reset` / `/context reset`
- Channel owners set the default community: `!link default <community>` / `/link default <community>` (admin/broadcaster only)
- New `ContextService` at `processing/router_module/services/context_service.py`

#### Server/Channel ↔ Community Linking Handshake
- Bi-directional approval flow: either community admin (WebUI) or platform owner (bot command) can initiate
- Platform commands: `/join <community>`, `/approve <community>`, `/leave <community>`, `/linked`, `/link status`, `/link default <community>` on Discord/Slack; `!join`, `!approve`, `!leave`, `!linked`, `!link` on Twitch
- New `initiated_by` column in `server_link_requests` (migration 052): `'community'` or `'platform'`
- New `link_type` column: `standard`, `read_only`, or `announcement_only`
- New API endpoint: `POST /api/admin/:communityId/server-link-requests` (community-initiated requests)
- `AdminServers.jsx` shows `initiated_by` badge on pending requests

#### Module Enable/Disable Enforcement
- Fixed `is_core` flags (migration 051): only `identity` and `workflow` are truly non-disableable
- `adminController.js` `updateModuleConfig()` now returns HTTP 403 if admin tries to disable a core module
- Command processor already enforced `is_enabled` check per command (confirmed working)
- `AdminModules.jsx` (existing) correctly greys out core module toggles

#### Platform Extensibility — libs/platform_receiver
- New shared library libs/platform_receiver installable via `pip install`
- `PlatformReceiverBase` abstract class: implement `start()`/`stop()`, call `dispatch(build_chat_event(...))` — router integration is automatic
- Standardised event schema: `build_chat_event`, `build_slash_event`, `build_stream_event`
- Shared response utilities: `split_for_chat`, `get_response_content`, `format_error`
- All 5 receiver Dockerfiles updated to install `platform_receiver` alongside `flask_core`
- Adding a new platform (e.g., Kick v2, YouTube Live, X/Twitter) requires only the SDK wrapper, no router changes

#### Interaction Forwarding Fixed
- `command_processor.py` `_process_interaction()` now actually forwards button/modal interactions to modules via `POST {module_url}/api/v1/execute`
- Previously this was a stub that returned the parsed data without calling any module

#### New WebUI Pages
- `/admin/:communityId/commands` — Command Reference: read-only table of all registered commands with platform, module, permission, and enabled status
- `/admin/:communityId/platform-settings` — Platform Settings: per-server default community management for community admins
- `/dashboard/my-channels` — My Channels: personal page for users to manage their own platform accounts, request community links, and approve incoming community requests

### Migration Files
| Migration | Description |
|-----------|-------------|
| `051_fix_is_core_flags.sql` | Resets is_core; only identity+workflow remain non-disableable |
| `052_server_link_requests_initiated_by.sql` | Adds initiated_by, link_type, platform_channel_id to server_link_requests |
| `053_register_platform_commands.sql` | Seeds 60+ global commands for all modules across Discord/Slack/Twitch |
| `054_user_platform_context.sql` | Creates user_platform_context table for per-user context overrides |

---

## Version 1.2.0

**Release Date:** 2026-02-24

### New Features

#### Quartermaster — Community Inventory System
- Generic named-item inventory for communities (e.g. "Barrels", "Licenses", "Loaner Laptops")
- Admin page: Items tab (create/edit/delete items, stock ±) + Claims tab (all active checkouts with force-return)
- Member pages: Browse available inventory and claim items; view and return own claims
- Immutable audit log for all stock changes
- Database: `inventory_items`, `inventory_checkouts`, `inventory_log` tables (migration 014)
- DB helper functions: `update_inventory_on_checkout`, `update_inventory_on_return`, `add_inventory_stock`, `remove_inventory_stock`, `get_inventory_summary`, `search_inventory_items`

#### New Community Types: `workforce` and `support`
- `workforce` type for internal teams, departments, and organisations
- `support` type for help desk communities (was previously enum-only, now fully surfaced in UI)
- Both types appear in Create Community and Community Profile admin dropdowns
- Public browse shows appropriate icons for each type

#### UI Fixes — Existing Modules
- **Admin Members**: Reset Password action in member row now shows modal with copyable temp password
- **Admin AI Insights**: Clicking an insight row loads full detail view
- **Admin Shoutouts**: History tab now has date-range and type filter controls
- **Admin Announcements**: Fixed buttons on announcement cards not triggering actions (were missing `type="button"`, defaulting to form submit)

#### Personal Access Tokens (PAT) and Community Access Tokens (CAT)
- PAT (`wdl_u_*`): One per user, acts as the user; optional scope ceiling; managed at `/account/tokens`
- CAT (`wdl_c_*`): Up to 5 (10 premium) per community; mandatory OAuth2 scopes; managed at `/admin/:communityId/tokens`
- Token prefix routing in auth middleware: `wdl_u_*` → user lookup, `wdl_c_*` → community lookup
- Plaintext shown once on creation with copy button
- Database: `user_access_tokens`, `community_access_tokens` tables (migration 048)
- Scope catalog reused from existing `permission_scopes` table

#### Signup Controls & CAPTCHA
- Super Admin can enable/disable public signup (`allow_public_signup`)
- CAPTCHA support: reCAPTCHA v2 and Cloudflare Turnstile; site/secret keys stored in `hub_settings`
- CAPTCHA validation runs in `authController.register()` before user creation
- Settings managed in Super Admin → Platform Config → Signup & Auth tab
- Database: new `hub_settings` keys via migration 049

#### WebAuthn Passkey Login
- Users can register passkeys (fingerprint, Face ID, hardware key) via Account Settings
- Passkey login button on login page as alternative to password
- Uses `@simplewebauthn/server` with challenge/response flow
- Credentials stored in `user_passkeys` table (migration 049)

#### Community Join Policy
- Admins set `join_mode` per community: `open` (default), `approval`, or `invite`
- `approval` mode: self-join creates a join request; admin reviews at `/admin/:communityId/join-requests`
- `invite` mode: self-join blocked entirely
- Database: `community_join_requests` table (migration 049)

### Testing
- 3 new Playwright E2E test specs: `inventory-workflow`, `token-management`, `join-requests`

---

## Version 1.1.0

**Release Date:** 2026-02-23

### New Features

#### All-Platform Dashboard
- Platform Dashboard now displays all 10 supported platforms (Discord, Twitch, Slack, YouTube, KICK, Telegram, Matrix, Guilded, Revolt, Hub Chat) instead of only 3
- HomePage stats section dynamically shows top platform counts

#### Spotlighted Communities
- New "Spotlighted Communities" section on the public home page
- Shows top 5 active public communities by member count
- Excludes support-type communities
- New backend endpoint: `GET /api/v1/public/communities/spotlighted`

#### Marketing & Branding Updates
- Updated hero, features, CTA, and footer text to mention "workforce & communities"
- Multi-platform feature description now lists all supported platforms

#### Form Results Visibility
- New `results_visibility` field on forms (community, registered, submitter_and_admins, admins)
- Controls who can see submitted form responses
- Default: submitter_and_admins

#### Calendar Module — Express API Routes
- Full user-facing calendar API: OAuth flows, availability settings, booking pages, bookings, group scheduling
- Admin calendar event management: CRUD, approval/rejection, RSVPs, attendees
- New admin page: Calendar Events with create/edit modal, status badges, approve/reject actions
- Routes registered at `/api/v1/calendar/*` (user) and `/api/v1/admin/:communityId/calendar/*` (admin)

#### Support Community Type + Ticket System
- New `support` community type for help desks and customer support
- Database tables: `support_ticket_categories`, `support_tickets`, `support_ticket_comments`
- Auto-generated ticket numbers (SUP-00001 format)
- Ticket statuses: open, in_progress, waiting, resolved, closed
- Priority levels: low, medium, high, urgent
- Category management with custom form fields (up to 8 per category)
- Internal vs public comments for admin-only notes
- Admin pages: Support Dashboard (stats + filters + table) and Ticket Detail (status/priority controls, comments)
- Member pages: Submit Ticket (with dynamic category fields) and My Tickets (with inline detail)

### Testing
- 8 new Playwright E2E test specs: platform-dashboard, calendar-events, calendar-settings, calendar-booking, support-workflow, form-results-visibility, marketing-text, spotlighted-communities

---

## Version 1.0.1

**Release Date:** 2024-03-15

### Overview
This is the initial stable release of the Waddles Hub Module - the central administration portal and community management interface for the Waddles platform.

---

## Features

### Core Functionality

#### Authentication System
- ✅ Email/password authentication with bcrypt hashing
- ✅ OAuth integration (Discord, Twitch, YouTube, KICK, Slack)
- ✅ JWT token-based authentication with 1-hour expiration
- ✅ Refresh token support
- ✅ Email verification system
- ✅ Temporary password system for quick onboarding
- ✅ Multi-platform identity linking
- ✅ Primary identity management

#### Community Management
- ✅ Create and manage communities across multiple platforms
- ✅ Public and private community visibility
- ✅ Join request approval workflow
- ✅ Member role management (admin, moderator, member)
- ✅ Server linking (Discord, Slack, etc.)
- ✅ Mirror groups for cross-channel messaging
- ✅ Community profile customization
- ✅ Custom domain support
- ✅ Browser source overlay generation for OBS

#### Admin Dashboard
- ✅ Comprehensive member management
- ✅ FICO-style reputation system (300-850 scoring)
- ✅ Join request approval/rejection
- ✅ Server link request management
- ✅ Module installation and configuration
- ✅ Announcement creation and broadcasting
- ✅ Analytics integration
- ✅ Security configuration
- ✅ Leaderboard configuration
- ✅ Bot detection and review

#### Module Marketplace
- ✅ Browse available modules
- ✅ Install/uninstall modules
- ✅ Module configuration via JSON
- ✅ Module reviews and ratings
- ✅ Official and community modules

#### Real-Time Chat
- ✅ WebSocket-based chat system (Socket.io)
- ✅ Cross-platform message aggregation
- ✅ Channel-based messaging
- ✅ Real-time message delivery
- ✅ Chat history persistence
- ✅ Message moderation tools

#### Workflows
- ✅ Visual workflow builder (drag-and-drop)
- ✅ Trigger nodes (events, schedules, webhooks)
- ✅ Action nodes (messages, reputation, roles)
- ✅ Condition nodes (if/else logic)
- ✅ Loop nodes (iteration)
- ✅ Data transformation nodes
- ✅ Workflow testing and validation
- ✅ Execution history and logging

#### Loyalty System
- ✅ Virtual currency system
- ✅ Earn rates and multipliers
- ✅ Currency leaderboard
- ✅ Giveaways with entry costs
- ✅ Casino-style games (slots, roulette, coinflip)
- ✅ Gear shop for virtual items
- ✅ Comprehensive statistics

#### Music Module
- ✅ Music provider integration (Spotify, YouTube, etc.)
- ✅ Internet radio station management
- ✅ Playback controls
- ✅ Genre filtering
- ✅ Artist blocking
- ✅ Volume limits
- ✅ DJ approval system

#### Bot Detection
- ✅ Community health grading (A-F)
- ✅ Suspected bot identification
- ✅ Confidence scoring
- ✅ AI-powered behavioral analysis
- ✅ Manual review and flagging
- ✅ Detection reason reporting

#### Analytics
- ✅ Member growth tracking
- ✅ Activity metrics (messages, watch time)
- ✅ Engagement scoring
- ✅ Retention analysis
- ✅ Bad actor detection
- ✅ Data export (CSV, JSON)

#### AI Features
- ✅ AI Insights integration
- ✅ AI Researcher configuration
- ✅ Sentiment analysis
- ✅ Content recommendations
- ✅ Community health analysis
- ✅ Model selection (GPT-4, Claude, etc.)

#### SuperAdmin Panel
- ✅ Platform dashboard
- ✅ Community creation and management
- ✅ Module registry administration
- ✅ Platform OAuth configuration
- ✅ Hub settings management
- ✅ SSL certificate management (Certbot integration)

#### Security Features
- ✅ Helmet.js security headers
- ✅ CORS configuration
- ✅ Rate limiting (100 req/min)
- ✅ CSRF protection
- ✅ XSS sanitization
- ✅ SQL injection prevention (parameterized queries)
- ✅ File upload validation
- ✅ Input validation and sanitization

#### Additional Features
- ✅ Announcement system with broadcasting
- ✅ Shoutout configuration (for creator communities)
- ✅ Translation configuration
- ✅ Cookie consent management (GDPR)
- ✅ Email notification system
- ✅ Live stream aggregation
- ✅ Public community directory
- ✅ User profile management
- ✅ Avatar/banner uploads

---

## Technical Stack

### Backend
- **Runtime:** Node.js 20+
- **Framework:** Express 4.21.2
- **Database:** PostgreSQL 13+ with pg 8.13.1
- **Authentication:** JWT (jsonwebtoken 9.0.2), bcrypt 5.1.1
- **WebSocket:** Socket.io 4.8.1
- **Security:** Helmet 8.0.0, express-rate-limit 7.5.0
- **Validation:** express-validator 7.0.1
- **Storage:** AWS S3 (optional)
- **Email:** Nodemailer 7.0.11

### Frontend
- **Framework:** React 18.3.1
- **Router:** React Router DOM 7.1.1
- **HTTP Client:** Axios 1.7.9
- **WebSocket Client:** Socket.io-client 4.8.1
- **Styling:** TailwindCSS 3.4.17
- **Icons:** Heroicons 2.2.0, Lucide React 0.468.0
- **Build Tool:** Vite 6.2.6
- **Workflow Editor:** ReactFlow 11.10.1

### Database Schema
- 20+ tables for users, communities, modules, etc.
- JSONB columns for flexible configuration
- Indexes for performance optimization
- Foreign key constraints for referential integrity

---

## API Endpoints

### Authentication
- `POST /api/v1/auth/register` - User registration
- `POST /api/v1/auth/login` - Login with email/password
- `GET /api/v1/auth/me` - Get current user
- `GET /api/v1/auth/oauth/:platform` - Start OAuth flow
- `POST /api/v1/auth/refresh` - Refresh JWT token
- `POST /api/v1/auth/logout` - Logout

### Public
- `GET /api/v1/public/stats` - Platform statistics
- `GET /api/v1/public/communities` - List public communities
- `GET /api/v1/public/communities/:id` - Get community details
- `GET /api/v1/public/live` - Live streams

### User
- `GET /api/v1/user/profile` - Get user profile
- `PUT /api/v1/user/profile` - Update profile
- `POST /api/v1/user/profile/avatar` - Upload avatar
- `GET /api/v1/user/identities` - Get linked identities

### Community
- `GET /api/v1/communities/my` - Get user's communities
- `POST /api/v1/communities/:id/join` - Join community
- `GET /api/v1/communities/:id/dashboard` - Community dashboard
- `GET /api/v1/communities/:id/leaderboard` - Leaderboard
- `GET /api/v1/communities/:id/chat/history` - Chat history

### Admin (60+ endpoints)
- Community settings, members, modules, domains
- Announcements, overlays, workflows
- Reputation, bot detection, AI insights
- Loyalty, giveaways, music
- Analytics, security

### SuperAdmin (25+ endpoints)
- Communities, modules, platform config
- Hub settings

---

## Database Migrations

### Version 1.0.1
- Initial schema creation (14 core tables)
- `007_add_translation_config.sql` - Translation configuration

---

## Known Issues

### Minor Issues
1. **WebSocket Reconnection:** Occasional reconnection delay on network interruption (workaround: page refresh)
2. **File Upload Progress:** No progress bar for large file uploads (planned for v1.1)
3. **Mobile Workflow Editor:** Limited functionality on small screens (desktop recommended)
4. **Search Performance:** Search can be slow with >10,000 members (index optimization planned)

### Limitations
1. **OAuth Platforms:** Limited to 5 platforms (Discord, Twitch, YouTube, KICK, Slack)
2. **File Size Limits:** Avatar (5MB), Banner (10MB)
3. **Rate Limiting:** 100 requests per minute per IP
4. **WebSocket Scaling:** Single-server deployment (Redis adapter planned for multi-server)
5. **Email Verification:** Requires SMTP configuration

---

## Upgrade Notes

### From Pre-1.0 to 1.0.1

**Database:**
1. Run migration scripts in order
2. Backup database before upgrading
3. No data loss expected

**Configuration:**
- Set `JWT_SECRET` and `SERVICE_API_KEY` in production
- Update `CORS_ORIGIN` for production domain
- Configure OAuth credentials via SuperAdmin panel

**Breaking Changes:**
- None (initial release)

---

## Security Updates

### Addressed in 1.0.1
- Fixed 30 Dependabot security vulnerabilities (merged 2024-02-20)
- Updated all dependencies to latest secure versions
- Implemented CSRF protection
- Added XSS sanitization middleware
- Enforced rate limiting

---

## Performance Improvements

- Database connection pooling (configurable pool size)
- Query optimization with indexes
- Frontend code splitting
- Static asset caching
- Lazy loading for admin routes

---

## Dependencies

### Production Dependencies (Backend)
```json
{
  "@aws-sdk/client-s3": "^3.700.0",
  "axios": "^1.7.9",
  "bcrypt": "^5.1.1",
  "cookie-parser": "^1.4.6",
  "cors": "^2.8.5",
  "dotenv": "^16.4.7",
  "express": "^4.21.2",
  "express-rate-limit": "^7.5.0",
  "express-validator": "^7.0.1",
  "helmet": "^8.0.0",
  "jsonwebtoken": "^9.0.2",
  "multer": "^1.4.5-lts.1",
  "nodemailer": "^7.0.11",
  "pg": "^8.13.1",
  "socket.io": "^4.8.1",
  "uuid": "^11.0.3",
  "xss": "^1.0.15"
}
```

### Production Dependencies (Frontend)
```json
{
  "@heroicons/react": "^2.2.0",
  "axios": "^1.7.9",
  "lucide-react": "^0.468.0",
  "react": "^18.3.1",
  "react-dom": "^18.3.1",
  "react-router-dom": "^7.1.1",
  "reactflow": "^11.10.1",
  "socket.io-client": "^4.8.1"
}
```

---

## Deployment

### Docker Support
- Multi-stage Dockerfile for optimized builds
- Alpine-based images (small footprint)
- Non-root user (security)
- Health checks included
- Dumb-init for proper signal handling

### System Requirements
- **CPU:** 2+ cores recommended
- **RAM:** 2GB minimum, 4GB recommended
- **Storage:** 10GB+ (database grows with usage)
- **Network:** HTTPS required for production

---

## Documentation

### Available Documentation
1. **API.md** - Complete API reference (1285 lines)
2. **CONFIGURATION.md** - Configuration guide (750 lines)
3. **ARCHITECTURE.md** - System architecture (1046 lines)
4. **USAGE.md** - User guide (1534 lines)
5. **TESTING.md** - Testing procedures (this file)
6. **RELEASE_NOTES.md** - Release notes (this file)
7. **TROUBLESHOOTING.md** - Common issues and solutions

---

## Contributors

- Waddles Development Team
- Community Contributors (see GitHub)

---

## License

Proprietary - All rights reserved

---

## Support

- **Documentation:** `/docs/hub_module/`
- **Issues:** GitHub Issues
- **Community:** Waddles Discord
- **Email:** support@waddlebot.io

---

## Roadmap (Future Releases)

### Version 1.1.0 (Planned)
- [ ] Multi-language support (i18n)
- [ ] Dark mode toggle
- [ ] File upload progress bars
- [ ] Advanced search with filters
- [ ] Notification system
- [ ] Mobile app (React Native)
- [ ] Redis caching layer
- [ ] WebSocket scaling (Redis adapter)
- [ ] Advanced analytics dashboard
- [ ] Automated backups
- [ ] Audit logging
- [ ] Two-factor authentication (2FA)

### Version 1.2.0 (Future)
- [ ] GraphQL API option
- [ ] Webhook system
- [ ] Advanced workflow triggers
- [ ] Custom module development SDK
- [ ] AI-powered moderation
- [ ] Voice chat integration
- [ ] Video streaming support
- [ ] Internationalization (i18n)

### Version 2.0.0 (Long-term)
- [ ] Microservices refactor
- [ ] Kubernetes deployment
- [ ] Multi-tenancy improvements
- [ ] Advanced permissions system
- [ ] Custom theming
- [ ] White-label support
- [ ] Enterprise features

---

## Changelog

### 1.0.1 (2024-03-15)
- Initial stable release
- All core features implemented
- Documentation completed
- Security audit passed
- Performance optimizations applied

---

## Acknowledgments

Special thanks to:
- All contributors to the Waddles project
- Open source community for amazing libraries
- Early adopters for feedback and bug reports

---

## Version History Summary

| Version | Date | Highlights |
|---------|------|-----------|
| 1.0.1 | 2024-03-15 | Initial stable release |
| 1.0.0 | 2024-03-01 | Beta release |
| 0.9.0 | 2024-02-15 | Alpha release |

---

For detailed API changes and migration guides, see individual version sections above.

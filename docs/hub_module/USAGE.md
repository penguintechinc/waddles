# Hub Module Usage Guide

## Overview

The Waddles Hub Module is the central web interface for managing communities, configuring modules, and accessing analytics. This guide covers all WebUI features from user perspective.

**Version:** 1.0.1
**Access URL:** `http://localhost:8060` (development) or your configured domain

---

## Table of Contents

- [Getting Started](#getting-started)
- [Authentication](#authentication)
- [Public Pages](#public-pages)
- [User Dashboard](#user-dashboard)
- [Community Management](#community-management)
- [Admin Dashboard](#admin-dashboard)
- [SuperAdmin Panel](#superadmin-panel)
- [Module Marketplace](#module-marketplace)
- [Chat System](#chat-system)
- [Workflows](#workflows)
- [Analytics](#analytics)
- [Common Tasks](#common-tasks)

---

## Getting Started

### Accessing the Hub

1. Open your web browser
2. Navigate to `http://localhost:8060` (or your hub domain)
3. You'll see the public homepage with:
   - Platform statistics
   - Public communities list
   - Live streams
   - Login button

### Initial Admin Credentials

No default admin account is ever created (CWE-798). On first boot, an
initial super-admin is created **only** if both `INITIAL_ADMIN_EMAIL` and
`INITIAL_ADMIN_PASSWORD` are set (see `config/postgres/migrations/081_seed_default_hub_admin.sql`);
if they're unset, no admin account exists until one is created via the
signup flow or `scripts/seed-admin.sh --email ... --password ...`.

**The initial admin is a platform account, not a person.** It is marked
`hub_users.is_bootstrap` and exists as the operator's break-glass identity. Do not tie it to a
real person's e-mail or identity, and do not use it day to day: create **named global admins**
for each operator (`POST /api/v1/superadmin/users` with `type: global_admin`). The platform
account cannot be deleted, deactivated, demoted from `system` admin or erased (typed 403
`PLATFORM_ACCOUNT_PROTECTED`, audited); the Users page shows it with a "Platform account" badge.
See `docs/GLOBAL_IDENTITY.md` and `docs/PRIVACY.md`.

Local dev (`docker-compose.yml` / `scripts/rebuild-cluster.sh`) sets a
dev-only default via these env vars -- override them in `.env` to change
the local login. **Production/beta must set them explicitly; there is no
built-in fallback.**

---

## Authentication

### Login Methods

#### 1. Email/Password Login

**Steps:**
1. Click "Login" button in navigation
2. Enter email and password
3. Click "Sign In"
4. You'll be redirected to your dashboard

**Screenshot Location:** `/login`

#### 2. OAuth Platform Login

Supported platforms:
- Discord
- Twitch
- YouTube
- KICK
- Slack

**Steps:**
1. Click "Login" button
2. Select "Sign in with [Platform]"
3. Authorize on the platform
4. You'll be redirected back and logged in

**Note:** OAuth platforms must be configured by SuperAdmin first.

### User Registration

If public signup is enabled:

**Steps:**
1. Click "Register" on login page
2. Fill in:
   - Email address
   - Username
   - Password (minimum 8 characters)
3. Click "Create Account"
4. Check email for verification link (if email verification enabled)
5. Click verification link
6. Log in with your credentials

### Account Linking

Link multiple platform accounts to one hub account:

**Steps:**
1. Go to **Dashboard > Account Settings**
2. Scroll to "Linked Accounts"
3. Click "Link [Platform]"
4. Authorize on the platform
5. Platform is now linked to your account

**Benefits:**
- Single sign-on across platforms
- Unified identity
- Access to all your communities

---

## Public Pages

### Homepage

**URL:** `/`

Features:
- Platform statistics (total communities, users, active streams)
- Featured communities
- Recent announcements
- Live stream carousel

### Communities Directory

**URL:** `/communities`

Features:
- Browse all public communities
- Search by name
- Filter by platform (Discord, Twitch, YouTube, etc.)
- Pagination (20 per page)
- Community cards show:
  - Logo
  - Name and description
  - Member count
  - Platform badge
  - "View" or "Join" button

**Actions:**
- Click community card to view public profile
- Click "Join" to join public community (requires login)
- Use search box to find specific communities

### Community Public Profile

**URL:** `/communities/:id`

Shows:
- Community banner
- Logo and name
- Description
- Member count
- Primary platform
- "Join Community" button (if public)
- Recent announcements (if public)
- Community stats

### Live Streams

**URL:** `/live`

Features:
- Grid of all live streams across all communities
- Stream cards show:
  - Thumbnail
  - Streamer name
  - Title
  - Viewer count
  - Platform badge
- Click card to watch embedded stream
- Auto-refresh every 30 seconds

---

## User Dashboard

### Dashboard Home

**URL:** `/dashboard`

**Sections:**

#### My Communities
- List of communities you're a member of
- Shows your role (Member, Moderator, Admin)
- Click to enter community dashboard

#### Recent Activity
- Your recent messages
- Reputation changes
- Community events

#### Quick Actions
- Join new community
- Edit profile
- View notifications

### Account Settings

**URL:** `/dashboard/settings`

**Tabs:**

#### Profile
- Display name
- Bio (1000 characters max)
- Location
- Website URL
- Avatar upload (max 5MB)

#### Linked Accounts
- View linked platforms (Discord, Twitch, etc.)
- Set primary identity
- Link additional accounts
- Unlink accounts

#### Security
- Change password
- Two-factor authentication (if enabled)
- Active sessions
- Security log

#### Notifications
- Email notifications
- Push notifications
- Notification preferences per community

### User Profile Edit

**URL:** `/dashboard/profile`

Features:
- Upload avatar (5MB max, PNG/JPG)
- Edit display name
- Write bio
- Add social links
- Set privacy preferences

---

## Community Management

### Community Dashboard

**URL:** `/dashboard/community/:id`

**Requires:** Community membership

**Widgets:**

#### Overview
- Community stats (members, messages, activity)
- Your role and reputation
- Recent announcements

#### Activity Feed
- Recent messages
- New members
- Events
- Achievements

#### Quick Links
- Chat rooms
- Leaderboard
- Members list
- Settings (if admin)

### Community Chat

**URL:** `/dashboard/community/:id/chat`

**Features:**

#### Channel List (Sidebar)
- List of chat channels
- Unread message indicators
- Click to switch channels

#### Chat Window
- Real-time message updates
- Infinite scroll (load older messages)
- User avatars
- Timestamps
- Emoji support

#### Message Input
- Type message
- Send with Enter or button
- File attachments (if enabled)
- @mentions

**Chat Commands:**
- `/help` - Show available commands
- `/me <action>` - Action message
- `/shrug` - Append ¯\_(ツ)_/¯

### Community Leaderboard

**URL:** `/dashboard/community/:id/leaderboard`

**Leaderboard Types:**

#### Reputation Leaderboard
| Rank | User | Score | Grade |
|------|------|-------|-------|
| 1 | TopUser | 850 | A+ |
| 2 | GreatMember | 780 | B+ |
| 3 | ActiveUser | 650 | C |

Scores range from 300-850 (FICO-style).

#### Watch Time Leaderboard
- Total hours watched
- Rank by watch time
- Monthly/All-time toggle

#### Message Leaderboard
- Total messages sent
- Rank by activity
- Weekly/Monthly/All-time

### Community Members

**URL:** `/dashboard/community/:id/members`

**Features:**
- Paginated member list (50 per page)
- Search by username
- Filter by role (All, Admin, Moderator, Member)
- Member cards show:
  - Avatar
  - Username
  - Role badge
  - Reputation score
  - Join date

**Actions (if moderator/admin):**
- View member profile
- Adjust reputation (admin only)
- Change role (admin only)
- Remove from community (admin only)

---

## Admin Dashboard

**URL:** `/admin/:communityId`

**Requires:** Community admin or moderator role

### Admin Home

**Overview Cards:**
- Total members
- Active members (7 days)
- Total messages (30 days)
- Bot health score (A-F grade)

**Quick Actions:**
- Manage members
- View join requests
- Create announcement
- Configure modules

### Member Management

**URL:** `/admin/:communityId/members`

**Features:**

#### Member List
- Search members
- Filter by role
- Sort by join date, reputation, activity
- Bulk actions

#### Member Actions
- **Change Role:** Promote/demote (member/moderator/admin)
- **Adjust Reputation:** Add or subtract points with reason
- **Remove Member:** Ban or kick with reason
- **View Profile:** See detailed member profile

**Reputation System (FICO-Style):**
- Score range: 300-850
- Starting score: 600
- Auto-ban threshold: 450 (configurable)
- Grade: A (850-800), B (799-700), C (699-600), D (599-500), F (<500)

### Join Requests

**URL:** `/admin/:communityId/join-requests` (for private communities)

**Features:**
- Pending requests list
- User info (username, request date, message)
- **Approve:** Add user as member
- **Reject:** Deny request with optional note

### Server Management

**URL:** `/admin/:communityId/servers`

**Purpose:** Link Discord servers, Slack workspaces, etc. to your community

**Features:**

#### Linked Servers
- List of connected platform servers
- Server name, platform, member count
- **Update:** Edit server settings
- **Remove:** Unlink server

#### Server Link Requests
- Pending requests to link servers
- Review and approve/reject

#### Mirror Groups
Create message mirroring across channels.

**URL:** `/admin/:communityId/mirror-groups`

**Steps:**
1. Click "Create Mirror Group"
2. Enter group name (e.g., "General Sync")
3. Add channels:
   - Select server
   - Enter channel ID
4. Enable/disable group
5. Messages in one channel appear in all mirrored channels

### Module Configuration

**URL:** `/admin/:communityId/modules`

**Installed Modules List:**

Each module card shows:
- Module name and description
- Version
- Enabled/disabled toggle
- Configure button

**Configure Module:**
1. Click "Configure" on module card
2. Edit JSON configuration
3. Save changes
4. Module reloads with new config

**Example: Loyalty Module Config**
```json
{
  "currencyName": "Waddle Coins",
  "earnRatePerMinute": 10,
  "bonusMultiplier": 2,
  "enabled": true
}
```

### Browser Source Overlays

**URL:** `/admin/:communityId/stream-overlays`

**Purpose:** Generate URLs for OBS Browser Source overlays

**Available Overlays:**
- **Alerts:** Sub alerts, donations, follows
- **Chat:** Live chat display
- **Goals:** Progress bars for goals
- **Ticker:** Scrolling news/announcements

**Steps:**
1. Select overlay type
2. Customize theme:
   - Background color
   - Text color
   - Font size
   - Position
3. Copy URL
4. Add to OBS as Browser Source
5. Paste URL

**Security:**
- Each overlay URL includes unique token
- Rotate token if compromised (invalidates old URLs)

### Custom Domains

**URL:** `/admin/:communityId/domains`

**Purpose:** Add custom domain for your community (e.g., `community.example.com`)

**Steps:**
1. Click "Add Domain"
2. Enter domain name
3. Add DNS records shown:
   - CNAME: `community.example.com` → `hub.waddlebot.io`
   - TXT: Verification token
4. Click "Verify Domain"
5. Domain is verified and active

**Blocked Subdomains:**
Reserved subdomains (www, api, admin, etc.) cannot be used.

### Announcements

**URL:** `/admin/:communityId/announcements`

**Features:**

#### Create Announcement
1. Click "Create Announcement"
2. Fill in:
   - Title (required, max 255 chars)
   - Content (Markdown supported)
   - Type (general, update, event, alert)
   - Priority (0-10)
3. Save as draft or publish immediately

#### Manage Announcements
- **Edit:** Modify title/content
- **Pin:** Pin to top of feed
- **Broadcast:** Send to connected platforms (Discord, Slack)
- **Archive:** Remove from active feed

#### Broadcast Status
- View which platforms announcement was sent to
- See message IDs
- Check for errors

### Reputation Settings

**URL:** `/admin/:communityId/reputation`

**FICO-Style Scoring System:**

#### Configuration
- **Starting Score:** Default score for new members (default: 600)
- **Min Score:** Minimum possible (300)
- **Max Score:** Maximum possible (850)
- **Auto-Ban Enabled:** Automatically ban users below threshold
- **Auto-Ban Threshold:** Score that triggers ban (default: 450)

#### Score Factors
Reputation changes based on:
- Message count (+)
- Watch time (+)
- Community participation (+)
- Warnings (-)
- Moderation actions (-)
- Manual adjustments (admin)

#### At-Risk Users
List of users near auto-ban threshold:
- Username
- Current score
- Days until potential ban
- Recent incidents

### Bot Detection

**URL:** `/admin/:communityId/bot-detection`

**Community Health Grade:**

The system analyzes your community and assigns a grade (A-F):
- **A (90-100):** Excellent health, minimal bots
- **B (80-89):** Good health, some suspicious accounts
- **C (70-79):** Average health, moderate bot activity
- **D (60-69):** Poor health, high bot activity
- **F (<60):** Critical health, severe bot problem

**Suspected Bots List:**

| User | Confidence | Flags | Action |
|------|-----------|-------|--------|
| bot_user_123 | 95% | [Generic name, No activity, Pattern] | Review |
| suspicious_456 | 78% | [High activity spike] | Review |

**Review Bot Detection:**
1. Click "Review" on suspected bot
2. View:
   - User profile
   - Activity patterns
   - AI analysis
   - Detection reasons
3. Mark as:
   - **Confirmed Bot:** Ban or remove
   - **False Positive:** Ignore and whitelist
   - **Needs Investigation:** Flag for later

### AI Insights

**URL:** `/admin/:communityId/ai-insights`

**Powered by AI Researcher module**

**Insights Categories:**

#### Community Health
- Member retention analysis
- Engagement trends
- Toxic behavior detection
- Growth opportunities

#### Content Analysis
- Popular topics
- Sentiment analysis
- Content recommendations

#### Member Insights
- Top contributors
- Influential members
- At-risk members

**Configure AI Researcher:**

**URL:** `/admin/:communityId/ai-config`

Settings:
- **Enabled:** Turn AI analysis on/off
- **Analysis Interval:** How often to run (1-168 hours)
- **Focus Areas:** Community health, content, engagement
- **AI Model:** Select model (GPT-4, Claude, etc.)

### Analytics

**URL:** `/admin/:communityId/analytics`

**Dashboards:**

#### Overview
- Member count (7/30/90 days)
- Message volume
- Active users
- Growth rate

#### Engagement
- Messages per day graph
- Peak activity hours
- Channel activity breakdown

#### Retention
- New vs. returning members
- Churn rate
- Member lifetime value

#### Bad Actors
- Warnings issued
- Bans/kicks
- Moderation actions log

**Export Options:**
- CSV export for all data
- Date range selection
- Custom reports

### Security

**URL:** `/admin/:communityId/security`

**Features:**

#### Content Filtering
- **Blocked Words:** Add words/phrases to auto-filter
- **Severity Levels:** Low (warn), Medium (timeout), High (ban)
- **Regex Support:** Advanced pattern matching

#### Moderation Log
- All moderation actions
- Who performed action
- Reason
- Timestamp
- Undo option (if available)

#### Security Warnings
- Suspicious login attempts
- Unusual activity patterns
- Potential security threats

### Shoutouts (Creator/Gaming Communities)

**URL:** `/admin/:communityId/shoutouts`

**Purpose:** Configure automatic shoutouts for streamers

**Features:**

#### Shoutout Config
- **Enabled:** Turn shoutouts on/off
- **Message Template:** Customize shoutout message
- **Cooldown:** Minutes between shoutouts (per streamer)
- **Auto-Shoutout:** Trigger when streamer joins chat

#### Creators List
- Add streamers to shoutout
- Platform (Twitch, YouTube, etc.)
- Channel name/URL
- Custom message override

#### Shoutout History
- Log of all shoutouts sent
- When, who, which channel
- Engagement stats (clicks, views)

### Translation Settings

**URL:** `/admin/:communityId/translation`

**Real-time message translation**

**Configuration:**
- **Enabled:** Turn translation on/off
- **Provider:** Google Translate, DeepL, etc.
- **Target Languages:** Select languages to translate to
- **Auto-Detect:** Automatically detect source language
- **Show Original:** Display original + translation

**Usage:**
Users can click "Translate" on any message to see translation.

### Music Module

**URL:** `/admin/:communityId/music`

**Dashboards and Settings for community music/radio**

#### Music Dashboard
**URL:** `/admin/:communityId/music`

Shows:
- Current playback status
- Active providers (Spotify, YouTube, etc.)
- Recent tracks
- Listener count

#### Music Settings
**URL:** `/admin/:communityId/music/settings`

Configuration:
- **Default Provider:** Spotify, YouTube Music, etc.
- **Autoplay:** Enable/disable
- **Volume Limit:** Max volume (0-100)
- **Allowed Genres:** Filter by genre
- **Blocked Artists:** Prevent specific artists
- **Require DJ Approval:** Songs need approval before playing

#### Music Providers
**URL:** `/admin/:communityId/music/providers`

**Connect Streaming Services:**

1. Click "Connect [Provider]"
2. Authorize OAuth (Spotify, YouTube, etc.)
3. Provider is now available
4. Configure provider settings

**Providers:**
- Spotify
- YouTube Music
- SoundCloud
- Apple Music (if configured)

#### Radio Stations
**URL:** `/admin/:communityId/music/radio`

**Internet Radio Integration:**

**Add Station:**
1. Click "Add Station"
2. Fill in:
   - Name (e.g., "Lofi Beats Radio")
   - Stream URL (e.g., `https://stream.example.com/lofi.mp3`)
   - Genre
   - Description
3. Click "Test Stream" to verify
4. Save station

**Manage Stations:**
- Edit station details
- Set as default station
- Disable/enable
- Delete station

### Loyalty System

**URL:** `/admin/:communityId/loyalty`

**Virtual currency and rewards system**

#### Loyalty Settings
**URL:** `/admin/:communityId/loyalty`

Configuration:
- **Currency Name:** "Waddle Coins", "Points", etc.
- **Currency Plural:** "Waddle Coins"
- **Earn Rate:** Points per minute watching/chatting (default: 10)
- **Bonus Multiplier:** Sub/VIP multiplier (default: 2x)
- **Enabled:** Turn system on/off

#### Leaderboard
**URL:** `/admin/:communityId/loyalty/leaderboard`

Features:
- Top users by currency balance
- Filter by all-time, monthly, weekly
- Adjust user balance (admin action)
- Wipe all currency (WARNING: irreversible)

#### Giveaways
**URL:** `/admin/:communityId/loyalty/giveaways`

**Create Giveaway:**
1. Click "Create Giveaway"
2. Fill in:
   - Title
   - Description
   - Entry cost (currency)
   - Max entries per user
   - Start/end date
3. Publish giveaway

**Manage Giveaway:**
- View entries
- Draw winner (random or weighted)
- End giveaway early
- Announce winner

#### Games
**URL:** `/admin/:communityId/loyalty/games`

**Configure mini-games users can play with currency:**

**Game Types:**
- **Slots:** Slot machine gambling
- **Roulette:** Bet on numbers/colors
- **Coinflip:** 50/50 gamble

**Settings:**
- Enable/disable each game
- Min bet amount
- Max bet amount
- House edge %

**Stats:**
- Total bets placed
- Total won/lost
- Most active players

#### Gear Shop
**URL:** `/admin/:communityId/loyalty/gear`

**Virtual items users can purchase with currency**

**Manage Items:**
1. Click "Add Item"
2. Fill in:
   - Name (e.g., "Custom Badge")
   - Description
   - Price (currency)
   - Stock (quantity available)
   - Category (cosmetic, privilege, physical)
   - Image URL
3. Save item

**Item Types:**
- **Cosmetic:** Badges, titles, profile decorations
- **Privilege:** Timeout immunity, custom emotes
- **Physical:** Real-world items (requires fulfillment)

**Orders:**
- View pending orders
- Mark as fulfilled
- Refund orders

### Workflows

**URL:** `/admin/:communityId/workflows`

**Visual workflow automation builder**

**Features:**

#### Workflow List
- List of created workflows
- Status (draft, published, active, paused)
- Last executed
- Success rate

#### Create Workflow
1. Click "Create Workflow"
2. Enter name and description
3. Drag nodes from palette:
   - **Trigger Nodes:** Event triggers (new member, message, etc.)
   - **Action Nodes:** Perform actions (send message, adjust reputation)
   - **Condition Nodes:** If/else branching
   - **Loop Nodes:** Iterate over data
   - **Data Nodes:** Transform data
4. Connect nodes with edges
5. Configure each node
6. Test workflow
7. Publish workflow

#### Workflow Nodes

**Trigger Nodes:**
- New member joined
- Message sent
- User gained role
- Scheduled (cron)
- Webhook received

**Action Nodes:**
- Send message
- Adjust reputation
- Add/remove role
- Create announcement
- Call external API

**Condition Nodes:**
- If reputation > X
- If role is Y
- If message contains Z

**Example Workflow:**
```
[New Member] → [Send Welcome Message]
                      ↓
              [Adjust Reputation +10]
                      ↓
              [If Watch Time > 60 min]
                      ↓
              [Add "Active" Role]
```

#### Test Workflow
- Provide test inputs
- Step through execution
- View logs and outputs
- Debug errors

#### Execution History
- List of workflow runs
- Success/failure status
- Execution time
- Error logs

---

## SuperAdmin Panel

**URL:** `/superadmin`

**Requires:** Super admin role

### SuperAdmin Dashboard

**Platform Overview:**
- Total communities
- Total users
- Platform revenue (if applicable)
- System health

**Recent Activity:**
- New communities created
- User registrations
- Support tickets
- System errors

### Community Management

**URL:** `/superadmin/communities`

**Features:**

#### List All Communities
- Search by name
- Filter by platform, visibility
- Sort by members, activity, created date

#### Create Community
**URL:** `/superadmin/communities/new`

**Steps:**
1. Click "Create Community"
2. Fill in:
   - Name (unique slug)
   - Display name
   - Platform (Discord, Twitch, YouTube, etc.)
   - Owner name (optional)
   - Description
   - Is public
3. Save community
4. Community is created and owner can log in

#### Manage Community
- **Edit:** Change settings
- **Reassign Owner:** Transfer ownership to another user
- **Deactivate:** Suspend community
- **Delete:** Permanently delete (WARNING: irreversible)

### Module Registry

**URL:** `/superadmin/modules`

**Purpose:** Manage global module marketplace

**Features:**

#### Module List
- All modules (published and unpublished)
- Official vs. community modules
- Version, author, downloads

#### Create Module
1. Click "Create Module"
2. Fill in:
   - Name (unique)
   - Display name
   - Description
   - Version (semver)
   - Author
   - Category (loyalty, analytics, etc.)
   - Repository URL
   - Is official (Waddles team)
   - Config schema (JSON schema)
3. Save module (as draft)

#### Publish Module
- Review module code/config
- Test installation
- Publish to marketplace (makes it available to all communities)

#### Update Module
- Edit details
- Bump version
- Update config schema

### Platform Configuration

**URL:** `/superadmin/platform-config`

**OAuth Provider Configuration:**

**Configure Platform:**
1. Select platform (Discord, Twitch, etc.)
2. Fill in OAuth credentials:
   - Client ID
   - Client Secret
   - Redirect URI (auto-filled)
3. Enable/disable platform
4. Test connection
5. Save

**Platforms:**
- Discord
- Twitch
- YouTube
- KICK
- Slack

**Hub Settings:**
- **Allow Public Signup:** Enable/disable user registration
- **Require Email Verification:** Force email verification
- **SMTP Configuration:** Email server settings

---

## Module Marketplace

**URL:** `/admin/:communityId/marketplace`

**Purpose:** Browse and install modules for your community

### Browse Modules

**Categories:**
- Loyalty & Rewards
- Analytics
- Moderation
- Entertainment
- Music & Media
- Utilities

**Module Card:**
- Name and icon
- Description
- Version
- Author
- Rating (stars)
- Download count
- Install button

**Filters:**
- Category
- Official/Community
- Free/Paid
- Rating

**Search:**
- Search by name or description

### Install Module

1. Click module card
2. View details:
   - Full description
   - Screenshots
   - Reviews
   - Configuration options
   - Requirements
3. Click "Install"
4. Module is installed and appears in `/admin/:communityId/modules`
5. Configure module

### Uninstall Module

1. Go to `/admin/:communityId/modules`
2. Click module card
3. Click "Uninstall"
4. Confirm (WARNING: all module data will be deleted)

### Rate Module

1. Go to `/admin/:communityId/marketplace`
2. Click installed module
3. Leave rating (1-5 stars)
4. Write review (optional)
5. Submit

---

## Chat System

### Real-time Chat

The hub includes a cross-platform chat system that aggregates messages from Discord, Slack, etc.

**Access:** `/dashboard/community/:id/chat`

**Features:**

#### Channel Sidebar
- List of channels
- Unread indicators
- Click to switch

#### Message Display
- User avatar
- Username
- Message content
- Timestamp
- Reactions (if supported)

#### Send Message
- Type in input box
- Press Enter or click Send
- Message appears instantly (WebSocket)
- Message saved to database

#### Message Actions
- Reply to message (threaded)
- React with emoji
- Edit (own messages, 5 min window)
- Delete (own messages or moderator)
- Report (spam, abuse)

### Chat Commands

- `/help` - Show commands
- `/me <text>` - Action message (e.g., "/me waves")
- `/clear` - Clear local chat history
- `/shrug` - Append ¯\_(ツ)_/¯ to message

### Moderation

**Moderator Actions:**
- Delete any message
- Timeout user (mute for X minutes)
- Ban user from chat
- Clear user's messages

---

## Workflows

### Visual Workflow Builder

**Access:** `/admin/:communityId/workflows`

**Purpose:** Automate community tasks with visual drag-and-drop workflows

### Workflow Editor

**Components:**

#### Node Palette (Left Sidebar)
Drag nodes onto canvas:
- Triggers
- Actions
- Conditions
- Loops
- Data transformations

#### Canvas (Center)
- Drag nodes from palette
- Connect nodes with edges
- Move nodes around
- Delete nodes/edges

#### Properties Panel (Right Sidebar)
Configure selected node:
- Node name
- Input parameters
- Conditions
- Error handling

#### Toolbar (Top)
- Save workflow
- Test workflow
- Publish workflow
- View execution history

### Example Workflows

#### 1. Welcome New Members
```
[New Member Joined]
    ↓
[Send Welcome Message]
    ↓
[Add "Newbie" Role]
    ↓
[Adjust Reputation +10]
```

#### 2. Auto-Moderate Spam
```
[Message Sent]
    ↓
[If Message Contains Spam]
    ↓ (yes)
[Delete Message]
    ↓
[Timeout User 5 min]
    ↓
[Send Warning DM]
```

#### 3. Reward Active Users
```
[Scheduled Daily]
    ↓
[Get Users with Watch Time > 60 min]
    ↓
[For Each User]
    ↓
[Add Currency +100]
    ↓
[Send Notification]
```

### Testing Workflows

1. Click "Test" button
2. Provide test inputs (e.g., mock member object)
3. Step through execution
4. View node outputs at each step
5. Fix errors
6. Re-test

### Publishing Workflows

1. Test workflow
2. Click "Publish"
3. Workflow becomes active
4. Workflow triggers on events

### Monitoring Workflows

**Execution History:**
- Date/time
- Trigger event
- Success/failure
- Execution time
- Error logs (if failed)

---

## Analytics

### Community Analytics

**Access:** `/admin/:communityId/analytics`

### Overview Dashboard

**Metrics:**

#### Member Growth
- Line chart (7/30/90 days)
- New members per day
- Total members
- Growth rate %

#### Activity Metrics
- Messages per day
- Active users (DAU/MAU)
- Avg. messages per user
- Peak activity hours

#### Engagement Score
- Calculated score (0-100)
- Based on:
  - Message frequency
  - Member retention
  - Chat participation

### Advanced Analytics

#### Retention Analysis
- Cohort retention table
- Churn rate
- Member lifetime value

#### Content Analysis
- Top channels by activity
- Popular topics (word cloud)
- Sentiment analysis

#### Bad Actor Detection
- List of flagged users
- Warning count
- Ban/kick history
- Suspicious activity patterns

### Export Data

1. Select date range
2. Select metrics to export
3. Choose format (CSV, JSON)
4. Click "Export"
5. Download file

---

## Common Tasks

### Task: Create a Community (SuperAdmin)

1. Log in as super admin
2. Go to `/superadmin/communities`
3. Click "Create Community"
4. Fill in details (name, platform, owner)
5. Click "Save"
6. Community is created

### Task: Install a Module

1. Go to `/admin/:communityId/marketplace`
2. Browse or search for module
3. Click module card
4. Click "Install"
5. Wait for installation
6. Go to `/admin/:communityId/modules`
7. Click "Configure" on new module
8. Edit config as needed
9. Save

### Task: Adjust Member Reputation

1. Go to `/admin/:communityId/members`
2. Search for member
3. Click member card
4. Click "Adjust Reputation"
5. Enter amount (+/-) and reason
6. Save
7. Member's reputation is updated

### Task: Create an Announcement

1. Go to `/admin/:communityId/announcements`
2. Click "Create Announcement"
3. Fill in title and content
4. Select type (general, update, event)
5. Save as draft or publish
6. (Optional) Click "Broadcast" to send to Discord/Slack

### Task: Set Up Browser Source Overlay

1. Go to `/admin/:communityId/stream-overlays`
2. Select overlay type (alerts, chat, etc.)
3. Customize theme (colors, fonts)
4. Copy overlay URL
5. Open OBS
6. Add Browser Source
7. Paste URL
8. Adjust width/height (1920x1080 recommended)
9. Overlay appears in OBS

### Task: Configure Music Module

1. Go to `/admin/:communityId/music/settings`
2. Set default provider (Spotify)
3. Enable autoplay
4. Set volume limit (80)
5. Add allowed genres
6. Save settings
7. Go to `/admin/:communityId/music/providers`
8. Click "Connect Spotify"
9. Authorize OAuth
10. Spotify is connected
11. Go to `/admin/:communityId/music/radio`
12. Add internet radio stations
13. Set default station

### Task: Create a Workflow

1. Go to `/admin/:communityId/workflows`
2. Click "Create Workflow"
3. Name it "Welcome New Members"
4. Drag "New Member Joined" trigger to canvas
5. Drag "Send Message" action
6. Connect trigger to action
7. Configure message: "Welcome {{username}}!"
8. Drag "Adjust Reputation" action
9. Connect to previous action
10. Set amount: +10
11. Click "Test" with mock data
12. Fix any errors
13. Click "Publish"
14. Workflow is active

---

## Tips & Best Practices

1. **Use roles effectively:** Assign moderators to help manage large communities
2. **Monitor bot score regularly:** Keep your community health at grade A or B
3. **Leverage workflows:** Automate repetitive tasks
4. **Rotate overlay tokens:** If tokens are compromised
5. **Review reputation leaderboard:** Recognize top contributors
6. **Back up announcements:** Export important announcements
7. **Test modules before production:** Use test community for module testing
8. **Set up alerts:** Configure notifications for critical events
9. **Review analytics weekly:** Spot trends and issues early
10. **Engage with members:** Use announcements and broadcasts effectively

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `/` | Focus search |
| `Ctrl+K` | Command palette |
| `Esc` | Close modal |
| `Enter` | Submit form |
| `Ctrl+Enter` | Send message (chat) |

---

## Mobile Support

The hub is responsive and works on mobile devices:
- Touch-friendly navigation
- Swipe gestures for chat
- Mobile-optimized layouts
- Works on iOS and Android browsers

---

## Accessibility

The hub follows WCAG 2.1 AA standards:
- Keyboard navigation
- Screen reader support
- High contrast mode
- Focus indicators
- Alt text for images

---

## Need Help?

- **Documentation:** See other docs in `/docs/hub_module/`
- **Support:** Contact your platform administrator
- **Issues:** Report bugs via GitHub issues
- **Community:** Join the Waddles Discord for help

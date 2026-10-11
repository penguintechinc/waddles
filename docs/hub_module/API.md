# Hub Module API Documentation

## Overview

The Waddles Hub Module provides a comprehensive RESTful API for managing communities, users, authentication, and platform integrations. The API follows REST conventions and returns JSON responses.

**Base URL:** `http://localhost:8060/api/v1`
**Version:** 1.0.1
**Authentication:** JWT Bearer tokens

---

## Table of Contents

- [Authentication](#authentication)
- [Public Endpoints](#public-endpoints)
- [User Endpoints](#user-endpoints)
- [Community Endpoints](#community-endpoints)
- [Admin Endpoints](#admin-endpoints)
- [SuperAdmin Endpoints](#superadmin-endpoints)
- [Marketplace Endpoints](#marketplace-endpoints)
- [Music Endpoints](#music-endpoints)
- [WebSocket API](#websocket-api)
- [Response Formats](#response-formats)
- [Error Codes](#error-codes)

---

## Authentication

### POST /auth/register
Register a new user account (if public signup is enabled).

**Request Body:**
```json
{
  "email": "user@example.com",
  "password": "SecurePassword123!",
  "username": "myusername"
}
```

**Response:** `201 Created`
```json
{
  "success": true,
  "data": {
    "id": 123,
    "email": "user@example.com",
    "username": "myusername",
    "emailVerified": false
  }
}
```

---

### POST /auth/login
Login with email and password credentials.

**Request Body:**
```json
{
  "email": "admin@localhost",
  "password": "admin123"
}
```

**Response:** `200 OK`
```json
{
  "success": true,
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "user": {
    "id": 1,
    "email": "admin@localhost",
    "username": "admin",
    "isSuperAdmin": true
  }
}
```

---

### GET /auth/me
Get current authenticated user information.

**Headers:** `Authorization: Bearer <token>`

**Response:** `200 OK`
```json
{
  "success": true,
  "user": {
    "id": 1,
    "email": "admin@localhost",
    "username": "admin",
    "displayName": "Admin User",
    "avatarUrl": null,
    "isSuperAdmin": true,
    "isActive": true
  }
}
```

---

### GET /auth/oauth/:platform
Start OAuth flow for platform authentication (Discord, Twitch, YouTube, KICK, Slack).

**Parameters:**
- `platform` (path): Platform name (discord|twitch|youtube|kick|slack)
- `redirectUrl` (query): URL to redirect after OAuth

**Response:** `200 OK`
```json
{
  "success": true,
  "url": "https://discord.com/api/oauth2/authorize?client_id=..."
}
```

---

### GET /auth/oauth/:platform/callback
OAuth callback handler (handled automatically by OAuth providers).

---

### POST /auth/refresh
Renew the session. Access tokens live 15 minutes; the **rotating refresh token** (HttpOnly cookie
`wb_refresh`, path `/api/v1/auth`) renews them with no forced logout. The refresh token is rotated
on every call (the browser takes the new `Set-Cookie`); presenting one that was already rotated
away revokes the whole session. `400` when there is no refresh cookie, `401` when it is invalid,
expired or revoked. See [Session Revocation](../TOKEN_REVOCATION.md).

**Response:** `200 OK`
```json
{
  "success": true,
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

---

### POST /auth/logout
Logout: ends the login session (refresh token, every access token of the session, and the access
token presented) and clears both cookies. Works from the refresh cookie alone when the access cookie
has already expired.

**Headers:** `Authorization: Bearer <token>` (or the `wb_session` cookie)

### POST /auth/logout-all
Sign out everywhere: every session of the caller ends and every access token issued so far is
revoked. Requires authentication.

**Response:** `200 OK`
```json
{
  "success": true,
  "message": "Logged out successfully"
}
```

---

## Public Endpoints

### GET /public/stats
Get platform-wide statistics (no authentication required).

**Response:** `200 OK`
```json
{
  "success": true,
  "stats": {
    "communities": 42,
    "users": 1523,
    "activeStreams": 8,
    "totalMessages": 125634
  }
}
```

---

### GET /public/communities
List all public communities with pagination.

**Query Parameters:**
- `page` (number, default: 1): Page number
- `limit` (number, default: 20): Items per page
- `search` (string): Search by name

**Response:** `200 OK`
```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "name": "waddle-community",
      "displayName": "Waddle Community",
      "description": "The official Waddles community",
      "logoUrl": "https://example.com/logo.png",
      "memberCount": 523,
      "isPublic": true,
      "platform": "discord"
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total": 42,
    "pages": 3
  }
}
```

---

### GET /public/communities/:id
Get detailed information about a specific public community.

**Response:** `200 OK`
```json
{
  "success": true,
  "community": {
    "id": 1,
    "name": "waddle-community",
    "displayName": "Waddle Community",
    "description": "The official Waddles community",
    "logoUrl": "https://example.com/logo.png",
    "bannerUrl": "https://example.com/banner.png",
    "memberCount": 523,
    "isPublic": true,
    "platform": "discord",
    "createdAt": "2024-01-15T10:30:00Z"
  }
}
```

---

### GET /public/live
Get currently live streams across all communities.

**Query Parameters:**
- `page` (number, default: 1)
- `limit` (number, default: 20)

**Response:** `200 OK`
```json
{
  "success": true,
  "streams": [
    {
      "entityId": "twitch:12345",
      "platform": "twitch",
      "username": "streamer123",
      "title": "Playing games!",
      "viewerCount": 234,
      "thumbnailUrl": "https://twitch.tv/thumb.jpg",
      "isLive": true
    }
  ]
}
```

---

### GET /public/signup-settings
Get signup availability settings for the login page.

**Response:** `200 OK`
```json
{
  "success": true,
  "signupEnabled": true,
  "emailVerificationRequired": false
}
```

---

## User Endpoints

All user endpoints require authentication (`Authorization: Bearer <token>`).

### GET /user/profile
Get current user's profile.

**Response:** `200 OK`
```json
{
  "success": true,
  "profile": {
    "id": 123,
    "displayName": "John Doe",
    "email": "john@example.com",
    "bio": "Community enthusiast",
    "avatarUrl": "https://cdn.example.com/avatar.jpg",
    "location": "New York, USA",
    "website": "https://johndoe.com",
    "createdAt": "2024-01-15T10:30:00Z"
  }
}
```

---

### PUT /user/profile
Update current user's profile.

**Request Body:**
```json
{
  "displayName": "Jane Doe",
  "bio": "Updated bio",
  "location": "San Francisco",
  "website": "https://janedoe.com"
}
```

**Response:** `200 OK`

---

### POST /user/profile/avatar
Upload avatar image (multipart/form-data).

**Form Data:**
- `avatar` (file): Image file (max 5MB, PNG/JPG)

**Response:** `200 OK`
```json
{
  "success": true,
  "avatarUrl": "https://cdn.example.com/avatars/user123.jpg"
}
```

---

### GET /user/identities
Get linked platform identities.

**Response:** `200 OK`
```json
{
  "success": true,
  "identities": [
    {
      "platform": "discord",
      "platformUserId": "123456789",
      "platformUsername": "johndoe#1234",
      "isPrimary": true,
      "linkedAt": "2024-01-15T10:30:00Z"
    },
    {
      "platform": "twitch",
      "platformUserId": "987654321",
      "platformUsername": "johndoe_ttv",
      "isPrimary": false,
      "linkedAt": "2024-02-01T14:20:00Z"
    }
  ]
}
```

---

## Community Endpoints

### GET /communities/my
Get communities the authenticated user is a member of.

**Response:** `200 OK`
```json
{
  "success": true,
  "communities": [
    {
      "id": 1,
      "name": "waddle-community",
      "displayName": "Waddle Community",
      "role": "admin",
      "reputation": 750,
      "joinedAt": "2024-01-15T10:30:00Z"
    }
  ]
}
```

---

### POST /communities/:id/join
Join a public community or request to join a private one.

**Request Body:**
```json
{
  "message": "I'd love to join your community!"
}
```

**Response:** `200 OK` (joined) or `201 Created` (request created)

---

### GET /communities/:id/dashboard
Get community dashboard data (requires membership).

**Response:** `200 OK`
```json
{
  "success": true,
  "dashboard": {
    "community": {
      "id": 1,
      "displayName": "Waddle Community",
      "memberCount": 523
    },
    "recentActivity": [...],
    "upcomingEvents": [...],
    "announcements": [...]
  }
}
```

---

### GET /communities/:id/leaderboard
Get community leaderboard (requires membership).

**Query Parameters:**
- `type` (string): Leaderboard type (reputation|watchtime|messages)
- `limit` (number, default: 10): Number of entries

**Response:** `200 OK`
```json
{
  "success": true,
  "leaderboard": [
    {
      "rank": 1,
      "userId": 123,
      "username": "TopUser",
      "score": 850,
      "avatarUrl": "https://cdn.example.com/avatar.jpg"
    }
  ]
}
```

---

### GET /communities/:id/chat/history
Get chat message history (requires membership).

**Query Parameters:**
- `limit` (number, default: 50)
- `before` (timestamp): Get messages before this timestamp

**Response:** `200 OK`
```json
{
  "success": true,
  "messages": [
    {
      "id": 1234,
      "senderUsername": "user123",
      "senderAvatar": "https://cdn.example.com/avatar.jpg",
      "content": "Hello everyone!",
      "createdAt": "2024-03-15T14:30:00Z"
    }
  ]
}
```

---

## Admin Endpoints

All admin endpoints require community admin role (`/api/v1/admin/:communityId/...`).

### GET /admin/:communityId/settings
Get community settings.

**Response:** `200 OK`
```json
{
  "success": true,
  "settings": {
    "name": "waddle-community",
    "displayName": "Waddle Community",
    "description": "Official community",
    "isPublic": true,
    "allowJoinRequests": true,
    "logoUrl": "https://example.com/logo.png",
    "bannerUrl": "https://example.com/banner.png"
  }
}
```

---

### PUT /admin/:communityId/settings
Update community settings.

**Request Body:**
```json
{
  "displayName": "Updated Community Name",
  "description": "Updated description",
  "isPublic": false,
  "allowJoinRequests": true
}
```

**Response:** `200 OK`

---

### GET /admin/:communityId/members
Get community members with filtering.

**Query Parameters:**
- `page` (number, default: 1)
- `limit` (number, default: 50)
- `role` (string): Filter by role (admin|moderator|member)
- `search` (string): Search by username

**Response:** `200 OK`
```json
{
  "success": true,
  "members": [
    {
      "id": 123,
      "username": "user123",
      "displayName": "John Doe",
      "role": "member",
      "reputation": 650,
      "joinedAt": "2024-01-15T10:30:00Z",
      "isActive": true
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 50,
    "total": 523
  }
}
```

---

### PUT /admin/:communityId/members/:userId/role
Update member's role.

**Request Body:**
```json
{
  "role": "moderator"
}
```

**Response:** `200 OK`

---

### PUT /admin/:communityId/members/:userId/reputation
Adjust member's reputation score.

**Request Body:**
```json
{
  "amount": 50,
  "reason": "Helpful community contribution"
}
```

**Response:** `200 OK`

---

### DELETE /admin/:communityId/members/:userId
Remove member from community.

**Response:** `200 OK`

---

### GET /admin/:communityId/join-requests
Get pending join requests.

**Response:** `200 OK`
```json
{
  "success": true,
  "requests": [
    {
      "id": 456,
      "userId": 789,
      "username": "newuser",
      "message": "I'd love to join!",
      "requestedAt": "2024-03-15T10:00:00Z"
    }
  ]
}
```

---

### POST /admin/:communityId/join-requests/:requestId/approve
Approve a join request.

**Response:** `200 OK`

---

### POST /admin/:communityId/join-requests/:requestId/reject
Reject a join request.

**Response:** `200 OK`

---

### GET /admin/:communityId/modules
Get installed modules.

**Response:** `200 OK`
```json
{
  "success": true,
  "modules": [
    {
      "id": 1,
      "name": "loyalty",
      "displayName": "Loyalty System",
      "version": "1.0.0",
      "isEnabled": true,
      "config": {...}
    }
  ]
}
```

---

### PUT /admin/:communityId/modules/:moduleId/config
Update module configuration.

**Request Body:**
```json
{
  "config": {
    "enabled": true,
    "setting1": "value1"
  }
}
```

**Response:** `200 OK`

---

### GET /admin/:communityId/browser-sources
Get browser source URLs for OBS.

**Response:** `200 OK`
```json
{
  "success": true,
  "sources": {
    "alerts": "https://hub.example.com/overlay/abc123?source=alerts",
    "chat": "https://hub.example.com/overlay/abc123?source=chat",
    "goals": "https://hub.example.com/overlay/abc123?source=goals"
  }
}
```

---

### POST /admin/:communityId/browser-sources/regenerate
Regenerate browser source token for security.

**Response:** `200 OK`

---

### GET /admin/:communityId/domains
Get custom domains.

**Response:** `200 OK`
```json
{
  "success": true,
  "domains": [
    {
      "id": 1,
      "domain": "community.example.com",
      "isVerified": true,
      "verifiedAt": "2024-01-20T10:00:00Z"
    }
  ]
}
```

---

### POST /admin/:communityId/domains
Add a custom domain.

**Request Body:**
```json
{
  "domain": "mycommunity.example.com"
}
```

**Response:** `201 Created`

---

### GET /admin/:communityId/reputation/config
Get FICO-style reputation system configuration.

**Response:** `200 OK`
```json
{
  "success": true,
  "config": {
    "enabled": true,
    "baseScore": 600,
    "minScore": 300,
    "maxScore": 850,
    "autoBanEnabled": false,
    "autoBanThreshold": 450
  }
}
```

---

### GET /admin/:communityId/bot-score
Get community health grade (A-F based on bot detection).

**Response:** `200 OK`
```json
{
  "success": true,
  "botScore": {
    "score": 85,
    "grade": "A",
    "suspectedBotCount": 3,
    "totalUsersAnalyzed": 523,
    "calculatedAt": "2024-03-15T10:00:00Z"
  }
}
```

---

### GET /admin/:communityId/announcements
Get community announcements.

**Query Parameters:**
- `status` (string): Filter by status (draft|published|archived)

**Response:** `200 OK`
```json
{
  "success": true,
  "data": [
    {
      "id": 1,
      "title": "Welcome to the community!",
      "content": "We're excited to have you here...",
      "status": "published",
      "isPinned": true,
      "createdAt": "2024-03-01T10:00:00Z"
    }
  ]
}
```

---

### POST /admin/:communityId/announcements
Create a new announcement.

**Request Body:**
```json
{
  "title": "New Feature Announcement",
  "content": "We just launched...",
  "announcementType": "general",
  "status": "draft"
}
```

**Response:** `201 Created`

---

### POST /admin/:communityId/announcements/:announcementId/broadcast
Broadcast announcement to connected platforms.

**Request Body:**
```json
{
  "platforms": ["discord", "twitch"]
}
```

**Response:** `200 OK`

---

### GET /admin/:communityId/loyalty/config
Get loyalty system configuration.

**Response:** `200 OK`
```json
{
  "success": true,
  "config": {
    "currencyName": "Waddle Coins",
    "currencyPlural": "Waddle Coins",
    "earnRatePerMinute": 10,
    "bonusMultiplier": 2,
    "enabled": true
  }
}
```

---

### GET /admin/:communityId/loyalty/leaderboard
Get loyalty currency leaderboard.

**Response:** `200 OK`
```json
{
  "success": true,
  "leaderboard": [
    {
      "rank": 1,
      "userId": 123,
      "username": "richuser",
      "balance": 50000
    }
  ]
}
```

---

## SuperAdmin Endpoints

All superadmin endpoints require super_admin role (`/api/v1/superadmin/...`).

### GET /superadmin/dashboard
Get platform dashboard statistics.

**Response:** `200 OK`
```json
{
  "success": true,
  "stats": {
    "totalCommunities": 42,
    "totalUsers": 1523,
    "activeCommunities": 38,
    "totalMessages": 125634
  }
}
```

---

### GET /superadmin/communities
List all communities (including private).

**Query Parameters:**
- `page` (number, default: 1)
- `limit` (number, default: 20)

**Response:** `200 OK`

---

### POST /superadmin/communities
Create a new community.

**Request Body:**
```json
{
  "name": "new-community",
  "displayName": "New Community",
  "platform": "discord",
  "isPublic": true,
  "ownerName": "OwnerUsername"
}
```

**Response:** `201 Created`

---

### DELETE /superadmin/communities/:id
Delete a community (non-reversible).

**Response:** `200 OK`

---

### GET /superadmin/marketplace/modules
Get all modules in registry.

**Response:** `200 OK`
```json
{
  "success": true,
  "modules": [
    {
      "id": 1,
      "name": "loyalty",
      "displayName": "Loyalty System",
      "version": "1.0.0",
      "isPublished": true,
      "isOfficial": true
    }
  ]
}
```

---

### POST /superadmin/marketplace/modules
Create a new module.

**Request Body:**
```json
{
  "name": "custom-module",
  "displayName": "Custom Module",
  "description": "Module description",
  "version": "1.0.0",
  "author": "Developer Name",
  "repositoryUrl": "https://github.com/user/module",
  "isOfficial": false
}
```

**Response:** `201 Created`

---

### Community OAuth Credentials (Community Admin)

Community admins manage their own OAuth credentials under the admin namespace.
These are scoped to the community and cannot be accessed by other communities.

#### GET /admin/:communityId/oauth/credentials
List all active `community_oauth` credentials for a community.

**Auth:** Community admin role required.

**Response:** `200 OK`
```json
{ "success": true, "data": [...], "count": 2 }
```

#### POST /admin/:communityId/oauth/credentials
Create a new community OAuth credential. `communityId` and `integrationType` are
enforced server-side — request body values are ignored/overridden.

**Auth:** Community admin role required.

**Request Body:**
```json
{ "platform": "twitch", "clientId": "...", "clientSecret": "...", "scopes": ["channel:read"] }
```

#### PUT /admin/:communityId/oauth/credentials/:id
Update a credential. Verifies the credential belongs to this community before updating.

#### DELETE /admin/:communityId/oauth/credentials/:id
Deactivate a credential. Verifies community ownership.

#### POST /admin/:communityId/oauth/credentials/:id/test
Test a credential against the platform API.

---

### User OAuth Credentials (Authenticated User)

Users manage their own personal OAuth credentials. The authenticated user's ID
is always derived from the JWT — no user ID can be passed in the request body.

#### GET /user/oauth/credentials
List the authenticated user's own `user_oauth` credentials.

**Auth:** Any authenticated user.

#### POST /user/oauth/credentials
Create a credential. `userId` and `integrationType` are enforced from session.

#### PUT /user/oauth/credentials/:id
Update a credential. Verifies ownership (user_id = authenticated user).

#### DELETE /user/oauth/credentials/:id
Deactivate a credential. Verifies ownership.

#### POST /user/oauth/credentials/:id/test
Test credential against platform API.

---

### GET /superadmin/platform-config
Get bot and auth platform configurations (bot credentials only — community OAuth
and user OAuth have been moved to their respective ownership layers above).

**Response:** `200 OK`
```json
{
  "success": true,
  "platforms": {
    "discord": { "enabled": true, "clientId": "123456789" },
    "twitch": { "enabled": true, "clientId": "987654321" }
  }
}
```

---

### GET /superadmin/settings
Get hub settings.

**Response:** `200 OK`
```json
{
  "success": true,
  "settings": {
    "allowPublicSignup": true,
    "requireEmailVerification": false,
    "smtpHost": "smtp.example.com",
    "smtpPort": 587,
    "smtpSecure": true
  }
}
```

---

## Marketplace Endpoints

### GET /admin/:communityId/marketplace/modules
Browse available modules.

**Response:** `200 OK`
```json
{
  "success": true,
  "modules": [
    {
      "id": 1,
      "name": "loyalty",
      "displayName": "Loyalty System",
      "description": "Points and rewards system",
      "version": "1.0.0",
      "author": "Waddles",
      "isInstalled": false,
      "rating": 4.8,
      "downloads": 523
    }
  ]
}
```

---

### POST /admin/:communityId/marketplace/modules/:id/install
Install a module.

**Response:** `201 Created`

---

### DELETE /admin/:communityId/marketplace/modules/:id
Uninstall a module.

**Response:** `200 OK`

---

## Music Endpoints

### GET /admin/:communityId/music/settings
Get music module settings.

**Response:** `200 OK`
```json
{
  "success": true,
  "settings": {
    "defaultProvider": "spotify",
    "autoplayEnabled": true,
    "volumeLimit": 80,
    "allowedGenres": ["rock", "pop", "electronic"],
    "requireDjApproval": false,
    "isActive": true
  }
}
```

---

### GET /admin/:communityId/music/providers
Get configured music providers.

**Response:** `200 OK`
```json
{
  "success": true,
  "providers": [
    {
      "provider": "spotify",
      "isConnected": true,
      "config": {...}
    }
  ]
}
```

---

### GET /admin/:communityId/music/radio-stations
Get radio stations.

**Response:** `200 OK`
```json
{
  "success": true,
  "stations": [
    {
      "id": 1,
      "name": "Lofi Beats",
      "url": "https://stream.example.com/lofi",
      "genre": "lofi",
      "isActive": true,
      "isDefault": false
    }
  ]
}
```

---

### POST /admin/:communityId/music/radio-stations
Add a radio station.

**Request Body:**
```json
{
  "name": "Jazz Station",
  "url": "https://stream.example.com/jazz",
  "description": "Smooth jazz 24/7",
  "genre": "jazz",
  "isActive": true
}
```

**Response:** `201 Created`

---

## WebSocket API

### Connection
```javascript
import io from 'socket.io-client';

const socket = io('http://localhost:8060', {
  auth: {
    token: 'your-jwt-token'
  }
});
```

### Events

#### join-channel
Join a community chat channel.

```javascript
socket.emit('join-channel', {
  communityId: 1,
  channelName: 'general'
});
```

#### send-message
Send a chat message.

```javascript
socket.emit('send-message', {
  communityId: 1,
  channelName: 'general',
  content: 'Hello everyone!'
});
```

#### new-message (receive)
Receive new chat messages.

```javascript
socket.on('new-message', (message) => {
  console.log(message);
  // { username: 'user123', content: 'Hello!', timestamp: '...' }
});
```

---

## Response Formats

### Success Response
```json
{
  "success": true,
  "data": { ... }
}
```

### Error Response
```json
{
  "success": false,
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid email format",
    "field": "email"
  }
}
```

---

## Error Codes

| Code | HTTP Status | Description |
|------|-------------|-------------|
| `VALIDATION_ERROR` | 400 | Request validation failed |
| `UNAUTHORIZED` | 401 | Authentication required |
| `FORBIDDEN` | 403 | Insufficient permissions |
| `NOT_FOUND` | 404 | Resource not found |
| `CONFLICT` | 409 | Resource already exists |
| `RATE_LIMITED` | 429 | Too many requests |
| `INTERNAL_ERROR` | 500 | Server error |

---

## Rate Limiting

- **Window:** 60 seconds
- **Max Requests:** 100 per window per IP
- **Headers:**
  - `X-RateLimit-Limit`: Maximum requests
  - `X-RateLimit-Remaining`: Remaining requests
  - `X-RateLimit-Reset`: Reset timestamp

---

## Pagination

All paginated endpoints support:

**Query Parameters:**
- `page` (number, default: 1): Page number
- `limit` (number, default: 20): Items per page

**Response:**
```json
{
  "data": [...],
  "pagination": {
    "page": 1,
    "limit": 20,
    "total": 100,
    "pages": 5
  }
}
```

---

## CSRF Protection

State-changing requests (POST/PUT/PATCH/DELETE) require CSRF token in cookies. The token is automatically set by the backend on GET requests and verified on mutations.

---

## API Versioning

Current version: **v1**

All endpoints are prefixed with `/api/v1`. Future versions will use `/api/v2`, etc.

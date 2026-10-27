# Module RTC — Architecture & Design

Comprehensive documentation of Module RTC's system architecture, including signaling flow, peer connection lifecycle, and component interactions.

## System Overview

Module RTC is a stateless, horizontally scalable WebRTC signaling and room management service. It coordinates with LiveKit's Selective Forwarding Unit (SFU) to enable large-scale video conferencing for Waddlebot communities.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Waddlebot Community                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┼─────────────┐
                │             │             │
        ┌───────▼─────┐ ┌────▼────┐ ┌────▼─────┐
        │   Browser   │ │ Browser  │ │ Browser  │
        │  (WebRTC    │ │(WebRTC   │ │(WebRTC   │
        │   Client)   │ │ Client)  │ │ Client)  │
        └───────┬─────┘ └────┬────┘ └────┬─────┘
                │             │           │
                └─────────────┼───────────┘
                              │
                    (REST API + WS)
                              │
        ┌─────────────────────▼──────────────────────┐
        │      Module RTC (Stateless Servers)        │
        │  ┌────────────────────────────────────┐    │
        │  │  HTTP Router (Gorilla Mux)         │    │
        │  │  ├─ Room Management Handlers      │    │
        │  │  ├─ Participant Control Handlers  │    │
        │  │  ├─ Raised Hand Queue Handlers    │    │
        │  │  └─ Health Check Endpoint         │    │
        │  └────────────────────────────────────┘    │
        │  ┌────────────────────────────────────┐    │
        │  │  Business Logic Services            │    │
        │  │  ├─ RoomService                    │    │
        │  │  │  ├─ CreateRoom (LiveKit API)   │    │
        │  │  │  ├─ JoinRoom (Token Gen)       │    │
        │  │  │  ├─ DeleteRoom                 │    │
        │  │  │  └─ MuteParticipant            │    │
        │  │  └─ CallFeaturesService            │    │
        │  │     ├─ RaiseHand (Queue Mgmt)    │    │
        │  │     ├─ MuteAll                    │    │
        │  │     ├─ LockRoom                   │    │
        │  │     └─ KickParticipant            │    │
        │  └────────────────────────────────────┘    │
        └─────────────────────┬──────────────────────┘
                              │
                ┌─────────────┼─────────────┐
                │             │             │
        ┌───────▼────────┐   │      ┌──────▼──────┐
        │    LiveKit     │   │      │  PostgreSQL  │
        │    Server      │   │      │   Database   │
        │   (SFU)        │   │      │              │
        │                │   │      │ - rooms      │
        │ - Media        │   │      │ - participants│
        │   Forwarding   │   │      │ - raised_hands
        │ - ICE          │   │      └──────────────┘
        │ - WebRTC       │   │
        │   Routing      │   │      ┌──────▼──────┐
        └────────────────┘   │      │    Redis     │
                             │      │   Cache      │
                             │      │              │
                             │      │ - hands queue
                        ┌────▼───────┤ - room locks │
                        │            │ - state      │
                        │            └──────────────┘
                   (gRPC / HTTP)
```

## Component Architecture

### 1. API Handler Layer

Located in `internal/api/handlers.go`

**Responsibility**: HTTP request routing and validation

**Components**:
```go
type Handlers struct {
    roomService     *services.RoomService        // Room lifecycle
    featuresService *services.CallFeaturesService // Call features
}
```

**Routes Registered**:
- Room Management: POST/GET/DELETE `/rooms`, `/rooms/{roomName}`
- Participants: POST `/rooms/{roomName}/join`, `/rooms/{roomName}/leave`
- Hand Raising: POST/GET `/raise-hand`, `/lower-hand`, `/raised-hands`
- Moderator Controls: POST `/mute/{userId}`, `/kick/{userId}`, `/lock`, `/unlock`

**Request Flow**:
```
HTTP Request
    ↓
Gorilla Mux Router
    ↓
Handler Function
    ↓
Service Layer Call
    ↓
HTTP Response (JSON)
```

### 2. Room Service

Located in `internal/services/room_service.go`

**Responsibility**: Room lifecycle and LiveKit integration

**Key Methods**:

```go
CreateRoom(ctx, communityID, roomName, maxParticipants)
    → Calls LiveKit API
    → Returns RoomInfo with room_id

JoinRoom(ctx, roomName, userID, userName, role)
    → Generates JWT access token
    → Encodes role-based permissions
    → Returns JoinToken for WebRTC client

ListParticipants(ctx, roomName)
    → Queries LiveKit participant list
    → Returns ParticipantInfo array

MuteParticipant(ctx, roomName, userID, muted)
    → Updates LiveKit participant permissions
    → Sets CanPublish permission

DeleteRoom(ctx, roomName)
    → Calls LiveKit delete API
    → Removes all participants
```

**LiveKit Integration Points**:
- Uses `github.com/livekit/server-sdk-go`
- Client: `lksdk.NewRoomServiceClient(host, apiKey, apiSecret)`
- Endpoints used:
  - `CreateRoom`: Create new room
  - `ListParticipants`: Get room participants
  - `RemoveParticipant`: Remove individual or all
  - `UpdateParticipant`: Modify permissions
  - `DeleteRoom`: Remove room

### 3. Call Features Service

Located in `internal/services/call_features.go`

**Responsibility**: Call-specific features (hands, muting, locking)

**Key Methods**:

```go
RaiseHand(ctx, roomName, userID, userName)
    → Adds to raisedHands[roomName]
    → FIFO ordering by RaisedAt timestamp

GetRaisedHands(ctx, roomName)
    → Returns copy of queue
    → Includes acknowledgment status

AcknowledgeHand(ctx, roomName, userID, moderatorID)
    → Marks hand as acknowledged
    → Records moderator and timestamp

LockRoom(ctx, roomName, adminID)
    → Sets lockedRooms[roomName] = true
    → Join requests checked against this

MuteAll(ctx, roomName, moderatorID)
    → Gets all participants
    → Calls roomService.MuteParticipant for each
    → Skips the moderator
```

**State Management**:
```go
type CallFeaturesService struct {
    raisedHands map[string][]*RaisedHand // In-memory queue
    lockedRooms map[string]bool           // In-memory locks
    mu          sync.RWMutex              // Thread safety
}
```

**Concurrency**: Uses `sync.RWMutex` for thread-safe access to shared state.

### 4. Configuration Layer

Located in `internal/config/config.go`

**Configuration Loading**:
```go
type Config struct {
    ModulePort       int    // REST API port (default: 8093)
    GrpcPort         int    // gRPC port (default: 50067)
    ModuleName       string // "module_rtc"
    ModuleVersion    string // "1.0.0"
    LiveKitHost      string // "localhost:7880"
    LiveKitAPIKey    string // API credentials
    LiveKitAPISecret string
    DatabaseURL      string // PostgreSQL connection
    LogLevel         string // DEBUG/INFO/WARN
    HubAPIURL        string // Hub API for notifications
}
```

**Environment Variables**:
- `MODULE_PORT`: REST API port
- `GRPC_PORT`: gRPC port
- `LIVEKIT_HOST`: LiveKit server address
- `LIVEKIT_API_KEY`: LiveKit API key
- `LIVEKIT_API_SECRET`: LiveKit API secret
- `DATABASE_URL`: PostgreSQL connection string
- `LOG_LEVEL`: Logging level
- `HUB_API_URL`: Hub API endpoint

## Peer Connection Lifecycle

### Complete Signaling Flow

```
┌──────────────────────────────────────────────────────────────┐
│                      Browser Client A                         │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 1. User clicks "Join Call"                             │  │
│  │    POST /api/v1/rooms/{name}/join                      │  │
│  │    {"user_id": "user_a", "role": "speaker"}           │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                  Module RTC Handler                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 2. JoinRoom handler receives request                  │  │
│  │    - Checks if room is locked (IsRoomLocked)          │  │
│  │    - If locked: return 403 Forbidden                  │  │
│  │    - If unlocked: continue                            │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   Room Service                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 3. JoinRoom(ctx, roomName, userID, userName, role)    │  │
│  │    - Creates auth.AccessToken with role-based grant   │  │
│  │    - Sets permissions based on role:                  │  │
│  │      * host/moderator/speaker: CanPublish=true        │  │
│  │      * viewer: CanPublish=false                       │  │
│  │    - Generates JWT token (24h validity)               │  │
│  │    - Returns JoinToken with token                     │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                  Browser Client A                            │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 4. Client receives token                              │  │
│  │    {                                                   │  │
│  │      "token": "eyJhbGc...",                           │  │
│  │      "room_name": "community_1_meeting",              │  │
│  │      "identity": "user_a"                             │  │
│  │    }                                                   │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│              LiveKit JavaScript Client                       │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 5. room.connect(livekit_url, token)                    │  │
│  │    - Validates JWT signature                          │  │
│  │    - Establishes WebSocket connection to LiveKit      │  │
│  │    - Performs ICE candidate gathering                 │  │
│  │    - Creates peer connections                         │  │
│  │    - Obtains media streams (audio/video)              │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   LiveKit SFU                                │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 6. Receives WebRTC connection from Client A            │  │
│  │    - Validates JWT permissions                        │  │
│  │    - Adds as participant in room                      │  │
│  │    - Starts receiving audio/video tracks              │  │
│  │    - Notifies other participants of join              │  │
│  │    - Begins forwarding tracks to other peers          │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────────────────┘
                       │
        ┌──────────────┴──────────────┐
        │                             │
        ▼                             ▼
   [Browser B]                   [Browser C]
  (receives                      (receives
   participant                    participant
   join event)                    join event)
```

### Raise Hand Sequence

```
User raises hand:
    ↓
POST /api/v1/rooms/{name}/raise-hand
    ↓
Handler.RaiseHand()
    ↓
CallFeaturesService.RaiseHand()
    ├─ Lock mutex
    ├─ Check if already raised (idempotent)
    ├─ Append to raisedHands[roomName] queue
    └─ Unlock mutex
    ↓
Return {"success": true}

Moderator acknowledges:
    ↓
POST /api/v1/rooms/{name}/acknowledge-hand/{userId}
    ↓
Handler.AcknowledgeHand()
    ↓
CallFeaturesService.AcknowledgeHand()
    ├─ Lock mutex
    ├─ Find user in queue
    ├─ Set acknowledged_at timestamp
    ├─ Set acknowledged_by moderator ID
    └─ Unlock mutex
    ↓
Return {"success": true}

Moderator unmutes user:
    ↓
POST /api/v1/rooms/{name}/unmute/{userId}
    ↓
Handler.UnmuteParticipant()
    ↓
CallFeaturesService.UnmuteParticipant()
    ↓
RoomService.MuteParticipant(ctx, roomName, userId, false)
    ↓
LiveKit UpdateParticipant API
    ├─ Sets CanPublish=true
    └─ Client receives permission grant
    ↓
WebRTC stream flows to SFU
```

## ICE, STUN, and TURN Configuration

### ICE Candidate Gathering

LiveKit handles ICE gathering internally. Module RTC configures via environment:

**STUN Servers** (used for NAT traversal discovery):
- Built into LiveKit server (default)
- Can specify custom via LiveKit configuration
- Used to discover public IP behind NAT

**TURN Servers** (used for UDP hole-punch and relay):
- Required for connections through restrictive firewalls
- Configured in LiveKit deployment
- Module RTC inherits TURN config from LiveKit

**LiveKit STUN/TURN Setup**:
```yaml
# In LiveKit server config
ice:
  stun_servers:
    - stun:stun.l.google.com:19302
    - stun:stun1.l.google.com:19302

  turn_servers:
    - urls:
        - "turn:turn.example.com?transport=udp"
        - "turn:turn.example.com?transport=tcp"
      username: user
      credential: password
```

**Connection Cascade**:
1. Client attempts **direct UDP** to SFU (best: low latency, no relay cost)
2. If direct fails, client attempts **STUN-discovered address** (works behind simple NAT)
3. If STUN fails, client attempts **TURN relay** (works behind restrictive firewall)

## Data Flows

### Participant State Update Flow

```
Moderator mutes User:
    ↓
POST /api/v1/rooms/{name}/mute/{userId}
    ↓
Handler.MuteParticipant()
    │
    ├─ vars["userId"] = user identity
    │
    ├─ CallFeaturesService.MuteParticipant()
    │   │
    │   └─ RoomService.MuteParticipant(ctx, roomName, userID, true)
    │       │
    │       ├─ Creates UpdateParticipantRequest
    │       │  └─ Permission: CanPublish=false
    │       │
    │       ├─ Calls LiveKit gRPC API
    │       │
    │       └─ LiveKit updates participant state
    │           ├─ Sends permission update to client via WebSocket
    │           └─ Notifies other participants
    │
    ├─ Client receives permission update
    │
    └─ Client-side tracks stop publishing to server
        └─ Media stream to SFU ceases
```

### Room Lock Flow

```
Moderator locks room:
    ↓
POST /api/v1/rooms/{name}/lock
    ↓
Handler.LockRoom()
    │
    ├─ CallFeaturesService.LockRoom()
    │   │
    │   ├─ Lock mutex
    │   ├─ Set lockedRooms[roomName] = true
    │   └─ Unlock mutex
    │
    └─ Return {"success": true}

When participant tries to join:
    ↓
POST /api/v1/rooms/{name}/join
    ↓
Handler.JoinRoom()
    │
    ├─ CallFeaturesService.IsRoomLocked(ctx, roomName)
    │   │
    │   ├─ Lock mutex (read)
    │   ├─ Return lockedRooms[roomName]
    │   └─ Unlock mutex
    │
    ├─ If locked: return 403 Forbidden "Room is locked"
    │
    └─ If unlocked: proceed with JoinRoom
```

## Scalability Architecture

### Horizontal Scaling

Module RTC scales horizontally through:

**1. Stateless Servers**
- Each instance is identical
- No session affinity required
- Any request can go to any instance
- Instances behind load balancer

**2. Distributed State**
- Room locking: Stored in Redis (for future: cross-instance consistency)
- Hand raising queue: In-memory with Redis backup
- Participant state: Queried from LiveKit (source of truth)

**3. Database Sharing**
- PostgreSQL for room/participant persistence
- All instances read/write same database
- No instance-specific data

**4. LiveKit as SFU**
- Single LiveKit cluster serves all Module RTC instances
- Handles media routing at scale
- Decouple media from signaling

```
Load Balancer
    ↓
┌───┬───┬───┬───┐
│ M │ M │ M │ M │  (Module RTC instances)
│ R │ R │ R │ R │
│ T │ T │ T │ T │
│ C │ C │ C │ C │
└───┴───┴───┴───┘
    ↓   ↓   ↓   ↓
    │ All instances share ↓
    ├─────────────────┬──────────────┬─────────────┐
    │                 │              │             │
  PostgreSQL        Redis         LiveKit      Hub API
```

**Scaling Limits**:
- **HTTP Capacity**: Limited by load balancer and VM resources
- **Database Capacity**: PostgreSQL connection pooling
- **Redis Capacity**: In-memory state size
- **LiveKit Capacity**: Media forwarding (1000s of participants/room)
- **Target**: 1000+ concurrent users per call, 10+ concurrent calls/community

## Error Handling & Recovery

### Connection Failures

**LiveKit Connectivity Lost**:
- Service logs errors
- HTTP responses fail with 500 Internal Server Error
- Client should retry join request
- Alternative: Use cached room info

**Database Connectivity Lost**:
- Room operations fail
- Hand raising still works (in-memory)
- Graceful degradation: In-memory state preserved

**WebRTC Connection Failed**:
- LiveKit handles client-side recovery
- Client exponential backoff + retry
- ICE fallback from direct → STUN → TURN

### Cleanup & Timeout

**Participant Cleanup**:
- User calls `/leave` endpoint explicitly
- LiveKit also times out idle participants (configurable)
- Hand automatically lowered on leave

**Empty Room Cleanup**:
- LiveKit removes room when empty after timeout (300s default)
- Hand raising state cleared when room deleted
- Database records remain for audit

## Thread Safety

### Mutex Usage

```go
type CallFeaturesService struct {
    raisedHands map[string][]*RaisedHand
    lockedRooms map[string]bool
    mu          sync.RWMutex  // Protects both maps
}

// Read operations: RLock
func (s *CallFeaturesService) IsRoomLocked(roomName string) bool {
    s.mu.RLock()  // Multiple readers allowed
    defer s.mu.RUnlock()
    return s.lockedRooms[roomName]
}

// Write operations: Lock
func (s *CallFeaturesService) LockRoom(roomName string) {
    s.mu.Lock()   // Exclusive write
    defer s.mu.Unlock()
    s.lockedRooms[roomName] = true
}
```

**Concurrency Model**:
- Multiple goroutines handle HTTP requests concurrently
- All access to shared state protected by mutex
- No deadlock: Single mutex, lock always in same order
- RWMutex allows concurrent reads (GetRaisedHands)

## Performance Characteristics

### Operation Latencies

| Operation | Latency | Bottleneck |
|-----------|---------|-----------|
| Create Room | 100-500ms | LiveKit API call |
| Join Room | 50-150ms | JWT generation |
| Get Raised Hands | 1-5ms | In-memory read |
| Raise Hand | 5-20ms | Map append + lock |
| Mute Participant | 50-200ms | LiveKit API update |
| Lock Room | 1-5ms | Map write + lock |

### Memory Usage

Per 1000 participant room:
- Room metadata: ~1KB
- 1000 participants: ~100KB (participant info)
- Raised hands (100 users): ~10KB
- HTTP server buffers: ~10MB total

### Concurrency Capacity

Single instance (typical 4-core VM):
- ~200 concurrent HTTP requests
- ~1000+ room queries/second
- Scales with CPU cores

## Future Improvements

1. **Redis State Persistence**: Move hands queue to Redis for cross-instance consistency
2. **WebSocket Support**: Real-time hand raise notifications
3. **Recording**: Video recording integration with SeaweedFS (S3-compatible)
4. **Screen Sharing**: Built-in screen annotation
5. **Metrics**: Prometheus metrics export
6. **Tracing**: Distributed tracing (OpenTelemetry)
7. **Webhooks**: Event notifications to external systems

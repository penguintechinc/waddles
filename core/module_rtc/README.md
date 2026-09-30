# Module RTC

Go/LiveKit-based WebRTC module for community calls. Stateless, scalable to 1000+ users per call.

## Features

- LiveKit integration for WebRTC
- Room creation and management
- Raise hand queue (FIFO order)
- Moderator controls (mute all, kick, lock room)
- Participant role management (host, moderator, speaker, viewer)
- Screen annotations and shared whiteboard
- Recording support (optional, storage to SeaweedFS/S3)

## Configuration

Environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `REST_PORT` | HTTP REST API port | 8093 |
| `GRPC_PORT` | gRPC service port | 50067 |
| `DB_HOST` | PostgreSQL host | localhost |
| `DB_PORT` | PostgreSQL port | 5432 |
| `DB_NAME` | Database name | waddlebot |
| `DB_USER` | Database user | waddlebot |
| `DB_PASS` | Database password | - |
| `LIVEKIT_HOST` | LiveKit server host | localhost |
| `LIVEKIT_API_KEY` | LiveKit API key | - |
| `LIVEKIT_API_SECRET` | LiveKit API secret | - |
| `REDIS_HOST` | Redis host (for room state) | localhost |
| `REDIS_PORT` | Redis port | 6379 |

## API Endpoints

### Room Management

- `GET /api/v1/rooms` - List rooms for a community
- `GET /api/v1/rooms/:room_name` - Get room details
- `POST /api/v1/rooms` - Create a room
- `DELETE /api/v1/rooms/:room_name` - Delete a room

### Room Controls

- `POST /api/v1/rooms/:room_name/lock` - Lock room
- `POST /api/v1/rooms/:room_name/unlock` - Unlock room
- `POST /api/v1/rooms/:room_name/mute-all` - Mute all participants

### Participants

- `GET /api/v1/rooms/:room_name/participants` - List participants
- `POST /api/v1/rooms/:room_name/kick` - Kick participant
- `POST /api/v1/rooms/:room_name/join` - Join room (returns token)
- `POST /api/v1/rooms/:room_name/leave` - Leave room

### Raised Hands

- `GET /api/v1/rooms/:room_name/raised-hands` - Get raised hands queue
- `POST /api/v1/rooms/:room_name/raise-hand` - Raise hand
- `POST /api/v1/rooms/:room_name/lower-hand` - Lower hand
- `POST /api/v1/rooms/:room_name/acknowledge-hand` - Acknowledge raised hand

### Health

- `GET /health` - Health check

## Database Tables

- `community_call_rooms` - WebRTC call rooms with LiveKit room IDs
- `community_call_participants` - Call participant tracking with roles
- `call_raised_hands` - Queue for raise hand feature

## Docker

```bash
docker build -t waddlebot/module-rtc .
docker run -p 8093:8093 -p 50067:50067 waddlebot/module-rtc
```

## Scaling

- Stateless containers behind load balancer
- Redis for room state coordination across instances
- LiveKit SFU handles media routing
- Target: 1000 concurrent users per call, multiple communities

## Participant Roles

| Role | Permissions |
|------|-------------|
| `host` | Full control, can promote/demote |
| `moderator` | Can mute, kick, acknowledge hands |
| `speaker` | Can unmute self, share screen |
| `viewer` | Listen only, can raise hand |

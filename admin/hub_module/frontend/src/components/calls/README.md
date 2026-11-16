# Calls UI (admin console + member voice view)

Thin presentation layer over hub-api's calls REST API (`openapi/v1.yaml`, `hub_api/blueprints/v1/calls.py`).
The webui holds no media-service ids, secrets or provider logic and calls hub-api only.

| Piece | File |
|---|---|
| Typed client, error mapping, UUID guard | `src/services/callsApi.ts` |
| Query/mutation hooks (polling, invalidation) | `src/hooks/useCalls.ts` |
| Moderator console | `src/pages/admin/AdminCommunityCalls.tsx`, `RoomDetail.tsx`, `HandsQueue.tsx`, `ParticipantList.tsx` |
| Member voice view | `src/components/interaction/VoiceView.tsx` |
| Town Hall page (`/community/:communityId/town-hall`) | `src/pages/community/TownHall.tsx` |
| In-room experience (lobby, tiles, controls, hands, failures) | `CallRoomView.tsx`, `CallControls.tsx`, `MediaTile.tsx`, `MyHandControl.tsx`, `RoomPresence.tsx` |
| Media client and session state machine | `src/services/media/*` |

## Contract the UI relies on

- Rooms are addressed by their UUID `room_id`; the creator-supplied `label` is what is shown. A non-UUID id is refused
  client-side and by hub-api (`400 INVALID_ROOM_ID`).
- Participants and hands carry `participant_id` (UUID, API calls only) and a hub-resolved `display_name` ("guest" when
  unknown). The UI never renders ids.
- Hands: members `raise-hand` / `lower-hand`; moderators list the queue (oldest first), `acknowledge-hand`, `reset-hands`,
  per-participant `mute` / `unmute`, `mute-all`, `kick`, `lock` / `unlock`.
- Errors are `{ success: false, error: { code, message, details? } }`; the UI shows `error.message`.

## States (every read has all of them)

| Situation | UI |
|---|---|
| Flag `waddles.streaming.rtc` OFF (HTTP 402 `FEATURE_NOT_ENABLED`) | "Calls are not enabled for this community"; no create button, no retry |
| 502/503/504 or `details.retryable` | Server message plus a "Try again" button; polling stops until retried |
| 404 / 403 / 429 / other | Error state with the server message, never an empty list |
| Response shape drift | Error state (`WireShapeError`), never an empty list |

## Media (browser WHIP / WHEP)

`src/services/media/` is a small typed WebRTC client for svc-streaming's room media endpoints:

- `whipWhep.ts`: `WhipPublisher` (POST SDP offer with `Authorization: Bearer <room token>`, answer applied, `DELETE Location` on stop)
  and `WhepSubscriber` (one per remote publisher, `?source=<participant uuid>`).
- `devices.ts`: `getUserMedia` with permission/no-device/in-use classification, device enumeration, track cleanup.
- `callSession.ts`: the join/leave state machine (`idle, requesting-media, joining, connecting, connected, leaving, failed`),
  listen-only fallbacks, remote reconciliation, mute/camera/device switching, reconnect.

Server constraints this client is built around (`core/svc_streaming/src/rooms/media.rs`): non-trickle ICE (the offer is posted after
gathering; PATCH does not exist, so there is **no ICE restart**: recovery is a fresh publish via `reconnect()`), the SFU forwards RTP
without transcoding and its viewer tracks are H264 + Opus, so the client offers H264 (packetization-mode 1) and Opus only. A browser
without H264 shares audio only and says so.

The room token returned by `join` lives only in private fields of the session/publisher/subscriber: it is never rendered, stored,
logged, put in a URL or included in an error message. ICE servers come from the join response (STUN only for now; TURN is v3.3.x).
What the room policy granted (`publish` / `subscribe` in the join response) is honoured before any media request is made.

Members have no participants route in hub-api today, so member views show head counts and numbered tiles; moderators get names.

Real-media validation against a deployed server is a manual two-browser run: `docs/rtc-town-hall-validation.md`.

## Accessibility

Hand-queue changes are announced through a polite `role="status"` live region (`useHandAnnouncements`); the member
raise/lower button is a toggle (`aria-pressed`); every row action has an accessible name including the person's name.

## Tests

`callsApi.openapi.contract.test.ts` runs every client function against a recording adapter and fails if the request
path, method or body keys are not in `openapi/v1.yaml`, if a parser rejects a spec-generated response (all properties and
required-only), or if a calls/voice operation in the spec has no client function.

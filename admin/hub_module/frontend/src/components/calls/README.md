# Calls UI (admin console + member voice view)

Thin presentation layer over hub-api's calls REST API (`openapi/v1.yaml`, `hub_api/blueprints/v1/calls.py`).
The webui holds no media-service ids, secrets or provider logic and calls hub-api only.

| Piece | File |
|---|---|
| Typed client, error mapping, UUID guard | `src/services/callsApi.ts` |
| Query/mutation hooks (polling, invalidation) | `src/hooks/useCalls.ts` |
| Moderator console | `src/pages/admin/AdminCommunityCalls.tsx`, `RoomDetail.tsx`, `HandsQueue.tsx`, `ParticipantList.tsx` |
| Member voice view | `src/components/interaction/VoiceView.tsx` |

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

## Media is not wired yet

There is no browser media (WHIP/WHEP) client in this release. Joining a room registers the member as a participant
(so they can raise a hand), and the in-call view says plainly that no audio or video is sent or received
(`MediaJoinNotice`). The room token returned by `join` is intentionally discarded until a media client exists.

## Accessibility

Hand-queue changes are announced through a polite `role="status"` live region (`useHandAnnouncements`); the member
raise/lower button is a toggle (`aria-pressed`); every row action has an accessible name including the person's name.

## Tests

`callsApi.openapi.contract.test.ts` runs every client function against a recording adapter and fails if the request
path, method or body keys are not in `openapi/v1.yaml`, if a parser rejects a spec-generated response (all properties and
required-only), or if a calls/voice operation in the spec has no client function.

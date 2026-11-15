# waddles.core.app.rooms

Rust APP bundle (`world stage-next-tools`): rooms per community, rooms per calendar event
(event `public_id` UUID stored as a data link -- no app-to-app call), virtual rooms inside a
room (breakout / stage / green room), role-based access and schedules. Every media/token
operation is a gated `tool.invoke` to `waddles.core.service.webrtc`. Also the home of
room moderation (raise-hand queue, kick/mute/lock) -- see below.

## Chat (`!room`, `!rooms`)

`!room [list]`, `!room info <room>`, `!room join [<room>]`, `!room create <label> [--event <uuid>]`,
`!room close <room>`, `!room virtual <parent> <breakout|stage|green_room> <label>`.
Create/close/virtual ride webrtc tools carrying `required_scope: community:admin`, so the
HOST verifies the actor -- the bundle never trusts a chat badge. `!room join` replies with a
sign-in link (`join_base_url` config) and never a credential.

## Moderation (flag `rooms.moderation`, default OFF)

All under `!room`; verbs follow the shared vocabulary (bare noun = read). Room and member
arguments are UUIDs only -- never usernames. With the flag off every verb below fails loud
("not enabled"), never a silent no-op; legacy verbs are unaffected.

| Command | Min role | Effect |
|---|---|---|
| `hand [list] <room>` | member | moderator: ordered queue (`*` = acknowledged); member: your own place in it |
| `hand add <room>` / `hand remove <room>` | member | raise / lower your hand (idempotent) |
| `hand remove <room> <member>` | moderator | lower someone else's hand |
| `hand set ack <room> <member>` | moderator | acknowledge a hand |
| `hand reset <room>` | moderator | clear the queue |
| `participants [list] <room>` | member | live participants (UUID + pub/sub/muted) |
| `participant remove <room>` | member | leave the room |
| `participant remove <room> <member>` | moderator | kick (closes sessions, blocks rejoin, lowers hand) |
| `mute <room>` | member | list muted participants |
| `mute enable <room> <member\|all>` / `mute disable <room> <member>` | moderator | mute / unmute (`all` = every publisher but you) |
| `lock [<room>]` / `lock enable\|disable <room>` | member / moderator | read / set lock; locked rooms refuse non-staff joins |
| `rooms [list]` | member | live rooms from the media plane |

AuthZ is the per-room ACL role (`acl.set`); a moderator cannot act on an equal or higher role.
The app sends NO `actor_id`/`actor_role` to any webrtc tool: each tool takes the host-verified
actor and is gated by its declared scope (`room.leave` and the own-hand tools: member; kick, mute,
lock and the other hand tools: moderate), so an ACL moderator without the community moderate scope
is refused by the host. The raised-hand queue is NOT kept here:
it is authoritative in svc-streaming and reached through the webrtc tools `room.hand.raise|lower|
list` (member scope, own hand only) and `room.hand.lower_other|list_all|ack|reset` (moderate scope,
verified by the host before dispatch). The app sends NO actor or role argument: the webrtc tool
takes the host-verified actor, and the tool the app picks (by the ACL role) is gated by scope. There
is no `hands.*` kv key, the server clears a hand on leave/kick, and a member's `hand list` shows only
their own place in the queue (moderators see every UUID). The rest of moderation fronts
`room.list|participants|leave|kick|mute|unmute|mute_all|lock|unlock`, which forward to
svc-streaming's moderation host API (`core/svc_streaming/docs/rooms-moderation-host-api.md`; the
host enforces the role again).
Telemetry: counter `waddles_bundle_counter_rooms_ops` and histogram `rooms_op_seconds`
(labels `op`, `outcome` only) plus one PII-free debug log per command; the stage supplies the span.

## Admin actions (bundle-admin gateway)

Delivered as an `admin.action` event `{op, args}` (the gateway has verified the caller):
`room.list|get|create|virtual|close|link_event|unlink_event|set_access|set_schedule`,
`acl.set|remove` (`guest|member|moderator|admin` per room, keyed by actor UUID) and
`join_token` (mints publish/subscribe endpoints for a room per the access policy).

## Access policy (`grants_for`)

Closed/outside-schedule/locked rooms refuse non-staff; green room = moderators/admins only; stage =
only moderators/admins publish; guests (opt-in per room) can only subscribe; everyone else
publish + subscribe. `grants_for` is an adapter over the webrtc bundle's canonical
`src/grants.rs` (compiled in from the same file; contract: `../webrtc/contract/grants.json`);
join tokens are requested with the contract's short TTL (60 s) and a UUID participant only.
State is community-scoped kv (`room.<uuid>`, `acl.<room>.<actor>`,
`event.<uuid>`, `rooms.index`; no `:` in keys).

Contract with the service: `tests/contract_calls.json` is pinned by a unit test here and by
`scripts/ci/tests/test_rooms_webrtc_contract.py` against the webrtc manifest.

## Identity (per-community pseudonym)

The app's actor is the caller's per-community call-participant pseudonym, obtained through the webrtc
tool `participant.self` (host-derived `__ctx.participant`), never the global community `user_uuid`. ACL keys,
`created_by`, join-token participants and every svc-streaming id therefore use the same id hub-api's REST layer
uses. Unresolvable => the command is refused (fail closed). Note: ACL entries / rooms created before this
change were keyed by the old id and must be recreated (alpha only; nothing is migrated).

Room facts: `room.create` now sets `kind` (community, or stage / green_room for virtual rooms),
`closed=false` and `allow_guests=false` on svc-streaming's `CreateRoom` (webrtc tool args -> host `room.create`),
so svc-streaming enforces them on join and hub-api can read them from the Room (T2b). Later fact changes
(close, set_access, set_schedule) still only update the app's kv; pushing them through `UpdateRoom` needs a webrtc
`room.update` tool (not built).

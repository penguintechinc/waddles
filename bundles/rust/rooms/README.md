# waddles.core.app.rooms

Rust APP bundle (`world stage-next-tools`): rooms per community, rooms per calendar event
(event `public_id` UUID stored as a data link -- no app-to-app call), virtual rooms inside a
room (breakout / stage / green room), role-based access and schedules. Every media/token
operation is a gated `tool.invoke` to `waddles.core.service.webrtc`. Replaces
`hub_api/blueprints/v1/calls.py` + `services/calls_proxy.py` and Go `core/module_rtc`
(deletion is a later PR).

## Chat (`!room`, `!rooms`)

`!room [list]`, `!room info <room>`, `!room join [<room>]`, `!room create <label> [--event <uuid>]`,
`!room close <room>`, `!room virtual <parent> <breakout|stage|green_room> <label>`.
Create/close/virtual ride webrtc tools carrying `required_scope: community:admin`, so the
HOST verifies the actor -- the bundle never trusts a chat badge. `!room join` replies with a
sign-in link (`join_base_url` config) and never a credential.

## Admin actions (bundle-admin gateway)

Delivered as an `admin.action` event `{op, args}` (the gateway has verified the caller):
`room.list|get|create|virtual|close|link_event|unlink_event|set_access|set_schedule`,
`acl.set|remove` (`guest|member|moderator|admin` per room, keyed by actor UUID) and
`join_token` (mints publish/subscribe endpoints for a room per the access policy).

## Access policy (`grants_for`)

Closed/outside-schedule rooms refuse non-staff; green room = moderators/admins only; stage =
only moderators/admins publish; guests (opt-in per room) can only subscribe; everyone else
publish + subscribe. State is community-scoped kv (`room.<uuid>`, `acl.<room>.<actor>`,
`event.<uuid>`, `rooms.index`; no `:` in keys).

Contract with the service: `tests/contract_calls.json` is pinned by a unit test here and by
`scripts/ci/tests/test_rooms_webrtc_contract.py` against the webrtc manifest.

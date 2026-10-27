# Community Tools Commands

Event, bookmark, looking-for-group, ticket and label commands ship as first-party App Bundles. Each keeps its state in community-scoped kv and is gated by a `waddles.command-<name>` flag, default OFF. Shared behavior (platforms, flags, mod checks, logging) is in [Command Bundles Reference](command-bundles.md).

Mod-only verbs require the caller to be a moderator or broadcaster. If the platform reports no role information the verb is refused (fails closed).

> **Concurrency.** Lists are stored as one value and updated read-modify-write. Two writes in the same instant in one community can lose one update. This is acceptable at chat scale.

## `!event` and `!rsvp`

Light community events with RSVPs. Flag: `waddles.command-event`. This is separate from event sync (Waddles pushing events out to Discord) and shares no data with it.

| Command | Effect | Who |
|---|---|---|
| `!event` or `!event list` | Lists events (10 shown). | Anyone |
| `!event view id` | One event with its yes/no RSVP counts. | Anyone |
| `!event create name when` (alias `add`) | Creates an event. `name` is one word or a `"quoted phrase"`, up to 40 characters. `when` is free text, up to 64 characters, displayed as typed and never parsed as a date. | Moderator or broadcaster |
| `!event remove id` (alias `delete`) | Deletes an event. | Moderator or broadcaster |
| `!rsvp id yes` / `!rsvp id no` | Records or changes your answer. | Anyone with a linked Waddles identity |

Limits: 50 events per community, 500 RSVPs per event. RSVPs are stored by the caller's UUID. A caller whose identity is not linked gets `RSVP needs a linked Waddles identity; yours isn't linked yet` and nothing is stored.

## `!bookmark`

Saved links for the community. Flag: `waddles.command-bookmark`.

| Command | Effect | Who |
|---|---|---|
| `!bookmark add url` | Saves a link and returns its short id. | Anyone |
| `!bookmark list` | Lists saved links. | Anyone |
| `!bookmark remove id` | Removes a link. | Moderator or broadcaster |

Only `http` and `https` URLs with a dotted host are accepted, with no embedded credentials or whitespace, up to 500 characters. Limit 50 bookmarks per community. The author is not stored.

## `!lfg`

Looking-for-group posts. Flag: `waddles.command-lfg`.

| Command | Effect | Who |
|---|---|---|
| `!lfg create description` | Opens a group you own. | Anyone |
| `!lfg list` | Lists open groups with member counts. | Anyone |
| `!lfg join id` | Joins a group. | Anyone |
| `!lfg leave id` | Leaves a group. If the owner or the last member leaves, the group is disbanded. | Members |
| `!lfg remove id` | Removes a group. | Group owner, moderator or broadcaster |

Limits: 25 open groups per community, one group per owner, 20 members per group, 200 characters per description. Members are stored as pseudonymous UUIDs; `list` shows counts only.

## `!ticket`

A support-ticket desk. Flag: `waddles.command-ticket`.

| Command | Effect | Who |
|---|---|---|
| `!ticket description` | Opens a ticket and returns its id. | Anyone |
| `!ticket status id` | Shows a ticket. | Its creator, moderator or broadcaster |
| `!ticket list` | Lists open tickets. | Moderator or broadcaster |
| `!ticket close id` | Closes a ticket. | Moderator or broadcaster |

Limits: 100 open tickets per community, 3 open per creator, 300 characters per description. Closed tickets are kept 30 days, then expire. Asking for `status` on someone else's ticket gives the same reply as a missing ticket, so ids cannot be enumerated. Creators are stored as pseudonymous UUIDs.

## `!label`

Labels attached to a user, keyed by the user's UUID. Flag: `waddles.command-label`.

| Command | Effect | Who |
|---|---|---|
| `!label add user label` | Adds a label to `user`. | Moderator or broadcaster |
| `!label remove user label` | Removes a label from `user`. | Moderator or broadcaster |
| `!label list [user]` | Lists labels. Defaults to yourself. | Anyone |

`user` can be a UUID, a Discord mention, or `@name` / `name` on platforms that expose only a handle. The target is converted to a UUID and the typed text is discarded; replies show only the first 8 characters of the UUID. Labels use `a-z`, `0-9`, space, `_` and `-`, up to 32 characters, and each user holds up to 10.

> **Pseudonymous ids.** Until the identity-tokenization pipeline supplies hub user UUIDs, handle-derived UUIDs are non-reversible stand-ins. A Discord `@name` typed as plain text derives a different UUID from the same person's id-based mention, so label by mention on Discord.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Something went wrong accessing ... storage` | The kv call failed. | Retry; check bundle-executor logs. |
| `!rsvp` rejected | Caller identity is not linked. | Link your Waddles identity. |
| Mod verb refused | No role information from the platform. | Run it as the broadcaster. |

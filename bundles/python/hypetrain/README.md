# hypetrain (Python)

`!hypetrain` -- a shared chat hype/combo meter. Chat activity builds one combo counter for the
whole community inside a short time window; the bundle announces milestones ("Hype x10!") and the
meter resets after a stretch of silence. First-party (`provider: builtin`,
`app_id: waddles.core.example.hypetrain`), **kv-only**, per community, activation-gated (no
feature flag -- the installed -> enabled -> activated check is the only gate).

## Behavior

| Input | Effect |
|---|---|
| any chat line from an identifiable chatter | combo +1 (silent) |
| a line whose first word is `!hype` (any case) | combo +3 (silent -- the `hype` bundle owns that reply) |
| combo reaches a multiple of 10 | `Hype x10! Keep the chat moving!` (then x20, x30, ...) |
| `!hypetrain` | `Hype train: x14 (best x30) - 22s left, keep chatting!` or the all-time best when idle |
| `!hypetrain reset` | broadcaster/moderator only: ends the running train (best is kept) |

Anything else after `!hypetrain` replies with usage and never echoes what was typed.
`!hypetrain ...` lines are control input and never count toward the combo.

## Window, reset and rate limits

All times are the event's own ingest stamp (`occurred_at`, whole seconds) -- no wall clock, no
TTL, so a redelivered event is idempotent.

| Rule | Value | Behavior |
|---|---|---|
| train window | 30 s | a contribution within 30 s (inclusive) of the last one continues the train; 31 s starts a new train at its own weight, the old peak survives as `best` |
| per-chatter gap | 5 s | one chatter counts at most once per 5 s (12/min), so spam cannot flood the meter |
| announcement gap | 20 s | at most one announcement per 20 s; a milestone suppressed by the gap stays pending and the HIGHEST milestone reached is announced by the next counted line once the gap has passed |
| one per milestone | per train | a milestone is announced once per train; a fresh train can announce x10 again |
| stale replay | window | an event older than `last - 30 s` is ignored; older-but-recent events still count |

Why not a TTL'd `kv.increment` ("combo key that expires after inactivity"): a lapsed TTL key that
is re-created leaks one unit of the host's 10,000-key quota every time
(`core/bundle_host_kv/src/backend.rs` module docs), and every train would be such a cycle.

## Data model (`kv`, host-scoped per `(tenant, community, app_id)`)

| Key | Value |
|---|---|
| `hypetrain.state` | `{"c":combo,"l":last,"b":best,"m":announced milestone,"t":last announcement}` -- integers only |
| `hypetrain.slot.<0..255>` | epoch seconds of the last counted line from chatters hashing into that slot |

Constant footprint of 257 keys, durable (no TTL). The per-chatter guard hashes the chatter into
one of 256 slots instead of keeping a key per chatter; a collision can only make the meter
under-count, never over-count. The state read-modify-write is lock-free: concurrent lines can
lose an increment or, rarely, announce twice -- cosmetic for a meter and bounded by the
announcement gap. Corrupt state is logged at ERROR and replaced on the next contribution.

## PII

No chatter id, name or text is stored or logged. The `{user:<id>}` token in `event.actor` is
hashed to pick a slot and discarded. A non-token actor (raw username, no actor, forged
`\{user:..\}`) is not counted. If the platform echoes the bot's own lines back as chat events they
count like any other chatter (once per gap). Logs carry only op names, reasons, counts and
exception type names; replies contain no chatter text and still pass through
`sanitize_user_text`.

## Permissions

`storage.kv`, `chat.send:discord`, `chat.send:twitch` -- nothing else (no `flags.read`).
`!hypetrain reset` reads `is_mod` / `is_broadcaster` as STRICT booleans (`is True`); an absent or
non-boolean badge (`"true"`, `1`) is denied. Registration in `bundles/core-bundles.yaml` is
intentionally not part of this change.

## Tests

`cd bundles/python/hypetrain && python3 -m pytest tests` -- window edges, announcement gap,
keyword weight, slot guard, strict badge matrix, corrupt state, kv failures, interleaved
concurrency, PII/charset checks. No `wit_fake_db`: the bundle declares no `data.tables`.

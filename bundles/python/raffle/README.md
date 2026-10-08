# raffle (Python)

`!raffle` (+ bare `!enter` alias) -- a per-community giveaway/raffle. Community-scoped
fun/utility content, first-party (`provider: builtin`, `app_id: waddles.core.example.raffle`),
**original Waddles content** -- not a port, not inspired by any third-party bot (contrast
`fish`/`music`, which credit superpenguintv (Psychoboy)'s `PenguinTwitchBot` for inspiration;
`author`/`license` here follow `bundles/python/count`/`bundles/python/pyping`'s own
`PenguinTech/waddles` convention for first-party, non-ported bundles).

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`) for
the bare/`list` shapes. `open`/`close`/`draw` are domain verbs outside the shared
`waddle_sdk.command.VERBS` vocabulary, special-cased ahead of the formal grammar parse --
exactly mirroring `music`'s own documented `next`/`skip` "bare advance keywords" extension.

**This is a single open/closed flag + one flat entrant list per community.** No prize tiers,
no weighted/multiple entries, no raffle history -- v1 scope only (see `src/app.py`'s own
module docstring).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!raffle open` | mod/broadcaster only | Clears any prior entrant list and marks the raffle OPEN. |
| `!raffle close` | mod/broadcaster only | Marks the raffle CLOSED. Entrants are left untouched. |
| `!raffle` / `!enter` (bare) | anyone | Enters the caller into the currently open raffle, once. Rejected with a reply if closed; a duplicate entry gets a friendly "already entered" reply. |
| `!raffle draw` | mod/broadcaster only | Picks one uniformly-random entrant and announces it. Does **not** require the raffle to be closed first, and does **not** clear state afterward -- re-open to start the next round. |
| `!raffle list` | anyone | The current entrant count and open/closed state. |

Any other grammar-legal verb (`set`/`add`/`sub`/`enable`/`disable`/`remove`/`delete`/`reset`) or
a malformed `!raffle ...`/`!enter ...` replies with usage text -- never silently dropped.

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller identity is a SHA-256 hash of the actor (never the raw username/actor id
-- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline (#429) in case
`event.actor` is still a raw username.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `raffle.entrants` | per-community | none | JSON array of entrant pseudonyms (SHA-256 hashes). |
| `raffle.state` | per-community | none | `b"open"` or `b"closed"` -- unset/any other value is treated as closed (fail-closed default). |

Keys use `.` as the separator, never `:` -- the real `kv` host capability rejects `:` as its
own reserved namespace separator (gh-631); see `waddle_sdk/kv.py`'s own module docstring.

## PII: winner announcement is pseudonym-only

Because only the SHA-256 pseudonym is ever retained, `!raffle draw` cannot resolve a past
entrant back to a display name the channel would recognize (unlike `!fish`/`!music`, which can
show the *caller's own* live username because that comes from the current event, not a stored
one). The announced winner is a short, non-identifying tag (`winner[:8]`) the actual winner can
self-recognize by comparing against their own freshly computed pseudonym -- see `src/app.py`'s
module docstring for the full rationale and the documented (not built) extension path.

## Deferred (not stubbed)

- **Resolvable winner identity / DM-the-winner.** Needs either the PII-tokenization pipeline
  (#429) or the platform's reputation/user-details cross-community lookup -- neither is a
  dependency of this v1.
- **Multiple concurrent raffles, prize tiers, weighted/multiple entries, raffle history.** Out
  of scope for this single open/closed-flag-plus-entrant-list v1.
- **Cross-community anything.** Every key here is scoped by `community_id` only.

# Command Bundles Reference

Chat commands that ship as first-party App Bundles. This page covers how they behave and the fun, utility, counter and social commands. Games and gambling are in [Games and Gambling](command-bundles-games.md); event, bookmark, LFG, ticket and label commands are in [Community Tools](command-bundles-community.md).

> **Availability.** `hug`, `boop` and `highfive` are on `release/v3.0.X`. The other 23 bundles on these pages are in the consolidated bundle batch (PR #727) and are available once it merges.

## Shared behavior

| Behavior | Detail |
|---|---|
| Platforms | Twitch and Discord. Each bundle's manifest declares a `chat.message` consume rule for both. None declares `supported_platforms`, so no further platform restriction applies (see [Bundle `supported_platforms`](bundle-supported-platforms.md)). |
| Trigger | Chat prefix `!`, matched case-insensitively on the first word. |
| Feature flag | Every command is gated by a `waddles.command-<name>` flag, **default OFF**. |
| Disabled command | A flag-off command is dropped. It is indistinguishable from an unrecognized command; no error is sent. |
| Permissions | Every bundle requests `flags.read`. Bundles that keep state also request `storage.kv`. None requests network egress. |
| State scope | kv state is scoped to tenant, community and bundle. No bundle shares state with another. |
| Mod-only verbs | Checked against the caller's moderator/broadcaster role. If the platform supplies no role information the check fails closed (the verb is refused). |
| Logging | PII-free. Logs carry platform, operation, counts and exception type, never chat text or usernames. |
| App id | `waddles.core.example.<name>`, version `1.0.0`. |

Turn a command on by enabling its flag (for example `waddles.command-catfact`) in the feature-flag backend for the tenant.

## Fun

Stateless. Each reply is picked at random from a curated list built into the bundle. No external API calls.

| Command | Reply | Flag |
|---|---|---|
| `!catfact` | A random cat fact. Trailing text is ignored. | `waddles.command-catfact` |
| `!dogfact` | A random dog fact. Trailing text is ignored. | `waddles.command-dogfact` |
| `!advice` | A random piece of advice. | `waddles.command-advice` |
| `!fortune` | A random fortune-cookie line. | `waddles.command-fortune` |
| `!waddle` | A random penguin-themed line. | `waddles.command-waddle` |
| `!hello`, `!hi`, `!hey` | A random friendly greeting. | `waddles.command-hello` |

## Utility

Stateless.

| Command | Reply | Flag |
|---|---|---|
| `!dice [NdM]` | Rolls `N` dice with `M` sides. No argument rolls one six-sided die. `N` is 1-100, `M` is 2-1000. Beyond 20 dice only the sum is shown. A bad spec returns the usage line, for example `!dice 2d6`. | `waddles.command-dice` |
| `!echo text` | Repeats `text`, up to 300 characters. Refuses text that starts with `/`, `.` or `!` so the bot cannot be made to run commands. No text returns `Usage: !echo <text>`. | `waddles.command-echo` |
| `!time` | Current UTC time from the host clock, formatted `YYYY-MM-DD HH:MM:SS UTC`. | `waddles.command-time` |
| `!bot` | Bot name, version and project links. | `waddles.command-bot` |

> **Trigger collision.** The existing `about` bundle also answers `!bot`. If both `waddles.command-about` and `waddles.command-bot` are on, both reply. Leave one flag off.

## Counters

Each counter keeps one total per community. All three use the same verbs.

| Verb | Effect | Who |
|---|---|---|
| `!NAME` | Adds 1 and replies with the new total. Any other trailing text (for example `!rip someone`) is treated as a bare add and is not stored. | Anyone |
| `!NAME add N` | Adds `N` (1-1000, default 1). | Anyone |
| `!NAME total` | Shows the total without changing it. Aliases: `get`, `show`, `count`. | Anyone |
| `!NAME reset` | Sets the total to 0. | Moderator or broadcaster |

| Command | Counts | Flag |
|---|---|---|
| `!rip` | Respects paid | `waddles.command-rip` |
| `!hype` | Hype | `waddles.command-hype` |
| `!cringe` | Cringe | `waddles.command-cringe` |

For named counters you define yourself, use the `count` bundle instead.

## Voting

`!vote` is a lighter tally than the `poll` bundle. Flag: `waddles.command-vote`.

| Command | Effect | Who |
|---|---|---|
| `!vote` | Shows current standings. | Anyone |
| `!vote start` | Opens a fresh tally. | Moderator or broadcaster |
| `!vote option` | Casts the caller's vote, or moves it if they already voted. Works only while the tally is open. | Anyone |
| `!vote close` | Closes the tally and shows final standings. | Moderator or broadcaster |

Options are single lowercase words of at most 32 characters, up to 20 distinct options per tally. Voters are stored as a truncated hash, one vote each. Two votes landing in the same instant can lose one update.

## Social

Stateless flavor-text replies. Flags are `waddles.command-hug`, `-boop` and `-highfive`.

| Command | Reply |
|---|---|
| `!hug [target]` | A wholesome hug, solo or aimed at `target`. |
| `!boop [target]` | A boop, solo or aimed at `target`. |
| `!highfive [target]` | A high five, solo or aimed at `target`. |

`target` may start with `@` and must be 1-32 characters of letters, digits, `_`, `.` or `-`. Anything else returns `Usage: !hug [target] -- target must look like a plausible username`.

## Command registry

Phase 1 of slash-command-first routing. Activation records which commands each bundle serves in `app_commands` (migration 0062; hub-api is the only writer). **Phase 1 only writes the registry -- no routing changes yet:** ingest/process still deliver events exactly as before.

**`commands:` manifest field** (optional; absent = infer everything):

```yaml
commands:
  - name: ping                 # '*' or [a-z0-9][a-z0-9_-]{0,31}
    types: [standard, webhook] # default [standard]; '*' is standard-only
    platforms: [discord]       # default: platforms the bundle consumes/supports
    description: Reply pong    # optional, 1-100 chars
```

**Inference** (from `consumes` rules on supported platforms; an explicit entry replaces an inferred row of the same name):

| Rule shape | Registered row |
|---|---|
| `filters.command_prefix: ["!ping"]` | `ping`, standard (one leading non-alphanumeric char stripped) |
| No / empty `command_prefix` | `*` catch-all, standard |
| Unusable prefix (no leading symbol, bad charset) | Skipped, warning logged (count only) |

Same-command rows merge by platform union. Scope: `community_id = 0` means tenant-wide; community and tenant-wide rows share one ownership queue per tenant.

**Ownership = first-registered.** For a contested command the owner is the earliest `(registered_at, id)` row among apps whose platforms overlap (`*` platform overlaps all). `*` command rows never conflict. Re-syncing an active app keeps its `registered_at`; deactivate + reactivate re-queues it behind the interim owner.

**Activation warning contract.** Activation never fails on a registry finding; `POST /community/<id>/activation` (always 201) returns `warnings[]`:

| `code` | When | Fields |
|---|---|---|
| `command_conflict` | Another app already owns `command` | `appId`, `command`, `ownerAppId`, `message` |
| `no_routable_command` | Bundle has a `process` stage but registers no command | `appId`, `message` |

Warnings are logged after commit by code only (no command or user text). Under READ COMMITTED, two simultaneous activations may both miss each other's uncommitted row, so a conflict warning can be lost -- ownership stays single and deterministic. The core-bundle seeder backfills rows for bundles activated before the registry existed, ordered by `activated_at`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Command gets no reply | Its `waddles.command-<name>` flag is off, or the bundle is not activated for the community. | Enable the flag; confirm the bundle is installed, available and activated. |
| Mod verb refused | Platform did not report moderator/broadcaster role (fails closed). | Run it as the broadcaster, or use a platform that supplies role info. |
| `!bot` answers twice | `about` and `bot` both enabled. | Disable one flag. |

## Source

Bundle source: `bundles/python/<name>/`. Catalog: `bundles/core-bundles.yaml`.

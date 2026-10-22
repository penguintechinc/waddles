# Command Bundles Reference

Chat commands that ship as first-party App Bundles. This page covers how they behave and the fun, utility, counter and social commands. Games and gambling are in [Games and Gambling](command-bundles-games.md); event, bookmark, LFG, ticket and label commands are in [Community Tools](command-bundles-community.md).

> **Availability.** `hug`, `boop` and `highfive` are on `release/v3.0.X`. The other 23 bundles on these pages are in the consolidated bundle batch (PR #727) and are available once it merges.

## Shared behavior

| Behavior | Detail |
|---|---|
| Platforms | Twitch and Discord. Each bundle's manifest declares a `chat.message` consume rule for both. None declares `supported_platforms`, so no further platform restriction applies (see [Bundle `supported_platforms`](bundle-supported-platforms.md)). |
| Trigger | Chat prefix `!`, matched case-insensitively on the first word. |
| Gating | **Activation-gated**, not feature-flag-gated. A command runs only once its bundle is installed, enabled for the tenant and activated for the community (install -> enable -> activate). Feature flags are reserved for system bundles. |
| Inactive command | A bundle that is not activated for the community never sees the message. It is indistinguishable from an unrecognized command; no error is sent. |
| Permissions | Every bundle requests `chat.send:<platform>` for its reply. Bundles that keep state also request `storage.kv`. None requests network egress or `flags.read`. |
| State scope | kv state is scoped to tenant, community and bundle. No bundle shares state with another. |
| Mod-only verbs | Checked against the caller's moderator/broadcaster role. If the platform supplies no role information the check fails closed (the verb is refused). |
| Logging | PII-free. Logs carry platform, operation, counts and exception type, never chat text or usernames. |
| App id | `waddles.core.example.<name>`, version `1.0.0`. |

Turn a command on by activating its bundle for the community.

## Fun

Stateless. Each reply is picked at random from a curated list built into the bundle. No external API calls.

| Command | Reply |
|---|---|
| `!catfact` | A random cat fact. Trailing text is ignored. |
| `!dogfact` | A random dog fact. Trailing text is ignored. |
| `!advice` | A random piece of advice. |
| `!fortune` | A random fortune-cookie line. |
| `!waddle` | A random penguin-themed line. |
| `!hello`, `!hi`, `!hey` | A random friendly greeting. |

## Utility

Stateless.

| Command | Reply |
|---|---|
| `!dice [NdM]` | Rolls `N` dice with `M` sides. No argument rolls one six-sided die. `N` is 1-100, `M` is 2-1000. Beyond 20 dice only the sum is shown. A bad spec returns the usage line, for example `!dice 2d6`. |
| `!echo text` | Repeats `text`, up to 300 characters. Refuses text that starts with `/`, `.` or `!` so the bot cannot be made to run commands. No text returns `Usage: !echo <text>`. |
| `!time` | Current UTC time from the host clock, formatted `YYYY-MM-DD HH:MM:SS UTC`. |
| `!bot` | Bot name, version and project links. |

> **Trigger collision.** The existing `about` bundle also answers `!bot`. If both are activated for a community, both reply. Activate only one.

## Counters

Each counter keeps one total per community. All three use the same verbs.

| Verb | Effect | Who |
|---|---|---|
| `!NAME` | Adds 1 and replies with the new total. Any other trailing text (for example `!rip someone`) is treated as a bare add and is not stored. | Anyone |
| `!NAME add N` | Adds `N` (1-1000, default 1). | Anyone |
| `!NAME total` | Shows the total without changing it. Aliases: `get`, `show`, `count`. | Anyone |
| `!NAME reset` | Sets the total to 0. | Moderator or broadcaster |

| Command | Counts |
|---|---|
| `!rip` | Respects paid |
| `!hype` | Hype |
| `!cringe` | Cringe |

For named counters you define yourself, use the `count` bundle instead.

## Voting

`!vote` is a lighter tally than the `poll` bundle.

| Command | Effect | Who |
|---|---|---|
| `!vote` | Shows current standings. | Anyone |
| `!vote start` | Opens a fresh tally. | Moderator or broadcaster |
| `!vote option` | Casts the caller's vote, or moves it if they already voted. Works only while the tally is open. | Anyone |
| `!vote close` | Closes the tally and shows final standings. | Moderator or broadcaster |

Options are single lowercase words of at most 32 characters, up to 20 distinct options per tally. Voters are stored as a truncated hash, one vote each. Two votes landing in the same instant can lose one update.

## Social

Stateless flavor-text replies.

| Command | Reply |
|---|---|
| `!hug [target]` | A wholesome hug, solo or aimed at `target`. |
| `!boop [target]` | A boop, solo or aimed at `target`. |
| `!highfive [target]` | A high five, solo or aimed at `target`. |

`target` may start with `@` and must be 1-32 characters of letters, digits, `_`, `.` or `-`. Anything else returns `Usage: !hug [target] -- target must look like a plausible username`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Command gets no reply | The bundle is not installed, enabled for the tenant or activated for the community. | Confirm all three steps (install, enable, activate). |
| Mod verb refused | Platform did not report moderator/broadcaster role (fails closed). | Run it as the broadcaster, or use a platform that supplies role info. |
| `!bot` answers twice | `about` and `bot` both activated. | Deactivate one. |

## Source

Bundle source: `bundles/python/<name>/`. Catalog: `bundles/core-bundles.yaml` (the seeder's input -- every bundle needs a row; `scripts/ci/tests/test_bundle_wit_world.py` fails on a missing one).

**WIT world.** `bundles/build_python_bundles.py` builds each row against `world stage` unless the row says `wit_world: stage-next`. Economy (`economy.*`, `identity.resolve`) and AI (`ai.generate`) bundles need `stage-next` -- those host imports do not exist in `stage` -- and the build fails if a manifest declares one of them while the row resolves to `stage`.

**Declared bounds.** `economy.wager` / `economy.transfer` must declare `params.max_bet` / `params.max_amount` (integer, 1..=1000), and `limits.timeout_ms` must be at most 10000, or the seeder's manifest parser refuses the bundle. The seeder carries the economy bounds into the bundle's grant, so the declared value is the enforced per-call ceiling.

# counter (Python)

`!counter` -- generic named counters per community (deaths, wins, fails ...). First-party
(`provider: builtin`, `app_id: waddles.core.example.counter`), kv-only, WASI component.
Idea-inspired by the counter features of popular chat bots (concepts only, no code or branding
shared). Distinct from `count`, whose counters are bare `!<name>` commands: this is one `!counter`
namespace, so it never claims arbitrary `!` words and costs 0 kv ops for any other message.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped |
| Catalog | NOT yet in `bundles/core-bundles.yaml` (step-2 registration) |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!counter <name>` | anyone | Shows the counter (`deaths: 3`). |
| `!counter add <name> [n]` | mod/broadcaster | `+n` (default 1); the first `add` creates the counter. |
| `!counter sub <name> [n]` | mod/broadcaster | `-n` (default 1); the counter must exist. |
| `!counter set <name> <n>` | mod/broadcaster | Sets the value; creates the counter if missing. |
| `!counter reset <name>` | mod/broadcaster | Back to 0; the counter must exist. |
| `!counter del <name>` | mod/broadcaster | Removes it (`delete`/`remove` work too). |
| `!counter list` | anyone | Names with values (first 15 + "and N more"). |
| `!counter public` | anyone | Reports whether public add/sub is on. |
| `!counter enable public` / `disable public` | mod/broadcaster | Toggles public add/sub (default OFF). |

Names: `[a-z0-9_-]`, 1-32, first character alphanumeric, not a grammar word (`add`, `sub`, `set`,
`reset`, `remove`, `delete`, `del`, `list`, `enable`, `disable`, `public`, `help`); at most 100 per
community. Amounts: whole numbers, `|n| <= 1e12`. Grammar is `waddle_sdk.command.parse_command`
(`del` is folded onto `delete`); the bare `!counter <name>` read is the one pre-check outside the
shared verb vocabulary (same layering as `!quote <id>`).

```text
!counter add deaths        (mod)    -> deaths: 1
!counter add deaths 5      (mod)    -> deaths: 6
!counter deaths                     -> deaths: 6
!counter enable public     (mod)    -> Public add/sub is now on: anyone can add or subtract 1 ...
!counter add deaths        (viewer) -> deaths: 7        (only +-1, only on an existing counter)
!counter add deaths 50     (viewer) -> Only the broadcaster or a moderator can change a counter by a custom amount.
```

## Permission model

Mutation is broadcaster/moderator only by default: strictly `is_mod is True or is_broadcaster is
True` (a string `"false"` badge is not a moderator), fail closed when neither badge is present
(Discord today). Public mode (`counter.public`) lets any chatter `add`/`sub` exactly 1 on counters
that already exist -- no amount, no creation, no `set`/`reset`/`del` -- so it cannot be used to
vandalise a total or fill the cap. Toggling it is mod-only.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | counter values, name index, creation lock, public flag |
| `chat.send:discord` / `chat.send:twitch` | reply via `relay.push` |

## State

| Key | Value |
|---|---|
| `counter.value.<name>` | integer text; mutated with the atomic `kv.increment`; its existence = "the counter exists" |
| `counter.names` | JSON array of names (drives `list` and the cap) |
| `counter.lock` | 5 s creation/delete claim (`kv.increment` is the only compare-and-swap) |
| `counter.public` | `1` while public add/sub is on |

Colon-free keys (gh-631). Cost: show 1 kv op; `add`/`sub` on an existing counter 2; creating one
~6; `list` 1 + up to 15 (host cap is 64 per invocation).

## Output safety / PII-free

No user-authored text is stored or echoed: replies are static strings, validated counter names and
integers; an invalid name or number is rejected, never echoed. Every reply still goes through
`waddle_sdk.safe_text.sanitize_user_text` (a byte-identical no-op for these strings, the defence
if a future edit echoes something), and cannot start with `/`, `.` or `!`. `event.actor` is never
read: no actor identity is stored or logged; logs carry `op`/`count`/`platform`/`error_type` only
(never a name or amount). A `kv` failure or a corrupt index/value logs `counter.kv_failure` (class
name only) and replies with an explicit error -- nothing is silently reset.

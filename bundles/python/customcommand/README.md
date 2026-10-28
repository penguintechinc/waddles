# customcommand (Python)

`!cc` -- per-community custom text commands. A moderator teaches the bot `!cc add welcome Hi
$(user)!`; afterwards `!welcome` replies with that text, variables substituted. First-party
(`provider: builtin`, `app_id: waddles.core.example.customcommand`), kv-only, WASI component.
Idea-inspired by the custom-command features of popular chat bots (concepts only, no code or
branding shared).

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (response, run counter, name index, edit lock) |
| Catalog | NOT yet in `bundles/core-bundles.yaml` (step-2 registration) |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!cc add <name> <response>` | mod/broadcaster | Creates `!name` (refuses to overwrite). |
| `!cc edit <name> <response>` | mod/broadcaster | Replaces the response; the `$(count)` total is kept. `set` works too. |
| `!cc del <name>` | mod/broadcaster | Removes the command, its counter and its index entry. `delete`/`remove` work too. |
| `!cc list` | anyone | Lists the names (shortened to one chat message). |
| `!cc` / `!cc help` | anyone | Usage and the variable list. |
| `!<name> [args]` | anyone | Runs a stored command. |

`!customcommand` is an alias of `!cc`. Grammar is `waddle_sdk.command.parse_command`; the shared
vocabulary has no `edit`/`del`, so those two are folded onto `set`/`delete` before parsing.

## Variables

Checked when the command is stored -- an unsupported one is rejected, never rendered literally.
At most 5 per response; substituted values are never re-scanned (`!welcome $(count)` prints it
literally).

| Variable | Value |
|---|---|
| `$(user)` | The invoker, as a live mention. |
| `$(target)` / `$(touser)` | The first user mentioned in the invocation; else its first word; else the invoker. |
| `$(count)` | How many times the command has run (atomic `kv.increment`, counted on every run). |
| `$(random <min> <max>)` | Whole number in [min, max] (`|n| <= 1e9`, `min <= max`). |

```text
!cc add cuddle $(user) hugs $(target) -- hug #$(count)!   (mod)
!cuddle @bob                                              -> @actor hugs @bob -- hug #1!
!cc add rolld6 You rolled $(random 1 6)                   (mod)
```

Limits: name `[a-z0-9_-]`, 1-32, first character alphanumeric, not an existing bundle command
(`RESERVED_NAMES`; a test fails when a bundle command is missing from it); response <= 300
characters, no control characters, no `@mention`/`{user:..}` (use `$(user)`/`$(target)`), may not
start with `/`, `.` or `!`; at most 100 commands per community.

## Output safety

The stored response and every substituted value are untrusted. The reply is assembled and
sanitized once, last:

1. each plain piece (literal runs, a plain actor handle, a plain target word) goes through
   `escape_egress_tokens` on its own -- balanced `\{ \} \\` pairs only, so a token a mod stored
   can never fire and a trailing backslash cannot swallow the live mention glued after it;
2. the only live `{user:<tok>}` placeholders are the host-minted actor token (anchored match on
   `event.actor`) and the first mention token of the RAW invocation; exactly those become the
   `allow_tokens` of the final `sanitize_user_text` pass (`@everyone`, `@here`, `<@id>`, `<#id>`,
   `<!here>`, `@name`, controls, cross-placeholder joins);
3. a leading-command guard prefixes a reply whose first visible character is `/`, `.` or `!`
   (a substituted `$(target)` value can produce one). Local copy of the guard that lands as
   `waddle_sdk.safe_text.guard_leading_command` with the AI-bundle batch -- swap on merge.

`dispatch` relays the text unchanged (a second sanitize pass without `allow_tokens` would escape
the live mentions).

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | responses, run counters, name index, edit lock |
| `chat.send:discord` / `chat.send:twitch` | reply via `relay.push` |

No `flags.read` (no flag guard); `random` is the stdlib, no host import.

## State

| Key | Value |
|---|---|
| `cc.names` | JSON array of names (drives `list` and the cap) |
| `cc.cmd.<name>` | the response text |
| `cc.uses.<name>` | run counter (integer text) |
| `cc.lock` | 5 s mutation claim (`kv.increment` is the only compare-and-swap) |

Colon-free keys (gh-631). Cost: running a command 2 kv ops; an unknown `!token` 1 `kv.get`
(`command_prefix` is not an enforced routing filter, so every `!` message reaches `transform`);
no leading `!` 0. Two moderators editing at once: the loser is told to retry.

## PII-free

No actor identity is stored or logged: `event.actor` is used transiently for `$(user)`; keys hold
only mod-chosen command names; logs carry `op`/`count`/`platform`/`error_type` only. The mod gate
is strict `is True` and fails closed (no badge keys, as on Discord today, = denied).

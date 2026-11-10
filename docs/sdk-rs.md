# waddle-sdk-rs: command parser

`waddle_sdk::command` (`sdk/waddle-sdk-rs/src/command.rs`) is the Rust port of the
Python SDK's shared chat-command parser (`waddle_sdk.command`). Same grammar, same
semantics, one golden-vector fixture shared by both test suites.

## Grammar

```
!<command> [sub-module] [verb] [args...]
```

| Verb (fixed vocabulary) | Shape |
|---|---|
| `set` `add` `sub` `remove` `delete` `list` `reset` | `args` = free-text remainder (case preserved); bundle parses it further |
| `enable` / `disable` | exactly one sub-module name (`!lurk enable ai` == `!lurk ai enable`) |
| none (bare) | a read: `option = None` |

Command name, verbs and sub-modules match case-insensitively. Anything else is a
`CommandError::Usage` (a ready-to-send reply) -- never a best-guess partial parse.

```rust
use waddle_sdk::command::{CommandSpec, parse_command};

let spec = CommandSpec::new("lurk", &["ai"]);
let cmd = parse_command("!lurk enable ai", &spec)?;
assert_eq!(cmd.option.as_deref(), Some("enable"));
assert_eq!(cmd.sub_module.as_deref(), Some("ai"));
```

## Flags and quoting (`parse_cli_command`)

Declarative `CliCommandSpec` (positionals, `--opt value`, `--opt=value`, `-x value`,
`-xvalue`, `--` terminator, unique-prefix long flags, optional subcommands with a
default, `--help`). Input is tokenized with POSIX `shlex` rules (`'literal'`,
`"with \" and \\ escapes"`, `\ `). Integer args are `-?[0-9]{1,20}` (no `_`, no `+`)
and land in `CliValue::Int(i128)`.

Declared-schema mistakes (duplicate names, bad flags, unknown default subcommand)
are `CommandError::Schema`, distinct from user-facing `Usage` errors.

## Helpers (Rust-only, additive)

| Fn | Accepts | Notes |
|---|---|---|
| `parse_duration` | `45s` `10m` `2h` `1d` `1w`, compound `1h30m` (descending, each unit once) | returns `std::time::Duration`; zero/overflow/malformed rejected; no clock use (wasip2-safe) |
| `parse_mention` | `<@123>`, `<@!123>`, `@handle` | `Mention::Id` / `Mention::Handle`; handles are PII -- never log them |
| `substitute_placeholders` / `extract_placeholders` | `$(name)` | missing value = `MissingPlaceholderError` (fail loud) |

## Limits

* Command text: `MAX_COMMAND_CHARS` = 4096 characters (Python enforces the same cap).
* Duration/mention tokens: `MAX_TOKEN_CHARS` = 64.

## Parity contract

`sdk/testdata/command-vectors.json` is generated from the Python implementation and
read by `sdk/waddle-sdk/tests/test_command_vectors.py` and
`sdk/waddle-sdk-rs/tests/command_vectors.rs` (both assert a minimum case count so an
empty fixture fails). `parse_command` and placeholder vectors compare exact values and
error messages. For `parse_cli_command`, values/subcommand/help-presence are compared;
error and help *wording* is not (Python's comes from argparse).

Known intentional gaps vs Python: CLI usage-error and help text wording.

## wasm32-wasip2

Pure std, no `SystemTime`, no host imports: builds with
`cargo build --target wasm32-wasip2` and is host-testable with `cargo test`.

## Follow-up adopters (not in this change)

Bundles with local parsers to migrate onto this module: calendar (#1114),
twitch-clips (#1113).

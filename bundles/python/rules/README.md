# rules (Python)

`!rules` -- show and manage a per-community rules text. `kv`-only, first-party
(`provider: builtin`, `app_id: waddles.core.example.rules`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** (not a port; no third-party attribution applies).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!rules` | anyone | Replies with the saved rules text, or `No rules have been set for this community yet.` Pure read. |
| `!rules set <text>` | broadcaster / mod | Replaces the rules (trimmed, 1-**1000** chars). Replies `Rules have been updated.` |
| `!rules clear` | broadcaster / mod | Deletes the rules; bare `!rules` reverts to the "no rules" message. Idempotent. |

`!rules remove` is an **equivalent alias** for `!rules clear` (`clear` is not in the shared
`waddle_sdk.command.VERBS` vocabulary, so a leading `clear` token is normalized onto the grammar's
own `remove` verb before parsing; both are case-insensitive). Trailing words
(`!rules clear now`), unsupported verbs (`list`, `enable`, ...) and malformed input reply with the
usage line -- never silently dropped, never a best-guess partial match. `!rules set` with no text
replies `Usage: !rules set <text>`.

```text
mod>    !rules set 1. Be kind. 2. No spam.
bot>    Rules have been updated.
viewer> !rules
bot>    1. Be kind. 2. No spam.
viewer> !rules clear
bot>    only moderators/broadcasters can manage the rules
```

## State (`kv`, community-scoped)

One durable key (TTL `0`, never expires), through `waddle_sdk.community_kv`:

| Key | Scope | Purpose |
|---|---|---|
| `rules.text` | per-community (`c.<community_id>.rules.text`) | The rules, UTF-8. |

`.`-separated, never `:` (gh-631 -- the real `kv` host rejects `:`). A **missing community is not
an error**: `None` is the host's tenant-wide sentinel (scoped under `c.0.`), alpha's only
activation shape today, so refusing it would make the bundle nonfunctional there.

### Failure semantics (fail loud)

| Condition | Behavior |
|---|---|
| `kv` backend error (`show`/`set`/`clear`) | ERROR `rules.kv_error` (`op` + reason), chat reply "rules are temporarily unavailable", then `RuntimeError("rules <op> failed: ...")`. |
| Stored value is not valid UTF-8 | `UnicodeDecodeError` propagates; nothing is relayed (no default is substituted for corrupt state). |
| Missing `channel_id` / unknown command | `ValueError`. |

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists the per-community rules text. |
| `flags.read` | Gates the command behind its `waddles.command-rules` feature flag. |

No `db`, no egress. Mod gate: `set`/`clear` require a real `is_mod` or `is_broadcaster` `True`;
**absent** badge fields (e.g. the Discord normalizer today) are **denied** (fail closed) with zero
`kv` access, and the denial log carries only `command` + the role signal -- never the attempted text.

## Feature flag

`waddles.command-rules`, **default OFF**, checked after the cheap `!rules` match and before grammar
resolution, requested with `default=False` (flag outage / missing `wit_world` -> off). While off
nothing is parsed, stored or logged.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!rules"]`; replies go
back to the event's own origin platform + channel.

## Logging / PII

Logs carry only `command`, `op`, `community`, platform and role signal -- never the rules text or
the user's typed arguments. Regression:
`tests/test_backfill.py::test_no_log_line_in_any_flow_contains_rules_text_or_user_input`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest + hub-api install-pipeline manifest |
| `src/app.py` | `transform` (recognize/resolve) / `dispatch` (permission, `kv` I/O, relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (shared charset-enforcing `kv` fake) |

## Test

```bash
cd bundles/python/rules
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```

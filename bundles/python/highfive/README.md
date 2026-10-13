# highfive (Python)

`!highfive [target]` -- a lighthearted reply that gives people high fives. First-party, net-new
Waddles flavor content (`provider: builtin`, `app_id: waddles.core.example.highfive`); no
third-party source, so no attribution to carry. Structural twin of `bundles/python/hug` and
`bundles/python/wave`, modeled on `bundles/python/slap`/`eightball`.

## Commands

| Command | Behavior |
|---|---|
| `!highfive` | Solo reply -- one of 4 fixed flavor lines. |
| `!highfive <target>` | Targeted reply -- one of 4 fixed templates with `<target>` substituted. A leading `@` is stripped. |
| `!highfive <bad-shape>` / `!highfive <two> <words>` | Usage reply: `Usage: !highfive [target] -- target must look like a plausible username`. Never a silent drop. |

`<target>` is shape-checked only (`_TARGET_RE`: optional `@`, then 1-32 chars of letters, digits,
`_`, `.`, `-`, first char not `.`/`-`) -- not a lookup against a community roster. The shape check
also keeps `{`, `}` and `%` out of the `str.format()` template, so a target can never inject a
format string. Matching is case-insensitive and whitespace-tolerant (`  !HighFive  `).

Whole-command logic lives in `transform()` (validate + random pick); `dispatch()` is a pure relay
of the text `transform()` produced. No shared-grammar `CommandSpec` use -- the only argument is a
free-text target with no verb shape (same documented deviation as `eightball`/`wave`).

## Examples

```text
> !highfive
Raises a hand for a high five, then high-fives the air. Solid form.
> !highfive @alice
SMACK! alice and the chat share a perfect high five.
> !highfive !!!
Usage: !highfive [target] -- target must look like a plausible username
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `flags.read` | Reads the `waddles.command-highfive` flag that gates the command. |

Stateless: no `storage.kv`, no `db`, no egress (`egress: []`, `data.tables: []`). A test fails if
either `kv` or `db` host import is ever touched.

## Feature flag

`waddles.command-highfive`, default **OFF** (`critical-rules.md` Feature Flags & License Tiers).
Order in `transform()`: cheap `!highfive` head match -> flag check -> classification, so
unrelated chat never costs a flag lookup. Flag off, or a host with no `flags` import, => no reply
(fails closed to the OFF default, never crashes).

## Platforms

Twitch and Discord `chat.message` events whose text starts with `!highfive`
(`stages.process.consumes`); the reply relays to the originating platform/channel. A
`dispatch()` with no `channel_id` raises `ValueError`; a failing `relay.push` propagates (never
swallowed).

## Logging / PII

Every log line carries exactly `{platform, shape}` (`shape` in `solo`/`targeted`/`usage`) --
**never** the raw message, `event.actor`, or the typed target (regression: gh-674; the suite
drives every branch with a sentinel string and asserts the exact field set). The reply text may
echo the typed target back into the same public channel it came from; logs never do.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, V2 permissions |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer) |
| `src/app.py` | `transform` / `dispatch` |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) |

## Test

```bash
cd bundles/python/highfive
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==6.0.0 mypy==1.14.1 ruff==0.14.1
# tests/conftest.py puts this bundle's src/ and the SDK's src/ on sys.path directly.
pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
```

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.highfive`, activation target
`global`); dark until `waddles.command-highfive` is turned on.

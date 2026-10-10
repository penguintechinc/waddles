# Bundle Authoring: Command Grammar & Data Conventions

This doc covers the standard chat-command grammar every bundle parses with, plus the
cross-cutting data/PII/flag rules that apply to any bundle handling a `!command`. For the
pipeline/entrypoint contract itself (`transform`/`dispatch` signatures, `PlatformEvent`/
`StageEnvelope`, DB access, registration) see `docs/APP_BUNDLE_AUTHORING.md` — this doc is
narrower and specific to `waddle_sdk.command`/`waddle_sdk.sub_modules`/`waddle_sdk.community_kv`.

## 1. Command Grammar

```
!<command> [sub-module] [option] <input>
```

Every bundle command is parsed by `waddle_sdk.command.parse_command()` against a
`CommandSpec` the bundle declares once:

```python
from waddle_sdk.command import CommandSpec, CommandUsageError, parse_command

SPEC = CommandSpec(name="lurk", sub_modules=frozenset({"ai"}))

async def transform(event: PlatformEvent) -> PlatformEvent | None:
    text = event.payload.get("text")
    if not isinstance(text, str) or not text.startswith("!lurk"):
        return None
    try:
        parsed = parse_command(text, SPEC)
    except CommandUsageError as exc:
        return _reply(event, str(exc))  # usage/error text, sent straight back
    ...
```

`parse_command()` returns a `ParsedCommand(command, sub_module, option, args)` or raises
`CommandUsageError` — never a best-guess partial parse. `str(exc)` is a ready-to-send reply.

### Verb vocabulary

| Verb | Meaning | Argument shape |
|---|---|---|
| `set` | assign a config value | free-text `args` (bundle parses further, e.g. `key value`) |
| `add` / `sub` | increment/decrement a value | free-text `args`, or none |
| `enable` / `disable` | toggle a sub-module | **exactly one** sub-module name, no other `args` |
| `remove` / `delete` | remove something | free-text `args`, or none |
| `list` | list state | usually no `args` |
| `reset` | reset to default | usually no `args` |
| *(bare)* | no verb at all | bundle's own default behavior (`!count` increments; could instead be a usage reply) |

### Sub-modules

A bundle declares named sub-modules in its `CommandSpec` (e.g. `!shoutout`'s `auto`/`ai`).
The parser accepts either order for routing to one:

```
!cmd <sub-module> <option> <input>      # !shoutout auto set 30
!cmd enable|disable <sub-module>        # !lurk enable ai  (equivalent: !lurk ai enable)
```

**Every declared sub-module starts disabled for every community.** This mirrors the
platform-wide features-default-OFF rule one level down — a sub-module is not live until an
admin runs `!<cmd> enable <sub-module>`. `waddle_sdk.sub_modules.SubModuleGate` is the
sanctioned way to check/toggle that state; never hand-roll a second kv convention for it:

```python
from waddle_sdk.sub_modules import SubModuleGate

_AI_GATE = SubModuleGate(command="lurk")

async def dispatch(envelope: StageEnvelope, config: dict, *, http_client) -> DispatchResult:
    parsed = parse_command(envelope.event.payload["text"], SPEC)
    toggled = await _AI_GATE.apply_toggle(envelope.community, parsed)
    if toggled is not None:
        return _reply(f"ai sub-module {'enabled' if toggled else 'disabled'}")

    if not await _AI_GATE.is_enabled(envelope.community, "ai"):
        return _reply("ai sub-module is disabled — enable with `!lurk enable ai`")
    ...  # ai-specific behavior
```

`apply_toggle()` returns `True`/`False` for an enable/disable `ParsedCommand`, or `None` for
anything else — a bundle calls it unconditionally first and falls through to its own
dispatch when the result is `None`.

### Placeholder substitution

Reply templates use `$(name)` placeholders (`$(username)`, `$(channel)`, ...):

```python
from waddle_sdk.command import extract_placeholders, substitute_placeholders

extract_placeholders("hi $(username), welcome to $(channel)!")  # -> ["username", "channel"]
substitute_placeholders(template, {"username": "penguin", "channel": "general"})
```

`substitute_placeholders()` raises `MissingPlaceholderError` for any placeholder absent from
`values` — never renders an empty string or leaves `$(name)` literally in the output (see
§4 fail-loud rule).

## 2. Per-Community Data Scoping

**All bundle-tracked state is keyed by `community_id` only — never global, never tenant.**
`waddle_sdk.community_kv` enforces this at the storage layer:

```python
from waddle_sdk import community_kv

total = await community_kv.increment(envelope.community, "count.<pseudonym>", 1)
```

Every call takes `community_id` as its first argument and raises `ValueError` if it's
falsy — a missing `community_id` is a bundle bug to surface immediately, never a reason to
silently fall back to a global/tenant-wide key.

**`key` charset: `[A-Za-z0-9_.-]` only, never `:` (gh-631).** The real `kv` host capability
(`core/bundle_host_kv/src/scope.rs::is_allowed_key_byte`) reserves `:` as its own namespace
separator and rejects any guest key containing one; `waddle_sdk.kv.validate_key()` enforces
this before every host call (including the key `community_kv` builds internally), raising
`InvalidKvKeyError` with the exact offending characters instead of a generic host error.
Use `.` to namespace your own key, e.g. `"count.registry"` / `"count.value.{name}"`, never
`"count:registry"`.

**Reputation and user-details are the only
two cross-community exceptions in the platform** (`reputation_tenant` -- cross-community but
hard-bounded to ONE tenant, never cross-tenant, see security.md Tenant Isolation -- and the
hub `users` table) — neither goes through `community_kv`; they have their own dedicated,
explicitly cross-community storage. A new bundle never introduces a third exception without
updating this doc first.

## 3. Feature Flags & License Gating

Every genuinely new command (not a like-for-like port of an already-shipped v2 feature) is
wrapped in `waddle_sdk.flask_core.feature_flags.feature_enabled()`, default OFF:

```python
from waddle_sdk.flask_core.feature_flags import feature_enabled, tier_at_least

if not await feature_enabled("waddles.command-lurk", default=False):
    return None
```

`feature_enabled()` IS the env/PostHog baseline gate — it fails open to the supplied
`default` on a flag-server outage or a stale binding, so a bundle never crashes on a
flag-server hiccup; it never needs its own separate env-var check. The one exception is an
**Enterprise-tier sub-feature**, which additionally gates on the tenant's license tier via
`tier_at_least()`:

```python
if not await tier_at_least("enterprise"):
    return _reply("this feature requires an Enterprise license")
```

Both gates are independent and additive — a PostHog flag never substitutes for a license
check, and vice versa (`critical-rules.md` Feature Flags & License Tiers).

## 4. PII & Fail-Loud Rules

- **No raw usernames in kv keys or logs.** Hash `(community, actor)` into a non-reversible
  pseudonym before it ever reaches `community_kv`/`log` — see `bundles/python/lurk/src/
  app.py::_kv_key()` for the canonical pattern. This applies even while the tokenization
  pipeline isn't fully merged and `event.actor` may still be a raw username.
- **Fail loud, never silently fall back.** `CommandUsageError`, `MissingPlaceholderError`,
  and `community_kv`'s `ValueError` on a missing `community_id` all exist so a bundle
  bug surfaces as a visible reply or a raised exception — never a swallowed no-op, never a
  default value standing in for "something went wrong."
- **No stubs.** A declared sub-module with no real implementation is not shipped — either
  implement it or don't declare it in `CommandSpec.sub_modules` yet.
- **Role badges are strict booleans.** Read `is_mod`/`is_broadcaster` with an identity check
  (`payload.get("is_mod") is True`), and forward them from `transform()` the same way
  (`payload["is_mod"] = event.payload["is_mod"] is True`). Never `bool(is_mod)` or a bare
  truthiness test: the string `"false"` is truthy, so a non-moderator whose normalizer emitted a
  string badge passed the mod gate, and `transform()` laundered it into a real `True` for
  `dispatch` (40 bundles, fixed in fix/bundle-defects-wave). Absent badge fields mean "unknown"
  and must be denied. `scripts/ci/check-bundle-source-hygiene.py --check badge-truthiness`
  (CI: `bundle-hygiene`) fails the build on the pattern, and
  `scripts/ci/tests/test_bundle_badge_gate.py` proves every bundle's gate behaviourally.
- **One table per bundle means `kv` carries the rest.** The structured `db` interface gives a
  bundle exactly one app-owned table with no column-equality filter; model anything relational
  as one row-per-entity table plus `community_kv` for the id counter, the `id -> row_id` index
  and atomic counters. `kv.increment` is the only compare-and-swap you have (it returns `1` to
  the single first caller): use it as a short-TTL claim/lock around any create-or-update that two
  invocations could race (see `bundles/python/poll`, `loyalty`, `inventory`), and compensate a
  failed second step of a two-step write before raising. Host limits to design within: 64 `kv`
  and 64 `db` ops per invocation, 8 KiB per text column, 10,000 `kv` keys per community.

## 5. Worked Examples

| Command | Grammar exercised |
|---|---|
| `!count` | bare command — default behavior (increment + reply) |
| `!lurk` / `!unlurk` | two aliases of a toggle-shaped command (hand-rolled today — a `parse_command`-based rewrite is a separate follow-up, not required by this doc) |
| `!sr set youtube-labels Gaming,Music` | `set` verb, free-text `args` the bundle parses further |
| `!shoutout enable auto` | `enable` toggle — sub-module `auto` |
| `!shoutout auto set 30` | sub-module routing — `auto` then `set` with `args="30"` |

See `sdk/waddle-sdk/tests/test_command.py` and `test_sub_modules.py` for the full parsed-
shape matrix, including every rejection case (`CommandUsageError`).

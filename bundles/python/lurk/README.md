# lurk (Python)

`!lurk` / `!unlurk` -- a timed, per-viewer lurk with a templated confirmation and an elapsed-time
welcome-back, plus mod-only per-community configuration. First-party (`provider: builtin`,
`app_id: waddles.core.example.lurk`), net-new Waddles content -- no third-party source, so no
attribution to carry. kv-only (`storage.kv`); no `db`, no egress.

`transform` only recognizes the command and forwards the normalized badge signal (no kv access);
`dispatch` does every `kv` read/write and the relay reply -- the same process/action split as
`pyping`.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!lurk` | anyone | Starts (or **resets**, if already lurking) the caller's lurk timer and replies with the community's lurk template (default `$(username) is now lurking`). |
| `!unlurk` | anyone | If lurking: replies `Welcome back, <name>! You lurked for 2h 13m.` (two largest units) and clears the state. If not (never lurked, already unlurked, or the 24 h TTL expired it): `You weren't lurking!` -- one code path for all three. |
| `!lurk set <message>` | **mod / broadcaster** | Sets the community's lurk template (1-200 chars). Placeholders: `$(username)`, `$(duration)`; any other `$(...)` is rejected at set time (no silent typo). |
| `!lurk enable ai` | **mod / broadcaster** + **Enterprise** | Turns on the AI-response toggle. Replies "AI lurk responses require Enterprise" below Enterprise. |
| `!lurk disable ai` | **mod / broadcaster** | Turns the toggle off (no license check -- turning a thing off is always allowed). |
| `!lurk reset` | **mod / broadcaster** | Restores the default template and clears the AI toggle. |

Anything else (`!lurk bogus`, `!lurk enable`, `!lurk setx hi`) replies with usage text. Matching is
case-insensitive and whitespace-tolerant.

## Moderator gate (fail-closed)

Config commands read `is_mod` / `is_broadcaster` off the normalized event
(`core/svc_ingest/src/normalize.rs::normalize_twitch_irc`) via `_caller_role_signal()`. The check
runs in `dispatch` **before any kv or license access**:

| Event badge fields | Result |
|---|---|
| neither present (e.g. Discord's normalizer today) | **denied** -- absence is never an implicit allow |
| present but falsy | denied |
| either/both true | allowed |

Denial logs `lurk.config_denied` (command + role-signal state only). `!lurk`/`!unlurk` never need a role.

## License gate (fail-closed)

`!lurk enable ai` reads the always-granted WIT `flags.tier()` import and proceeds only for
`enterprise`. A missing `wit_world`, a stale component with no `flags` import, or no `tier`
function all resolve to `free` -- never an implicit allow, never env-overridable
(`critical-rules.md` Feature Flags & License Tiers). A non-mod on an Enterprise tenant is still
denied by the mod gate first.

## Examples

```text
> !lurk
viewer is now lurking
> !unlurk                                          (2 h 13 m later)
Welcome back, viewer! You lurked for 2h 13m.
> !lurk set $(username) tiptoes into the shadows   (mod)
lurk message updated: $(username) tiptoes into the shadows
> !lurk set hi $(nope)                             (mod)
invalid lurk message: unknown placeholder(s): nope (allowed: $(username), $(duration))
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | Persists per-community lurk state (start timestamps, template, AI toggle). Without it `_derive_capabilities()` never grants `kv` at all. |
| `flags.read` | Reads the `waddles.command-lurk` feature flag (and the license tier for the AI toggle). |

## Feature flag

`waddles.command-lurk`, default **OFF** (`critical-rules.md` Feature Flags & License Tiers),
checked in `transform()` after the cheap command-name match. Flag off => no reply.

## Platforms

Twitch and Discord `chat.message` events with `!lurk`/`!unlurk` (`stages.process.consumes`).
Discord events carry no mod/broadcaster badge today, so config commands are denied there until its
normalizer supplies the fields.

## State (kv)

| Key | Value | TTL |
|---|---|---|
| `lurk.state.<community>.<sha256(actor)>` | lurk-start epoch millis | **24 h** (the TTL *is* the auto-expiry; no sweep job) |
| `lurk.config.<community>.message` | custom template (UTF-8) | none |
| `lurk.config.<community>.ai_enabled` | presence = enabled | none |

- **Scope.** `envelope.community is None` is the host's deliberate tenant-wide sentinel (alpha
  activates this bundle with `community_id: null`) and is scoped under the literal `"0"`
  (`waddle_sdk.community_kv.TENANT_WIDE_SENTINEL`) -- so `!lurk` actually works on alpha (gh-655).
  Only an **empty-string** community is a caller-side bug and fails loud (ERROR log + chat reply +
  `ValueError`).
- **Keys** use `.` as the separator, never `:` (gh-631: the host rejects `:`). A community id that
  would build a host-rejected key (e.g. contains `:`) fails loud rather than silently no-op'ing.
- **PII.** The actor is SHA-256-hashed into a pseudonym before it reaches `kv`; the raw name is
  used only in the visible chat reply.

## Failure behavior (fail-loud, never silent)

| Condition | Behavior |
|---|---|
| `kv` get/set/delete raises | ERROR `lurk.kv_error` (`op` + WIT error **case name**), chat reply "lurk is temporarily unavailable, try again shortly.", then `RuntimeError`. Exactly one relay -- never a success line. A failed `lurk` write stores nothing. |
| Corrupt template (non-UTF-8) | ERROR `lurk.template_corrupt`; replies with the **default** template. |
| Corrupt lurk state (not an integer) | ERROR `lurk.state_corrupt`; the bad key is deleted; replies `You weren't lurking!`. A failing delete then fails loud. |
| Start timestamp in the future (clock skew) | Elapsed clamps to `0s`. |
| Missing `channel_id`, empty community, unknown command | `ValueError`. |

**Known v1 deferrals (tracked, not stubbed):** the AI-generated response itself
([#610](https://github.com/penguintechinc/waddles/issues/610)) -- `ai_enabled` is a real,
license-checked toggle but `!lurk` always falls back to the template, announced at DEBUG
(`lurk.ai_requested_but_pending`); and `stream.offline` cancelling an in-progress lurk
([#611](https://github.com/penguintechinc/waddles/issues/611)) -- needs that event added to the
manifest's `consumes`, until then a lurk rides out its 24 h TTL.

## Logging / PII

Every log message has a strict field allowlist (command, platform, community id, tier, op/error
case) -- **never** the raw message, `!lurk set` text, or `event.actor` (regression: gh-674; the
suite drives every command, the denial/license paths and the failure paths with a sentinel string
and asserts both its absence and the exact per-message field set).

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, V2 permissions, version history |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer) |
| `src/app.py` | `transform` / `dispatch` |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring |
| `tests/` | Host-native pytest suite (fake `wit_world` incl. `kv`/`clock`/`flags.tier`; no wasmtime) |

## Test

```bash
cd bundles/python/lurk
python3 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==6.0.0 mypy==1.14.1 ruff==0.14.1
# tests/conftest.py puts this bundle's src/ and the SDK's src/ on sys.path directly.
pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=90
```

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.lurk`, activation target
`global` / `community_id: null`); dark until `waddles.command-lurk` is turned on.

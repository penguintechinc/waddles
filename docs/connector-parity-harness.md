# Connector-overlay golden-payload parity harness

Proves a P6b platform overlay (Twitch / Discord / YouTube; Rust WASM component, `world = "connector-tools"`)
normalizes every corpus payload to the SAME events the hardcoded path produced. Reuses the existing
`tests/data_plane_parity` corpus; it does not replace the Rust-oracle/Python gate.

```
corpus/<platform>/*.json  (frame + golden = hardcoded-normalizer output; kept current by Data-Plane Parity CI)
   | scripts/ci/gen-connector-parity-expected.py   (reshape only; CI --check)
   v
tests/data_plane_parity/expected/<platform>/*.json  (input bytes + expected normalized-event list)
   | core/bundle_executor/tests/connector_parity.rs  (production ConnectorHost::load + receiver.on-frame
   v                                                   + real identity bridge, scripted stage)
field-level diff report + `connector_parity: ... examined=N passed=N failed=0`
```

## Run it

```bash
# real gate (build your overlay first)
PARITY_OVERLAY_WASM=bundles/rust/twitch/target/wasm32-wasip2/release/twitch_overlay.wasm \
  make test-connector-parity PLATFORM=twitch

# no overlay yet -> harness self-check against the fixture component (loudly bannered, NOT overlay parity)
make test-connector-parity PLATFORM=discord
```

| Env | Meaning |
|---|---|
| `PLATFORM` | `twitch`, `discord` or `youtube` (required) |
| `PARITY_OVERLAY_WASM` | built overlay component. Set-but-missing = FAIL, never skip. Unset = fixture self-check |
| `PARITY_OVERLAY_DIGEST` | optional `sha256:<hex>` pin; mismatch fails |
| `PARITY_OVERLAY_APP_ID` | default `waddles.core.parity.<platform>` (must be `waddles.core.*`) |
| `PARITY_OVERLAY_WORLD` | `connector-tools` (default) or `connector` |

**Use a per-agent `CARGO_TARGET_DIR`** (e.g. `/mnt/ssd/cargo-target-<agent>`): a shared target dir lets another
checkout's build overwrite the cached test artifact. The harness never trusts compiled-in paths — it locates
the repo root at runtime (`CONNECTOR_PARITY_ROOT`, set by the make target, else walk up from cwd) and enforces
`examined == expected vectors == corpus files` (all printed, any mismatch or zero fails).

The target fails if the expected files are stale, if zero vectors were examined, or if any vector differs.
Exact failing command in CI form: `cd core/bundle_executor && PARITY_PLATFORM=twitch PARITY_OVERLAY_WASM=<abs path> cargo test --locked --test connector_parity overlay_parity -- --ignored --nocapture`.

## Contract the overlay is tested against

* **Input**: each vector's `input.text` bytes go to `receiver.on-frame` as `raw-frame.bytes` (verified webhook
  bodies and WS/poll frames alike). Twitch IRC = raw IRC line; EventSub = notification body JSON; Discord =
  gateway dispatch `{"op":0,"t":"MESSAGE_CREATE","s":1,"d":<message>}`; YouTube = receiver-raw poll item JSON.
  The encoding lives in ONE place (`encode_input` in the generator); change it there and regenerate.
* **`ctx`**: `platform` = vector platform, `source-id` = `parity-source`. Vector `context` (self nick, guild roles,
  broadcaster id...) is passed as **JSON in `session-token`** — a harness hint only. If your overlay needs that
  data some other way, declare a divergence (below) rather than inventing a channel.
* **Output**: `list<normalized-event>` — `platform`, `event-type`, `payload-json` (parsed, compared by value with
  field-level diffs), `occurred-at` (RFC 3339 shape only). Source/account/channel are host-stamped, not compared.
  `outcome: dropped` (self message, unknown EventSub type) = `Ok([])`.
* **Actor is tokenized**: expected `actor` = pseudonym UUID the harness identity stub returns for
  `(platform, platform-user-id)` (`bundle_executor::parity_support::pseudonym_uuid`). The overlay must call
  `identity.lookup(platform-identity(platform, id))` (id = golden `user_id`/`author_id`, or the login when the wire
  carries no id, e.g. untagged IRC — see `actor.platform_user_id` in the expected file). Returning the raw
  handle is reported as `RAW HANDLE LEAKED`. The harness links `identity` (grants `connector.pii.read`) and
  serves lookups over the real host-call path; the report prints `identity_lookups=N`.
* Other host imports (`http`, `tool`, ...) are not scripted: calls fail typed. Parity is about `on-frame`.

## Declared divergences

Intended differences from the legacy path go in `tests/data_plane_parity/expected/<platform>/divergences.json`
(hand-written, reviewed):

```json
{ "discord/guild_text_message": [
    {"path": "events[0].payload.is_mod", "reason": "role flags resolved host-side", "tracking": "#123"} ] }
```

A declared path must actually differ (a stale declaration FAILS) and every other difference still fails. Paths are
those in the report (`events[0].payload.a.b`, `events.len`, `outcome`, `events[0].actor`).

## Overlay-author workflow

1. `make test-connector-parity PLATFORM=<p>` with no wasm: confirms corpus count + harness works.
2. Build your overlay, rerun with `PARITY_OVERLAY_WASM=...`. Read the report:
   `FAIL <vector id>` then `    <path>: expected X, got Y`; last line is the denominator.
3. Fix the overlay until `failed=0`. Anything you intend to keep different → `divergences.json` + tracking issue.
4. Never edit `expected/*.json` by hand (CI `--check` fails): fix the corpus golden via the existing
   `PARITY_BLESS` flow (`tests/data_plane_parity/README.md`) then `make gen-connector-parity-expected`.
5. Add the exact command to your PR; the deletion PR (P6b Task 4) is gated on `failed=0` for all three.

## Files

| File | Role |
|---|---|
| `scripts/ci/gen-connector-parity-expected.py` | corpus golden -> `expected/` (`--check` in the Data-Plane Parity job) |
| `scripts/ci/connector-parity.sh` | make-target driver; asserts the denominator line |
| `core/bundle_executor/tests/connector_parity.rs` | loader, runner, diff, report; self-tests incl. mutation |
| `core/bundle_executor/src/parity_support.rs` | scripted identity bridge + pseudonym (crate-private `Connection` needs an in-crate seam) |
| `core/bundle_executor/tests/fixtures/parity/fixture/` | hand-written vectors for the fixture component (self-check) |

PII: fixtures are synthetic; the report prints only field paths and fixture values; the harness never logs raw frames.

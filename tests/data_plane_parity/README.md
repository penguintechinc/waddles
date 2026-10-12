# Data-plane golden-vector parity harness (gh #840 PR4)

Proves the Rust/bundle ingest path matches today's Python **before** anything is removed.
Tests/tooling only -- no data-plane behaviour change. Run: `make test-data-plane-parity`.

```
corpus/<platform>/<name>.json  (raw platform frame + golden PlatformEvent)
        |
        +--> core/svc_ingest/tests/parity_golden.rs   real connector frame parser + normalize.rs  == golden   (Rust oracle)
        +--> tests/data_plane_parity/test_golden_parity.py   real Python receiver + normalize()   == golden - declared divergences
        +--> core/svc_process/tests/parity_pii_tokenize.rs   every event through the pii_tokenize closed-schema classifier
```

Python-vs-Rust is proven transitively through the one reviewable golden. Nothing on the normalize
path is mocked: Twitch IRC runs over a real local TCP IRC server into the real `TwitchIrcReceiver`;
EventSub runs the real `TwitchEventSubHandler.handle_webhook` (HMAC + type filter + `build_raw_event`);
Discord drives the real `_is_self`/`_build_raw_event` over a duck-typed `discord.Message` (py-cord's
`Message` needs live HTTP state -- same precedent as `core/svc_ingest/tests/test_discord_gateway.py`).
The only test double is the `IdentityMinter` seam (hub-api's gRPC) in the PII gate.

## Corpus slots

| Platform | Path(s) | Dual-path (Rust oracle)? |
|---|---|---|
| `twitch` | `twitch_irc`, `twitch_eventsub` | yes |
| `discord` | `discord_gateway` | yes |
| `slack` `kick` `youtube` `spectrum` | `slack_socket` `kick_pusher` `youtube_live_poll` `spectrum_poll`/`spectrum_org` | **stub slots** -- Python-only; `rust_path: null`, golden = frozen Python output until the Rust/bundle side lands |

## Vector schema

`id` (`<platform>/<file stem>`), `platform`, `path`, `seed` (where it came from), `frame` (raw IRC line /
Discord `MESSAGE_CREATE` `d` / EventSub notification body / receiver-raw dict for Python-only slots),
`context` (self nick / self user id / guild roles ...), `outcome` (`event`|`dropped`), `golden`
(`platform,event_type,actor,payload,source` -- no `occurred_at`, an arrival timestamp checked for shape only),
`golden_origin` (`rust`|`python`), `python_divergences` (`path`, `python` value, `reason`, `tracking`),
`pii_classifier` (`pass` | `reject:<sorted,comma,fields>`).

* **Divergences must match exactly** -- an undeclared difference *or* a declared one that no longer
  occurs fails. Global (not repeated per vector): Python `PlatformEvent` has no `source`.
* **PII gate**: `pass` = every field classified AND every identity field tokenized; `reject:` lists the
  exact fields the classifier would dead-letter today (Python-only platforms) -- porting a platform
  must classify them (IDENTITY in `ID_NAME_FIELD_PAIRS` or SAFE in `SAFE_PAYLOAD_FIELDS`) and flip it to `pass`.

## Adding / re-blessing a vector

1. Drop a `corpus/<platform>/<name>.json` with `"golden": null`.
2. `PARITY_BLESS=1 cargo test --test parity_golden` in `core/svc_ingest` (dual-path) or
   `PARITY_BLESS=1 python3 -m pytest tests/data_plane_parity` (Python-only); **review the diff** -- a golden
   change is a behaviour change. The gate refuses to run with `PARITY_BLESS=1`.
3. Run the harness; declare any Python-vs-Rust difference you intend to keep, with a tracking issue.

Companion gate: `scripts/ci/check-builtin-handler-inventory.py` (`.ci/builtin-handler-map.yaml`) fails if a
live Python `builtin_handlers/*` module has no Rust-path/bundle mapping, or a still-pending one is deleted.

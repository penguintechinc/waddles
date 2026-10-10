# ping (Rust)

`!ping` -> `pong` -- the minimal, real **Rust** app bundle and the original deployable fixture for
the end-to-end bundle pipeline. First-party (`provider: builtin`,
`app_id: waddles.core.example.ping`, `author: PenguinTech/waddles`, Apache-2.0), **original Waddles
content** -- no third-party attribution applies. Its siblings are `bundles/python/pyping`
(`!pyping` -> `pong (py)`) and `bundles/csharp/csping` (`!csping`); together they are the
"big three" language check (a reply on each language's own command).

It is deliberately tiny: it gives the executor a small, correct, real WASI 0.2 component to load
and drive through svc-process / svc-action. See `bundles/rust/example` for SDK surface area.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!ping` | anyone | Replies `pong` on the event's own origin platform + channel. |

Matching is **exact** on the trimmed `text` field: `!ping` and `  !ping  ` match; `!PING`,
`!ping extra`, `!pingpong`, `! ping` do not (no case folding, no arguments, no prefix match). There
is no verb grammar, no usage reply and nothing to mis-parse; a payload that is not a
`chat.message`-shaped object with a string `text` is dropped silently rather than erroring the
pipeline over an event the bundle was never meant to handle.

```text
viewer> !ping
bot>    pong
```

## Stages

| Stage | Entry | What it does |
|---|---|---|
| `process` | `waddle:bundle/process-stage#transform` | `!ping` -> a reply `PlatformEvent` (`pong`, same platform / channel / actor / timestamp / event type). Anything else -> `None`. |
| `action` | `waddle:bundle/action-stage#dispatch` | Relays `{"channel", "text"}` over the `relay` host import to **`envelope.event.platform`** (never a hardcoded provider -- regression-tested for `twitch` and `discord`). A missing `channel_id` -> fatal `MISSING_CHANNEL`; a non-pong payload -> fatal `BAD_PAYLOAD`; a relay failure -> retryable `RELAY_PUSH_FAILED`. |

`dispatch` and the `export_stage!` wiring are `#[cfg(target_arch = "wasm32")]`-gated (the relay is a
wasm32-only host call), so on the host `cargo test` exercises `transform` and `build_relay` --
the pure, target-independent halves -- without a WASI runner.

## Permissions (V2 structured)

`permissions: []` in both `bundle.yaml` and `hub-manifest.yaml` -- **none requested**:

| Capability | Why not needed |
|---|---|
| `flags.read` | Deliberately **not feature-flagged** -- it is an e2e fixture; a flag would make the pipeline check depend on PostHog (same exception as `pyping`). |
| `storage.kv` / `db` | Stateless (`data.tables: []`). |
| egress / `net.http.*` | `egress: []`; replies go over the `relay` queue, never HTTP. |

## Feature flag

None, by design (see above) -- the documented exception to the `waddles.command-<name>` convention.

## Platforms

`consumes` **Twitch** `chat.message` with `command_prefix: ["!ping"]` and **Discord**
`chat.message` (no `command_prefix` filter in the manifest -- the bundle itself matches `!ping`).
The Discord rule was added in 1.0.3 so auto-bind can attach the alpha Discord ingest source.

## Logging / PII

The bundle emits **no log lines**, and only `{"channel", "text"}` cross the relay boundary -- the
actor never rides along (`relay_message_is_exactly_channel_and_text_on_the_wire`).

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest (bundle-compiler schema) / hub-api install-pipeline manifest (separate schema consumer) |
| `src/lib.rs` | `ProcessStage::transform`, `build_relay`, `ActionStage::dispatch` (wasm32), unit tests |
| `Cargo.toml` / `Cargo.lock` / `deny.toml` / `rust-toolchain.toml` | Exact-pinned deps, supply-chain policy, pinned toolchain (`wasm32-wasip2` + `wasm32-wasip1` targets) |

The crate version in `Cargo.toml` only exists to force a different component digest when a
manifest-only release needs new bytes (`app_versions.artifact_digest` is immutable and globally
unique) -- see the comment there. The reproducible-build gate is
`.github/workflows/core-bundles-reproducible.yml`.

## Test

```bash
cd bundles/rust/ping
cargo test                       # 11 host-side unit tests
cargo llvm-cov --summary-only    # ~99% line coverage of src/lib.rs (the wasm32-only dispatch
                                 # body is the only line not run on the host)
cargo clippy --lib --target wasm32-wasip2 -- -D warnings
cargo fmt --check
```

## Build

```bash
cargo component build --release --target wasm32-wasip2 --locked
# -> target/wasm32-wasip2/release/waddle_bundle_ping_rust.wasm
# The pinned, reproducible container build lives in bundles/Dockerfile.core-bundles
# (rust-bundle-builder stage).
```

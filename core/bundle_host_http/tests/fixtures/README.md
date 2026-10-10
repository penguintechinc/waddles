# `http_fixture.wasm`

A real, compiled WASI 0.2 component built against the committed
`wit/waddle-bundle/stage.wit` world (`waddle:bundle/stage@1.0.0`). It is the
guest half of `tests/executor_wire_e2e.rs`, which proves the `http.send` JSON
wire between the real `bundle-executor` and this crate's real `EgressGuard`
(request body sent, response headers + body decoded) -- see that file's module
doc for the defect this pins.

`process-stage.transform` handles one event type, `http-send`: its
`payload-json` is `METHOD URL BODYHEX SECRET` (space-separated; `-` for no
body / no secret; `SECRET` is `SLOT=REF`). It issues the WIT `http.send`
import and echoes the outcome into `payload-json` (`ok;status=..;truncated=..;
headers=name:value|..;body_hex=..` or `err;<variant>;<text>`). The fixture
only imports `waddle:bundle/http`, so it needs no other capability.

## Regenerating

`http-fixture-src/wit/stage.wit` is a copy of `wit/waddle-bundle/stage.wit`
(refresh it after any WIT change). To rebuild:

```bash
rustup target add wasm32-wasip1   # cargo-component's default target
cargo install cargo-component --locked
cd http-fixture-src
cargo component build --release
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/http_fixture.wasm" ../http_fixture.wasm
```

Built with `cargo-component 0.21.1` against `wit-bindgen-rt 0.44.0` (same
toolchain as `core/bundle_executor/tests/fixtures/hostile_fixture.wasm`).
`src/bindings.rs` is generated on every build and gitignored.

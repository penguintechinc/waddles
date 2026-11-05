# `reputation_service.wasm`

The REAL production reputation SERVICE bundle (`bundles/rust/reputation`, bundle-MCP-tools P5),
componentized, used by `../reputation_service_e2e.rs` so the e2e runs the shipped tool code (not a
stand-in) through the real executor + `tool.invoke` + gate.

It is a build artifact of `bundles/rust/reputation` -- rebuild it whenever that crate (or
`wit/waddle-bundle/stage.wit` / `sdk/waddle-sdk-rs`) changes, then re-run the e2e:

```bash
cd bundles/rust/reputation
cargo component build --release --target wasm32-wasip2 --locked
cp "${CARGO_TARGET_DIR:-target}/wasm32-wasip1/release/waddle_bundle_reputation_service.wasm" \
   ../../../core/tool_invoke_e2e/tests/fixtures/reputation_service.wasm
```

Built with `cargo-component 0.21.1` against `wit-bindgen-rt 0.44.0`.

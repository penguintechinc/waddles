# connector_e2e fixtures

## `twitch_overlay.wasm`

The REAL Twitch platform overlay (`bundles/rust/twitch`) compiled on `world connector-tools`; used by
`tests/twitch_profile.rs`. Not a synthetic fixture: rebuild after ANY change to that crate:

```bash
cd bundles/rust/twitch
cargo component build --release --target wasm32-wasip2 --locked
cp target/wasm32-wasip2/release/waddle_bundle_twitch_overlay.wasm \
   ../../../core/connector_e2e/tests/fixtures/twitch_overlay.wasm
```

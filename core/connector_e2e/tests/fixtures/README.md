# connector_e2e fixtures

REAL platform overlays built from `bundles/rust/{twitch,youtube}` (rebuild after ANY change to those crates):

    cd bundles/rust/twitch && cargo component build --release --target wasm32-wasip2 --locked
    cp target/wasm32-wasip2/release/waddle_bundle_twitch_overlay.wasm ../../../core/connector_e2e/tests/fixtures/twitch_overlay.wasm
    cd ../youtube && cargo component build --release --target wasm32-wasip2 --locked
    cp target/wasm32-wasip2/release/waddle_bundle_youtube_service.wasm ../../../core/connector_e2e/tests/fixtures/youtube_overlay.wasm

The discord overlay is `core/bundle_executor/tests/fixtures/discord_overlay.wasm`.

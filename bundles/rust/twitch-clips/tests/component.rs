//! Real-path build test: compiles the bundle to a WASI 0.2 component exactly as CI does
//! (`cargo component build --release --target wasm32-wasip2 --locked`) and asserts the component's
//! WIT surface. No skip path: a missing toolchain/`wasm-tools` fails the test.
use std::path::PathBuf;
use std::process::Command;

fn crate_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
}

fn target_dir() -> PathBuf {
    std::env::var_os("CARGO_TARGET_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate_dir().join("target"))
}

#[test]
fn component_builds_and_exposes_expected_wit_surface() {
    // Separate target dir: the outer `cargo test` may hold the default build-dir lock.
    let out = target_dir().join("component-test");
    let status = Command::new("cargo")
        .args([
            "component",
            "build",
            "--release",
            "--target",
            "wasm32-wasip2",
            "--locked",
        ])
        .env("CARGO_TARGET_DIR", &out)
        .current_dir(crate_dir())
        .status()
        .expect("cargo-component must be installed");
    assert!(status.success(), "cargo component build failed");

    let wasm = out.join("wasm32-wasip2/release/waddle_bundle_twitch_clips.wasm");
    assert!(wasm.is_file(), "missing artifact {}", wasm.display());
    let wit = Command::new("wasm-tools")
        .args(["component", "wit"])
        .arg(&wasm)
        .output()
        .expect("wasm-tools must be installed");
    assert!(wit.status.success(), "wasm-tools failed");
    let wit = String::from_utf8(wit.stdout).expect("utf8 wit");

    for needle in [
        "export waddle:bundle/process-stage@1.0.0",
        "export waddle:bundle/action-stage@1.0.0",
        "import waddle:bundle/tool@1.0.0",
        "import waddle:bundle/identity@1.0.0",
        "import waddle:bundle/relay@1.0.0",
        "import waddle:bundle/kv@1.0.0",
    ] {
        assert!(
            wit.contains(needle),
            "component WIT lacks `{needle}`:\n{wit}"
        );
    }
    // Least privilege: no raw HTTP, db, economy, ai, media, object storage in this app bundle.
    for forbidden in [
        "waddle:bundle/http",
        "waddle:bundle/economy",
        "waddle:bundle/db",
        "waddle:bundle/ai",
    ] {
        assert!(
            !wit.contains(forbidden),
            "component must not import `{forbidden}`"
        );
    }
}

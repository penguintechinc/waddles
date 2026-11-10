//! Real-path test: builds the actual WASI component with the repo's tooling
//! (`cargo component build --target wasm32-wasip2 --locked --release`) and inspects the result
//! with `wasm-tools`. Missing tooling FAILS loudly (never a silent skip).

use std::path::{Path, PathBuf};
use std::process::Command;

fn find_wasm(dir: &Path) -> Option<PathBuf> {
    ["wasm32-wasip2", "wasm32-wasip1"]
        .iter()
        .map(|t| dir.join(t).join("release/waddle_bundle_ai_teammates.wasm"))
        .find(|p| p.is_file())
}

#[test]
fn component_builds_and_exports_the_stage_next_tools_surface() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    let target = manifest.join("target/component-test");
    let status = Command::new("cargo")
        .args([
            "component",
            "build",
            "--target",
            "wasm32-wasip2",
            "--locked",
            "--release",
        ])
        .current_dir(manifest)
        .env("CARGO_TARGET_DIR", &target)
        .status()
        .expect("`cargo component` must be installed (cargo install cargo-component --locked)");
    assert!(status.success(), "cargo component build failed");
    let wasm = find_wasm(&target).expect("component artifact not found");

    let wit = Command::new("wasm-tools")
        .args(["component", "wit"])
        .arg(&wasm)
        .output()
        .expect("`wasm-tools` must be installed");
    assert!(wit.status.success(), "wasm-tools component wit failed");
    let wit = String::from_utf8_lossy(&wit.stdout);
    for export in [
        "export waddle:bundle/process-stage@1.0.0;",
        "export waddle:bundle/action-stage@1.0.0;",
        "export waddle:bundle/streaming-lifecycle@1.0.0;",
    ] {
        assert!(wit.contains(export), "missing {export}");
    }
    for import in [
        "import waddle:bundle/tool@1.0.0;",
        "import waddle:bundle/identity@1.0.0;",
        "import waddle:bundle/kv@1.0.0;",
        "import waddle:bundle/relay@1.0.0;",
    ] {
        assert!(wit.contains(import), "missing {import}");
    }
    // The model-facing ai host import is NOT used: all model access is the gated AI service tools.
    assert!(
        !wit.contains("waddle:bundle/ai@"),
        "bundle must not import the raw ai host capability"
    );
    assert!(
        !wit.contains("waddle:bundle/http@"),
        "bundle has no network egress"
    );
    let bytes = std::fs::metadata(&wasm).expect("artifact metadata").len();
    assert!(
        bytes > 10_000,
        "artifact suspiciously small ({bytes} bytes)"
    );
}

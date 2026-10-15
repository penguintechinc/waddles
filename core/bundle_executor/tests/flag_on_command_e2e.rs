//! End-to-end proof that a real command bundle's `%flags` WIT import, this
//! executor's `flags::Host` capability, and the Docker-ENV flag baseline
//! all actually work together -- the exact seam the 2026-10-04 alpha
//! incident broke (stale/mis-built `eightball`/`roll`/`lurk`/`count`
//! components whose `wit_world.imports` had no `flags` attribute at all,
//! which wasm-trapped `transform()` and silently dropped every `!8ball`
//! reply with no error surfaced anywhere).
//!
//! NO MOCKING of the WIT/flags/wasm boundary that hid that bug:
//!   - the `bundles/python/eightball` component is **freshly compiled** on
//!     every test run via a real `componentize-py componentize` invocation
//!     (the exact command `bundles/Dockerfile.core-bundles`'s
//!     `python-batch1-builder` stage runs), inside the pinned
//!     `python:3.13-slim-bookworm` image used in production -- a stale
//!     cached `.wasm` or a `stage.wit` missing `import %flags;` fails this
//!     build outright, which fails this test outright (no fallback, no
//!     skip);
//!   - it is instantiated by the REAL `bundle_executor::invoke::Executor`
//!     (real wasmtime engine/linker, real `crate::host::imports`
//!     `flags::Host` impl -- never stubbed);
//!   - the only thing simulated is the stage half of the host-API wire
//!     connection (mirrors `tests/host_bridge_integration.rs`), and even
//!     that answers `flags.enabled` by reading the literal
//!     `FLAG_WADDLES_COMMAND_8BALL` process env var this test sets --
//!     the same Docker-ENV-baseline variable name
//!     `core/svc_process/src/license.rs`'s `env_flag_var_name`/
//!     `env_flag_value` derive and read in production
//!     (`waddles.command-8ball` -> `FLAG_WADDLES_COMMAND_8BALL`, feature/
//!     flags-env-baseline, alpha 2026-10-04).
//!
//! Requires `docker` on PATH (same requirement as
//! `scripts/verify-core-bundles-reproducible.sh`) -- wired as a FATAL
//! check into this crate's existing `cargo test` gate
//! (`.github/workflows/rust-bundle-executor.yml` ->
//! `rust-crate-ci.yml`, every push/PR touching `core/bundle_executor/**`),
//! and reachable directly via `make test-bundle-flag-on-command-e2e`.
//!
//! **Post-activation-only update:** app bundles no longer gate on feature
//! flags (Global:INSTALLED -> Tenant:ENABLED -> Community:ACTIVATED is the
//! only gate), so `eightball` must reply with the legacy flag env var unset,
//! `false`, or `true`, and must NEVER issue a `flags.enabled` host-call. The
//! real build + real wasmtime host path (the point of this test) is unchanged.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::OnceLock;

use bundle_executor::config::CliConfig;
use bundle_executor::heartbeat::Heartbeat;
use bundle_executor::invoke::{ComponentSource, Executor};
use bundle_executor::wire::run_connection;
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ExportKind, Frame, HelloBody, HelloLimits,
    HelloOkBody, HostResultBody, InvokeBody, LoadBody, LoadLimits, Message, SandboxInfo,
    ShutdownBody,
};
use sha2::{Digest, Sha256};
use std::sync::Arc;

const APP_ID: &str = "waddles.core.example.eightball";

/// Legacy Docker-ENV flag-baseline variable the old `eightball` flag gate read
/// (`waddles.command-8ball` -> `FLAG_WADDLES_COMMAND_8BALL`). App bundles are now
/// activation-gated only, so the test sets it to every value and asserts it is IGNORED.
const FLAG_ENV_VAR: &str = "FLAG_WADDLES_COMMAND_8BALL";

/// Hands back one freshly-built component's bytes regardless of key --
/// there is exactly one bundle under test.
struct FixtureSource {
    wasm: Vec<u8>,
}

impl ComponentSource for FixtureSource {
    async fn fetch(
        &self,
        _component_key: &str,
        _sidecar_key: &str,
    ) -> Result<(Vec<u8>, Vec<u8>), bundle_executor::error::ExecutorError> {
        // `test_config()` below sets no `bundle_signing_public_keys`, so
        // `Executor::on_load` skips signature verification -- this `{}`
        // stub sidecar (the same one `storage_service.
        // upload_bundle_component()` writes before an artifact is signed)
        // is never parsed. Same convention as `tests/host_bridge_integration.rs`.
        Ok((self.wasm.clone(), b"{}".to_vec()))
    }
}

fn test_config() -> CliConfig {
    use clap::Parser;
    CliConfig::try_parse_from([
        "bundle-executor",
        "--stage-host-api-addr",
        "svc-process:8301",
    ])
    .expect("static test args always parse")
}

/// Runs the EXACT `componentize-py componentize` invocation
/// `bundles/Dockerfile.core-bundles`'s `python-batch1-builder` stage uses
/// for `eightball.wasm`, inside the same pinned `python:3.13-slim-bookworm`
/// digest, against this checkout's own `wit/waddle-bundle/stage.wit` and
/// `bundles/python/eightball/src` -- a from-scratch, every-run build, never
/// a committed/cached artifact. Panics (fails the test outright, never a
/// silent skip -- `rules/critical-rules.md` Verification Integrity) if
/// Docker is unavailable, the build fails (e.g. `stage.wit` missing
/// `import %flags;`, or any other componentize-py error), or the resulting
/// file is missing/empty.
fn build_fresh_eightball_wasm() -> (Vec<u8>, String) {
    const PYTHON_IMAGE: &str =
        "python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26";
    const COMPONENTIZE_PY_VERSION: &str = "0.25.1";

    let repo_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("core/bundle_executor has a parent (core/)")
        .parent()
        .expect("core/ has a parent (repo root)")
        .to_path_buf();

    let out_dir = std::env::temp_dir().join(format!(
        "flag-on-command-e2e-eightball-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&out_dir).expect("create temp wasm output dir");

    let build_script = format!(
        // `sh` in `python:3.13-slim-bookworm` is dash, not bash -- no
        // `pipefail` support, and this script contains no pipelines anyway,
        // so plain `set -eu` (fail on first error / unset var) is both
        // sufficient and portable.
        "set -eu; \
         pip install --no-cache-dir --quiet componentize-py=={COMPONENTIZE_PY_VERSION}; \
         cd /repo; \
         PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 componentize-py \
            -d wit/waddle-bundle -w stage \
            componentize -p sdk/waddle-sdk/src -p bundles/python/eightball/src \
            waddle_sdk._component_entry \
            -o /out/eightball.wasm"
    );

    let status = std::process::Command::new("docker")
        .args([
            "run",
            "--rm",
            "-v",
            &format!("{}:/repo:ro", repo_root.display()),
            "-v",
            &format!("{}:/out", out_dir.display()),
            PYTHON_IMAGE,
            "sh",
            "-c",
            &build_script,
        ])
        .status()
        .unwrap_or_else(|e| {
            panic!(
                "flag_on_command_e2e: `docker` must be on PATH to build the real \
                 eightball.wasm fixture -- no mock/stub substitute is permitted at this \
                 boundary (error: {e})"
            )
        });

    assert!(
        status.success(),
        "flag_on_command_e2e: fresh `componentize-py componentize` build of \
         bundles/python/eightball FAILED (exit {:?}) -- this is the real build the \
         flag->wasm->reply path depends on; a broken bundle or a stage.wit missing \
         `import %flags;` MUST fail here, not fall back to a stale/stubbed component",
        status.code()
    );

    let wasm_path = out_dir.join("eightball.wasm");
    let bytes = std::fs::read(&wasm_path).unwrap_or_else(|e| {
        panic!("componentize-py reported success but {wasm_path:?} is unreadable: {e}")
    });
    assert!(
        !bytes.is_empty(),
        "freshly built eightball.wasm at {wasm_path:?} is empty"
    );

    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    let digest = format!("sha256:{:x}", hasher.finalize());

    let _ = std::fs::remove_dir_all(&out_dir);
    (bytes, digest)
}

/// Built once per test binary process and shared -- rebuilding per
/// assertion would pay the ~componentize-py cost twice for no extra
/// freshness guarantee (the whole point is "once per `cargo test` run",
/// not "once per assertion").
fn eightball_wasm() -> &'static (Vec<u8>, String) {
    static WASM: OnceLock<(Vec<u8>, String)> = OnceLock::new();
    WASM.get_or_init(build_fresh_eightball_wasm)
}

/// Drives one `load` + one `process-stage.transform("!8ball ...")` + one
/// `shutdown` over the stage half of `io`, answering every host-call the
/// bundle issues mid-flight. Returns the `transform` call's raw JSON
/// result payload (`null` for "no reply", a `platform-event` JSON object
/// otherwise).
///
/// The `flags`/`enabled` branch is this test's one deliberate seam: it
/// reads the literal `FLAG_WADDLES_COMMAND_8BALL` env var this test set,
/// exactly mirroring `core/svc_process/src/license.rs`'s Docker-ENV
/// baseline (PostHog -> ENV -> caller default) for the one case that
/// matters here (no PostHog client configured in this test). Any other
/// host-call the bundle issues panics the test -- a transform() that
/// starts needing `kv`/`db`/`http`/`relay` without this test being updated
/// to expect it is itself a regression worth surfacing loudly.
async fn drive_stage<S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin>(
    mut io: S,
    digest: String,
) -> serde_json::Value {
    let hello = read_frame(&mut io).await.expect("hello");
    write_frame(
        &mut io,
        &Frame::new(
            hello.id,
            Message::HelloOk(HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: HelloLimits {
                    call_timeout_ms: 5000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            }),
        ),
    )
    .await
    .expect("write hello-ok");

    write_frame(
        &mut io,
        &Frame::new(
            1000,
            Message::Load(LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: APP_ID.to_string(),
                version: "1.0.0".to_string(),
                digest: digest.clone(),
                component_key: "bundles/eightball/1/x.wasm".to_string(),
                sidecar_key: "bundles/eightball/1/x.json".to_string(),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 5000,
                    memory_mb: 64,
                },
            }),
        ),
    )
    .await
    .expect("write load");
    let loaded = read_frame(&mut io).await.expect("loaded reply");
    let Message::Loaded(loaded_body) = loaded.message else {
        panic!("expected loaded, got {:?}", loaded.message);
    };
    assert_eq!(loaded_body.digest, digest);

    write_frame(
        &mut io,
        &Frame::new(
            1001,
            Message::Invoke(InvokeBody {
                app_id: APP_ID.to_string(),
                digest: digest.clone(),
                export: ExportKind::Transform,
                payload: serde_json::json!({
                    "platform": "twitch",
                    "event_type": "chat.message",
                    "actor": null,
                    "payload_json": "{\"text\":\"!8ball will this e2e test catch the next regression?\",\"channel_id\":\"chan-e2e\"}",
                    "occurred_at": "2026-10-04T00:00:00.000Z",
                }),
                deadline_ms: 10_000,
                trace: None,
            }),
        ),
    )
    .await
    .expect("write invoke transform");

    // Answer every host-call the bundle issues until it returns its
    // transform result -- `waddle_sdk._component_entry`'s shared wiring
    // always calls `context.get-context` first, then `app.py` itself calls
    // `flags.enabled`, then (flag ON only) `log.write` for the
    // matched-command log line before returning its reply. Flag OFF stops
    // at `flags.enabled` -- `app.py` returns `None` before ever logging.
    loop {
        let frame = read_frame(&mut io).await.expect("expected a frame");
        match frame.message {
            Message::HostCall(call) => {
                let result = match (call.capability, call.op.as_str()) {
                    // `waddle_sdk._component_entry`'s shared wiring calls
                    // `context.get-context` once per invocation before
                    // handing off to the bundle's own `transform()` --
                    // not something `app.py` itself calls directly.
                    (CapabilityKind::Context, "get-context") => serde_json::json!({
                        "tenant": "t1",
                        "app_id": APP_ID,
                        "feature": "waddles.core.example",
                        "version": "1.0.0",
                        "message_id": "msg-e2e-1",
                        "config_json": "{}",
                    }),
                    (CapabilityKind::Flags, "enabled") => panic!(
                        "app (command) bundles are activation-gated only: eightball must NOT \
                         call flags.enabled (args={:?})",
                        call.args
                    ),
                    (CapabilityKind::Log, "write") => serde_json::json!({}),
                    other => panic!(
                        "flag_on_command_e2e: unexpected host-call {other:?} (args={:?}) -- \
                         eightball's transform() should only ever call context.get-context \
                         and log.write",
                        call.args
                    ),
                };
                write_frame(
                    &mut io,
                    &Frame::new(
                        frame.id,
                        Message::HostResult(HostResultBody {
                            result: Some(result),
                            error: None,
                        }),
                    ),
                )
                .await
                .expect("write host-result");
            }
            Message::Result(result_body) => {
                write_frame(
                    &mut io,
                    &Frame::new(1002, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
                )
                .await
                .expect("write shutdown");
                return result_body.payload;
            }
            Message::Error(err) => {
                panic!("transform invocation errored instead of returning a result: {err:?}");
            }
            other => panic!("unexpected frame while awaiting transform result: {other:?}"),
        }
    }
}

/// Spins up a fresh real `Executor` + real wasmtime instantiation of the
/// (shared, already-built) eightball component, runs one `transform`
/// invocation end to end over a real host-API wire connection, and returns
/// its JSON result payload.
async fn run_one_transform(wasm: &[u8], digest: &str) -> serde_json::Value {
    let (executor_io, stage_io) = tokio::io::duplex(256 * 1024);
    let executor = Arc::new(
        Executor::new(
            &test_config(),
            FixtureSource {
                wasm: wasm.to_vec(),
            },
        )
        .expect("executor builds"),
    );

    let digest_owned = digest.to_string();
    let stage = tokio::spawn(drive_stage(stage_io, digest_owned));

    let executor_task = tokio::spawn(async move {
        run_connection(
            executor_io,
            HelloBody {
                protocol_version: 1,
                executor_version: "0.1.0".to_string(),
                wasmtime_version: bundle_executor::engine::WASMTIME_VERSION.to_string(),
                wasmtime_abi: bundle_executor::engine::WASMTIME_VERSION.to_string(),
                collector: "drc".to_string(),
                sandbox: SandboxInfo {
                    runtime: "runc".to_string(),
                    verified: false,
                },
            },
            Arc::clone(&executor),
            "test-peer",
            Heartbeat::disabled(),
        )
        .await
    });

    let payload = stage.await.expect("stage task");
    executor_task
        .await
        .expect("executor task")
        .expect("connection ran to a clean shutdown");
    payload
}

/// Activation-only regression test: a real, freshly-compiled `eightball`
/// component (its SDK entry still eager-imports `%flags`, but the manifest
/// no longer grants `flags.read` -- `Load` carries `capabilities: []`) must
/// instantiate in the real wasmtime host and reply REGARDLESS of the
/// legacy `FLAG_WADDLES_COMMAND_8BALL` env var, never calling
/// `flags.enabled`.
#[tokio::test]
async fn app_bundle_8ball_replies_without_flag_gate() {
    let (wasm, digest) = eightball_wasm();

    for flag_env in [None, Some("false"), Some("true")] {
        match flag_env {
            Some(v) => std::env::set_var(FLAG_ENV_VAR, v),
            None => std::env::remove_var(FLAG_ENV_VAR),
        }
        let payload = run_one_transform(wasm, digest).await;
        assert!(
            !payload.is_null(),
            "{FLAG_ENV_VAR}={flag_env:?}: expected a real 8ball reply, got null -- \
             app bundles must be activation-gated only"
        );
        let inner = payload["payload_json"]
            .as_str()
            .expect("transform reply has a string payload_json field");
        let reply: serde_json::Value =
            serde_json::from_str(inner).expect("payload_json is valid JSON");
        let text = reply["text"].as_str().expect("8ball reply has text");
        assert!(
            text.starts_with('\u{1f3b1}') && text.len() > "\u{1f3b1} ".len(),
            "8ball reply text malformed: {text:?}"
        );
        assert_eq!(reply["channel_id"], serde_json::json!("chan-e2e"));
    }
    std::env::remove_var(FLAG_ENV_VAR);
}

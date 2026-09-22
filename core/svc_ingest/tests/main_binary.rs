//! End-to-end integration test for the compiled `svc-ingest` binary itself
//! -- `main.rs`'s `main()` dispatch, `run()`'s real CLI-arg parsing and
//! signal wiring, and `run_healthcheck()`'s process-exit-code contract.
//! `tests/run.rs` and the `#[cfg(test)]` unit tests in `src/lib.rs` cover
//! everything below the process boundary; this file is the one test that
//! actually spawns `env!("CARGO_BIN_EXE_svc-ingest")` as a subprocess --
//! the only way to exercise `main()` itself and a real OS-delivered
//! SIGTERM, matching the container `HEALTHCHECK`'s and Kubernetes'
//! `SIGTERM`-based shutdown contract exactly.

use std::net::TcpListener;
use std::process::{Command, Stdio};
use std::time::Duration;

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .expect("bind to ephemeral port")
        .local_addr()
        .expect("read local addr")
        .port()
}

#[tokio::test]
async fn healthcheck_subcommand_exits_nonzero_when_nothing_is_listening() {
    let port = free_port();
    let status = Command::new(env!("CARGO_BIN_EXE_svc-ingest"))
        .arg("--healthcheck")
        .env("MODULE_PORT", port.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("spawn svc-ingest --healthcheck");
    assert_eq!(status.code(), Some(1));
}

#[cfg(unix)]
#[tokio::test]
async fn full_binary_serves_healthz_and_shuts_down_gracefully_on_sigterm() {
    let http_port = free_port();
    let metrics_port = free_port();

    let mut child = Command::new(env!("CARGO_BIN_EXE_svc-ingest"))
        .args([
            "--http-port",
            &http_port.to_string(),
            "--metrics-port",
            &metrics_port.to_string(),
            "--bind-addr",
            "127.0.0.1",
        ])
        .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn svc-ingest");

    // Wait for the HTTP listener to come up rather than sleeping a fixed
    // guess -- the CI runner's scheduling latency is not this test's to
    // assume.
    let url = format!("http://127.0.0.1:{http_port}/healthz");
    let client = reqwest::Client::new();
    let mut ready = false;
    for _ in 0..50 {
        if client
            .get(&url)
            .send()
            .await
            .map(|r| r.status().is_success())
            .unwrap_or(false)
        {
            ready = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ready, "svc-ingest did not become ready in time");

    // The `--healthcheck` subcommand must agree while the server is up --
    // this is the exact invocation `Dockerfile.rust`'s HEALTHCHECK uses.
    let status = Command::new(env!("CARGO_BIN_EXE_svc-ingest"))
        .arg("--healthcheck")
        .env("MODULE_PORT", http_port.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .expect("spawn svc-ingest --healthcheck");
    assert!(status.success());

    // SIGTERM (Kubernetes pod termination) must trigger graceful shutdown
    // -- exit 0, not a hang or a crash.
    // SAFETY: `child.id()` is a live PID we just spawned and still own.
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    let exit = tokio::task::spawn_blocking(move || child.wait())
        .await
        .expect("join wait task")
        .expect("wait on child");
    assert!(exit.success(), "expected clean shutdown, got {exit:?}");
}

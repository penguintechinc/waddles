//! gRPC health listener and the real binary (startup, `--healthcheck`, SIGTERM shutdown).

mod support;

use std::net::TcpListener;
use std::process::{Command, Stdio};
use std::time::Duration;

use support::*;
use svc_analytics::health::{check, serve, Ready};
use tokio::sync::watch;

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

#[tokio::test]
async fn health_serves_and_reflects_readiness_then_shuts_down() {
    let port = free_port();
    let addr = format!("127.0.0.1:{port}");
    let ready = Ready::new();
    let (tx, mut rx) = watch::channel(false);
    let server = tokio::spawn(serve(addr.parse().unwrap(), ready.clone(), async move {
        let _ = rx.wait_for(|v| *v).await;
    }));
    let mut ok = false;
    for _ in 0..50 {
        if check(&addr).await.is_ok() {
            ok = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok, "SERVING");
    ready.set(false);
    let mut not_serving = false;
    for _ in 0..30 {
        if check(&addr).await.is_err() {
            not_serving = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(not_serving, "NOT_SERVING while not ready");
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(
        check(&addr).await.is_err(),
        "nothing listening after shutdown"
    );
}

#[tokio::test]
async fn binary_starts_idle_with_flag_off_serves_health_and_exits_on_sigterm() {
    let fx = Fixture::new().await;
    let port = free_port();
    let bin = env!("CARGO_BIN_EXE_svc-analytics");
    let mut child = Command::new(bin)
        .env("VALKEY_URL", valkey_url())
        .env("DATABASE_URL", &fx.writer_url)
        .env("GRPC_HEALTH_PORT", port.to_string())
        .env("ANALYTICS_STREAM_KEY", unique_stream())
        .env_remove("OTEL_EXPORTER_OTLP_ENDPOINT")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut up = false;
    for _ in 0..100 {
        let out = Command::new(bin)
            .arg("--healthcheck")
            .env("GRPC_HEALTH_PORT", port.to_string())
            .status()
            .unwrap();
        if out.success() {
            up = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    assert!(
        up,
        "binary reports SERVING via --healthcheck (flag OFF => idle but healthy)"
    );
    let killed = Command::new("kill")
        .arg("-TERM")
        .arg(child.id().to_string())
        .status()
        .unwrap();
    assert!(killed.success());
    let mut status = None;
    for _ in 0..100 {
        if let Some(s) = child.try_wait().unwrap() {
            status = Some(s);
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    let status = status.unwrap_or_else(|| {
        let _ = child.kill();
        panic!("binary did not exit on SIGTERM");
    });
    assert!(status.success(), "clean shutdown: {status:?}");
}

#[test]
fn binary_fails_fast_on_missing_config_and_healthcheck_fails_when_down() {
    let bin = env!("CARGO_BIN_EXE_svc-analytics");
    let out = Command::new(bin)
        .env_remove("VALKEY_URL")
        .env_remove("DATABASE_URL")
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("VALKEY_URL is required"));
    let port = free_port();
    let st = Command::new(bin)
        .arg("--healthcheck")
        .env("GRPC_HEALTH_PORT", port.to_string())
        .status()
        .unwrap();
    assert!(!st.success());
}

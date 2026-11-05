//! Docker-CLI test container helper shared (via `#[path]`) by every crate
//! whose integration tests need a throwaway Postgres/Valkey/Redis.
//!
//! Replaces the `testcontainers` dev-dependency (RUSTSEC-2026-0440: its
//! `astral-tokio-tar` is unmaintained) with zero new crates: it shells out
//! to the `docker` CLI (rootless Docker/Podman) via `std::process`. Each
//! container gets a unique name and a Docker-assigned random loopback port
//! (`-p 127.0.0.1::<port>`), so parallel tests never collide. The container
//! is force-removed on `Drop`, including during a panic unwind. Lives under
//! a `tests/` directory so `cargo llvm-cov` excludes it from coverage.

use std::process::{Command, Output};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

static COUNTER: AtomicU64 = AtomicU64::new(0);

/// A running, ready test container; removed (with its volumes) on drop.
pub struct DockerContainer {
    name: String,
    port: u16,
}

impl DockerContainer {
    /// Loopback host the published port is bound to.
    pub fn host(&self) -> &'static str {
        "127.0.0.1"
    }

    /// Host port Docker mapped to the container port.
    pub fn port(&self) -> u16 {
        self.port
    }
}

impl Drop for DockerContainer {
    fn drop(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "-f", "-v", &self.name])
            .output();
    }
}

fn docker(args: &[&str]) -> Result<Output, String> {
    Command::new("docker")
        .args(args)
        .output()
        .map_err(|e| format!("cannot run `docker {}`: {e}", args.join(" ")))
}

fn stdout(out: &Output) -> String {
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

fn stderr(out: &Output) -> String {
    String::from_utf8_lossy(&out.stderr).trim().to_string()
}

/// Starts `image` detached with `env`, publishes `container_port` on a random
/// loopback port, and polls `ready_cmd` (run via `docker exec`, exit 0 =
/// ready) until `timeout`. Blocking; call [`start_container`] from async code.
pub fn start_container_blocking(
    image: &str,
    container_port: u16,
    env: &[(&str, &str)],
    ready_cmd: &[&str],
    timeout: Duration,
) -> Result<DockerContainer, String> {
    let name = format!(
        "wtc-{}-{}-{}",
        std::process::id(),
        COUNTER.fetch_add(1, Ordering::Relaxed),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.subsec_nanos())
            .unwrap_or(0)
    );
    let publish = format!("127.0.0.1::{container_port}");
    let env_args: Vec<String> = env.iter().map(|(k, v)| format!("{k}={v}")).collect();
    let mut args: Vec<&str> = vec!["run", "-d", "--name", &name, "-p", &publish];
    for e in &env_args {
        args.push("-e");
        args.push(e);
    }
    args.push(image);
    let run = docker(&args)?;
    // `Drop` owns cleanup from here on, even if readiness later fails.
    let mut container = DockerContainer { name, port: 0 };
    if !run.status.success() {
        return Err(format!("docker run {image} failed: {}", stderr(&run)));
    }

    let deadline = Instant::now() + timeout;
    let port_spec = format!("{container_port}/tcp");
    loop {
        let mut exec: Vec<&str> = vec!["exec", &container.name];
        exec.extend_from_slice(ready_cmd);
        if docker(&exec)?.status.success() {
            break;
        }
        if Instant::now() >= deadline {
            let logs = docker(&["logs", "--tail", "30", &container.name])
                .map(|o| format!("{}\n{}", stdout(&o), stderr(&o)))
                .unwrap_or_default();
            return Err(format!(
                "{image} not ready after {timeout:?}; last logs:\n{logs}"
            ));
        }
        std::thread::sleep(Duration::from_millis(250));
    }

    let mapped = docker(&["port", &container.name, &port_spec])?;
    // `docker port` prints one `host:port` line per binding (e.g. `127.0.0.1:49153`).
    container.port = stdout(&mapped)
        .lines()
        .find_map(|l| l.rsplit(':').next()?.trim().parse().ok())
        .ok_or_else(|| format!("cannot parse `docker port` output: {}", stdout(&mapped)))?;
    Ok(container)
}

/// Async wrapper over [`start_container_blocking`]; panics with the failure
/// reason, which is what a test fixture wants.
pub async fn start_container(
    image: &'static str,
    container_port: u16,
    env: &'static [(&'static str, &'static str)],
    ready_cmd: &'static [&'static str],
    timeout: Duration,
) -> DockerContainer {
    tokio::task::spawn_blocking(move || {
        start_container_blocking(image, container_port, env, ready_cmd, timeout)
    })
    .await
    .expect("container start task joins")
    .unwrap_or_else(|e| panic!("test container starts: {e}"))
}

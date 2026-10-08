//! Thin binary entrypoint -- all real logic lives in `src/lib.rs` so
//! `tests/` integration tests can exercise it directly (same pattern as
//! `core/svc_process`).

/// What `--healthcheck[=<mode>]` was asked to check -- parsed from raw
/// `argv` (not `clap`) since the healthcheck invocation deliberately
/// avoids `CliConfig::parse`'s `STAGE_HOST_API_ADDR` requirement (see
/// `bundle_executor::run_session_healthcheck`'s own doc comment).
enum HealthcheckMode {
    /// `--healthcheck` or `--healthcheck=engine` (default): build-tests the
    /// wasmtime engine/linker.
    Engine,
    /// `--healthcheck=session [--max-age <secs>]`: checks the on-disk
    /// liveness probe file `crate::heartbeat` maintains.
    Session {
        max_age: Option<std::time::Duration>,
    },
}

/// Recognizes `--healthcheck`, `--healthcheck=engine`, `--healthcheck=
/// session`, or the space-separated `--healthcheck session` form (matching
/// how Kubernetes' `exec.command` list is usually authored), plus an
/// optional trailing `--max-age <secs>` for the session mode. Returns
/// `None` for the normal `bundle_executor::run()` invocation (no
/// `--healthcheck` flag at all).
fn healthcheck_mode(args: &[String]) -> Option<HealthcheckMode> {
    let first = args.get(1)?;
    let (flag, inline_mode) = match first.split_once('=') {
        Some((flag, mode)) => (flag, Some(mode)),
        None => (first.as_str(), None),
    };
    if flag != "--healthcheck" {
        return None;
    }
    let mode = inline_mode
        .map(str::to_string)
        .or_else(|| args.get(2).filter(|a| !a.starts_with("--")).cloned())
        .unwrap_or_else(|| "engine".to_string());

    match mode.as_str() {
        "session" => {
            let max_age = args
                .iter()
                .position(|a| a == "--max-age")
                .and_then(|i| args.get(i + 1))
                .and_then(|v| v.parse::<u64>().ok())
                .map(std::time::Duration::from_secs);
            Some(HealthcheckMode::Session { max_age })
        }
        _ => Some(HealthcheckMode::Engine),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if let Some(mode) = healthcheck_mode(&args) {
        let result = match mode {
            HealthcheckMode::Engine => bundle_executor::run_healthcheck().await,
            HealthcheckMode::Session { max_age } => {
                bundle_executor::run_session_healthcheck(max_age).await
            }
        };
        if let Err(e) = result {
            eprintln!("bundle-executor healthcheck failed: {e}");
            std::process::exit(1);
        }
        return Ok(());
    }
    bundle_executor::run().await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn no_healthcheck_flag_returns_none() {
        assert!(healthcheck_mode(&args(&["bundle-executor"])).is_none());
    }

    #[test]
    fn bare_healthcheck_flag_is_engine_mode() {
        assert!(matches!(
            healthcheck_mode(&args(&["bundle-executor", "--healthcheck"])),
            Some(HealthcheckMode::Engine)
        ));
    }

    #[test]
    fn healthcheck_equals_session_is_session_mode() {
        assert!(matches!(
            healthcheck_mode(&args(&["bundle-executor", "--healthcheck=session"])),
            Some(HealthcheckMode::Session { max_age: None })
        ));
    }

    #[test]
    fn space_separated_session_mode_is_recognized() {
        assert!(matches!(
            healthcheck_mode(&args(&["bundle-executor", "--healthcheck", "session"])),
            Some(HealthcheckMode::Session { max_age: None })
        ));
    }

    #[test]
    fn session_mode_parses_trailing_max_age() {
        match healthcheck_mode(&args(&[
            "bundle-executor",
            "--healthcheck=session",
            "--max-age",
            "20",
        ])) {
            Some(HealthcheckMode::Session { max_age: Some(d) }) => {
                assert_eq!(d, std::time::Duration::from_secs(20))
            }
            other => panic!("expected Session{{max_age: Some(20s)}}, got {other:?}"),
        }
    }

    impl std::fmt::Debug for HealthcheckMode {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            match self {
                HealthcheckMode::Engine => write!(f, "Engine"),
                HealthcheckMode::Session { max_age } => {
                    write!(f, "Session {{ max_age: {max_age:?} }}")
                }
            }
        }
    }
}

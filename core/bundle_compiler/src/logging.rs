//! Structured JSON logging via `tracing`, per `critical-rules.md`
//! Observability. No Rust penguin-logging crate exists yet (known gap,
//! tracked repo-wide) -- this module is the placeholder every other Rust
//! data-plane service in this repo also uses: it asserts `tracing` is in
//! use and that log lines are structured JSON, never a hand-rolled
//! `println!`.

use tracing_subscriber::EnvFilter;

/// Initializes the global `tracing` subscriber. Call exactly once, at the
/// top of `main()`, before any other log line. Level defaults to `info`
/// when `RUST_LOG` is unset, matching `critical-rules.md` Observability's
/// "INFO is the default runtime level" rule.
pub fn init_logging() {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .with_target(true)
        .init();
}

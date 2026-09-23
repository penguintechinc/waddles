//! Thin binary entrypoint -- all real logic lives in `src/lib.rs` so
//! `tests/` integration tests can exercise it directly (same pattern as
//! `core/svc_process`).

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::args().nth(1).as_deref() == Some("--healthcheck") {
        if let Err(e) = bundle_executor::run_healthcheck().await {
            eprintln!("bundle-executor healthcheck failed: {e}");
            std::process::exit(1);
        }
        return Ok(());
    }
    bundle_executor::run().await?;
    Ok(())
}

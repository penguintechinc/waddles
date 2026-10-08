//! Thin binary entrypoint -- all real logic lives in `src/lib.rs` so
//! `tests/` integration tests can exercise it without subprocessing.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--healthcheck") {
        return egress_proxy::run_healthcheck().await;
    }
    egress_proxy::run().await
}

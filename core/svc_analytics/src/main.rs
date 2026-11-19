// SPDX-License-Identifier: AGPL-3.0-or-later
//! Thin binary entrypoint -- all logic lives in `src/lib.rs`.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--healthcheck") {
        return svc_analytics::run_healthcheck().await;
    }
    svc_analytics::run().await
}

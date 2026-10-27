// SPDX-License-Identifier: AGPL-3.0-or-later
//! Thin binary entrypoint -- all real logic lives in `src/lib.rs` so
//! `tests/` integration tests can exercise it without subprocessing.

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().nth(1).as_deref() == Some("--healthcheck") {
        return egress_proxy::run_healthcheck().await;
    }
    // `git-proxy` run mode: allowlist-only CONNECT proxy for git clones
    // (no machine-JWT gate) -- see `egress_proxy::git_proxy`.
    if std::env::args().nth(1).as_deref() == Some("git-proxy") {
        return egress_proxy::git_proxy::run().await;
    }
    egress_proxy::run().await
}

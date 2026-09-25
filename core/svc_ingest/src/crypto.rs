//! Process-wide rustls `CryptoProvider` installation.
//!
//! Two rustls crypto backends compile into this binary: `ring` (this
//! crate's own direct `rustls` pin, used for the Valkey outbound-relay TLS
//! connection, `src/outbound.rs`; also linked internally by
//! `penguin-spine`) and `aws-lc-rs` (pulled in transitively by
//! `penguin-licensing`'s `reqwest` dependency) -- confirmed via
//! `Cargo.lock`: both `aws-lc-sys`/`aws-lc-rs` and `ring` are present.
//! Two backends in one process defeats rustls's own automatic provider
//! auto-detection (`rustls::crypto::CryptoProvider::get_default`), which
//! panics on the first TLS-capable client/connection built without an
//! explicit choice ("Could not automatically determine the process-level
//! CryptoProvider") -- unless exactly one provider is installed
//! explicitly, first, before anything else has a chance to build a
//! `rustls::ClientConfig` (a `reqwest::Client`/`redis::Client`/IRC or
//! Discord TLS socket can all trigger this at construction time, not just
//! at connection time).
//!
//! `ensure_installed` MUST be called before constructing any TLS-capable
//! client in this process. `crate::lib::run_with_shutdown` calls it as
//! its first statement, before `telemetry::init` (which may build an
//! OTLP/HTTPS exporter) and `license::build_license_client` (which
//! always builds an HTTPS `reqwest::Client`). Idempotent by construction
//! (`OnceLock`): safe to call again from `src/outbound.rs` (which needs
//! the same guarantee for its own Valkey TLS connection) or from any test
//! that constructs a real TLS-capable client, regardless of call order.

use std::sync::OnceLock;

/// Installs the process-level rustls `CryptoProvider` (`ring`) exactly
/// once. See this module's doc comment for why this is load-bearing, not
/// defensive boilerplate.
pub fn ensure_installed() {
    static INSTALLED: OnceLock<()> = OnceLock::new();
    INSTALLED.get_or_init(|| {
        // Ignoring the `Result`: `install_default` only fails when a
        // provider is already installed (by this call or a previous one),
        // which is exactly the outcome this function wants.
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ensure_installed_is_idempotent() {
        // Calling twice (or, across the full test binary, from every test
        // module that also calls it) must not panic -- the `OnceLock`
        // guard is exactly what makes this safe regardless of order.
        ensure_installed();
        ensure_installed();
    }
}

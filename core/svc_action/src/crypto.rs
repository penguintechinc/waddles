//! Installs the process-level rustls `CryptoProvider` exactly once,
//! process-wide.
//!
//! Two rustls crypto backends are now linked into this binary: `ring`
//! (this crate's own `rustls`/`tokio-rustls`/`redis` TLS features) and
//! `aws-lc-rs` (pulled in transitively by `penguin-licensing`'s
//! `rustls-platform-verifier` dependency, added in the licensing/flags
//! finalization pass). With exactly one backend linked, rustls 0.23 can
//! auto-select it as the default; with two, it refuses to guess and every
//! default-provider-based builder call (`rustls::ServerConfig::builder()`,
//! `ClientConfig::builder()`) panics until something calls
//! `CryptoProvider::install_default()` explicitly. This module is that
//! call, made idempotent (`std::sync::Once`) so every TLS-touching call
//! site in this crate can invoke it defensively without caring whether
//! another one already has.
//!
//! Call this **before** the first rustls builder call on any code path --
//! `crate::host_api::build_server_config` (the mTLS host-API listener) and
//! `crate::usage::connect` (the direct Valkey TLS connection) both do.

static CRYPTO_PROVIDER_INIT: std::sync::Once = std::sync::Once::new();

/// Installs the `ring` backend as the process-level default `CryptoProvider`
/// -- a no-op on every call after the first. `ring` is this crate's own
/// explicit choice (matching every other rustls-touching dependency this
/// crate itself pins with the `ring` feature), not `aws-lc-rs`, which is
/// only present because a transitive dependency needs it and never
/// configured as this process's default.
pub(crate) fn ensure_crypto_provider_installed() {
    CRYPTO_PROVIDER_INIT.call_once(|| {
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ensure_crypto_provider_installed_is_idempotent() {
        // Calling it any number of times must never panic -- the whole
        // point of the `Once` guard.
        ensure_crypto_provider_installed();
        ensure_crypto_provider_installed();
        ensure_crypto_provider_installed();
    }
}

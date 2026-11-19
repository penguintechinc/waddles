//! Process-wide rustls `CryptoProvider` installation (`ring`). Two rustls backends compile
//! into the binary (ring via this crate / redis, aws-lc-rs via the license client's
//! `reqwest`), which defeats rustls's automatic provider selection; exactly one provider
//! must be installed explicitly before any TLS-capable client is built.

use std::sync::OnceLock;

/// Installs the `ring` provider exactly once (idempotent).
pub fn ensure_installed() {
    static INSTALLED: OnceLock<()> = OnceLock::new();
    INSTALLED.get_or_init(|| {
        // Err only means a provider is already installed, which is the desired outcome.
        let _ = rustls::crypto::ring::default_provider().install_default();
    });
}

#[cfg(test)]
mod tests {
    #[test]
    fn idempotent() {
        super::ensure_installed();
        super::ensure_installed();
    }
}

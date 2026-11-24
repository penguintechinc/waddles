// SPDX-License-Identifier: AGPL-3.0-or-later
//! Process-level rustls `CryptoProvider` installation.
//!
//! rustls 0.23 panics ("Could not automatically determine the process-level
//! CryptoProvider") on the first default-provider `ServerConfig`/`ClientConfig`
//! build when it cannot pick exactly one backend. Every binary entrypoint in
//! this crate calls [`install_default_provider_or_exit`] first, before
//! telemetry, TLS or tonic setup, so a missing install can never crash-loop
//! the pod.

/// Installs the `ring` provider as the process default, idempotently.
///
/// `install_default` only errors when a provider is already installed; that is
/// accepted only if a default really is present. Anything else is returned so
/// the caller can log it and exit non-zero.
pub fn install_default_provider() -> Result<(), String> {
    if rustls::crypto::ring::default_provider()
        .install_default()
        .is_ok()
    {
        return Ok(());
    }
    if rustls::crypto::CryptoProvider::get_default().is_some() {
        Ok(())
    } else {
        Err("failed to install the rustls ring CryptoProvider".to_string())
    }
}

/// Entrypoint helper: installs the provider or logs and exits non-zero.
pub fn install_default_provider_or_exit() {
    if let Err(e) = install_default_provider() {
        eprintln!("fatal: {e}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Regression: the startup TLS-config build path must not panic once the
    /// provider is installed, and installing twice must stay Ok.
    #[test]
    fn provider_install_makes_default_tls_builders_work() {
        assert!(install_default_provider().is_ok(), "first install");
        assert!(
            install_default_provider().is_ok(),
            "idempotent second install"
        );
        assert!(rustls::crypto::CryptoProvider::get_default().is_some());
        // `builder()` panics without a process-level default provider.
        let _ = rustls::ServerConfig::builder().with_no_client_auth();
        let _ = rustls::ClientConfig::builder();
    }
}

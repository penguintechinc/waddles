//! DNS resolution seam -- mirrors `bundle_host_http::egress`'s own
//! `Resolver` trait/`TokioResolver` pattern exactly: production always
//! resolves via `tokio::net::lookup_host` and pins the *first* returned
//! address (resolve once, connect to that address only, never re-resolve
//! between check and connect -- the anti-DNS-rebinding property this
//! crate's tests exercise with a fake multi-answer resolver).

use std::io;
use std::net::SocketAddr;

use async_trait::async_trait;

#[async_trait]
pub trait Resolver: Send + Sync {
    async fn resolve(&self, host: &str, port: u16) -> io::Result<Vec<SocketAddr>>;
}

pub struct TokioResolver;

#[async_trait]
impl Resolver for TokioResolver {
    async fn resolve(&self, host: &str, port: u16) -> io::Result<Vec<SocketAddr>> {
        let addrs = tokio::net::lookup_host((host, port)).await?;
        Ok(addrs.collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn tokio_resolver_resolves_localhost_to_a_loopback_address() {
        let resolver = TokioResolver;
        let addrs = resolver
            .resolve("localhost", 4242)
            .await
            .expect("localhost must resolve via /etc/hosts");
        assert!(!addrs.is_empty(), "expected at least one address");
        assert!(
            addrs.iter().all(|a| a.ip().is_loopback()),
            "expected only loopback addresses, got {addrs:?}"
        );
        assert!(addrs.iter().all(|a| a.port() == 4242));
    }

    #[tokio::test]
    async fn tokio_resolver_propagates_an_error_for_an_unresolvable_host() {
        let resolver = TokioResolver;
        let result = resolver
            .resolve("this-host-does-not-exist.invalid", 443)
            .await;
        assert!(result.is_err(), "expected a resolution error");
    }
}

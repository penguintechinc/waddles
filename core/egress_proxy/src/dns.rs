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

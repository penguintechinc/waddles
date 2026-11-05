//! SSRF egress address policy for the generic WebSocket / poll transports.
//!
//! A thin adapter over `bundle_host_http::guarded` -- the ONE implementation
//! of the denylist, resolve-then-connect pinning, scheme/userinfo checks and
//! typed errors shared with svc_action/svc_process. The endpoint host comes
//! from overlay config (untrusted): it is resolved there, every address is
//! checked, and the connection is made to the PINNED address (never
//! re-resolved by the client), closing DNS-rebind. Loopback, link-local,
//! metadata, CGNAT, private, multicast, reserved and documentation ranges
//! are refused unless the EXACT ip is in the explicit allowlist the host
//! config grants. The previously mirrored denylist was deleted; nothing here
//! may re-implement range logic.

use std::net::{IpAddr, SocketAddr};
use std::time::Duration;

/// Resolves the URL's host, requires EVERY address to be permitted (or
/// exactly allowlisted), and returns the address to pin the connection to.
/// The error text is the guard's class-only message (never host/address).
pub(crate) async fn resolve_pinned(
    url: &reqwest::Url,
    allow: &[IpAddr],
) -> Result<SocketAddr, String> {
    bundle_host_http::guarded::resolve_pinned(url, allow)
        .await
        .map_err(|e| e.to_string())
}

/// One-shot client pinned to the validated address (redirects and ambient
/// proxies off), built by the shared guard. Poll's `pinned_client` is the
/// intended caller once the in-flight connector work lands.
#[allow(dead_code)]
pub(crate) async fn pinned_client(
    url: &reqwest::Url,
    allow: &[IpAddr],
    timeout: Duration,
) -> Result<reqwest::Client, String> {
    bundle_host_http::guarded::pinned_client(url, allow, timeout)
        .await
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn pinned_resolution_refuses_and_allowlists() {
        let u = |s: &str| reqwest::Url::parse(s).unwrap();
        assert!(resolve_pinned(&u("https://169.254.169.254/x"), &[])
            .await
            .is_err());
        assert!(resolve_pinned(&u("https://10.0.0.5/x"), &[]).await.is_err());
        assert!(resolve_pinned(&u("https://[::ffff:10.0.0.5]/x"), &[])
            .await
            .is_err());
        assert!(resolve_pinned(&u("https://localhost/x"), &[])
            .await
            .is_err());
        let lo: IpAddr = "127.0.0.1".parse().unwrap();
        assert_eq!(
            resolve_pinned(&u("http://127.0.0.1:9/x"), &[lo])
                .await
                .unwrap()
                .port(),
            9
        );
        assert!(resolve_pinned(&u("https://8.8.8.8/x"), &[]).await.is_ok());
    }

    #[tokio::test]
    async fn pinned_client_refuses_forbidden_and_builds_for_public() {
        let u = |s: &str| reqwest::Url::parse(s).unwrap();
        let t = Duration::from_secs(1);
        assert!(pinned_client(&u("https://169.254.169.254/"), &[], t)
            .await
            .is_err());
        assert!(pinned_client(&u("https://8.8.8.8/"), &[], t).await.is_ok());
    }
}

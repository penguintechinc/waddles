//! Producer-side user detokenization for host-executed platform tools.
//!
//! A tool call names its target by the TOKENIZED user UUID (the only identifier a bundle ever
//! holds). A platform API needs the raw numeric platform id (e.g. Twitch `user_id`), so BEFORE
//! queueing the producer op the host resolves UUID -> platform id through hub-api's
//! `IdentityService.ResolvePlatformUser` (community-scoped, machine JWT scope
//! [`PLATFORM_USER_SCOPE`]). The raw id exists only in the queued, MAC-signed envelope (the
//! consumer needs it); it is never logged, never returned to a guest and never cached. Hub-api
//! answers any miss with one `NOT_FOUND`, so this resolver sends exactly one RPC per call and
//! never probes. Every failure refuses the call (fail closed) -- there is no fallback id.

use std::future::Future;
use std::pin::Pin;

use hub_client::{HubClient, HubClientError};

/// Machine-JWT scope hub-api requires for `ResolvePlatformUser`.
pub const PLATFORM_USER_SCOPE: &str = "identity:platform-user:resolve";

/// Longest raw platform user id accepted from the resolver.
const MAX_PLATFORM_ID_LEN: usize = 64;

/// Why a user could not be detokenized. Carries no identifiers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum ResolveError {
    /// Unknown uuid / not a member of this community / platform mismatch (one answer, no oracle).
    #[error("user is not resolvable in this community")]
    NotFound,
    /// The service identity lacks the scope / tenant (operator problem, not retryable).
    #[error("detokenization denied")]
    Denied,
    /// Hub-api rate limited the call (retryable).
    #[error("detokenization rate limited")]
    RateLimited,
    /// Malformed input or answer.
    #[error("invalid detokenization request")]
    Invalid,
    /// Transport / deadline / circuit open (retryable).
    #[error("detokenization unavailable")]
    Unavailable,
}

/// Boxed future so the seam is object-safe.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Resolves one tokenized user uuid to the raw platform user id, scoped to a community.
pub trait PlatformUserResolver: Send + Sync {
    /// The raw id of `uuid` (canonical lowercase) on `platform` for `(tenant, community)`.
    fn resolve<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        platform: &'a str,
        uuid: &'a str,
    ) -> BoxFuture<'a, Result<String, ResolveError>>;
}

/// `true` for a platform id the producer may put on the wire: bounded, no control characters,
/// no whitespace (it becomes a query/path/body value verbatim).
#[must_use]
pub fn is_wire_safe_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= MAX_PLATFORM_ID_LEN
        && id.bytes().all(|b| b.is_ascii_graphic())
}

fn classify(e: &HubClientError) -> ResolveError {
    match e {
        HubClientError::Grpc(s) => match s.code() {
            tonic::Code::NotFound => ResolveError::NotFound,
            tonic::Code::PermissionDenied | tonic::Code::Unauthenticated => ResolveError::Denied,
            tonic::Code::ResourceExhausted => ResolveError::RateLimited,
            tonic::Code::InvalidArgument | tonic::Code::FailedPrecondition => ResolveError::Invalid,
            _ => ResolveError::Unavailable,
        },
        _ => ResolveError::Unavailable,
    }
}

/// Production resolver over a [`HubClient`] connected with [`PLATFORM_USER_SCOPE`].
pub struct HubPlatformUserResolver {
    client: HubClient,
}

impl HubPlatformUserResolver {
    /// Wraps a client whose machine-JWT scope is [`PLATFORM_USER_SCOPE`].
    #[must_use]
    pub fn new(client: HubClient) -> Self {
        Self { client }
    }
}

impl PlatformUserResolver for HubPlatformUserResolver {
    fn resolve<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        platform: &'a str,
        uuid: &'a str,
    ) -> BoxFuture<'a, Result<String, ResolveError>> {
        Box::pin(async move {
            let resp = self
                .client
                .resolve_platform_user(
                    tenant_id.to_string(),
                    i64::from(community_id),
                    platform.to_string(),
                    vec![uuid.to_string()],
                )
                .await
                .map_err(|e| classify(&e))?;
            match resp.users.as_slice() {
                [u] if u.uuid == uuid && is_wire_safe_id(&u.platform_user_id) => {
                    Ok(u.platform_user_id.clone())
                }
                _ => Err(ResolveError::Invalid),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wire_safe_ids_are_bounded_printable_and_non_empty() {
        assert!(is_wire_safe_id("141981764"));
        for bad in ["", " 1", "1 2", "a\nb", "\u{e9}", &"9".repeat(65)] {
            assert!(!is_wire_safe_id(bad), "{bad:?}");
        }
    }

    #[test]
    fn grpc_codes_map_to_distinct_classes() {
        let g = |c| HubClientError::Grpc(tonic::Status::new(c, "x"));
        assert_eq!(classify(&g(tonic::Code::NotFound)), ResolveError::NotFound);
        assert_eq!(classify(&g(tonic::Code::PermissionDenied)), ResolveError::Denied);
        assert_eq!(classify(&g(tonic::Code::ResourceExhausted)), ResolveError::RateLimited);
        assert_eq!(classify(&g(tonic::Code::InvalidArgument)), ResolveError::Invalid);
        assert_eq!(classify(&g(tonic::Code::Unavailable)), ResolveError::Unavailable);
    }
}

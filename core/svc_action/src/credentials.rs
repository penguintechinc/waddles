//! Per-tenant Discord bot credential resolution for the action path
//! (#500 data-plane half; schema contract:
//! `docs/superpowers/specs/2026-09-29-guild-binding-contract.md` Sec2).
//!
//! **Fail-closed, mandatory, no exceptions:**
//! - Tenant `0` (the reserved global/default tenant) -> the platform
//!   bot's credentials, sourced from the cluster Secret
//!   `waddlebot-platform-credentials` (#478).
//! - Any other tenant -> that tenant's OWN `tenant_platform_credentials`
//!   row, resolved through hub-api's internal credential-resolution
//!   endpoint (contract Sec2: "the data plane never reads credentials off
//!   a table directly"). **There is no fallback to the platform bot** --
//!   a non-global tenant with no configured Discord app is a hard error,
//!   every time, per the owner's 2026-09-29 correction.
//!
//! Mirrors the contract's own `resolve_credentials(tenant_id, platform)`
//! pseudocode exactly -- see [`CredentialResolver::resolve`].
//!
//! **Known gap, not silently bridged.** Hub-api's internal
//! KeyService/IdentityService (PR #455) is the intended transport but is
//! not confirmed merged as of this branch. [`HubApiCredentialResolver`] is
//! the documented seam: it exists, implements [`CredentialResolver`], and
//! returns [`CredentialError::TransportUnavailable`] today rather than
//! silently degrading -- wiring in the real gRPC client is the remaining
//! work tracked in PR #500's data-plane half.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;

/// The reserved global/default tenant id (contract Sec2's
/// `tenants.is_global(tenant_id)`) -- uses the platform bot, never its own
/// `tenant_platform_credentials` row (DB-trigger-enforced on the hub-api
/// side too: `trg_reject_global_tenant_credentials`).
pub const GLOBAL_TENANT_ID: &str = "0";

/// One resolved Discord bot's credentials -- never logged or `Debug`-
/// printed in full (masked token hygiene, `rules/critical-rules.md` Token
/// & Secret Hygiene).
#[derive(Clone)]
pub struct BotCredentials {
    pub client_id: String,
    pub bot_token: String,
}

impl std::fmt::Debug for BotCredentials {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BotCredentials")
            .field("client_id", &self.client_id)
            .field("bot_token", &"tok_****")
            .finish()
    }
}

/// Why credential resolution failed -- every variant is a hard error for
/// the caller (send fails), never a fallback to the platform bot.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CredentialError {
    /// A non-global tenant has no active `tenant_platform_credentials`
    /// row -- contract Sec2: "NEVER fall back to the platform bot."
    #[error("tenant {0} has no configured Discord application -- no platform-bot fallback")]
    TenantAppNotConfigured(String),
    /// The platform Secret (`waddlebot-platform-credentials`) could not
    /// be read -- tenant 0 only; also a hard error, since tenant 0 has no
    /// per-tenant row to fall back to either.
    #[error("platform credentials unavailable: {0}")]
    PlatformCredentialsUnavailable(String),
    /// The hub-api internal credential-resolution transport (PR #455) is
    /// not wired in yet -- documented seam, see module doc.
    #[error("hub-api credential-resolution transport unavailable: {0}")]
    TransportUnavailable(String),
}

impl CredentialError {
    pub fn reason(&self) -> &'static str {
        match self {
            CredentialError::TenantAppNotConfigured(_) => "tenant_app_not_configured",
            CredentialError::PlatformCredentialsUnavailable(_) => {
                "platform_credentials_unavailable"
            }
            CredentialError::TransportUnavailable(_) => "transport_unavailable",
        }
    }
}

/// Object-safe async resolver seam (same manually-boxed-future pattern as
/// `crate::flags::FeatureFlag`) -- lets [`resolve_credentials`] be
/// exercised against a fake in tests with no live hub-api/Secret access.
pub trait CredentialResolver: Send + Sync {
    fn resolve<'a>(
        &'a self,
        tenant_id: &'a str,
        platform: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<BotCredentials, CredentialError>> + Send + 'a>>;
}

/// Reads the platform bot's credentials from the cluster Secret
/// `waddlebot-platform-credentials` (#478) -- used only for
/// [`GLOBAL_TENANT_ID`]. The concrete Secret-mount read (env vars/mounted
/// file, matching `core/svc_action/src/crypto.rs`'s existing secret-read
/// conventions) is injected via `read_client_id`/`read_bot_token` so this
/// type stays unit-testable without a live cluster.
pub struct PlatformCredentialResolver {
    read_client_id: Box<dyn Fn() -> Result<String, String> + Send + Sync>,
    read_bot_token: Box<dyn Fn() -> Result<String, String> + Send + Sync>,
}

impl PlatformCredentialResolver {
    pub fn new(
        read_client_id: impl Fn() -> Result<String, String> + Send + Sync + 'static,
        read_bot_token: impl Fn() -> Result<String, String> + Send + Sync + 'static,
    ) -> Self {
        Self {
            read_client_id: Box::new(read_client_id),
            read_bot_token: Box::new(read_bot_token),
        }
    }

    /// Standard `waddlebot-platform-credentials` Secret mount, `WADDLEBOT_PLATFORM_CLIENT_ID`/
    /// `WADDLEBOT_PLATFORM_BOT_TOKEN` env vars -- the K8s-standard shape
    /// every other cluster Secret in this crate uses (never a CLI arg,
    /// per `rules/critical-rules.md` Token & Secret Hygiene).
    pub fn from_env() -> Self {
        Self::new(
            || {
                std::env::var("WADDLEBOT_PLATFORM_CLIENT_ID")
                    .map_err(|e| format!("WADDLEBOT_PLATFORM_CLIENT_ID: {e}"))
            },
            || {
                std::env::var("WADDLEBOT_PLATFORM_BOT_TOKEN")
                    .map_err(|e| format!("WADDLEBOT_PLATFORM_BOT_TOKEN: {e}"))
            },
        )
    }
}

impl CredentialResolver for PlatformCredentialResolver {
    fn resolve<'a>(
        &'a self,
        _tenant_id: &'a str,
        _platform: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<BotCredentials, CredentialError>> + Send + 'a>> {
        Box::pin(async move {
            let client_id =
                (self.read_client_id)().map_err(CredentialError::PlatformCredentialsUnavailable)?;
            let bot_token =
                (self.read_bot_token)().map_err(CredentialError::PlatformCredentialsUnavailable)?;
            Ok(BotCredentials {
                client_id,
                bot_token,
            })
        })
    }
}

/// The documented seam for hub-api's internal KeyService/IdentityService
/// (PR #455) -- see module doc "Known gap". Returns
/// [`CredentialError::TransportUnavailable`] until that client is wired
/// in; never falls back to the platform bot in the meantime (that would
/// violate the fail-closed contract for exactly the tenants this type
/// exists to serve).
pub struct HubApiCredentialResolver;

impl CredentialResolver for HubApiCredentialResolver {
    fn resolve<'a>(
        &'a self,
        tenant_id: &'a str,
        _platform: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<BotCredentials, CredentialError>> + Send + 'a>> {
        Box::pin(async move {
            Err(CredentialError::TransportUnavailable(format!(
                "hub-api internal KeyService/IdentityService (PR #455) not yet wired in -- tenant {tenant_id}"
            )))
        })
    }
}

/// Fixed-map test double -- a non-global tenant absent from the map
/// resolves to [`CredentialError::TenantAppNotConfigured`], exercising
/// the fail-closed path with no live hub-api dependency.
#[derive(Default)]
pub struct StaticCredentialResolver {
    pub by_tenant: HashMap<String, BotCredentials>,
}

impl CredentialResolver for StaticCredentialResolver {
    fn resolve<'a>(
        &'a self,
        tenant_id: &'a str,
        _platform: &'a str,
    ) -> Pin<Box<dyn Future<Output = Result<BotCredentials, CredentialError>> + Send + 'a>> {
        let result = self
            .by_tenant
            .get(tenant_id)
            .cloned()
            .ok_or_else(|| CredentialError::TenantAppNotConfigured(tenant_id.to_string()));
        Box::pin(async move { result })
    }
}

/// Contract Sec2's `resolve_credentials(tenant_id, platform)` verbatim:
/// tenant 0 -> `platform`; every other tenant -> `tenant`, with **no
/// fallback** from `tenant`'s failure back to `platform`.
pub async fn resolve_credentials(
    tenant_id: &str,
    platform_name: &str,
    platform: &dyn CredentialResolver,
    tenant: &dyn CredentialResolver,
) -> Result<BotCredentials, CredentialError> {
    if tenant_id == GLOBAL_TENANT_ID {
        platform.resolve(tenant_id, platform_name).await
    } else {
        // NEVER `.or_else(|_| platform.resolve(...))` here -- a missing
        // tenant app is a hard error, per contract Sec2 and the owner's
        // 2026-09-29 fail-closed correction.
        tenant.resolve(tenant_id, platform_name).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn creds(client_id: &str, bot_token: &str) -> BotCredentials {
        BotCredentials {
            client_id: client_id.to_string(),
            bot_token: bot_token.to_string(),
        }
    }

    #[tokio::test]
    async fn global_tenant_uses_platform_credentials() {
        let platform = StaticCredentialResolver {
            by_tenant: HashMap::from([(
                GLOBAL_TENANT_ID.to_string(),
                creds("plat-id", "plat-tok"),
            )]),
        };
        let tenant = StaticCredentialResolver::default();

        let resolved = resolve_credentials(GLOBAL_TENANT_ID, "discord", &platform, &tenant)
            .await
            .unwrap();
        assert_eq!(resolved.client_id, "plat-id");
    }

    #[tokio::test]
    async fn non_global_tenant_uses_its_own_credentials() {
        let platform = StaticCredentialResolver {
            by_tenant: HashMap::from([(
                GLOBAL_TENANT_ID.to_string(),
                creds("plat-id", "plat-tok"),
            )]),
        };
        let tenant = StaticCredentialResolver {
            by_tenant: HashMap::from([(
                "tenant-a".to_string(),
                creds("tenant-a-id", "tenant-a-tok"),
            )]),
        };

        let resolved = resolve_credentials("tenant-a", "discord", &platform, &tenant)
            .await
            .unwrap();
        assert_eq!(resolved.client_id, "tenant-a-id");
        assert_ne!(resolved.client_id, "plat-id");
    }

    #[tokio::test]
    async fn non_global_tenant_without_app_fails_closed_no_platform_fallback() {
        let platform = StaticCredentialResolver {
            by_tenant: HashMap::from([(
                GLOBAL_TENANT_ID.to_string(),
                creds("plat-id", "plat-tok"),
            )]),
        };
        // tenant-b has no row at all.
        let tenant = StaticCredentialResolver::default();

        let err = resolve_credentials("tenant-b", "discord", &platform, &tenant)
            .await
            .unwrap_err();
        assert_eq!(
            err,
            CredentialError::TenantAppNotConfigured("tenant-b".to_string())
        );
        assert_eq!(err.reason(), "tenant_app_not_configured");
    }

    #[tokio::test]
    async fn hub_api_resolver_seam_fails_closed_not_silently_unimplemented() {
        let resolver = HubApiCredentialResolver;
        let err = resolver.resolve("tenant-c", "discord").await.unwrap_err();
        assert_eq!(err.reason(), "transport_unavailable");
    }

    #[tokio::test]
    async fn platform_resolver_surfaces_missing_secret_as_hard_error() {
        let resolver = PlatformCredentialResolver::new(
            || Err("missing".to_string()),
            || Ok("tok".to_string()),
        );
        let err = resolver
            .resolve(GLOBAL_TENANT_ID, "discord")
            .await
            .unwrap_err();
        assert_eq!(err.reason(), "platform_credentials_unavailable");
    }

    #[test]
    fn bot_credentials_debug_never_leaks_token() {
        let c = creds("id-1", "super-secret-token-value");
        let debug = format!("{c:?}");
        assert!(!debug.contains("super-secret-token-value"));
        assert!(debug.contains("tok_****"));
    }
}

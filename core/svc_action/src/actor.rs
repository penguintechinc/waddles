//! Host-verified ACTOR resolution for the action stage (twin of `svc_process`'s
//! `identity::resolve_actor_scopes`, #976).
//!
//! A scope-restricted tool (`required_scope`) may only run for a caller whose triggering
//! event's platform account is an ACTIVE member of the invoke's `(tenant, community)` holding
//! the scope through its community role. The actor is derived from the MAC-verified delivered
//! envelope ([`crate::capabilities::InvokeScope::origin_user_id`]) and the PII-free
//! `community_member_identities` view (read-only `waddles_bundle_reader`), never from guest
//! input. Every failure is an explicit [`ActorError`]; there is no default, guessed or
//! pseudonym actor.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend, Statement, Value};

/// Boxed future so the directory seam is object-safe without `async_trait`.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Bound on one directory read; a slow directory fails closed as [`ActorError::Unavailable`].
pub const DIRECTORY_QUERY_TIMEOUT: Duration = Duration::from_secs(3);

/// Why an actor could not be resolved. Carries no identifiers (PII-free).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ActorError {
    /// No active member row for this platform account in this community.
    #[error("not an active member of this community")]
    NotAMember,
    /// An active member without a linked user uuid or without a role.
    #[error("member has no linked identity or role")]
    NotLinked,
    /// The directory could not be read in time (retryable).
    #[error("identity directory unavailable")]
    Unavailable,
    /// A malformed or inconsistent directory answer.
    #[error("identity directory backend error")]
    Backend,
}

/// Resolves a platform account to its community `(user_uuid, role)` under a host-derived
/// `(tenant, community)`.
pub trait ActorDirectory: Send + Sync {
    /// The ACTIVE member's `(user_uuid, role)`; see [`ActorError`] for the refusals.
    fn member<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> BoxFuture<'a, Result<(String, String), ActorError>>;
}

/// Resolves the verified [`bundle_tool_call::ActorContext`] (uuid + the role's scopes).
///
/// # Errors
/// [`ActorError`] when the account is not an active, linked member with a role.
pub async fn resolve_actor_scopes(
    directory: &Arc<dyn ActorDirectory>,
    tenant_id: i32,
    community_id: i32,
    platform: &str,
    platform_user_id: &str,
) -> Result<bundle_tool_call::ActorContext, ActorError> {
    let (user, role) = directory
        .member(tenant_id, community_id, platform, platform_user_id)
        .await?;
    Ok(bundle_tool_call::ActorContext::for_role(user, &role))
}

/// One row, `LIMIT 2` so a (constraint-forbidden) duplicate is detected, not first-wins.
const MEMBER_SQL: &str =
    "SELECT user_uuid::text AS user_uuid, role, is_active_member AS is_active \
     FROM community_member_identities \
     WHERE tenant_id = $1 AND community_id = $2 AND platform = $3 AND platform_user_id = $4 \
     LIMIT 2";

/// Production [`ActorDirectory`] over the read-only bundle-reader connection.
pub struct PgActorDirectory {
    db: DatabaseConnection,
}

impl PgActorDirectory {
    /// Wraps the `waddles_bundle_reader` connection the grant gate already holds.
    #[must_use]
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

impl ActorDirectory for PgActorDirectory {
    fn member<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> BoxFuture<'a, Result<(String, String), ActorError>> {
        Box::pin(async move {
            let stmt = Statement::from_sql_and_values(
                DbBackend::Postgres,
                MEMBER_SQL,
                vec![
                    Value::Int(Some(tenant_id)),
                    Value::Int(Some(community_id)),
                    Value::String(Some(platform.to_string())),
                    Value::String(Some(platform_user_id.to_string())),
                ],
            );
            let rows =
                match tokio::time::timeout(DIRECTORY_QUERY_TIMEOUT, self.db.query_all_raw(stmt))
                    .await
                {
                    Ok(Ok(rows)) => rows,
                    Ok(Err(e)) => {
                        tracing::error!(error = %e, "actor directory query failed");
                        return Err(ActorError::Backend);
                    }
                    Err(_) => return Err(ActorError::Unavailable),
                };
            let row = match rows.as_slice() {
                [] => return Err(ActorError::NotAMember),
                [row] => row,
                _ => return Err(ActorError::Backend),
            };
            let active: bool = row
                .try_get("", "is_active")
                .map_err(|_| ActorError::Backend)?;
            if !active {
                return Err(ActorError::NotAMember);
            }
            let user: Option<String> = row
                .try_get("", "user_uuid")
                .map_err(|_| ActorError::Backend)?;
            let role: Option<String> = row.try_get("", "role").map_err(|_| ActorError::Backend)?;
            match (user, role.map(|r| r.trim().to_string())) {
                (Some(u), Some(r)) if !r.is_empty() && uuid::Uuid::parse_str(&u).is_ok() => {
                    Ok((u, r))
                }
                _ => Err(ActorError::NotLinked),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};
    use std::collections::BTreeMap;

    fn row(user: Option<&str>, role: Option<&str>, active: bool) -> BTreeMap<String, Value> {
        BTreeMap::from([
            (
                "user_uuid".to_string(),
                Value::String(user.map(str::to_string)),
            ),
            ("role".to_string(), Value::String(role.map(str::to_string))),
            ("is_active".to_string(), Value::Bool(Some(active))),
        ])
    }

    const U: &str = "3f2b8c1e-9d4a-4c55-8a10-0a1b2c3d4e5f";

    async fn resolve(
        rows: Vec<Vec<BTreeMap<String, Value>>>,
    ) -> Result<bundle_tool_call::ActorContext, ActorError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results(rows)
            .into_connection();
        let dir: Arc<dyn ActorDirectory> = Arc::new(PgActorDirectory::new(db));
        resolve_actor_scopes(&dir, 7, 3, "twitch", "999").await
    }

    #[tokio::test]
    async fn moderator_gets_moderate_scope_and_member_does_not() {
        let m = resolve(vec![vec![row(Some(U), Some("moderator"), true)]]).await;
        let m = m.unwrap_or_else(|e| panic!("{e}"));
        assert!(m.has_scope("community:moderate"));
        let p = resolve(vec![vec![row(Some(U), Some("member"), true)]]).await;
        let p = p.unwrap_or_else(|e| panic!("{e}"));
        assert!(p.has_scope("community:member") && !p.has_scope("community:moderate"));
    }

    #[tokio::test]
    async fn every_unresolved_state_is_an_explicit_refusal() {
        assert_eq!(resolve(vec![vec![]]).await, Err(ActorError::NotAMember));
        assert_eq!(
            resolve(vec![vec![row(Some(U), Some("moderator"), false)]]).await,
            Err(ActorError::NotAMember)
        );
        assert_eq!(
            resolve(vec![vec![row(None, Some("moderator"), true)]]).await,
            Err(ActorError::NotLinked)
        );
        assert_eq!(
            resolve(vec![vec![row(Some(U), None, true)]]).await,
            Err(ActorError::NotLinked)
        );
        assert_eq!(
            resolve(vec![vec![row(Some("nope"), Some("moderator"), true)]]).await,
            Err(ActorError::NotLinked)
        );
        assert_eq!(
            resolve(vec![vec![
                row(Some(U), Some("moderator"), true),
                row(Some(U), Some("moderator"), true)
            ]])
            .await,
            Err(ActorError::Backend)
        );
    }
}

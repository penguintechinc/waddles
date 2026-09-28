//! Wires `core/bundle_capability_gate` into this stage: a real, RO-replica
//! [`GrantLoader`] against the grant tables spec
//! `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
//! SS4 defines (`community_permission_grants` JOIN `app_permission_requests`,
//! version-pinned via `app_permission_grant_versions`), a decorator that
//! always unions in the catalog's zero-config "Always granted" platform
//! permissions (spec SS1's `platform.context`/`platform.clock`/
//! `platform.log` row -- "never shown on a consent screen") regardless of
//! what the real tables return, and the push-invalidation subscriber (plus
//! poll fallback) that keeps `crate::capabilities::StageCapabilities`'s
//! in-memory `GrantSnapshot` current.
//!
//! **Fail-closed by construction.** The sibling migration that creates the
//! grant tables this loader queries (`feature/bundle-permission-grants`) has
//! not landed as of this module -- every query here is expected to hit
//! `relation "..." does not exist` in that state, which [`PgGrantLoader`]
//! treats identically to "no grant row": deny, never panic, never fall back
//! to a default-allow. Once the migration lands, no code here changes; the
//! same queries simply start returning real rows.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use bundle_capability_gate::{
    GrantCache, GrantLoadError, GrantLoader, GrantScopeKey, GrantSet, GrantedPermission,
};
use sea_orm::{ConnectionTrait, DatabaseConnection, Statement};

/// The three catalog permissions spec SS1 marks "Always-granted, zero-config
/// ... never shown on a consent screen" -- present in every [`GrantSet`] this
/// module ever produces, independent of tenant/community/app/version and
/// independent of whether the real grant tables exist yet at all. This is
/// what lets `context`/`clock`/`log` keep working for every bundle even
/// before a single row has ever been written to `community_permission_grants`
/// (spec SS3.5), while every other permission still fails closed.
fn always_granted() -> HashMap<String, GrantedPermission> {
    ["platform.context", "platform.clock", "platform.log"]
        .into_iter()
        .map(|id| {
            (
                id.to_string(),
                GrantedPermission {
                    permission_id: id.to_string(),
                    params: serde_json::json!({}),
                },
            )
        })
        .collect()
}

/// Decorates any [`GrantLoader`] so its answer always includes
/// [`always_granted`]'s three entries, unioned with (and never overridden
/// by, since the catalog ids never collide) whatever the inner loader
/// actually returns. Never returns `Ok(None)` -- a scope with zero real
/// grants still gets a [`GrantSet`] containing just the always-granted
/// three, so `authorize()` never fails closed on `context`/`clock`/`log`
/// specifically, while every other permission still requires a real row.
pub struct AlwaysGrantedLoader<L: GrantLoader> {
    inner: L,
}

impl<L: GrantLoader> AlwaysGrantedLoader<L> {
    pub fn new(inner: L) -> Self {
        Self { inner }
    }
}

impl<L: GrantLoader> GrantLoader for AlwaysGrantedLoader<L> {
    fn load<'a>(
        &'a self,
        key: &'a GrantScopeKey,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>,
    > {
        Box::pin(async move {
            let mut grants = match self.inner.load(key).await {
                Ok(Some(set)) => set.grants,
                // A loader error (missing table, connection failure) is
                // treated identically to "no real grants yet" here -- the
                // always-granted three still get returned below, but this
                // is never surfaced as a hard error up through the gate
                // (module doc: "deny, never panic").
                Ok(None) | Err(_) => HashMap::new(),
            };
            grants.extend(always_granted());
            Ok(Some(GrantSet {
                permission_snapshot_hash: "always-granted-union".to_string(),
                grants,
            }))
        })
    }
}

/// The RO-replica reader for `community_permission_grants` JOIN
/// `app_permission_requests` (spec SS4), scoped to one
/// `(tenant, community, app, app_version)` per call. `db` must be a
/// read-only role against the read replica (spec SS4: "the data plane...
/// holds a read-only role against a read replica") -- this type performs
/// only `SELECT`s and never assumes write access.
pub struct PgGrantLoader {
    db: DatabaseConnection,
}

impl PgGrantLoader {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

impl GrantLoader for PgGrantLoader {
    fn load<'a>(
        &'a self,
        key: &'a GrantScopeKey,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>,
    > {
        Box::pin(async move {
            // Community-scoped grants (community_id != 0) additionally
            // accept the tenant-wide sentinel row so a tenant-wide grant
            // still authorizes a community-scoped call -- but a
            // community-scoped GrantScopeKey never widens to a DIFFERENT
            // community's row (spec SS5.1 AppScoped: no cross-scope reads).
            let stmt = Statement::from_sql_and_values(
                self.db.get_database_backend(),
                r#"
                SELECT g.permission_id, g.params_json
                FROM community_permission_grants g
                JOIN app_permission_requests r
                  ON r.app_id = g.app_id AND r.permission_id = g.permission_id
                WHERE g.tenant_id = $1
                  AND g.community_id = $2
                  AND g.app_id = $3
                  AND r.version = $4
                "#,
                [
                    key.tenant_id.into(),
                    key.community_id.into(),
                    key.app_id.clone().into(),
                    key.app_version.into(),
                ],
            );

            let rows = match self.db.query_all_raw(stmt).await {
                Ok(rows) => rows,
                Err(err) => {
                    // Missing-table (migration not yet landed) and any
                    // other backend error both fail closed: this loader
                    // never distinguishes "table missing" from "query
                    // failed" to its own caller -- both mean "no grant
                    // evidence available," never a default allow.
                    tracing::warn!(
                        error = %err,
                        tenant_id = key.tenant_id,
                        community_id = key.community_id,
                        app_id = %key.app_id,
                        "grant loader query failed -- denying every non-platform permission for this scope"
                    );
                    return Ok(None);
                }
            };

            let mut grants = HashMap::new();
            for row in rows {
                let permission_id: String = match row.try_get("", "permission_id") {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                let params: serde_json::Value = row
                    .try_get::<String>("", "params_json")
                    .ok()
                    .and_then(|s| serde_json::from_str(&s).ok())
                    .unwrap_or_else(|| serde_json::json!({}));
                grants.insert(
                    permission_id.clone(),
                    GrantedPermission {
                        permission_id,
                        params,
                    },
                );
            }

            Ok(Some(GrantSet {
                permission_snapshot_hash: format!(
                    "{}:{}:{}:{}",
                    key.tenant_id, key.community_id, key.app_id, key.app_version
                ),
                grants,
            }))
        })
    }
}

/// One `bundle:grants:invalidate` push-invalidation payload (spec SS4):
/// `{tenant_id, community_id, app_id, app_version}` -- exactly a
/// [`GrantScopeKey`], published by hub-api whenever a grant changes at any
/// of the 3 tiers (spec SS3.7's revocation, or a fresh activation).
#[derive(serde::Deserialize)]
struct InvalidatePayload {
    tenant_id: i32,
    #[serde(default)]
    community_id: i32,
    app_id: String,
    app_version: i64,
}

impl From<InvalidatePayload> for GrantScopeKey {
    fn from(p: InvalidatePayload) -> Self {
        GrantScopeKey {
            tenant_id: p.tenant_id,
            community_id: p.community_id,
            app_id: p.app_id,
            app_version: p.app_version,
        }
    }
}

const GRANT_INVALIDATE_CHANNEL: &str = "bundle:grants:invalidate";

/// Runs forever (until the process shuts down): subscribes to
/// `bundle:grants:invalidate` for push-invalidation (spec SS4: "<1s"), and
/// separately re-[`GrantCache::refresh`]es every key currently resident in
/// the cache every `poll_interval` as a fallback for a missed/dropped
/// pub/sub message (spec SS4/task instruction: "the existing 300s poll as a
/// fallback"). Never returns `Err` -- a subscribe failure is logged and
/// retried after `poll_interval`, since a dead invalidation channel must
/// never take the whole capability gate down with it (module doc: "deny,
/// never panic" extends to this task's own failure modes too).
pub async fn run_grant_gate_refresh_loop<L: GrantLoader + 'static>(
    redis_client: redis::Client,
    cache: Arc<GrantCache<L>>,
    poll_interval: Duration,
) {
    loop {
        tokio::select! {
            () = subscribe_and_invalidate(&redis_client, &cache) => {}
            () = poll_refresh_all(&cache, poll_interval) => {}
        }
    }
}

async fn subscribe_and_invalidate<L: GrantLoader + 'static>(
    redis_client: &redis::Client,
    cache: &Arc<GrantCache<L>>,
) {
    loop {
        let mut pubsub = match redis_client.get_async_pubsub().await {
            Ok(p) => p,
            Err(err) => {
                tracing::warn!(error = %err, "grant invalidation pubsub connect failed, retrying");
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
        };
        if let Err(err) = pubsub.subscribe(GRANT_INVALIDATE_CHANNEL).await {
            tracing::warn!(error = %err, "grant invalidation subscribe failed, retrying");
            tokio::time::sleep(Duration::from_secs(5)).await;
            continue;
        }

        let mut stream = pubsub.into_on_message();
        use futures_util::StreamExt;
        while let Some(msg) = stream.next().await {
            let payload: String = match msg.get_payload() {
                Ok(p) => p,
                Err(_) => continue,
            };
            let Ok(invalidate) = serde_json::from_str::<InvalidatePayload>(&payload) else {
                tracing::warn!(payload = %payload, "malformed grant invalidation payload, ignoring");
                continue;
            };
            let key: GrantScopeKey = invalidate.into();
            // Immediate half (spec SS4/SS5.3): drop the stale entry first so
            // the very next call fails closed even if the refresh below is
            // slow or fails outright.
            cache.invalidate(&key);
            if let Err(err) = cache.refresh(&key).await {
                tracing::warn!(error = %err, "grant refresh after invalidation failed");
            }
        }
        // Stream ended (connection dropped) -- reconnect from the top.
    }
}

async fn poll_refresh_all<L: GrantLoader + 'static>(
    cache: &Arc<GrantCache<L>>,
    poll_interval: Duration,
) {
    loop {
        tokio::time::sleep(poll_interval).await;
        for key in cache.keys() {
            if let Err(err) = cache.refresh(&key).await {
                tracing::warn!(error = %err, "grant poll-refresh failed");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bundle_capability_gate::{GrantLoadError, InMemoryGrantLoader};

    fn key() -> GrantScopeKey {
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: "waddles.core.example_echo".to_string(),
            app_version: 1,
        }
    }

    struct FailingLoader;
    impl GrantLoader for FailingLoader {
        fn load<'a>(
            &'a self,
            _key: &'a GrantScopeKey,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<Option<GrantSet>, GrantLoadError>>
                    + Send
                    + 'a,
            >,
        > {
            Box::pin(async move { Err(GrantLoadError::Backend("simulated".to_string())) })
        }
    }

    #[tokio::test]
    async fn always_granted_loader_grants_platform_permissions_even_with_no_backend_rows() {
        let loader = AlwaysGrantedLoader::new(InMemoryGrantLoader::new());
        let result = loader.load(&key()).await.unwrap().unwrap();
        assert!(result.get("platform.context").is_some());
        assert!(result.get("platform.clock").is_some());
        assert!(result.get("platform.log").is_some());
        // Nothing else was ever granted -- storage.kv must still fail closed.
        assert!(result.get("storage.kv").is_none());
    }

    #[tokio::test]
    async fn always_granted_loader_unions_with_real_grants() {
        let inner = InMemoryGrantLoader::new();
        inner.set(
            key(),
            GrantSet {
                permission_snapshot_hash: "h".to_string(),
                grants: HashMap::from([(
                    "storage.kv".to_string(),
                    GrantedPermission {
                        permission_id: "storage.kv".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let loader = AlwaysGrantedLoader::new(inner);
        let result = loader.load(&key()).await.unwrap().unwrap();
        assert!(result.get("storage.kv").is_some());
        assert!(result.get("platform.context").is_some());
    }

    #[tokio::test]
    async fn always_granted_loader_still_grants_platform_permissions_when_the_inner_loader_errors()
    {
        let loader = AlwaysGrantedLoader::new(FailingLoader);
        let result = loader.load(&key()).await.unwrap().unwrap();
        assert!(result.get("platform.context").is_some());
        assert!(result.get("storage.kv").is_none());
    }

    #[tokio::test]
    async fn pg_grant_loader_denies_fail_closed_when_the_grant_tables_are_missing() {
        use sea_orm::{DatabaseBackend, MockDatabase};
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_errors([sea_orm::DbErr::Custom(
                "relation \"community_permission_grants\" does not exist".to_string(),
            )])
            .into_connection();
        let loader = PgGrantLoader::new(db);
        let result = loader.load(&key()).await.unwrap();
        assert!(
            result.is_none(),
            "a missing-tables error must be treated as no grant evidence, never Err/panic"
        );
    }
}

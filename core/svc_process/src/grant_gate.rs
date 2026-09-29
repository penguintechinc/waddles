//! Wires `core/bundle_capability_gate` into this stage: a real, RO-replica
//! [`GrantLoader`] against the grant tables migration
//! `alembic/versions/0032_bundle_permission_grants.py`
//! (`feature/bundle-permission-grants`, PR #432) actually created
//! (`community_permission_grants` JOIN `app_permission_requests` JOIN
//! `app_versions` -- see [`PgGrantLoader::load`]'s doc for why the third
//! join exists), a decorator that always unions in the catalog's
//! zero-config "Always granted" platform permissions (spec SS1's
//! `platform.context`/`platform.clock`/`platform.log` row -- "never shown
//! on a consent screen") regardless of what the real tables return, and the
//! push-invalidation subscriber (plus poll fallback) that keeps
//! `crate::capabilities::StageCapabilities`'s in-memory `GrantSnapshot`
//! current. [`build_production_gate`] is the single call site every
//! production entry point (`crate::lib`/`crate::source_supervisor`) uses to
//! build a [`bundle_capability_gate::CapabilityGate`] and, when a Valkey
//! client is available, spawn its refresh loop -- see that function's doc.
//!
//! **Fail-closed on any query error**, not just a missing table: any
//! backend error (including the now-landed migration's own transient
//! failures) is treated identically to "no grant row" by [`PgGrantLoader`]:
//! deny, never panic, never fall back to a default-allow.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use bundle_capability_gate::{
    CapabilityGate, GrantCache, GrantLoadError, GrantLoader, GrantScopeKey, GrantSet,
    GrantedPermission, InMemoryMembership, InMemoryQuotaLedger,
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
/// `app_permission_requests` (migration `0032_bundle_permission_grants`,
/// PR #432), scoped to one `(tenant, community, app, app_version)` per
/// call. `db` must be a read-only role against the read replica
/// (`waddles_bundle_reader`, the same migration's own grant) -- this type
/// performs only `SELECT`s and never assumes write access.
pub struct PgGrantLoader {
    db: DatabaseConnection,
}

impl PgGrantLoader {
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }
}

impl GrantLoader for PgGrantLoader {
    /// **Why the `app_versions` join exists:** [`GrantScopeKey::app_version`]
    /// is `app_versions.id` (a `BIGSERIAL`, the same numeric id
    /// `app_active_versions.version_id` stores -- `bundle_active_set::
    /// entities::app_versions`), but `app_permission_requests.version` (PR
    /// #432's migration 0032) is the semver TEXT column every other grant
    /// table keys its version-pinning on. Comparing `r.version = $4`
    /// directly (an earlier draft of this loader) would compare TEXT to
    /// BIGINT and either fail to bind or never match -- this join
    /// translates the numeric id to that same TEXT value first, exactly
    /// once, before the real comparison.
    fn load<'a>(
        &'a self,
        key: &'a GrantScopeKey,
    ) -> std::pin::Pin<
        Box<dyn std::future::Future<Output = Result<Option<GrantSet>, GrantLoadError>> + Send + 'a>,
    > {
        Box::pin(async move {
            // `g.revoked_at IS NULL`: a revoked grant row is never deleted
            // (`bundle_permission_service.deactivate_permission` sets
            // `revoked_by`/`revoked_at` instead, spec Sec3.7) -- omitting
            // this filter would keep authorizing a permission the community
            // admin already revoked. `params_json::text`: cast the JSONB
            // column to TEXT in SQL so `try_get::<String>` below never
            // depends on whether the `sea-orm`/`sqlx` build enables native
            // JSON decoding for this raw-SQL path.
            let stmt = Statement::from_sql_and_values(
                self.db.get_database_backend(),
                r#"
                SELECT g.permission_id, g.params_json::text AS params_json
                FROM community_permission_grants g
                JOIN app_versions v
                  ON v.id = $4 AND v.app_id = $3
                JOIN app_permission_requests r
                  ON r.app_id = g.app_id
                 AND r.permission_id = g.permission_id
                 AND r.version = v.version
                WHERE g.tenant_id = $1
                  AND g.community_id = $2
                  AND g.app_id = $3
                  AND g.revoked_at IS NULL
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

/// One `bundle:grants:invalidate` entry as hub-api's
/// `bundle_permission_service._publish_invalidation`
/// (`hub_api/services/bundle_permission_service.py`) actually `XADD`s it --
/// field names/types matched exactly to that call: `tenant_id`/
/// `community_id`/`app_id` are the scope, `version` is the semver TEXT
/// (`app_permission_requests.version`), never the numeric `app_versions.id`
/// [`GrantScopeKey::app_version`] uses, so this payload alone can't
/// reconstruct one. [`invalidate_matching`] below invalidates every cached
/// key for this `(tenant_id, community_id, app_id)` regardless of
/// `app_version` instead of attempting a version-string-to-id lookup on the
/// hot invalidation path.
#[derive(Debug, PartialEq, Eq)]
struct InvalidatePayload {
    tenant_id: i32,
    community_id: i32,
    app_id: String,
}

/// Parses one `XREAD`/`XRANGE` stream entry's field map into an
/// [`InvalidatePayload`] -- `None` on any missing/unparseable required
/// field, treated by the caller identically to a malformed pub/sub payload
/// (log and ignore, never panic).
fn parse_invalidate_fields(fields: &HashMap<String, String>) -> Option<InvalidatePayload> {
    Some(InvalidatePayload {
        tenant_id: fields.get("tenant_id")?.parse().ok()?,
        community_id: fields.get("community_id")?.parse().ok()?,
        app_id: fields.get("app_id")?.clone(),
    })
}

/// Invalidates (and eagerly re-[`GrantCache::refresh`]es) every cached key
/// matching `payload`'s `(tenant_id, community_id, app_id)`, regardless of
/// `app_version` -- see [`InvalidatePayload`]'s doc for why the event alone
/// can't name one specific key. Over-invalidating a scope this stage never
/// actually holds a cache entry for is a no-op ([`GrantCache::keys`] simply
/// won't contain it); under-invalidating (missing the one entry that
/// changed) is the failure mode this function exists to prevent.
async fn invalidate_matching<L: GrantLoader + 'static>(
    cache: &Arc<GrantCache<L>>,
    payload: &InvalidatePayload,
) {
    for key in cache.keys() {
        if key.tenant_id == payload.tenant_id
            && key.community_id == payload.community_id
            && key.app_id == payload.app_id
        {
            // Immediate half (spec SS4/SS5.3): drop the stale entry first so
            // the very next call fails closed even if the refresh below is
            // slow or fails outright.
            cache.invalidate(&key);
            if let Err(err) = cache.refresh(&key).await {
                tracing::warn!(error = %err, "grant refresh after invalidation failed");
            }
        }
    }
}

const GRANT_INVALIDATE_STREAM: &str = "bundle:grants:invalidate";

/// Runs forever (until the process shuts down): tails `bundle:grants:
/// invalidate` (an `XADD`ed Valkey Stream -- hub-api's actual publish
/// mechanism, `bundle_permission_service.GRANT_INVALIDATION_STREAM`) for
/// push-invalidation (spec SS4: "<1s"), and separately re-[`GrantCache::
/// refresh`]es every key currently resident in the cache every
/// `poll_interval` as a fallback for a missed/dropped stream entry (spec
/// SS4/task instruction: "the existing 300s poll as a fallback"). Never
/// returns -- a read failure is logged and the stream connection retried,
/// since a dead invalidation channel must never take the whole capability
/// gate down with it (module doc: "deny, never panic" extends to this
/// task's own failure modes too).
pub async fn run_grant_gate_refresh_loop<L: GrantLoader + 'static>(
    redis_client: redis::Client,
    cache: Arc<GrantCache<L>>,
    poll_interval: Duration,
) {
    loop {
        tokio::select! {
            () = tail_invalidation_stream(&redis_client, &cache) => {}
            () = poll_refresh_all(&cache, poll_interval) => {}
        }
    }
}

async fn tail_invalidation_stream<L: GrantLoader + 'static>(
    redis_client: &redis::Client,
    cache: &Arc<GrantCache<L>>,
) {
    use redis::streams::{StreamReadOptions, StreamReadReply};
    use redis::AsyncCommands;

    loop {
        let mut conn = match redis_client.get_multiplexed_async_connection().await {
            Ok(c) => c,
            Err(err) => {
                tracing::warn!(error = %err, "grant invalidation stream connect failed, retrying");
                tokio::time::sleep(Duration::from_secs(5)).await;
                continue;
            }
        };
        // "$" on every fresh connection: only entries appended from here on
        // -- an entry missed during a reconnect gap is covered by
        // `poll_refresh_all`'s fallback, same posture pub/sub `SUBSCRIBE`
        // (which also can't replay history) had.
        let mut last_id = "$".to_string();
        let opts = StreamReadOptions::default().block(5_000);
        loop {
            let reply: Result<StreamReadReply, redis::RedisError> = conn
                .xread_options(&[GRANT_INVALIDATE_STREAM], &[last_id.as_str()], &opts)
                .await;
            let reply = match reply {
                Ok(r) => r,
                Err(err) => {
                    tracing::warn!(error = %err, "grant invalidation XREAD failed, reconnecting");
                    break;
                }
            };
            for stream_key in reply.keys {
                for entry in stream_key.ids {
                    last_id = entry.id.clone();
                    let fields: HashMap<String, String> = entry
                        .map
                        .iter()
                        .filter_map(|(k, v)| {
                            redis::from_redis_value::<String>(v.clone())
                                .ok()
                                .map(|s| (k.clone(), s))
                        })
                        .collect();
                    let Some(payload) = parse_invalidate_fields(&fields) else {
                        tracing::warn!(
                            entry_id = %last_id,
                            "malformed grant invalidation entry, ignoring"
                        );
                        continue;
                    };
                    invalidate_matching(cache, &payload).await;
                }
            }
        }
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

/// The single production call site for building a [`CapabilityGate`]:
/// wraps `loader` (production: [`PgGrantLoader`]; degraded/unconfigured:
/// `bundle_capability_gate::InMemoryGrantLoader`, which always answers
/// `None` -- fails closed for every non-platform permission) in
/// [`AlwaysGrantedLoader`] so `context`/`clock`/`log` are never gated on a
/// live DB connection, then -- when `redis_client` is `Some` -- spawns
/// [`run_grant_gate_refresh_loop`] against the SAME cache this gate reads,
/// so push-invalidation/poll-fallback actually reach the memo `authorize()`
/// consults. `redis_client: None` (Valkey unconfigured/unreachable) still
/// returns a working gate -- grants simply never refresh until the next
/// full process restart, the same degraded-but-serving posture every other
/// optional dependency in this stage takes (`crate::lib::connect_kv`'s
/// doc).
pub fn build_production_gate<L: GrantLoader + 'static>(
    loader: L,
    redis_client: Option<redis::Client>,
    poll_interval: Duration,
) -> Arc<CapabilityGate> {
    let cache = Arc::new(GrantCache::new(Arc::new(AlwaysGrantedLoader::new(loader))));
    if let Some(client) = redis_client {
        tokio::spawn(run_grant_gate_refresh_loop(
            client,
            Arc::clone(&cache),
            poll_interval,
        ));
    } else {
        tracing::warn!(
            "grant cache refresh loop not started (no Valkey client available); \
             grants will not update until the next process restart"
        );
    }
    Arc::new(CapabilityGate::new(
        cache,
        Arc::new(InMemoryMembership::new()),
        Arc::new(InMemoryQuotaLedger::new()),
        Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
    ))
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

    /// One seeded row (migration `0032_bundle_permission_grants`'s actual
    /// column shape: `permission_id`, `params_json` cast to text) parses
    /// into a real [`GrantedPermission`] -- proves the row extraction
    /// itself, not just the fail-closed-on-error path the test above
    /// covers.
    #[tokio::test]
    async fn pg_grant_loader_parses_a_seeded_grant_row() {
        use sea_orm::{DatabaseBackend, MockDatabase};
        use std::collections::BTreeMap;

        let row: BTreeMap<String, sea_orm::Value> = BTreeMap::from([
            (
                "permission_id".to_string(),
                sea_orm::Value::from("chat.send:discord".to_string()),
            ),
            (
                "params_json".to_string(),
                sea_orm::Value::from("{}".to_string()),
            ),
        ]);
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![row]])
            .into_connection();
        let loader = PgGrantLoader::new(db);
        let result = loader
            .load(&key())
            .await
            .unwrap()
            .expect("a row was seeded");
        let granted = result
            .get("chat.send:discord")
            .expect("the seeded permission_id must be present");
        assert_eq!(granted.permission_id, "chat.send:discord");
    }

    /// End-to-end (spec SS4/SS5.3, task instruction): a core bundle
    /// (`waddles.core.ping`, `chat.send:discord`) with a grant seeded like
    /// PR #432's seeder gets ALLOWED; the identical scope with no grant row
    /// gets DENIED; and a revoke -- modeled the same way `invalidate_
    /// matching` handles a real `bundle:grants:invalidate` entry, drop the
    /// memo then re-[`GrantCache::refresh`] -- makes the very next call
    /// deny, without needing a live Postgres or Valkey broker.
    #[tokio::test]
    async fn ping_bundle_chat_send_allow_deny_and_post_revoke_deny() {
        use bundle_capability_gate::{
            AppScopedResource, CapabilityGate, HostInvokeScopeBuilder, InMemoryMembership,
            InMemoryQuotaLedger, PermissionId, ResourceRef, TenantTier,
        };
        use sea_orm::{DatabaseBackend, MockDatabase};
        use std::collections::BTreeMap;

        let scope = HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("waddles.core.ping".to_string())
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .expect("every required field is set above");
        let permission = PermissionId::ChatSend("discord".to_string());
        let resource = ResourceRef::AppScoped(AppScopedResource::None);

        fn granted_row() -> BTreeMap<String, sea_orm::Value> {
            BTreeMap::from([
                (
                    "permission_id".to_string(),
                    sea_orm::Value::from("chat.send:discord".to_string()),
                ),
                (
                    "params_json".to_string(),
                    sea_orm::Value::from("{}".to_string()),
                ),
            ])
        }

        // --- ALLOW: a grant is seeded for this scope. ---
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![granted_row()]])
            .into_connection();
        let cache = Arc::new(GrantCache::new(Arc::new(AlwaysGrantedLoader::new(
            PgGrantLoader::new(db),
        ))));
        let gate = CapabilityGate::new(
            cache.clone(),
            Arc::new(InMemoryMembership::new()),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        );
        let key = GrantScopeKey::from_scope(&scope);
        cache.refresh(&key).await.expect("seeded row loads cleanly");
        gate.authorize(&scope, permission.clone(), resource.clone())
            .expect("a seeded grant must allow the call");

        // --- DENY: identical scope, no grant row at all. ---
        let empty_db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<BTreeMap<String, sea_orm::Value>>::new()])
            .into_connection();
        let deny_cache = Arc::new(GrantCache::new(Arc::new(AlwaysGrantedLoader::new(
            PgGrantLoader::new(empty_db),
        ))));
        let deny_gate = CapabilityGate::new(
            deny_cache.clone(),
            Arc::new(InMemoryMembership::new()),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        );
        deny_cache
            .refresh(&key)
            .await
            .expect("an empty result set still refreshes cleanly (no grant, not an error)");
        let err = deny_gate
            .authorize(&scope, permission.clone(), resource.clone())
            .expect_err("no grant row must deny, never default-allow");
        assert_eq!(err.reason_str(), "not_granted");

        // --- REVOKE: one connection queued with TWO results in order --
        // granted, then (post-revoke) empty -- exactly the sequence
        // `invalidate_matching` drives against a live RO replica: drop the
        // memo, re-[`GrantCache::refresh`], and the second query now omits
        // the row `g.revoked_at IS NULL` filters out.
        let revoke_db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![granted_row()]])
            .append_query_results([Vec::<BTreeMap<String, sea_orm::Value>>::new()])
            .into_connection();
        let revoke_cache = Arc::new(GrantCache::new(Arc::new(AlwaysGrantedLoader::new(
            PgGrantLoader::new(revoke_db),
        ))));
        let revoke_gate = CapabilityGate::new(
            revoke_cache.clone(),
            Arc::new(InMemoryMembership::new()),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        );
        revoke_cache
            .refresh(&key)
            .await
            .expect("first refresh consumes the granted-row result");
        revoke_gate
            .authorize(&scope, permission.clone(), resource.clone())
            .expect("granted before the revoke");

        // The revoke event itself: `invalidate` (immediate half) then
        // `refresh` (re-fetch, now consuming the queued empty result).
        revoke_cache.invalidate(&key);
        revoke_cache
            .refresh(&key)
            .await
            .expect("second refresh consumes the post-revoke empty result (not an error)");
        let err = revoke_gate
            .authorize(&scope, permission, resource)
            .expect_err("post-revoke, the very next call must deny");
        assert_eq!(err.reason_str(), "not_granted");
    }
}

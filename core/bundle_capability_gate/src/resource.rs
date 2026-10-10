//! `ResourceRef` (spec SS5): what a host capability implementation asks the
//! gate to authorize a call against -- either [`AppScopedResource`]
//! (resource derived entirely server-side from the [`InvokeScope`]) or
//! [`ReputationTarget`] (a bundle-named target UUID the gate independently
//! verifies belongs to the invocation's community/tenant).
//!
//! `authorize()` (`crate::gate`) resolves a granted `ResourceRef` into an
//! [`AuthorizedCall`] carrying the concrete, server-derived resource (schema-
//! qualified table name, kv key prefix, object bucket/prefix, overlay
//! community id, or verified reputation/profile target) -- the capability
//! implementation never re-derives any of this from bundle args itself
//! (spec SS5: "the gate is the only place resource derivation happens").

use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::scope::{InvokeScope, TenantTier};

/// `waddles.core.*` app ids get the `app_core` schema; every other app id
/// gets `app_community` (spec SS1.1). Mirrors
/// `hub_api/services/vendor_bundle_authz.py::CORE_NAMESPACE_PREFIX` --
/// enforced at bundle submission on the hub-api side, so a vendor bundle can
/// never reach this crate with a `waddles.core.*` app_id in the first place;
/// this constant exists only so this crate's own schema derivation agrees
/// with that already-enforced invariant, not to re-enforce it.
pub const CORE_NAMESPACE_PREFIX: &str = "waddles.core.";

const POSTGRES_NAMEDATALEN_MAX: usize = 63;

/// Server-derived, never bundle-supplied for the scoping part (spec SS5.2
/// `AppScoped`). A capability implementation names *which* app-scoped
/// resource it needs; the gate fills in the tenant/community/app-derived
/// specifics.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum AppScopedResource {
    /// `storage.kv`'s own `...:state` hash.
    KvState,
    /// `storage.tables`'s single schema-qualified table (spec SS1.1).
    Table,
    /// `storage.objects`' bucket/prefix (spec SS6).
    Objects,
    /// `overlay.media`'s community/token resolution (spec SS8).
    Overlay,
    /// Every other `AppScoped` family (`net.http`, `chat.send`,
    /// `moderation`, `flags.read`, `platform.*`) -- the permission grant
    /// itself (id + community-granted `params`) is everything the
    /// capability implementation needs; there is no separate resource to
    /// derive.
    None,
}

/// Community vs. tenant reputation/profile scope (spec SS7.1's `scope-kind`).
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum ScopeKind {
    Community,
    Tenant,
}

/// A bundle-named target user for a `ReputationScoped`-style call (spec
/// SS5.2). `target_user` is the only guest-suppliable identity-shaped
/// argument anywhere in the catalog -- it always names a *target*, never the
/// scope the call executes under (spec SS5.1). `delta` is `Some` only for a
/// `reputation.*.write` `adjust()` call; `None` for a read
/// (`reputation.read`, `users.profile.read`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReputationTarget {
    pub target_user: Uuid,
    pub scope_kind: ScopeKind,
    pub delta: Option<i32>,
}

/// The users + metered amount of one `economy.*` call (issue #714). Every
/// named user is a bundle-supplied TARGET the gate verifies is an active
/// member of the invocation's community; the community/tenant the call runs
/// under is never part of this type. `target_user` is the user acted on (the
/// wager's player, the transfer's SENDER, a balance read's subject) -- `None`
/// for a community-wide read (leaderboard). `counterparty` is the transfer's
/// recipient. `amount` is `Some(abs)` for a money-moving call (a wager's
/// STAKE, a transfer's amount) and `None` for a read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EconomyTarget {
    pub target_user: Option<Uuid>,
    pub counterparty: Option<Uuid>,
    pub amount: Option<i64>,
}

/// What a capability implementation passes to `authorize()` (spec SS5).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResourceRef {
    AppScoped(AppScopedResource),
    ReputationScoped(ReputationTarget),
    /// `economy.*` (issue #714).
    EconomyScoped(EconomyTarget),
}

/// The concrete, server-derived resource `authorize()` hands back on success
/// -- the capability implementation reads this instead of re-deriving
/// anything from bundle args (spec SS5).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResolvedResource {
    /// `penguin_spine::Scope::state_key`-shaped prefix, e.g.
    /// `waddles:app:{tenant}:{community}:{app_id}:state` (spec SS5.2).
    KvKeyPrefix(String),
    /// Postgres schema + the bundle's single table name within it (spec
    /// SS1.1) -- `schema_qualified()` gives the `schema.table` form PR #415's
    /// `db.execute` wiring needs.
    Table { schema: String, table_name: String },
    /// Tiered bucket/prefix (spec SS6) -- `Enterprise` gets a dedicated
    /// bucket with an empty prefix; `Free`/`Professional` share one bucket
    /// under a `tenant/.../community/.../app/...` prefix.
    Objects { bucket: String, prefix: String },
    /// The community this overlay call resolves to -- the actual browser-
    /// source token lookup is `core/browser_source_core_module`'s job (spec
    /// SS8), out of this crate's scope.
    Overlay { community_id: i32 },
    /// A `reputation.*`/`users.profile.read` target whose membership in the
    /// invocation's community/tenant has already been verified.
    ReputationTarget(ReputationTarget),
    /// An `economy.*` call whose named users are verified members.
    EconomyTarget(EconomyTarget),
    /// See [`AppScopedResource::None`].
    None,
}

impl ResolvedResource {
    pub fn schema_qualified_table(&self) -> Option<String> {
        match self {
            Self::Table { schema, table_name } => Some(format!("{schema}.{table_name}")),
            _ => None,
        }
    }
}

/// The `authorize()` result (spec SS5): the granted permission plus its
/// resolved resource and the community's own granted `params` (e.g. a
/// tighter `delta_max` than the global-approved ceiling).
#[derive(Clone, Debug, PartialEq)]
pub struct AuthorizedCall {
    pub permission: crate::permission::PermissionId,
    pub resource: ResolvedResource,
    pub params: serde_json::Value,
}

/// Sanitizes an app_id into a Postgres-identifier-safe table name (spec
/// SS1.1): non-`[a-z0-9_]` characters normalized to `_`, truncated with a
/// SHA-256-derived hash suffix past the 63-byte `NAMEDATALEN` cap so two
/// different long app_ids sharing a 63-byte prefix never collide.
///
/// **Reconciliation note:** PR #415's hub-api schema compiler
/// (`docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md`)
/// is the authoritative source for this algorithm; this is this crate's own
/// resource-derivation copy, since no shared Rust implementation exists yet.
/// A divergence here fails closed (Postgres errors on an unknown relation
/// rather than writing to the wrong table) but must be reconciled
/// byte-for-byte before `storage.tables` is wired into `svc_process`/
/// `svc_action` (spec SS12 Phase 6).
pub fn sanitized_app_id(app_id: &str) -> String {
    let sanitized: String = app_id
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_lowercase()
            } else {
                '_'
            }
        })
        .collect();

    if sanitized.len() <= POSTGRES_NAMEDATALEN_MAX {
        return sanitized;
    }

    let mut hasher = Sha256::new();
    hasher.update(app_id.as_bytes());
    let digest = hasher.finalize();
    let suffix = format!("_{:x}", digest)[..9].to_string(); // "_" + 8 hex chars
    let keep = POSTGRES_NAMEDATALEN_MAX - suffix.len();
    format!("{}{}", &sanitized[..keep], suffix)
}

/// `storage.tables`' schema-qualified resource (spec SS1.1): `app_core` for
/// `waddles.core.*` app ids, `app_community` otherwise.
pub fn resolve_table(scope: &InvokeScope) -> ResolvedResource {
    let schema = if scope.app_id().starts_with(CORE_NAMESPACE_PREFIX) {
        "app_core"
    } else {
        "app_community"
    };
    ResolvedResource::Table {
        schema: schema.to_string(),
        table_name: sanitized_app_id(scope.app_id()),
    }
}

/// `storage.kv`'s server-derived key prefix (spec SS5.2).
pub fn resolve_kv_key_prefix(scope: &InvokeScope) -> ResolvedResource {
    ResolvedResource::KvKeyPrefix(format!(
        "waddles:app:{}:{}:{}:state",
        scope.tenant_id(),
        scope.community_id(),
        scope.app_id()
    ))
}

/// `storage.objects`' tiered bucket/prefix (spec SS6): Enterprise tenants get
/// a dedicated bucket; Free/Professional share one bucket under a
/// tenant/community/app prefix.
pub fn resolve_object_prefix(scope: &InvokeScope) -> ResolvedResource {
    match scope.tenant_tier() {
        TenantTier::Enterprise => ResolvedResource::Objects {
            bucket: format!("waddles-bundle-objects-tenant-{}", scope.tenant_id()),
            prefix: format!("app/{}/", scope.app_id()),
        },
        TenantTier::Free | TenantTier::Professional => ResolvedResource::Objects {
            bucket: "waddles-bundle-objects".to_string(),
            prefix: format!(
                "tenant/{}/community/{}/app/{}/",
                scope.tenant_id(),
                scope.community_id(),
                scope.app_id()
            ),
        },
    }
}

/// `overlay.media`'s server-derived community resolution (spec SS8) -- the
/// bundle never sees or supplies the overlay token itself.
pub fn resolve_overlay(scope: &InvokeScope) -> ResolvedResource {
    ResolvedResource::Overlay {
        community_id: scope.community_id(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scope::HostInvokeScopeBuilder;

    fn scope_with_app_id(app_id: &str) -> InvokeScope {
        HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id(app_id)
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap()
    }

    #[test]
    fn sanitized_app_id_lowercases_and_replaces_non_identifier_chars() {
        assert_eq!(
            sanitized_app_id("waddles.core.example_echo"),
            "waddles_core_example_echo"
        );
    }

    #[test]
    fn sanitized_app_id_is_stable_and_deterministic() {
        let a = sanitized_app_id("waddles.core.fishing-bundle");
        let b = sanitized_app_id("waddles.core.fishing-bundle");
        assert_eq!(a, b);
    }

    #[test]
    fn sanitized_app_id_truncates_and_hash_suffixes_past_namedatalen() {
        let long_app_id = format!("vendor.{}", "x".repeat(100));
        let sanitized = sanitized_app_id(&long_app_id);
        assert_eq!(sanitized.len(), POSTGRES_NAMEDATALEN_MAX);
        assert!(sanitized.contains('_'));
    }

    #[test]
    fn sanitized_app_id_never_collides_for_two_ids_sharing_a_63_byte_prefix() {
        let base = "vendor.".to_string() + &"x".repeat(80);
        let a = sanitized_app_id(&format!("{base}-one"));
        let b = sanitized_app_id(&format!("{base}-two"));
        assert_ne!(
            a, b,
            "distinct long app_ids must not collide after truncation"
        );
    }

    #[test]
    fn resolve_table_uses_app_core_schema_for_core_namespace() {
        let scope = scope_with_app_id("waddles.core.example_echo");
        let resolved = resolve_table(&scope);
        assert_eq!(
            resolved.schema_qualified_table().unwrap(),
            "app_core.waddles_core_example_echo"
        );
    }

    #[test]
    fn resolve_table_uses_app_community_schema_for_vendor_bundles() {
        let scope = scope_with_app_id("some_vendor.fishing_game");
        let resolved = resolve_table(&scope);
        assert_eq!(
            resolved.schema_qualified_table().unwrap(),
            "app_community.some_vendor_fishing_game"
        );
    }

    #[test]
    fn resolve_kv_key_prefix_embeds_tenant_community_and_app() {
        let scope = scope_with_app_id("some_vendor.fishing_game");
        let ResolvedResource::KvKeyPrefix(prefix) = resolve_kv_key_prefix(&scope) else {
            panic!("expected KvKeyPrefix");
        };
        assert_eq!(prefix, "waddles:app:7:3:some_vendor.fishing_game:state");
    }

    #[test]
    fn resolve_object_prefix_uses_a_dedicated_bucket_for_enterprise() {
        let scope = HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("some_vendor.fishing_game")
            .app_version(1)
            .tenant_tier(TenantTier::Enterprise)
            .build()
            .unwrap();
        let ResolvedResource::Objects { bucket, .. } = resolve_object_prefix(&scope) else {
            panic!("expected Objects");
        };
        assert_eq!(bucket, "waddles-bundle-objects-tenant-7");
    }

    #[test]
    fn resolve_object_prefix_uses_the_shared_bucket_for_professional() {
        let scope = HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("some_vendor.fishing_game")
            .app_version(1)
            .tenant_tier(TenantTier::Professional)
            .build()
            .unwrap();
        let ResolvedResource::Objects { bucket, prefix } = resolve_object_prefix(&scope) else {
            panic!("expected Objects");
        };
        assert_eq!(bucket, "waddles-bundle-objects");
        assert_eq!(prefix, "tenant/7/community/3/app/some_vendor.fishing_game/");
    }
}

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

/// `penguintechinc.waddles.*` app ids get the `app_core` schema; every other app id
/// gets `app_community` (spec SS1.1). Mirrors
/// `hub_api/services/vendor_bundle_authz.py::CORE_NAMESPACE_PREFIX` --
/// enforced at bundle submission on the hub-api side, so a vendor bundle can
/// never reach this crate with a `penguintechinc.waddles.*` app_id in the first place;
/// this constant exists only so this crate's own schema derivation agrees
/// with that already-enforced invariant, not to re-enforce it.
pub const CORE_NAMESPACE_PREFIX: &str = "penguintechinc.waddles.";

/// Maximum length of a bundle app id (`<github-org>.<repo>.<bundle-name>`).
pub const APP_ID_MAX_LEN: usize = 127;

/// One app-id segment: `[a-z0-9][a-z0-9-]*`. No `_` -- GitHub org/repo names map
/// `_` -> `-` when lowercased (docs/BUNDLE-APP-IDS.md); this is the ONE grammar
/// every service in both languages enforces (golden vectors in
/// `testdata/bundle_app_id/vectors.json`).
fn is_valid_segment(seg: &str) -> bool {
    let mut chars = seg.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit())
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

/// True only for a well-formed bundle app id: exactly three `.`-separated
/// `[a-z0-9][a-z0-9-]*` segments, at most [`APP_ID_MAX_LEN`] bytes. The single
/// Rust implementation of the app-id grammar; the Python twin is
/// `hub_api/services/bundle_app_id.py::is_valid_app_id`.
#[must_use]
pub fn is_valid_app_id(app_id: &str) -> bool {
    if app_id.len() > APP_ID_MAX_LEN {
        return false;
    }
    let mut count = 0usize;
    for seg in app_id.split('.') {
        count += 1;
        if !is_valid_segment(seg) {
            return false;
        }
    }
    count == 3
}

/// True only for a well-formed first-party core id: the reserved
/// `penguintechinc.waddles.` prefix followed by exactly one valid trailing
/// segment (so the whole id is a valid app id). A bare string prefix match would
/// also trust ids such as `penguintechinc.waddles.x.y`; hub-api additionally
/// refuses vendor submissions under this org, so trust never rests on the name
/// alone.
#[must_use]
pub fn is_core_namespace(app_id: &str) -> bool {
    match app_id.strip_prefix(CORE_NAMESPACE_PREFIX) {
        Some(rest) => is_valid_segment(rest) && app_id.len() <= APP_ID_MAX_LEN,
        None => false,
    }
}

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
/// (`reputation.read`, `users.profile.read`). `target_user` is `None` only for
/// a community-wide read (`reputation.leaderboard`) that names no user -- a
/// delta call MUST name its target (the gate refuses `delta: Some` with no
/// user as a resource-scope mismatch).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReputationTarget {
    pub target_user: Option<Uuid>,
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
/// STAKE, a transfer's amount) and `None` for a read. `payout` is `Some` only
/// for `economy.wager`: the amount the call CREDITS to the player, which is the
/// currency the call mints and so is metered against its own daily aggregates
/// (separately from the stake). A wager with no payout is refused fail-closed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EconomyTarget {
    pub target_user: Option<Uuid>,
    pub counterparty: Option<Uuid>,
    pub amount: Option<i64>,
    pub payout: Option<i64>,
}

/// Read vs. write intent of a service-tool call (bundle-MCP-tools P2).
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum ToolAccess {
    Read,
    Write,
}

/// The bundle-named target of a `tool.call` (bundle-MCP-tools P2). All three
/// fields are bundle-supplied and re-validated by the host; the caller's
/// tenant/community/app identity is never part of this type -- it comes from
/// the host-asserted [`InvokeScope`] passed to `authorize()`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolCallTarget {
    pub target_app_id: String,
    pub tool: String,
    pub access: ToolAccess,
}

/// What a capability implementation passes to `authorize()` (spec SS5).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ResourceRef {
    /// `tool.call` (bundle-MCP-tools P2).
    ToolScoped(ToolCallTarget),
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
    /// `waddles:app:{tenant}:{community}:{bundle_storage_key}:state` (spec SS5.2).
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
    /// A `tool.call` target that passed the scope-shape check.
    ToolTarget(ToolCallTarget),
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

/// Hex chars of `sha256(app_id)` appended to every storage key.
const STORAGE_KEY_HASH_LEN: usize = 16;

/// The ONE derivation of a per-bundle storage key (table name, kv namespace,
/// object prefix): `<readable>_<sha256(raw app_id)[..16 hex]>`.
///
/// The readable part (lower-cased ASCII alphanumerics, every other char -> `_`,
/// runs collapsed, ends trimmed, `b_`-prefixed if it would not start with an ASCII
/// letter, `b` if empty,
/// truncated to leave room for the suffix) is for humans only. Injectivity comes
/// from the suffix hashing the EXACT app id, so `acme.bot.kv-store` and
/// `acme.bot_kv.store` can never share a key. The result is always <= 63 bytes
/// (Postgres `NAMEDATALEN` - 1), `[a-z][a-z0-9_]*`, never contains `__` or `:`.
/// Byte-identical Python twin: `hub_api/services/bundle_app_id.py::bundle_storage_key`
/// (proved by `testdata/bundle_app_id/vectors.json`).
pub fn bundle_storage_key(app_id: &str) -> String {
    let mut readable = String::with_capacity(POSTGRES_NAMEDATALEN_MAX);
    if app_id
        .chars()
        .next()
        .is_some_and(|c| !c.is_ascii_alphabetic())
    {
        readable.push_str("b_");
    }
    for c in app_id.chars() {
        let mapped = if c.is_ascii_alphanumeric() {
            c.to_ascii_lowercase()
        } else {
            '_'
        };
        if mapped == '_' && readable.ends_with('_') {
            continue;
        }
        readable.push(mapped);
    }
    let keep = POSTGRES_NAMEDATALEN_MAX - STORAGE_KEY_HASH_LEN - 1;
    readable.truncate(keep); // ASCII only, so byte truncation is char-safe
    let readable = match readable.trim_end_matches('_') {
        "" => "b",
        trimmed => trimmed,
    };

    let digest = Sha256::digest(app_id.as_bytes());
    let hex = format!("{digest:x}");
    format!("{readable}_{}", &hex[..STORAGE_KEY_HASH_LEN])
}

/// `storage.tables`' schema-qualified resource (spec SS1.1): `app_core` for
/// `penguintechinc.waddles.*` app ids, `app_community` otherwise.
pub fn resolve_table(scope: &InvokeScope) -> ResolvedResource {
    let schema = if is_core_namespace(scope.app_id()) {
        "app_core"
    } else {
        "app_community"
    };
    ResolvedResource::Table {
        schema: schema.to_string(),
        table_name: bundle_storage_key(scope.app_id()),
    }
}

/// `storage.kv`'s server-derived key prefix (spec SS5.2).
pub fn resolve_kv_key_prefix(scope: &InvokeScope) -> ResolvedResource {
    ResolvedResource::KvKeyPrefix(format!(
        "waddles:app:{}:{}:{}:state",
        scope.tenant_id(),
        scope.community_id(),
        bundle_storage_key(scope.app_id())
    ))
}

/// `storage.objects`' tiered bucket/prefix (spec SS6): Enterprise tenants get
/// a dedicated bucket; Free/Professional share one bucket under a
/// tenant/community/app prefix.
pub fn resolve_object_prefix(scope: &InvokeScope) -> ResolvedResource {
    match scope.tenant_tier() {
        TenantTier::Enterprise => ResolvedResource::Objects {
            bucket: format!("waddles-bundle-objects-tenant-{}", scope.tenant_id()),
            prefix: format!("app/{}/", bundle_storage_key(scope.app_id())),
        },
        TenantTier::Free | TenantTier::Professional => ResolvedResource::Objects {
            bucket: "waddles-bundle-objects".to_string(),
            prefix: format!(
                "tenant/{}/community/{}/app/{}/",
                scope.tenant_id(),
                scope.community_id(),
                bundle_storage_key(scope.app_id())
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
    fn core_namespace_accepts_hyphenated_and_rejects_lookalikes() {
        for ok in [
            "penguintechinc.waddles.discord-invite",
            "penguintechinc.waddles.wix-event-sync",
        ] {
            assert!(is_core_namespace(ok), "{ok}");
        }
        assert!(is_core_namespace("penguintechinc.waddles.x"));
        for bad in [
            "waddles.core.x",
            "penguintechinc.waddles.",
            "penguintechinc.waddles.a.b",
            "penguintechinc.waddles.-x",
            "penguintechinc.waddles.X",
            "penguintechinc.other.x",
            "psychoboy.penguintwitchbot.roll",
            "penguintechinc.waddlesx",
        ] {
            assert!(!is_core_namespace(bad), "{bad}");
        }
    }

    const VECTORS: &str = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../testdata/bundle_app_id/vectors.json"
    ));

    fn vectors() -> serde_json::Value {
        serde_json::from_str(VECTORS).expect("vectors.json parses")
    }

    fn strings(v: &serde_json::Value, key: &str) -> Vec<String> {
        v[key]
            .as_array()
            .unwrap_or_else(|| panic!("{key} array"))
            .iter()
            .map(|x| x.as_str().expect("string").to_string())
            .collect()
    }

    #[test]
    fn golden_vectors_grammar_accept_and_reject() {
        let v = vectors();
        let valid = strings(&v, "valid");
        let invalid = strings(&v, "invalid");
        assert!(!valid.is_empty() && !invalid.is_empty());
        for id in &valid {
            assert!(is_valid_app_id(id), "should accept {id:?}");
        }
        for id in &invalid {
            assert!(!is_valid_app_id(id), "should reject {id:?}");
        }
    }

    #[test]
    fn golden_vectors_core_namespace() {
        let v = vectors();
        let yes = strings(&v["core"], "yes");
        let no = strings(&v["core"], "no");
        assert!(!yes.is_empty() && !no.is_empty());
        for id in &yes {
            assert!(is_core_namespace(id), "core {id:?}");
        }
        for id in &no {
            assert!(!is_core_namespace(id), "not core {id:?}");
        }
    }

    #[test]
    fn golden_vectors_storage_keys_match_python() {
        let v = vectors();
        let cases = v["storage_keys"].as_array().expect("storage_keys");
        assert!(!cases.is_empty());
        for case in cases {
            let id = case["app_id"].as_str().unwrap();
            let want = case["expected"].as_str().unwrap();
            assert_eq!(bundle_storage_key(id), want, "key for {id:?}");
        }
    }

    #[test]
    fn storage_key_is_injective_for_planted_collision() {
        let a = bundle_storage_key("acme.bot.kv-store");
        let b = bundle_storage_key("acme.bot_kv.store");
        assert_ne!(a, b);
        assert!(a.starts_with("acme_bot_kv_store_") && b.starts_with("acme_bot_kv_store_"));
    }

    #[test]
    fn storage_key_is_bounded_charset_safe_and_deterministic() {
        let long = format!("vendor.{}.x", "y".repeat(120));
        for id in [
            "penguintechinc.waddles.echo",
            "1a.2b.3c",
            "a.b.c",
            "",
            "..",
            long.as_str(),
            "UP.per.\u{dc}n\u{ef}",
        ] {
            let k = bundle_storage_key(id);
            assert!(k.len() <= POSTGRES_NAMEDATALEN_MAX, "{id:?} -> {k}");
            assert!(
                k.chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_'),
                "{k}"
            );
            assert!(!k.contains("__") && !k.contains(':'), "{k}");
            assert_eq!(k, bundle_storage_key(id));
        }
    }

    #[test]
    fn long_ids_sharing_a_prefix_never_collide() {
        let base = "vendor.".to_string() + &"x".repeat(80);
        let a = bundle_storage_key(&format!("{base}-one"));
        let b = bundle_storage_key(&format!("{base}-two"));
        assert_ne!(a, b);
    }

    #[test]
    fn resolve_table_uses_app_core_schema_for_core_namespace() {
        let scope = scope_with_app_id("penguintechinc.waddles.echo");
        let resolved = resolve_table(&scope);
        assert_eq!(
            resolved.schema_qualified_table().unwrap(),
            format!(
                "app_core.{}",
                bundle_storage_key("penguintechinc.waddles.echo")
            )
        );
    }

    #[test]
    fn resolve_table_uses_app_community_schema_for_vendor_bundles() {
        let scope = scope_with_app_id("acme.bot.fishing-game");
        let resolved = resolve_table(&scope);
        assert_eq!(
            resolved.schema_qualified_table().unwrap(),
            format!(
                "app_community.{}",
                bundle_storage_key("acme.bot.fishing-game")
            )
        );
    }

    #[test]
    fn resolve_kv_key_prefix_embeds_tenant_community_and_app() {
        let scope = scope_with_app_id("acme.bot.fishing-game");
        let ResolvedResource::KvKeyPrefix(prefix) = resolve_kv_key_prefix(&scope) else {
            panic!("expected KvKeyPrefix");
        };
        assert_eq!(
            prefix,
            format!(
                "waddles:app:7:3:{}:state",
                bundle_storage_key("acme.bot.fishing-game")
            )
        );
    }

    #[test]
    fn resolve_object_prefix_uses_a_dedicated_bucket_for_enterprise() {
        let scope = HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("acme.bot.fishing-game")
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
            .app_id("acme.bot.fishing-game")
            .app_version(1)
            .tenant_tier(TenantTier::Professional)
            .build()
            .unwrap();
        let ResolvedResource::Objects { bucket, prefix } = resolve_object_prefix(&scope) else {
            panic!("expected Objects");
        };
        assert_eq!(bucket, "waddles-bundle-objects");
        assert_eq!(
            prefix,
            format!(
                "tenant/7/community/3/app/{}/",
                bundle_storage_key("acme.bot.fishing-game")
            )
        );
    }
}

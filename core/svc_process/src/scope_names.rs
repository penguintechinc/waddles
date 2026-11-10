//! Process-wide `(tenant_id, community_id) -> (tenant slug, community name)` registry.
//!
//! The event path keys each bundle's kv namespace on the tenant slug / community name carried by
//! its stream key, which `crate::changelog_consumer` resolves from the database per numeric scope.
//! The hub-api bundle-admin path has only the signed numeric scope, so it must key on the SAME
//! strings or its reads and writes would land in a different (possibly colliding) namespace.
//! The changelog consumer records every scope it resolves here; the admin path only reads. A scope
//! never recorded is unknown, and the admin path refuses it rather than inventing a name.

use std::collections::HashMap;
use std::sync::{LazyLock, RwLock};

use bundle_active_set::ResolvedScope;

type Names = HashMap<(i32, i32), (String, Option<String>)>;

static NAMES: LazyLock<RwLock<Names>> = LazyLock::new(|| RwLock::new(HashMap::new()));

/// Records the DB-resolved names for `scope`, replacing any earlier entry (a renamed tenant or
/// community converges on the next resolution).
pub fn record(scope: (i32, i32), resolved: &ResolvedScope) {
    let mut g = NAMES.write().unwrap_or_else(|p| p.into_inner());
    g.insert(
        scope,
        (
            resolved.tenant_slug.clone(),
            resolved.community_name.clone(),
        ),
    );
}

/// The recorded names for `scope`, if this stage has resolved it.
#[must_use]
pub fn lookup(scope: (i32, i32)) -> Option<(String, Option<String>)> {
    NAMES
        .read()
        .unwrap_or_else(|p| p.into_inner())
        .get(&scope)
        .cloned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rs(t: &str, c: Option<&str>) -> ResolvedScope {
        ResolvedScope {
            tenant_slug: t.to_owned(),
            community_name: c.map(str::to_owned),
        }
    }

    // regression: admin kv namespace must equal the event path's (slug/name, not numeric strings)
    #[test]
    fn admin_scope_resolves_to_exactly_the_event_path_names() {
        record((9001, 3), &rs("acme", Some("main")));
        record((9001, 0), &rs("acme", None));
        assert_eq!(
            crate::bundle_admin::display_scope((9001, 3)).unwrap(),
            ("acme".to_owned(), Some("main".to_owned()))
        );
        // Tenant-wide stays `None`, exactly like the event path's tenant-wide stream key.
        assert_eq!(
            crate::bundle_admin::display_scope((9001, 0)).unwrap(),
            ("acme".to_owned(), None)
        );
        // Same names => same kv scope (and so the same `bundlekv:` prefix) as the event path.
        let (t, c) = crate::bundle_admin::display_scope((9001, 3)).unwrap();
        let admin = bundle_host_kv::KvScope::new(t, c, "waddles.x.y.z");
        let event = bundle_host_kv::KvScope::new("acme", Some("main".to_owned()), "waddles.x.y.z");
        assert_eq!(admin, event);
    }

    // regression: a numeric scope can never address another tenant's namespace
    #[test]
    fn same_community_id_under_two_tenants_never_collides_and_unknown_is_refused() {
        record((9002, 5), &rs("tenant-a", Some("shared-name")));
        record((9003, 5), &rs("tenant-b", Some("shared-name")));
        let a = crate::bundle_admin::display_scope((9002, 5)).unwrap();
        let b = crate::bundle_admin::display_scope((9003, 5)).unwrap();
        assert_ne!(a.0, b.0);
        assert!(crate::bundle_admin::display_scope((9004, 5)).is_err());
        // Numeric-string keying would make (1, 23) and (12, 3) collide-prone; names come from
        // the DB, never from the digits, so an unrecorded scope is simply refused.
        assert!(crate::bundle_admin::display_scope((1, 23)).is_err());
    }
}

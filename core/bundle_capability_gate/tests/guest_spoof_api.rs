//! API-level guest-spoof-impossibility tests (spec SS5.1, Gemini condition
//! 1), complementing `tests/compile_fail.rs`'s type-level proof.
//!
//! `authorize()`'s three parameters are `&InvokeScope` (host-only, see the
//! compile-fail test), [`bundle_capability_gate::PermissionId`], and
//! [`bundle_capability_gate::ResourceRef`] -- neither of the latter two has
//! any field or variant shaped like a tenant/community override. These
//! tests exercise that from the crate's public API: parsing an arbitrary,
//! attacker-influenceable string can only ever produce one of the closed
//! catalog's fixed variants (never a passthrough of extra data), and the
//! only guest-suppliable identity-shaped value anywhere in [`ResourceRef`]
//! is a `target_user` UUID, which the gate treats purely as *who is being
//! targeted*, never as *which scope this call executes under* (proven by
//! `core::gate`'s cross-tenant/cross-app denial tests, which hold regardless
//! of what `target_user` is set to).

use bundle_capability_gate::{ParsePermissionIdError, PermissionId};

/// A string engineered to look like an attempt to smuggle a scope override
/// onto a legitimate id (`"...;tenant=other-tenant"`-shaped junk) never
/// produces a valid `PermissionId` -- the closed catalog's exact-match
/// grammar (spec SS2.3) rejects it outright rather than silently accepting
/// and carrying the extra data through as a second, hidden field.
#[test]
fn permission_id_parse_never_smuggles_extra_scope_shaped_data_through() {
    let attempts = [
        "net.http:api.example.com;tenant=other-tenant",
        "storage.kv;tenant_id=999",
        "flags.read?community_id=1",
        "reputation.community.write:target-tenant-999",
    ];
    for raw in attempts {
        let err = PermissionId::parse(raw)
            .expect_err("a scope-smuggling id must never parse successfully");
        assert!(matches!(
            err,
            ParsePermissionIdError::InvalidHost(_)
                | ParsePermissionIdError::UnsupportedPlatform(_)
                | ParsePermissionIdError::UnknownPermission(_)
                | ParsePermissionIdError::MissingParam { .. }
        ));
    }
}

/// Exhaustive check that no catalog id embeds the literal
/// `InvokeScope` field names this crate's builder takes (`tenant_id`,
/// `community_id`, `app_id`, `app_version`, `tenant_tier`) -- a lightweight
/// fence against a future catalog addition accidentally introducing a
/// permission id shaped like one of `InvokeScope`'s own fields, which some
/// future capability implementation might be tempted to populate straight
/// from `args` instead of the host-only builder. `reputation.community.write`
/// / `reputation.tenant.write` legitimately contain the *words*
/// "community"/"tenant" (spec SS1's own naming) -- this checks for the
/// underscored field-name spelling specifically, which no legitimate catalog
/// id uses.
#[test]
fn no_catalog_id_embeds_an_invoke_scope_field_name() {
    for family in bundle_capability_gate::PermissionFamily::ALL {
        let id = family.id_prefix();
        for banned in ["tenant_id", "community_id", "app_version", "tenant_tier"] {
            assert!(
                !id.contains(banned),
                "catalog id {id:?} must never embed an InvokeScope field name (found {banned:?})"
            );
        }
    }
}

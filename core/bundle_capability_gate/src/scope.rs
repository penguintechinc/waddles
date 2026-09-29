//! `InvokeScope`: the `(tenant, community, app_id, app_version, tier)`
//! identity a host call executes under (spec SS5.1, Gemini condition 1).
//!
//! **Confused-deputy prevention is the entire point of this module.**
//! `InvokeScope` is host-constructed only, from the executor connection's
//! pinned stage identity plus the invocation's server-resolved envelope --
//! never from guest arguments, and never influenceable by them. This is
//! enforced by the type system, not by convention: every field is private,
//! so no struct literal (`InvokeScope { tenant_id: ..., .. }`) compiles
//! outside this crate (see `tests/compile_fail.rs`) -- the only path to an
//! instance is [`HostInvokeScopeBuilder::build`], and every builder setter
//! takes a strongly-typed, already-trusted value (`i32`/`String`/`i64`/
//! [`TenantTier`]), never a `serde_json::Value` or anything shaped like a
//! guest's `host-call` `args` payload. A caller wiring this crate into
//! `svc_process`/`svc_action` must populate the builder only from the
//! connection's own pinned identity and the delivered event's
//! server-resolved envelope -- never from `HostCallBody.args`.

use std::fmt;

/// The tenant's licensed tier, resolved host-side at connection setup --
/// gates `storage.objects`' bucket topology (spec SS6) the same way it
/// already gates `ai.generate`'s marketplace-time availability (spec SS9).
/// Never guest-suppliable; carried on [`InvokeScope`] as one more piece of
/// the server-side envelope.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash)]
pub enum TenantTier {
    Free,
    Professional,
    Enterprise,
}

/// A host-constructed, immutable execution identity. See the module doc for
/// why every field is private and the only constructor is
/// [`HostInvokeScopeBuilder`].
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct InvokeScope {
    tenant_id: i32,
    /// `0` is the tenant-wide sentinel, matching
    /// `app_active_versions.community_id`'s existing convention
    /// (`core/bundle_active_set::scope`).
    community_id: i32,
    app_id: String,
    /// The specific approved `(app_id, version)` this connection was
    /// instantiated for (spec SS4: "a grant is scoped to (app_id, version),
    /// not just app_id") -- never the "latest" version, since a community
    /// can be pinned to an older, already-consented version (spec SS3.4).
    app_version: i64,
    tenant_tier: TenantTier,
}

impl InvokeScope {
    pub fn tenant_id(&self) -> i32 {
        self.tenant_id
    }

    pub fn community_id(&self) -> i32 {
        self.community_id
    }

    pub fn app_id(&self) -> &str {
        &self.app_id
    }

    pub fn app_version(&self) -> i64 {
        self.app_version
    }

    pub fn tenant_tier(&self) -> TenantTier {
        self.tenant_tier
    }
}

/// Every field required before [`HostInvokeScopeBuilder::build`] will
/// produce an [`InvokeScope`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InvokeScopeField {
    TenantId,
    AppId,
    AppVersion,
    TenantTier,
}

impl fmt::Display for InvokeScopeField {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            Self::TenantId => "tenant_id",
            Self::AppId => "app_id",
            Self::AppVersion => "app_version",
            Self::TenantTier => "tenant_tier",
        };
        f.write_str(name)
    }
}

/// Returned by [`HostInvokeScopeBuilder::build`] when a required field was
/// never set, or `app_id` is empty -- a programmer error in the host's own
/// wiring (never a guest-triggerable condition), surfaced as `Err` rather
/// than a panic per this crate's `Result`-everywhere posture.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum InvokeScopeBuildError {
    #[error("InvokeScope missing required field: {0}")]
    MissingField(InvokeScopeField),
    #[error("InvokeScope app_id must not be empty")]
    EmptyAppId,
}

/// The **sole** host-only path to an [`InvokeScope`] (spec SS5.1). Every
/// setter takes a trusted, already-typed value the caller must have derived
/// from its own pinned connection identity / server-resolved envelope --
/// this builder has no method that accepts a `serde_json::Value` or any
/// other guest-`args`-shaped input, by construction.
#[derive(Default, Debug, Clone)]
pub struct HostInvokeScopeBuilder {
    tenant_id: Option<i32>,
    community_id: i32,
    app_id: Option<String>,
    app_version: Option<i64>,
    tenant_tier: Option<TenantTier>,
}

impl HostInvokeScopeBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn tenant_id(mut self, tenant_id: i32) -> Self {
        self.tenant_id = Some(tenant_id);
        self
    }

    /// Defaults to `0` (the tenant-wide sentinel) if never called.
    pub fn community_id(mut self, community_id: i32) -> Self {
        self.community_id = community_id;
        self
    }

    pub fn app_id(mut self, app_id: impl Into<String>) -> Self {
        self.app_id = Some(app_id.into());
        self
    }

    pub fn app_version(mut self, app_version: i64) -> Self {
        self.app_version = Some(app_version);
        self
    }

    pub fn tenant_tier(mut self, tenant_tier: TenantTier) -> Self {
        self.tenant_tier = Some(tenant_tier);
        self
    }

    pub fn build(self) -> Result<InvokeScope, InvokeScopeBuildError> {
        let tenant_id = self.tenant_id.ok_or(InvokeScopeBuildError::MissingField(
            InvokeScopeField::TenantId,
        ))?;
        let app_id = self
            .app_id
            .ok_or(InvokeScopeBuildError::MissingField(InvokeScopeField::AppId))?;
        if app_id.is_empty() {
            return Err(InvokeScopeBuildError::EmptyAppId);
        }
        let app_version = self.app_version.ok_or(InvokeScopeBuildError::MissingField(
            InvokeScopeField::AppVersion,
        ))?;
        let tenant_tier = self.tenant_tier.ok_or(InvokeScopeBuildError::MissingField(
            InvokeScopeField::TenantTier,
        ))?;

        Ok(InvokeScope {
            tenant_id,
            community_id: self.community_id,
            app_id,
            app_version,
            tenant_tier,
        })
    }
}

/// The grant-cache lookup key derived from an [`InvokeScope`] -- identical
/// tuple, just named for its role as a `HashMap`/`GrantSnapshot` key (spec
/// SS4: cache keyed `(tenant, community, app_id)`, this crate additionally
/// folds in `app_version` per SS4's own versioning rule so a stale/
/// unconsented version never matches a newer grant row).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct GrantScopeKey {
    pub tenant_id: i32,
    pub community_id: i32,
    pub app_id: String,
    pub app_version: i64,
}

impl GrantScopeKey {
    pub fn from_scope(scope: &InvokeScope) -> Self {
        Self {
            tenant_id: scope.tenant_id,
            community_id: scope.community_id,
            app_id: scope.app_id.clone(),
            app_version: scope.app_version,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn builder() -> HostInvokeScopeBuilder {
        HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .community_id(3)
            .app_id("waddles.core.example_echo")
            .app_version(2)
            .tenant_tier(TenantTier::Free)
    }

    #[test]
    fn build_succeeds_with_every_field_set() {
        let scope = builder().build().expect("all required fields set");
        assert_eq!(scope.tenant_id(), 7);
        assert_eq!(scope.community_id(), 3);
        assert_eq!(scope.app_id(), "waddles.core.example_echo");
        assert_eq!(scope.app_version(), 2);
        assert_eq!(scope.tenant_tier(), TenantTier::Free);
    }

    #[test]
    fn community_id_defaults_to_the_tenant_wide_sentinel() {
        let scope = HostInvokeScopeBuilder::new()
            .tenant_id(7)
            .app_id("waddles.core.example_echo")
            .app_version(1)
            .tenant_tier(TenantTier::Enterprise)
            .build()
            .expect("community_id is optional");
        assert_eq!(scope.community_id(), 0);
    }

    #[test]
    fn build_fails_closed_on_missing_tenant_id() {
        let err = HostInvokeScopeBuilder::new()
            .app_id("x")
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap_err();
        assert_eq!(
            err,
            InvokeScopeBuildError::MissingField(InvokeScopeField::TenantId)
        );
    }

    #[test]
    fn build_fails_closed_on_missing_app_version() {
        let err = HostInvokeScopeBuilder::new()
            .tenant_id(1)
            .app_id("x")
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap_err();
        assert_eq!(
            err,
            InvokeScopeBuildError::MissingField(InvokeScopeField::AppVersion)
        );
    }

    #[test]
    fn build_fails_closed_on_missing_tenant_tier() {
        let err = HostInvokeScopeBuilder::new()
            .tenant_id(1)
            .app_id("x")
            .app_version(1)
            .build()
            .unwrap_err();
        assert_eq!(
            err,
            InvokeScopeBuildError::MissingField(InvokeScopeField::TenantTier)
        );
    }

    #[test]
    fn build_rejects_an_empty_app_id() {
        let err = HostInvokeScopeBuilder::new()
            .tenant_id(1)
            .app_id("")
            .app_version(1)
            .tenant_tier(TenantTier::Free)
            .build()
            .unwrap_err();
        assert_eq!(err, InvokeScopeBuildError::EmptyAppId);
    }

    #[test]
    fn grant_scope_key_mirrors_the_scope_it_was_derived_from() {
        let scope = builder().build().unwrap();
        let key = GrantScopeKey::from_scope(&scope);
        assert_eq!(key.tenant_id, scope.tenant_id());
        assert_eq!(key.community_id, scope.community_id());
        assert_eq!(key.app_id, scope.app_id());
        assert_eq!(key.app_version, scope.app_version());
    }

    /// Cross-tenant/cross-app regression: two scopes differing only in
    /// tenant, or only in app_id, must never hash/compare equal -- this is
    /// the property `GrantCache`'s `HashMap<GrantScopeKey, _>` relies on to
    /// keep one tenant's/app's grants from ever being read under another's
    /// key (spec SS5.2 AppScoped's "never accepts a bundle-supplied resource
    /// identifier for the scoping part").
    #[test]
    fn grant_scope_key_distinguishes_tenant_and_app() {
        let a = GrantScopeKey::from_scope(&builder().build().unwrap());
        let b = GrantScopeKey::from_scope(
            &HostInvokeScopeBuilder::new()
                .tenant_id(999)
                .community_id(3)
                .app_id("waddles.core.example_echo")
                .app_version(2)
                .tenant_tier(TenantTier::Free)
                .build()
                .unwrap(),
        );
        let c = GrantScopeKey::from_scope(
            &HostInvokeScopeBuilder::new()
                .tenant_id(7)
                .community_id(3)
                .app_id("some.other.app")
                .app_version(2)
                .tenant_tier(TenantTier::Free)
                .build()
                .unwrap(),
        );
        assert_ne!(a, b, "different tenant_id must produce a different key");
        assert_ne!(a, c, "different app_id must produce a different key");
    }
}

//! The standard bundle-host enforcement gate (spec
//! `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
//! SS5): "One crate, one function, every host import calls it first."
//!
//! This crate ships the catalog, the host-only scope type, the resource-
//! derivation types, the grant/quota/membership read paths, and
//! [`gate::CapabilityGate::authorize`] itself. It does **not** wire any of
//! this into `core/svc_process` or `core/svc_action`'s `CapabilityHandler::
//! handle` -- that integration (replacing today's hardcoded `db`/`kv`
//! `not_implemented` denials, and the per-component `Linker` registration
//! that only links a granted permission's host functions) is spec SS12
//! Phase 4's own follow-on task, deliberately out of scope here so this
//! crate can be reviewed and tested in isolation first.
//!
//! # Reading order
//!
//! - [`scope`] -- [`scope::InvokeScope`], host-constructed only (spec SS5.1,
//!   Gemini condition 1: confused-deputy prevention).
//! - [`permission`] -- the closed [`permission::PermissionId`] catalog (spec
//!   SS1), with risk levels and default quotas.
//! - [`resource`] -- [`resource::ResourceRef`]'s `AppScoped`/
//!   `ReputationScoped` split and server-side resource derivation (spec
//!   SS5.2, SS1.1, SS6, SS8).
//! - [`membership`] / [`grant`] / [`quota`] -- the three sync, zero-I/O
//!   hot-path reads `authorize()` performs (spec SS5.5), plus the async
//!   loader boundary a later task backs with a real RO-replica reader.
//! - [`denied`] -- the stable denial-reason vocabulary (spec SS5.4).
//! - [`gate`] -- [`gate::CapabilityGate::authorize`] itself, tying the above
//!   together.
//!
//! # No kill-switch
//!
//! Per this platform's security-sensitive-mechanism rule, there is no env
//! var, CLI flag, or config setting anywhere in this crate that disables
//! `authorize()`. The only sanctioned bypass is the core-bundle system-
//! approval path (spec SS3.6) -- which still populates the same grant tables
//! and goes through this same gate, distinguished only by its
//! `approval_source` audit field, a hub-api-side concern this crate never
//! sees.

pub mod audit;
pub mod denied;
pub mod gate;
pub mod grant;
pub mod instance_policy;
pub mod membership;
pub mod permission;
pub mod quota;
pub mod resource;
pub mod scope;

pub use denied::Denied;
pub use gate::CapabilityGate;
pub use grant::{
    GrantCache, GrantLoader, GrantSet, GrantSnapshot, GrantedPermission, InMemoryGrantLoader,
    InMemoryGrantSnapshot,
};
pub use instance_policy::{InMemoryInstancePolicySnapshot, InstanceAction, InstancePolicySnapshot};
pub use membership::{InMemoryMembership, MembershipCheck};
pub use permission::{
    CapabilityKind, ParsePermissionIdError, PermissionFamily, PermissionId, Quota, Risk,
};
pub use quota::{InMemoryQuotaLedger, QuotaDenial, QuotaLedger};
pub use resource::{
    AppScopedResource, AuthorizedCall, ReputationTarget, ResolvedResource, ResourceRef, ScopeKind,
};
pub use scope::{
    GrantScopeKey, HostInvokeScopeBuilder, InvokeScope, InvokeScopeBuildError, TenantTier,
};

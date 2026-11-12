//! `caller_can` -- "may the actor who triggered this invocation exercise `scope`?"
//! over the `stage-next` WIT `authz` import (`wit/waddle-bundle/stage.wit`
//! `interface authz`; always granted, no capability-gate permission).
//!
//! **Authorization source of truth.** The event payload's `is_mod` /
//! `is_broadcaster` badges are DISPLAY-ONLY: platform chat badges a connector
//! copies in, never authorization. Gate every privileged action on
//! [`caller_can`] instead.
//!
//! **Fail-closed.** A malformed scope, a host error, or no installed transport
//! all yield `false`; nothing here ever returns `true` by default.
//!
//! **Transport seam.** Like [`crate::tool`] and [`crate::storage_v2`], this crate
//! generates bindings only for the frozen `stage` world; the `stage-next` `authz`
//! import is generated in the BUNDLE crate. The bundle therefore implements
//! [`AuthzTransport`] as a thin adapter over its generated
//! `waddle::bundle::authz::caller_can` and installs it once with
//! [`install_transport`] (e.g. in its stage `init`); [`caller_can`] then works
//! as a plain free function. (No cargo feature is needed: nothing here touches
//! the `stage` bindings.)
//!
//! ```ignore
//! struct HostAuthz;
//! impl waddle_sdk::authz::AuthzTransport for HostAuthz {
//!     fn caller_can(&self, scope: &str) -> Result<bool, AuthzError> {
//!         wit::authz::caller_can(scope).map_err(|e| match e {
//!             wit::authz::Error::Unavailable(m) => AuthzError::Unavailable(m),
//!             wit::authz::Error::Invalid(m) => AuthzError::Invalid(m),
//!             wit::authz::Error::Backend(m) => AuthzError::Backend(m),
//!         })
//!     }
//! }
//! waddle_sdk::authz::install_transport(Box::new(HostAuthz));
//! if waddle_sdk::authz::caller_can(authz::SCOPE_BAN) { /* ... */ }
//! ```

use std::collections::BTreeSet;
use std::sync::RwLock;

use thiserror::Error;

pub const SCOPE_MEMBER: &str = "community:member";
pub const SCOPE_VIP: &str = "community:vip";
pub const SCOPE_MODERATE: &str = "community:moderate";
pub const SCOPE_BAN: &str = "community:ban";
pub const SCOPE_TIMEOUT: &str = "community:timeout";
pub const SCOPE_ADMIN: &str = "community:admin";
pub const SCOPE_OWNER: &str = "community:owner";

/// Mirrors the executor + stage rule: scope is 1..=64 bytes.
pub const MAX_SCOPE_LEN: usize = 64;

/// Mirrors WIT `authz.error`.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum AuthzError {
    #[error("unavailable: {0}")]
    Unavailable(String),
    #[error("invalid: {0}")]
    Invalid(String),
    #[error("backend: {0}")]
    Backend(String),
}

/// The `authz` host import, as the bundle's generated bindings expose it.
pub trait AuthzTransport: Send + Sync {
    /// `caller-can`: does the triggering actor hold `scope`?
    fn caller_can(&self, scope: &str) -> Result<bool, AuthzError>;
}

static TRANSPORT: RwLock<Option<Box<dyn AuthzTransport>>> = RwLock::new(None);

/// Installs (or replaces) the process-wide transport used by [`caller_can`].
pub fn install_transport(transport: Box<dyn AuthzTransport>) {
    *TRANSPORT.write().unwrap_or_else(|p| p.into_inner()) = Some(transport);
}

/// Removes the installed transport (every [`caller_can`] then fails closed).
pub fn clear_transport() {
    *TRANSPORT.write().unwrap_or_else(|p| p.into_inner()) = None;
}

/// True when `scope` matches `^[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*$` and is 1..=64 bytes.
pub fn is_valid_scope(scope: &str) -> bool {
    if scope.is_empty() || scope.len() > MAX_SCOPE_LEN {
        return false;
    }
    let Some((left, right)) = scope.split_once(':') else {
        return false;
    };
    let ok = |s: &str| {
        let mut chars = s.chars();
        matches!(chars.next(), Some('a'..='z'))
            && chars.all(|c| matches!(c, 'a'..='z' | '0'..='9' | '_' | '.'))
    };
    ok(left) && ok(right)
}

/// [`caller_can`] against an explicit transport; fails closed on every error.
pub fn caller_can_with(transport: &dyn AuthzTransport, scope: &str) -> bool {
    if !is_valid_scope(scope) {
        report("invalid_scope", scope, None);
        return false;
    }
    match transport.caller_can(scope) {
        Ok(allowed) => allowed,
        Err(err) => {
            report("host_error", scope, Some(&err));
            false
        }
    }
}

/// True only if the triggering actor holds `scope` in this community. Any
/// failure (invalid scope, host error, no installed transport) is `false`.
pub fn caller_can(scope: &str) -> bool {
    let guard = TRANSPORT.read().unwrap_or_else(|p| p.into_inner());
    match guard.as_deref() {
        Some(transport) => caller_can_with(transport, scope),
        None => {
            report("no_transport", scope, None);
            false
        }
    }
}

/// Logs a fail-closed outcome (scope + error kind only, never PII).
#[cfg(target_arch = "wasm32")]
fn report(reason: &str, scope: &str, err: Option<&AuthzError>) {
    let kind = err.map(|e| match e {
        AuthzError::Unavailable(_) => "unavailable",
        AuthzError::Invalid(_) => "invalid",
        AuthzError::Backend(_) => "backend",
    });
    let mut fields = crate::log::Fields::new();
    if let Ok(f) = fields.clone().with("reason", reason) {
        fields = f;
    }
    if is_valid_scope(scope)
        && let Ok(f) = fields.clone().with("scope", scope)
    {
        fields = f;
    }
    if let Some(kind) = kind
        && let Ok(f) = fields.clone().with("error_kind", kind)
    {
        fields = f;
    }
    let _ = crate::log::write(
        crate::log::Level::Warn,
        "authz.caller_can failed closed",
        &fields,
    );
}

#[cfg(not(target_arch = "wasm32"))]
fn report(_reason: &str, _scope: &str, _err: Option<&AuthzError>) {}

/// Native test double: an actor holding a fixed scope set (see [`StaticAuthz::for_role`]),
/// optionally simulating an unavailable host. Counts host calls.
#[derive(Debug, Default)]
pub struct StaticAuthz {
    pub scopes: BTreeSet<String>,
    pub unavailable: bool,
    calls: std::sync::atomic::AtomicUsize,
}

impl StaticAuthz {
    /// An actor holding exactly `scopes`.
    pub fn with_scopes<I: IntoIterator<Item = S>, S: Into<String>>(scopes: I) -> Self {
        Self {
            scopes: scopes.into_iter().map(Into::into).collect(),
            ..Self::default()
        }
    }

    /// An actor whose scopes mirror the contract role map (unknown role: none).
    pub fn for_role(role: &str) -> Self {
        let names: &[&str] = match role {
            "owner" | "community-owner" => &[
                "member", "vip", "moderate", "ban", "timeout", "admin", "owner",
            ],
            "community-admin" => &["member", "vip", "moderate", "ban", "timeout", "admin"],
            "moderator" => &["member", "moderate", "ban", "timeout"],
            "vip" => &["member", "vip"],
            "member" => &["member"],
            _ => &[],
        };
        Self::with_scopes(names.iter().map(|n| format!("community:{n}")))
    }

    /// An unavailable host: every call errors.
    pub fn unavailable() -> Self {
        Self {
            unavailable: true,
            ..Self::default()
        }
    }

    /// Number of host calls received.
    pub fn call_count(&self) -> usize {
        self.calls.load(std::sync::atomic::Ordering::SeqCst)
    }
}

impl AuthzTransport for StaticAuthz {
    fn caller_can(&self, scope: &str) -> Result<bool, AuthzError> {
        self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        if self.unavailable {
            return Err(AuthzError::Unavailable("authz unavailable".to_string()));
        }
        Ok(self.scopes.contains(scope))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // `install_transport` is process-global; serialize the tests that touch it.
    static GLOBAL: Mutex<()> = Mutex::new(());

    #[test]
    fn moderator_can_ban_and_timeout_not_admin() {
        let m = StaticAuthz::for_role("moderator");
        assert!(caller_can_with(&m, SCOPE_BAN));
        assert!(caller_can_with(&m, SCOPE_TIMEOUT));
        assert!(!caller_can_with(&m, SCOPE_ADMIN));
        assert!(!caller_can_with(&m, SCOPE_OWNER));
    }

    #[test]
    fn member_and_unknown_role() {
        let m = StaticAuthz::for_role("member");
        assert!(caller_can_with(&m, SCOPE_MEMBER));
        assert!(!caller_can_with(&m, SCOPE_BAN));
        let none = StaticAuthz::for_role("stranger");
        assert!(!caller_can_with(&none, SCOPE_MEMBER));
    }

    #[test]
    fn owner_holds_all_seven_and_admin_lacks_owner() {
        let o = StaticAuthz::for_role("community-owner");
        assert_eq!(o.scopes.len(), 7);
        let a = StaticAuthz::for_role("community-admin");
        assert!(a.scopes.contains(SCOPE_ADMIN) && !a.scopes.contains(SCOPE_OWNER));
        assert_eq!(StaticAuthz::for_role("vip").scopes.len(), 2);
    }

    #[test]
    fn unavailable_host_fails_closed() {
        let u = StaticAuthz::unavailable();
        assert!(!caller_can_with(&u, SCOPE_MEMBER));
        assert_eq!(u.call_count(), 1);
    }

    #[test]
    fn invalid_scope_never_reaches_host() {
        let o = StaticAuthz::for_role("owner");
        let long = format!("a:{}", "b".repeat(64));
        for bad in [
            "",
            "community",
            "Community:ban",
            "community:Ban",
            "1c:ban",
            "community:ban!",
            "community:",
            ":ban",
            "community:ban:x",
            " community:ban",
            "community:ban\n",
            &long,
        ] {
            assert!(!caller_can_with(&o, bad), "{bad:?}");
        }
        assert_eq!(o.call_count(), 0);
    }

    #[test]
    fn scope_length_boundary() {
        let ok = format!("a:{}", "b".repeat(62));
        assert!(is_valid_scope(&ok));
        assert!(!is_valid_scope(&format!("{ok}b")));
        assert!(is_valid_scope("a1_.x:b2_.y"));
    }

    #[test]
    fn global_transport_install_clear_and_fail_closed() {
        let _g = GLOBAL.lock().unwrap_or_else(|p| p.into_inner());
        clear_transport();
        assert!(!caller_can(SCOPE_MEMBER), "no transport -> false");
        install_transport(Box::new(StaticAuthz::for_role("moderator")));
        assert!(caller_can(SCOPE_BAN));
        assert!(!caller_can(SCOPE_ADMIN));
        install_transport(Box::new(StaticAuthz::unavailable()));
        assert!(!caller_can(SCOPE_BAN));
        clear_transport();
        assert!(!caller_can(SCOPE_BAN));
    }

    #[test]
    fn explicit_scope_set_and_error_display() {
        let s = StaticAuthz::with_scopes(["community:ban"]);
        assert!(caller_can_with(&s, "community:ban"));
        assert!(!caller_can_with(&s, "community:timeout"));
        assert_eq!(AuthzError::Backend("x".into()).to_string(), "backend: x");
        assert_eq!(AuthzError::Invalid("x".into()).to_string(), "invalid: x");
    }
}

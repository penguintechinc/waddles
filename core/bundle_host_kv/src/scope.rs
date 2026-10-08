//! Server-derived key namespace for the bundle `kv` capability.
//!
//! [`KvScope`] is built exclusively from the invocation's already-validated
//! `(tenant, community, app_id)` -- the same triple `StageCapabilities`
//! already carries in both `core/svc_process` and `core/svc_action`,
//! itself sourced from the JWT/manifest-driven invoke scope, never from a
//! bundle host-call's own `args` (module doc of both crates'
//! `capabilities.rs`: "every capability here resolves its own scope from
//! `self`, never from the `args`/`op` the guest supplied"). This module
//! never accepts a tenant/community/app_id from guest-controlled input --
//! only [`validate_guest_key`] ever looks at a guest-supplied string, and
//! it is only ever used as the final path segment appended after the
//! server-derived prefix, so a guest can influence its own key's suffix
//! and nothing else.

/// The literal segment rendered for a tenant-wide (no-community)
/// activation -- mirrors `penguin_spine::scope::TENANT_WIDE_SEGMENT` so a
/// `bundlekv:` key reads consistently with every other spine key in the
/// same Valkey instance, without this crate taking a dependency on
/// `penguin-spine` just for one constant.
pub const TENANT_WIDE_SEGMENT: &str = "_tenant";

/// The longest guest-supplied key this capability accepts, in bytes
/// (task quota: "max key length"). Chosen to comfortably fit a
/// dotted/namespaced identifier (e.g. `"counters.viewer.acme_channel"`)
/// while keeping a single bundle's key names cheap to index and log.
pub const MAX_GUEST_KEY_LEN: usize = 256;

/// A guest key is rejected unless every byte is one of these -- no `:`
/// (the namespace separator this whole scheme relies on to keep one app's
/// keys unreachable from another), no `*`/`?`/`[`/`]` (Valkey `KEYS`/`SCAN`
/// glob metacharacters -- irrelevant to a single `GET`/`SET` today, but a
/// guest key is never trusted to be glob-safe for whatever admin tooling
/// might later `SCAN` this namespace), and no whitespace/control bytes.
fn is_allowed_key_byte(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.')
}

/// One rejected guest key, before any Valkey round trip -- the caller maps
/// this to the WIT `kv.error` variant that best fits (`too-large` for a
/// length violation, `backend` for everything else, since `kv.error` has
/// no dedicated "invalid key" variant -- see `stage.wit` `interface kv`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KeyValidationError {
    /// Empty, or longer than [`MAX_GUEST_KEY_LEN`] bytes. Carries the
    /// offending length so the caller can report it via `kv.error::too-large`.
    TooLong(u64),
    /// Contains a byte outside [`is_allowed_key_byte`] -- most importantly
    /// `:` (namespace escape) or a glob metacharacter.
    InvalidChars,
}

/// Validates a guest-supplied key **before** it is ever appended to a
/// server-derived prefix. Length-limited and charset-validated (task
/// requirement) rather than hashed: an allowlist charset that excludes
/// every namespace/glob metacharacter makes an escape a syntax error, not
/// a probabilistic property, and keeps a bundle author's keys readable in
/// Valkey for operator debugging.
pub fn validate_guest_key(key: &str) -> Result<(), KeyValidationError> {
    if key.is_empty() || key.len() > MAX_GUEST_KEY_LEN {
        return Err(KeyValidationError::TooLong(key.len() as u64));
    }
    if !key.bytes().all(is_allowed_key_byte) {
        return Err(KeyValidationError::InvalidChars);
    }
    Ok(())
}

/// The authenticated, server-derived scope one `kv` host-call is answered
/// under. Every field here is trusted input by the time it reaches this
/// struct -- `tenant`/`community`/`app_id` come from the invocation's own
/// validated scope (spec §5.11: "No bundle host call accepts a tenant or
/// community argument at all"), exactly like `handle_context`/`handle_relay`
/// already assume for every other capability in both stages.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct KvScope {
    pub tenant: String,
    pub community: Option<String>,
    pub app_id: String,
}

impl KvScope {
    pub fn new(
        tenant: impl Into<String>,
        community: Option<String>,
        app_id: impl Into<String>,
    ) -> Self {
        Self {
            tenant: tenant.into(),
            community,
            app_id: app_id.into(),
        }
    }

    fn community_segment(&self) -> &str {
        self.community.as_deref().unwrap_or(TENANT_WIDE_SEGMENT)
    }

    /// `bundlekv:{tenant}:{community|_tenant}:{app_id}` -- everything a
    /// guest key is appended to, and the exact prefix every quota/count/
    /// rate-limit key below also roots itself under (never something a
    /// guest key could partially overlap with, since every one of these
    /// adds its own reserved sub-segment: `:data:`, `:count`, `:opr:`).
    fn app_prefix(&self) -> String {
        format!(
            "bundlekv:{}:{}:{}",
            self.tenant,
            self.community_segment(),
            self.app_id
        )
    }

    /// The Valkey key one guest `key` is actually stored under:
    /// `bundlekv:{tenant}:{community}:{app_id}:data:{key}`. The literal
    /// `:data:` segment (rather than appending `guest_key` directly after
    /// `app_prefix()`) means a guest key of e.g. `"count"` can never
    /// collide with this scope's own [`Self::count_key`] regardless of
    /// what a future reserved suffix might be named.
    pub fn data_key(&self, guest_key: &str) -> String {
        format!("{}:data:{}", self.app_prefix(), guest_key)
    }

    /// The live-key counter this app/community's quota is enforced
    /// against (task quota: "max key count per app per community").
    pub fn count_key(&self) -> String {
        format!("{}:count", self.app_prefix())
    }

    /// The `SCAN MATCH` pattern covering every live data key for this app
    /// -- used only by `crate::backend`'s self-heal reconciliation
    /// (`count_key` missing, e.g. evicted under `allkeys-*` memory
    /// pressure -- `crate::policy`'s doc) to recompute the true live-key
    /// count. Never used on the per-op hot path.
    pub fn data_scan_pattern(&self) -> String {
        format!("{}:data:*", self.app_prefix())
    }

    /// The reconciliation mutex (`SET NX`) guarding
    /// [`Self::data_scan_pattern`]'s `SCAN` -- one reconciliation in
    /// flight per app at a time; a caller that fails to acquire it must
    /// fail closed (deny the write), never proceed against a
    /// known-possibly-stale `count_key`.
    pub fn reconcile_lock_key(&self) -> String {
        format!("{}:reconcile-lock", self.app_prefix())
    }

    /// The per-invocation op-count key the rate limit is enforced against
    /// (task quota: "per-invocation op rate limit"). Scoped by the
    /// host-API `call_id` (`penguin_bundle_host::wire::HostCallBody::call_id`,
    /// "the originating `invoke` id", spec §6.6) rather than any
    /// in-process state, so the exact same limiter works whether the
    /// caller constructs a fresh capability set per invoke
    /// (`core/svc_process`) or reuses one across many invokes on a shared
    /// connection (`core/svc_action`) -- see `crate::KvHost`'s module doc.
    /// Short-lived: expires on its own shortly after the invocation ends,
    /// so no per-invocation cleanup is required.
    pub fn rate_key(&self, call_id: u64) -> String {
        format!("{}:opr:{}", self.app_prefix(), call_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn data_key_is_namespaced_by_tenant_community_and_app() {
        let scope = KvScope::new("acme", Some("main".to_string()), "waddles.bot.a");
        assert_eq!(
            scope.data_key("counter"),
            "bundlekv:acme:main:waddles.bot.a:data:counter"
        );
    }

    #[test]
    fn data_key_renders_tenant_wide_segment_when_community_is_none() {
        let scope = KvScope::new("acme", None, "waddles.bot.a");
        assert_eq!(
            scope.data_key("counter"),
            "bundlekv:acme:_tenant:waddles.bot.a:data:counter"
        );
    }

    #[test]
    fn two_apps_in_the_same_community_never_share_a_prefix() {
        let a = KvScope::new("acme", Some("main".to_string()), "waddles.bot.a");
        let b = KvScope::new("acme", Some("main".to_string()), "waddles.bot.b");
        assert_ne!(a.data_key("counter"), b.data_key("counter"));
        assert!(!b.data_key("counter").starts_with(&a.app_prefix()));
    }

    #[test]
    fn two_tenants_with_the_same_app_id_never_share_a_prefix() {
        let a = KvScope::new("acme", None, "waddles.bot.a");
        let b = KvScope::new("globex", None, "waddles.bot.a");
        assert_ne!(a.data_key("counter"), b.data_key("counter"));
    }

    #[test]
    fn count_and_rate_keys_cannot_collide_with_any_valid_data_key() {
        let scope = KvScope::new("acme", None, "waddles.bot.a");
        // Even a guest key literally named "count" or "opr:1" (the latter
        // is rejected by validate_guest_key for containing ':', proving
        // the point twice over) lands under `:data:`, never bare.
        assert_ne!(scope.data_key("count"), scope.count_key());
        assert_ne!(scope.data_key("opr"), scope.rate_key(1));
    }

    #[test]
    fn validate_guest_key_accepts_the_allowed_charset() {
        assert!(validate_guest_key("counters.viewer-count_1").is_ok());
    }

    #[test]
    fn validate_guest_key_rejects_empty() {
        assert_eq!(validate_guest_key(""), Err(KeyValidationError::TooLong(0)));
    }

    #[test]
    fn validate_guest_key_rejects_over_length() {
        let long = "a".repeat(MAX_GUEST_KEY_LEN + 1);
        assert_eq!(
            validate_guest_key(&long),
            Err(KeyValidationError::TooLong((MAX_GUEST_KEY_LEN + 1) as u64))
        );
    }

    #[test]
    fn validate_guest_key_rejects_colon_namespace_escape() {
        assert_eq!(
            validate_guest_key("other_app:data:secret"),
            Err(KeyValidationError::InvalidChars)
        );
    }

    #[test]
    fn validate_guest_key_rejects_glob_metacharacters() {
        for bad in ["*", "?", "[abc]", "a*b"] {
            assert_eq!(
                validate_guest_key(bad),
                Err(KeyValidationError::InvalidChars),
                "expected {bad:?} to be rejected"
            );
        }
    }

    #[test]
    fn validate_guest_key_rejects_path_traversal_shaped_input() {
        assert_eq!(
            validate_guest_key("../../etc/passwd"),
            Err(KeyValidationError::InvalidChars)
        );
    }

    #[test]
    fn validate_guest_key_rejects_control_characters_and_whitespace() {
        assert_eq!(
            validate_guest_key("line1\nline2"),
            Err(KeyValidationError::InvalidChars)
        );
        assert_eq!(
            validate_guest_key("has space"),
            Err(KeyValidationError::InvalidChars)
        );
    }
}

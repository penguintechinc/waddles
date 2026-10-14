//! Typed wire DTOs for hub-api's internal provider-connections routes
//! (`hub_api/blueprints/v1/provider_connections.py`, `DataPlaneBindingDTO` /
//! `DataPlaneListResponse` / `ResolvedCredentialsDTO`).
//!
//! [`AccessToken`] is the only secret-bearing type here: its `Debug`/`Display`
//! never print the value (length only), so a `{:?}` of a whole
//! [`ResolvedCredentials`] -- or any struct embedding one -- is safe to log.
//! The raw value is reachable only through the explicit
//! [`AccessToken::expose`] call, which exists for exactly one purpose: placing
//! it in the IRC `PASS` line.

use std::fmt;

use serde::Deserialize;
use serde_json::{Map, Value};

/// The hub-api provider id this subsystem drives.
pub const PROVIDER_TWITCH: &str = "twitch";

/// The adapter capability a Twitch binding must grant to be ingested
/// (`hub_api/services/provider_connections/twitch.py`'s `CAPABILITY_CHAT_INGEST`).
pub const CAPABILITY_CHAT_INGEST: &str = "chat_ingest";

/// Longest Twitch login (nick/channel) -- Twitch's own limit, and the bound
/// [`normalize_login`] enforces before a value may reach an IRC wire line.
const MAX_LOGIN_LEN: usize = 25;

/// A decrypted OAuth access token. Redacted in every formatting trait; see the
/// module doc.
#[derive(Clone, Deserialize)]
#[serde(transparent)]
pub struct AccessToken(String);

impl AccessToken {
    /// Wraps a raw token (tests and the client's own decode path).
    #[must_use]
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// The raw token. Callers must never log, trace, or label with this value.
    #[must_use]
    pub fn expose(&self) -> &str {
        &self.0
    }

    /// True when the hub returned an empty token -- treated as a protocol fault.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for AccessToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "AccessToken(***redacted***, len={})", self.0.len())
    }
}

impl fmt::Display for AccessToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "***redacted***(len={})", self.0.len())
    }
}

impl PartialEq for AccessToken {
    /// Constant-time-ish byte comparison -- used only to decide whether a
    /// re-resolved token differs from the one a socket is already using.
    fn eq(&self, other: &Self) -> bool {
        use subtle::ConstantTimeEq;
        self.0.as_bytes().ct_eq(other.0.as_bytes()).into()
    }
}

impl Eq for AccessToken {}

/// One (community, connection) pair the data plane should run. Carries no
/// token (`DataPlaneBindingDTO`).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DataPlaneBinding {
    /// The grant id -- the stable, monotonically increasing pagination cursor
    /// and this binding's identity across polls.
    pub grant_id: i64,
    /// The shared connection row backing this binding.
    pub connection_id: i64,
    /// The community this binding feeds.
    pub community_id: i64,
    /// The owning tenant (numeric id).
    pub tenant_id: i64,
    /// Provider id (always `twitch` here -- the list is filtered server-side,
    /// re-checked client-side).
    pub provider: String,
    /// Adapter resource type (`channel` for Twitch).
    #[serde(default)]
    pub resource_type: String,
    /// Provider-stable resource id (the Twitch channel's user id).
    #[serde(default)]
    pub resource_id: String,
    /// Capabilities the stored scopes actually grant.
    #[serde(default)]
    pub capabilities: Vec<String>,
    /// Token expiry as hub-api reports it (informational; never acted on).
    #[serde(default)]
    pub expires_at: Option<String>,
    /// Last-modified stamp -- the "changed binding" signal between polls.
    #[serde(default)]
    pub updated_at: Option<String>,
    /// Runtime metadata allow-list (`channel_login`, `bot_login`, ...). Never
    /// contains a token.
    #[serde(default)]
    pub metadata: Map<String, Value>,
    /// Optional tenant slug -- the spine `Scope`'s tenant segment. Not part of
    /// hub-api's contract today (forward-compatible, additive): see
    /// `crate::connections::supervisor::ScopeMode::Binding`.
    #[serde(default)]
    pub tenant_slug: Option<String>,
    /// Optional community name -- the spine `Scope`'s community segment.
    #[serde(default)]
    pub community_name: Option<String>,
}

impl DataPlaneBinding {
    fn meta_str(&self, key: &str) -> Option<&str> {
        self.metadata.get(key).and_then(Value::as_str)
    }

    /// The validated, normalized channel login (`metadata.channel_login`), or
    /// `None` when absent/malformed. Validation is an allow-list, so a value
    /// can never smuggle IRC control characters or extra channels into a
    /// `JOIN` line.
    #[must_use]
    pub fn channel_login(&self) -> Option<String> {
        self.meta_str("channel_login").and_then(normalize_login)
    }

    /// The validated, normalized bot login (`metadata.bot_login`, the IRC
    /// nick), or `None` when absent/malformed.
    #[must_use]
    pub fn bot_login(&self) -> Option<String> {
        self.meta_str("bot_login").and_then(normalize_login)
    }
}

/// One page of the internal list (`DataPlaneListResponse`).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BindingPage {
    /// Always `true` on a 2xx (errors use a different envelope).
    #[serde(default)]
    pub success: bool,
    /// The bindings on this page.
    #[serde(default)]
    pub items: Vec<DataPlaneBinding>,
    /// Cursor for the next page, `None` on the last page.
    #[serde(default)]
    pub next_after: Option<i64>,
}

/// A fresh access token plus the metadata needed to build the IRC tuple
/// (`ResolvedCredentialsDTO`). The only secret-bearing response in the API;
/// `Cache-Control: no-store`.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResolvedCredentials {
    /// Always `true` on a 2xx.
    #[serde(default)]
    pub success: bool,
    /// Echo of the requested connection id.
    pub connection_id: i64,
    /// Echo of the requested community id.
    pub community_id: i64,
    /// Provider id.
    pub provider: String,
    /// The decrypted access token. Redacted in `Debug`.
    pub access_token: AccessToken,
    /// Token expiry (informational).
    #[serde(default)]
    pub expires_at: Option<String>,
    /// Runtime metadata (`bot_login`, `channel_login`, `bot_user_id`,
    /// `channel_id`, `client_id`, `scopes`).
    #[serde(default)]
    pub metadata: Map<String, Value>,
}

impl ResolvedCredentials {
    /// The validated bot login the token authenticates as, if hub-api sent one.
    #[must_use]
    pub fn bot_login(&self) -> Option<String> {
        self.metadata
            .get("bot_login")
            .and_then(Value::as_str)
            .and_then(normalize_login)
    }
}

/// Lowercases `raw`, strips one leading `#`, and accepts it only if it is a
/// plausible Twitch login: 1-25 characters of `[a-z0-9_]`. Returns `None`
/// otherwise. This is the allow-list every value passes before it can reach an
/// IRC `NICK`/`JOIN` line or a spine stream key.
#[must_use]
pub fn normalize_login(raw: &str) -> Option<String> {
    let trimmed = raw.trim().trim_start_matches('#').to_ascii_lowercase();
    let valid = !trimmed.is_empty()
        && trimmed.len() <= MAX_LOGIN_LEN
        && trimmed
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    valid.then_some(trimmed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn binding_json() -> Value {
        json!({
            "grantId": 7, "connectionId": 11, "communityId": 3, "tenantId": 2,
            "provider": "twitch", "resourceType": "channel", "resourceId": "9001",
            "capabilities": ["chat_ingest", "action_relay"],
            "expiresAt": "2026-10-14T12:00:00+00:00",
            "updatedAt": "2026-10-14T10:00:00+00:00",
            "metadata": {"channel_login": "PenguinzPlays", "bot_login": "waddlebots",
                         "client_id": "abc", "scopes": ["chat:read"]}
        })
    }

    #[test]
    fn binding_parses_the_documented_wire_shape() {
        let b: DataPlaneBinding = serde_json::from_value(binding_json()).unwrap();
        assert_eq!(b.grant_id, 7);
        assert_eq!(b.connection_id, 11);
        assert_eq!(b.community_id, 3);
        assert_eq!(b.tenant_id, 2);
        assert_eq!(b.capabilities, ["chat_ingest", "action_relay"]);
        // Mixed-case metadata is normalized.
        assert_eq!(b.channel_login().as_deref(), Some("penguinzplays"));
        assert_eq!(b.bot_login().as_deref(), Some("waddlebots"));
        assert_eq!(b.tenant_slug, None, "optional forward-compatible field");
    }

    #[test]
    fn binding_tolerates_missing_optional_fields_and_unknown_ones() {
        let b: DataPlaneBinding = serde_json::from_value(json!({
            "grantId": 1, "connectionId": 2, "communityId": 3, "tenantId": 4,
            "provider": "twitch", "someFutureField": true
        }))
        .unwrap();
        assert!(b.metadata.is_empty());
        assert_eq!(b.channel_login(), None);
        assert_eq!(b.bot_login(), None);
    }

    #[test]
    fn page_parses_cursor_and_last_page() {
        let page: BindingPage = serde_json::from_value(json!({
            "success": true, "items": [binding_json()], "nextAfter": 7
        }))
        .unwrap();
        assert_eq!(page.items.len(), 1);
        assert_eq!(page.next_after, Some(7));
        let last: BindingPage =
            serde_json::from_value(json!({"success": true, "items": [], "nextAfter": null}))
                .unwrap();
        assert_eq!(last.next_after, None);
        assert!(last.items.is_empty());
    }

    #[test]
    fn resolved_credentials_parse_and_never_debug_print_the_token() {
        let r: ResolvedCredentials = serde_json::from_value(json!({
            "success": true, "connectionId": 11, "communityId": 3, "provider": "twitch",
            "accessToken": "SUPER-SECRET-TOKEN-VALUE", "expiresAt": null,
            "metadata": {"bot_login": "WaddleBots", "channel_login": "chan"}
        }))
        .unwrap();
        assert_eq!(r.access_token.expose(), "SUPER-SECRET-TOKEN-VALUE");
        assert_eq!(r.bot_login().as_deref(), Some("waddlebots"));
        let debug = format!("{r:?}");
        let display = r.access_token.to_string();
        assert!(!debug.contains("SUPER-SECRET"), "Debug leaked: {debug}");
        assert!(!display.contains("SUPER-SECRET"), "Display leaked: {display}");
        assert!(debug.contains("redacted"));
    }

    #[test]
    fn access_token_equality_is_value_based() {
        assert_eq!(AccessToken::new("a"), AccessToken::new("a"));
        assert_ne!(AccessToken::new("a"), AccessToken::new("b"));
        assert_ne!(AccessToken::new("a"), AccessToken::new("ab"));
        assert!(AccessToken::new("").is_empty());
        assert!(!AccessToken::new("x").is_empty());
    }

    #[test]
    fn normalize_login_is_a_strict_allow_list() {
        assert_eq!(normalize_login("#SomeChannel").as_deref(), Some("somechannel"));
        assert_eq!(normalize_login("  bot_01 ").as_deref(), Some("bot_01"));
        assert_eq!(normalize_login(""), None);
        assert_eq!(normalize_login("#"), None);
        assert_eq!(normalize_login(&"a".repeat(26)), None);
        assert_eq!(normalize_login(&"a".repeat(25)).map(|s| s.len()), Some(25));
        // IRC injection shapes are all rejected, never sanitized into validity.
        for hostile in [
            "chan\r\nQUIT",
            "chan,#other",
            "chan name",
            "chan\0",
            "ch@n",
            "chan:evil",
            "../x",
        ] {
            assert_eq!(normalize_login(hostile), None, "{hostile:?} must be rejected");
        }
    }
}

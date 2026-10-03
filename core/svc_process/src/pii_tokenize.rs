//! Inbound PII-tokenization pre-dispatch pass (PII boundary hard
//! invariant, `rules/critical-rules.md` PII Tokenization: "the API server
//! is the PII boundary ... everything outside ... references users by
//! UUID/token only, never raw PII"). Runs in [`crate::spine::
//! handle_delivered`] immediately after hop verification and strictly
//! before `invoke_transform` -- the actual guest-dispatch boundary -- so a
//! bundle's `transform` invoke never receives a raw platform username,
//! login, display name, or free-text `@mention`.
//!
//! Identity resolution is delegated entirely to hub-api's
//! `waddles.hub.internal.v1.IdentityService.MintEphemeralPseudonyms` RPC
//! (`core/hub_client`) -- this module never derives a pseudonym locally
//! (a fixed-namespace `UUIDv5` of a public handle is reversible by
//! dictionary attack; hub-api mints inside the PII boundary with a
//! per-tenant secret instead). [`IdentityMinter`] is a narrow seam over
//! that RPC (mirrors `crate::license::FeatureGate`'s "wrap the external
//! dependency behind a one-method trait" pattern) so this module's own
//! tests never perform real gRPC I/O.
//!
//! **Fail-closed, not fail-open:** any minting failure (hub-api
//! unreachable, circuit open, no minter configured, or a requested
//! identity missing from the response) is returned as [`TokenizeError`] --
//! `crate::spine::handle_delivered` dead-letters the entry rather than
//! ever forwarding the raw event to a bundle. There is no local fallback
//! pseudonym; a transient hub-api outage degrades to "events queue for
//! redelivery," never to "events leak PII."

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, OnceLock};

use penguin_spine::PlatformEvent;
use regex::Regex;
use serde_json::Value;

/// Every failure [`tokenize_event`] can raise -- all fail-closed (never a
/// partial/best-effort tokenization reaches the caller).
#[derive(Debug, thiserror::Error)]
pub enum TokenizeError {
    #[error("identity resolution unavailable: {0}")]
    ResolutionUnavailable(String),
}

/// One (platform, platform_user_id, handle) tuple this module needs
/// resolved to a token. `handle` is the display name at mint time --
/// stored only inside hub-api's PII boundary, never echoed back in the
/// response (`waddles.hub.internal.v1.MintEphemeralPseudonymRequest`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MintItem {
    pub platform: String,
    pub platform_user_id: String,
    pub handle: String,
}

/// A pending identity-mint resolution: maps `MintItem::platform_user_id`
/// -> pseudonym string (opaque -- this module never parses it as a UUID,
/// it only ever re-emits it verbatim inside a `{user:<token>}`
/// placeholder).
pub type MintResult<'a> =
    Pin<Box<dyn Future<Output = Result<HashMap<String, String>, TokenizeError>> + Send + 'a>>;

/// Resolves a batch of [`MintItem`]s to pseudonym tokens in one call --
/// object-safe (manually-boxed future, matching `crate::license::
/// FeatureGate`'s identical rationale) so `crate::spine::ProcessDeps` can
/// hold `Option<Arc<dyn IdentityMinter>>` without an `async-trait`
/// dependency.
pub trait IdentityMinter: Send + Sync {
    fn mint_many<'a>(&'a self, tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a>;
}

/// Production [`IdentityMinter`]: a thin adapter over `hub_client::
/// HubClient::mint_ephemeral_pseudonyms`.
pub struct HubClientMinter(pub Arc<hub_client::HubClient>);

impl IdentityMinter for HubClientMinter {
    fn mint_many<'a>(&'a self, tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
        Box::pin(async move {
            let proto_items = items
                .into_iter()
                .map(|i| hub_client::pb::MintEphemeralPseudonymRequest {
                    tenant_id: tenant_id.to_string(),
                    platform: i.platform,
                    platform_user_id: i.platform_user_id,
                    handle: i.handle,
                })
                .collect();
            match self.0.mint_ephemeral_pseudonyms(proto_items).await {
                Ok(pseudonyms) => Ok(pseudonyms
                    .into_iter()
                    .map(|p| (p.platform_user_id, p.pseudonym))
                    .collect()),
                Err(err) => Err(TokenizeError::ResolutionUnavailable(err.to_string())),
            }
        })
    }
}

/// Renders a resolved token as the bundle-visible placeholder text (spec:
/// "a bundle only ever emits/receives `{user:<uuid>}` placeholders").
pub fn format_user_token(token: &str) -> String {
    format!("{{user:{token}}}")
}

/// Escapes every literal `{`, `}`, and `\` in `text` *before* this pass
/// inserts any `{user:<token>}` placeholder of its own, so a sequence a
/// user typed verbatim in chat can never be confused with a placeholder
/// this pass itself inserted (forgery protection). Backslash-escaping is
/// unambiguous and reversible (`\{`, `\}`, `\\`) -- the same convention
/// `core/egress_detokenizer`'s grammar scanner treats as "not a real
/// placeholder."
pub fn escape_braces(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '{' => out.push_str("\\{"),
            '}' => out.push_str("\\}"),
            '\\' => out.push_str("\\\\"),
            other => out.push(other),
        }
    }
    out
}

fn discord_mention_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"<@!?(\d+)>").expect("valid regex"))
}

/// Twitch/IRC username grammar (Twitch Helix: 3-25 alphanumeric/underscore
/// characters).
fn twitch_handle_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"@([A-Za-z0-9_]{3,25})\b").expect("valid regex"))
}

/// The free-text field every `core/svc_ingest::normalize` payload puts
/// user-authored chat content in (Twitch IRC's `text`, Discord's `text`
/// copied from `msg.content`). Twitch EventSub payloads carry no such
/// field, so the mention scan is naturally a no-op for those event types.
const TEXT_FIELD: &str = "text";

/// `(platform_user_id field, raw-name-duplicate field)` pairs
/// `core/svc_ingest::normalize` populates alongside `PlatformEvent::actor`
/// -- both must be tokenized identically, and a raid's
/// `broadcaster_user_id`/`broadcaster_login` is a *different* identity
/// from the actor, never collapsed into the same token.
const ID_NAME_FIELD_PAIRS: &[(&str, &str)] = &[
    ("user_id", "user_login"),
    ("user_id", "user_display_name"),
    ("author_id", "author"),
    ("broadcaster_user_id", "broadcaster_login"),
];

/// Where a resolved token gets written back once minting succeeds.
enum SubstTarget {
    Actor,
    JsonField(&'static str),
    Mention { matched_text: String },
}

/// Tokenizes `event`: replaces [`PlatformEvent::actor`], every recognized
/// raw-name-duplicate payload field, and every recognized free-text
/// `@mention`/structured mention with a `{user:<token>}` placeholder.
///
/// Returns a new event -- never mutates `event` in place, so a caller that
/// still holds the original (e.g. for logging a dead-letter reason) can't
/// accidentally forward the un-tokenized copy.
///
/// **Fails closed**: any minter error, or any requested identity missing
/// from the minter's response, aborts tokenization entirely and returns
/// [`TokenizeError`] -- never a partially-tokenized event.
pub async fn tokenize_event(
    event: &PlatformEvent,
    tenant_id: &str,
    minter: &dyn IdentityMinter,
) -> Result<PlatformEvent, TokenizeError> {
    let mut payload = event.payload.clone();

    // Forgery protection: escape braces in free text BEFORE scanning for
    // mentions, so a literal `{user:...}` a user typed can't survive or be
    // re-interpreted as a real placeholder.
    if let Some(Value::String(text)) = payload.get_mut(TEXT_FIELD) {
        *text = escape_braces(text);
    }

    let platform = event.platform.clone();
    let mut items = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut targets: Vec<(String, SubstTarget)> = Vec::new();

    let mut push_item = |key: String,
                         handle: String,
                         target: SubstTarget,
                         items: &mut Vec<MintItem>,
                         targets: &mut Vec<(String, SubstTarget)>| {
        if seen.insert(key.clone()) {
            items.push(MintItem {
                platform: platform.clone(),
                platform_user_id: key.clone(),
                handle,
            });
        }
        targets.push((key, target));
    };

    // Actor: prefer the structured `user_id`/`author_id` the payload
    // carries; fall back to a handle-derived key (never confusable with a
    // real numeric id) when neither is present.
    if let Some(actor) = &event.actor {
        let key = payload
            .get("user_id")
            .and_then(Value::as_str)
            .or_else(|| payload.get("author_id").and_then(Value::as_str))
            .map(str::to_string)
            .unwrap_or_else(|| format!("handle:{}", actor.to_ascii_lowercase()));
        push_item(
            key,
            actor.clone(),
            SubstTarget::Actor,
            &mut items,
            &mut targets,
        );
    }

    // Raw-name-duplicate fields: tokenize the name field, keyed by its
    // paired id field when present.
    for (id_field, name_field) in ID_NAME_FIELD_PAIRS {
        let Some(name) = payload
            .get(*name_field)
            .and_then(Value::as_str)
            .map(str::to_string)
        else {
            continue;
        };
        let key = payload
            .get(*id_field)
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| format!("handle:{}", name.to_ascii_lowercase()));
        push_item(
            key,
            name,
            SubstTarget::JsonField(name_field),
            &mut items,
            &mut targets,
        );
    }

    // Free-text mentions.
    if let Some(Value::String(text)) = payload.get(TEXT_FIELD) {
        for cap in discord_mention_regex().captures_iter(text) {
            let id = cap[1].to_string();
            push_item(
                id,
                String::new(),
                SubstTarget::Mention {
                    matched_text: cap[0].to_string(),
                },
                &mut items,
                &mut targets,
            );
        }
        for cap in twitch_handle_regex().captures_iter(text) {
            let handle = cap[1].to_string();
            let key = format!("handle:{}", handle.to_ascii_lowercase());
            push_item(
                key,
                handle,
                SubstTarget::Mention {
                    matched_text: cap[0].to_string(),
                },
                &mut items,
                &mut targets,
            );
        }
    }

    let resolved = if items.is_empty() {
        HashMap::new()
    } else {
        minter.mint_many(tenant_id, items).await?
    };

    let mut new_actor = event.actor.clone();
    let mut text_accum: Option<String> = payload
        .get(TEXT_FIELD)
        .and_then(Value::as_str)
        .map(str::to_string);

    for (key, target) in targets {
        // Fail-closed: a requested identity missing from the response is
        // treated exactly like a transport failure -- never silently fall
        // back to the raw value.
        let Some(token_val) = resolved.get(&key) else {
            return Err(TokenizeError::ResolutionUnavailable(format!(
                "no pseudonym returned for platform_user_id {key}"
            )));
        };
        let token = format_user_token(token_val);
        match target {
            SubstTarget::Actor => new_actor = Some(token),
            SubstTarget::JsonField(field) => {
                payload.insert(field.to_string(), Value::String(token));
            }
            SubstTarget::Mention { matched_text } => {
                if let Some(text) = &mut text_accum {
                    *text = text.replace(&matched_text, &token);
                }
            }
        }
    }

    if let Some(text) = text_accum {
        payload.insert(TEXT_FIELD.to_string(), Value::String(text));
    }

    Ok(PlatformEvent {
        platform: event.platform.clone(),
        event_type: event.event_type.clone(),
        actor: new_actor,
        payload,
        occurred_at: event.occurred_at.clone(),
        source: event.source.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(actor: Option<&str>, payload: serde_json::Value) -> PlatformEvent {
        PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: actor.map(str::to_string),
            payload: payload.as_object().cloned().unwrap_or_default(),
            occurred_at: "2026-10-03T00:00:00.000Z".to_string(),
            source: None,
        }
    }

    fn payload_string(ev: &PlatformEvent) -> String {
        serde_json::to_string(&ev.payload).unwrap()
    }

    /// A minter that always succeeds, returning `"tok-<platform_user_id>"`
    /// for every item it's asked to mint -- deterministic and
    /// order-independent, so tests can assert exact substitution without
    /// a real gRPC server.
    struct FakeMinter;

    impl IdentityMinter for FakeMinter {
        fn mint_many<'a>(&'a self, _tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
            Box::pin(async move {
                Ok(items
                    .into_iter()
                    .map(|i| {
                        (
                            i.platform_user_id.clone(),
                            format!("tok-{}", i.platform_user_id),
                        )
                    })
                    .collect())
            })
        }
    }

    /// Simulates hub-api being unreachable -- every call fails.
    struct AlwaysFailMinter;

    impl IdentityMinter for AlwaysFailMinter {
        fn mint_many<'a>(&'a self, _tenant_id: &'a str, _items: Vec<MintItem>) -> MintResult<'a> {
            Box::pin(async move {
                Err(TokenizeError::ResolutionUnavailable(
                    "hub-api unreachable (simulated)".to_string(),
                ))
            })
        }
    }

    /// A minter that mints everything EXCEPT the given platform_user_id --
    /// simulates a partial/short response from hub-api.
    struct PartialMinter(&'static str);

    impl IdentityMinter for PartialMinter {
        fn mint_many<'a>(&'a self, _tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
            let missing = self.0;
            Box::pin(async move {
                Ok(items
                    .into_iter()
                    .filter(|i| i.platform_user_id != missing)
                    .map(|i| {
                        (
                            i.platform_user_id.clone(),
                            format!("tok-{}", i.platform_user_id),
                        )
                    })
                    .collect())
            })
        }
    }

    #[test]
    fn escape_braces_escapes_brace_and_backslash_characters() {
        assert_eq!(escape_braces("hi {user:x} \\"), "hi \\{user:x\\} \\\\");
        assert_eq!(escape_braces("no braces here"), "no braces here");
    }

    #[test]
    fn format_user_token_wraps_the_token_in_the_placeholder_grammar() {
        assert_eq!(format_user_token("abc-123"), "{user:abc-123}");
    }

    #[tokio::test]
    async fn tokenizes_actor_and_known_raw_name_fields() {
        let ev = event(
            Some("CoolStreamer"),
            serde_json::json!({
                "user_id": "999",
                "user_login": "coolstreamer",
                "text": "hello chat",
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        assert_eq!(out.actor.as_deref(), Some("{user:tok-999}"));
        assert_eq!(
            out.payload.get("user_login").and_then(Value::as_str),
            Some("{user:tok-999}")
        );
        // No raw substrings survive anywhere in the event.
        let dump = payload_string(&out);
        assert!(!dump.to_lowercase().contains("coolstreamer"));
    }

    #[tokio::test]
    async fn tokenizes_discord_structured_mentions_in_text() {
        let ev = event(
            Some("asker"),
            serde_json::json!({
                "user_id": "1",
                "text": "hey <@123456789012345678> check this out",
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        let text = out.payload.get("text").and_then(Value::as_str).unwrap();
        assert!(text.contains("{user:tok-123456789012345678}"));
        assert!(!text.contains("<@123456789012345678>"));
    }

    #[tokio::test]
    async fn tokenizes_twitch_irc_handle_mentions_in_text() {
        let ev = event(
            Some("asker"),
            serde_json::json!({
                "user_id": "1",
                "text": "thanks @SomeViewer for the raid",
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        let text = out.payload.get("text").and_then(Value::as_str).unwrap();
        assert!(text.contains("{user:tok-handle:someviewer}"));
        assert!(!text.to_lowercase().contains("@someviewer"));
    }

    #[tokio::test]
    async fn raid_actor_and_broadcaster_resolve_to_distinct_tokens() {
        let ev = event(
            Some("raider"),
            serde_json::json!({
                "user_id": "111",
                "broadcaster_user_id": "222",
                "broadcaster_login": "targetchannel",
                "text": "raid incoming",
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        assert_eq!(out.actor.as_deref(), Some("{user:tok-111}"));
        let broadcaster = out.payload.get("broadcaster_login").and_then(Value::as_str);
        assert_eq!(broadcaster, Some("{user:tok-222}"));
        assert_ne!(out.actor.as_deref(), broadcaster);
    }

    #[tokio::test]
    async fn forged_placeholder_in_chat_text_is_escaped_not_substituted() {
        let ev = event(
            Some("attacker"),
            serde_json::json!({
                "user_id": "1",
                "text": "{user:00000000-0000-0000-0000-000000000000}",
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        let text = out.payload.get("text").and_then(Value::as_str).unwrap();
        // Escaped, never passed through unescaped as a literal placeholder.
        assert_eq!(text, "\\{user:00000000-0000-0000-0000-000000000000\\}");
    }

    #[tokio::test]
    async fn no_actor_and_no_recognized_pii_fields_returns_event_unchanged_aside_from_escaping() {
        let ev = event(None, serde_json::json!({"text": "no pii here"}));
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        assert_eq!(out.actor, None);
        assert_eq!(
            out.payload.get("text").and_then(Value::as_str),
            Some("no pii here")
        );
    }

    #[tokio::test]
    async fn minter_failure_is_fail_closed_never_returns_a_partial_event() {
        let ev = event(
            Some("someone"),
            serde_json::json!({"user_id": "1", "text": "hi"}),
        );
        let err = tokenize_event(&ev, "tenant-1", &AlwaysFailMinter)
            .await
            .unwrap_err();
        assert!(matches!(err, TokenizeError::ResolutionUnavailable(_)));
    }

    #[tokio::test]
    async fn partial_mint_response_is_fail_closed() {
        let ev = event(
            Some("someone"),
            serde_json::json!({"user_id": "1", "text": "hi"}),
        );
        let err = tokenize_event(&ev, "tenant-1", &PartialMinter("1"))
            .await
            .unwrap_err();
        assert!(matches!(err, TokenizeError::ResolutionUnavailable(_)));
    }

    #[tokio::test]
    async fn source_metadata_passes_through_unchanged() {
        let mut ev = event(
            Some("someone"),
            serde_json::json!({"user_id": "1", "text": "hi"}),
        );
        ev.source = Some(penguin_spine::Source {
            platform: "twitch".to_string(),
            account_id: "bot-1".to_string(),
            channel_id: Some("chan-1".to_string()),
        });
        let out = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        assert_eq!(out.source, ev.source);
    }
}

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
use sha2::{Digest, Sha256};

use crate::identity::{MentionBinding, MentionRef};

/// Every failure [`tokenize_event`] can raise -- all fail-closed (never a
/// partial/best-effort tokenization reaches the caller).
#[derive(Debug, thiserror::Error)]
pub enum TokenizeError {
    #[error("identity resolution unavailable: {0}")]
    ResolutionUnavailable(String),
    /// Closed-schema fail-closed gate (sec review 2026-10-03): `event`
    /// carries a top-level payload field this module has no vetted
    /// classification for. A new `core/svc_ingest::normalize` field must be
    /// consciously classified as `IDENTITY` (added to
    /// [`ID_NAME_FIELD_PAIRS`]) or `SAFE` (added to
    /// [`SAFE_PAYLOAD_FIELDS`]) before it can ever reach a bundle --
    /// silently passing an unclassified field through is exactly the
    /// allowlist fail-open bug this variant exists to close off.
    #[error("payload field '{field}' has no vetted PII classification (fail-closed)")]
    UnclassifiedField { field: String },
    /// Same gate as [`TokenizeError::UnclassifiedField`], scoped to the
    /// nested `metadata` sub-object Twitch EventSub normalization emits
    /// (`core/svc_ingest::normalize::normalize_twitch_eventsub`) -- its keys
    /// are classified separately in [`METADATA_SAFE_FIELDS`] since they vary
    /// per `event_type` and are never scanned as top-level fields.
    #[error("metadata field '{field}' has no vetted PII classification (fail-closed)")]
    UnclassifiedMetadataField { field: String },
}

/// Sanitizes a mint-item key for error messages and log/trace fields --
/// never place a raw handle in an error or log line (sec review
/// 2026-10-03: `TokenizeError`'s own Display previously embedded the raw
/// `handle:<username>` fallback key verbatim, and `spine.rs` logs that
/// Display at `tracing::error!`). A numeric platform id (`"12345"`) is not
/// itself a handle and is safe to surface as-is; a `handle:<raw>` fallback
/// key (used only when the payload carries no structured id) is hashed
/// (SHA-256, first 16 hex chars -- enough to correlate repeat failures,
/// not reversible) so the raw value never appears in any error, log, or
/// metric.
fn sanitize_mint_key(key: &str) -> String {
    match key.strip_prefix("handle:") {
        Some(handle) => {
            let digest = Sha256::digest(handle.as_bytes());
            let hex: String = digest.iter().take(8).map(|b| format!("{b:02x}")).collect();
            format!("handle:sha256:{hex}")
        }
        None => key.to_string(),
    }
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

/// The nested sub-object `normalize_twitch_eventsub` folds per-event-type
/// metadata into -- validated as its own closed schema
/// ([`METADATA_SAFE_FIELDS`]) rather than at the top level, since its key
/// set varies by `event_type` and none of its fields are identity fields.
const METADATA_FIELD: &str = "metadata";

/// `(platform_user_id field, raw-name-duplicate field)` pairs
/// `core/svc_ingest::normalize` populates alongside `PlatformEvent::actor`
/// -- both must be tokenized identically, and a raid's
/// `broadcaster_user_id`/`broadcaster_login` is a *different* identity
/// from the actor, never collapsed into the same token.
///
/// This is the single source of truth for which payload fields are
/// `IDENTITY` (must-tokenize) -- [`identity_payload_fields`] derives its
/// set from this list's name-fields, so a field can never be "classified
/// identity" without also being wired into the actual substitution loop
/// below.
///
/// `("user_id", "display_name")`: regression fix (sec review 2026-10-03,
/// `rules/critical-rules.md` PII Tokenization) -- `normalize_twitch_irc`
/// emits a raw Twitch display name under the key `display_name`, but this
/// list previously had no entry naming it, so [`tokenize_event`]'s
/// substitution loop never touched it and the allowlist's fail-open gap
/// let it reach the bundle (and therefore every community WASM bundle on
/// the Twitch chat path) in cleartext. Keyed by `user_id` -- the same
/// numeric identity `("author_id", "author")` resolves for the same
/// message (`normalize_twitch_irc` sets `author_id`/`user_id` from the
/// identical IRCv3 `user-id` tag), so both fields mint to the same token.
const ID_NAME_FIELD_PAIRS: &[(&str, &str)] = &[
    ("user_id", "user_login"),
    ("user_id", "user_display_name"),
    ("user_id", "display_name"),
    ("author_id", "author"),
    ("broadcaster_user_id", "broadcaster_login"),
];

/// Every `core/svc_ingest::normalize` top-level payload field this module
/// has vetted as containing **no** personal identity -- closed-schema
/// fail-closed gate (sec review 2026-10-03): [`validate_known_schema`]
/// rejects (dead-letters, via [`TokenizeError::UnclassifiedField`]) any
/// payload field that is neither in this list nor an `IDENTITY` field
/// (derived from [`ID_NAME_FIELD_PAIRS`] by [`identity_payload_fields`]),
/// rather than ever silently forwarding an unrecognized field. Adding a
/// new `normalize.rs` field now requires landing it here (if non-identity)
/// or in `ID_NAME_FIELD_PAIRS` (if identity) -- the event simply stops
/// (dead-letters) until a human makes that call, replacing the previous
/// enumerated-allowlist design where an unlisted field passed through by
/// default.
///
/// `TEXT_FIELD`/`METADATA_FIELD` are validated separately (scanned/escaped,
/// and nested-schema-checked, respectively) and are deliberately excluded
/// from this list.
const SAFE_PAYLOAD_FIELDS: &[&str] = &[
    // normalize_twitch_irc
    "channel_name",
    "author_id",
    "user_id",
    "message_id",
    "room_id",
    "badges",
    "is_mod",
    "is_subscriber",
    "is_vip",
    "is_broadcaster",
    // normalize_discord
    "guild_id",
    "channel_id",
    // normalize_twitch_eventsub
    "broadcaster_id",
    "broadcaster_user_id",
];

/// Every key `normalize_twitch_eventsub` may place inside the nested
/// `metadata` sub-object, across all its `event_type` branches -- closed
/// schema, same fail-closed rationale as [`SAFE_PAYLOAD_FIELDS`]. None of
/// these are identity fields (tier/counts/flags/timestamps only).
const METADATA_SAFE_FIELDS: &[&str] = &[
    "tier",
    "is_gift",
    "total",
    "is_anonymous",
    "viewers",
    "bits",
    "type",
    "started_at",
];

/// The set of payload field names [`ID_NAME_FIELD_PAIRS`] tokenizes --
/// derived once from that list (never hand-duplicated) so "classified
/// IDENTITY" and "actually wired into the substitution loop" can never
/// drift apart.
fn identity_payload_fields() -> &'static HashSet<&'static str> {
    static SET: OnceLock<HashSet<&'static str>> = OnceLock::new();
    SET.get_or_init(|| ID_NAME_FIELD_PAIRS.iter().map(|(_, name)| *name).collect())
}

/// Closed-schema fail-closed gate (sec review 2026-10-03, regression: raw
/// `display_name` leaked to bundle; allowlist was fail-open). Rejects
/// `event.payload` if it carries any top-level field outside
/// `identity_payload_fields() ∪ SAFE_PAYLOAD_FIELDS ∪ {TEXT_FIELD,
/// METADATA_FIELD}`, or if a `metadata` sub-object carries any key outside
/// [`METADATA_SAFE_FIELDS`] -- an unrecognized field is treated as
/// *potential* PII and dead-lettered rather than ever silently passed
/// through to a bundle.
fn validate_known_schema(payload: &serde_json::Map<String, Value>) -> Result<(), TokenizeError> {
    let identity = identity_payload_fields();
    for key in payload.keys() {
        if key == TEXT_FIELD || key == METADATA_FIELD {
            continue;
        }
        if identity.contains(key.as_str()) || SAFE_PAYLOAD_FIELDS.contains(&key.as_str()) {
            continue;
        }
        return Err(TokenizeError::UnclassifiedField { field: key.clone() });
    }
    if let Some(Value::Object(metadata)) = payload.get(METADATA_FIELD) {
        for key in metadata.keys() {
            if !METADATA_SAFE_FIELDS.contains(&key.as_str()) {
                return Err(TokenizeError::UnclassifiedMetadataField { field: key.clone() });
            }
        }
    }
    Ok(())
}

/// Where a resolved token gets written back once minting succeeds.
enum SubstTarget {
    Actor,
    JsonField(&'static str),
    Mention {
        matched_text: String,
        /// The raw reference this mention stands for -- kept HOST-SIDE only
        /// (it feeds [`TokenizedEvent::mentions`], never the event handed to
        /// a bundle) so the `identity` capability can resolve it later.
        reference: MentionRef,
    },
}

/// One free-text mention found by [`scan_mentions`].
pub(crate) struct ScannedMention {
    /// The exact text as typed (`<@123>`, `@bob`) -- what gets replaced by a
    /// `{user:<token>}` placeholder, and the lookup key when tokenization is off.
    pub matched_text: String,
    /// The mint key: the numeric platform id, or `handle:<lower-case>`.
    pub mint_key: String,
    /// The display handle handed to the mint RPC (empty for a numeric mention).
    pub mint_handle: String,
    /// The raw reference, for host-side identity resolution.
    pub reference: MentionRef,
}

/// Scans free `text` for structured Discord mentions (`<@id>` / `<@!id>`)
/// then Twitch/IRC `@handle` mentions, in that order. The single scanner both
/// [`tokenize_event`] and the untokenized identity path
/// (`crate::identity::InvocationIdentity::for_untokenized_event`) use, so the
/// two can never disagree about what counts as a mention.
pub(crate) fn scan_mentions(text: &str) -> Vec<ScannedMention> {
    let mut out = Vec::new();
    let mut discord_spans: Vec<(usize, usize)> = Vec::new();
    for cap in discord_mention_regex().captures_iter(text) {
        if let Some(whole) = cap.get(0) {
            discord_spans.push((whole.start(), whole.end()));
        }
        out.push(ScannedMention {
            matched_text: cap[0].to_string(),
            mint_key: cap[1].to_string(),
            mint_handle: String::new(),
            reference: MentionRef::PlatformId(cap[1].to_string()),
        });
    }
    for cap in twitch_handle_regex().captures_iter(text) {
        // The `@123` inside a Discord `<@123>` mention is the SAME mention, not
        // a second `@handle` one: minting a `handle:123` pseudonym for it would
        // be a redundant (and, for hub-api's later handle resolution, polluting)
        // second identity for a person the structured mention already names.
        if let Some(whole) = cap.get(0) {
            if discord_spans
                .iter()
                .any(|(start, end)| whole.start() >= *start && whole.end() <= *end)
            {
                continue;
            }
        }
        let handle = cap[1].to_string();
        out.push(ScannedMention {
            matched_text: cap[0].to_string(),
            mint_key: format!("handle:{}", handle.to_ascii_lowercase()),
            mint_handle: handle.clone(),
            reference: MentionRef::Handle(handle),
        });
    }
    out
}

/// The result of a tokenization pass: the bundle-safe event plus the
/// HOST-SIDE mention bindings (opaque token the bundle was shown -> the raw
/// reference it stands for). `Debug` is redacted via [`MentionBinding`].
#[derive(Debug)]
pub struct TokenizedEvent {
    /// The event handed to the bundle: every identity replaced by a
    /// `{user:<token>}` placeholder.
    pub event: PlatformEvent,
    /// One binding per mention placeholder inserted into free text. Never
    /// forwarded to a bundle; consumed by `crate::identity`.
    pub mentions: Vec<MentionBinding>,
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
    tokenize_event_with_mentions(event, tenant_id, minter)
        .await
        .map(|t| t.event)
}

/// [`tokenize_event`], additionally returning the host-side
/// [`MentionBinding`]s the `identity` capability resolves later (the token a
/// bundle was shown for each free-text mention -> the raw reference it stands
/// for). Identical tokenization and fail-closed behavior; the bindings are an
/// extra output, never part of the returned event.
pub async fn tokenize_event_with_mentions(
    event: &PlatformEvent,
    tenant_id: &str,
    minter: &dyn IdentityMinter,
) -> Result<TokenizedEvent, TokenizeError> {
    // Closed-schema fail-closed gate -- runs before any other processing,
    // on the untouched input payload. regression: raw display_name leaked
    // to bundle; allowlist was fail-open (sec review 2026-10-03).
    validate_known_schema(&event.payload)?;

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
        for m in scan_mentions(text) {
            push_item(
                m.mint_key,
                m.mint_handle,
                SubstTarget::Mention {
                    matched_text: m.matched_text,
                    reference: m.reference,
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
    let mut mention_bindings: Vec<MentionBinding> = Vec::new();
    let mut text_accum: Option<String> = payload
        .get(TEXT_FIELD)
        .and_then(Value::as_str)
        .map(str::to_string);

    for (key, target) in targets {
        // Fail-closed: a requested identity missing from the response is
        // treated exactly like a transport failure -- never silently fall
        // back to the raw value. `sanitize_mint_key` keeps a raw handle
        // out of this error (sec review 2026-10-03: this message previously
        // embedded `key` -- a `handle:<raw username>` fallback -- verbatim,
        // and `spine.rs` logs this Display at `tracing::error!`).
        let Some(token_val) = resolved.get(&key) else {
            return Err(TokenizeError::ResolutionUnavailable(format!(
                "no pseudonym returned for platform_user_id {}",
                sanitize_mint_key(&key)
            )));
        };
        let token = format_user_token(token_val);
        match target {
            SubstTarget::Actor => new_actor = Some(token),
            SubstTarget::JsonField(field) => {
                payload.insert(field.to_string(), Value::String(token));
            }
            SubstTarget::Mention {
                matched_text,
                reference,
            } => {
                if let Some(text) = &mut text_accum {
                    *text = text.replace(&matched_text, &token);
                }
                // The bundle sees `{user:<token_val>}`; remember what that
                // token stands for, host-side only.
                mention_bindings.push(MentionBinding {
                    key: token_val.clone(),
                    reference,
                });
            }
        }
    }

    if let Some(text) = text_accum {
        payload.insert(TEXT_FIELD.to_string(), Value::String(text));
    }

    Ok(TokenizedEvent {
        event: PlatformEvent {
            platform: event.platform.clone(),
            event_type: event.event_type.clone(),
            actor: new_actor,
            payload,
            occurred_at: event.occurred_at.clone(),
            source: event.source.clone(),
        },
        mentions: mention_bindings,
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

    /// Always succeeds with an empty resolution map -- every requested item
    /// comes back missing. Used to exercise the "no pseudonym returned for
    /// platform_user_id {key}" error path specifically (distinct from
    /// [`AlwaysFailMinter`], which fails the whole RPC rather than
    /// returning a short response).
    struct EmptyMinter;

    impl IdentityMinter for EmptyMinter {
        fn mint_many<'a>(&'a self, _tenant_id: &'a str, _items: Vec<MintItem>) -> MintResult<'a> {
            Box::pin(async move { Ok(HashMap::new()) })
        }
    }

    /// A minter whose returned tokens have zero textual relation to the
    /// input `platform_user_id`/`handle` -- faithfully represents what a
    /// real `HubClientMinter` response looks like (an opaque, unguessable
    /// pseudonym minted inside hub-api's PII boundary), unlike
    /// [`FakeMinter`]'s deliberate "echo the key into the token" shape.
    /// `FakeMinter` is fine for exact-substitution assertions, but it would
    /// make a `handle:<raw>` fallback mint key's own lowercased copy of the
    /// raw value show up inside *its own fake token* -- an artifact of that
    /// mock, not a real leak. The full-field-set regression tests use this
    /// minter instead so "no sentinel survives" is a genuine assertion
    /// about `tokenize_event`'s behavior, not about the mock's echo.
    struct OpaqueMinter;

    impl IdentityMinter for OpaqueMinter {
        fn mint_many<'a>(&'a self, _tenant_id: &'a str, items: Vec<MintItem>) -> MintResult<'a> {
            Box::pin(async move {
                Ok(items
                    .into_iter()
                    .enumerate()
                    .map(|(i, item)| (item.platform_user_id, format!("opaque-pseudonym-{i:04}")))
                    .collect())
            })
        }
    }

    /// Panics if ever called -- proves [`validate_known_schema`]'s
    /// fail-closed rejection happens strictly before any identity-mint RPC
    /// is attempted (schema validation is the very first thing
    /// `tokenize_event` does, on the untouched input payload).
    struct PanicMinter;

    impl IdentityMinter for PanicMinter {
        fn mint_many<'a>(&'a self, _tenant_id: &'a str, _items: Vec<MintItem>) -> MintResult<'a> {
            Box::pin(async move {
                panic!("minter must never be invoked when schema validation fails closed")
            })
        }
    }

    /// Recursively collects every JSON string value reachable from `value`
    /// -- used by [`assert_no_sentinel_leaks`] so a regression test checks
    /// the *entire* event the bundle would receive, not just the specific
    /// keys the bug happened to live in.
    fn collect_strings(value: &Value, out: &mut Vec<String>) {
        match value {
            Value::String(s) => out.push(s.clone()),
            Value::Array(items) => items.iter().for_each(|v| collect_strings(v, out)),
            Value::Object(map) => map.values().for_each(|v| collect_strings(v, out)),
            _ => {}
        }
    }

    /// Asserts that none of `sentinels` (raw PII planted in the input
    /// fixture) survives anywhere in `event` -- `actor` plus every string
    /// reachable by recursing the whole payload (including nested
    /// `metadata`), not just the specific field names the tokenizer is
    /// known to touch. Case-insensitive: a token/placeholder substring
    /// match would itself be a bug (tokens never echo the raw value back).
    fn assert_no_sentinel_leaks(event: &PlatformEvent, sentinels: &[&str]) {
        let mut strings: Vec<String> = Vec::new();
        if let Some(actor) = &event.actor {
            strings.push(actor.clone());
        }
        collect_strings(&Value::Object(event.payload.clone()), &mut strings);
        let haystack = strings.join("\u{1}").to_lowercase();
        for sentinel in sentinels {
            assert!(
                !haystack.contains(&sentinel.to_lowercase()),
                "sentinel PII '{sentinel}' leaked into the tokenized event reaching the bundle: {event:?}"
            );
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

    // regression: raw display_name leaked to bundle; allowlist was fail-open (sec review 2026-10-03)

    /// Full `normalize_twitch_irc` field set (`core/svc_ingest::normalize`),
    /// every identity-shaped field seeded with a distinct sentinel raw
    /// value. This is the exact regression: `display_name` had no
    /// `ID_NAME_FIELD_PAIRS` entry, so it reached the bundle in cleartext
    /// on every Twitch chat message.
    #[tokio::test]
    async fn twitch_irc_full_field_set_leaks_no_sentinel_pii() {
        let ev = event(
            Some("SentinelAuthorLogin"),
            serde_json::json!({
                "text": "hello @SentinelMention chat",
                "channel_name": "somechannel",
                "author": "SentinelAuthorLogin",
                "author_id": "1001",
                "user_id": "1001",
                "display_name": "SentinelDisplayName",
                "message_id": "msg-1",
                "room_id": "room-1",
                "badges": ["vip"],
                "is_mod": false,
                "is_subscriber": true,
                "is_vip": true,
                "is_broadcaster": false,
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &OpaqueMinter)
            .await
            .unwrap();
        assert_no_sentinel_leaks(
            &out,
            &[
                "SentinelAuthorLogin",
                "SentinelDisplayName",
                "SentinelMention",
            ],
        );
        // The regression target specifically: display_name must actually be
        // replaced with a placeholder, not merely "not equal to the raw
        // string" by coincidence.
        let display_name = out
            .payload
            .get("display_name")
            .and_then(Value::as_str)
            .unwrap();
        assert!(display_name.starts_with("{user:"));
    }

    /// Full `normalize_discord` field set -- `author_id` is the only
    /// identity-adjacent field in the Discord payload (the raw username
    /// lives solely in `PlatformEvent::actor`, tokenized via the actor
    /// path), plus a structured `<@id>` mention in `text`.
    #[tokio::test]
    async fn discord_full_field_set_leaks_no_sentinel_pii() {
        let ev = event(
            Some("SentinelDiscordAuthor"),
            serde_json::json!({
                "text": "hey <@222333444555666777> check this out",
                "guild_id": "g1",
                "channel_id": "c1",
                "message_id": "m1",
                "author_id": "999888777666555444",
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &OpaqueMinter)
            .await
            .unwrap();
        assert_no_sentinel_leaks(&out, &["SentinelDiscordAuthor"]);
        // The structured `<@id>` mention markup is fully gone -- `OpaqueMinter`
        // mints a token with no textual relation to the input, so this is a
        // real assertion that the raw mention id no longer appears anywhere.
        assert!(!payload_string(&out).contains("222333444555666777"));
    }

    /// Full `normalize_twitch_eventsub` field set for a `channel.subscribe`
    /// event, including the nested `metadata` sub-object.
    #[tokio::test]
    async fn twitch_eventsub_full_field_set_leaks_no_sentinel_pii() {
        let ev = event(
            Some("SentinelUserLogin"),
            serde_json::json!({
                "broadcaster_id": "b1",
                "broadcaster_login": "SentinelBroadcasterLogin",
                "user_id": "u1",
                "user_login": "SentinelUserLogin",
                "user_display_name": "SentinelUserDisplayName",
                "metadata": {"tier": "1000", "is_gift": false},
            }),
        );
        let out = tokenize_event(&ev, "tenant-1", &OpaqueMinter)
            .await
            .unwrap();
        assert_no_sentinel_leaks(
            &out,
            &[
                "SentinelBroadcasterLogin",
                "SentinelUserLogin",
                "SentinelUserDisplayName",
            ],
        );
    }

    /// Fail-closed gate, not fail-open: a payload field this module has no
    /// vetted classification for must dead-letter (via
    /// `TokenizeError::UnclassifiedField`) rather than pass through --
    /// `PanicMinter` proves this rejection happens strictly before any
    /// identity-mint RPC is attempted.
    #[tokio::test]
    async fn unknown_top_level_field_fails_closed_before_minting() {
        let ev = event(
            Some("someone"),
            serde_json::json!({
                "user_id": "1",
                "text": "hi",
                "some_future_identity_field": "raw PII a future normalizer might add",
            }),
        );
        let err = tokenize_event(&ev, "tenant-1", &PanicMinter)
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            TokenizeError::UnclassifiedField { field } if field == "some_future_identity_field"
        ));
    }

    /// Same fail-closed gate, scoped to the nested `metadata` sub-object.
    #[tokio::test]
    async fn unknown_metadata_field_fails_closed_before_minting() {
        let ev = event(
            Some("someone"),
            serde_json::json!({
                "user_id": "1",
                "metadata": {"tier": "1000", "unexpected_pii_field": "raw"},
            }),
        );
        let err = tokenize_event(&ev, "tenant-1", &PanicMinter)
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            TokenizeError::UnclassifiedMetadataField { field } if field == "unexpected_pii_field"
        ));
    }

    /// The counterpart positive case: every field in the real normalizers'
    /// full output shape is already classified, so a legitimate event never
    /// trips the fail-closed gate.
    #[tokio::test]
    async fn known_schema_fields_never_trip_the_fail_closed_gate() {
        let ev = event(
            Some("someone"),
            serde_json::json!({
                "broadcaster_id": "b1",
                "broadcaster_login": "login",
                "user_id": "u1",
                "user_login": "login2",
                "user_display_name": "name",
                "metadata": {
                    "tier": "1000", "is_gift": false, "total": 1,
                    "is_anonymous": false, "viewers": 0, "bits": 0,
                    "type": "live", "started_at": "2026-10-03T00:00:00Z",
                },
            }),
        );
        assert!(tokenize_event(&ev, "tenant-1", &FakeMinter).await.is_ok());
    }

    /// `security.md` Token & Secret Hygiene: never place a raw handle in an
    /// error or log line. An actor-only event with no structured id field
    /// resolves via the `handle:<raw username>` fallback key -- this test
    /// asserts the raw handle never appears in the resulting error's
    /// `Display` (the exact text `spine.rs` passes to `tracing::error!`).
    #[tokio::test]
    async fn resolution_failure_for_handle_fallback_key_never_logs_raw_handle() {
        let ev = event(
            Some("SentinelRawHandleValue"),
            serde_json::json!({"text": "hi"}),
        );
        let err = tokenize_event(&ev, "tenant-1", &EmptyMinter)
            .await
            .unwrap_err();
        let message = err.to_string();
        assert!(!message.to_lowercase().contains("sentinelrawhandlevalue"));
        assert!(matches!(err, TokenizeError::ResolutionUnavailable(_)));
    }

    #[test]
    fn sanitize_mint_key_hashes_handle_fallback_never_embeds_raw_value() {
        let sanitized = sanitize_mint_key("handle:sentinelrawvalue");
        assert!(!sanitized.contains("sentinelrawvalue"));
        assert!(sanitized.starts_with("handle:sha256:"));
    }

    #[test]
    fn sanitize_mint_key_passes_numeric_platform_ids_through_unchanged() {
        assert_eq!(sanitize_mint_key("12345"), "12345");
    }

    // ---- host-side mention bindings (identity capability) ----------------

    #[tokio::test]
    async fn with_mentions_returns_the_identical_event_plus_a_binding_per_mention() {
        let ev = event(
            Some("asker"),
            serde_json::json!({
                "user_id": "1",
                "text": "!steal <@123456789012345678> and @SomeViewer",
            }),
        );
        let plain = tokenize_event(&ev, "tenant-1", &FakeMinter).await.unwrap();
        let with = tokenize_event_with_mentions(&ev, "tenant-1", &FakeMinter)
            .await
            .unwrap();
        // The bundle-visible event is byte-for-byte what `tokenize_event` returns.
        assert_eq!(with.event.payload, plain.payload);
        assert_eq!(with.event.actor, plain.actor);

        // One binding per mention: key = the token inside the placeholder the
        // bundle was shown; reference = the raw thing it stands for.
        assert_eq!(with.mentions.len(), 2);
        assert_eq!(with.mentions[0].key, "tok-123456789012345678");
        assert_eq!(
            with.mentions[0].reference,
            MentionRef::PlatformId("123456789012345678".to_string())
        );
        assert_eq!(with.mentions[1].key, "tok-handle:someviewer");
        assert_eq!(
            with.mentions[1].reference,
            MentionRef::Handle("SomeViewer".to_string())
        );
        // Each key really is the token shown in the text.
        let text = with
            .event
            .payload
            .get("text")
            .and_then(Value::as_str)
            .unwrap();
        for m in &with.mentions {
            assert!(text.contains(&format!("{{user:{}}}", m.key)), "{text}");
        }
    }

    #[tokio::test]
    async fn the_mention_bindings_never_leak_into_the_bundle_visible_event() {
        /// Mints opaque random-looking tokens that embed nothing of the
        /// platform id, so a leak of the raw id can only come from the event.
        struct OpaqueMinter;
        impl IdentityMinter for OpaqueMinter {
            fn mint_many<'a>(
                &'a self,
                _tenant_id: &'a str,
                items: Vec<MintItem>,
            ) -> MintResult<'a> {
                Box::pin(async move {
                    Ok(items
                        .into_iter()
                        .enumerate()
                        .map(|(n, i)| {
                            (
                                i.platform_user_id,
                                format!("00000000-0000-4000-8000-{n:012}"),
                            )
                        })
                        .collect())
                })
            }
        }
        let ev = event(
            Some("asker"),
            serde_json::json!({
                "user_id": "1",
                "text": "hi <@987654321> and @SecretHandleName",
            }),
        );
        let with = tokenize_event_with_mentions(&ev, "tenant-1", &OpaqueMinter)
            .await
            .unwrap();
        let dump = serde_json::to_string(&with.event.payload).unwrap();
        assert!(!dump.contains("987654321"), "{dump}");
        assert!(!dump.to_lowercase().contains("secrethandlename"), "{dump}");
        // And the Debug rendering of the bindings is redacted too.
        let dbg = format!("{:?}", with.mentions);
        assert!(!dbg.contains("987654321"), "{dbg}");
        assert!(!dbg.to_lowercase().contains("secrethandlename"), "{dbg}");
    }

    #[tokio::test]
    async fn a_message_with_no_mentions_has_no_bindings() {
        let ev = event(
            Some("asker"),
            serde_json::json!({ "user_id": "1", "text": "!gamble 50" }),
        );
        let with = tokenize_event_with_mentions(&ev, "tenant-1", &FakeMinter)
            .await
            .unwrap();
        assert!(with.mentions.is_empty());
    }

    #[tokio::test]
    async fn a_minting_failure_returns_no_bindings_either() {
        let ev = event(
            Some("asker"),
            serde_json::json!({ "user_id": "1", "text": "hi <@123>" }),
        );
        assert!(
            tokenize_event_with_mentions(&ev, "tenant-1", &AlwaysFailMinter)
                .await
                .is_err()
        );
    }

    #[test]
    fn scan_mentions_orders_discord_ids_before_twitch_handles_and_keeps_the_raw_text() {
        let found = scan_mentions("a @Bob_99 b <@!42> c <@7>");
        let texts: Vec<_> = found.iter().map(|m| m.matched_text.as_str()).collect();
        assert_eq!(texts, ["<@!42>", "<@7>", "@Bob_99"]);
        assert_eq!(found[0].mint_key, "42");
        assert_eq!(found[2].mint_key, "handle:bob_99");
        assert_eq!(found[2].mint_handle, "Bob_99");
        // A role mention is not a user mention.
        assert!(scan_mentions("<@&555> and <#777>").is_empty());
        // The `@555` inside `<@555>` is the same mention, not a second
        // `@handle` one -- but a genuine standalone `@555` elsewhere still is.
        assert_eq!(scan_mentions("<@555>").len(), 1);
        let both = scan_mentions("<@555> then @555");
        assert_eq!(both.len(), 2);
        assert_eq!(both[0].matched_text, "<@555>");
        assert_eq!(both[1].matched_text, "@555");
    }
}

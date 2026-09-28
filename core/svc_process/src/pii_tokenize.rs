//! Inbound PII-tokenization pre-dispatch pass (spec
//! `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
//! S10.1/S10.3, Phase 1 Task 1 -- HARD BLOCKER): replaces
//! `penguin_spine::PlatformEvent::actor` and every recognized user mention
//! in the event's own text fields with a `{user:<uuid>}` placeholder --
//! either a real, tenant-linked `hub_users` UUID or an ephemeral
//! pseudonym for an unknown/unlinked platform account -- so a bundle's
//! `transform` invoke (`crate::spine::invoke_transform`) never receives a
//! raw platform username/login.
//!
//! **Ephemeral pseudonyms are minted INSIDE the PII boundary
//! (`crate::hub_identity_client`), never locally.** Security review fix:
//! an earlier version of this module derived the pseudonym itself,
//! `UUIDv5(FIXED_PUBLIC_NAMESPACE, "platform:handle")` -- reversible by
//! dictionary attack (anyone can recompute the same fixed, public
//! derivation for a known handle). See `crate::hub_identity_client`'s
//! module doc for the hub-api-minted, per-tenant-secret-keyed
//! replacement and its random-token failure fallback.
//!
//! **Placement decision (this crate, not `core/svc_ingest`) -- see this
//! change's PR description for the full justification.** Short version:
//! `core/svc_ingest` is deliberately database-less by design (its own
//! crate doc: "No database: ingest has no SeaORM dependency, unlike
//! svc_process/svc_action") and only carries tenant/community as resolved
//! *slugs*, not the numeric `(tenant_id, community_id)` pair
//! `community_members` is keyed by -- resolving that pair there would
//! require either the exact DB dependency this module avoids giving
//! ingest, or a second remote hop duplicating `bundle_active_set::scope`'s
//! existing resolution. `crate::spine::handle_delivered` already holds a
//! live RO reader connection scoped to exactly one `(tenant_id,
//! community_id)` pair per instance and runs immediately after hop
//! verification, strictly before [`crate::spine::invoke_transform`] --
//! the same "before the guest ever sees it" boundary the spec's own
//! wording describes ("never forwarded past svc_ingest/svc_process's
//! pre-dispatch pass into guest-visible data"). The raw actor/login value
//! transiting the internal Valkey stream between `svc_ingest::publish` and
//! this pass is an internal hop inside the PII boundary (both are
//! data-plane workers, not the API server and not a WASM guest -- see
//! `rules/critical-rules.md` PII Tokenization) and is the only consumer of
//! that stream (`core/svc_action` reads the separate post-transform
//! `:action` stream, never the raw ingest stream) -- confirmed by grepping
//! this repo for every `penguin_spine::GroupReader`/`XREADGROUP` call site.

use std::future::Future;
use std::pin::Pin;
use std::sync::OnceLock;

use penguin_spine::PlatformEvent;
use regex::Regex;
use serde_json::Value;
use uuid::Uuid;

use crate::hub_identity_client::EphemeralIdentityMinter;
use crate::telemetry::TokenizeMetrics;

/// One resolved identity: either a real, tenant-linked `hub_users` UUID,
/// or an ephemeral pseudonym for an unknown/unlinked platform account.
/// Both render identically on the wire (`{user:<uuid>}`) -- the
/// distinction exists purely for telemetry
/// (`TokenizeMetrics::unresolved_users_total`) and tests, never for the
/// bundle, which must not be able to tell the two apart.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResolvedIdentity {
    Linked(Uuid),
    Ephemeral(Uuid),
}

impl ResolvedIdentity {
    pub fn uuid(self) -> Uuid {
        match self {
            Self::Linked(u) | Self::Ephemeral(u) => u,
        }
    }

    pub fn is_ephemeral(self) -> bool {
        matches!(self, Self::Ephemeral(_))
    }
}

/// Renders a resolved identity as the bundle-visible placeholder text
/// (spec S10.4: "A bundle only ever emits/receives `{user:<uuid>}`
/// placeholders").
pub fn format_user_token(id: Uuid) -> String {
    format!("{{user:{id}}}")
}

/// Escapes every literal `{`, `}`, and `\` in `text` *before* this pass
/// inserts any `{user:<uuid>}` placeholder of its own -- so a sequence a
/// user typed verbatim in chat (`{user:not-a-real-uuid}`, or an attempt to
/// pre-emptively smuggle a nested/self-referential placeholder) can never
/// be confused, by any later consumer, with a placeholder this pass
/// itself inserted. Backslash-escaping is unambiguous and reversible
/// (`\{`, `\}`, `\\`), matching the escape convention
/// `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
/// S10.4's outbound detokenizer (Phase 8) is specified to treat as "not a
/// real placeholder" once that pass lands -- picking the same scheme now
/// avoids two incompatible escape conventions later.
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

/// Resolves one platform identity to a [`ResolvedIdentity`]. Hand-written
/// boxed-future trait object (matching `penguin_spine::SpineOps`'s own
/// async-trait-object convention already used in `crate::spine`, so this
/// crate doesn't need a new `async-trait` dependency) -- lets
/// `crate::spine::ProcessDeps` hold this behind `Arc<dyn IdentityResolver>`
/// exactly like its existing `metrics`/`license` fields.
///
/// Both methods **always succeed** -- an unknown/unlinked/query-error case
/// mints [`ResolvedIdentity::Ephemeral`] rather than returning an error,
/// since "nobody has linked this platform account yet" is an expected,
/// common steady-state condition, and a transient RO-reader query failure
/// must fail toward safety (never raw PII), not toward dropping the whole
/// event.
pub trait IdentityResolver: Send + Sync {
    /// Resolves a structured reference: a platform's own numeric/opaque
    /// user id (Twitch `user-id`, a Discord snowflake).
    fn resolve_by_id<'a>(
        &'a self,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>>;

    /// Best-effort resolution of an unstructured `@handle` mention with no
    /// platform_user_id available (spec S10.3 step 2). See
    /// `bundle_active_set::identity::resolve_member_by_handle`'s own doc
    /// for the known-imperfect-match caveat this delegates to.
    fn resolve_by_handle<'a>(
        &'a self,
        platform: &'a str,
        handle: &'a str,
    ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>>;
}

/// [`IdentityResolver`] backed by `bundle_active_set`'s RO reader
/// connection, scoped to `(tenant_id, community_id)` -- this instance's own
/// static `BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` (never a
/// per-call parameter: cross-tenant/community lookups would be a
/// tenant-isolation bug, identical in spirit to `bundle_active_set::
/// scope::resolve_scope`'s own fail-closed single-scope contract).
/// `community_id == 0` is the tenant-wide sentinel, resolved across every
/// community in `tenant_id` -- see `bundle_active_set::identity`'s doc.
///
/// An unlinked/unknown identity is minted via `minter`
/// (`crate::hub_identity_client`), never derived locally.
pub struct SeaOrmIdentityResolver {
    conn: sea_orm::DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    minter: std::sync::Arc<dyn EphemeralIdentityMinter>,
}

impl SeaOrmIdentityResolver {
    pub fn new(
        conn: sea_orm::DatabaseConnection,
        tenant_id: i32,
        community_id: i32,
        minter: std::sync::Arc<dyn EphemeralIdentityMinter>,
    ) -> Self {
        Self {
            conn,
            tenant_id,
            community_id,
            minter,
        }
    }

    /// Shared "parse the linked UUID, or mint an ephemeral pseudonym"
    /// tail -- a corrupt/non-UUID `community_members.user_id` (a
    /// free-form `VARCHAR`, not a database `uuid` column) is treated the
    /// same as unlinked, logged loudly rather than ever forwarded raw.
    async fn parse_or_mint(
        &self,
        user_id: Option<String>,
        platform: &str,
        platform_user_id: &str,
        handle: Option<&str>,
    ) -> ResolvedIdentity {
        if let Some(user_id) = user_id {
            match Uuid::parse_str(&user_id) {
                Ok(uuid) => return ResolvedIdentity::Linked(uuid),
                Err(_) => {
                    tracing::warn!(
                        platform,
                        platform_user_id,
                        "community_members.user_id is not a valid UUID; minting an ephemeral \
                         pseudonym instead"
                    );
                }
            }
        }
        let pseudonym = self
            .minter
            .mint(self.tenant_id, platform, platform_user_id, handle)
            .await;
        ResolvedIdentity::Ephemeral(pseudonym)
    }
}

impl IdentityResolver for SeaOrmIdentityResolver {
    fn resolve_by_id<'a>(
        &'a self,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>> {
        Box::pin(async move {
            match bundle_active_set::resolve_linked_user_id(
                &self.conn,
                self.tenant_id,
                self.community_id,
                platform,
                platform_user_id,
            )
            .await
            {
                Ok(user_id) => {
                    self.parse_or_mint(user_id, platform, platform_user_id, None)
                        .await
                }
                Err(err) => {
                    tracing::error!(
                        platform,
                        platform_user_id,
                        error = %err,
                        "identity resolution query failed; minting an ephemeral pseudonym \
                         (fail-safe -- never raw PII, never blocks the event)"
                    );
                    self.parse_or_mint(None, platform, platform_user_id, None)
                        .await
                }
            }
        })
    }

    fn resolve_by_handle<'a>(
        &'a self,
        platform: &'a str,
        handle: &'a str,
    ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>> {
        Box::pin(async move {
            match bundle_active_set::identity::resolve_member_by_handle(
                &self.conn,
                self.tenant_id,
                self.community_id,
                platform,
                handle,
            )
            .await
            {
                Ok(Some(m)) => {
                    self.parse_or_mint(m.user_id, platform, &m.platform_user_id, Some(handle))
                        .await
                }
                Ok(None) => {
                    // No structured platform_user_id at all -- the mint
                    // call still carries the handle (hub-api's own
                    // pseudonym derivation may use it as a display hint;
                    // this crate never derives anything from it locally),
                    // keyed by a distinguishable fallback identity string
                    // so it can never collide with a real numeric id.
                    let fallback_key = format!("handle:{}", handle.to_ascii_lowercase());
                    self.parse_or_mint(None, platform, &fallback_key, Some(handle))
                        .await
                }
                Err(err) => {
                    tracing::error!(
                        platform,
                        handle,
                        error = %err,
                        "handle-mention resolution query failed; minting an ephemeral pseudonym \
                         (fail-safe -- never raw PII, never blocks the event)"
                    );
                    let fallback_key = format!("handle:{}", handle.to_ascii_lowercase());
                    self.parse_or_mint(None, platform, &fallback_key, Some(handle))
                        .await
                }
            }
        })
    }
}

/// [`IdentityResolver`] for the legacy, env-driven process loop
/// (`crate::try_start_process_loop`) -- that path never resolves a
/// numeric `(tenant_id, community_id)` scope at all (it hardcodes the
/// tenant-wide `"global"` activation, see that function's own doc) and
/// has no RO reader connection to query, so it cannot look up
/// `community_members` -- nor a numeric `tenant_id` to call hub-api's
/// tenant-scoped mint endpoint with. Every identity therefore gets a
/// fresh random token (`crate::hub_identity_client::RandomTokenMinter`) --
/// this still fully satisfies the hard invariant (a raw platform
/// username/login is never forwarded to a guest), it just means this
/// legacy path can never resolve a real linked `hub_users` UUID, nor keep
/// a stable pseudonym for the same unknown user across repeated mentions.
/// `crate::source_supervisor`'s DB-driven path (backed by
/// [`SeaOrmIdentityResolver`]) is what actually resolves linked accounts;
/// this type exists only so the legacy fallback isn't left without a
/// resolver at all.
pub struct AlwaysEphemeralIdentityResolver {
    minter: crate::hub_identity_client::RandomTokenMinter,
}

impl Default for AlwaysEphemeralIdentityResolver {
    fn default() -> Self {
        Self {
            minter: crate::hub_identity_client::RandomTokenMinter,
        }
    }
}

impl IdentityResolver for AlwaysEphemeralIdentityResolver {
    fn resolve_by_id<'a>(
        &'a self,
        platform: &'a str,
        platform_user_id: &'a str,
    ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>> {
        Box::pin(async move {
            ResolvedIdentity::Ephemeral(self.minter.mint(0, platform, platform_user_id, None).await)
        })
    }

    fn resolve_by_handle<'a>(
        &'a self,
        platform: &'a str,
        handle: &'a str,
    ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>> {
        Box::pin(async move {
            ResolvedIdentity::Ephemeral(self.minter.mint(0, platform, handle, Some(handle)).await)
        })
    }
}

/// The one payload field every normalizer (`core/svc_ingest::normalize`)
/// puts user-authored free text in -- Twitch IRC's `text`, Discord's
/// `text` (copied from `msg.content`). Twitch EventSub payloads carry no
/// such field, so the mention scan below is naturally a no-op for those
/// event types.
const TEXT_FIELD: &str = "text";

fn discord_mention_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"<@!?(\d+)>").expect("valid regex"))
}

/// Twitch/IRC username grammar (Twitch Helix: 3-25 alphanumeric/underscore
/// characters) -- deliberately does NOT match a leading `@` that is
/// itself preceded by an already-escaped `\{`/similar (irrelevant here:
/// `@` is never a brace character, so `escape_braces` never touches it).
fn twitch_handle_regex() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"@([A-Za-z0-9_]{3,25})\b").expect("valid regex"))
}

/// Outcome of one [`tokenize_platform_event`] call -- feeds
/// `TokenizeMetrics::unresolved_users_total` and is asserted on directly
/// by this module's own regression tests (never just "no panic").
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct TokenizeOutcome {
    pub linked: usize,
    pub ephemeral: usize,
}

impl TokenizeOutcome {
    fn record(&mut self, identity: ResolvedIdentity) {
        if identity.is_ephemeral() {
            self.ephemeral += 1;
        } else {
            self.linked += 1;
        }
    }

    fn merge(&mut self, other: TokenizeOutcome) {
        self.linked += other.linked;
        self.ephemeral += other.ephemeral;
    }
}

/// Extracts the platform's own numeric/opaque user id for `event`'s actor
/// from its own payload -- the same fields `core/svc_ingest::normalize`
/// already populates (`user_id`/`author_id` for Twitch IRC and Twitch
/// EventSub, `author_id` for Discord).
fn extract_actor_platform_id(payload: &serde_json::Map<String, Value>) -> Option<String> {
    for field in ["user_id", "author_id"] {
        if let Some(id) = payload.get(field).and_then(Value::as_str) {
            if !id.is_empty() {
                return Some(id.to_string());
            }
        }
    }
    None
}

/// Payload fields that duplicate the ACTOR's own raw name/login
/// (`core/svc_ingest::normalize`'s byte-exact-port payload shapes:
/// Twitch IRC's `author`/`display_name`, Twitch EventSub's `user_login`/
/// `user_display_name`) -- replaced with the exact same token already
/// resolved for [`PlatformEvent::actor`], never re-queried, since they
/// name the identical identity.
const ACTOR_DUPLICATE_FIELDS: &[&str] =
    &["author", "display_name", "user_login", "user_display_name"];

/// Twitch EventSub's `broadcaster_login` names the CHANNEL OWNER, not
/// necessarily the acting user (e.g. a raid's actor is the raider, not
/// the broadcaster being raided) -- resolved independently via its own
/// `broadcaster_id` field, never assumed to be the same identity as
/// `actor`.
const BROADCASTER_LOGIN_FIELD: &str = "broadcaster_login";
const BROADCASTER_ID_FIELD: &str = "broadcaster_id";

/// The full inbound tokenization pass for one [`PlatformEvent`], run
/// in-place immediately before [`crate::spine::invoke_transform`]
/// (`crate::spine::handle_delivered`, right after hop verification):
///
/// 1. `event.actor` -- resolved by platform_user_id when the payload has
///    one; falls back to an `actor:<lowercased name>`-keyed ephemeral
///    pseudonym on the rare payload that carries no id at all (e.g. a
///    Twitch IRC line with no `user-id` tag), so the raw actor string is
///    replaced under every circumstance, never left as a fallback default.
/// 2. Every [`ACTOR_DUPLICATE_FIELDS`] payload field -- these are the SAME
///    identity as `actor` in every normalizer that sets them, so they get
///    the identical token, not a fresh resolution (regression test:
///    `tokenize_platform_event_never_leaks_a_raw_actor_username_or_login`
///    caught this gap the first time this pass only tokenized `actor`
///    itself and left `payload.author`/`payload.display_name`
///    untouched).
/// 3. [`BROADCASTER_LOGIN_FIELD`] (Twitch EventSub only) -- a genuinely
///    different identity from `actor`, resolved on its own via
///    [`BROADCASTER_ID_FIELD`].
/// 4. Every recognized mention in `event.payload["text"]` (Discord
///    `<@id>`, Twitch/IRC `@handle`) -- brace-escaped first (
///    [`escape_braces`]) so a forged `{user:...}`-shaped sequence the user
///    typed verbatim can never be mistaken for one this pass inserted.
///
/// Records [`TokenizeMetrics::duration_seconds`] over the whole call and
/// [`TokenizeMetrics::unresolved_users_total`] for every ephemeral
/// resolution (actor, broadcaster, or mention).
pub async fn tokenize_platform_event(
    event: &mut PlatformEvent,
    resolver: &dyn IdentityResolver,
    metrics: &TokenizeMetrics,
) -> TokenizeOutcome {
    let start = std::time::Instant::now();
    let mut outcome = TokenizeOutcome::default();

    let mut actor_token: Option<String> = None;
    if let Some(raw_actor) = event.actor.take() {
        let identity_key = extract_actor_platform_id(&event.payload)
            .unwrap_or_else(|| format!("actor:{}", raw_actor.to_ascii_lowercase()));
        let resolved = resolver.resolve_by_id(&event.platform, &identity_key).await;
        outcome.record(resolved);
        let token = format_user_token(resolved.uuid());
        event.actor = Some(token.clone());
        actor_token = Some(token);
    }

    if let Some(token) = &actor_token {
        for field in ACTOR_DUPLICATE_FIELDS {
            if matches!(event.payload.get(*field), Some(Value::String(_))) {
                event
                    .payload
                    .insert((*field).to_string(), Value::String(token.clone()));
            }
        }
    }

    if matches!(
        event.payload.get(BROADCASTER_LOGIN_FIELD),
        Some(Value::String(_))
    ) {
        let identity_key = event
            .payload
            .get(BROADCASTER_ID_FIELD)
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| "broadcaster:unknown".to_string());
        let resolved = resolver.resolve_by_id(&event.platform, &identity_key).await;
        outcome.record(resolved);
        event.payload.insert(
            BROADCASTER_LOGIN_FIELD.to_string(),
            Value::String(format_user_token(resolved.uuid())),
        );
    }

    if let Some(Value::String(text)) = event.payload.get(TEXT_FIELD).cloned() {
        let (tokenized, text_outcome) = tokenize_text(&text, &event.platform, resolver).await;
        outcome.merge(text_outcome);
        event
            .payload
            .insert(TEXT_FIELD.to_string(), Value::String(tokenized));
    }

    metrics
        .duration_seconds
        .observe(start.elapsed().as_secs_f64());
    if outcome.ephemeral > 0 {
        metrics
            .unresolved_users_total
            .inc_by(outcome.ephemeral as u64);
    }
    outcome
}

/// Escapes `text` then substitutes every recognized platform-specific
/// mention with `{user:<uuid>}`, single-pass (never re-scans its own
/// substituted output -- mirrors the non-recursive discipline spec S10.4
/// requires of the *outbound* detokenizer, applied here too even though
/// this is the inbound half).
async fn tokenize_text(
    text: &str,
    platform: &str,
    resolver: &dyn IdentityResolver,
) -> (String, TokenizeOutcome) {
    let escaped = escape_braces(text);
    match platform {
        "discord" => {
            substitute_matches(&escaped, discord_mention_regex(), platform, resolver, true).await
        }
        "twitch" => {
            substitute_matches(&escaped, twitch_handle_regex(), platform, resolver, false).await
        }
        _ => (escaped, TokenizeOutcome::default()),
    }
}

/// Single left-to-right pass over `text`'s regex matches: copies each
/// unmatched span verbatim, resolves each match (by structured id when
/// `by_id`, else by best-effort handle), and appends the resulting
/// `{user:<uuid>}` token in its place. `last_end` tracking guarantees no
/// span of `text` is visited twice and the substituted output itself is
/// never re-matched.
async fn substitute_matches(
    text: &str,
    regex: &Regex,
    platform: &str,
    resolver: &dyn IdentityResolver,
    by_id: bool,
) -> (String, TokenizeOutcome) {
    let mut out = String::with_capacity(text.len());
    let mut last_end = 0;
    let mut outcome = TokenizeOutcome::default();

    for caps in regex.captures_iter(text) {
        let whole = caps.get(0).expect("capture 0 always matches");
        let captured = caps.get(1).expect("group 1 always matches").as_str();
        out.push_str(&text[last_end..whole.start()]);
        let identity = if by_id {
            resolver.resolve_by_id(platform, captured).await
        } else {
            resolver.resolve_by_handle(platform, captured).await
        };
        outcome.record(identity);
        out.push_str(&format_user_token(identity.uuid()));
        last_end = whole.end();
    }
    out.push_str(&text[last_end..]);

    (out, outcome)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    use serde_json::json;

    fn test_metrics() -> TokenizeMetrics {
        crate::telemetry::register_tokenize_metrics(&prometheus::Registry::new())
    }

    /// TEST-ONLY deterministic pseudonym helper -- production code never
    /// calls this (see this module's own doc: minting happens inside the
    /// PII boundary via `crate::hub_identity_client`, never locally). Only
    /// [`FakeResolver`] below uses it, purely so this module's tokenization-
    /// logic tests (mention substitution, brace escaping, actor-field
    /// replacement) get a stable, assertable placeholder without needing a
    /// real hub-api call.
    fn ephemeral_pseudonym(platform: &str, identity_key: &str) -> Uuid {
        const TEST_NAMESPACE: Uuid = Uuid::from_bytes(*b"test-only-ns\0\0\0\0");
        Uuid::new_v5(
            &TEST_NAMESPACE,
            format!("{platform}:{identity_key}").as_bytes(),
        )
    }

    /// Deterministic in-memory [`IdentityResolver`] -- linked identities are
    /// explicit, everything else falls through to the test-only
    /// [`ephemeral_pseudonym`] helper above so these tests get a stable,
    /// assertable placeholder for "some ephemeral token", never a real
    /// hub-api call.
    #[derive(Default)]
    struct FakeResolver {
        linked_by_id: HashMap<(String, String), Uuid>,
        linked_by_handle: HashMap<(String, String), Uuid>,
    }

    impl FakeResolver {
        fn link_id(mut self, platform: &str, platform_user_id: &str, uuid: Uuid) -> Self {
            self.linked_by_id
                .insert((platform.to_string(), platform_user_id.to_string()), uuid);
            self
        }

        fn link_handle(mut self, platform: &str, handle: &str, uuid: Uuid) -> Self {
            self.linked_by_handle
                .insert((platform.to_string(), handle.to_ascii_lowercase()), uuid);
            self
        }
    }

    impl IdentityResolver for FakeResolver {
        fn resolve_by_id<'a>(
            &'a self,
            platform: &'a str,
            platform_user_id: &'a str,
        ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>> {
            Box::pin(async move {
                match self
                    .linked_by_id
                    .get(&(platform.to_string(), platform_user_id.to_string()))
                {
                    Some(uuid) => ResolvedIdentity::Linked(*uuid),
                    None => {
                        ResolvedIdentity::Ephemeral(ephemeral_pseudonym(platform, platform_user_id))
                    }
                }
            })
        }

        fn resolve_by_handle<'a>(
            &'a self,
            platform: &'a str,
            handle: &'a str,
        ) -> Pin<Box<dyn Future<Output = ResolvedIdentity> + Send + 'a>> {
            Box::pin(async move {
                let key = (platform.to_string(), handle.to_ascii_lowercase());
                match self.linked_by_handle.get(&key) {
                    Some(uuid) => ResolvedIdentity::Linked(*uuid),
                    None => ResolvedIdentity::Ephemeral(ephemeral_pseudonym(
                        platform,
                        &format!("handle:{}", handle.to_ascii_lowercase()),
                    )),
                }
            })
        }
    }

    fn payload_of(value: serde_json::Value) -> serde_json::Map<String, Value> {
        value
            .as_object()
            .expect("fixture payload must be an object")
            .clone()
    }

    /// Byte-shape match of `core/svc_ingest::normalize::normalize_twitch_irc`'s
    /// own payload -- `user_id`/`author_id` both carry the platform's
    /// numeric id, `text` carries the raw chat line.
    fn twitch_irc_fixture(actor: &str, user_id: &str, text: &str) -> PlatformEvent {
        PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "message".to_string(),
            actor: Some(actor.to_string()),
            payload: payload_of(json!({
                "text": text,
                "channel_name": "somechannel",
                "author": actor,
                "author_id": user_id,
                "user_id": user_id,
            })),
            occurred_at: "2026-09-28T00:00:00.000Z".to_string(),
            source: None,
        }
    }

    /// Byte-shape match of `core/svc_ingest::normalize::normalize_discord`'s
    /// own payload -- `author_id` carries the Discord snowflake, `text`
    /// carries the raw message content.
    fn discord_fixture(actor: &str, author_id: &str, text: &str) -> PlatformEvent {
        PlatformEvent {
            platform: "discord".to_string(),
            event_type: "message".to_string(),
            actor: Some(actor.to_string()),
            payload: payload_of(json!({
                "text": text,
                "guild_id": "111",
                "channel_id": "222",
                "message_id": "333",
                "author_id": author_id,
            })),
            occurred_at: "2026-09-28T00:00:00.000Z".to_string(),
            source: None,
        }
    }

    /// Byte-shape match of `normalize_twitch_eventsub`'s payload for a
    /// `channel.raid` event -- carries TWO distinct identities
    /// (`broadcaster_login`, the channel being raided, and `user_login`/
    /// `user_display_name`, the raider/actor) and no free-text field at
    /// all, so the mention scan must be a no-op here, not a panic on a
    /// missing `text` key.
    fn twitch_raid_fixture(
        actor_login: &str,
        actor_display_name: &str,
        user_id: &str,
        broadcaster_login: &str,
        broadcaster_id: &str,
    ) -> PlatformEvent {
        PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "channel.raid".to_string(),
            actor: Some(actor_login.to_string()),
            payload: payload_of(json!({
                "broadcaster_id": broadcaster_id,
                "broadcaster_login": broadcaster_login,
                "user_id": user_id,
                "user_login": actor_login,
                "user_display_name": actor_display_name,
                "metadata": {"viewers": 5},
            })),
            occurred_at: "2026-09-28T00:00:00.000Z".to_string(),
            source: None,
        }
    }

    fn serialized(event: &PlatformEvent) -> String {
        serde_json::to_string(event).expect("PlatformEvent always serializes")
    }

    // -- pure function unit tests --------------------------------------

    #[test]
    fn ephemeral_pseudonym_is_deterministic_for_the_same_identity() {
        let a = ephemeral_pseudonym("twitch", "999");
        let b = ephemeral_pseudonym("twitch", "999");
        assert_eq!(
            a, b,
            "the same unknown platform identity must always mint the same pseudonym"
        );
    }

    #[test]
    fn ephemeral_pseudonym_differs_across_platforms_for_the_same_raw_id() {
        let twitch = ephemeral_pseudonym("twitch", "999");
        let discord = ephemeral_pseudonym("discord", "999");
        assert_ne!(
            twitch, discord,
            "the same raw id string on two different platforms must not collide"
        );
    }

    #[test]
    fn format_user_token_wraps_the_uuid_in_the_expected_grammar() {
        let id = Uuid::nil();
        assert_eq!(
            format_user_token(id),
            "{user:00000000-0000-0000-0000-000000000000}"
        );
    }

    #[test]
    fn escape_braces_escapes_braces_and_backslashes_reversibly() {
        assert_eq!(escape_braces("a{b}c\\d"), "a\\{b\\}c\\\\d");
        assert_eq!(escape_braces("no braces here"), "no braces here");
    }

    // -- requirement: no raw username/login anywhere in the bundle-visible
    //    event (table test, Discord + Twitch fixtures, corpus size
    //    reported per `critical-rules.md` Verification Integrity) --------

    #[tokio::test]
    async fn tokenize_platform_event_never_leaks_a_raw_actor_username_or_login() {
        let metrics = test_metrics();
        let resolver = FakeResolver::default();

        // (fixture builder, raw identity substring that must never survive)
        let fixtures: Vec<(PlatformEvent, &str)> = vec![
            (
                twitch_irc_fixture("SneakyStreamerHandle", "111", "hello chat"),
                "SneakyStreamerHandle",
            ),
            (
                discord_fixture("EmbarrassingDiscordTag", "222", "hi everyone"),
                "EmbarrassingDiscordTag",
            ),
            (
                twitch_raid_fixture(
                    "RaiderLogin",
                    "RaiderDisplay",
                    "333",
                    "RaidedChannelLogin",
                    "444",
                ),
                "RaiderLogin",
            ),
            (
                twitch_raid_fixture(
                    "RaiderLogin2",
                    "RaiderDisplay2",
                    "555",
                    "RaidedChannelLogin2",
                    "666",
                ),
                "RaidedChannelLogin2",
            ),
        ];
        assert_eq!(
            fixtures.len(),
            4,
            "corpus size examined by this regression test"
        );

        for (mut event, raw_identity) in fixtures {
            tokenize_platform_event(&mut event, &resolver, &metrics).await;
            let out = serialized(&event);
            assert!(
                !out.contains(raw_identity),
                "raw identity {raw_identity:?} leaked into the bundle-visible event: {out}"
            );
            assert!(
                event
                    .actor
                    .as_deref()
                    .is_some_and(|a| a.starts_with("{user:") && a.ends_with('}')),
                "actor must always be a {{user:<uuid>}} token, got {:?}",
                event.actor
            );
        }
    }

    /// A raid's actor (the raider) and `broadcaster_login` (the channel
    /// being raided) are two DIFFERENT identities -- both must be
    /// tokenized, and to DIFFERENT tokens, never silently collapsed onto
    /// the actor's own resolution.
    #[tokio::test]
    async fn raid_broadcaster_and_actor_resolve_to_distinct_tokens() {
        let actor_uuid = Uuid::new_v4();
        let broadcaster_uuid = Uuid::new_v4();
        let resolver = FakeResolver::default()
            .link_id("twitch", "333", actor_uuid)
            .link_id("twitch", "444", broadcaster_uuid);
        let metrics = test_metrics();
        let mut event = twitch_raid_fixture(
            "RaiderLogin",
            "RaiderDisplay",
            "333",
            "RaidedChannelLogin",
            "444",
        );

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        assert_eq!(
            event.actor.as_deref(),
            Some(format_user_token(actor_uuid).as_str())
        );
        assert_eq!(
            event.payload.get("user_login").and_then(Value::as_str),
            Some(format_user_token(actor_uuid).as_str()),
            "user_login mirrors the actor's own identity"
        );
        assert_eq!(
            event
                .payload
                .get("user_display_name")
                .and_then(Value::as_str),
            Some(format_user_token(actor_uuid).as_str())
        );
        assert_eq!(
            event
                .payload
                .get("broadcaster_login")
                .and_then(Value::as_str),
            Some(format_user_token(broadcaster_uuid).as_str()),
            "broadcaster_login is a DIFFERENT identity from the actor"
        );
        assert_ne!(actor_uuid, broadcaster_uuid);
        let out = serialized(&event);
        for raw in ["RaiderLogin", "RaiderDisplay", "RaidedChannelLogin"] {
            assert!(!out.contains(raw), "raw identity {raw:?} leaked: {out}");
        }
    }

    // -- requirement: mention tokenization (structured Discord + handle-
    //    based Twitch/IRC) ------------------------------------------------

    #[tokio::test]
    async fn discord_structured_mention_is_replaced_with_the_mentioned_users_token() {
        let mentioned_uuid = Uuid::new_v4();
        let resolver = FakeResolver::default().link_id("discord", "999888777", mentioned_uuid);
        let metrics = test_metrics();
        let mut event = discord_fixture("Carol", "1", "welcome <@999888777> to the server");

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        let text = event.payload.get("text").and_then(Value::as_str).unwrap();
        assert_eq!(
            text,
            format!(
                "welcome {} to the server",
                format_user_token(mentioned_uuid)
            )
        );
        assert!(
            !text.contains("999888777"),
            "the raw Discord snowflake must not survive"
        );
    }

    #[tokio::test]
    async fn twitch_handle_mention_is_replaced_with_the_mentioned_users_token() {
        let mentioned_uuid = Uuid::new_v4();
        let resolver = FakeResolver::default().link_handle("twitch", "bobviewer", mentioned_uuid);
        let metrics = test_metrics();
        let mut event = twitch_irc_fixture("Alice", "1", "hey @BobViewer welcome in");

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        let text = event.payload.get("text").and_then(Value::as_str).unwrap();
        assert_eq!(
            text,
            format!("hey {} welcome in", format_user_token(mentioned_uuid))
        );
        assert!(!text.to_lowercase().contains("bobviewer"));
    }

    // -- requirement: unknown/unlinked user -> ephemeral pseudonym, never
    //    the raw handle ---------------------------------------------------

    #[tokio::test]
    async fn an_unmentioned_unknown_twitch_handle_becomes_a_deterministic_ephemeral_token() {
        let resolver = FakeResolver::default();
        let metrics = test_metrics();
        let mut event = twitch_irc_fixture("Alice", "1", "hey @UnknownGuy sup");

        let outcome = tokenize_platform_event(&mut event, &resolver, &metrics).await;

        let text = event.payload.get("text").and_then(Value::as_str).unwrap();
        assert!(!text.to_lowercase().contains("unknownguy"));
        assert!(
            text.contains("{user:"),
            "unknown mention still becomes a placeholder, not raw text"
        );
        // The mention resolves to an ephemeral pseudonym, distinctly
        // counted from the (also-ephemeral, since FakeResolver has no
        // linked actor here) actor resolution -- both are ephemeral in
        // this fixture.
        assert_eq!(outcome.ephemeral, 2, "actor + one unresolved mention");
        assert_eq!(outcome.linked, 0);
    }

    #[tokio::test]
    async fn an_unknown_actor_with_no_platform_user_id_still_never_leaks_its_raw_name() {
        // A payload with neither `user_id` nor `author_id` set (possible on
        // a Twitch IRC line with no tags at all) must still fully replace
        // the actor -- the `actor:<name>` fallback identity key.
        let resolver = FakeResolver::default();
        let metrics = test_metrics();
        let mut event = PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "message".to_string(),
            actor: Some("NoTagsUser".to_string()),
            payload: payload_of(json!({"text": "hi"})),
            occurred_at: "2026-09-28T00:00:00.000Z".to_string(),
            source: None,
        };

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        assert!(event.actor.as_deref().unwrap().starts_with("{user:"));
        assert!(!serialized(&event).to_lowercase().contains("notagsuser"));
    }

    // -- requirement: brace-sequence escaping so a user can't forge a
    //    `{user:...}` token ----------------------------------------------

    #[tokio::test]
    async fn a_forged_user_token_typed_in_chat_is_escaped_not_substituted() {
        let resolver = FakeResolver::default();
        let metrics = test_metrics();
        let forged = "{user:11111111-1111-1111-1111-111111111111}";
        let mut event = twitch_irc_fixture("Alice", "1", &format!("look: {forged}"));

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        let text = event.payload.get("text").and_then(Value::as_str).unwrap();
        assert!(
            !text.contains(forged),
            "the forged sequence must not survive unescaped: {text}"
        );
        assert!(
            text.contains("\\{user:11111111-1111-1111-1111-111111111111\\}"),
            "the forged sequence must be brace-escaped verbatim: {text}"
        );
    }

    #[tokio::test]
    async fn a_forged_token_is_never_re_scanned_after_a_real_mention_is_substituted() {
        // Single-pass discipline: a real mention resolves to a
        // `{user:<uuid>}` token, and a forged sequence elsewhere in the
        // SAME message must stay escaped -- proves the substitution pass
        // never re-scans its own output (which could otherwise "discover"
        // the escaped-then-unescaped forged sequence a second time).
        let mentioned_uuid = Uuid::new_v4();
        let resolver = FakeResolver::default().link_handle("twitch", "bob", mentioned_uuid);
        let metrics = test_metrics();
        let forged = "{user:22222222-2222-2222-2222-222222222222}";
        let mut event = twitch_irc_fixture("Alice", "1", &format!("@Bob look at {forged}"));

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        let text = event.payload.get("text").and_then(Value::as_str).unwrap();
        assert!(text.starts_with(&format_user_token(mentioned_uuid)));
        assert!(text.contains("\\{user:22222222-2222-2222-2222-222222222222\\}"));
        assert!(!text.contains(forged));
    }

    // -- telemetry ---------------------------------------------------------

    #[tokio::test]
    async fn tokenize_platform_event_records_latency_and_unresolved_count() {
        let resolver = FakeResolver::default();
        let metrics = test_metrics();
        let mut event = twitch_irc_fixture("Alice", "1", "hi @UnknownGuy");

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        assert_eq!(metrics.duration_seconds.get_sample_count(), 1);
        // actor (unlinked) + the one unresolved mention.
        assert_eq!(metrics.unresolved_users_total.get(), 2);
    }

    #[tokio::test]
    async fn a_linked_actor_records_no_unresolved_count() {
        let uuid = Uuid::new_v4();
        let resolver = FakeResolver::default().link_id("twitch", "1", uuid);
        let metrics = test_metrics();
        let mut event = twitch_irc_fixture("Alice", "1", "no mentions here");

        tokenize_platform_event(&mut event, &resolver, &metrics).await;

        assert_eq!(
            event.actor.as_deref(),
            Some(format_user_token(uuid).as_str())
        );
        assert_eq!(metrics.unresolved_users_total.get(), 0);
    }
}

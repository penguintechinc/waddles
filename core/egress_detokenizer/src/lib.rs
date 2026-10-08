//! Outbound PII-detokenization pass -- the egress half of the PII
//! boundary whose inbound half is `core/svc_process/src/pii_tokenize.rs`.
//! A single-pass, non-recursive `{user:<token>}` scanner that substitutes
//! real display names back into chat/overlay text **only at the final
//! send** (`core/svc_action`'s `relay.send`/Discord relay host calls),
//! per-sink-escaped so a resolved display name can never break out of its
//! position in the rendered message (Discord markdown/mention injection,
//! Twitch-IRC CRLF injection).
//!
//! Identity resolution is delegated entirely to hub-api's
//! `waddles.hub.internal.v1.IdentityService.ResolveDisplayNames` RPC
//! (`core/hub_client`) -- this crate never caches or derives a display
//! name itself. [`DisplayNameResolver`] is a narrow seam over that RPC
//! (mirrors `svc_process::pii_tokenize::IdentityMinter`'s identical
//! rationale) so this crate's own tests never perform real gRPC I/O.
//!
//! **Fail-safe-empty, never fail-open:** a resolution failure (hub-api
//! unreachable, circuit open, no resolver configured) or a token the
//! response doesn't recognize (stale/evicted pseudonym, expected and not
//! an error) both substitute [`NEUTRAL_LABEL`] -- never the raw token,
//! never a fabricated name, and never a leak of the un-tokenized original
//! (there IS no un-tokenized original downstream of `core/svc_process`'s
//! inbound pass). A resolution failure is logged `ERROR` by the caller
//! (`detokenize`'s `DetokenizeOutcome::resolution_failed` flag) -- this
//! crate itself never panics or drops the message; a relay send degrades
//! to showing `NEUTRAL_LABEL` rather than failing closed on the whole
//! send, since dropping a chat relay message entirely on a transient
//! hub-api blip is a worse outage than one message showing a placeholder
//! name.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

/// Shown in place of a token this pass could not resolve to a real name
/// (hub-api unreachable, or a stale/evicted pseudonym) -- never the raw
/// token, never a guess.
pub const NEUTRAL_LABEL: &str = "Unknown User";

/// Every failure [`DisplayNameResolver::resolve_many`] can raise.
#[derive(Debug, thiserror::Error)]
pub enum DetokenizeError {
    #[error("display-name resolution unavailable: {0}")]
    ResolutionUnavailable(String),
}

type ResolveResult<'a> =
    Pin<Box<dyn Future<Output = Result<HashMap<String, String>, DetokenizeError>> + Send + 'a>>;

/// Resolves a batch of opaque tokens (hub_users UUIDs or ephemeral
/// pseudonyms) to display names in one call -- object-safe (manually-boxed
/// future, matching `svc_process::pii_tokenize::IdentityMinter`'s
/// identical rationale).
pub trait DisplayNameResolver: Send + Sync {
    fn resolve_many<'a>(&'a self, tenant_id: &'a str, tokens: Vec<String>) -> ResolveResult<'a>;
}

/// Production [`DisplayNameResolver`]: a thin adapter over `hub_client::
/// HubClient::resolve_display_names`.
pub struct HubClientResolver(pub Arc<hub_client::HubClient>);

impl DisplayNameResolver for HubClientResolver {
    fn resolve_many<'a>(&'a self, tenant_id: &'a str, tokens: Vec<String>) -> ResolveResult<'a> {
        Box::pin(async move {
            match self
                .0
                .resolve_display_names(tenant_id.to_string(), tokens)
                .await
            {
                Ok(resp) => Ok(resp
                    .names
                    .into_iter()
                    .map(|n| (n.uuid, n.display_name))
                    .collect()),
                Err(err) => Err(DetokenizeError::ResolutionUnavailable(err.to_string())),
            }
        })
    }
}

/// Which chat/overlay sink a detokenized message is about to be sent to --
/// each has its own escaping rules for a resolved display name (never for
/// the surrounding bundle-authored text, which this crate never touches).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sink {
    /// Discord: escape markdown control characters inside the display
    /// name so it can't open/close formatting or smuggle a raw `@everyone`/
    /// `@here`/role mention out of its position as a plain name.
    Discord,
    /// Twitch/IRC: strip CR/LF and other C0 control characters (the same
    /// defense `core/svc_action::capabilities::sanitize_irc_component`
    /// already applies to bundle-authored text) so a display name can't
    /// inject a second IRC line.
    Twitch,
    /// Browser-source/overlay HTML: escape `< > & " '` so a display name
    /// can't break out of its text-node position (XSS).
    Overlay,
}

/// Escapes a resolved display name for safe inclusion in `sink`'s
/// rendered output. Never applied to anything except the substituted name
/// itself -- the surrounding bundle-authored text passes through
/// unchanged (already sanitized by the sink's own existing pipeline,
/// e.g. `sanitize_irc_component`).
fn escape_name(name: &str, sink: Sink) -> String {
    match sink {
        Sink::Discord => {
            let mut out = String::with_capacity(name.len());
            for ch in name.chars() {
                if matches!(ch, '*' | '_' | '~' | '`' | '|' | '>' | '@') {
                    out.push('\\');
                }
                out.push(ch);
            }
            out
        }
        Sink::Twitch => name.chars().filter(|c| !c.is_control()).collect::<String>(),
        Sink::Overlay => {
            let mut out = String::with_capacity(name.len());
            for ch in name.chars() {
                match ch {
                    '<' => out.push_str("&lt;"),
                    '>' => out.push_str("&gt;"),
                    '&' => out.push_str("&amp;"),
                    '"' => out.push_str("&quot;"),
                    '\'' => out.push_str("&#39;"),
                    other => out.push(other),
                }
            }
            out
        }
    }
}

/// Outcome of one [`detokenize`] call -- asserted on directly by this
/// crate's own regression tests (never just "no panic").
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct DetokenizeOutcome {
    pub text: String,
    /// Tokens found in the text that did NOT resolve to a display name
    /// (either the resolver failed outright, or the response didn't
    /// recognize this specific token) -- the caller logs `ERROR` with this
    /// list when non-empty.
    pub unresolved_tokens: Vec<String>,
}

const TOKEN_PREFIX: &str = "{user:";

/// Returns `true` if `c` is valid inside a `{user:<token>}` token body --
/// covers UUIDv4/v5 text, this repo's `handle:<lowercased>` fallback keys,
/// and plain numeric platform ids.
fn is_token_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | ':' | '.')
}

/// Single-pass, non-recursive scan: un-escapes `\{`/`\}`/`\\` (the
/// forgery-protection escaping `core/svc_process/src/pii_tokenize.rs`'s
/// `escape_braces` applies to inbound free text) and substitutes every
/// well-formed, unescaped `{user:<token>}` placeholder with its resolved,
/// sink-escaped display name -- or [`NEUTRAL_LABEL`] when `names` doesn't
/// have an entry for that token. Never recurses into a substituted name
/// (a display name containing literal `{user:...}` text is never
/// re-scanned), and never matches a placeholder inside an escaped
/// sequence.
pub fn detokenize(text: &str, names: &HashMap<String, String>, sink: Sink) -> DetokenizeOutcome {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut unresolved = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\\' && i + 1 < chars.len() && matches!(chars[i + 1], '{' | '}' | '\\') {
            out.push(chars[i + 1]);
            i += 2;
            continue;
        }
        if c == '{'
            && chars[i..]
                .iter()
                .collect::<String>()
                .starts_with(TOKEN_PREFIX)
        {
            let body_start = i + TOKEN_PREFIX.len();
            let mut j = body_start;
            while j < chars.len() && is_token_char(chars[j]) {
                j += 1;
            }
            if j < chars.len() && chars[j] == '}' && j > body_start {
                let token: String = chars[body_start..j].iter().collect();
                match names.get(&token) {
                    Some(name) => out.push_str(&escape_name(name, sink)),
                    None => {
                        unresolved.push(token);
                        out.push_str(NEUTRAL_LABEL);
                    }
                }
                i = j + 1;
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    DetokenizeOutcome {
        text: out,
        unresolved_tokens: unresolved,
    }
}

/// Extracts every well-formed, unescaped `{user:<token>}` token in `text`
/// (deduplicated) without substituting anything -- used to build the
/// resolver batch before a single `resolve_many` call.
pub fn extract_tokens(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let mut tokens = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\\' && i + 1 < chars.len() && matches!(chars[i + 1], '{' | '}' | '\\') {
            i += 2;
            continue;
        }
        if c == '{'
            && chars[i..]
                .iter()
                .collect::<String>()
                .starts_with(TOKEN_PREFIX)
        {
            let body_start = i + TOKEN_PREFIX.len();
            let mut j = body_start;
            while j < chars.len() && is_token_char(chars[j]) {
                j += 1;
            }
            if j < chars.len() && chars[j] == '}' && j > body_start {
                let token: String = chars[body_start..j].iter().collect();
                if !tokens.contains(&token) {
                    tokens.push(token);
                }
                i = j + 1;
                continue;
            }
        }
        i += 1;
    }
    tokens
}

/// End-to-end convenience: extracts tokens, resolves them in one batch
/// (fail-safe-empty on any resolver error -- never propagates the error,
/// since a relay send must never hard-fail the whole message on a
/// transient hub-api blip), and substitutes.
pub async fn detokenize_resolving(
    text: &str,
    tenant_id: &str,
    resolver: &dyn DisplayNameResolver,
    sink: Sink,
) -> DetokenizeOutcome {
    let tokens = extract_tokens(text);
    if tokens.is_empty() {
        return DetokenizeOutcome {
            text: detokenize(text, &HashMap::new(), sink).text,
            unresolved_tokens: Vec::new(),
        };
    }
    let names = match resolver.resolve_many(tenant_id, tokens.clone()).await {
        Ok(names) => names,
        Err(err) => {
            tracing::error!(
                error = %err,
                token_count = tokens.len(),
                "display-name resolution unavailable; substituting the neutral label \
                 (fail-safe-empty, never leaking a raw token)"
            );
            HashMap::new()
        }
    };
    let outcome = detokenize(text, &names, sink);
    if !outcome.unresolved_tokens.is_empty() {
        tracing::error!(
            unresolved = ?outcome.unresolved_tokens,
            "one or more tokens did not resolve to a display name; substituted the neutral label"
        );
    }
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::strategy::Strategy as _;

    fn names(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    struct FakeResolver(HashMap<String, String>);

    impl DisplayNameResolver for FakeResolver {
        fn resolve_many<'a>(
            &'a self,
            _tenant_id: &'a str,
            tokens: Vec<String>,
        ) -> ResolveResult<'a> {
            let map = self.0.clone();
            Box::pin(async move {
                Ok(tokens
                    .into_iter()
                    .filter_map(|t| map.get(&t).cloned().map(|n| (t, n)))
                    .collect())
            })
        }
    }

    struct AlwaysFailResolver;

    impl DisplayNameResolver for AlwaysFailResolver {
        fn resolve_many<'a>(
            &'a self,
            _tenant_id: &'a str,
            _tokens: Vec<String>,
        ) -> ResolveResult<'a> {
            Box::pin(async move {
                Err(DetokenizeError::ResolutionUnavailable(
                    "hub-api unreachable (simulated)".to_string(),
                ))
            })
        }
    }

    #[test]
    fn substitutes_a_resolved_token_with_its_display_name() {
        let out = detokenize(
            "hi {user:abc-123}, welcome!",
            &names(&[("abc-123", "CoolStreamer")]),
            Sink::Twitch,
        );
        assert_eq!(out.text, "hi CoolStreamer, welcome!");
        assert!(out.unresolved_tokens.is_empty());
    }

    #[test]
    fn unresolved_token_becomes_the_neutral_label_not_the_raw_token() {
        let out = detokenize("hi {user:missing-1}", &HashMap::new(), Sink::Twitch);
        assert_eq!(out.text, format!("hi {NEUTRAL_LABEL}"));
        assert_eq!(out.unresolved_tokens, vec!["missing-1".to_string()]);
        assert!(!out.text.contains("missing-1"));
    }

    #[test]
    fn escaped_placeholder_is_unescaped_but_never_substituted() {
        let out = detokenize(
            "literal: \\{user:not-real\\}",
            &names(&[("not-real", "ShouldNotAppear")]),
            Sink::Twitch,
        );
        assert_eq!(out.text, "literal: {user:not-real}");
        assert!(out.unresolved_tokens.is_empty());
    }

    #[test]
    fn discord_sink_escapes_markdown_control_characters_in_the_name_only() {
        let out = detokenize(
            "hey {user:x}!",
            &names(&[("x", "*bold*_name_")]),
            Sink::Discord,
        );
        assert_eq!(out.text, "hey \\*bold\\*\\_name\\_!");
    }

    #[test]
    fn discord_sink_escapes_an_at_sign_so_a_name_cannot_smuggle_a_mention() {
        let out = detokenize(
            "{user:x} said hi",
            &names(&[("x", "@everyone")]),
            Sink::Discord,
        );
        assert_eq!(out.text, "\\@everyone said hi");
    }

    #[test]
    fn twitch_sink_strips_control_characters_from_the_name() {
        let out = detokenize(
            "hi {user:x}",
            &names(&[("x", "evil\r\nPRIVMSG")]),
            Sink::Twitch,
        );
        assert_eq!(out.text, "hi evilPRIVMSG");
        assert!(!out.text.contains('\r'));
        assert!(!out.text.contains('\n'));
    }

    #[test]
    fn overlay_sink_html_escapes_the_name() {
        let out = detokenize(
            "{user:x}",
            &names(&[("x", "<script>alert(1)</script>")]),
            Sink::Overlay,
        );
        assert_eq!(out.text, "&lt;script&gt;alert(1)&lt;/script&gt;");
        assert!(!out.text.contains('<'));
    }

    #[test]
    fn substituted_name_is_never_re_scanned_for_placeholders() {
        // A pathological display name that itself looks like a
        // placeholder must never be substituted a second time.
        let out = detokenize("{user:x}", &names(&[("x", "{user:y}")]), Sink::Twitch);
        assert_eq!(out.text, "{user:y}");
    }

    #[test]
    fn extract_tokens_deduplicates_and_skips_escaped_placeholders() {
        let tokens = extract_tokens("{user:a} said hi to {user:a} and \\{user:b\\}");
        assert_eq!(tokens, vec!["a".to_string()]);
    }

    #[tokio::test]
    async fn detokenize_resolving_substitutes_via_a_real_resolver_trait_call() {
        let resolver = FakeResolver(names(&[("a", "Alice")]));
        let out = detokenize_resolving("hi {user:a}", "tenant-1", &resolver, Sink::Twitch).await;
        assert_eq!(out.text, "hi Alice");
    }

    #[tokio::test]
    async fn detokenize_resolving_is_fail_safe_empty_never_fail_closed_on_resolver_error() {
        let out =
            detokenize_resolving("hi {user:a}", "tenant-1", &AlwaysFailResolver, Sink::Twitch)
                .await;
        // Never the raw token, never an error propagated to the caller --
        // the message still sends, with the neutral label.
        assert_eq!(out.text, format!("hi {NEUTRAL_LABEL}"));
        assert!(!out.text.contains("{user:a}"));
    }

    #[tokio::test]
    async fn detokenize_resolving_is_a_no_op_when_no_tokens_are_present() {
        let resolver = FakeResolver(HashMap::new());
        let out = detokenize_resolving("no tokens here", "tenant-1", &resolver, Sink::Twitch).await;
        assert_eq!(out.text, "no tokens here");
        assert!(out.unresolved_tokens.is_empty());
    }

    // Salvaged from PR #427 (`feature/egress-detokenizer`, closed as
    // superseded by this crate's shipped single-file design): two edge
    // cases the PR's multi-file `grammar.rs`/`render.rs` covered that this
    // crate's own test module did not yet assert directly.

    #[test]
    fn nested_placeholder_in_input_resolves_only_the_inner_one() {
        // `{user:{user:a}}` -- the outer `{user:` attempt fails because
        // the next byte (`{`) is not a valid token char, so it falls back
        // to literal text; the inner `{user:a}` is a well-formed
        // placeholder and resolves normally; the leftover trailing `}` is
        // literal. Regression for PR #427's
        // `nested_placeholder_resolves_only_the_inner_valid_one`.
        let out = detokenize("{user:{user:a}}", &names(&[("a", "Alice")]), Sink::Twitch);
        assert_eq!(out.text, "{user:Alice}");
    }

    proptest::proptest! {
        #![proptest_config(proptest::prelude::ProptestConfig::with_cases(256))]

        /// `detokenize` must never panic, and no raw token that resolved
        /// to a name may leave its literal `{user:<token>}` text in the
        /// output -- checked over 256 randomly generated inputs per run.
        /// Adapted from PR #427's `render.rs`
        /// `never_panics_and_never_leaks_a_valid_uuid` fuzz test to this
        /// crate's single-pass `detokenize` API.
        #[test]
        fn never_panics_and_never_leaks_a_known_token(
            pieces in proptest::collection::vec(
                proptest::prop_oneof![
                    "[a-zA-Z0-9 !?.,{}\\\\]{0,12}".prop_map(|s: String| (false, s)),
                    proptest::prelude::any::<u16>()
                        .prop_map(|n| (true, format!("{{user:tok-{n}}}"))),
                ],
                0..12,
            ),
            sink_idx in 0u8..3,
        ) {
            let sink = match sink_idx {
                0 => Sink::Discord,
                1 => Sink::Twitch,
                _ => Sink::Overlay,
            };
            let mut input = String::new();
            let mut placeholder_tokens = Vec::new();
            for (is_placeholder, text) in &pieces {
                input.push_str(text);
                if *is_placeholder {
                    // text is `{user:tok-<n>}`; the raw token is `tok-<n>`.
                    let token = text
                        .trim_start_matches("{user:")
                        .trim_end_matches('}')
                        .to_string();
                    placeholder_tokens.push(token);
                }
            }
            let resolved = names(&[("tok-1", "Alice"), ("tok-2", "Bob")]);
            let out = detokenize(&input, &resolved, sink);
            for token in &placeholder_tokens {
                if resolved.contains_key(token) {
                    let needle = format!("{{user:{token}}}");
                    proptest::prop_assert!(
                        !out.text.contains(&needle),
                        "raw placeholder {} leaked into output {:?} for input {:?}",
                        needle,
                        out.text,
                        input
                    );
                }
            }
        }
    }
}

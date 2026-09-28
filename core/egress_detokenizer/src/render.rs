//! Ties the strict grammar scanner ([`crate::grammar`]), the cached
//! resolver ([`crate::cache::NameCache`]), and per-sink escaping
//! ([`crate::sink::Sink`]) into the single public entry point,
//! [`Detokenizer::render`].
//!
//! Security review follow-ups on PR #427, implemented here:
//!
//! - **HIGH**: [`twitch_neutralize_command_starts`] neutralizes a leading
//!   `/`/`.` at true message start, immediately after any newline, and
//!   (unconditionally) at the start of every substituted name -- an
//!   IRC/Twitch client interprets a line starting with one of those
//!   characters as a local command (`/ban`, `.mods`, `/me`, ...), and a
//!   resolved display name is PII-store content this crate does not fully
//!   trust to never contain one.
//! - **MEDIUM**: every resolved name is grapheme-truncated to
//!   [`crate::limits::NAME_MAX_GRAPHEMES`] before rendering, and the total
//!   output is capped to `sink.default_max_len()` (or an explicit override
//!   via [`Detokenizer::render_with_limit`]), truncating at an atom
//!   boundary -- [`crate::sink::Sink::render_atoms`]'s docs -- so a cut
//!   never lands mid-grapheme or mid-escape-sequence.
//! - Cross-tenant resolution: see [`crate::resolver::NameResolver`]'s
//!   hardened contract doc and this module's
//!   `cross_tenant_uuid_never_resolves_to_another_tenants_user` test.

use std::collections::HashMap;

use uuid::Uuid;

use crate::cache::{CacheConfig, NameCache};
use crate::grammar::{scan, Segment};
use crate::limits::{truncate_graphemes, NAME_MAX_GRAPHEMES, TRUNCATION_INDICATOR};
use crate::resolver::{NameResolver, NEUTRAL_LABEL};
use crate::sink::{Atoms, Sink};

/// The host-side output detokenizer for one sink's egress point. Construct
/// one per `(sink component, resolver backend)` -- e.g. `svc_action` holds
/// one `Detokenizer` for its chat egress, backed by whatever client reaches
/// hub-api's `hub_users` mapping.
pub struct Detokenizer<R: NameResolver> {
    cache: NameCache<R>,
}

impl<R: NameResolver> Detokenizer<R> {
    pub fn new(resolver: R, config: CacheConfig) -> Self {
        Self {
            cache: NameCache::new(resolver, config),
        }
    }

    /// Renders `input` for `sink` using that sink's default length cap
    /// ([`Sink::default_max_len`]). See [`Self::render_with_limit`] for the
    /// full behavior and for overriding the overlay sink's configurable
    /// budget.
    pub async fn render(&self, tenant: &str, sink: Sink, input: &str) -> String {
        self.render_with_limit(tenant, sink, input, sink.default_max_len())
            .await
    }

    /// Renders `input` for `sink`, resolving every `{user:<uuid>}`
    /// placeholder to an escaped display name (or [`NEUTRAL_LABEL`] if
    /// erased/unknown/unresolvable/belonging to a different tenant) and
    /// escaping every literal segment for `sink`. **Single-pass,
    /// non-recursive**: `input` is scanned exactly once via [`scan`]; the
    /// resulting segments are walked once, in order, to build the output --
    /// a resolved name is escaped and appended, never re-scanned.
    /// Infallible by design: a resolver backend failure degrades to the
    /// neutral label rather than surfacing an `Err`.
    ///
    /// The rendered output never exceeds `max_len` `char`s -- once
    /// appending the next atom would, rendering stops and
    /// [`TRUNCATION_INDICATOR`] is appended once, at an atom boundary
    /// (never mid-grapheme or mid-escape-sequence).
    pub async fn render_with_limit(
        &self,
        tenant: &str,
        sink: Sink,
        input: &str,
        max_len: usize,
    ) -> String {
        let segments = scan(input);

        let mut uuids: Vec<Uuid> = segments
            .iter()
            .filter_map(|s| match s {
                Segment::Placeholder(u) => Some(*u),
                Segment::Literal(_) => None,
            })
            .collect();
        uuids.sort_unstable();
        uuids.dedup();

        let resolved: HashMap<Uuid, Option<String>> = if uuids.is_empty() {
            HashMap::new()
        } else {
            self.cache.resolve(tenant, &uuids).await
        };

        let mut out = String::new();
        let mut out_len = 0usize;
        let mut twitch_at_line_start = true;
        let mut truncated = false;

        'segments: for segment in segments {
            let (atoms, is_placeholder) = match segment {
                Segment::Literal(lit) => (sink.render_atoms(lit), false),
                Segment::Placeholder(uuid) => {
                    let raw_name = resolved
                        .get(&uuid)
                        .and_then(|opt| opt.as_deref())
                        .unwrap_or(NEUTRAL_LABEL);
                    // The resolved name is escaped for `sink` but is NOT
                    // re-passed through `scan` -- this is the "never
                    // re-scans its own substitution output" property
                    // (module doc / spec S10.4, Gemini condition 5).
                    let capped = truncate_graphemes(raw_name, NAME_MAX_GRAPHEMES);
                    (sink.render_atoms(&capped), true)
                }
            };

            let atoms = if sink == Sink::ChatTwitch {
                twitch_neutralize_command_starts(atoms, is_placeholder, &mut twitch_at_line_start)
            } else {
                atoms
            };

            for atom in atoms {
                let atom_len = atom.chars().count();
                if out_len + atom_len > max_len.saturating_sub(1) {
                    truncated = true;
                    break 'segments;
                }
                out.push_str(&atom);
                out_len += atom_len;
            }
        }

        if truncated {
            out.push(TRUNCATION_INDICATOR);
        }

        out
    }

    /// Immediate cache invalidation for one user within one tenant --
    /// call on a rename event (spec S10.4). See [`NameCache::invalidate`].
    pub fn invalidate(&self, tenant: &str, user: Uuid) {
        self.cache.invalidate(tenant, user);
    }

    /// Immediate cache invalidation for every cached entry in `tenant` --
    /// call on erasure (DSAR/right-to-erasure) or a bulk compliance
    /// operation. See [`NameCache::invalidate_tenant`].
    pub fn invalidate_tenant(&self, tenant: &str) {
        self.cache.invalidate_tenant(tenant);
    }
}

/// Neutralizes a leading IRC/Twitch client-command trigger (`/` or `.`)
/// wherever it would start a new logical line in the rendered Twitch
/// output (security review HIGH finding): the true start of the message,
/// immediately after any `\n` within literal bundle text, and --
/// regardless of surrounding context, since a resolved display name is
/// less trusted PII-store content than ordinary bundle text -- the start
/// of every substituted name.
///
/// `force_leading_check` treats the first atom of `atoms` as a line start
/// even if `*at_line_start` is currently `false` (the substituted-name
/// case, always checked); `at_line_start` is read/updated normally for
/// every other atom (including ones ending in `\n`, so internal newlines
/// within a single literal segment are also covered), and the final value
/// is left in `*at_line_start` so callers can thread it across multiple
/// segments in render order.
fn twitch_neutralize_command_starts(
    atoms: Atoms,
    force_leading_check: bool,
    at_line_start: &mut bool,
) -> Atoms {
    let mut out = Vec::with_capacity(atoms.len() + 1);
    for (i, atom) in atoms.into_iter().enumerate() {
        let is_check_position = if i == 0 {
            *at_line_start || force_leading_check
        } else {
            *at_line_start
        };
        if is_check_position && matches!(atom.chars().next(), Some('/') | Some('.')) {
            out.push('\u{200B}'.to_string());
        }
        *at_line_start = atom.ends_with('\n');
        out.push(atom);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::resolver::test_support::FixtureResolver;
    use std::sync::Arc;

    fn uuid_str(hex_tail: &str) -> String {
        format!("11111111-1111-4111-8111-{hex_tail}")
    }

    fn v4(tail: &str) -> Uuid {
        Uuid::parse_str(&uuid_str(tail)).unwrap()
    }

    fn v5(tail: &str) -> Uuid {
        Uuid::parse_str(&format!("22222222-2222-5222-9222-{tail}")).unwrap()
    }

    fn detok(resolver: FixtureResolver) -> Detokenizer<FixtureResolver> {
        Detokenizer::new(resolver, CacheConfig::default())
    }

    #[tokio::test]
    async fn resolves_a_plain_placeholder() {
        let user = v4("000000000001");
        let d = detok(FixtureResolver::with(&[("t1", user, "Alice")]));
        let out = d
            .render("t1", Sink::ChatTwitch, &format!("hi {{user:{user}}}!"))
            .await;
        assert_eq!(out, "hi Alice!");
    }

    #[tokio::test]
    async fn nested_placeholder_only_the_inner_valid_one_resolves() {
        let user = v4("000000000002");
        let d = detok(FixtureResolver::with(&[("t1", user, "Bob")]));
        let input = format!("{{user:{{user:{user}}}}}");
        let out = d.render("t1", Sink::Overlay, &input).await;
        // Outer `{`/leftover `}` are stray literal braces -> entity-escaped
        // (overlay only); the inner valid placeholder resolves to "Bob".
        assert_eq!(out, "&#123;user:Bob&#125;");
        assert!(!out.contains(&user.to_string()));
    }

    #[tokio::test]
    async fn escaped_and_malformed_braces_never_substitute_on_the_overlay_sink() {
        let d = detok(FixtureResolver::with(&[]));
        let cases = [
            "{user:not-a-uuid}",
            "{user:11111111-1111-4111-8111-111111111111", // unterminated
            "{USER:11111111-1111-4111-8111-111111111111}", // wrong case keyword
            "{user:11111111111141118111111111111111}",    // no dashes
        ];
        for input in cases {
            let out = d.render("t1", Sink::Overlay, input).await;
            assert!(
                out.contains("&#123;"),
                "expected the opening brace escaped in output for {input:?}, got {out:?}"
            );
            assert!(
                !out.contains('{') && !out.contains('}'),
                "no raw brace may survive rendering for {input:?}, got {out:?}"
            );
            assert!(
                !out.contains("a former viewer"),
                "malformed grammar must never be treated as a resolvable placeholder: {input:?} -> {out:?}"
            );
        }
    }

    #[tokio::test]
    async fn malformed_braces_stay_literal_on_chat_sinks() {
        // Security review LOW finding: chat sinks don't entity-escape
        // stray braces -- the scanner has already ruled these out as real
        // placeholders, and neither Twitch nor Discord treats `{`/`}`
        // specially.
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatTwitch, "{user:not-a-uuid}").await;
        assert_eq!(out, "{user:not-a-uuid}");
        let out = d.render("t1", Sink::ChatDiscord, "{user:not-a-uuid}").await;
        assert_eq!(out, "{user:not-a-uuid}");
    }

    #[tokio::test]
    async fn forged_token_wrong_version_nibble_is_never_substituted() {
        // Structurally UUID-shaped but version nibble '1' (not 4/5) --
        // never a real placeholder this platform could have minted. Stays
        // literal (Twitch: no brace escaping, per the LOW finding above).
        let d = detok(FixtureResolver::with(&[]));
        let input = "{user:11111111-1111-1111-8111-111111111111}";
        let out = d.render("t1", Sink::ChatTwitch, input).await;
        assert_eq!(out, input);
    }

    #[tokio::test]
    async fn forged_token_valid_shape_but_unresolvable_renders_neutral_label() {
        // Syntactically a legitimate placeholder (v4 shape) but not a user
        // the resolver has ever heard of -- an attacker guessing/forging a
        // UUID that was never actually issued.
        let user = v4("0000000000ff");
        let d = detok(FixtureResolver::with(&[]));
        let out = d
            .render(
                "t1",
                Sink::ChatTwitch,
                &format!("shoutout to {{user:{user}}}"),
            )
            .await;
        assert_eq!(out, "shoutout to a former viewer");
        assert!(!out.contains(&user.to_string()));
    }

    #[tokio::test]
    async fn resolved_name_containing_placeholder_syntax_is_not_re_substituted() {
        let real_user = v4("000000000003");
        let embedded_user = v5("000000000004");
        // A display name that is itself a crafted string shaped like a
        // second placeholder -- the self-referential injection Gemini
        // condition 5 calls out.
        let crafted_name = format!("Evil{{user:{embedded_user}}}Name");
        let d = detok(FixtureResolver::with(&[("t1", real_user, &crafted_name)]));

        let out = d
            .render("t1", Sink::ChatTwitch, &format!("hi {{user:{real_user}}}"))
            .await;

        assert_eq!(out, format!("hi {crafted_name}"));
        assert!(
            !out.contains("a former viewer"),
            "the embedded placeholder-shaped text must not be interpreted at all: {out:?}"
        );
    }

    #[tokio::test]
    async fn xss_payload_in_resolved_name_is_html_escaped_for_overlay() {
        let user = v4("000000000005");
        let payload = "<script>alert(document.cookie)</script>";
        let d = detok(FixtureResolver::with(&[("t1", user, payload)]));

        let out = d
            .render("t1", Sink::Overlay, &format!("{{user:{user}}}"))
            .await;

        assert!(!out.contains('<'));
        assert!(!out.contains('>'));
        assert_eq!(out, "&lt;script&gt;alert(document.cookie)&lt;/script&gt;");
    }

    #[tokio::test]
    async fn xss_payload_in_literal_bundle_text_is_also_escaped_for_overlay() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d
            .render("t1", Sink::Overlay, "<img src=x onerror=alert(1)>")
            .await;
        assert!(!out.contains('<'));
        assert!(!out.contains('>'));
    }

    #[tokio::test]
    async fn erased_user_renders_neutral_label_never_the_uuid() {
        let user = v4("000000000006");
        // Shared via `Arc` so the test can mutate the backing store
        // (simulating an erasure event) while `Detokenizer` holds its own
        // handle to the exact same resolver instance -- see the blanket
        // `NameResolver` impl for `Arc<T>` in `resolver.rs`.
        let resolver = Arc::new(FixtureResolver::with(&[("t1", user, "WasHere")]));
        let d = Detokenizer::new(resolver.clone(), CacheConfig::default());

        let first = d
            .render("t1", Sink::ChatDiscord, &format!("{{user:{user}}}"))
            .await;
        assert_eq!(first, "WasHere");

        // Erasure (DSAR/right-to-erasure): the identity store drops the
        // row; the caller invalidates the cache immediately rather than
        // waiting out the TTL (spec S10.4).
        resolver.erase("t1", user);
        d.invalidate("t1", user);

        let second = d
            .render("t1", Sink::ChatDiscord, &format!("{{user:{user}}}"))
            .await;
        assert_eq!(second, NEUTRAL_LABEL);
        assert!(!second.contains(&user.to_string()));
    }

    /// Security review item 4: a bundle writing `{user:<uuid>}` for a UUID
    /// that genuinely belongs to a DIFFERENT tenant must get the neutral
    /// label, never that other tenant's real display name -- the resolver
    /// is only ever asked, and only ever trusted, within the invoking
    /// tenant (`NameResolver`'s hardened contract doc).
    #[tokio::test]
    async fn cross_tenant_uuid_never_resolves_to_another_tenants_user() {
        let shared_uuid = v4("00000000000d");
        let resolver = FixtureResolver::with(&[("tenant-a", shared_uuid, "RealNameInTenantA")]);
        let d = detok(resolver);

        // Rendered under tenant-a: resolves normally.
        let out_a = d
            .render(
                "tenant-a",
                Sink::ChatTwitch,
                &format!("hi {{user:{shared_uuid}}}"),
            )
            .await;
        assert_eq!(out_a, "hi RealNameInTenantA");

        // The SAME uuid, rendered under a DIFFERENT tenant (as if a
        // bundle running for tenant-b had guessed/forged tenant-a's real
        // user id): must render the neutral label, never the leaked name.
        let out_b = d
            .render(
                "tenant-b",
                Sink::ChatTwitch,
                &format!("hi {{user:{shared_uuid}}}"),
            )
            .await;
        assert_eq!(out_b, "hi a former viewer");
        assert!(!out_b.contains("RealNameInTenantA"));
    }

    /// Corpus regression test (spec S10.6): renders a fixed corpus of
    /// synthetic bundle outputs, each containing one or more valid
    /// `{user:<uuid>}` placeholders (a mix of resolvable and
    /// erased/unknown), and asserts none of the underlying UUID strings
    /// survive rendering, across every sink. Corpus size and the number of
    /// (input, sink) pairs actually examined are both asserted non-zero
    /// (`critical-rules.md` Verification Integrity: a zero-payload test
    /// proves nothing).
    #[tokio::test]
    async fn no_uuid_survives_across_a_synthetic_corpus() {
        let known_user = v4("00000000000a");
        let erased_user = v4("00000000000b");
        let unknown_user = v5("00000000000c");
        let resolver = FixtureResolver::with(&[("t1", known_user, "StreamerFan42")]);
        let d = detok(resolver);

        let corpus: Vec<String> = vec![
            format!("gg {{user:{known_user}}} thanks for the raid!"),
            format!("{{user:{erased_user}}} was here"),
            format!("shoutout {{user:{unknown_user}}} and {{user:{known_user}}}"),
            "no placeholders at all in this one".to_string(),
            format!("malformed {{user:not-a-uuid}} next to real {{user:{known_user}}}"),
            format!("{{user:{known_user}}}{{user:{erased_user}}}{{user:{unknown_user}}}"),
        ];
        assert!(
            !corpus.is_empty(),
            "corpus must be non-empty for this assertion to mean anything"
        );

        let mut examined = 0usize;
        for input in &corpus {
            for sink in [Sink::ChatTwitch, Sink::ChatDiscord, Sink::Overlay] {
                let out = d.render("t1", sink, input).await;
                for uuid in [known_user, erased_user, unknown_user] {
                    assert!(
                        !out.contains(&uuid.to_string()),
                        "uuid {uuid} leaked into output for sink {sink:?}, input {input:?}: {out:?}"
                    );
                }
                examined += 1;
            }
        }
        assert_eq!(
            examined,
            corpus.len() * 3,
            "sanity: every corpus item x sink pair must be examined"
        );
    }

    // --- Security review item 1 (HIGH): Twitch/IRC command neutralization ---

    #[tokio::test]
    async fn twitch_neutralizes_a_leading_slash_command_at_message_start() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatTwitch, "/ban someone").await;
        assert!(!out.starts_with("/ban"));
        assert_eq!(out, "\u{200B}/ban someone");
    }

    #[tokio::test]
    async fn twitch_neutralizes_a_leading_dot_command_at_message_start() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatTwitch, ".mods").await;
        assert_eq!(out, "\u{200B}.mods");
    }

    #[tokio::test]
    async fn twitch_neutralizes_slash_me_at_message_start() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatTwitch, "/me does a thing").await;
        assert_eq!(out, "\u{200B}/me does a thing");
    }

    #[tokio::test]
    async fn twitch_neutralizes_a_command_immediately_after_a_newline() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d
            .render("t1", Sink::ChatTwitch, "hello\n/ban someone")
            .await;
        assert_eq!(out, "hello\n\u{200B}/ban someone");
    }

    #[tokio::test]
    async fn twitch_does_not_neutralize_a_slash_mid_line() {
        // Only a genuine line-start `/`/`.` is a command trigger; a
        // mid-sentence slash is ordinary text and must render unchanged.
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatTwitch, "n/a for now").await;
        assert_eq!(out, "n/a for now");
    }

    #[tokio::test]
    async fn twitch_neutralizes_a_resolved_name_starting_with_a_slash_command() {
        let user = v4("0000000000e1");
        let d = detok(FixtureResolver::with(&[("t1", user, "/ban everyone")]));
        // The name is substituted mid-sentence (not at message start or
        // after a newline) -- still neutralized, because a substituted
        // name is always checked regardless of position (module doc).
        let out = d
            .render("t1", Sink::ChatTwitch, &format!("hey {{user:{user}}}"))
            .await;
        assert_eq!(out, "hey \u{200B}/ban everyone");
    }

    #[tokio::test]
    async fn twitch_neutralizes_a_resolved_name_starting_with_dot_mods() {
        let user = v4("0000000000e2");
        let d = detok(FixtureResolver::with(&[("t1", user, ".mods")]));
        let out = d
            .render("t1", Sink::ChatTwitch, &format!("re: {{user:{user}}}"))
            .await;
        assert_eq!(out, "re: \u{200B}.mods");
    }

    #[tokio::test]
    async fn twitch_neutralizes_a_resolved_name_starting_with_slash_me() {
        let user = v4("0000000000e3");
        let d = detok(FixtureResolver::with(&[("t1", user, "/me was here")]));
        let out = d
            .render("t1", Sink::ChatTwitch, &format!("{{user:{user}}} says hi"))
            .await;
        assert_eq!(out, "\u{200B}/me was here says hi");
    }

    #[tokio::test]
    async fn discord_and_overlay_do_not_apply_twitch_command_neutralization() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatDiscord, "/ban someone").await;
        assert_eq!(out, "/ban someone");
        let out = d.render("t1", Sink::Overlay, "/ban someone").await;
        assert_eq!(out, "/ban someone");
    }

    // --- Security review item 2 (MEDIUM): length bounds ---

    #[tokio::test]
    async fn resolved_name_over_the_cap_is_truncated_with_an_ellipsis() {
        let user = v4("0000000000f1");
        let long_name = "A".repeat(100);
        let d = detok(FixtureResolver::with(&[("t1", user, &long_name)]));
        let out = d
            .render("t1", Sink::ChatDiscord, &format!("{{user:{user}}}"))
            .await;
        assert_eq!(out.chars().count(), NAME_MAX_GRAPHEMES);
        assert!(out.ends_with('\u{2026}'));
        assert!(out.starts_with(&"A".repeat(63)));
    }

    #[tokio::test]
    async fn total_output_is_capped_at_the_sinks_default_length() {
        let d = detok(FixtureResolver::with(&[]));
        let huge = "x".repeat(5000);
        let discord_out = d.render("t1", Sink::ChatDiscord, &huge).await;
        assert_eq!(discord_out.chars().count(), 2000);
        assert!(discord_out.ends_with('\u{2026}'));

        let twitch_out = d.render("t1", Sink::ChatTwitch, &huge).await;
        assert_eq!(twitch_out.chars().count(), 500);
        assert!(twitch_out.ends_with('\u{2026}'));
    }

    #[tokio::test]
    async fn overlay_length_cap_is_configurable_via_render_with_limit() {
        let d = detok(FixtureResolver::with(&[]));
        let huge = "x".repeat(1000);
        let out = d.render_with_limit("t1", Sink::Overlay, &huge, 50).await;
        assert_eq!(out.chars().count(), 50);
        assert!(out.ends_with('\u{2026}'));
    }

    #[tokio::test]
    async fn output_under_the_cap_is_not_truncated() {
        let d = detok(FixtureResolver::with(&[]));
        let out = d.render("t1", Sink::ChatDiscord, "short message").await;
        assert_eq!(out, "short message");
        assert!(!out.contains('\u{2026}'));
    }

    #[tokio::test]
    async fn truncation_never_splits_an_escape_sequence_or_grapheme() {
        // Force a truncation boundary to fall right where a Discord
        // backslash-escape pair would otherwise be split: 1999 plain
        // chars then a markdown-special, capped at 2000.
        let d = detok(FixtureResolver::with(&[]));
        let input = format!("{}*", "x".repeat(1999));
        let out = d.render("t1", Sink::ChatDiscord, &input).await;
        // Either the full 2-char escape ("\\*") made it in (2001 raw
        // chars would exceed the cap, so it must NOT have) or it was
        // dropped whole for the indicator -- never a lone trailing `\`.
        assert!(
            !out.ends_with('\\'),
            "must never end on a lone escape byte: {out:?}"
        );
        assert_eq!(out.chars().count(), 2000);
        assert!(out.ends_with('\u{2026}'));
    }
}

/// Property/fuzz test (spec: "Also a property/fuzz test"). Generates random
/// mixes of literal text, stray braces, and valid `{user:<uuid>}`
/// placeholders drawn from a small fixed pool, then asserts the invariants
/// that must hold for ANY input, not just the hand-picked examples in
/// `tests` above: `render` never panics, and every valid placeholder's own
/// UUID text is absent from the rendered output.
#[cfg(test)]
mod proptest_tests {
    use proptest::prelude::*;
    use uuid::Uuid;

    use crate::resolver::test_support::FixtureResolver;
    use crate::{CacheConfig, Detokenizer, Sink};

    /// A small fixed pool of valid v4/v5 placeholder UUIDs, half of which
    /// the fixture resolver actually knows a name for -- so generated
    /// inputs exercise both the resolved and the neutral-label path.
    fn known_pool() -> [Uuid; 2] {
        [
            Uuid::parse_str("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").unwrap(),
            Uuid::parse_str("bbbbbbbb-bbbb-5bbb-9bbb-bbbbbbbbbbbb").unwrap(),
        ]
    }

    fn unknown_pool() -> [Uuid; 2] {
        [
            Uuid::parse_str("cccccccc-cccc-4ccc-8ccc-cccccccccccc").unwrap(),
            Uuid::parse_str("dddddddd-dddd-5ddd-9ddd-dddddddddddd").unwrap(),
        ]
    }

    #[derive(Debug, Clone)]
    enum Piece {
        Text(String),
        Placeholder(Uuid),
    }

    fn piece_strategy() -> impl Strategy<Value = Piece> {
        let all_uuids: Vec<Uuid> = known_pool().into_iter().chain(unknown_pool()).collect();
        prop_oneof![
            3 => "[a-zA-Z0-9 @*_~`>{}/.\n]{0,12}".prop_map(Piece::Text),
            2 => prop::sample::select(all_uuids).prop_map(Piece::Placeholder),
        ]
    }

    fn sink_strategy() -> impl Strategy<Value = Sink> {
        prop_oneof![
            Just(Sink::ChatTwitch),
            Just(Sink::ChatDiscord),
            Just(Sink::Overlay),
        ]
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(256))]

        /// `render` must never panic, and no valid placeholder's UUID text
        /// may survive into the rendered output -- checked over 256
        /// randomly generated inputs per run (proptest reports the exact
        /// failing case, shrunk, on failure).
        #[test]
        fn never_panics_and_never_leaks_a_valid_uuid(
            pieces in prop::collection::vec(piece_strategy(), 0..12),
            sink in sink_strategy(),
        ) {
            let rt = tokio::runtime::Runtime::new().unwrap();
            let resolver = FixtureResolver::with(&[
                ("fuzz-tenant", known_pool()[0], "Alice"),
                ("fuzz-tenant", known_pool()[1], "Bob"),
            ]);
            let detokenizer = Detokenizer::new(resolver, CacheConfig::default());

            let mut input = String::new();
            let mut placeholder_uuids = Vec::new();
            for piece in &pieces {
                match piece {
                    Piece::Text(t) => input.push_str(t),
                    Piece::Placeholder(u) => {
                        input.push_str(&format!("{{user:{u}}}"));
                        placeholder_uuids.push(*u);
                    }
                }
            }

            let out = rt.block_on(detokenizer.render("fuzz-tenant", sink, &input));

            for uuid in &placeholder_uuids {
                prop_assert!(
                    !out.contains(&uuid.to_string()),
                    "uuid {} leaked into output {:?} for input {:?}",
                    uuid,
                    out,
                    input
                );
            }
        }
    }
}

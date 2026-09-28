//! Ties the strict grammar scanner ([`crate::grammar`]), the cached
//! resolver ([`crate::cache::NameCache`]), and per-sink escaping
//! ([`crate::sink::Sink`]) into the single public entry point,
//! [`Detokenizer::render`].

use std::collections::HashMap;

use uuid::Uuid;

use crate::cache::{CacheConfig, NameCache};
use crate::grammar::{scan, Segment};
use crate::resolver::{NameResolver, NEUTRAL_LABEL};
use crate::sink::Sink;

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

    /// Renders `input` for `sink`, resolving every `{user:<uuid>}`
    /// placeholder to an escaped display name (or [`NEUTRAL_LABEL`] if
    /// erased/unknown/unresolvable) and escaping every literal segment for
    /// `sink`. **Single-pass, non-recursive**: `input` is scanned exactly
    /// once via [`scan`]; the resulting segments are walked once, in
    /// order, to build `out` -- a resolved name is escaped and appended,
    /// never re-scanned. Infallible by design: a resolver backend failure
    /// degrades to the neutral label (see [`crate::cache::NameCache::
    /// resolve`]'s doc) rather than surfacing an `Err` a caller might be
    /// tempted to handle by sending the original, unrendered text.
    pub async fn render(&self, tenant: &str, sink: Sink, input: &str) -> String {
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

        let mut out = String::with_capacity(input.len());
        for segment in segments {
            match segment {
                Segment::Literal(lit) => out.push_str(&sink.escape_literal(lit)),
                Segment::Placeholder(uuid) => {
                    let name = resolved
                        .get(&uuid)
                        .and_then(|opt| opt.as_deref())
                        .unwrap_or(NEUTRAL_LABEL);
                    // The resolved name is escaped for `sink` but is NOT
                    // re-passed through `scan` -- this is the "never
                    // re-scans its own substitution output" property
                    // (module doc / spec S10.4, Gemini condition 5). A
                    // display name that itself contains literal
                    // `{user:...}`-shaped text is rendered as inert,
                    // sink-escaped text, not interpreted a second time.
                    out.push_str(&sink.escape(name));
                }
            }
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
        // Outer `{`/leftover `}` are stray literal braces -> entity-escaped;
        // the inner valid placeholder resolves to "Bob".
        assert_eq!(out, "&#123;user:Bob&#125;");
        assert!(!out.contains(&user.to_string()));
    }

    #[tokio::test]
    async fn escaped_and_malformed_braces_never_substitute() {
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
    async fn forged_token_wrong_version_nibble_is_never_substituted() {
        // Structurally UUID-shaped but version nibble '1' (not 4/5) --
        // never a real placeholder this platform could have minted.
        let d = detok(FixtureResolver::with(&[]));
        let input = "{user:11111111-1111-1111-8111-111111111111}";
        let out = d.render("t1", Sink::ChatTwitch, input).await;
        assert_eq!(out, "&#123;user:11111111-1111-1111-8111-111111111111&#125;");
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
            3 => "[a-zA-Z0-9 @*_~`>{}]{0,12}".prop_map(Piece::Text),
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

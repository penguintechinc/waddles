//! Overlay detokenization: turns the tokenized user references an overlay
//! push carries (`{user:<uuid>}` placeholders in free text, plus the
//! `chat_message.user`/`alert.user` UUID fields) into resolved, HTML-escaped
//! display names -- the PII boundary's outbound half for the overlay sink
//! (`rules/critical-rules.md` PII Tokenization: "the API server is the PII
//! boundary"; `core/egress_detokenizer`'s module doc describes the same
//! pass for chat sinks).
//!
//! # Two phases, one invariant
//!
//! * **Resolve (async, once per push)** -- [`OverlayDetokenizer::resolve`]
//!   collects every resolvable user UUID in a push, batches them into one
//!   `waddles.hub.internal.v1.IdentityService.ResolveDisplayNames` call
//!   through [`egress_detokenizer::DisplayNameResolver`] (production:
//!   `hub_client`), and returns a [`ResolvedNames`] map. Fail-safe-empty: an
//!   unreachable hub-api, an open circuit, a timeout, or an unknown UUID all
//!   yield "no name", never an error that drops the push and never a leaked
//!   token.
//! * **Render (sync, inside [`with_names`])** -- every per-surface renderer
//!   (`overlay::render::{alert_box,chat,crawler,full_screen,goals,media,
//!   ticker}`) sanitizes its free-text fields through [`sanitize_text`]:
//!   HTML-escape the bundle-authored text first, then substitute each
//!   `{user:<token>}` with its resolved name via
//!   [`egress_detokenizer::detokenize`] + [`Sink::Overlay`] (which escapes
//!   the *name* itself). Escaping first is safe because the placeholder
//!   grammar (`{`, `}`, `:`, alphanumerics, `-`, `_`, `.`) contains none of
//!   the five characters the escaper rewrites, and it guarantees every byte
//!   of output is escaped exactly once.
//!
//! The renderers' signature (`Renderer::render(&self, &OverlayPush,
//! &ResolvedTheme)`) is deliberately unchanged, so the resolved names reach
//! them through a thread-scoped slot ([`with_names`]) rather than an extra
//! parameter. **The invariant this buys:** a renderer can only ever emit a
//! display name that came out of a [`ResolvedNames`] map; rendered outside
//! any scope (a caller that forgot to resolve), every token becomes
//! [`NEUTRAL_LABEL`] and a `WARN` is logged -- never the raw token, never
//! the caller-supplied `display_name`.
//!
//! # Logging
//!
//! Nothing in this module logs a UUID, token, display name, or push text --
//! only counts, community/tenant ids, and static strings. That is why
//! [`egress_detokenizer::detokenize_resolving`] (which logs the unresolved
//! token values) is not used here; this module calls the resolver and the
//! pure [`detokenize`] directly.

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::Arc;
use std::time::{Duration, Instant};

use egress_detokenizer::{
    detokenize, extract_tokens, DisplayNameResolver, HubClientResolver, Sink, NEUTRAL_LABEL,
};
use overlay_schema::{OverlayPush, Surface};
use prometheus::{Histogram, HistogramOpts, IntCounter, IntCounterVec, Opts};

use super::render::{self, RenderError, RenderMetrics, RenderTheme, RenderedFrame};

/// hub-api's per-call cap on `ResolveDisplayNamesRequest.uuids`
/// (`proto/waddles/hub/internal/v1/identity.proto`: "Max 100 per call").
/// A push naming more distinct users than this resolves the first 100 and
/// renders the rest as [`NEUTRAL_LABEL`] -- one bounded RPC per push, never
/// an N-call fan-out an abusive bundle could drive.
pub const MAX_TOKENS_PER_PUSH: usize = 100;

/// Upper bound on one resolve round-trip (covers `hub_client`'s own
/// deadline + retries). A push must never hang on a slow hub-api; on
/// expiry every token renders as [`NEUTRAL_LABEL`].
pub const DEFAULT_RESOLVE_TIMEOUT: Duration = Duration::from_secs(3);

/// Escapes the five HTML-significant characters (`& < > " '`) -- the same
/// set, with the same replacements, as
/// `egress_detokenizer::Sink::Overlay`'s name escaper (a parity test in
/// this module's suite fails if the two ever drift).
pub fn escape_html(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            other => out.push(other),
        }
    }
    out
}

/// `true` only for a canonical, hyphenated 36-character UUID (any case).
///
/// Stricter than `uuid::Uuid::parse_str`, which also accepts the braced,
/// `urn:uuid:`-prefixed and simple (no hyphen) forms -- a user reference is
/// a hub-api-minted `str(uuid)`, so any other shape is malformed input (or
/// a raw username dressed up), never something to forward to hub-api.
pub fn is_user_uuid(value: &str) -> bool {
    value.len() == 36 && uuid::Uuid::parse_str(value).is_ok()
}

/// Display names hub-api resolved for one push, keyed by the token exactly
/// as it appeared in the push. Holds PII (display names), so `Debug` prints
/// the entry count only -- a stray `{:?}` can never put a name in a log.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct ResolvedNames(HashMap<String, String>);

impl ResolvedNames {
    /// Builds a map from `(token, display_name)` pairs.
    pub fn new(entries: impl IntoIterator<Item = (String, String)>) -> Self {
        Self(entries.into_iter().collect())
    }

    /// Number of resolved tokens.
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// `true` when nothing resolved.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

impl fmt::Debug for ResolvedNames {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ResolvedNames")
            .field("entries", &self.0.len())
            .finish_non_exhaustive()
    }
}

thread_local! {
    /// The names the renderer running on this thread may substitute. Set
    /// only for the duration of a [`with_names`] call (the renderers are
    /// synchronous, so one render never straddles threads).
    static ACTIVE_NAMES: RefCell<Option<ResolvedNames>> = const { RefCell::new(None) };
}

/// Runs `f` (a synchronous render) with `names` as the active resolution
/// map, restoring whatever was active before -- even if `f` panics. Scopes
/// nest; the innermost wins.
pub fn with_names<R>(names: ResolvedNames, f: impl FnOnce() -> R) -> R {
    struct Restore(Option<ResolvedNames>);
    impl Drop for Restore {
        fn drop(&mut self) {
            // `try_with`: during thread teardown the slot may already be
            // destroyed; there is nothing left to restore then.
            let _ = ACTIVE_NAMES.try_with(|slot| *slot.borrow_mut() = self.0.take());
        }
    }
    let previous = ACTIVE_NAMES.with(|slot| slot.borrow_mut().replace(names));
    let _restore = Restore(previous);
    f()
}

/// Sanitizes one bundle-authored free-text field for the overlay sink:
/// HTML-escapes it, then replaces every well-formed `{user:<token>}`
/// placeholder with its resolved, escaped display name (or
/// [`NEUTRAL_LABEL`] when the token is unknown, unresolved, or no
/// [`with_names`] scope is active). The result is safe to place in an HTML
/// text node and never contains a raw token.
pub fn sanitize_text(text: &str) -> String {
    let escaped = escape_html(text);
    ACTIVE_NAMES.with(|slot| {
        let slot = slot.borrow();
        let empty = HashMap::new();
        let (names, in_scope) = match slot.as_ref() {
            Some(resolved) => (&resolved.0, true),
            None => (&empty, false),
        };
        let outcome = detokenize(&escaped, names, Sink::Overlay);
        if !outcome.unresolved_tokens.is_empty() {
            let unresolved = outcome.unresolved_tokens.len();
            if in_scope {
                tracing::debug!(
                    unresolved,
                    "overlay text referenced user tokens with no resolved name; rendered the neutral label"
                );
            } else {
                tracing::warn!(
                    unresolved,
                    "overlay text rendered outside a resolved-name scope; every user token \
                     rendered as the neutral label (caller skipped OverlayDetokenizer::resolve)"
                );
            }
        }
        outcome.text
    })
}

/// The display name to show for `user` (a tenant-tokenized UUID): its
/// resolved, HTML-escaped name from the active [`with_names`] scope, or
/// [`NEUTRAL_LABEL`]. Never the UUID itself and never a caller-supplied
/// name -- the only source is hub-api via [`ResolvedNames`].
pub fn resolved_display_name(user: &str) -> String {
    if !is_user_uuid(user) {
        // Not a placeholder-grammar-safe token; do not build a `{user:..}`
        // string from untrusted text.
        return NEUTRAL_LABEL.to_string();
    }
    sanitize_text(&format!("{{user:{user}}}"))
}

/// Which tenant/community a resolve is for. `tenant_id` is the only value
/// hub-api scopes the lookup by (it must come from the validated
/// credential, never from a request body or path); `community_id` is carried
/// for log/trace context.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DetokScope {
    pub tenant_id: String,
    pub community_id: i64,
}

impl DetokScope {
    /// Builds a scope from a credential-derived tenant and community.
    pub fn new(tenant_id: impl Into<String>, community_id: i64) -> Self {
        Self {
            tenant_id: tenant_id.into(),
            community_id,
        }
    }
}

/// Detokenization metric handles. Histogram first, per
/// `rules/critical-rules.md` Observability.
#[derive(Clone)]
pub struct DetokMetrics {
    /// Resolve round-trips, labeled `outcome` = `ok` (every token
    /// resolved), `partial` (some unresolved), `unavailable` (hub-api
    /// error, timeout, or an unusable scope).
    pub resolutions_total: IntCounterVec,
    /// Wall-clock duration of the hub-api resolve round-trip.
    pub resolve_duration_seconds: Histogram,
    /// Tokens that rendered as the neutral label because no name resolved.
    pub unresolved_tokens_total: IntCounter,
}

/// Registers the detokenization metrics against `registry`. Call once per
/// registry (a duplicate registration panics), alongside
/// [`render::register_render_metrics`].
pub fn register_detok_metrics(registry: &prometheus::Registry) -> DetokMetrics {
    // The metric definitions below are static literals; `new` only fails on
    // an invalid name/label, so these `expect`s are provably infallible
    // (same precedent as `render::register_render_metrics`).
    let resolutions_total = IntCounterVec::new(
        Opts::new(
            "svc_presentation_overlay_detok_resolutions_total",
            "Overlay display-name resolutions against hub-api, labeled by outcome (ok/partial/unavailable)",
        ),
        &["outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(resolutions_total.clone()))
        .expect("register svc_presentation_overlay_detok_resolutions_total");

    let resolve_duration_seconds = Histogram::with_opts(HistogramOpts::new(
        "svc_presentation_overlay_detok_resolve_duration_seconds",
        "Duration of the hub-api ResolveDisplayNames round-trip for an overlay push, in seconds",
    ))
    .expect("valid metric definition");
    registry
        .register(Box::new(resolve_duration_seconds.clone()))
        .expect("register svc_presentation_overlay_detok_resolve_duration_seconds");

    let unresolved_tokens_total = IntCounter::new(
        "svc_presentation_overlay_detok_unresolved_tokens_total",
        "User tokens in overlay pushes that rendered as the neutral label (no name resolved)",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(unresolved_tokens_total.clone()))
        .expect("register svc_presentation_overlay_detok_unresolved_tokens_total");

    DetokMetrics {
        resolutions_total,
        resolve_duration_seconds,
        unresolved_tokens_total,
    }
}

/// Every free-text field a renderer sanitizes through [`sanitize_text`] and
/// that may therefore carry `{user:<token>}` placeholders. The single list
/// [`OverlayDetokenizer::resolve`] scans, kept next to the renderers'
/// contract so a field one renders is a field the other resolves (the
/// `every_rendered_field_is_resolved` test enforces it surface by surface).
fn push_texts(push: &OverlayPush) -> Vec<&str> {
    let mut texts: Vec<&str> = Vec::new();
    texts.extend(push.title.as_deref());
    texts.extend(push.body.as_deref());
    texts.extend(push.text.as_deref());
    if let Some(alert) = &push.alert {
        texts.push(&alert.alert_type);
        // The alert renderer shows a caller-supplied `display_name` only
        // when there is no `user` to resolve; otherwise it is ignored.
        if alert.user.is_none() {
            texts.extend(alert.display_name.as_deref());
        }
        texts.extend(alert.message.as_deref());
        if let Some(amount) = &alert.amount {
            json_strings(amount, &mut texts);
        }
    }
    if let Some(chat) = &push.chat_message {
        texts.push(&chat.text);
        texts.push(&chat.platform);
    }
    if let Some(goal) = &push.goal {
        texts.push(&goal.label);
        texts.extend(goal.unit.as_deref());
    }
    texts
}

/// Appends every string *value* inside `value` (recursively) to `out`.
fn json_strings<'a>(value: &'a serde_json::Value, out: &mut Vec<&'a str>) {
    match value {
        serde_json::Value::String(s) => out.push(s),
        serde_json::Value::Array(items) => items.iter().for_each(|v| json_strings(v, out)),
        serde_json::Value::Object(map) => map.values().for_each(|v| json_strings(v, out)),
        _ => {}
    }
}

/// Every distinct, resolvable user UUID in `push`, in first-seen order:
/// the `chat_message.user`/`alert.user` fields plus every `{user:<uuid>}`
/// placeholder in the text fields. Tokens that are not canonical UUIDs are
/// skipped (hub-api only resolves UUIDs; they render as the neutral label).
fn collect_tokens(push: &OverlayPush) -> Vec<String> {
    let mut seen: HashSet<String> = HashSet::new();
    let mut ordered: Vec<String> = Vec::new();
    let mut add = |token: &str| {
        if is_user_uuid(token) && seen.insert(token.to_string()) {
            ordered.push(token.to_string());
        }
    };
    for text in push_texts(push) {
        for token in extract_tokens(text) {
            add(&token);
        }
    }
    if let Some(chat) = &push.chat_message {
        add(&chat.user);
    }
    if let Some(user) = push.alert.as_ref().and_then(|a| a.user.as_deref()) {
        add(user);
    }
    ordered
}

/// Resolves a push's user references against hub-api and renders it with
/// those names in scope. Cheap to clone (the resolver is shared).
#[derive(Clone)]
pub struct OverlayDetokenizer {
    resolver: Arc<dyn DisplayNameResolver>,
    metrics: Option<DetokMetrics>,
    timeout: Duration,
}

impl OverlayDetokenizer {
    /// Builds a detokenizer over any [`DisplayNameResolver`] (production:
    /// [`Self::from_hub_client`]; tests: a fake -- no real gRPC I/O).
    pub fn new(resolver: Arc<dyn DisplayNameResolver>) -> Self {
        Self {
            resolver,
            metrics: None,
            timeout: DEFAULT_RESOLVE_TIMEOUT,
        }
    }

    /// Production constructor: resolves through hub-api's
    /// `IdentityService.ResolveDisplayNames` via `hub_client` (machine-JWT
    /// authenticated, retried, circuit-broken -- all inside the client).
    pub fn from_hub_client(client: Arc<hub_client::HubClient>) -> Self {
        Self::new(Arc::new(HubClientResolver(client)))
    }

    /// Records resolve outcomes/latency into `metrics`.
    #[must_use]
    pub fn with_metrics(mut self, metrics: DetokMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    /// Overrides [`DEFAULT_RESOLVE_TIMEOUT`] (mainly so tests can expire it
    /// quickly).
    #[must_use]
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// Resolves every user UUID in `push` to a display name in one batched,
    /// tenant-scoped hub-api call.
    ///
    /// Fail-safe-empty: any failure (no tenant, hub-api error, timeout)
    /// returns an empty map after an `ERROR` log -- the push still renders,
    /// every user as [`NEUTRAL_LABEL`]. The response is filtered to the
    /// tokens actually requested and to non-blank names, so a misbehaving
    /// resolver cannot inject names for other keys or blank out a label.
    #[tracing::instrument(
        name = "overlay.detok.resolve",
        skip_all,
        fields(community_id = scope.community_id, token_count = tracing::field::Empty)
    )]
    pub async fn resolve(&self, scope: &DetokScope, push: &OverlayPush) -> ResolvedNames {
        let mut tokens = collect_tokens(push);
        if tokens.is_empty() {
            return ResolvedNames::default();
        }
        if tokens.len() > MAX_TOKENS_PER_PUSH {
            tracing::warn!(
                requested = tokens.len(),
                cap = MAX_TOKENS_PER_PUSH,
                "overlay push names more users than one resolve call allows; the rest render as the neutral label"
            );
            tokens.truncate(MAX_TOKENS_PER_PUSH);
        }
        tracing::Span::current().record("token_count", tokens.len());

        if scope.tenant_id.trim().is_empty() {
            // Fail closed: never ask hub-api to resolve without a tenant.
            tracing::error!(
                community_id = scope.community_id,
                token_count = tokens.len(),
                "overlay detokenization has no tenant; refusing to resolve, rendering the neutral label"
            );
            self.record_outcome("unavailable", tokens.len(), None);
            return ResolvedNames::default();
        }

        // hub-api compares/echoes canonical lowercase UUIDs; send those,
        // then map each answer back to the token's original spelling.
        let mut by_canonical: HashMap<String, Vec<String>> = HashMap::new();
        for token in &tokens {
            by_canonical
                .entry(token.to_ascii_lowercase())
                .or_default()
                .push(token.clone());
        }
        let mut request: Vec<String> = by_canonical.keys().cloned().collect();
        request.sort_unstable();

        let started = Instant::now();
        let outcome = tokio::time::timeout(
            self.timeout,
            self.resolver.resolve_many(&scope.tenant_id, request),
        )
        .await;
        let elapsed = started.elapsed();

        let answered = match outcome {
            Ok(Ok(answered)) => answered,
            Ok(Err(err)) => {
                tracing::error!(
                    error = %err,
                    community_id = scope.community_id,
                    token_count = tokens.len(),
                    "display-name resolution unavailable; overlay renders the neutral label \
                     (fail-safe-empty, no raw token leaked)"
                );
                self.record_outcome("unavailable", tokens.len(), Some(elapsed));
                return ResolvedNames::default();
            }
            Err(_elapsed) => {
                tracing::error!(
                    timeout_ms = self.timeout.as_millis() as u64,
                    community_id = scope.community_id,
                    token_count = tokens.len(),
                    "display-name resolution timed out; overlay renders the neutral label \
                     (fail-safe-empty, no raw token leaked)"
                );
                self.record_outcome("unavailable", tokens.len(), Some(elapsed));
                return ResolvedNames::default();
            }
        };

        let mut resolved: HashMap<String, String> = HashMap::new();
        for (canonical, originals) in &by_canonical {
            let Some(name) = answered.get(canonical).filter(|n| !n.trim().is_empty()) else {
                continue;
            };
            for original in originals {
                resolved.insert(original.clone(), name.clone());
            }
        }
        let unresolved = tokens.len() - resolved.len();
        if unresolved > 0 {
            tracing::warn!(
                unresolved,
                community_id = scope.community_id,
                token_count = tokens.len(),
                "some overlay user tokens did not resolve; they render as the neutral label"
            );
        }
        tracing::debug!(
            resolved = resolved.len(),
            elapsed_ms = elapsed.as_secs_f64() * 1000.0,
            "overlay display names resolved"
        );
        self.record_outcome(
            if unresolved == 0 { "ok" } else { "partial" },
            unresolved,
            Some(elapsed),
        );
        ResolvedNames(resolved)
    }

    /// Records one resolve outcome: `unresolved` is the number of tokens
    /// that will render as the neutral label for this outcome.
    fn record_outcome(&self, outcome: &str, unresolved: usize, elapsed: Option<Duration>) {
        if let Some(metrics) = &self.metrics {
            metrics
                .resolutions_total
                .with_label_values(&[outcome])
                .inc();
            metrics.unresolved_tokens_total.inc_by(unresolved as u64);
            if let Some(elapsed) = elapsed {
                metrics
                    .resolve_duration_seconds
                    .observe(elapsed.as_secs_f64());
            }
        }
    }

    /// Resolves `push`'s user references, then renders it for `surface`
    /// with those names in scope -- the entry point a push route calls
    /// instead of [`render::render`] directly.
    pub async fn render(
        &self,
        scope: &DetokScope,
        surface: Surface,
        push: &OverlayPush,
        theme: &RenderTheme,
    ) -> Result<RenderedFrame, RenderError> {
        let names = self.resolve(scope, push).await;
        with_names(names, || render::render(surface, push, theme))
    }

    /// Same as [`Self::render`], recording [`RenderMetrics`] as
    /// [`render::render_with_metrics`] does.
    pub async fn render_with_metrics(
        &self,
        scope: &DetokScope,
        surface: Surface,
        push: &OverlayPush,
        theme: &RenderTheme,
        metrics: &RenderMetrics,
    ) -> Result<RenderedFrame, RenderError> {
        let names = self.resolve(scope, push).await;
        with_names(names, || {
            render::render_with_metrics(surface, push, theme, metrics)
        })
    }
}

/// Fixtures shared by this module's tests and the per-renderer test
/// modules.
#[cfg(test)]
pub(crate) mod test_support {
    use super::*;

    pub const USER_A: &str = "11111111-1111-1111-1111-111111111111";
    pub const USER_B: &str = "22222222-2222-2222-2222-222222222222";
    pub const USER_UNKNOWN: &str = "99999999-9999-9999-9999-999999999999";

    /// Builds a [`ResolvedNames`] from `(token, name)` string pairs.
    pub fn names(pairs: &[(&str, &str)]) -> ResolvedNames {
        ResolvedNames::new(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())))
    }

    /// A [`DisplayNameResolver`] answering from a fixed table (keys
    /// lowercase canonical UUIDs), recording every call's tenant and
    /// requested tokens.
    pub struct FakeResolver {
        pub table: HashMap<String, String>,
        pub calls: std::sync::Mutex<Vec<(String, Vec<String>)>>,
        pub delay: Option<Duration>,
        pub fail: bool,
    }

    impl FakeResolver {
        pub fn with(pairs: &[(&str, &str)]) -> Self {
            Self {
                table: pairs
                    .iter()
                    .map(|(k, v)| (k.to_string(), v.to_string()))
                    .collect(),
                calls: std::sync::Mutex::new(Vec::new()),
                delay: None,
                fail: false,
            }
        }

        pub fn failing() -> Self {
            Self {
                fail: true,
                ..Self::with(&[])
            }
        }

        pub fn call_count(&self) -> usize {
            self.calls.lock().unwrap().len()
        }
    }

    impl DisplayNameResolver for FakeResolver {
        fn resolve_many<'a>(
            &'a self,
            tenant_id: &'a str,
            tokens: Vec<String>,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<
                        Output = Result<
                            HashMap<String, String>,
                            egress_detokenizer::DetokenizeError,
                        >,
                    > + Send
                    + 'a,
            >,
        > {
            self.calls
                .lock()
                .unwrap()
                .push((tenant_id.to_string(), tokens.clone()));
            Box::pin(async move {
                if let Some(delay) = self.delay {
                    tokio::time::sleep(delay).await;
                }
                if self.fail {
                    return Err(egress_detokenizer::DetokenizeError::ResolutionUnavailable(
                        "hub-api unreachable (simulated)".to_string(),
                    ));
                }
                Ok(tokens
                    .into_iter()
                    .filter_map(|t| self.table.get(&t).cloned().map(|n| (t, n)))
                    .collect())
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::*;
    use super::*;
    use crate::overlay::render::register_render_metrics;
    use overlay_schema::{AlertPayload, ChatMessagePayload, GoalPayload, PushKind};

    fn scope() -> DetokScope {
        DetokScope::new("tenant-1", 7)
    }

    fn theme() -> RenderTheme {
        RenderTheme::default()
    }

    fn detokenizer(resolver: FakeResolver) -> (OverlayDetokenizer, Arc<FakeResolver>) {
        let resolver = Arc::new(resolver);
        (OverlayDetokenizer::new(resolver.clone()), resolver)
    }

    // -- pure helpers -------------------------------------------------

    #[test]
    fn escape_html_rewrites_all_five_significant_characters() {
        assert_eq!(
            escape_html(r#"<a href="x" onclick='y'>&</a>"#),
            "&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;"
        );
        assert_eq!(escape_html("plain text"), "plain text");
        assert_eq!(escape_html(""), "");
    }

    #[test]
    fn escape_html_matches_the_egress_overlay_sink_escaper() {
        // Guards against drift between this module's escaper and
        // `Sink::Overlay`'s name escaper (which escapes resolved names).
        let nasty = r#"<>&"'plain"#;
        let via_sink = detokenize(
            "{user:x}",
            &HashMap::from([("x".to_string(), nasty.to_string())]),
            Sink::Overlay,
        );
        assert_eq!(via_sink.text, escape_html(nasty));
    }

    #[test]
    fn is_user_uuid_accepts_only_canonical_hyphenated_uuids() {
        assert!(is_user_uuid(USER_A));
        assert!(is_user_uuid("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"));
        assert!(!is_user_uuid(""));
        assert!(!is_user_uuid("bob"));
        assert!(!is_user_uuid("11111111111111111111111111111111")); // simple
        assert!(!is_user_uuid("{11111111-1111-1111-1111-111111111111}")); // braced
        assert!(!is_user_uuid(
            "urn:uuid:11111111-1111-1111-1111-111111111111"
        ));
        assert!(!is_user_uuid("11111111-1111-1111-1111-11111111111g"));
    }

    #[test]
    fn resolved_names_debug_never_prints_a_name() {
        let n = names(&[(USER_A, "Secret Real Name")]);
        let shown = format!("{n:?}");
        assert!(!shown.contains("Secret Real Name"));
        assert!(!shown.contains(USER_A));
        assert!(shown.contains("entries"));
        assert_eq!(n.len(), 1);
        assert!(!n.is_empty());
        assert!(ResolvedNames::default().is_empty());
    }

    // -- scope --------------------------------------------------------

    #[test]
    fn with_names_scopes_nest_and_restore() {
        let outer = names(&[(USER_A, "Outer")]);
        let inner = names(&[(USER_A, "Inner")]);
        let tok = format!("{{user:{USER_A}}}");
        with_names(outer, || {
            assert_eq!(sanitize_text(&tok), "Outer");
            with_names(inner, || assert_eq!(sanitize_text(&tok), "Inner"));
            assert_eq!(sanitize_text(&tok), "Outer");
        });
        assert_eq!(sanitize_text(&tok), NEUTRAL_LABEL);
    }

    #[test]
    fn with_names_restores_the_previous_scope_after_a_panic() {
        let tok = format!("{{user:{USER_A}}}");
        let result = std::panic::catch_unwind(|| {
            with_names(names(&[(USER_A, "Boom")]), || panic!("render blew up"));
        });
        assert!(result.is_err());
        assert_eq!(
            sanitize_text(&tok),
            NEUTRAL_LABEL,
            "scope leaked past a panic"
        );
    }

    #[test]
    fn with_names_is_isolated_per_thread() {
        let tok = format!("{{user:{USER_A}}}");
        with_names(names(&[(USER_A, "Main")]), || {
            let other = std::thread::spawn({
                let tok = tok.clone();
                move || sanitize_text(&tok)
            })
            .join()
            .unwrap();
            assert_eq!(other, NEUTRAL_LABEL);
            assert_eq!(sanitize_text(&tok), "Main");
        });
    }

    // -- sanitize_text / resolved_display_name ------------------------

    #[test]
    fn sanitize_text_escapes_surrounding_text_and_substitutes_resolved_names() {
        let text = format!("<b>hi</b> {{user:{USER_A}}} & {{user:{USER_B}}}!");
        let out = with_names(names(&[(USER_A, "Alice"), (USER_B, "Bob")]), || {
            sanitize_text(&text)
        });
        assert_eq!(out, "&lt;b&gt;hi&lt;/b&gt; Alice &amp; Bob!");
    }

    #[test]
    fn sanitize_text_escapes_a_hostile_display_name_exactly_once() {
        let text = format!("{{user:{USER_A}}}");
        let out = with_names(
            names(&[(USER_A, "<script>alert('x')</script> & co")]),
            || sanitize_text(&text),
        );
        assert_eq!(
            out,
            "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt; &amp; co"
        );
        assert!(!out.contains('<'));
    }

    #[test]
    fn sanitize_text_renders_an_unresolved_token_as_the_neutral_label() {
        let text = format!("hi {{user:{USER_UNKNOWN}}}");
        let out = with_names(names(&[(USER_A, "Alice")]), || sanitize_text(&text));
        assert_eq!(out, format!("hi {NEUTRAL_LABEL}"));
        assert!(!out.contains(USER_UNKNOWN));
    }

    #[test]
    fn sanitize_text_outside_any_scope_never_leaks_the_token() {
        let text = format!("hi {{user:{USER_A}}} <i>x</i>");
        let out = sanitize_text(&text);
        assert_eq!(out, format!("hi {NEUTRAL_LABEL} &lt;i&gt;x&lt;/i&gt;"));
        assert!(!out.contains(USER_A));
    }

    #[test]
    fn sanitize_text_does_not_re_resolve_a_name_that_looks_like_a_token() {
        let tricky = format!("{{user:{USER_B}}}");
        let out = with_names(names(&[(USER_A, tricky.as_str()), (USER_B, "Bob")]), || {
            sanitize_text(&format!("{{user:{USER_A}}}"))
        });
        assert_eq!(out, tricky, "a substituted name must never be re-scanned");
    }

    #[test]
    fn sanitize_text_keeps_a_backslash_escaped_placeholder_literal() {
        let out = with_names(names(&[(USER_A, "Alice")]), || {
            sanitize_text(&format!("literal \\{{user:{USER_A}\\}}"))
        });
        assert_eq!(out, format!("literal {{user:{USER_A}}}"));
        assert!(!out.contains("Alice"));
    }

    #[test]
    fn sanitize_text_passes_plain_text_through_unchanged() {
        assert_eq!(sanitize_text("hello world"), "hello world");
    }

    #[test]
    fn resolved_display_name_returns_the_escaped_name_or_the_label() {
        with_names(names(&[(USER_A, "A <b>")]), || {
            assert_eq!(resolved_display_name(USER_A), "A &lt;b&gt;");
            assert_eq!(resolved_display_name(USER_B), NEUTRAL_LABEL);
        });
    }

    #[test]
    fn resolved_display_name_refuses_a_non_uuid_reference() {
        with_names(names(&[("bob", "Bob Real Name")]), || {
            assert_eq!(resolved_display_name("bob"), NEUTRAL_LABEL);
            assert_eq!(resolved_display_name("}{user:bob"), NEUTRAL_LABEL);
        });
    }

    // -- token collection ---------------------------------------------

    #[test]
    fn collect_tokens_gathers_every_field_dedups_and_filters_non_uuids() {
        let push = OverlayPush {
            title: Some(format!("{{user:{USER_A}}}")),
            body: Some(format!("{{user:{USER_A}}} and {{user:{USER_B}}}")),
            text: Some("{user:not-a-uuid} {user:handle:bob}".to_string()),
            alert: Some(AlertPayload {
                alert_type: "sub".to_string(),
                user: Some(USER_UNKNOWN.to_string()),
                display_name: Some(format!(
                    "{{user:{}}}",
                    "33333333-3333-3333-3333-333333333333"
                )),
                amount: Some(
                    serde_json::json!({"who": format!("{{user:{}}}", "44444444-4444-4444-4444-444444444444"), "n": 3}),
                ),
                message: None,
            }),
            ..Default::default()
        };
        let tokens = collect_tokens(&push);
        assert_eq!(
            tokens,
            vec![
                USER_A.to_string(),
                USER_B.to_string(),
                "44444444-4444-4444-4444-444444444444".to_string(),
                USER_UNKNOWN.to_string(),
            ],
            "alert.display_name is ignored when alert.user is set"
        );
    }

    #[test]
    fn collect_tokens_reads_alert_display_name_only_without_a_user() {
        let push = OverlayPush {
            alert: Some(AlertPayload {
                alert_type: "follow".to_string(),
                user: None,
                display_name: Some(format!("{{user:{USER_A}}}")),
                amount: None,
                message: None,
            }),
            ..Default::default()
        };
        assert_eq!(collect_tokens(&push), vec![USER_A.to_string()]);
    }

    #[test]
    fn collect_tokens_includes_chat_user_goal_and_nested_amount_strings() {
        let push = OverlayPush {
            chat_message: Some(ChatMessagePayload {
                user: USER_A.to_string(),
                display_name: String::new(),
                platform: "twitch".to_string(),
                text: format!("hey {{user:{USER_B}}}"),
            }),
            goal: Some(GoalPayload {
                label: format!("for {{user:{USER_UNKNOWN}}}"),
                current: 1.0,
                target: 2.0,
                unit: None,
            }),
            alert: Some(AlertPayload {
                alert_type: "cheer".to_string(),
                user: None,
                display_name: None,
                amount: Some(
                    serde_json::json!([[{"deep": format!("{{user:{}}}", "55555555-5555-5555-5555-555555555555")}], true, null]),
                ),
                message: None,
            }),
            ..Default::default()
        };
        let tokens = collect_tokens(&push);
        for expected in [
            USER_A,
            USER_B,
            USER_UNKNOWN,
            "55555555-5555-5555-5555-555555555555",
        ] {
            assert!(tokens.contains(&expected.to_string()), "missing {expected}");
        }
        assert_eq!(tokens.len(), 4);
    }

    #[test]
    fn collect_tokens_is_empty_for_a_push_with_no_user_references() {
        let push = OverlayPush {
            title: Some("plain".to_string()),
            ..Default::default()
        };
        assert!(collect_tokens(&push).is_empty());
    }

    // -- resolve ------------------------------------------------------

    fn chat_push(text: &str) -> OverlayPush {
        OverlayPush {
            chat_message: Some(ChatMessagePayload {
                user: USER_A.to_string(),
                display_name: "caller supplied".to_string(),
                platform: "twitch".to_string(),
                text: text.to_string(),
            }),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn resolve_batches_every_token_into_one_tenant_scoped_call() {
        let (d, resolver) = detokenizer(FakeResolver::with(&[(USER_A, "Alice"), (USER_B, "Bob")]));
        let push = chat_push(&format!("hi {{user:{USER_B}}} and {{user:{USER_B}}}"));
        let resolved = d.resolve(&scope(), &push).await;
        assert_eq!(resolved, names(&[(USER_A, "Alice"), (USER_B, "Bob")]));
        let calls = resolver.calls.lock().unwrap();
        assert_eq!(calls.len(), 1, "one batched RPC per push");
        assert_eq!(calls[0].0, "tenant-1");
        let mut sent = calls[0].1.clone();
        sent.sort();
        assert_eq!(sent, vec![USER_A.to_string(), USER_B.to_string()]);
    }

    #[tokio::test]
    async fn resolve_makes_no_call_when_the_push_names_no_users() {
        let (d, resolver) = detokenizer(FakeResolver::with(&[]));
        let push = OverlayPush {
            title: Some("nothing to resolve".to_string()),
            ..Default::default()
        };
        assert!(d.resolve(&scope(), &push).await.is_empty());
        assert_eq!(resolver.call_count(), 0);
    }

    #[tokio::test]
    async fn resolve_refuses_to_call_hub_api_without_a_tenant() {
        let (d, resolver) = detokenizer(FakeResolver::with(&[(USER_A, "Alice")]));
        let resolved = d.resolve(&DetokScope::new("  ", 7), &chat_push("hi")).await;
        assert!(resolved.is_empty());
        assert_eq!(
            resolver.call_count(),
            0,
            "must fail closed, not call with no tenant"
        );
    }

    #[tokio::test]
    async fn resolve_is_fail_safe_empty_when_the_resolver_errors() {
        let (d, resolver) = detokenizer(FakeResolver::failing());
        let resolved = d.resolve(&scope(), &chat_push("hi")).await;
        assert!(resolved.is_empty());
        assert_eq!(resolver.call_count(), 1);
    }

    #[tokio::test]
    async fn resolve_is_fail_safe_empty_on_timeout() {
        let mut fake = FakeResolver::with(&[(USER_A, "Alice")]);
        fake.delay = Some(Duration::from_millis(500));
        let (d, _resolver) = detokenizer(fake);
        let d = d.with_timeout(Duration::from_millis(20));
        let resolved = d.resolve(&scope(), &chat_push("hi")).await;
        assert!(resolved.is_empty());
    }

    #[tokio::test]
    async fn resolve_caps_the_batch_at_hub_apis_per_call_limit() {
        let (d, resolver) = detokenizer(FakeResolver::with(&[]));
        let text: String = (0..MAX_TOKENS_PER_PUSH + 25)
            .map(|i| format!("{{user:{i:08}-0000-0000-0000-000000000000}}"))
            .collect::<Vec<_>>()
            .join(" ");
        let push = OverlayPush {
            // Spread over fields so the per-field text cap is not the limit.
            title: Some(text[..400].to_string()),
            body: Some(text.clone()),
            ..Default::default()
        };
        let _ = d.resolve(&scope(), &push).await;
        let calls = resolver.calls.lock().unwrap();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0].1.len(), MAX_TOKENS_PER_PUSH);
    }

    #[tokio::test]
    async fn resolve_drops_names_for_unrequested_keys_and_blank_names() {
        struct Sloppy;
        impl DisplayNameResolver for Sloppy {
            fn resolve_many<'a>(
                &'a self,
                _tenant_id: &'a str,
                _tokens: Vec<String>,
            ) -> std::pin::Pin<
                Box<
                    dyn std::future::Future<
                            Output = Result<
                                HashMap<String, String>,
                                egress_detokenizer::DetokenizeError,
                            >,
                        > + Send
                        + 'a,
                >,
            > {
                Box::pin(async move {
                    Ok(HashMap::from([
                        (USER_A.to_string(), "   ".to_string()),
                        (USER_B.to_string(), "Bob".to_string()),
                        ("injected-key".to_string(), "Mallory".to_string()),
                    ]))
                })
            }
        }
        let d = OverlayDetokenizer::new(Arc::new(Sloppy));
        let push = chat_push(&format!("{{user:{USER_B}}} {{user:injected-key}}"));
        let resolved = d.resolve(&scope(), &push).await;
        assert_eq!(resolved, names(&[(USER_B, "Bob")]));
    }

    #[tokio::test]
    async fn resolve_maps_an_uppercase_token_back_to_its_original_spelling() {
        let upper = USER_A.replace('1', "A").to_ascii_uppercase();
        let lower = upper.to_ascii_lowercase();
        let (d, resolver) = detokenizer(FakeResolver::with(&[(lower.as_str(), "Alice")]));
        let push = OverlayPush {
            title: Some(format!("{{user:{upper}}}")),
            ..Default::default()
        };
        let resolved = d.resolve(&scope(), &push).await;
        assert_eq!(resolved, names(&[(upper.as_str(), "Alice")]));
        assert_eq!(resolver.calls.lock().unwrap()[0].1, vec![lower]);
    }

    #[tokio::test]
    async fn resolve_records_ok_partial_and_unavailable_outcomes() {
        let registry = prometheus::Registry::new();
        let metrics = register_detok_metrics(&registry);

        // ok
        let (d, _) = detokenizer(FakeResolver::with(&[(USER_A, "Alice")]));
        let d = d.with_metrics(metrics.clone());
        d.resolve(&scope(), &chat_push("hi")).await;
        // partial
        let push = chat_push(&format!("{{user:{USER_UNKNOWN}}}"));
        d.resolve(&scope(), &push).await;
        // unavailable (error), unavailable (no tenant)
        let (failing, _) = detokenizer(FakeResolver::failing());
        let failing = failing.with_metrics(metrics.clone());
        failing.resolve(&scope(), &chat_push("hi")).await;
        failing
            .resolve(&DetokScope::new("", 1), &chat_push("hi"))
            .await;

        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        for line in [
            "svc_presentation_overlay_detok_resolutions_total{outcome=\"ok\"} 1",
            "svc_presentation_overlay_detok_resolutions_total{outcome=\"partial\"} 1",
            "svc_presentation_overlay_detok_resolutions_total{outcome=\"unavailable\"} 2",
            "svc_presentation_overlay_detok_unresolved_tokens_total 3",
            "svc_presentation_overlay_detok_resolve_duration_seconds_count 3",
        ] {
            assert!(rendered.contains(line), "missing {line:?} in:\n{rendered}");
        }
    }

    // -- end to end through the real renderers ------------------------

    fn rendered_json(frame: &RenderedFrame) -> String {
        serde_json::to_string(frame).unwrap()
    }

    #[tokio::test]
    async fn render_resolves_chat_and_never_emits_the_user_uuid() {
        let (d, _) = detokenizer(FakeResolver::with(&[(USER_A, "Alice"), (USER_B, "Bob")]));
        let push = chat_push(&format!("<b>hey</b> {{user:{USER_B}}}"));
        let frame = d
            .render(&scope(), Surface::Chat, &push, &theme())
            .await
            .unwrap();
        let json = rendered_json(&frame);
        assert!(json.contains(r#""display_name":"Alice""#), "{json}");
        assert!(json.contains("&lt;b&gt;hey&lt;/b&gt; Bob"), "{json}");
        assert!(!json.contains(USER_A) && !json.contains(USER_B), "{json}");
        assert!(!json.contains("caller supplied"), "{json}");
    }

    #[tokio::test]
    async fn render_is_fail_safe_empty_when_hub_api_is_down() {
        let (d, _) = detokenizer(FakeResolver::failing());
        let push = chat_push(&format!("hey {{user:{USER_B}}}"));
        let frame = d
            .render(&scope(), Surface::Chat, &push, &theme())
            .await
            .unwrap();
        let json = rendered_json(&frame);
        assert!(
            json.contains(&format!(r#""display_name":"{NEUTRAL_LABEL}""#)),
            "{json}"
        );
        assert!(json.contains(&format!("hey {NEUTRAL_LABEL}")), "{json}");
        assert!(!json.contains(USER_A) && !json.contains(USER_B) && !json.contains("{user:"));
    }

    #[tokio::test]
    async fn render_with_metrics_resolves_and_records_the_render() {
        let registry = prometheus::Registry::new();
        let render_metrics = register_render_metrics(&registry);
        let (d, _) = detokenizer(FakeResolver::with(&[(USER_A, "Alice")]));
        let frame = d
            .render_with_metrics(
                &scope(),
                Surface::Chat,
                &chat_push("hi"),
                &theme(),
                &render_metrics,
            )
            .await
            .unwrap();
        assert!(rendered_json(&frame).contains("Alice"));
        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("svc_presentation_overlay_renders_total"));
    }

    #[tokio::test]
    async fn render_propagates_a_renderer_rejection_unchanged() {
        let (d, _) = detokenizer(FakeResolver::with(&[]));
        let err = d
            .render(&scope(), Surface::Chat, &OverlayPush::default(), &theme())
            .await
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::MissingField {
                surface: Surface::Chat,
                field: "chat_message"
            }
        );
    }

    /// Builds a push with `payload` (a `<script>` + a user token) in every
    /// text field the given surface renders, so the assertions below prove
    /// each field is both resolved and escaped.
    fn hostile_push(surface: Surface) -> OverlayPush {
        let tok = format!("{{user:{USER_A}}}");
        let evil = format!("<script>alert(1)</script>{tok}");
        match surface {
            Surface::FullScreen | Surface::Media => OverlayPush {
                title: Some(evil.clone()),
                body: Some(evil.clone()),
                image_url: Some("https://example.com/a.png".to_string()),
                ..Default::default()
            },
            Surface::Crawler | Surface::Ticker => OverlayPush {
                text: Some(evil.clone()),
                ..Default::default()
            },
            Surface::AlertBox => OverlayPush {
                alert: Some(AlertPayload {
                    alert_type: "sub".to_string(),
                    user: Some(USER_B.to_string()),
                    display_name: Some(evil.clone()),
                    amount: Some(serde_json::json!({"note": evil.clone()})),
                    message: Some(evil.clone()),
                }),
                ..Default::default()
            },
            Surface::Chat => OverlayPush {
                chat_message: Some(ChatMessagePayload {
                    user: USER_B.to_string(),
                    display_name: evil.clone(),
                    platform: "twitch".to_string(),
                    text: evil.clone(),
                }),
                ..Default::default()
            },
            Surface::Goals => OverlayPush {
                goal: Some(GoalPayload {
                    label: evil.clone(),
                    current: 1.0,
                    target: 10.0,
                    unit: Some(evil.clone()),
                }),
                ..Default::default()
            },
            Surface::Music => OverlayPush {
                kind: Some(PushKind::Clear),
                ..Default::default()
            },
            Surface::Image => OverlayPush::default(),
        }
    }

    /// regression: every text-bearing surface resolves tokens AND escapes
    /// markup, and no raw token / UUID / `<script>` ever reaches the wire
    /// frame (`rules/critical-rules.md` PII Tokenization + XSS output
    /// escaping). Also pins that every field a renderer sanitizes is a
    /// field `resolve` scans (otherwise the token would render as the
    /// neutral label even though hub-api knew the name).
    #[tokio::test]
    async fn every_rendered_field_is_resolved_escaped_and_leak_free() {
        for surface in Surface::ALL {
            if *surface == Surface::Image {
                continue; // fail-loud stub, nothing rendered
            }
            let (d, _) = detokenizer(FakeResolver::with(&[(USER_A, "Al<i>ce"), (USER_B, "Bob")]));
            let frame = d
                .render(&scope(), *surface, &hostile_push(*surface), &theme())
                .await
                .unwrap_or_else(|e| panic!("{surface} should render: {e}"));
            let json = rendered_json(&frame);
            assert!(
                !json.contains("<script"),
                "{surface}: raw <script> leaked: {json}"
            );
            assert!(!json.contains(USER_A), "{surface}: uuid leaked: {json}");
            assert!(!json.contains(USER_B), "{surface}: uuid leaked: {json}");
            assert!(
                !json.contains("{user:"),
                "{surface}: raw token leaked: {json}"
            );
            assert!(
                !json.contains("Al<i>ce"),
                "{surface}: name not escaped: {json}"
            );
            match surface {
                Surface::Music => assert!(!json.contains("Al&lt;i&gt;ce")),
                Surface::AlertBox | Surface::Chat => {
                    assert!(json.contains("Bob"), "{surface}: user name missing: {json}");
                    assert!(
                        json.contains("&lt;script&gt;alert(1)&lt;/script&gt;Al&lt;i&gt;ce"),
                        "{surface}: text token not resolved+escaped: {json}"
                    );
                }
                _ => assert!(
                    json.contains("&lt;script&gt;alert(1)&lt;/script&gt;Al&lt;i&gt;ce"),
                    "{surface}: token not resolved+escaped: {json}"
                ),
            }
        }
    }

    #[tokio::test]
    async fn every_rendered_field_is_leak_free_when_nothing_resolves() {
        for surface in Surface::ALL {
            if *surface == Surface::Image {
                continue;
            }
            let (d, _) = detokenizer(FakeResolver::failing());
            let frame = d
                .render(&scope(), *surface, &hostile_push(*surface), &theme())
                .await
                .unwrap();
            let json = rendered_json(&frame);
            assert!(!json.contains("<script"), "{surface}: {json}");
            assert!(
                !json.contains(USER_A) && !json.contains(USER_B),
                "{surface}: {json}"
            );
            assert!(!json.contains("{user:"), "{surface}: {json}");
        }
    }

    #[test]
    fn register_detok_metrics_registers_once_per_registry() {
        let registry = prometheus::Registry::new();
        let _ = register_detok_metrics(&registry);
        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        // Counters/histograms with no observations still gather once a
        // labeled child or the metric itself exists; the unlabeled ones do.
        assert!(rendered.contains("svc_presentation_overlay_detok_unresolved_tokens_total 0"));
        assert!(rendered.contains("svc_presentation_overlay_detok_resolve_duration_seconds"));
    }
}

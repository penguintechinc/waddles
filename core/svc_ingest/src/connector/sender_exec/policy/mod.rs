//! Per-platform, per-kind template policy for the generic outbound executor
//! (P6a T6d-1): which hosts a template may name, which slots may appear and
//! where, which headers are tolerated, and the request shape of every
//! supported action kind. The data is the contract the platform connectors
//! (`bundles/rust/{twitch,discord,youtube}` READMEs, "host-config" + "slot"
//! tables) are written against; the verifier in [`super::verify`] enforces it
//! and the substituter in [`super::subst`] fills it.
//!
//! One file per platform (`twitch`, `discord`, `youtube`, `kick`): every v3.0 op
//! family is PRE-POPULATED here as an exact, fail-closed [`KindPolicy`], so a
//! platform bundle only has to implement `build-request` for a kind that already
//! exists. A kind absent from the table is an `UnknownOp` at parse time and
//! `UnsupportedKind` at execution -- never a default.

pub mod discord;
pub mod kick;
#[cfg(test)]
mod kind_tests;
pub mod twitch;
pub mod youtube;

use std::sync::LazyLock;

/// How a platform's connector spells a slot.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SlotSyntax {
    /// `{{NAME}}` (Twitch, YouTube).
    Double,
    /// `{name}` (Discord's `{target.channel_id}`).
    Single,
}

/// Who supplies a slot's value. NEVER the guest or the queue payload.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SlotOrigin {
    /// The channel proven by the signed origin stamp (`outbound_pin`).
    Stamp,
    /// The host's per-community connection / credential resolution
    /// ([`super::SlotSource`]), one call per send, never cached.
    Resolver,
    /// A [`super::DutyHook`] (T6d-2: the Discord DM channel the host opened).
    Duty,
}

/// Value shape a resolved slot must satisfy before it is substituted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SlotShape {
    /// `[A-Za-z0-9_-]{1,128}` -- numeric ids, snowflakes, YouTube ids.
    Id,
    /// Visible ASCII (`0x21..=0x7e`), 1..=4096 bytes -- a bearer/bot token.
    Secret,
    /// Any non-NUL string of 1..=256 chars. Test-only: proves the encoders
    /// neutralize hostile values even if a shape check were ever loosened.
    #[cfg(test)]
    Opaque,
}

/// Where in the template a slot may legally occur.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Position {
    /// A whole path segment.
    Path,
    /// The whole value of this query key.
    Query(&'static str),
    /// The value of this (lowercase) header.
    Header(&'static str),
    /// The whole string value at this JSON pointer in the body.
    Body(&'static str),
}

/// One declared slot.
#[derive(Clone, Debug)]
pub struct SlotDecl {
    /// Token text between the braces (`TWITCH_TOKEN`, `target.channel_id`).
    pub name: &'static str,
    /// The connector's `secret-refs` handle for this slot (`""` for a
    /// [`SlotOrigin::Stamp`] / [`SlotOrigin::Duty`] placeholder).
    pub handle: &'static str,
    /// Value source.
    pub origin: SlotOrigin,
    /// Required value shape.
    pub shape: SlotShape,
    /// Allowed positions (any one).
    pub positions: Vec<Position>,
}

/// A credential delivered through the `secret-refs` header convention
/// (`("Authorization", "discord-bot")`): the host composes `prefix + secret`
/// into this header; the template never carries it.
#[derive(Clone, Copy, Debug)]
pub struct HeaderCred {
    /// Lowercase header name.
    pub header: &'static str,
    /// Handle the connector pairs with it.
    pub handle: &'static str,
    /// Literal scheme prefix (`"Bot "`).
    pub prefix: &'static str,
}

/// One expected path segment.
#[derive(Clone, Copy, Debug)]
pub enum PathSeg {
    /// Exact literal.
    Lit(&'static str),
    /// A declared slot occupying the whole segment.
    Slot(&'static str),
    /// A literal id (`[A-Za-z0-9_-]{1,128}`) such as a message id.
    Id,
    /// A percent-encoded literal (`[A-Za-z0-9_.~%-]{1,128}`) such as a URL-encoded emoji;
    /// every `%` must start a two-hex-digit escape.
    Enc,
}

/// What the executor returns to the producer from a 2xx reply.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResultMode {
    /// Nothing (the default).
    None,
    /// The reply must carry a snowflake `id` (create-style Discord ops); the id is returned.
    CreatedSnowflake,
    /// The reply body (UTF-8 JSON, already bounded by the guarded client's response ceiling)
    /// is returned -- list/create ops whose result the producer must read.
    Body,
}

/// Handle prefix marking an APP-scoped credential slot (app token rather than the
/// community/user token). A kind whose resolver slots carry it runs as an app-scoped request:
/// the executor refuses it unless the [`super::SlotSource`] declares
/// `supports_app_credentials`, and the kind may not also use a user-scoped resolver slot.
pub const APP_HANDLE_PREFIX: &str = "app.";

/// The request shape of one action kind.
#[derive(Clone, Debug)]
pub struct KindPolicy {
    /// `chat.send`, `chat.delete`, ...
    pub kind: &'static str,
    /// Required HTTP method.
    pub method: &'static str,
    /// Expected path, segment by segment.
    pub path: Vec<PathSeg>,
    /// The EXACT query key set (all required, no extras).
    pub query: Vec<&'static str>,
    /// Slot names this kind may use.
    pub slots: Vec<&'static str>,
    /// Header-convention credentials this kind requires.
    pub header_creds: Vec<HeaderCred>,
    /// `Some(max)` = a JSON object body is required and bounded; `None` = no
    /// body is allowed.
    pub max_body: Option<usize>,
    /// Payload key the queued text is handed to the connector under.
    pub text_key: &'static str,
    /// `true` = the kind needs a [`super::DutyHook`] host duty (T6d-2); with
    /// none claiming it the executor refuses before building anything.
    pub needs_duty: bool,
    /// What a 2xx reply yields for the producer.
    pub result: ResultMode,
    /// `true` = the op acts on a channel/chat the stamp must prove (moderation, message ops):
    /// a generic op with no `channel`/`origin_channel` is refused before anything is built.
    pub requires_pin: bool,
    /// `true` = the body must be a JSON ARRAY of objects (bulk-overwrite endpoints) instead
    /// of an object.
    pub array_body: bool,
    /// Non-empty = the EXACT set of top-level body keys the template may carry (anything else
    /// is refused). Empty = no key restriction (legacy kinds).
    pub body_keys: Vec<&'static str>,
}

impl KindPolicy {
    /// A kind with no query, no slots, no body, no credential-by-header, no duty and the
    /// default result/pin settings; callers set what the endpoint needs with struct-update.
    #[must_use]
    pub fn new(kind: &'static str, method: &'static str, path: Vec<PathSeg>) -> Self {
        Self {
            kind,
            method,
            path,
            query: vec![],
            slots: vec![],
            header_creds: vec![],
            max_body: None,
            text_key: "text",
            needs_duty: false,
            result: ResultMode::None,
            requires_pin: false,
            array_body: false,
            body_keys: vec![],
        }
    }
}

/// One platform's full template policy.
#[derive(Clone, Debug)]
pub struct PlatformPolicy {
    /// `twitch` / `discord` / `youtube` / `kick`.
    pub platform: &'static str,
    /// Slot grammar.
    pub syntax: SlotSyntax,
    /// Exact lowercase authorities a URL may name (the manifest's declared
    /// egress hosts; no port in production).
    pub hosts: Vec<String>,
    /// Declared slots.
    pub slots: Vec<SlotDecl>,
    /// Supported kinds.
    pub kinds: Vec<KindPolicy>,
    /// Literal prefixes an `Authorization` header value may carry before its
    /// credential slot.
    pub auth_prefixes: Vec<&'static str>,
    /// Non-credential header names a template may set (lowercase).
    pub allowed_headers: Vec<&'static str>,
    /// Test seam: permit `http://` (local fake servers). Always `false`
    /// from the production constructors.
    pub allow_http: bool,
}

pub(super) fn resolver_slot(
    name: &'static str,
    handle: &'static str,
    shape: SlotShape,
    positions: Vec<Position>,
) -> SlotDecl {
    SlotDecl {
        name,
        handle,
        origin: SlotOrigin::Resolver,
        shape,
        positions,
    }
}

/// A slot the [`super::DutyHook`] fills (never the guest, never the payload).
pub(super) fn duty_slot(name: &'static str) -> SlotDecl {
    SlotDecl {
        name,
        handle: "",
        origin: SlotOrigin::Duty,
        shape: SlotShape::Id,
        positions: vec![Position::Path],
    }
}

impl PlatformPolicy {
    /// Index of the slot declared as `name`.
    pub(crate) fn slot_index(&self, name: &str) -> Option<usize> {
        self.slots.iter().position(|s| s.name == name)
    }

    /// The kind policy for `kind`.
    #[must_use]
    pub fn kind(&self, kind: &str) -> Option<&KindPolicy> {
        self.kinds.iter().find(|k| k.kind == kind)
    }

    /// Restricts the allowed hosts to those ALSO declared by the connector
    /// manifest's egress list (intersection; never widens).
    #[must_use]
    pub fn with_manifest_hosts(mut self, declared: &[&str]) -> Self {
        self.hosts
            .retain(|h| declared.iter().any(|d| d.eq_ignore_ascii_case(h)));
        self
    }

    /// All four production policies (Kick is declared but carries no kinds yet).
    #[must_use]
    pub fn production_set() -> Vec<Self> {
        vec![
            Self::twitch(),
            Self::discord(),
            Self::youtube(),
            Self::kick(),
        ]
    }
}

impl SlotDecl {
    /// `true` for an app-scoped credential slot (see [`APP_HANDLE_PREFIX`]).
    #[must_use]
    pub fn is_app_scoped(&self) -> bool {
        self.origin == SlotOrigin::Resolver && self.handle.starts_with(APP_HANDLE_PREFIX)
    }
}

impl KindPolicy {
    /// `true` when any resolver slot this kind may use is app-scoped.
    #[must_use]
    pub fn is_app_scoped(&self, policy: &PlatformPolicy) -> bool {
        self.slots
            .iter()
            .filter_map(|n| policy.slot_index(n))
            .any(|i| policy.slots[i].is_app_scoped())
    }
}

static PRODUCTION_TABLE: LazyLock<Vec<PlatformPolicy>> =
    LazyLock::new(PlatformPolicy::production_set);

/// The static op spelling registered for `platform` in the production policy table, or `None`
/// when the platform or op is not in it. This is the single table lookup the queue parser and
/// the producer's `RelayOp` accept an op string through -- there is no second, closed list.
#[must_use]
pub fn registered_op(platform: &str, op: &str) -> Option<&'static str> {
    PRODUCTION_TABLE
        .iter()
        .find(|p| p.platform == platform)
        .and_then(|p| p.kinds.iter().find(|k| k.kind == op))
        .map(|k| k.kind)
}

/// Every op spelling in the production table (bounded metric-label universe).
#[must_use]
pub fn all_registered_ops() -> Vec<&'static str> {
    let mut v: Vec<&'static str> = PRODUCTION_TABLE
        .iter()
        .flat_map(|p| p.kinds.iter().map(|k| k.kind))
        .collect();
    v.sort_unstable();
    v.dedup();
    v
}

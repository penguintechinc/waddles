//! Per-platform, per-kind template policy for the generic outbound executor
//! (P6a T6d-1): which hosts a template may name, which slots may appear and
//! where, which headers are tolerated, and the request shape of every
//! supported action kind. The data is the contract the platform connectors
//! (`bundles/rust/{twitch,discord,youtube}` READMEs, "host-config" + "slot"
//! tables) are written against; the verifier in [`super::verify`] enforces it
//! and the substituter in [`super::subst`] fills it.

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
}

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
}

/// One platform's full template policy.
#[derive(Clone, Debug)]
pub struct PlatformPolicy {
    /// `twitch` / `discord` / `youtube`.
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

fn resolver_slot(
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
fn duty_slot(name: &'static str) -> SlotDecl {
    SlotDecl {
        name,
        handle: "",
        origin: SlotOrigin::Duty,
        shape: SlotShape::Id,
        positions: vec![Position::Path],
    }
}

/// Discord `PUT|DELETE /guilds/{guild}/members/{user}/roles/{role}`: every
/// path slot is host-filled and the request has no body.
fn role_kind(kind: &'static str, method: &'static str, cred: HeaderCred) -> KindPolicy {
    KindPolicy {
        kind,
        method,
        path: vec![
            PathSeg::Lit("api"),
            PathSeg::Lit("v10"),
            PathSeg::Lit("guilds"),
            PathSeg::Slot("target.guild_id"),
            PathSeg::Lit("members"),
            PathSeg::Slot("target.user"),
            PathSeg::Lit("roles"),
            PathSeg::Slot("target.role"),
        ],
        query: vec![],
        slots: vec!["target.guild_id", "target.user", "target.role"],
        header_creds: vec![cred],
        max_body: None,
        text_key: "content",
        needs_duty: true,
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

    /// Twitch Helix policy (connector README "Slot set for T6d").
    #[must_use]
    pub fn twitch() -> Self {
        let all = vec![
            "TWITCH_TOKEN",
            "TWITCH_CLIENT_ID",
            "TWITCH_BROADCASTER_ID",
            "TWITCH_SENDER_ID",
        ];
        let helix = |rest: &[&'static str]| {
            let mut p = vec![PathSeg::Lit("helix")];
            p.extend(rest.iter().map(|s| PathSeg::Lit(s)));
            p
        };
        Self {
            platform: "twitch",
            syntax: SlotSyntax::Double,
            hosts: vec!["api.twitch.tv".to_string()],
            slots: vec![
                resolver_slot(
                    "TWITCH_TOKEN",
                    "token",
                    SlotShape::Secret,
                    vec![Position::Header("authorization")],
                ),
                resolver_slot(
                    "TWITCH_CLIENT_ID",
                    "client_id",
                    SlotShape::Id,
                    vec![Position::Header("client-id")],
                ),
                resolver_slot(
                    "TWITCH_BROADCASTER_ID",
                    "broadcaster_id",
                    SlotShape::Id,
                    vec![
                        Position::Body("/broadcaster_id"),
                        Position::Query("broadcaster_id"),
                    ],
                ),
                resolver_slot(
                    "TWITCH_SENDER_ID",
                    "sender_id",
                    SlotShape::Id,
                    vec![
                        Position::Body("/sender_id"),
                        Position::Query("moderator_id"),
                    ],
                ),
            ],
            kinds: vec![
                KindPolicy {
                    kind: "chat.send",
                    method: "POST",
                    path: helix(&["chat", "messages"]),
                    query: vec![],
                    slots: all.clone(),
                    header_creds: vec![],
                    max_body: Some(4096),
                    text_key: "text",
                    needs_duty: false,
                },
                KindPolicy {
                    kind: "chat.delete",
                    method: "DELETE",
                    path: helix(&["moderation", "chat"]),
                    query: vec!["broadcaster_id", "moderator_id", "message_id"],
                    slots: all,
                    header_creds: vec![],
                    max_body: None,
                    text_key: "text",
                    needs_duty: false,
                },
                KindPolicy {
                    kind: "dm.send",
                    method: "POST",
                    path: helix(&["whispers"]),
                    query: vec!["from_user_id", "to_user_id"],
                    slots: vec!["TWITCH_TOKEN", "TWITCH_CLIENT_ID", "TWITCH_SENDER_ID"],
                    header_creds: vec![],
                    max_body: Some(4096),
                    text_key: "text",
                    needs_duty: true,
                },
            ],
            auth_prefixes: vec!["Bearer "],
            allowed_headers: vec![
                "authorization",
                "client-id",
                "content-type",
                "accept",
                "user-agent",
            ],
            allow_http: false,
        }
    }

    /// Discord REST policy (connector README "Outbound (T6d)"). Only
    /// `{target.channel_id}` is a stamp slot; `dm.send` needs a host duty
    /// and `role.*` are host duties (`discord_duties`): their
    /// placeholders are normalized by the hook, then filled by it.
    #[must_use]
    pub fn discord() -> Self {
        let cred = HeaderCred {
            header: "authorization",
            handle: "discord-bot",
            prefix: "Bot ",
        };
        let channel_messages = |tail: Option<PathSeg>| {
            let mut p = vec![
                PathSeg::Lit("api"),
                PathSeg::Lit("v10"),
                PathSeg::Lit("channels"),
                PathSeg::Slot("target.channel_id"),
                PathSeg::Lit("messages"),
            ];
            p.extend(tail);
            p
        };
        Self {
            platform: "discord",
            syntax: SlotSyntax::Single,
            hosts: vec!["discord.com".to_string()],
            slots: vec![
                SlotDecl {
                    name: "target.channel_id",
                    handle: "",
                    origin: SlotOrigin::Stamp,
                    shape: SlotShape::Id,
                    positions: vec![Position::Path],
                },
                duty_slot("dm.channel_id"),
                duty_slot("target.guild_id"),
                duty_slot("target.user"),
                duty_slot("target.role"),
            ],
            kinds: vec![
                KindPolicy {
                    kind: "chat.send",
                    method: "POST",
                    path: channel_messages(None),
                    query: vec![],
                    slots: vec!["target.channel_id"],
                    header_creds: vec![cred],
                    max_body: Some(16_384),
                    text_key: "content",
                    needs_duty: false,
                },
                KindPolicy {
                    kind: "chat.delete",
                    method: "DELETE",
                    path: channel_messages(Some(PathSeg::Id)),
                    query: vec![],
                    slots: vec!["target.channel_id"],
                    header_creds: vec![cred],
                    max_body: None,
                    text_key: "content",
                    needs_duty: false,
                },
                KindPolicy {
                    kind: "dm.send",
                    method: "POST",
                    path: vec![
                        PathSeg::Lit("api"),
                        PathSeg::Lit("v10"),
                        PathSeg::Lit("channels"),
                        PathSeg::Slot("dm.channel_id"),
                        PathSeg::Lit("messages"),
                    ],
                    query: vec![],
                    slots: vec!["dm.channel_id"],
                    header_creds: vec![cred],
                    max_body: Some(16_384),
                    text_key: "content",
                    needs_duty: true,
                },
                role_kind("role.add", "PUT", cred),
                role_kind("role.remove", "DELETE", cred),
            ],
            auth_prefixes: vec![],
            allowed_headers: vec!["content-type", "accept", "user-agent"],
            allow_http: false,
        }
    }

    /// YouTube Data API policy (connector README "Host config"): `chat.send`
    /// only; the channel/video id slots are declared for the read path and
    /// are not permitted in any outbound kind.
    #[must_use]
    pub fn youtube() -> Self {
        Self {
            platform: "youtube",
            syntax: SlotSyntax::Double,
            hosts: vec!["www.googleapis.com".to_string()],
            slots: vec![
                resolver_slot(
                    "YT_OAUTH_TOKEN",
                    "youtube.oauth",
                    SlotShape::Secret,
                    vec![Position::Header("authorization")],
                ),
                resolver_slot(
                    "YT_LIVE_CHAT_ID",
                    "youtube.live_chat_id",
                    SlotShape::Id,
                    vec![
                        Position::Body("/snippet/liveChatId"),
                        Position::Query("liveChatId"),
                    ],
                ),
                resolver_slot(
                    "YT_CHANNEL_ID",
                    "youtube.channel_id",
                    SlotShape::Id,
                    vec![Position::Query("channelId")],
                ),
                resolver_slot(
                    "YT_VIDEO_ID",
                    "youtube.video_id",
                    SlotShape::Id,
                    vec![Position::Query("id")],
                ),
            ],
            kinds: vec![KindPolicy {
                kind: "chat.send",
                method: "POST",
                path: vec![
                    PathSeg::Lit("youtube"),
                    PathSeg::Lit("v3"),
                    PathSeg::Lit("liveChat"),
                    PathSeg::Lit("messages"),
                ],
                query: vec!["part"],
                slots: vec!["YT_OAUTH_TOKEN", "YT_LIVE_CHAT_ID"],
                header_creds: vec![],
                max_body: Some(4096),
                text_key: "text",
                needs_duty: false,
            }],
            auth_prefixes: vec!["Bearer "],
            allowed_headers: vec!["authorization", "content-type", "accept", "user-agent"],
            allow_http: false,
        }
    }

    /// All three production policies.
    #[must_use]
    pub fn production_set() -> Vec<Self> {
        vec![Self::twitch(), Self::discord(), Self::youtube()]
    }
}

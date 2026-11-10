//! YouTube Data API v3 template policy: live-chat send, message delete, bans and moderators.
//! Endpoints verified against <https://developers.google.com/youtube/v3/live/docs>; the
//! `liveChatId` is the host-resolved slot (the community's connection), never bundle input.

use super::{
    resolver_slot, KindPolicy, PathSeg, PlatformPolicy, Position, ResultMode, SlotShape, SlotSyntax,
};

/// `/youtube/v3/liveChat/<resource>`.
fn live_chat(resource: &'static str) -> Vec<PathSeg> {
    vec![
        PathSeg::Lit("youtube"),
        PathSeg::Lit("v3"),
        PathSeg::Lit("liveChat"),
        PathSeg::Lit(resource),
    ]
}

/// An `insert` kind (`POST ...?part=snippet`, body `{"snippet": {...}}`): the body may carry
/// only the `snippet` key; the live chat id inside it is the host slot.
fn insert_kind(kind: &'static str, resource: &'static str, result: ResultMode) -> KindPolicy {
    KindPolicy {
        query: vec!["part"],
        slots: vec!["YT_OAUTH_TOKEN", "YT_LIVE_CHAT_ID"],
        max_body: Some(4096),
        body_keys: vec!["snippet"],
        requires_pin: true,
        result,
        ..KindPolicy::new(kind, "POST", live_chat(resource))
    }
}

/// A `delete` kind (`DELETE ...?id=<resource id>`): no body; the id is a literal the
/// verifier restricts to a safe charset.
fn delete_kind(kind: &'static str, resource: &'static str) -> KindPolicy {
    KindPolicy {
        query: vec!["id"],
        slots: vec!["YT_OAUTH_TOKEN"],
        requires_pin: true,
        ..KindPolicy::new(kind, "DELETE", live_chat(resource))
    }
}

impl PlatformPolicy {
    /// YouTube Data API policy (connector README "Host config"). The channel/video id slots
    /// are declared for the read path and are not permitted in any outbound kind.
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
            kinds: vec![
                // https://developers.google.com/youtube/v3/live/docs/liveChatMessages/insert
                KindPolicy {
                    query: vec!["part"],
                    slots: vec!["YT_OAUTH_TOKEN", "YT_LIVE_CHAT_ID"],
                    max_body: Some(4096),
                    ..KindPolicy::new("chat.send", "POST", live_chat("messages"))
                },
                // https://developers.google.com/youtube/v3/live/docs/liveChatMessages/delete
                delete_kind("chat.delete", "messages"),
                // https://developers.google.com/youtube/v3/live/docs/liveChatBans/insert
                // (the reply carries the ban resource id `mod.unban` needs).
                insert_kind("mod.ban", "bans", ResultMode::Body),
                // https://developers.google.com/youtube/v3/live/docs/liveChatBans/delete
                delete_kind("mod.unban", "bans"),
                // https://developers.google.com/youtube/v3/live/docs/liveChatModerators/insert
                insert_kind("mod.add", "moderators", ResultMode::Body),
                // https://developers.google.com/youtube/v3/live/docs/liveChatModerators/delete
                delete_kind("mod.remove", "moderators"),
            ],
            auth_prefixes: vec!["Bearer "],
            allowed_headers: vec!["authorization", "content-type", "accept", "user-agent"],
            allow_http: false,
        }
    }
}

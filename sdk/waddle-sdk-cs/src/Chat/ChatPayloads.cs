using System.Text.Json.Serialization;

namespace WaddleSdk.Chat;

/// <summary>
/// Inbound `chat.message` payload shape (spec
/// `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS6.1.1) --
/// the JSON `PlatformEventInfo.PayloadJson` carries for a chat event.
/// </summary>
public sealed record ChatMessagePayload(
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("channel_id")] string? ChannelId);

/// <summary>
/// Outbound reply payload: the shape a `process-stage.transform` writes back
/// onto the rewritten event's `payload-json` for `action-stage.dispatch` to
/// read, mirroring every Tier-1 SDK sibling's ping/pong bundle.
/// </summary>
public sealed record ChatReplyPayload(
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("channel_id")] string? ChannelId);

/// <summary>
/// The `{channel, text}` shape the provider-scoped outbound relay transport
/// (e.g. `waddle_transports.transports.irc_relay.RelayOutboundIrcTransport.send`
/// for Twitch) LPUSHes onto its queue -- the wire contract `action-stage.dispatch`
/// must match exactly for svc-ingest's drain loop to deliver the reply.
/// </summary>
public sealed record RelayMessagePayload(
    [property: JsonPropertyName("channel")] string Channel,
    [property: JsonPropertyName("text")] string Text);

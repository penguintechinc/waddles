using WaddleSdk.Chat;
using WaddleSdk.Json;

namespace WaddleSdk.Relay;

/// <summary>
/// Convenience helper for the common `action-stage.dispatch` shape: read a
/// `{channel, text}` reply back off the inbound event and push it to the
/// event's own origin platform via <see cref="IRelayClient.Push"/> -- never a
/// hardcoded provider constant (`bundles/rust/ping`'s own regression contract:
/// a Discord-origin command's reply must relay to Discord, not a fixed platform).
/// </summary>
public static class ReplyHelper
{
    /// <summary>Builds the `{channel, text}` relay message JSON and pushes it to
    /// <paramref name="provider"/> (always the inbound event's own `platform`).</summary>
    public static void SendReply(IRelayClient relay, string provider, string channel, string text)
    {
        var payload = new RelayMessagePayload(channel, text);
        var json = System.Text.Json.JsonSerializer.Serialize(payload, WaddleSdkJsonContext.Default.RelayMessagePayload);
        relay.Push(provider, json);
    }
}

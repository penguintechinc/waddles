using System.Text.Json;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinTimers.Tests;

/// <summary>Test helpers: chat events with role flags, and a runner that returns the reply text.</summary>
public static class TestKit
{
    /// <summary>Builds a chat.message event whose payload carries the role flags the host would stamp.</summary>
    public static PlatformEventInfo Chat(string text, bool mod = false, bool broadcaster = false, string platform = "twitch") =>
        new(platform, "chat.message", "platform-actor",
            $"{{\"text\":{JsonSerializer.Serialize(text)},\"channel_id\":\"chan-1\",\"is_mod\":{(mod ? "true" : "false")},\"is_broadcaster\":{(broadcaster ? "true" : "false")}}}",
            "2026-11-07T00:00:00.000Z");

    /// <summary>Runs one chat event as <paramref name="actor"/>; returns the reply text or null when silent.</summary>
    public static string? Say(this World w, WaddleProcessStageNext logic, string actor, string text, string messageId, bool mod = false, bool broadcaster = false, string platform = "twitch")
    {
        var reply = logic.Run(Chat(text, mod, broadcaster, platform), w.HostFor(actor, messageId));
        return reply is null ? null : World.TextOf(reply);
    }
}

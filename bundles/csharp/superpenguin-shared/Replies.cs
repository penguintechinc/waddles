using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace SuperPenguinShared;

/// <summary>Small helpers every port repeats: build the chat reply event, resolve the currency alias.</summary>
public static class Replies
{
    /// <summary>The inbound event re-emitted with a chat reply payload for the action stage to relay.</summary>
    public static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>The community currency alias from the economy service; falls back to its documented default (logged) if the tool cannot answer.</summary>
    public static string Alias(IWaddleHostNext host, string actor, string logPrefix)
    {
        try
        {
            var alias = EconomyServiceTools.PointsView(host.Tool, actor).Alias;
            if (!string.IsNullOrWhiteSpace(alias))
            {
                return alias;
            }
        }
        catch (WaddleToolException ex)
        {
            host.Log.Warn(logPrefix + ".alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }
}

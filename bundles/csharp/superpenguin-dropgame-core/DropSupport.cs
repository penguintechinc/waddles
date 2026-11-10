using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace SuperPenguin.DropGameCore;

/// <summary>
/// Small helpers shared by every drop game, usable on their own by games that do not use <see cref="WageredGameBase"/>
/// (Racing's join window, UfoClaw's control loop): the chat reply event and the community currency alias.
/// </summary>
public static class DropSupport
{
    /// <summary>The chat command prefix every ported game answers to.</summary>
    public const string CommandPrefix = "!";

    /// <summary>Builds the reply event (a chat reply payload into the event's own channel) that the action stage relays to the origin platform.</summary>
    public static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>The community currency alias from the economy service; falls back to its documented default (logged at warn) if the tool cannot answer.</summary>
    public static string Alias(IWaddleHostNext host, string actor, string game)
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
            host.Log.Warn($"{game}.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }
}

using WaddleSdk.Chat;
using WaddleSdk.Json;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleCsping;

/// <summary>
/// `!csping` -&gt; `pong (c#)` business logic, built on `waddle-sdk-cs`
/// (<see cref="WaddleProcessStage"/>) instead of this bundle's original
/// hand-rolled `System.Text.Json.Nodes` parsing. C# equivalent of
/// `bundles/rust/ping`/`bundles/python/pyping`'s process-stage half: matches
/// the exact command `!csping` in an inbound `chat.message`'s payload and
/// rewrites the event into a `pong (c#)` reply on the same platform/channel.
/// Non-matching or non-command payloads produce no reply (null) rather than
/// throwing -- this bundle must never fail the pipeline over an event it was
/// never meant to react to, same contract as every Tier-1 SDK sibling.
/// </summary>
public sealed class CspingLogic : WaddleProcessStage
{
    /// <summary>The exact command name this bundle reacts to (matched after the `!` prefix).</summary>
    internal const string CommandName = "csping";

    /// <summary>The reply body sent back for a matching command.</summary>
    internal const string PongReply = "pong (c#)";

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHost host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.Is(CommandName))
        {
            return null;
        }

        return PlatformEventInfo.WithPayload(
            @event,
            new ChatReplyPayload(PongReply, command.ChannelId),
            WaddleSdkJsonContext.Default.ChatReplyPayload);
    }
}

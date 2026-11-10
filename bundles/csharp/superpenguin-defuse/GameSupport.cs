using WaddleSdk.Chat;
using WaddleSdk.Context;
using WaddleSdk.Economy;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinDefuse;

/// <summary>
/// Helpers shared by this bundle's logic: the replay key (event identity), the currency alias lookup and the chat reply
/// builder. Kept in the bundle (not the SDK) because wit-bindgen's per-project codegen makes each bundle self-contained.
/// </summary>
internal static class GameSupport
{
    /// <summary>Unit separator between the replay-key parts: a character no platform id or command name can contain.</summary>
    private const char Sep = '\u001f';

    /// <summary>
    /// Refuses loudly when the invocation carries no message id: the id is the only thing that makes a redelivery
    /// distinguishable from a fresh command, so without it the replay/idempotency guarantees cannot hold.
    /// </summary>
    internal static void RequireMessageId(BundleContextInfo context)
    {
        if (string.IsNullOrWhiteSpace(context.MessageId))
        {
            throw new InvalidOperationException("invocation has no message id; refusing to run a money game without an idempotency key");
        }
    }

    /// <summary>
    /// The state key of the per-event outcome record: a hash over message id AND platform AND actor uuid AND command, so the
    /// same message id replayed by a different actor, platform or command never reads another delivery's recorded outcome.
    /// </summary>
    internal static string ReplayKey(string prefix, BundleContextInfo context, string platform, string actor, string command)
    {
        RequireMessageId(context);
        return StateKey.Of(prefix, StateKey.Hash(string.Join(Sep, context.MessageId, platform, actor, command)));
    }

    /// <summary>The community currency alias from the economy service; falls back to its documented default (logged) if the tool cannot answer.</summary>
    internal static string Alias(IWaddleHostNext host, string actor, string logPrefix)
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

    /// <summary>Wraps <paramref name="text"/> as the chat reply payload for the event's own channel.</summary>
    internal static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>Thousands-separated integer, the original's `ToString("N0")`.</summary>
    internal static string N0(long value) => value.ToString("N0", System.Globalization.CultureInfo.InvariantCulture);
}

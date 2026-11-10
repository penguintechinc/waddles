using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinDuel;

/// <summary>
/// Builds the per-event replay key for a money command. The key binds the host message id, the platform, the resolved actor
/// UUID and the command, so a record created for one invocation can never be mistaken for another's. A blank message id is
/// refused loudly: without it every event would collide on one key and the first duel would be "replayed" for everyone.
/// </summary>
internal static class ReplayGuard
{
    /// <summary>The state key for this invocation; throws when the host supplied no message id.</summary>
    internal static string KeyFor(string prefix, PlatformEventInfo @event, IWaddleHostNext host, string actor, string command)
    {
        var messageId = host.Context.MessageId;
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new InvalidOperationException("the host supplied no message id; refusing to run a money command without a replay key");
        }

        return StateKey.Of(prefix, StateKey.Hash($"{messageId}|{@event.Platform}|{actor}|{command}"));
    }
}

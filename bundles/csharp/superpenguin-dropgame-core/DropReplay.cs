using System.Text.Json.Serialization;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace SuperPenguin.DropGameCore;

/// <summary>
/// One settled-or-settling play, recorded in `kv` under the invocation's replay key BEFORE the single `wager` call so a
/// redelivered event re-runs the simulation from the very same <see cref="Seed"/> and reprints the same result. <see cref="Actor"/>,
/// <see cref="Command"/> and <see cref="ArgsHash"/> bind the record to the invocation that created it (it is only reused on an
/// exact match); <see cref="Config"/> names the game-config revision the seed must be replayed under; <see cref="Extra"/> is an
/// opaque slot for a game that needs more than a seed (never PII).
/// </summary>
public sealed record DropReplayRecord(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("args")] string ArgsHash,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("cfg")] string Config,
    [property: JsonPropertyName("extra")] string? Extra = null);

/// <summary>The core library's source-generated JSON context (a library, like a bundle, cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(DropReplayRecord))]
public sealed partial class DropCoreJsonContext : JsonSerializerContext;

/// <summary>
/// Builds the per-event replay key for a drop game. The key binds the host message id, the platform, the resolved actor UUID and
/// the command, so a record made for one invocation can never be mistaken for another's. A blank message id is refused loudly:
/// without it every event would collide on one key and the first play would be "replayed" for everyone.
/// </summary>
public static class DropReplayGuard
{
    /// <summary>The state key for this invocation of <paramref name="game"/>; throws when the host supplied no message id.</summary>
    public static string KeyFor(string game, PlatformEventInfo @event, IWaddleHostNext host, string actor, string command)
    {
        var messageId = host.Context.MessageId;
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new InvalidOperationException("the host supplied no message id; refusing to run a money command without a replay key");
        }

        return StateKey.Of("dg", game, "out", StateKey.Hash($"{messageId}|{@event.Platform}|{actor}|{command}"));
    }

    /// <summary>
    /// The recorded play for this invocation, or null when none exists or it belongs to a different invocation (actor, command or
    /// raw-argument hash mismatch: that is a FRESH request and must go through full validation, cooldown and a new seed).
    /// </summary>
    public static DropReplayRecord? TryReplay(IStateStore store, string key, string actor, string command, string argsHash)
    {
        var recorded = store.GetJson(key, DropCoreJsonContext.Default.DropReplayRecord);
        return recorded is not null && recorded.Actor == actor && recorded.Command == command && recorded.ArgsHash == argsHash
            ? recorded
            : null;
    }
}

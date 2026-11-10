using System.Text.Json.Serialization.Metadata;

namespace WaddleSdk.State;

/// <summary>
/// Safe replay pattern for at-least-once delivery: anything random or decided once (dice, a winner, a stake) is recorded BEFORE the
/// money call and reused when the SAME delivery is replayed. The record is bound to (message id, platform, actor, command): a blank
/// message id is refused loudly, and the key hashes all four length-prefixed parts, so another actor, command or platform can never
/// pick up someone else's outcome (which would skip validation and cooldown and move money). Callers must additionally verify the
/// stored record's own binding fields via <c>matches</c> in <see cref="TryReplay{T}"/> (defense in depth against a key collision).
/// </summary>
public sealed class IdempotentOutcome(IStateStore store, string name, uint ttlSeconds = 3600)
{
    /// <summary>Builds the host-safe key; throws <see cref="ArgumentException"/> for a blank message id (a shared/empty id would alias unrelated events).</summary>
    public string KeyFor(string messageId, string platform, string actor, string command)
    {
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new ArgumentException("message id is blank; refusing to derive an idempotency key", nameof(messageId));
        }

        string[] parts = [messageId, platform, actor, command];
        var bound = string.Concat(parts.Select(p => p.Length + ":" + p + "|"));
        return StateKey.Of("out", name, StateKey.Hash(bound));
    }

    /// <summary>The recorded outcome for exactly this delivery, or null. A record whose own binding fails <paramref name="matches"/> is NOT a replay.</summary>
    public T? TryReplay<T>(string messageId, string platform, string actor, string command, JsonTypeInfo<T> typeInfo, Func<T, bool> matches)
        where T : class
    {
        var recorded = store.GetJson(KeyFor(messageId, platform, actor, command), typeInfo);
        return recorded is not null && matches(recorded) ? recorded : null;
    }

    /// <summary>Records the outcome (call BEFORE the money call).</summary>
    public void Record<T>(string messageId, string platform, string actor, string command, T outcome, JsonTypeInfo<T> typeInfo) =>
        store.SetJson(KeyFor(messageId, platform, actor, command), outcome, typeInfo, ttlSeconds);

    /// <summary>Discards the record (the settlement was refused and must not replay).</summary>
    public void Discard(string messageId, string platform, string actor, string command) =>
        store.Delete(KeyFor(messageId, platform, actor, command));
}

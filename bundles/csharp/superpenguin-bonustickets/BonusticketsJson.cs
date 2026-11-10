using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinBonustickets;

/// <summary>One `!bonus` claim recorded under the event's replay key before the credit, so a redelivery re-sends the same credit.</summary>
public sealed record BonusOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("award")] long Award);

/// <summary>The accumulated `!addactive` amount waiting for its quiet period; `Seq` numbers settlements.</summary>
public sealed record BonusPool(
    [property: JsonPropertyName("amount")] long Amount,
    [property: JsonPropertyName("last")] long LastAddMillis,
    [property: JsonPropertyName("seq")] long Seq);

/// <summary>A frozen payout: every recipient receives `Each`; `Next` is the index of the first recipient not yet paid.</summary>
public sealed record BonusSettlement(
    [property: JsonPropertyName("id")] long Id,
    [property: JsonPropertyName("each")] long Each,
    [property: JsonPropertyName("who")] List<string> Recipients,
    [property: JsonPropertyName("next")] int Next);

/// <summary>The recipient slice one event pays, recorded before any credit so a redelivery replays exactly that slice.</summary>
public sealed record BonusChunk(
    [property: JsonPropertyName("id")] long SettlementId,
    [property: JsonPropertyName("each")] long Each,
    [property: JsonPropertyName("who")] List<string> Recipients,
    [property: JsonPropertyName("first")] bool Started,
    [property: JsonPropertyName("total")] int Total);

/// <summary>When one active chatter was last seen.</summary>
public sealed record RosterEntry(
    [property: JsonPropertyName("a")] string Actor,
    [property: JsonPropertyName("t")] long SeenMillis);

/// <summary>One of the sixteen roster shards (keyed by the first hex digit of the actor hash).</summary>
public sealed record RosterShard([property: JsonPropertyName("e")] List<RosterEntry> Entries);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(BonusOutcome))]
[JsonSerializable(typeof(BonusPool))]
[JsonSerializable(typeof(BonusSettlement))]
[JsonSerializable(typeof(BonusChunk))]
[JsonSerializable(typeof(RosterShard))]
public sealed partial class BonusticketsJsonContext : JsonSerializerContext;

using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinRoulette;

/// <summary>
/// One spin, recorded in `kv` under the event's replay key before the single `wager` call so a redelivered event reprints the
/// original outcome. <see cref="Actor"/>, <see cref="Command"/> and <see cref="ArgsHash"/> bind the record to the exact
/// invocation that created it: it is only reused as a replay when all three match.
/// </summary>
public sealed record RouletteOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("args")] string ArgsHash,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("value")] int Value,
    [property: JsonPropertyName("total")] ulong DayTotal);

/// <summary>This bundle's own source-generated JSON context (a bundle cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(RouletteOutcome))]
public sealed partial class RouletteJsonContext : JsonSerializerContext;

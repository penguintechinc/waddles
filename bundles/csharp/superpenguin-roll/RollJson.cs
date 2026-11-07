using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinRoll;

/// <summary>
/// The dice, stake and chosen message indices of one roll, recorded in `kv` under the event's message id before the
/// `wager` call so a redelivered event reprints the original outcome instead of re-rolling.
/// </summary>
public sealed record RollOutcome(
    [property: JsonPropertyName("d1")] int Dice1,
    [property: JsonPropertyName("d2")] int Dice2,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("win_msg")] int WinMessage,
    [property: JsonPropertyName("lose_msg")] int LoseMessage,
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command);

/// <summary>This bundle's own source-generated JSON context (a bundle cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(RollOutcome))]
public sealed partial class RollJsonContext : JsonSerializerContext;

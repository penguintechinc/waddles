using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinFirst;

/// <summary>
/// The position and award of one `!first` claim, recorded before the single `economy.credit` so a redelivered event re-sends
/// the identical credit. Bound to the actor UUID and command; reused as a replay only on an exact match.
/// </summary>
public sealed record FirstOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("pos")] long Position,
    [property: JsonPropertyName("award")] long Award);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(FirstOutcome))]
public sealed partial class FirstJsonContext : JsonSerializerContext;

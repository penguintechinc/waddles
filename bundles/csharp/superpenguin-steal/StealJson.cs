using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinSteal;

/// <summary>
/// The outcome of one steal attempt, decided and recorded BEFORE the money call (roll, amount and direction included) and reused
/// verbatim on a redelivery. <see cref="From"/> pays <see cref="To"/> exactly <see cref="Amount"/> through the economy
/// `transfer_from` tool; <see cref="Kind"/> is `success`, `failed` or `poor`.
/// </summary>
public sealed record StealRecord(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("args")] string ArgsHash,
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("from")] string From,
    [property: JsonPropertyName("to")] string To,
    [property: JsonPropertyName("amt")] ulong Amount,
    [property: JsonPropertyName("thief")] string Thief,
    [property: JsonPropertyName("victim")] string Victim,
    [property: JsonPropertyName("reply")] string Reply);

/// <summary>The part of the bundle config this app reads: UUIDs that can never be stolen from (e.g. moderators).</summary>
public sealed record StealConfig([property: JsonPropertyName("protected_users")] List<string>? ProtectedUsers);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(StealRecord))]
[JsonSerializable(typeof(StealConfig))]
public sealed partial class StealJsonContext : JsonSerializerContext;

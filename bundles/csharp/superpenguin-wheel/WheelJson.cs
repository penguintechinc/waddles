using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinWheel;

/// <summary>One slice of a defined wheel: its chat-safe label and integer weight (relative odds).</summary>
public sealed record WheelItem(
    [property: JsonPropertyName("l")] string Label,
    [property: JsonPropertyName("w")] int Weight);

/// <summary>A named wheel a moderator defined, with its optional winning-message template (`{label}` is replaced).</summary>
public sealed record WheelDef(
    [property: JsonPropertyName("n")] string Name,
    [property: JsonPropertyName("i")] List<WheelItem> Items,
    [property: JsonPropertyName("m")] string? Message);

/// <summary>All wheels of the community (one kv document).</summary>
public sealed record WheelDefs([property: JsonPropertyName("w")] List<WheelDef> Wheels);

/// <summary>
/// The wheel currently shown on the overlay. `Winning` is -1 until a spin; once set it was chosen and recorded by this bundle
/// BEFORE the overlay was told to spin. `Who` holds the entrant user uuids of a viewer (name) wheel, parallel to `Labels`.
/// </summary>
public sealed record CurrentWheel(
    [property: JsonPropertyName("n")] string Name,
    [property: JsonPropertyName("l")] List<string> Labels,
    [property: JsonPropertyName("w")] List<int> Weights,
    [property: JsonPropertyName("u")] List<string> Who,
    [property: JsonPropertyName("m")] string? Message,
    [property: JsonPropertyName("v")] bool IsNames,
    [property: JsonPropertyName("x")] int Winning,
    [property: JsonPropertyName("t")] long SpunAtMillis,
    [property: JsonPropertyName("d")] int DurationMillis,
    [property: JsonPropertyName("s")] string SpinId);

/// <summary>The viewer-wheel signup: open flag and the entrant user uuids in join order.</summary>
public sealed record NameWheel(
    [property: JsonPropertyName("o")] bool Open,
    [property: JsonPropertyName("e")] List<string> Entrants);

/// <summary>A `!wheel spin` recorded under the event's replay key: the chosen index, so a redelivery re-announces the same spin.</summary>
public sealed record SpinOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("idx")] int Index,
    [property: JsonPropertyName("at")] long AtMillis,
    [property: JsonPropertyName("id")] string SpinId);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(WheelDefs))]
[JsonSerializable(typeof(CurrentWheel))]
[JsonSerializable(typeof(NameWheel))]
[JsonSerializable(typeof(SpinOutcome))]
public sealed partial class WheelJsonContext : JsonSerializerContext;

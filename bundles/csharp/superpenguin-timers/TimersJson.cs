using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinTimers;

/// <summary>
/// One announcement timer group (the original's `TimerGroup` plus its timer-trigger actions folded into plain message lines).
/// Times are unix milliseconds; `LastCount` is the community message counter value at the last run.
/// </summary>
public sealed record TimerGroupState(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("active")] bool Active,
    [property: JsonPropertyName("repeat")] bool Repeat,
    [property: JsonPropertyName("min")] int IntervalMinimumSeconds,
    [property: JsonPropertyName("max")] int IntervalMaximumSeconds,
    [property: JsonPropertyName("msgs")] int MinimumMessages,
    [property: JsonPropertyName("shuffle")] bool Shuffle,
    [property: JsonPropertyName("lines")] List<string> Lines,
    [property: JsonPropertyName("next")] long NextRun,
    [property: JsonPropertyName("last")] long LastCount,
    [property: JsonPropertyName("bag")] List<int> Bag,
    [property: JsonPropertyName("cur")] int Cursor);

/// <summary>Every timer group of one community, stored as a single kv document so a chat message costs one read.</summary>
public sealed record TimerDoc([property: JsonPropertyName("groups")] List<TimerGroupState> Groups);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(TimerDoc))]
public sealed partial class TimersJsonContext : JsonSerializerContext;

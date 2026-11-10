using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinRaidtracker;

/// <summary>The `metadata` object of a normalized `channel.raid` event.</summary>
public sealed record RaidMetadata([property: JsonPropertyName("viewers")] long? Viewers);

/// <summary>The slice of a normalized `channel.raid` payload this bundle reads (the raider comes from the event actor, never a name).</summary>
public sealed record RaidPayload([property: JsonPropertyName("metadata")] RaidMetadata? Metadata);

/// <summary>Incoming-raid history of one raiding community user (UUID only; no names are stored).</summary>
public sealed record RaidRecord(
    [property: JsonPropertyName("u")] string User,
    [property: JsonPropertyName("n")] long Raids,
    [property: JsonPropertyName("v")] long Viewers,
    [property: JsonPropertyName("t")] long LastMillis);

/// <summary>Most-recently-raiding users first; bounded so a `!raids` read stays under the host's kv operation cap.</summary>
public sealed record RaidIndex([property: JsonPropertyName("users")] List<string> Users);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(RaidPayload))]
[JsonSerializable(typeof(RaidRecord))]
[JsonSerializable(typeof(RaidIndex))]
public sealed partial class RaidtrackerJsonContext : JsonSerializerContext;

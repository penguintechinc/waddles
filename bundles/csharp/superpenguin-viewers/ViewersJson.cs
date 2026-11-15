using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinViewers;

/// <summary>One tracked viewer: community user UUID, last activity and last `!lurk` time (unix milliseconds, 0 = never).</summary>
public sealed record ViewerEntry(
    [property: JsonPropertyName("u")] string User,
    [property: JsonPropertyName("t")] long LastActiveMillis,
    [property: JsonPropertyName("l")] long LurkMillis);

/// <summary>The presence table of one community, stored as a single bounded kv document.</summary>
public sealed record ViewerDoc([property: JsonPropertyName("viewers")] List<ViewerEntry> Viewers);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(ViewerDoc))]
public sealed partial class ViewersJsonContext : JsonSerializerContext;

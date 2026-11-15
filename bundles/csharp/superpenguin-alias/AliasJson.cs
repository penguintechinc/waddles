using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinAlias;

/// <summary>The per-community alias registry: alias name (lowercase, no prefix) to the target command text (with its `!`).</summary>
public sealed record AliasRegistry([property: JsonPropertyName("aliases")] Dictionary<string, string> Aliases);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(AliasRegistry))]
public sealed partial class AliasJsonContext : JsonSerializerContext;

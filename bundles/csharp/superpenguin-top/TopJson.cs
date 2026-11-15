using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinTop;

/// <summary>Arguments of the economy service's `points_leaders` tool.</summary>
public sealed record LeadersArgs([property: JsonPropertyName("limit")] int Limit);

/// <summary>One `points_leaders` row (community user UUID and balance).</summary>
public sealed record EconomyLeader(
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("balance")] long Balance);

/// <summary>`points_leaders` result: ranked rows (best first) plus the community's currency alias.</summary>
public sealed record EconomyLeaders(
    [property: JsonPropertyName("entries")] List<EconomyLeader>? Entries,
    [property: JsonPropertyName("alias")] string Alias);

/// <summary>One reputation `leaderboard` row.</summary>
public sealed record ReputationEntry(
    [property: JsonPropertyName("rank")] int Rank,
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("score")] long Score,
    [property: JsonPropertyName("tier")] string Tier);

/// <summary>Reputation `leaderboard` result.</summary>
public sealed record ReputationLeaderboard([property: JsonPropertyName("entries")] List<ReputationEntry>? Entries);

/// <summary>This bundle's own source-generated JSON context (tool contracts only).</summary>
[JsonSerializable(typeof(LeadersArgs))]
[JsonSerializable(typeof(EconomyLeaders))]
[JsonSerializable(typeof(ReputationLeaderboard))]
public sealed partial class TopJsonContext : JsonSerializerContext;

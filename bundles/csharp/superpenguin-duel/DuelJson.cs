using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinDuel;

/// <summary>An open challenge: both players are UUIDs, <see cref="Stake"/> is what EACH player puts up, <see cref="DueAt"/> is the expiry in epoch ms.</summary>
public sealed record DuelRecord(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("atk")] string Challenger,
    [property: JsonPropertyName("def")] string Defender,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("win_pct")] int WinPercent,
    [property: JsonPropertyName("due")] ulong DueAt);

/// <summary>One economy call of a plan: <see cref="Kind"/> is `debit` (the invoking actor's own stake) or `credit` (a mint to <see cref="User"/>).</summary>
public sealed record MoneyOp(
    [property: JsonPropertyName("k")] string Kind,
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("amt")] ulong Amount);

/// <summary>A state change applied AFTER its section's money ops: `open` stores the <see cref="Duel"/>, `close` removes the duel with <see cref="Id"/>.</summary>
public sealed record DuelEffect(
    [property: JsonPropertyName("t")] string Type,
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("duel")] DuelRecord? Duel);

/// <summary>An ordered batch of money ops and effects. The order is part of the plan: the host de-duplicates by (event, kind, ordinal).</summary>
public sealed record PlanSection(
    [property: JsonPropertyName("ops")] List<MoneyOp> Ops,
    [property: JsonPropertyName("fx")] List<DuelEffect> Effects);

/// <summary>
/// The write-ahead plan of ONE event: everything the event will do to money and state, recorded BEFORE the first economy call and
/// re-executed identically on a redelivery. <see cref="Sweep"/> is expiry refunds (always committed); <see cref="Own"/> is the
/// command's own action (dropped, with <see cref="Reply"/> replaced, if its first debit is refused). Bound to the actor, command
/// and raw arguments: only an exact match is treated as a replay.
/// </summary>
public sealed record EventPlan(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("args")] string ArgsHash,
    [property: JsonPropertyName("sweep")] PlanSection Sweep,
    [property: JsonPropertyName("own")] PlanSection? Own,
    [property: JsonPropertyName("release")] List<string> ReleaseOnAbort,
    [property: JsonPropertyName("reply")] string Reply,
    [property: JsonPropertyName("abort_reply")] string AbortReply);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(DuelRecord))]
[JsonSerializable(typeof(EventPlan))]
public sealed partial class DuelJsonContext : JsonSerializerContext;

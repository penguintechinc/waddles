using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinHeist;

/// <summary>The one open heist of a community: <see cref="Due"/> is the join-window end in epoch ms.</summary>
public sealed record GameRecord(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("due")] ulong Due);

/// <summary>One registered entrant, stored in its own slot key so concurrent joins never overwrite each other.</summary>
public sealed record Entrant(
    [property: JsonPropertyName("u")] string User,
    [property: JsonPropertyName("b")] ulong Bet);

/// <summary>One economy call of a plan: `debit` is the invoking actor's own bet, `credit` a mint of exactly what was burned (survivor payout or refund).</summary>
public sealed record MoneyOp(
    [property: JsonPropertyName("k")] string Kind,
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("amt")] ulong Amount);

/// <summary>A state change applied AFTER its section's money ops: `open` stores <see cref="Game"/>, `put` stores the entrant (<see cref="User"/> with <see cref="Bet"/>) in <see cref="Slot"/>, `close` ends game <see cref="Id"/>.</summary>
public sealed record Effect(
    [property: JsonPropertyName("t")] string Type,
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("slot")] int Slot,
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("bet")] ulong Bet,
    [property: JsonPropertyName("game")] GameRecord? Game);

/// <summary>An ordered batch of money ops and effects. The order is part of the plan: the host de-duplicates by (event, kind, ordinal).</summary>
public sealed record PlanSection(
    [property: JsonPropertyName("ops")] List<MoneyOp> Ops,
    [property: JsonPropertyName("fx")] List<Effect> Effects);

/// <summary>
/// The write-ahead plan of ONE event, recorded BEFORE the first economy call and re-executed identically on a redelivery
/// (every survival roll is already inside it). <see cref="Sweep"/> settles an overdue game; <see cref="Own"/> is the command's own join
/// (dropped, with <see cref="AbortReply"/>, if its debit is refused). <see cref="GameId"/> is the game a join targets and
/// <see cref="ClosedReply"/> replaces the reply when that game was settled before the join could register (the fee is refunded).
/// </summary>
public sealed record EventPlan(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("args")] string ArgsHash,
    [property: JsonPropertyName("sweep")] PlanSection Sweep,
    [property: JsonPropertyName("own")] PlanSection? Own,
    [property: JsonPropertyName("release")] List<string> ReleaseOnAbort,
    [property: JsonPropertyName("reply")] string Reply,
    [property: JsonPropertyName("abort_reply")] string AbortReply,
    [property: JsonPropertyName("game_id")] string? GameId,
    [property: JsonPropertyName("closed_reply")] string ClosedReply);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(GameRecord))]
[JsonSerializable(typeof(Entrant))]
[JsonSerializable(typeof(EventPlan))]
public sealed partial class HeistJsonContext : JsonSerializerContext;

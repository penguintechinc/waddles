using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinRacing;

/// <summary>The int status codes of a race record (int codes, not strings, so a status can never drift).</summary>
public static class RaceStatus
{
    /// <summary>The join window is open.</summary>
    public const int Open = 0;

    /// <summary>The window closed and settlement started; payouts may be partly paid (resumable).</summary>
    public const int Settling = 1;

    /// <summary>Raced and every payout paid.</summary>
    public const int Done = 2;

    /// <summary>Voided (too few racers or a stale window) and every stake refunded.</summary>
    public const int Void = 3;
}

/// <summary>
/// One race, recorded in `kv` when its first joiner opens it. <see cref="Seed"/> is derived from the race id and the recorded
/// host <see cref="Entropy"/> BEFORE the first stake is taken, so the result is auditable (re-run the simulation from
/// seed + config + roster). <see cref="OpenMessageId"/> is the opening invocation's message id: it is the stable part of every
/// payout/refund idempotency key, so a settlement retried from a different event still maps to the same economy writes.
/// </summary>
public sealed record RaceRecord(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("opened")] ulong OpenedAt,
    [property: JsonPropertyName("closes")] ulong ClosesAt,
    [property: JsonPropertyName("msg")] string OpenMessageId,
    [property: JsonPropertyName("plat")] string Platform,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("entropy")] string Entropy,
    [property: JsonPropertyName("cfg")] string Config,
    [property: JsonPropertyName("laps")] int Laps,
    [property: JsonPropertyName("max")] int MaxRacers,
    [property: JsonPropertyName("status")] int Status);

/// <summary>One roster slot: the racer's community user uuid and the stake already debited for them (never a name).</summary>
public sealed record RosterEntry(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("stake")] ulong Stake);

/// <summary>One racer's settled line: grid lane, place (1 = winner, 0 for a void refund), the payout credited, and the finish time.</summary>
public sealed record RacerResult(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("lane")] int Lane,
    [property: JsonPropertyName("place")] int Place,
    [property: JsonPropertyName("payout")] ulong Payout,
    [property: JsonPropertyName("finish_ms")] long FinishMs,
    [property: JsonPropertyName("dnf")] bool Dnf);

/// <summary>
/// The decided result of a window, written ONCE before any payout so a settlement that is interrupted resumes with exactly the same
/// amounts. <see cref="Paid"/> lists the actors whose credit/refund already went through; <see cref="VoidReason"/> is empty for a
/// real race, else `too_few` or `late`.
/// </summary>
public sealed record RaceResultRecord(
    [property: JsonPropertyName("void")] string VoidReason,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("pot")] ulong Pot,
    [property: JsonPropertyName("racers")] List<RacerResult> Racers,
    [property: JsonPropertyName("paid")] List<string> Paid);

/// <summary>This bundle's own source-generated JSON context (a bundle cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(RaceRecord))]
[JsonSerializable(typeof(RosterEntry))]
[JsonSerializable(typeof(RaceResultRecord))]
public sealed partial class RacingJsonContext : JsonSerializerContext;

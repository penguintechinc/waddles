using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinGiveaway;

/// <summary>The commitment half of a closed pool: fingerprint, public commitment, the PRIVATE seed, and the draws made so far.</summary>
public sealed record FreezeInfo(
    [property: JsonPropertyName("fp")] string Fingerprint,
    [property: JsonPropertyName("commit")] string Commitment,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("total")] long Total,
    [property: JsonPropertyName("n")] int Entrants,
    [property: JsonPropertyName("draws")] int Draws,
    [property: JsonPropertyName("won")] List<string> Won);

/// <summary>The community's one giveaway: current round, whether it accepts entries, prize text, cooldown tier, and the frozen pool.</summary>
public sealed record GiveawayState(
    [property: JsonPropertyName("round")] long Round,
    [property: JsonPropertyName("open")] bool Open,
    [property: JsonPropertyName("prize")] string Prize,
    [property: JsonPropertyName("tier")] int Tier,
    [property: JsonPropertyName("freeze")] FreezeInfo? Freeze);

/// <summary>One entrant in a pool shard; `Events` holds the last few applied event tags so a redelivered entry is not applied twice.</summary>
public sealed record PoolEntry(
    [property: JsonPropertyName("a")] string Actor,
    [property: JsonPropertyName("t")] long Tickets,
    [property: JsonPropertyName("e")] List<string> Events);

/// <summary>One of the sixteen pool shards (keyed by round and the first hex digit of the actor hash).</summary>
public sealed record PoolShard([property: JsonPropertyName("e")] List<PoolEntry> Entries);

/// <summary>An explicit exclusion; `UntilMillis` 0 means permanent.</summary>
public sealed record ExclusionItem(
    [property: JsonPropertyName("a")] string Actor,
    [property: JsonPropertyName("u")] long UntilMillis);

/// <summary>The community's explicit exclusion list.</summary>
public sealed record ExclusionList([property: JsonPropertyName("i")] List<ExclusionItem> Items);

/// <summary>A past winner and the number of following rounds they sit out.</summary>
public sealed record WinnerItem(
    [property: JsonPropertyName("a")] string Actor,
    [property: JsonPropertyName("r")] long Round,
    [property: JsonPropertyName("c")] int Cooldown);

/// <summary>The bounded log of recent winners.</summary>
public sealed record WinnerLog([property: JsonPropertyName("i")] List<WinnerItem> Items);

/// <summary>One entry command recorded under the event's replay key before the debit.</summary>
public sealed record EntryOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("round")] long Round,
    [property: JsonPropertyName("tickets")] long Tickets,
    [property: JsonPropertyName("tag")] string Tag);

/// <summary>The published audit record of one draw; everything a viewer needs to re-derive the winning ticket index.</summary>
public sealed record AuditRecord(
    [property: JsonPropertyName("round")] long Round,
    [property: JsonPropertyName("draw")] int Draw,
    [property: JsonPropertyName("fp")] string Fingerprint,
    [property: JsonPropertyName("commit")] string Commitment,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("total")] long Total,
    [property: JsonPropertyName("index")] long Index,
    [property: JsonPropertyName("start")] long RangeStart,
    [property: JsonPropertyName("end")] long RangeEnd,
    [property: JsonPropertyName("winner")] string Winner,
    [property: JsonPropertyName("eligible")] int Eligible);

/// <summary>A draw recorded before any state change so a redelivered `draw` event reprints and re-applies the same result.</summary>
public sealed record DrawRecord(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("prize")] string Prize,
    [property: JsonPropertyName("tier")] int Tier,
    [property: JsonPropertyName("audit")] AuditRecord Audit);

/// <summary>Moderator-controlled bundle settings.</summary>
public sealed record GiveawayConfig([property: JsonPropertyName("overlay")] bool Overlay);

/// <summary>A running raffle: free single entry per user, seed committed at start, winners fixed (`Pending`) before any payout.</summary>
public sealed record RaffleDoc(
    [property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("round")] long Round,
    [property: JsonPropertyName("winners")] int Winners,
    [property: JsonPropertyName("total")] long Total,
    [property: JsonPropertyName("entries")] List<string> Entries,
    [property: JsonPropertyName("seed")] string Seed,
    [property: JsonPropertyName("commit")] string Commitment,
    [property: JsonPropertyName("tag")] string StartTag,
    [property: JsonPropertyName("pending")] List<string>? Pending,
    [property: JsonPropertyName("each")] long Each,
    [property: JsonPropertyName("paid")] List<string>? Awarded,
    [property: JsonPropertyName("failed")] List<string>? Failed);

/// <summary>The final text of an ended raffle, recorded so a redelivered `raffle end` reprints it instead of reporting "not running".</summary>
public sealed record RaffleEndRecord(
    [property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("winners")] List<string> Winners,
    [property: JsonPropertyName("each")] long Each);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(GiveawayState))]
[JsonSerializable(typeof(PoolShard))]
[JsonSerializable(typeof(ExclusionList))]
[JsonSerializable(typeof(WinnerLog))]
[JsonSerializable(typeof(EntryOutcome))]
[JsonSerializable(typeof(AuditRecord))]
[JsonSerializable(typeof(DrawRecord))]
[JsonSerializable(typeof(GiveawayConfig))]
[JsonSerializable(typeof(RaffleDoc))]
[JsonSerializable(typeof(RaffleEndRecord))]
public sealed partial class GiveawayJsonContext : JsonSerializerContext;

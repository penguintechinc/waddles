using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinFishing;

/// <summary>Fish rarity tier, ported from upstream `FishRarity` (numeric values are the upstream ordering).</summary>
public enum FishRarity
{
    Common = 0,
    Uncommon = 1,
    Rare = 2,
    Epic = 3,
    Legendary = 4,
    Mythical = 5,
}

/// <summary>Equipment slot of a shop item, ported from upstream `EquipmentSlot`.</summary>
public enum EquipmentSlot
{
    Rod,
    Reel,
    Line,
    Hook,
    Bait,
    Lure,
    TackleBox,
    Net,
    Special,
}

/// <summary>What a shop-item boost slot improves, ported from upstream `FishingBoostType`.</summary>
public enum BoostType
{
    GeneralRarityBoost,
    SpecificFishBoost,
    WeightBoost,
    StarBoost,
    SpecificCategoryBoost,
}

/// <summary>Tournament scoring rule, ported from upstream `FishingTournamentScoreCategory`.</summary>
public enum ScoreCategory
{
    SpecificFish,
    Largest,
    MostValuable,
    Smallest,
    Average,
    MostCatches,
    TotalWeight,
}

/// <summary>Price-rank tier of a shop item, ported from upstream `EquipmentTier`.</summary>
public enum EquipmentTier
{
    Entry,
    Mid,
    High,
    Top,
    Consumable,
}

/// <summary>One catchable fish. Upstream keeps these as admin-managed database rows; here the default catalog is data in <see cref="FishingCatalog"/>.</summary>
public sealed record FishType(string Id, string Name, FishRarity Rarity, double BaseWeight, int BaseGold, string[] Categories);

/// <summary>One purchasable item (upstream `FishingShopItem`). Up to three boosts, optional targets, uses and durability.</summary>
public sealed record ShopItem(
    string Id,
    string Name,
    string Description,
    int Cost,
    BoostType BoostType,
    double BoostAmount,
    EquipmentSlot? Slot,
    int? MaxUses = null,
    BoostType? BoostType2 = null,
    double? BoostAmount2 = null,
    BoostType? BoostType3 = null,
    double? BoostAmount3 = null,
    string? TargetFishId = null,
    string? TargetCategory = null,
    bool Enabled = true,
    bool AdminOnly = false,
    bool DisableBreaking = false,
    int? MaxDurability = null,
    double? DurabilityLossPerUse = null);

/// <summary>Global fishing settings (upstream `FishingSettings`); all chances are probabilities in [0,1].</summary>
public sealed record FishingSettings(
    bool BoostMode = false,
    double BoostModeRarityMultiplier = 2.0,
    double LineSnapChance = 0.02,
    double RodSnapChance = 0.0005,
    double ReelJamChance = 0.0,
    double TackleBoxLostChance = 0.0,
    double NetBreakChance = 0.0,
    double RepairCostMultiplier = 0.0);

/// <summary>One item a player owns (upstream `UserFishingBoost`). <c>RemainingUses</c> of -1 means unlimited.</summary>
public sealed class OwnedItem
{
    /// <summary>Per-player sequence id (the player-visible handle for equip/sell/repair).</summary>
    [JsonPropertyName("n")] public int Id { get; set; }

    /// <summary>The <see cref="ShopItem.Id"/> this instance was bought as.</summary>
    [JsonPropertyName("s")] public string ShopId { get; set; } = string.Empty;

    [JsonPropertyName("e")] public bool Equipped { get; set; }

    [JsonPropertyName("u")] public int RemainingUses { get; set; } = -1;

    [JsonPropertyName("d")] public double? Durability { get; set; }

    /// <summary>True when durability is tracked and exhausted (upstream `IsBroken`).</summary>
    [JsonIgnore] public bool IsBroken => Durability is { } d && d <= 0.000001;
}

/// <summary>A player's best catch of one fish type (highest stars, then weight).</summary>
public sealed record BestCatch(
    [property: JsonPropertyName("st")] int Stars,
    [property: JsonPropertyName("w")] double Weight);

/// <summary>Everything the bundle persists per player (one kv value, so a state change is a single atomic write).</summary>
public sealed class PlayerState
{
    [JsonPropertyName("gold")] public long Gold { get; set; }

    [JsonPropertyName("next")] public int NextItemId { get; set; } = 1;

    [JsonPropertyName("items")] public List<OwnedItem> Items { get; set; } = [];

    [JsonPropertyName("best")] public Dictionary<string, BestCatch> Best { get; set; } = [];

    [JsonPropertyName("counts")] public Dictionary<string, int> Counts { get; set; } = [];

    [JsonPropertyName("earned")] public long TotalGoldEarned { get; set; }

    [JsonPropertyName("lost")] public long TotalGoldLost { get; set; }

    [JsonPropertyName("casts")] public long Casts { get; set; }

    /// <summary>Short keys of the most recent mutating events applied to this player (and settlement payouts): a redelivery applies nothing twice.</summary>
    [JsonPropertyName("applied")] public List<string> Applied { get; set; } = [];

    /// <summary>The most applied-event keys remembered per player.</summary>
    public const int MaxApplied = 24;

    /// <summary>True when <paramref name="key"/> was already applied.</summary>
    public bool HasApplied(string key) => Applied.Contains(key);

    /// <summary>Remembers <paramref name="key"/> as applied, dropping the oldest beyond <see cref="MaxApplied"/>.</summary>
    public void MarkApplied(string key)
    {
        Applied.Add(key);
        if (Applied.Count > MaxApplied)
        {
            Applied.RemoveRange(0, Applied.Count - MaxApplied);
        }
    }
}

/// <summary>What one cast produced.</summary>
public enum CastKind
{
    Caught,
    LineSnapped,
    RodSnapped,
    ReelJammed,
    TackleBoxLost,
    NetBroken,
}

/// <summary>An item that ran out of durability on a catch.</summary>
public sealed record BrokenItem(
    [property: JsonPropertyName("n")] string Name,
    [property: JsonPropertyName("r")] bool Replaced);

/// <summary>The recorded result of one cast (compact; this is what a replay reprints).</summary>
public sealed record CastResult(
    [property: JsonPropertyName("k")] CastKind Kind,
    [property: JsonPropertyName("f")] string FishId,
    [property: JsonPropertyName("st")] int Stars,
    [property: JsonPropertyName("w")] double Weight,
    [property: JsonPropertyName("g")] int Gold,
    [property: JsonPropertyName("lost")] long GoldLost,
    [property: JsonPropertyName("li")] string[] LostItems,
    [property: JsonPropertyName("br")] BrokenItem[] Broken);

/// <summary>The recorded outcome of one cast command (replay record): seed, rendered reply and who/what it belongs to.</summary>
public sealed record CastOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("seed")] ulong Seed,
    [property: JsonPropertyName("n")] int Count,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("res")] CastResult[] Results);

/// <summary>The recorded outcome of a shop/inventory command (replay record).</summary>
public sealed record ShopOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("text")] string Text);

/// <summary>One participant's running tournament aggregate (everything every <see cref="ScoreCategory"/> needs).</summary>
public sealed class TournamentAggregate
{
    [JsonPropertyName("count")] public int Count { get; set; }

    [JsonPropertyName("max")] public double MaxWeight { get; set; }

    [JsonPropertyName("min")] public double MinWeight { get; set; } = double.MaxValue;

    [JsonPropertyName("sum")] public double TotalWeight { get; set; }

    [JsonPropertyName("gold")] public int MaxGold { get; set; }

    [JsonPropertyName("stars")] public int TotalStars { get; set; }

    [JsonPropertyName("applied")] public List<string> Applied { get; set; } = [];
}

/// <summary>One row of a tournament's bounded standings board (a user and their score under the tournament's rule).</summary>
public sealed record BoardEntry(
    [property: JsonPropertyName("u")] string User,
    [property: JsonPropertyName("s")] double Score,
    [property: JsonPropertyName("c")] int Count,
    [property: JsonPropertyName("st")] int TotalStars);

/// <summary>A tournament window's persisted state: its bounded board and whether rewards were settled.</summary>
public sealed class TournamentBoard
{
    [JsonPropertyName("rows")] public List<BoardEntry> Rows { get; set; } = [];

    [JsonPropertyName("settled")] public bool Settled { get; set; }

    /// <summary>True once the window's settlement task was scheduled (so it is scheduled exactly once).</summary>
    [JsonPropertyName("sched")] public bool Scheduled { get; set; }
}

/// <summary>One reward placement of a tournament template: community points (minted via the economy service) and/or gold.</summary>
public sealed record TournamentReward(int Placement, long Points, long Gold);

/// <summary>A recurring tournament definition (upstream `FishingTournament` + reward rules, minus the admin editing surface).</summary>
public sealed record TournamentTemplate(
    string Id,
    string Name,
    string Description,
    ScoreCategory Score,
    string? TargetFishId,
    string[] EligibleFishIds,
    string[] EligibleCategories,
    int PeriodMinutes,
    int DurationMinutes,
    int OffsetMinutes,
    TournamentReward[] Rewards);

/// <summary>Top-N lifetime leaderboards (gold earned, gold lost to accidents), both monotone so a bounded board stays exact.</summary>
public sealed class LifetimeBoards
{
    [JsonPropertyName("earned")] public List<LifetimeRow> Earned { get; set; } = [];

    [JsonPropertyName("lost")] public List<LifetimeRow> Lost { get; set; } = [];
}

/// <summary>One leaderboard row.</summary>
public sealed record LifetimeRow(
    [property: JsonPropertyName("u")] string User,
    [property: JsonPropertyName("v")] long Value);

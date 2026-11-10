namespace WaddleBundleSuperpenguinFishing;

/// <summary>Source of integer randomness for the game rules, so a recorded seed reproduces a whole command byte for byte.</summary>
public interface IFishRng
{
    /// <summary>A uniform integer in [<paramref name="minInclusive"/>, <paramref name="maxExclusive"/>) (upstream `RandomNumberGenerator.GetInt32`).</summary>
    int Next(int minInclusive, int maxExclusive);
}

/// <summary>
/// SplitMix64 stream over one recorded 64-bit seed. The seed is drawn once from the platform random source (`System.Random.Shared`: `System.Security.Cryptography` throws PlatformNotSupported in the NativeAOT-wasi build, found by the real-executor test) and recorded BEFORE any state
/// changes, so a redelivered event replays the identical casts (upstream draws every roll straight from a CSPRNG; this port cannot, so odds are
/// statistically faithful but not cryptographically unpredictable).
/// </summary>
public sealed class SeededRng(ulong seed) : IFishRng
{
    private ulong _state = seed;

    /// <inheritdoc />
    public int Next(int minInclusive, int maxExclusive)
    {
        if (maxExclusive <= minInclusive)
        {
            throw new ArgumentOutOfRangeException(nameof(maxExclusive), "range must be non-empty");
        }

        var span = (ulong)((long)maxExclusive - minInclusive);
        // Rejection sampling removes modulo bias.
        var limit = ulong.MaxValue - (ulong.MaxValue % span);
        ulong value;
        do
        {
            value = NextUInt64();
        }
        while (value >= limit);

        return (int)(minInclusive + (long)(value % span));
    }

    private ulong NextUInt64()
    {
        _state += 0x9E3779B97F4A7C15UL;
        var z = _state;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
        return z ^ (z >> 31);
    }
}

/// <summary>
/// The catch mathematics, ported from upstream `FishingCalculations`, `FishingRarityWeightProfiles` and `FishingValueRules`:
/// rarity-weighted fish selection (with boost-mode, general-rarity and targeted fish/category boosts), stars, weight and gold.
/// </summary>
public static class FishingCalculations
{
    /// <summary>Upstream base rarity weights (Common 50 ... Mythical 0.2).</summary>
    public static readonly IReadOnlyDictionary<FishRarity, double> BaseRarityWeights = new Dictionary<FishRarity, double>
    {
        [FishRarity.Common] = 50.0,
        [FishRarity.Uncommon] = 30.0,
        [FishRarity.Rare] = 15.0,
        [FishRarity.Epic] = 4.0,
        [FishRarity.Legendary] = 1.0,
        [FishRarity.Mythical] = 0.2,
    };

    /// <summary>Boost amounts outside this range are refused (upstream `FishingValueRules`).</summary>
    public const double MinBoostAmount = -0.8;

    /// <summary>Upper bound of a boost amount.</summary>
    public const double MaxBoostAmount = 5.0;

    /// <summary>Rarity weights restricted to the rarities present among <paramref name="fish"/> and renormalized to the base total.</summary>
    public static Dictionary<FishRarity, double> AvailableWeights(IEnumerable<FishType> fish)
    {
        var present = fish.Select(f => f.Rarity).ToHashSet();
        var weights = BaseRarityWeights.Where(kv => present.Contains(kv.Key)).ToDictionary(kv => kv.Key, kv => kv.Value);
        if (weights.Count == 0)
        {
            return weights;
        }

        var factor = BaseRarityWeights.Values.Sum() / weights.Values.Sum();
        foreach (var rarity in weights.Keys.ToList())
        {
            weights[rarity] *= factor;
        }

        return weights;
    }

    /// <summary>Picks the fish for one cast using the equipped, unbroken <paramref name="boosts"/>.</summary>
    public static FishType SelectRandomFish(IReadOnlyList<FishType> fish, FishingSettings settings, IReadOnlyList<ShopItem> boosts, IFishRng rng)
    {
        var weights = AvailableWeights(fish);
        if (settings.BoostMode)
        {
            foreach (var rarity in weights.Keys.Where(r => r != FishRarity.Common).ToList())
            {
                weights[rarity] *= settings.BoostModeRarityMultiplier;
            }
        }

        foreach (var item in boosts)
        {
            foreach (var (type, amount) in Boosts(item))
            {
                if (type != BoostType.GeneralRarityBoost)
                {
                    continue;
                }

                foreach (var rarity in weights.Keys.Where(r => r != FishRarity.Common).ToList())
                {
                    weights[rarity] *= 1.0 + amount;
                }
            }
        }

        var total = weights.Values.Sum();
        var roll = rng.Next(0, (int)(total * 1000)) / 1000.0;
        var running = 0.0;
        var selected = FishRarity.Common;
        foreach (var (rarity, weight) in weights.OrderBy(kv => (int)kv.Key))
        {
            running += weight;
            if (roll < running)
            {
                selected = rarity;
                break;
            }
        }

        var pool = fish.Where(f => f.Rarity == selected).ToList();
        if (pool.Count == 0)
        {
            pool = fish.ToList();
        }

        var targeted = boosts.Where(IsTargeted).ToList();
        if (targeted.Count > 0)
        {
            var weighted = pool.Select(f => (Fish: f, Weight: targeted.Aggregate(1.0, (acc, item) => acc * TargetedMultiplier(item, f)))).ToList();
            var fishTotal = weighted.Sum(w => w.Weight);
            var fishRoll = rng.Next(0, (int)(fishTotal * 1000)) / 1000.0;
            var fishRunning = 0.0;
            foreach (var (candidate, weight) in weighted)
            {
                fishRunning += weight;
                if (fishRoll < fishRunning)
                {
                    return candidate;
                }
            }
        }

        return pool[rng.Next(0, pool.Count)];
    }

    /// <summary>The per-fish multiplier a targeted-boost item contributes (1.0 when the fish/category does not match).</summary>
    public static double TargetedMultiplier(ShopItem item, FishType fish)
    {
        var categoryMatches = !string.IsNullOrWhiteSpace(item.TargetCategory)
            && fish.Categories.Any(c => string.Equals(c, item.TargetCategory, StringComparison.OrdinalIgnoreCase));
        var fishMatches = item.TargetFishId == fish.Id;
        var multiplier = 1.0;
        foreach (var (type, amount) in Boosts(item))
        {
            if ((type == BoostType.SpecificFishBoost && fishMatches) || (type == BoostType.SpecificCategoryBoost && categoryMatches))
            {
                multiplier *= 1.0 + amount;
            }
        }

        return multiplier;
    }

    /// <summary>Stars for one catch: 3 on a roll under 5% (+star boost), else 2 under another 20% (+star boost), else 1.</summary>
    public static int CalculateStars(IReadOnlyList<ShopItem> boosts, IFishRng rng)
    {
        var boost = TotalBoost(boosts, BoostType.StarBoost);
        var threeStar = 5.0 + (boost * 100);
        var twoStar = 20.0 + (boost * 100);
        var roll = rng.Next(0, 10000) / 100.0;
        if (roll < threeStar)
        {
            return 3;
        }

        return roll < twoStar + threeStar ? 2 : 1;
    }

    /// <summary>Weight of one catch: base weight x [0.8,1.13) x star multiplier x weight boost, floored at 0.01 and rounded to 2 places.</summary>
    public static double CalculateWeight(FishType fish, int stars, IReadOnlyList<ShopItem> boosts, IFishRng rng)
    {
        var boostMultiplier = Math.Max(0.01, 1.0 + TotalBoost(boosts, BoostType.WeightBoost));
        var starMultiplier = stars switch { 3 => 1.5, 2 => 1.2, _ => 1.0 };
        var random = rng.Next(0, 10000) / 10000.0;
        var weightMultiplier = 0.8 + (random * (1.13 - 0.8));
        var weight = fish.BaseWeight * weightMultiplier * starMultiplier * boostMultiplier;
        return Math.Max(0.01, Math.Round(weight, 2));
    }

    /// <summary>Gold of one catch: base gold x a star-banded multiplier (1: 0.75-1.0, 2: 1.0-1.25, 3: 1.25-1.41) x a weight factor in [0.9,1.065].</summary>
    public static int CalculateGold(FishType fish, int stars, double actualWeight, IFishRng rng)
    {
        var (min, max) = stars switch { 3 => (1250, 1410), 2 => (1000, 1250), _ => (750, 1000) };
        var random = rng.Next(min, max) / 1000.0;
        var weightMultiplier = 1.0;
        if (fish.BaseWeight > 0)
        {
            weightMultiplier = 0.9 + ((actualWeight / fish.BaseWeight - 0.8) / (1.13 - 0.8) * 0.165);
            weightMultiplier = Math.Max(0.9, Math.Min(1.065, weightMultiplier));
        }

        return Math.Max(1, (int)(fish.BaseGold * random * weightMultiplier));
    }

    /// <summary>Sum of a boost type's amounts across every boost slot of every item.</summary>
    public static double TotalBoost(IEnumerable<ShopItem> items, BoostType type)
    {
        var total = 0.0;
        foreach (var item in items)
        {
            foreach (var (t, amount) in Boosts(item))
            {
                if (t == type)
                {
                    total += amount;
                }
            }
        }

        return total;
    }

    /// <summary>The (type, amount) pairs an item carries (up to three).</summary>
    public static IEnumerable<(BoostType Type, double Amount)> Boosts(ShopItem item)
    {
        yield return (item.BoostType, item.BoostAmount);
        if (item.BoostType2 is { } t2)
        {
            yield return (t2, item.BoostAmount2 ?? 0);
        }

        if (item.BoostType3 is { } t3)
        {
            yield return (t3, item.BoostAmount3 ?? 0);
        }
    }

    private static bool IsTargeted(ShopItem item) =>
        (Boosts(item).Any(b => b.Type == BoostType.SpecificFishBoost) && item.TargetFishId is not null)
        || (Boosts(item).Any(b => b.Type == BoostType.SpecificCategoryBoost) && !string.IsNullOrWhiteSpace(item.TargetCategory));

    /// <summary>
    /// Upstream `FishingValueRules.NormalizeAndValidate` for shop items: a boost amount must be a finite number in
    /// [<see cref="MinBoostAmount"/>, <see cref="MaxBoostAmount"/>] and targeted boosts need their target. Returns an error message or null.
    /// </summary>
    public static string? ValidateShopItem(ShopItem item)
    {
        foreach (var (_, amount) in Boosts(item))
        {
            if (double.IsNaN(amount) || double.IsInfinity(amount) || amount < MinBoostAmount || amount > MaxBoostAmount)
            {
                return $"Boost amount must be between {MinBoostAmount} and {MaxBoostAmount}.";
            }
        }

        if (Boosts(item).Any(b => b.Type == BoostType.SpecificFishBoost) && item.TargetFishId is null)
        {
            return "Target fish is required when using a specific fish boost.";
        }

        if (Boosts(item).Any(b => b.Type == BoostType.SpecificCategoryBoost) && string.IsNullOrWhiteSpace(item.TargetCategory))
        {
            return "Target category is required when using a specific category boost.";
        }

        return null;
    }
}

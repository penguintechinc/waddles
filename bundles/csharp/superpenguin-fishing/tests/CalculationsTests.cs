using Xunit;

namespace WaddleBundleSuperpenguinFishing.Tests;

public class CalculationsTests
{
    private static readonly FishType Common = Cat.Fish("c", FishRarity.Common, 10, 10, "pond");
    private static readonly FishType Rare = Cat.Fish("r", FishRarity.Rare, 4, 80, "reef");
    private static readonly FishType Mythic = Cat.Fish("m", FishRarity.Mythical, 1, 400, "pond", "deep");

    [Fact]
    public void seeded_rng_is_deterministic_in_range_and_rejects_empty_ranges()
    {
        var a = new SeededRng(7);
        var b = new SeededRng(7);
        for (var i = 0; i < 200; i++)
        {
            var x = a.Next(5, 15);
            Assert.Equal(x, b.Next(5, 15));
            Assert.InRange(x, 5, 14);
        }

        Assert.NotEqual(new SeededRng(1).Next(0, 1_000_000), new SeededRng(2).Next(0, 1_000_000));
        Assert.Throws<ArgumentOutOfRangeException>(() => new SeededRng(1).Next(3, 3));
    }

    [Fact]
    public void seeded_rng_is_roughly_uniform()
    {
        var rng = new SeededRng(99);
        var buckets = new int[4];
        for (var i = 0; i < 4000; i++)
        {
            buckets[rng.Next(0, 4)]++;
        }

        Assert.All(buckets, c => Assert.InRange(c, 800, 1200));
    }

    [Fact]
    public void available_weights_keep_only_present_rarities_and_renormalize_to_the_base_total()
    {
        var w = FishingCalculations.AvailableWeights([Common, Rare]);
        Assert.Equal([FishRarity.Common, FishRarity.Rare], w.Keys.OrderBy(k => k));
        Assert.Equal(FishingCalculations.BaseRarityWeights.Values.Sum(), w.Values.Sum(), 9);
        Assert.Equal(50.0 / 65.0, w[FishRarity.Common] / w.Values.Sum(), 9);
        Assert.Empty(FishingCalculations.AvailableWeights([]));
    }

    [Fact]
    public void selection_follows_the_rarity_cdf_in_the_upstream_order()
    {
        // Common and Rare only: total 100.2, common share 50/65 of it. A roll of 0 is Common, the last milli-roll is Rare.
        var settings = new FishingSettings();
        Assert.Equal("c", FishingCalculations.SelectRandomFish([Common, Rare], settings, [], new ScriptedRng(0, 0)).Id);
        Assert.Equal("r", FishingCalculations.SelectRandomFish([Common, Rare], settings, [], new ScriptedRng(100_199, 0)).Id);
    }

    [Fact]
    public void boost_mode_and_general_rarity_boost_scale_every_non_common_weight()
    {
        var fish = new[] { Common, Rare, Mythic };
        // Unboosted cdf (total 100.2): common 0..76.84, rare ..99.89, mythical ..100.2. A roll of 100.000 is Mythical.
        Assert.Equal("m", FishingCalculations.SelectRandomFish(fish, new FishingSettings(), [], new ScriptedRng(100_000, 0)).Id);

        // Boost mode x2 widens rare's band (to 122.9): the same roll is now Rare.
        Assert.Equal("r", FishingCalculations.SelectRandomFish(fish, new FishingSettings(BoostMode: true, BoostModeRarityMultiplier: 2.0), [], new ScriptedRng(100_000, 0)).Id);

        // A +100% general rarity rod does the same.
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, amount: 1.0);
        Assert.Equal("r", FishingCalculations.SelectRandomFish(fish, new FishingSettings(), [rod], new ScriptedRng(100_000, 0)).Id);
    }

    [Fact]
    public void specific_fish_and_category_boosts_reweight_within_the_chosen_rarity()
    {
        var a = Cat.Fish("a", FishRarity.Common, 5, 10, "pond");
        var b = Cat.Fish("b", FishRarity.Common, 5, 10, "reef");
        var settings = new FishingSettings();
        var byFish = Cat.Gear("bait", EquipmentSlot.Bait, type: BoostType.SpecificFishBoost, amount: 3.0, targetFish: "b");
        var byCategory = Cat.Gear("lure", EquipmentSlot.Lure, type: BoostType.SpecificCategoryBoost, amount: 3.0, targetCategory: "REEF");

        // weights: a=1, b=4 (total 5000 milli). Roll 900 -> a, roll 1100 -> b.
        Assert.Equal("a", FishingCalculations.SelectRandomFish([a, b], settings, [byFish], new ScriptedRng(0, 900)).Id);
        Assert.Equal("b", FishingCalculations.SelectRandomFish([a, b], settings, [byFish], new ScriptedRng(0, 1100)).Id);
        Assert.Equal("b", FishingCalculations.SelectRandomFish([a, b], settings, [byCategory], new ScriptedRng(0, 1100)).Id);
        // Both stacked: b = 16x.
        Assert.Equal("b", FishingCalculations.SelectRandomFish([a, b], settings, [byFish, byCategory], new ScriptedRng(0, 1100)).Id);
        // A targeted boost that matches nothing leaves the weights equal: 1.4 of 2.0 lands on the second fish, 0.9 on the first.
        var miss = Cat.Gear("x", EquipmentSlot.Bait, type: BoostType.SpecificFishBoost, amount: 3.0, targetFish: "zzz");
        Assert.Equal("b", FishingCalculations.SelectRandomFish([a, b], settings, [miss], new ScriptedRng(0, 1400)).Id);
        Assert.Equal("a", FishingCalculations.SelectRandomFish([a, b], settings, [miss], new ScriptedRng(0, 900)).Id);
        // No targeted boost at all: uniform pick by index.
        Assert.Equal("b", FishingCalculations.SelectRandomFish([a, b], settings, [], new ScriptedRng(0, 1)).Id);
    }

    [Fact]
    public void targeted_multiplier_only_applies_to_matching_boost_slots()
    {
        var item = new ShopItem("i", "i", "d", 1, BoostType.SpecificFishBoost, 0.5, EquipmentSlot.Bait, TargetFishId: "c",
            BoostType2: BoostType.SpecificCategoryBoost, BoostAmount2: 1.0, TargetCategory: "pond",
            BoostType3: BoostType.WeightBoost, BoostAmount3: 9);
        Assert.Equal(1.5 * 2.0, FishingCalculations.TargetedMultiplier(item, Common), 9);
        Assert.Equal(2.0, FishingCalculations.TargetedMultiplier(item, Mythic), 9); // category only
        Assert.Equal(1.0, FishingCalculations.TargetedMultiplier(item, Rare), 9);
    }

    [Theory]
    [InlineData(0, 3)]
    [InlineData(499, 3)]
    [InlineData(500, 2)]
    [InlineData(2499, 2)]
    [InlineData(2500, 1)]
    [InlineData(9999, 1)]
    public void stars_use_the_5_and_20_percent_bands(int roll, int stars) =>
        Assert.Equal(stars, FishingCalculations.CalculateStars([], new ScriptedRng(roll)));

    [Fact]
    public void star_boost_widens_both_bands()
    {
        var reel = Cat.Gear("reel", EquipmentSlot.Reel, type: BoostType.StarBoost, amount: 0.10);
        // 5% + 10% = 15% three-star, then 20% + 10% two-star: roll 1499 -> 3 stars, 1500 -> 2, 4499 -> 2, 4500 -> 1.
        Assert.Equal(3, FishingCalculations.CalculateStars([reel], new ScriptedRng(1499)));
        Assert.Equal(2, FishingCalculations.CalculateStars([reel], new ScriptedRng(1500)));
        Assert.Equal(2, FishingCalculations.CalculateStars([reel], new ScriptedRng(4499)));
        Assert.Equal(1, FishingCalculations.CalculateStars([reel], new ScriptedRng(4500)));
    }

    [Fact]
    public void weight_scales_by_roll_stars_and_boost_with_a_floor()
    {
        var fish = Cat.Fish("f", weight: 10);
        Assert.Equal(8.0, FishingCalculations.CalculateWeight(fish, 1, [], new ScriptedRng(0)), 2);
        Assert.Equal(16.95, FishingCalculations.CalculateWeight(fish, 3, [], new ScriptedRng(9999)), 2);
        Assert.Equal(8.0 * 1.2, FishingCalculations.CalculateWeight(fish, 2, [], new ScriptedRng(0)), 2);
        var line = Cat.Gear("line", EquipmentSlot.Line, type: BoostType.WeightBoost, amount: 0.5);
        Assert.Equal(12.0, FishingCalculations.CalculateWeight(fish, 1, [line], new ScriptedRng(0)), 2);
        var cursed = Cat.Gear("cursed", EquipmentSlot.Net, type: BoostType.WeightBoost, amount: -5.0);
        Assert.True(FishingCalculations.CalculateWeight(fish, 1, [cursed], new ScriptedRng(0)) >= 0.01);
    }

    [Fact]
    public void gold_uses_star_bands_and_a_clamped_weight_factor()
    {
        var fish = Cat.Fish("f", weight: 10, gold: 100);
        // One star, lowest roll (0.75), lightest weight (0.8x -> factor 0.9): 100 * 0.75 * 0.9 = 67.
        Assert.Equal(67, FishingCalculations.CalculateGold(fish, 1, 8.0, new ScriptedRng(0)));
        // Three stars, highest band roll (1.409), heaviest weight (factor clamps to 1.065): int(100*1.409*1.065) = 150.
        Assert.Equal(150, FishingCalculations.CalculateGold(fish, 3, 99.0, new ScriptedRng(1_000_000)));
        Assert.InRange(FishingCalculations.CalculateGold(fish, 2, 10.0, new ScriptedRng(125)), 100, 140);
        // A fish with zero base weight skips the weight factor; gold never drops below 1.
        var weightless = Cat.Fish("w", weight: 0, gold: 1);
        Assert.Equal(1, FishingCalculations.CalculateGold(weightless, 1, 0.01, new ScriptedRng(0)));
    }

    [Fact]
    public void total_boost_sums_every_slot_of_every_item()
    {
        var box = Cat.Gear("box", EquipmentSlot.TackleBox, type: BoostType.WeightBoost, amount: 0.1, type2: BoostType.WeightBoost, amount2: 0.2);
        var three = new ShopItem("t", "t", "d", 1, BoostType.StarBoost, 0.1, null, BoostType3: BoostType.WeightBoost, BoostAmount3: 0.4);
        Assert.Equal(0.7, FishingCalculations.TotalBoost([box, three], BoostType.WeightBoost), 9);
        Assert.Equal(0.1, FishingCalculations.TotalBoost([box, three], BoostType.StarBoost), 9);
        Assert.Equal(2, FishingCalculations.Boosts(three).Count());
        var defaulted = new ShopItem("d", "d", "d", 1, BoostType.StarBoost, 0.1, null, BoostType2: BoostType.WeightBoost);
        Assert.Equal(0.0, FishingCalculations.TotalBoost([defaulted], BoostType.WeightBoost));
    }

    [Fact]
    public void shop_item_validation_matches_the_upstream_rules()
    {
        Assert.Null(FishingCalculations.ValidateShopItem(Cat.Gear("ok", EquipmentSlot.Rod)));
        Assert.NotNull(FishingCalculations.ValidateShopItem(Cat.Gear("hi", EquipmentSlot.Rod, amount: 5.1)));
        Assert.NotNull(FishingCalculations.ValidateShopItem(Cat.Gear("lo", EquipmentSlot.Rod, amount: -0.81)));
        Assert.NotNull(FishingCalculations.ValidateShopItem(Cat.Gear("nan", EquipmentSlot.Rod, amount: double.NaN)));
        Assert.NotNull(FishingCalculations.ValidateShopItem(Cat.Gear("inf", EquipmentSlot.Rod, amount: double.PositiveInfinity)));
        Assert.Contains("Target fish", FishingCalculations.ValidateShopItem(Cat.Gear("f", EquipmentSlot.Bait, type: BoostType.SpecificFishBoost)));
        Assert.Contains("Target category", FishingCalculations.ValidateShopItem(Cat.Gear("c", EquipmentSlot.Bait, type: BoostType.SpecificCategoryBoost)));
        Assert.Null(FishingCalculations.ValidateShopItem(Cat.Gear("f", EquipmentSlot.Bait, type: BoostType.SpecificFishBoost, targetFish: "x")));
    }
}

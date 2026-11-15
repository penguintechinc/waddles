namespace WaddleBundleSuperpenguinFishing;

/// <summary>
/// The catalog the bundle plays with: fish, shop items, settings and tournament templates. <see cref="Default"/> is the shipped
/// data set. The shop items are ported verbatim from upstream's `FishingShopItemGenerator.GenerateDefaultItems` (names,
/// descriptions, costs, boosts, slots, uses). Upstream keeps the fish list as admin-edited database rows that are NOT part of the
/// source, so the default fish list is authored here; each fish's rarity is the upstream `FishingRarityThresholdRules` rarity of
/// its base gold (thresholds 35/60/110/201/300), enforced by a unit test. Tests inject a custom catalog to reach rarely used
/// paths (durability, repair, targeted boosts) that the default catalog leaves off, exactly as upstream's defaults do.
/// </summary>
public sealed class FishingCatalog
{
    /// <summary>Upstream default rarity thresholds on base gold (Uncommon, Rare, Epic, Legendary, Mythical).</summary>
    public static readonly int[] RarityThresholds = [35, 60, 110, 201, 300];

    public required IReadOnlyList<FishType> Fish { get; init; }

    public required IReadOnlyList<ShopItem> Shop { get; init; }

    public required FishingSettings Settings { get; init; }

    public required IReadOnlyList<TournamentTemplate> Tournaments { get; init; }

    /// <summary>The shipped catalog.</summary>
    public static FishingCatalog Default { get; } = new()
    {
        Fish = DefaultFish(),
        Shop = DefaultShop(),
        Settings = new FishingSettings(),
        Tournaments = DefaultTournaments(),
    };

    /// <summary>Upstream `CalculateRarityFromGold`: the rarity a fish of <paramref name="baseGold"/> belongs to.</summary>
    public static FishRarity RarityFromGold(int baseGold)
    {
        var rarity = FishRarity.Common;
        for (var i = 0; i < RarityThresholds.Length; i++)
        {
            if (baseGold >= RarityThresholds[i])
            {
                rarity = (FishRarity)(i + 1);
            }
        }

        return rarity;
    }

    /// <summary>Finds a fish by id.</summary>
    public FishType? FishById(string id) => Fish.FirstOrDefault(f => f.Id == id);

    /// <summary>Finds a shop item by id.</summary>
    public ShopItem? ShopById(string id) => Shop.FirstOrDefault(s => s.Id == id);

    private static FishType F(string id, string name, double weight, int gold, params string[] categories) =>
        new(id, name, RarityFromGold(gold), weight, gold, categories);

    private static List<FishType> DefaultFish() =>
    [
        F("bluegill", "Bluegill", 0.5, 10, "freshwater", "panfish"),
        F("perch", "Yellow Perch", 0.8, 12, "freshwater", "panfish"),
        F("sunfish", "Sunfish", 0.4, 8, "freshwater", "panfish"),
        F("crappie", "Crappie", 0.9, 15, "freshwater", "panfish"),
        F("carp", "Common Carp", 8.0, 20, "freshwater", "bottom-feeder"),
        F("catfish", "Channel Catfish", 12.0, 25, "freshwater", "bottom-feeder"),
        F("sardine", "Sardine", 0.2, 9, "saltwater", "schooling"),
        F("mackerel", "Mackerel", 1.5, 18, "saltwater", "schooling"),
        F("rainbow-trout", "Rainbow Trout", 3.0, 38, "freshwater", "trout"),
        F("brook-trout", "Brook Trout", 2.0, 42, "freshwater", "trout"),
        F("largemouth-bass", "Largemouth Bass", 5.5, 45, "freshwater", "bass", "game-fish"),
        F("smallmouth-bass", "Smallmouth Bass", 4.0, 48, "freshwater", "bass", "game-fish"),
        F("walleye", "Walleye", 6.0, 52, "freshwater", "game-fish"),
        F("pike", "Northern Pike", 10.0, 58, "freshwater", "predator", "game-fish"),
        F("salmon", "Atlantic Salmon", 12.0, 65, "saltwater", "salmon", "game-fish"),
        F("steelhead", "Steelhead", 9.0, 72, "freshwater", "trout", "game-fish"),
        F("red-snapper", "Red Snapper", 12.0, 80, "saltwater", "reef"),
        F("halibut", "Halibut", 40.0, 95, "saltwater", "bottom-feeder", "flatfish"),
        F("musky", "Muskellunge", 25.0, 105, "freshwater", "predator", "game-fish"),
        F("yellowfin-tuna", "Yellowfin Tuna", 80.0, 125, "saltwater", "tuna", "game-fish"),
        F("mahi-mahi", "Mahi-Mahi", 25.0, 140, "saltwater", "game-fish"),
        F("swordfish", "Swordfish", 200.0, 170, "saltwater", "billfish", "game-fish"),
        F("sturgeon", "Lake Sturgeon", 120.0, 190, "freshwater", "ancient"),
        F("bluefin-tuna", "Bluefin Tuna", 400.0, 230, "saltwater", "tuna", "game-fish"),
        F("marlin", "Blue Marlin", 600.0, 260, "saltwater", "billfish", "game-fish"),
        F("coelacanth", "Coelacanth", 150.0, 295, "saltwater", "ancient", "deep-sea"),
        F("great-white", "Great White Shark", 2000.0, 320, "saltwater", "predator", "deep-sea"),
        F("oarfish", "Giant Oarfish", 500.0, 360, "saltwater", "deep-sea", "ancient"),
        F("golden-koi", "Golden Koi", 15.0, 420, "freshwater", "mythic"),
        F("kraken-tentacle", "Kraken Tentacle", 800.0, 500, "saltwater", "deep-sea", "mythic"),
    ];

    private static ShopItem Gear(string id, string name, string desc, int cost, BoostType type, double amount, EquipmentSlot slot,
        BoostType? type2 = null, double? amount2 = null) =>
        new(id, name, desc, cost, type, amount, slot, BoostType2: type2, BoostAmount2: amount2);

    private static ShopItem Consumable(string id, string name, string desc, int cost, double amount, EquipmentSlot slot, int uses) =>
        new(id, name, desc, cost, BoostType.GeneralRarityBoost, amount, slot, MaxUses: uses);

    private static List<ShopItem> DefaultShop() =>
    [
        Gear("bamboo-rod", "Bamboo Rod", "A basic bamboo fishing rod (+5% rare fish)", 150, BoostType.GeneralRarityBoost, 0.05, EquipmentSlot.Rod),
        Gear("fiberglass-rod", "Fiberglass Rod", "Flexible and durable (+10% rare fish)", 400, BoostType.GeneralRarityBoost, 0.10, EquipmentSlot.Rod),
        Gear("carbon-fiber-rod", "Carbon Fiber Rod", "Lightweight and strong (+15% rare fish)", 1000, BoostType.GeneralRarityBoost, 0.15, EquipmentSlot.Rod),
        Gear("legendary-rod", "Legendary Rod", "The stuff of legends (+25% rare fish)", 2500, BoostType.GeneralRarityBoost, 0.25, EquipmentSlot.Rod),

        Gear("basic-reel", "Basic Reel", "Simple spinning reel (+5% star quality)", 200, BoostType.StarBoost, 0.05, EquipmentSlot.Reel),
        Gear("precision-reel", "Precision Reel", "Smooth drag system (+10% star quality)", 500, BoostType.StarBoost, 0.10, EquipmentSlot.Reel),
        Gear("professional-reel", "Professional Reel", "Tournament-grade precision (+15% star quality)", 1200, BoostType.StarBoost, 0.15, EquipmentSlot.Reel),
        Gear("master-reel", "Master Reel", "Ultimate control and smoothness (+20% star quality)", 3000, BoostType.StarBoost, 0.20, EquipmentSlot.Reel),

        Gear("monofilament-line", "Monofilament Line", "Basic fishing line (+10% weight)", 175, BoostType.WeightBoost, 0.10, EquipmentSlot.Line),
        Gear("braided-line", "Braided Line", "High strength, low stretch (+20% weight)", 450, BoostType.WeightBoost, 0.20, EquipmentSlot.Line),
        Gear("fluorocarbon-line", "Fluorocarbon Line", "Nearly invisible, very strong (+30% weight)", 1100, BoostType.WeightBoost, 0.30, EquipmentSlot.Line),
        Gear("titanium-wire", "Titanium Wire", "Unbreakable fishing wire (+45% weight)", 2800, BoostType.WeightBoost, 0.45, EquipmentSlot.Line),

        Gear("standard-hook", "Standard Hook", "Reliable J-hook (+5% star quality)", 150, BoostType.StarBoost, 0.05, EquipmentSlot.Hook),
        Gear("circle-hook", "Circle Hook", "Self-setting design (+10% star quality)", 400, BoostType.StarBoost, 0.10, EquipmentSlot.Hook),
        Gear("treble-hook", "Treble Hook", "Triple the catching power (+15% star quality)", 1000, BoostType.StarBoost, 0.15, EquipmentSlot.Hook),
        Gear("diamond-hook", "Diamond Hook", "Razor-sharp perfection (+22% star quality)", 2500, BoostType.StarBoost, 0.22, EquipmentSlot.Hook),

        Gear("basic-tackle-box", "Basic Tackle Box", "Organized storage (+5% rarity, +5% weight)", 300, BoostType.GeneralRarityBoost, 0.05, EquipmentSlot.TackleBox, BoostType.WeightBoost, 0.05),
        Gear("pro-tackle-box", "Pro Tackle Box", "Everything you need (+10% rarity, +10% weight)", 800, BoostType.GeneralRarityBoost, 0.10, EquipmentSlot.TackleBox, BoostType.WeightBoost, 0.10),
        Gear("master-tackle-box", "Master Tackle Box", "Complete arsenal (+15% rarity, +15% weight)", 2000, BoostType.GeneralRarityBoost, 0.15, EquipmentSlot.TackleBox, BoostType.WeightBoost, 0.15),

        Gear("landing-net", "Landing Net", "Helps land bigger fish (+15% weight)", 350, BoostType.WeightBoost, 0.15, EquipmentSlot.Net),
        Gear("knotless-net", "Knotless Net", "Gentle on fish, strong hold (+25% weight)", 900, BoostType.WeightBoost, 0.25, EquipmentSlot.Net),
        Gear("tournament-net", "Tournament Net", "Professional-grade netting (+35% weight)", 2200, BoostType.WeightBoost, 0.35, EquipmentSlot.Net),

        Consumable("worms", "Worms", "Classic bait for all fish (+15% rarity, 5 uses)", 75, 0.15, EquipmentSlot.Bait, 5),
        Consumable("minnows", "Minnows", "Live bait for predators (+20% rarity, 5 uses)", 120, 0.20, EquipmentSlot.Bait, 5),
        Consumable("premium-bait", "Premium Bait", "Irresistible to rare fish (+30% rarity, 5 uses)", 200, 0.30, EquipmentSlot.Bait, 5),
        Consumable("golden-bait", "Golden Bait", "Legendary attractant (+50% rarity, 3 uses)", 350, 0.50, EquipmentSlot.Bait, 3),

        Consumable("spoon-lure", "Spoon Lure", "Flashy wobbling action (+12% rarity, 8 uses)", 90, 0.12, EquipmentSlot.Lure, 8),
        Consumable("crankbait", "Crankbait", "Deep diving lure (+18% rarity, 7 uses)", 140, 0.18, EquipmentSlot.Lure, 7),
        Consumable("jerkbait", "Jerkbait", "Erratic swimming motion (+25% rarity, 6 uses)", 220, 0.25, EquipmentSlot.Lure, 6),
        Consumable("topwater-popper", "TopWater Popper", "Surface explosion action (+35% rarity, 5 uses)", 320, 0.35, EquipmentSlot.Lure, 5),
        Consumable("swimbait", "Swimbait", "Realistic swimming lure (+45% rarity, 5 uses)", 400, 0.45, EquipmentSlot.Lure, 5),
    ];

    /// <summary>
    /// Default recurring tournaments (upstream has no defaults: tournaments are admin-created). Each runs <c>DurationMinutes</c> out of
    /// every <c>PeriodMinutes</c>, starting <c>OffsetMinutes</c> after each UTC period boundary; rewards are modest because points are minted (see the README).
    /// </summary>
    private static List<TournamentTemplate> DefaultTournaments() =>
    [
        new("heavyweight", "Heavyweight Hour", "Land the single heaviest fish.", ScoreCategory.Largest, null, [], [], 24 * 60, 60, 18 * 60,
            [new(1, 50, 500), new(2, 25, 250), new(3, 10, 100)]),
        new("gamefish-rush", "Game Fish Rush", "Most game fish caught.", ScoreCategory.MostCatches, null, [], ["game-fish"], 24 * 60, 120, 20 * 60,
            [new(1, 40, 400), new(2, 20, 200), new(3, 10, 100)]),
    ];
}

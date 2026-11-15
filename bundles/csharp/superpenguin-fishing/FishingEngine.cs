namespace WaddleBundleSuperpenguinFishing;

/// <summary>Result of a shop/inventory operation: <c>Ok</c> false carries the player-facing refusal.</summary>
public sealed record OpResult(bool Ok, string Message);

/// <summary>
/// The fishing rules over one <see cref="PlayerState"/>, ported from upstream `FishingGameplayService.PerformFishingAttempt`,
/// `FishingInventoryService` (equip, use/durability consumption, snap losses, replacement, repair, sell, purchase),
/// `FishingInventorySellRules` and `FishingShopService` tier ranking. Pure: all randomness comes from the supplied
/// <see cref="IFishRng"/>, all state from the passed player, so the same seed plus the same state always yields the same result.
/// Upstream refusals that were exceptions are returned as <see cref="OpResult"/> chat text instead.
/// </summary>
public sealed class FishingEngine(FishingCatalog catalog)
{
    /// <summary>Upstream sell rate: an unequipped permanent item sells for 15% of its cost (scaled by remaining durability).</summary>
    public const double SellRate = 0.15;

    /// <summary>The most items one player may hold (bounds the per-player kv value).</summary>
    public const int MaxInventory = 200;

    /// <summary>Upstream clamps gold to a 32-bit total.</summary>
    private const long GoldCeiling = int.MaxValue;

    /// <summary>Runs one cast (upstream `PerformFishingAttempt`): accident checks in upstream order, then the catch, then item wear.</summary>
    public CastResult Cast(PlayerState player, IFishRng rng)
    {
        var settings = catalog.Settings;
        var equipped = EquippedItems(player);
        var bySlot = equipped
            .Where(e => e.Shop.Slot.HasValue)
            .GroupBy(e => e.Shop.Slot!.Value)
            .ToDictionary(g => g.Key, g => g.OrderByDescending(e => e.Item.Id).First());

        if (bySlot.TryGetValue(EquipmentSlot.Reel, out var reel) && !reel.Shop.DisableBreaking && !reel.Item.IsBroken
            && Chance(rng) < Normalize(settings.ReelJamChance, 0.0))
        {
            return Accident(player, CastKind.ReelJammed, SnapLoss.Reel);
        }

        if (bySlot.TryGetValue(EquipmentSlot.TackleBox, out var box) && !box.Shop.DisableBreaking && !box.Item.IsBroken
            && Chance(rng) < Normalize(settings.TackleBoxLostChance, 0.0))
        {
            return Accident(player, CastKind.TackleBoxLost, SnapLoss.TackleBox);
        }

        if (bySlot.TryGetValue(EquipmentSlot.Net, out var net) && !net.Shop.DisableBreaking && !net.Item.IsBroken
            && Chance(rng) < Normalize(settings.NetBreakChance, 0.0))
        {
            return Accident(player, CastKind.NetBroken, SnapLoss.Net);
        }

        var canRodSnap = !bySlot.TryGetValue(EquipmentSlot.Rod, out var rod) || (!rod.Shop.DisableBreaking && !rod.Item.IsBroken);
        if (canRodSnap && Chance(rng) < Normalize(settings.RodSnapChance, 0.0005))
        {
            return Accident(player, CastKind.RodSnapped, SnapLoss.Rod);
        }

        var canLineSnap = !bySlot.TryGetValue(EquipmentSlot.Line, out var line) || (!line.Shop.DisableBreaking && !line.Item.IsBroken);
        if (canLineSnap && Chance(rng) < Normalize(settings.LineSnapChance, 0.02))
        {
            return Accident(player, CastKind.LineSnapped, SnapLoss.Line);
        }

        var active = equipped.Where(e => !e.Item.IsBroken).Select(e => e.Shop).ToList();
        var fish = catalog.Fish.ToList();
        var chosen = FishingCalculations.SelectRandomFish(fish, settings, active, rng);
        var stars = FishingCalculations.CalculateStars(active, rng);
        var weight = FishingCalculations.CalculateWeight(chosen, stars, active, rng);
        var gold = FishingCalculations.CalculateGold(chosen, stars, weight, rng);

        player.Casts++;
        player.Gold = Math.Min(GoldCeiling, player.Gold + gold);
        player.TotalGoldEarned += gold;
        player.Counts[chosen.Id] = player.Counts.GetValueOrDefault(chosen.Id) + 1;
        if (!player.Best.TryGetValue(chosen.Id, out var best) || stars > best.Stars || (stars == best.Stars && weight > best.Weight))
        {
            player.Best[chosen.Id] = new BestCatch(stars, weight);
        }

        ConsumeUses(player, equipped.Select(e => e.Item).ToList());
        // Upstream re-queries still-equipped items between the two steps: a spent bait/lure is already gone.
        var broken = ConsumeDurability(player, equipped.Select(e => e.Item).Where(i => i.Equipped).ToList());
        return new CastResult(CastKind.Caught, chosen.Id, stars, weight, gold, 0, [], broken);
    }

    /// <summary>Buys <paramref name="quantity"/> of a shop item with gold (upstream `PurchaseBoost`): the debit and the new items commit together.</summary>
    public OpResult Buy(PlayerState player, ShopItem item, int quantity)
    {
        if (quantity < 1)
        {
            return new(false, "Purchase quantity must be at least 1.");
        }

        if (!item.Enabled || item.AdminOnly)
        {
            return new(false, "That item is not available for purchase.");
        }

        if (item.MaxUses is null && quantity != 1)
        {
            return new(false, "Only limited-use items can be bought in multiples.");
        }

        if (item.MaxUses is <= 0)
        {
            return new(false, "That item is misconfigured (no uses).");
        }

        var copies = item.MaxUses.HasValue ? quantity : 1;
        if (player.Items.Count + copies > MaxInventory)
        {
            return new(false, $"Your inventory is full ({MaxInventory} items max).");
        }

        var total = (long)item.Cost * quantity;
        if (player.Gold < total)
        {
            return new(false, $"Not enough gold: {item.Name} x{quantity} costs {total}g, you have {player.Gold}g.");
        }

        player.Gold -= total;
        var firstId = player.NextItemId;
        for (var i = 0; i < copies; i++)
        {
            player.Items.Add(new OwnedItem
            {
                Id = player.NextItemId++,
                ShopId = item.Id,
                RemainingUses = item.MaxUses ?? -1,
                Durability = item.MaxDurability.HasValue ? item.MaxDurability.Value : null,
            });
        }

        return new(true, $"Bought {item.Name} x{copies} for {total}g (item #{firstId}). Equip it with !fishequip {firstId}. Gold left: {player.Gold}g.");
    }

    /// <summary>Sells an unequipped, permanent item for <see cref="SellPrice"/> (upstream `SellItem` + `FishingInventorySellRules`).</summary>
    public OpResult Sell(PlayerState player, int itemId)
    {
        var owned = player.Items.FirstOrDefault(i => i.Id == itemId);
        var shop = owned is null ? null : catalog.ShopById(owned.ShopId);
        if (owned is null || shop is null)
        {
            return new(false, "Item not found.");
        }

        if (owned.Equipped)
        {
            return new(false, "Cannot sell an equipped item.");
        }

        if (owned.RemainingUses >= 0)
        {
            return new(false, "Cannot sell items with limited usage.");
        }

        if (shop.MaxUses.HasValue)
        {
            return new(false, "Cannot sell consumable items.");
        }

        var price = SellPrice(shop, owned);
        player.Gold = Math.Min(GoldCeiling, player.Gold + price);
        player.Items.Remove(owned);
        return new(true, $"Sold {shop.Name} for {price}g. Gold: {player.Gold}g.");
    }

    /// <summary>Sell price: 15% of cost, floored, scaled by remaining durability when the item has any.</summary>
    public static int SellPrice(ShopItem shop, OwnedItem owned)
    {
        var basePrice = shop.Cost * SellRate;
        if (owned.Durability is { } d && shop.MaxDurability is > 0)
        {
            var ratio = Math.Clamp(d / shop.MaxDurability.Value, 0.0, 1.0);
            return (int)Math.Floor(basePrice * ratio);
        }

        return (int)basePrice;
    }

    /// <summary>Equips an item, unequipping whatever held its slot (upstream `EquipItem`).</summary>
    public OpResult Equip(PlayerState player, int itemId)
    {
        var owned = player.Items.FirstOrDefault(i => i.Id == itemId);
        var shop = owned is null ? null : catalog.ShopById(owned.ShopId);
        if (owned is null || shop is null)
        {
            return new(false, "Item not found.");
        }

        if (shop.MaxUses.HasValue && owned.RemainingUses == 0)
        {
            return new(false, "That item has no uses left.");
        }

        if (shop.Slot is { } slot)
        {
            foreach (var other in player.Items.Where(i => i.Equipped && catalog.ShopById(i.ShopId)?.Slot == slot))
            {
                other.Equipped = false;
            }
        }

        owned.Equipped = true;
        return new(true, $"Equipped {shop.Name} (#{owned.Id}).");
    }

    /// <summary>Unequips an item (upstream `UnequipItem`).</summary>
    public OpResult Unequip(PlayerState player, int itemId)
    {
        var owned = player.Items.FirstOrDefault(i => i.Id == itemId);
        if (owned is null)
        {
            return new(false, "Item not found.");
        }

        owned.Equipped = false;
        return new(true, $"Unequipped #{owned.Id}.");
    }

    /// <summary>Gold to restore an item to full durability (upstream `CalculateRepairCost`); 0 when repair is off or nothing is missing.</summary>
    public static int RepairCost(ShopItem shop, double currentDurability, double multiplier)
    {
        if (multiplier <= 0 || shop.MaxDurability is not > 0)
        {
            return 0;
        }

        var missing = Math.Max(0.0, (shop.MaxDurability.Value - currentDurability) / shop.MaxDurability.Value);
        return missing <= 0.0 ? 0 : Math.Max(1, (int)Math.Ceiling(shop.Cost * missing * multiplier));
    }

    /// <summary>Repairs one item (<paramref name="itemId"/> set) or every equipped item (null), charging gold (upstream `RepairItem` / `RepairAllEquippedItems`).</summary>
    public OpResult Repair(PlayerState player, int? itemId)
    {
        var multiplier = catalog.Settings.RepairCostMultiplier;
        if (multiplier <= 0)
        {
            return new(false, "Repair is currently disabled.");
        }

        List<(OwnedItem Item, ShopItem Shop, int Cost)> jobs = [];
        var candidates = itemId is { } id ? player.Items.Where(i => i.Id == id).ToList() : player.Items.Where(i => i.Equipped).ToList();
        if (itemId is not null && candidates.Count == 0)
        {
            return new(false, "Item not found.");
        }

        foreach (var item in candidates)
        {
            var shop = catalog.ShopById(item.ShopId);
            if (shop?.MaxDurability is not > 0 || item.Durability is not { } current)
            {
                if (itemId is not null)
                {
                    return new(false, "This item does not have durability.");
                }

                continue;
            }

            if (current >= shop.MaxDurability.Value)
            {
                if (itemId is not null)
                {
                    return new(false, "That item is already at full durability.");
                }

                continue;
            }

            jobs.Add((item, shop, RepairCost(shop, current, multiplier)));
        }

        if (jobs.Count == 0)
        {
            return new(true, "Nothing to repair.");
        }

        var total = jobs.Sum(j => (long)j.Cost);
        if (player.Gold < total)
        {
            return new(false, $"Not enough gold to repair: it costs {total}g, you have {player.Gold}g.");
        }

        player.Gold -= total;
        foreach (var (item, shop, _) in jobs)
        {
            item.Durability = shop.MaxDurability!.Value;
        }

        return new(true, $"Repaired {jobs.Count} item(s) for {total}g. Gold left: {player.Gold}g.");
    }

    /// <summary>
    /// Price-rank tiers of every shop item (upstream `CalculateDynamicTiers`): within a slot, permanent enabled items split by cost
    /// into quartiles (Top, High, Mid, Entry); limited-use items are Consumable; everything else is Entry.
    /// </summary>
    public Dictionary<string, EquipmentTier> Tiers()
    {
        var map = new Dictionary<string, EquipmentTier>();
        var bySlot = catalog.Shop
            .Where(i => i.MaxUses is null && i.Enabled && i.Slot.HasValue)
            .GroupBy(i => i.Slot!.Value);
        foreach (var group in bySlot)
        {
            var ranked = group.OrderByDescending(i => i.Cost).ToList();
            var n = ranked.Count;
            var top = Math.Max(1, (int)Math.Ceiling(n * 0.25));
            var high = Math.Max(2, (int)Math.Ceiling(n * 0.50));
            var mid = Math.Max(3, (int)Math.Ceiling(n * 0.75));
            for (var rank = 0; rank < n; rank++)
            {
                map[ranked[rank].Id] = rank < top ? EquipmentTier.Top : rank < high ? EquipmentTier.High : rank < mid ? EquipmentTier.Mid : EquipmentTier.Entry;
            }
        }

        foreach (var item in catalog.Shop.Where(i => i.MaxUses.HasValue))
        {
            map[item.Id] = EquipmentTier.Consumable;
        }

        foreach (var item in catalog.Shop.Where(i => !map.ContainsKey(i.Id)))
        {
            map[item.Id] = EquipmentTier.Entry;
        }

        return map;
    }

    /// <summary>How buying <paramref name="item"/> compares with what the player has equipped in its slot (upstream `FishingTierComparisonRules`).</summary>
    public string Compare(ShopItem item, PlayerState player, IReadOnlyDictionary<string, EquipmentTier> tiers)
    {
        if (item.MaxUses.HasValue || !item.Slot.HasValue)
        {
            return string.Empty;
        }

        var held = player.Items.Where(i => i.Equipped).Select(i => catalog.ShopById(i.ShopId)).FirstOrDefault(s => s?.Slot == item.Slot);
        if (held is null)
        {
            return "new";
        }

        if (held.Id == item.Id)
        {
            return "equipped";
        }

        var a = tiers.GetValueOrDefault(item.Id, EquipmentTier.Entry);
        var b = tiers.GetValueOrDefault(held.Id, EquipmentTier.Entry);
        return a > b ? "upgrade" : a < b ? "downgrade" : "sidegrade";
    }

    private List<(OwnedItem Item, ShopItem Shop)> EquippedItems(PlayerState player)
    {
        var result = new List<(OwnedItem, ShopItem)>();
        foreach (var item in player.Items.Where(i => i.Equipped))
        {
            var shop = catalog.ShopById(item.ShopId);
            if (shop is null)
            {
                continue;
            }

            ReconcileDurability(item, shop);
            result.Add((item, shop));
        }

        return result;
    }

    private static void ReconcileDurability(OwnedItem item, ShopItem shop)
    {
        if (shop.MaxDurability is { } max)
        {
            item.Durability = item.Durability is null ? max : Math.Min(item.Durability.Value, max);
        }
        else
        {
            item.Durability = null;
        }
    }

    private static double Chance(IFishRng rng) => rng.Next(0, 10_000_000) / 10_000_000.0;

    private static double Normalize(double chance, double fallback) =>
        !double.IsNaN(chance) && !double.IsInfinity(chance) && chance >= 0 && chance <= 1 ? chance : fallback;

    private enum SnapLoss
    {
        Line,
        Rod,
        Reel,
        TackleBox,
        Net,
    }

    private CastResult Accident(PlayerState player, CastKind kind, SnapLoss loss)
    {
        var lostItems = new List<string>();
        decimal goldLost = 0m;
        foreach (var item in player.Items.Where(i => i.Equipped).ToList())
        {
            var shop = catalog.ShopById(item.ShopId);
            if (shop is null)
            {
                continue;
            }

            ReconcileDurability(item, shop);
            if (shop.DisableBreaking)
            {
                continue;
            }

            var slot = shop.Slot;
            var targetLost = loss switch
            {
                SnapLoss.Rod => slot is EquipmentSlot.Rod or EquipmentSlot.Line or EquipmentSlot.Hook,
                SnapLoss.Line => slot is EquipmentSlot.Line or EquipmentSlot.Hook,
                SnapLoss.Reel => slot == EquipmentSlot.Reel,
                SnapLoss.TackleBox => slot == EquipmentSlot.TackleBox,
                _ => slot == EquipmentSlot.Net,
            };
            if (targetLost)
            {
                goldLost += Math.Round((decimal)shop.Cost, 2, MidpointRounding.AwayFromZero);
                lostItems.Add(shop.Name);
                player.Items.Remove(item);
                continue;
            }

            if (loss is not (SnapLoss.Line or SnapLoss.Rod) || slot is not (EquipmentSlot.Bait or EquipmentSlot.Lure))
            {
                continue;
            }

            if (item.RemainingUses == -1)
            {
                goldLost += Math.Round((decimal)shop.Cost, 2, MidpointRounding.AwayFromZero);
                lostItems.Add(shop.Name);
                player.Items.Remove(item);
                continue;
            }

            if (item.RemainingUses > 0)
            {
                item.RemainingUses--;
                var perUse = shop.MaxUses is > 0 ? (decimal)shop.Cost / shop.MaxUses.Value : shop.Cost;
                goldLost += Math.Round(perUse, 2, MidpointRounding.AwayFromZero);
                lostItems.Add($"{shop.Name} (1 use)");
            }

            if (item.RemainingUses <= 0)
            {
                RemoveAndEquipReplacement(player, item);
            }
        }

        var lost = (long)Math.Round(goldLost, 0, MidpointRounding.AwayFromZero);
        player.TotalGoldLost += lost;
        return new CastResult(kind, string.Empty, 0, 0, 0, lost, [.. lostItems], []);
    }

    private void ConsumeUses(PlayerState player, List<OwnedItem> equipped)
    {
        foreach (var item in equipped.Where(i => i.RemainingUses != -1))
        {
            if (item.RemainingUses > 0)
            {
                item.RemainingUses--;
            }

            if (item.RemainingUses <= 0)
            {
                RemoveAndEquipReplacement(player, item);
            }
        }
    }

    private BrokenItem[] ConsumeDurability(PlayerState player, List<OwnedItem> equipped)
    {
        var broken = new List<BrokenItem>();
        var multiplier = catalog.Settings.RepairCostMultiplier;
        foreach (var item in equipped)
        {
            var shop = catalog.ShopById(item.ShopId);
            if (shop?.MaxDurability is null || item.Durability is not { } before)
            {
                continue;
            }

            var loss = Math.Max(0.0, shop.DurabilityLossPerUse ?? 1.0);
            item.Durability = Math.Max(0.0, before - loss);
            if (before > 0.0 && item.Durability.Value <= 0.0)
            {
                var replaced = false;
                if (multiplier > 0)
                {
                    item.Durability = 0.0;
                }
                else
                {
                    replaced = RemoveAndEquipReplacement(player, item);
                }

                broken.Add(new BrokenItem(shop.Name, replaced));
            }
        }

        return [.. broken];
    }

    /// <summary>Removes a spent item and equips the oldest spare of the same shop item (upstream `RemoveAndEquipReplacement`).</summary>
    private static bool RemoveAndEquipReplacement(PlayerState player, OwnedItem item)
    {
        item.Equipped = false;
        player.Items.Remove(item);
        var replacement = player.Items
            .Where(i => i.ShopId == item.ShopId && i.Id != item.Id && !i.Equipped && i.RemainingUses != 0)
            .OrderBy(i => i.Id)
            .FirstOrDefault();
        if (replacement is null)
        {
            return false;
        }

        replacement.Equipped = true;
        return true;
    }
}

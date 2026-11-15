using Xunit;

namespace WaddleBundleSuperpenguinFishing.Tests;

public class EngineTests
{
    private static OwnedItem Own(PlayerState p, string shopId, bool equipped = true, int uses = -1, double? durability = null)
    {
        var item = new OwnedItem { Id = p.NextItemId++, ShopId = shopId, Equipped = equipped, RemainingUses = uses, Durability = durability };
        p.Items.Add(item);
        return item;
    }

    private static FishingEngine Engine(FishingSettings settings, params ShopItem[] shop) => new(Cat.Of(settings, shop));

    [Fact]
    public void a_catch_updates_gold_counts_best_and_lifetime_totals()
    {
        var engine = new FishingEngine(Cat.Of());
        var p = new PlayerState();
        var rng = new SeededRng(5);

        var first = engine.Cast(p, rng);

        Assert.Equal(CastKind.Caught, first.Kind);
        Assert.Equal("minnow", first.FishId);
        Assert.Equal(first.Gold, p.Gold);
        Assert.Equal(first.Gold, p.TotalGoldEarned);
        Assert.Equal(1, p.Casts);
        Assert.Equal(1, p.Counts["minnow"]);
        Assert.Equal(new BestCatch(first.Stars, first.Weight), p.Best["minnow"]);
        Assert.InRange(first.Stars, 1, 3);
        Assert.True(first.Weight >= 0.01);

        // A worse catch never replaces the best; a better one does.
        p.Best["minnow"] = new BestCatch(3, 999);
        engine.Cast(p, new SeededRng(6));
        Assert.Equal(new BestCatch(3, 999), p.Best["minnow"]);
        p.Best["minnow"] = new BestCatch(1, 0.01);
        engine.Cast(p, new SeededRng(7));
        Assert.True(p.Best["minnow"].Weight > 0.01 || p.Best["minnow"].Stars > 1);
        Assert.Equal(3, p.Casts);
    }

    [Fact]
    public void a_same_star_heavier_catch_replaces_the_best_and_a_lighter_one_does_not()
    {
        var engine = new FishingEngine(Cat.Of(fish: [Cat.Fish("minnow", weight: 10)]));
        var p = new PlayerState();
        // Force one-star casts: star roll 9999 -> 1 star; weight roll decides the weight.
        var low = engine.Cast(p, new ScriptedRng(0, 0, 0, 0, 9999, 0, 0));
        Assert.Equal(1, low.Stars);
        var high = engine.Cast(p, new ScriptedRng(0, 0, 0, 0, 9999, 9999, 0));
        Assert.True(high.Weight > low.Weight);
        Assert.Equal(high.Weight, p.Best["minnow"].Weight);
        engine.Cast(p, new ScriptedRng(0, 0, 0, 0, 9999, 0, 0));
        Assert.Equal(high.Weight, p.Best["minnow"].Weight);
    }

    [Fact]
    public void gold_is_clamped_to_the_upstream_32_bit_total()
    {
        var engine = new FishingEngine(Cat.Of());
        var p = new PlayerState { Gold = int.MaxValue - 1 };
        engine.Cast(p, new SeededRng(1));
        Assert.Equal(int.MaxValue, p.Gold);
    }

    [Fact]
    public void reel_jam_tackle_box_loss_and_net_break_each_destroy_only_their_own_slot()
    {
        var reel = Cat.Gear("reel", EquipmentSlot.Reel, cost: 200);
        var box = Cat.Gear("box", EquipmentSlot.TackleBox, cost: 300);
        var net = Cat.Gear("net", EquipmentSlot.Net, cost: 400);
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 100);

        foreach (var (settings, kind, lostName, cost) in new[]
        {
            (Cat.Calm(reel: 1), CastKind.ReelJammed, "reel", 200),
            (Cat.Calm(box: 1), CastKind.TackleBoxLost, "box", 300),
            (Cat.Calm(net: 1), CastKind.NetBroken, "net", 400),
        })
        {
            var engine = Engine(settings, reel, box, net, rod);
            var p = new PlayerState { Gold = 50 };
            foreach (var id in new[] { "reel", "box", "net", "rod" })
            {
                Own(p, id);
            }

            var result = engine.Cast(p, new SeededRng(1));

            Assert.Equal(kind, result.Kind);
            Assert.Equal([lostName], result.LostItems);
            Assert.Equal(cost, result.GoldLost);
            Assert.Equal(cost, p.TotalGoldLost);
            Assert.Equal(3, p.Items.Count);
            Assert.DoesNotContain(p.Items, i => i.ShopId == lostName);
            Assert.Equal(50, p.Gold);
            Assert.Equal(0, p.Casts);
        }
    }

    [Fact]
    public void accidents_need_the_matching_item_equipped()
    {
        // Reel/box/net chances of 1.0 do nothing for a player without those items.
        var engine = Engine(Cat.Calm(reel: 1, box: 1, net: 1), Cat.Gear("reel", EquipmentSlot.Reel));
        var p = new PlayerState();
        Own(p, "reel", equipped: false);
        Assert.Equal(CastKind.Caught, engine.Cast(p, new SeededRng(1)).Kind);
    }

    [Fact]
    public void rod_snap_destroys_rod_line_hook_and_one_use_of_each_bait_and_lure()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 100);
        var line = Cat.Gear("line", EquipmentSlot.Line, cost: 50);
        var hook = Cat.Gear("hook", EquipmentSlot.Hook, cost: 25);
        var reel = Cat.Gear("reel", EquipmentSlot.Reel, cost: 999);
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, cost: 100, maxUses: 5);
        var lure = Cat.Gear("lure", EquipmentSlot.Lure, cost: 80, maxUses: 4);
        var engine = Engine(Cat.Calm(rod: 1), rod, line, hook, reel, bait, lure);
        var p = new PlayerState();
        foreach (var id in new[] { "rod", "line", "hook", "reel" })
        {
            Own(p, id);
        }

        var baitItem = Own(p, "bait", uses: 5);
        var lureItem = Own(p, "lure", uses: 4);

        var result = engine.Cast(p, new SeededRng(1));

        Assert.Equal(CastKind.RodSnapped, result.Kind);
        Assert.Equal(["rod", "line", "hook", "bait (1 use)", "lure (1 use)"], result.LostItems.OrderBy(x => x == "rod" ? 0 : x == "line" ? 1 : x == "hook" ? 2 : x.StartsWith("bait") ? 3 : 4));
        Assert.Equal(100 + 50 + 25 + 20 + 20, result.GoldLost); // bait 100/5, lure 80/4 per use
        Assert.Equal(4, baitItem.RemainingUses);
        Assert.Equal(3, lureItem.RemainingUses);
        Assert.Contains(p.Items, i => i.ShopId == "reel");
        Assert.DoesNotContain(p.Items, i => i.ShopId is "rod" or "line" or "hook");
    }

    [Fact]
    public void line_snap_keeps_the_rod_and_unlimited_bait_is_fully_lost()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 100);
        var line = Cat.Gear("line", EquipmentSlot.Line, cost: 50);
        var hook = Cat.Gear("hook", EquipmentSlot.Hook, cost: 25);
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, cost: 70);
        var engine = Engine(Cat.Calm(line: 1), rod, line, hook, bait);
        var p = new PlayerState();
        Own(p, "rod");
        Own(p, "line");
        Own(p, "hook");
        Own(p, "bait", uses: -1);

        var result = engine.Cast(p, new SeededRng(1));

        Assert.Equal(CastKind.LineSnapped, result.Kind);
        Assert.Equal(50 + 25 + 70, result.GoldLost);
        Assert.Equal(["rod"], p.Items.Select(i => i.ShopId));
    }

    [Fact]
    public void a_snapped_last_use_of_bait_equips_the_spare_of_the_same_item()
    {
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, cost: 50, maxUses: 2);
        var engine = Engine(Cat.Calm(line: 1), bait);
        var p = new PlayerState();
        Own(p, "bait", uses: 1);
        var spare = Own(p, "bait", equipped: false, uses: 2);

        var result = engine.Cast(p, new SeededRng(1));

        Assert.Equal(25, result.GoldLost);
        Assert.Single(p.Items);
        Assert.True(spare.Equipped);
    }

    [Fact]
    public void unbreakable_and_already_broken_items_are_immune_and_gate_their_accident()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, disableBreaking: true);
        var line = Cat.Gear("line", EquipmentSlot.Line, disableBreaking: true);
        var reel = Cat.Gear("reel", EquipmentSlot.Reel, disableBreaking: true);
        var box = Cat.Gear("box", EquipmentSlot.TackleBox, maxDurability: 10, loss: 1);
        var net = Cat.Gear("net", EquipmentSlot.Net, disableBreaking: true);
        // Every chance 1: unbreakable rod/line/reel/net never trigger; the broken tackle box cannot be lost either.
        var engine = Engine(Cat.Calm(rod: 1, line: 1, reel: 1, box: 1, net: 1), rod, line, reel, box, net);
        var p = new PlayerState();
        foreach (var id in new[] { "rod", "line", "reel", "net" })
        {
            Own(p, id);
        }

        Own(p, "box", durability: 0);

        var result = engine.Cast(p, new SeededRng(1));

        Assert.Equal(CastKind.Caught, result.Kind);
        Assert.Equal(5, p.Items.Count);
    }

    [Fact]
    public void an_unbreakable_item_survives_an_accident_that_destroys_its_slot_mates()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod);
        var line = Cat.Gear("line", EquipmentSlot.Line, disableBreaking: true);
        var engine = Engine(Cat.Calm(rod: 1), rod, line);
        var p = new PlayerState();
        Own(p, "rod");
        Own(p, "line");

        var result = engine.Cast(p, new SeededRng(1));

        Assert.Equal(CastKind.RodSnapped, result.Kind);
        Assert.Equal(["line"], p.Items.Select(i => i.ShopId));
    }

    [Fact]
    public void a_snap_with_nothing_equipped_loses_nothing()
    {
        var engine = Engine(Cat.Calm(rod: 1));
        var p = new PlayerState();
        var result = engine.Cast(p, new SeededRng(1));
        Assert.Equal(CastKind.RodSnapped, result.Kind);
        Assert.Empty(result.LostItems);
        Assert.Equal(0, result.GoldLost);
    }

    [Fact]
    public void equipped_items_of_unknown_shop_items_are_ignored()
    {
        var engine = Engine(Cat.Calm(rod: 1));
        var p = new PlayerState();
        Own(p, "ghost");
        var result = engine.Cast(p, new SeededRng(1));
        Assert.Equal(CastKind.RodSnapped, result.Kind);
        Assert.Single(p.Items);
    }

    [Fact]
    public void a_catch_consumes_a_use_and_a_spent_item_is_replaced_by_the_oldest_spare()
    {
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, maxUses: 2);
        var engine = Engine(Cat.Calm(), bait);
        var p = new PlayerState();
        var active = Own(p, "bait", uses: 2);
        var spare = Own(p, "bait", equipped: false, uses: 2);
        var emptySpare = Own(p, "bait", equipped: false, uses: 0);

        engine.Cast(p, new SeededRng(1));
        Assert.Equal(1, active.RemainingUses);
        Assert.False(spare.Equipped);

        engine.Cast(p, new SeededRng(2));
        Assert.DoesNotContain(active, p.Items);
        Assert.True(spare.Equipped);
        Assert.False(emptySpare.Equipped);

        // Unlimited (-1) items are untouched by use consumption.
        var rod = Cat.Gear("rod", EquipmentSlot.Rod);
        var p2 = new PlayerState();
        var inf = Own(p2, "rod");
        Engine(Cat.Calm(), rod).Cast(p2, new SeededRng(1));
        Assert.Equal(-1, inf.RemainingUses);
    }

    [Fact]
    public void durability_wears_and_a_broken_item_is_removed_with_a_replacement_when_repair_is_off()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, maxDurability: 2, loss: 1);
        var engine = Engine(Cat.Calm(), rod);
        var p = new PlayerState();
        var worn = Own(p, "rod", durability: null);
        var spare = Own(p, "rod", equipped: false);

        var first = engine.Cast(p, new SeededRng(1));
        Assert.Equal(1.0, worn.Durability);
        Assert.Empty(first.Broken);

        var second = engine.Cast(p, new SeededRng(2));
        Assert.Equal([new BrokenItem("rod", true)], second.Broken);
        Assert.DoesNotContain(worn, p.Items);
        Assert.True(spare.Equipped);
    }

    [Fact]
    public void with_repair_on_a_broken_item_is_kept_at_zero_and_stops_boosting()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, maxDurability: 1, loss: 5);
        var engine = Engine(Cat.Calm(repair: 1.0), rod);
        var p = new PlayerState();
        var item = Own(p, "rod", durability: 1);

        var result = engine.Cast(p, new SeededRng(1));

        Assert.Equal([new BrokenItem("rod", false)], result.Broken);
        Assert.Contains(item, p.Items);
        Assert.True(item.IsBroken);
        Assert.Equal(0.0, item.Durability);
        // Already at zero: no second "broke" report, durability stays 0.
        Assert.Empty(engine.Cast(p, new SeededRng(2)).Broken);
    }

    [Fact]
    public void durability_is_reconciled_to_the_shop_max_and_cleared_when_the_item_has_none()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, maxDurability: 5, loss: 0);
        var plain = Cat.Gear("line", EquipmentSlot.Line);
        var engine = Engine(Cat.Calm(), rod, plain);
        var p = new PlayerState();
        var over = Own(p, "rod", durability: 99);
        var stray = Own(p, "line", durability: 3);
        engine.Cast(p, new SeededRng(1));
        Assert.Equal(5.0, over.Durability);
        Assert.Null(stray.Durability);
    }

    [Fact]
    public void out_of_range_chances_fall_back_to_the_upstream_defaults()
    {
        // NaN/negative/>1 chances are replaced by the defaults (0.0005 rod, 0.02 line, 0 others): essentially never snap.
        var settings = new FishingSettings(LineSnapChance: double.NaN, RodSnapChance: -1, ReelJamChance: 2, TackleBoxLostChance: double.PositiveInfinity, NetBreakChance: 5);
        var engine = Engine(settings, Cat.Gear("reel", EquipmentSlot.Reel), Cat.Gear("box", EquipmentSlot.TackleBox), Cat.Gear("net", EquipmentSlot.Net));
        var p = new PlayerState();
        Own(p, "reel");
        Own(p, "box");
        Own(p, "net");
        // Chance roll of 0.0 is < 0.02 (line default) but not < 0 (reel/box/net default) nor < 0.0005 only when roll >= 5000.
        var result = engine.Cast(p, new ScriptedRng(0, 0, 0, 9_999_999, 9_999_999));
        Assert.Equal(CastKind.Caught, result.Kind);
        var line = engine.Cast(new PlayerState(), new ScriptedRng(9_999_999, 100_000));
        Assert.Equal(CastKind.LineSnapped, line.Kind);
    }

    [Fact]
    public void buy_debits_gold_and_adds_items_with_uses_and_durability()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 100, maxDurability: 8);
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, cost: 30, maxUses: 5);
        var engine = Engine(Cat.Calm(), rod, bait);
        var p = new PlayerState { Gold = 500 };

        var r1 = engine.Buy(p, rod, 1);
        Assert.True(r1.Ok, r1.Message);
        Assert.Equal(400, p.Gold);
        Assert.Equal(8.0, p.Items[0].Durability);
        Assert.Equal(-1, p.Items[0].RemainingUses);

        var r2 = engine.Buy(p, bait, 3);
        Assert.True(r2.Ok, r2.Message);
        Assert.Equal(310, p.Gold);
        Assert.Equal(4, p.Items.Count);
        Assert.All(p.Items.Skip(1), i => Assert.Equal(5, i.RemainingUses));
        Assert.Equal([1, 2, 3, 4], p.Items.Select(i => i.Id));
        Assert.Equal(5, p.NextItemId);
    }

    [Theory]
    [InlineData(0, "at least 1")]
    [InlineData(2, "multiples")]
    public void buy_refuses_bad_quantities_for_permanent_items(int quantity, string message)
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod);
        var p = new PlayerState { Gold = 9999 };
        var r = Engine(Cat.Calm(), rod).Buy(p, rod, quantity);
        Assert.False(r.Ok);
        Assert.Contains(message, r.Message);
        Assert.Equal(9999, p.Gold);
        Assert.Empty(p.Items);
    }

    [Fact]
    public void buy_refuses_unavailable_misconfigured_full_and_unaffordable_purchases_without_charging()
    {
        var disabled = Cat.Gear("d", EquipmentSlot.Rod, enabled: false);
        var admin = Cat.Gear("a", EquipmentSlot.Rod, adminOnly: true);
        var noUses = Cat.Gear("n", EquipmentSlot.Bait, maxUses: 0);
        var pricey = Cat.Gear("p", EquipmentSlot.Rod, cost: 1000);
        var bait = Cat.Gear("b", EquipmentSlot.Bait, cost: 1, maxUses: 1);
        var engine = Engine(Cat.Calm(), disabled, admin, noUses, pricey, bait);
        var p = new PlayerState { Gold = 10 };

        Assert.Contains("not available", engine.Buy(p, disabled, 1).Message);
        Assert.Contains("not available", engine.Buy(p, admin, 1).Message);
        Assert.Contains("misconfigured", engine.Buy(p, noUses, 1).Message);
        Assert.Contains("Not enough gold", engine.Buy(p, pricey, 1).Message);

        for (var i = 0; i < FishingEngine.MaxInventory; i++)
        {
            p.Items.Add(new OwnedItem { Id = i + 100, ShopId = "b" });
        }

        Assert.Contains("inventory is full", engine.Buy(p, bait, 1).Message);
        Assert.Equal(10, p.Gold);
        Assert.Equal(FishingEngine.MaxInventory, p.Items.Count);
    }

    [Fact]
    public void sell_pays_fifteen_percent_scaled_by_durability_and_refuses_equipped_limited_or_unknown_items()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 1000);
        var worn = Cat.Gear("worn", EquipmentSlot.Line, cost: 1000, maxDurability: 10);
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, cost: 100, maxUses: 3);
        var engine = Engine(Cat.Calm(), rod, worn, bait);
        var p = new PlayerState { Gold = 5 };
        var plain = Own(p, "rod", equipped: false);
        var half = Own(p, "worn", equipped: false, durability: 5);
        var equipped = Own(p, "rod");
        var limited = Own(p, "bait", equipped: false, uses: 3);
        var consumableSpent = Own(p, "bait", equipped: false, uses: -1);

        Assert.True(engine.Sell(p, plain.Id).Ok);
        Assert.Equal(5 + 150, p.Gold);
        Assert.True(engine.Sell(p, half.Id).Ok);
        Assert.Equal(5 + 150 + 75, p.Gold);
        Assert.Contains("equipped", engine.Sell(p, equipped.Id).Message);
        Assert.Contains("limited usage", engine.Sell(p, limited.Id).Message);
        Assert.Contains("consumable", engine.Sell(p, consumableSpent.Id).Message);
        Assert.Contains("not found", engine.Sell(p, 9999).Message);
        var ghost = Own(p, "missing", equipped: false);
        Assert.Contains("not found", engine.Sell(p, ghost.Id).Message);
    }

    [Fact]
    public void sell_price_floors_and_ignores_durability_for_items_without_it()
    {
        var shop = Cat.Gear("x", EquipmentSlot.Rod, cost: 99);
        Assert.Equal(14, FishingEngine.SellPrice(shop, new OwnedItem { Durability = 3 }));
        var dur = Cat.Gear("y", EquipmentSlot.Rod, cost: 99, maxDurability: 4);
        Assert.Equal(7, FishingEngine.SellPrice(dur, new OwnedItem { Durability = 2 }));
        Assert.Equal(14, FishingEngine.SellPrice(dur, new OwnedItem { Durability = null }));
        Assert.Equal(14, FishingEngine.SellPrice(dur, new OwnedItem { Durability = 400 }));
    }

    [Fact]
    public void equip_swaps_the_slot_and_refuses_spent_or_unknown_items_and_unequip_clears()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod);
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, maxUses: 3);
        var special = new ShopItem("sp", "sp", "d", 1, BoostType.StarBoost, 0.1, null);
        var engine = Engine(Cat.Calm(), rod, bait, special);
        var p = new PlayerState();
        var a = Own(p, "rod");
        var b = Own(p, "rod", equipped: false);
        var spent = Own(p, "bait", equipped: false, uses: 0);
        var slotless = Own(p, "sp", equipped: false);

        Assert.True(engine.Equip(p, b.Id).Ok);
        Assert.False(a.Equipped);
        Assert.True(b.Equipped);
        Assert.Contains("no uses", engine.Equip(p, spent.Id).Message);
        Assert.Contains("not found", engine.Equip(p, 9999).Message);
        Assert.True(engine.Equip(p, slotless.Id).Ok);
        Assert.True(b.Equipped);

        Assert.True(engine.Unequip(p, b.Id).Ok);
        Assert.False(b.Equipped);
        Assert.Contains("not found", engine.Unequip(p, 9999).Message);
        var ghost = Own(p, "missing", equipped: false);
        Assert.Contains("not found", engine.Equip(p, ghost.Id).Message);
    }

    [Fact]
    public void repair_cost_is_the_missing_fraction_of_the_price_times_the_multiplier()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 1000, maxDurability: 10);
        Assert.Equal(250, FishingEngine.RepairCost(rod, 5, 0.5));
        Assert.Equal(1, FishingEngine.RepairCost(Cat.Gear("c", EquipmentSlot.Rod, cost: 1, maxDurability: 100), 99, 0.1));
        Assert.Equal(0, FishingEngine.RepairCost(rod, 10, 1));
        Assert.Equal(0, FishingEngine.RepairCost(rod, 5, 0));
        Assert.Equal(0, FishingEngine.RepairCost(Cat.Gear("n", EquipmentSlot.Rod), 5, 1));
    }

    [Fact]
    public void repair_restores_one_or_all_equipped_items_for_gold_and_refuses_when_off_or_poor()
    {
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 1000, maxDurability: 10);
        var line = Cat.Gear("line", EquipmentSlot.Line, cost: 100, maxDurability: 10);
        var plain = Cat.Gear("hook", EquipmentSlot.Hook);

        Assert.Contains("disabled", Engine(Cat.Calm(), rod).Repair(new PlayerState(), null).Message);

        var engine = Engine(Cat.Calm(repair: 1.0), rod, line, plain);
        var p = new PlayerState { Gold = 100 };
        var r = Own(p, "rod", durability: 5);
        var l = Own(p, "line", durability: 0);
        Own(p, "hook");
        var full = Own(p, "line", equipped: false, durability: 10);

        Assert.Contains("Not enough gold", engine.Repair(p, null).Message); // 500 + 100 > 100
        Assert.Equal(100, p.Gold);
        Assert.Contains("Not enough gold", engine.Repair(p, r.Id).Message);

        p.Gold = 10_000;
        Assert.True(engine.Repair(p, l.Id).Ok);
        Assert.Equal(10.0, l.Durability);
        Assert.Equal(9_900, p.Gold);
        Assert.Contains("full durability", engine.Repair(p, full.Id).Message);
        Assert.Contains("Nothing to repair", engine.Repair(new PlayerState { Gold = 5 }, null).Message);

        var all = engine.Repair(p, null);
        Assert.True(all.Ok);
        Assert.Equal(10.0, r.Durability);
        Assert.Equal(9_400, p.Gold);
        Assert.Contains("Nothing to repair", engine.Repair(p, null).Message);
    }

    [Fact]
    public void repair_refuses_unknown_or_durability_less_items_when_named()
    {
        var plain = Cat.Gear("hook", EquipmentSlot.Hook);
        var engine = Engine(Cat.Calm(repair: 1.0), plain);
        var p = new PlayerState { Gold = 99 };
        var hook = Own(p, "hook");
        Assert.Contains("not found", engine.Repair(p, 4242).Message);
        Assert.Contains("does not have durability", engine.Repair(p, hook.Id).Message);
        var ghost = Own(p, "missing");
        Assert.Contains("does not have durability", engine.Repair(p, ghost.Id).Message);
    }

    [Fact]
    public void tiers_rank_permanent_items_by_price_quartile_within_a_slot()
    {
        var rods = Enumerable.Range(1, 8).Select(i => Cat.Gear("rod" + i, EquipmentSlot.Rod, cost: i * 100)).ToArray();
        var single = Cat.Gear("solo", EquipmentSlot.Reel, cost: 5);
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, maxUses: 3);
        var off = Cat.Gear("off", EquipmentSlot.Rod, enabled: false);
        var noSlot = new ShopItem("ns", "ns", "d", 1, BoostType.StarBoost, 0.1, null);
        var engine = new FishingEngine(Cat.Of(shop: [.. rods, single, bait, off, noSlot]));

        var t = engine.Tiers();

        Assert.Equal(EquipmentTier.Top, t["rod8"]);
        Assert.Equal(EquipmentTier.Top, t["rod7"]);
        Assert.Equal(EquipmentTier.High, t["rod6"]);
        Assert.Equal(EquipmentTier.High, t["rod5"]);
        Assert.Equal(EquipmentTier.Mid, t["rod4"]);
        Assert.Equal(EquipmentTier.Mid, t["rod3"]);
        Assert.Equal(EquipmentTier.Entry, t["rod2"]);
        Assert.Equal(EquipmentTier.Entry, t["rod1"]);
        Assert.Equal(EquipmentTier.Top, t["solo"]);
        Assert.Equal(EquipmentTier.Consumable, t["bait"]);
        Assert.Equal(EquipmentTier.Entry, t["off"]);
        Assert.Equal(EquipmentTier.Entry, t["ns"]);
    }

    [Fact]
    public void compare_reports_new_equipped_upgrade_downgrade_and_sidegrade()
    {
        var rods = Enumerable.Range(1, 8).Select(i => Cat.Gear("rod" + i, EquipmentSlot.Rod, cost: i * 100)).ToArray();
        var bait = Cat.Gear("bait", EquipmentSlot.Bait, maxUses: 2);
        var plain = new ShopItem("ns", "ns", "d", 1, BoostType.StarBoost, 0.1, null);
        var engine = new FishingEngine(Cat.Of(shop: [.. rods, bait, plain]));
        var tiers = engine.Tiers();
        var p = new PlayerState();

        Assert.Equal("new", engine.Compare(rods[0], p, tiers));
        Own(p, "rod8");
        Assert.Equal("equipped", engine.Compare(rods[7], p, tiers));
        Assert.Equal("sidegrade", engine.Compare(rods[6], p, tiers)); // both Top
        Assert.Equal("downgrade", engine.Compare(rods[0], p, tiers));
        p.Items.Clear();
        Own(p, "rod1");
        Assert.Equal("upgrade", engine.Compare(rods[7], p, tiers));
        Assert.Equal(string.Empty, engine.Compare(bait, p, tiers));
        Assert.Equal(string.Empty, engine.Compare(plain, p, tiers));
    }

    [Fact]
    public void the_default_catalog_is_internally_consistent()
    {
        var c = FishingCatalog.Default;
        Assert.Equal(c.Fish.Count, c.Fish.Select(f => f.Id).Distinct().Count());
        Assert.Equal(c.Shop.Count, c.Shop.Select(s => s.Id).Distinct().Count());
        Assert.All(c.Fish, f => Assert.Equal(FishingCatalog.RarityFromGold(f.BaseGold), f.Rarity));
        Assert.All(c.Shop, s => Assert.Null(FishingCalculations.ValidateShopItem(s)));
        Assert.All(c.Tournaments, t => Assert.Null(FishingTournament.Validate(t)));
        foreach (var rarity in Enum.GetValues<FishRarity>())
        {
            Assert.Contains(c.Fish, f => f.Rarity == rarity);
        }

        Assert.Equal(FishRarity.Common, FishingCatalog.RarityFromGold(34));
        Assert.Equal(FishRarity.Uncommon, FishingCatalog.RarityFromGold(35));
        Assert.Equal(FishRarity.Mythical, FishingCatalog.RarityFromGold(300));
        Assert.Equal(31, c.Shop.Count);
        Assert.NotNull(c.FishById("salmon"));
        Assert.Null(c.FishById("nope"));
        Assert.NotNull(c.ShopById("bamboo-rod"));
        Assert.Null(c.ShopById("nope"));
        // Upstream defaults: line snap 2%, rod snap 0.05%, nothing else on, repair off.
        Assert.Equal(0.02, c.Settings.LineSnapChance);
        Assert.Equal(0.0005, c.Settings.RodSnapChance);
        Assert.Equal(0.0, c.Settings.RepairCostMultiplier);
    }

    [Fact]
    public void a_default_catalog_player_can_cast_hundreds_of_times_without_error_and_golds_stay_consistent()
    {
        var engine = new FishingEngine(FishingCatalog.Default);
        var p = new PlayerState();
        var rng = new SeededRng(2026);
        var earned = 0L;
        for (var i = 0; i < 500; i++)
        {
            var r = engine.Cast(p, rng);
            if (r.Kind == CastKind.Caught)
            {
                earned += r.Gold;
            }
        }

        Assert.Equal(earned, p.TotalGoldEarned);
        Assert.Equal(earned, p.Gold);
        Assert.True(p.Casts > 400);
    }
}

using System.Text.Json;
using System.Text.RegularExpressions;
using WaddleSdk.Identity;
using WaddleSdk.Overlay;
using WaddleSdk.Scheduling;
using WaddleSdk.State;
using WaddleSdk.Tool;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinFishing.Tests;

public class FishingLogicTests
{
    private const string AliceTag = "{user:" + World.Alice + "}";

    private static FishingLogic Logic(FishingCatalog? catalog = null)
    {
        ulong n = 1000;
        return new FishingLogic(catalog, () => ++n);
    }

    private static void Cool(World w, string user) => StateCooldown.Release(new KvStateStore(w.Kv), "cast", user);

    private static TournamentTemplate Cup(string id = "cup", ScoreCategory score = ScoreCategory.Largest, string[]? fish = null, TournamentReward[]? rewards = null) =>
        new(id, "Test Cup", "d", score, null, fish ?? [], [], 60, 60, 0, rewards ?? [new(1, 50, 500), new(2, 25, 250)]);

    private static FishingCatalog CupCatalog(params TournamentTemplate[] cups) => Cat.Of(tournaments: cups.Length == 0 ? [Cup()] : cups);

    private static ulong HourStart(World w) => w.Clock.Now - (w.Clock.Now % 3_600_000UL);

    // ------------------------------------------------------------------ gating

    [Theory]
    [InlineData("hello there")]
    [InlineData("!roll")]
    [InlineData("!fishy")]
    [InlineData("!")]
    public void other_text_is_ignored(string text) => Assert.Null(new World().Say(Logic(), World.Alice, text));

    [Fact]
    public void everything_is_behind_the_flag_default_off()
    {
        var w = new World();
        w.Flags.Overrides.Clear();
        Assert.Null(w.Say(Logic(), World.Alice, "!fish"));
        Assert.Equal(0, w.Kv.Ops);
        Assert.Contains(w.Flags.Calls, c => c.Key == FishingLogic.FlagKey && c.DefaultValue == false);
    }

    [Fact]
    public void a_channel_without_a_community_and_an_unlinked_actor_get_a_clear_refusal()
    {
        var w = new World();
        var noCommunity = Logic().Run(World.Msg("!fish"), w.HostFor(World.Alice, "m0", community: null))!;
        Assert.Contains("needs a community", World.TextOf(noCommunity));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", w.Say(Logic(), World.Alice, "!fish"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", w.Say(Logic(), World.Alice, "!fishgold"));
        Assert.Contains(w.Log.Writes, l => l.FieldsJson.Contains("not_linked"));
        Assert.Contains(w.Log.Writes, l => l.FieldsJson.Contains("not_a_member"));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "boom");
        Assert.Throws<WaddleIdentityException>(() => w.Say(Logic(), World.Alice, "!fish"));
    }

    // ------------------------------------------------------------------ casting

    [Fact]
    public void a_cast_replies_with_the_catch_and_persists_the_player()
    {
        var w = new World();
        var text = w.Say(Logic(Cat.Of()), World.Alice, "!fish")!;

        Assert.StartsWith(AliceTag + ": ", text);
        Assert.Matches(@"\*{1,3} .+ \(\w+, [\d.]+ lb\) \+\d+g", text);
        var p = w.Player(World.Alice);
        Assert.Equal(1, p.Casts);
        Assert.True(p.Gold > 0);
        Assert.EndsWith($"Gold: {p.Gold}g.", text);
        Assert.Single(p.Applied);
    }

    [Fact]
    public void cast_is_an_alias_and_the_reply_goes_to_the_events_channel()
    {
        var w = new World();
        var reply = Logic(Cat.Of()).Run(World.Msg("!cast"), w.HostFor(World.Alice, "m1"))!;
        Assert.Equal("chan-1", reply.Payload(WaddleSdk.Json.WaddleSdkJsonContext.Default.ChatReplyPayload)!.ChannelId);
        Assert.Equal(1, w.Player(World.Alice).Casts);
    }

    [Fact]
    public void a_second_cast_inside_the_cooldown_is_silently_dropped_and_does_not_change_state()
    {
        var w = new World();
        Assert.NotNull(w.Say(Logic(), World.Alice, "!fish"));
        var before = w.Player(World.Alice).Gold;
        Assert.Null(w.Say(Logic(), World.Alice, "!fish"));
        Assert.Equal(before, w.Player(World.Alice).Gold);
        Assert.NotNull(w.Say(Logic(), World.Bob, "!fish")); // cooldowns are per user
    }

    [Theory]
    [InlineData("!fish 0")]
    [InlineData("!fish 11")]
    [InlineData("!fish many")]
    [InlineData("!fish 1 2")]
    [InlineData("!fish -3")]
    public void a_bad_cast_count_gets_usage_and_costs_nothing(string text)
    {
        var w = new World();
        Assert.Equal("Usage: !fish [1-10]", w.Say(Logic(), World.Alice, text));
        Assert.Equal(0, w.Player(World.Alice).Casts);
        Assert.NotNull(w.Say(Logic(), World.Alice, "!fish")); // no cooldown was consumed
    }

    [Fact]
    public void a_multi_cast_runs_every_cast_and_totals_the_gold()
    {
        var w = new World();
        var text = w.Say(Logic(), World.Alice, "!fish 5")!;
        Assert.Contains("casts 5 times", text);
        Assert.Contains("Total", text);
        var p = w.Player(World.Alice);
        Assert.InRange(p.Casts, 3, 5); // an accident cast is not a catch
        Assert.Contains("|", text);
    }

    [Fact]
    public void a_redelivered_cast_reprints_the_same_result_and_moves_nothing_twice()
    {
        var w = new World();
        var logic = Logic();
        var first = w.Send(logic, World.Alice, "!fish 3", "evt-1")!;
        var state = w.Player(World.Alice);
        var gold = state.Gold;

        for (var i = 0; i < 3; i++)
        {
            Assert.Equal(first, w.Send(logic, World.Alice, "!fish 3", "evt-1"));
        }

        var after = w.Player(World.Alice);
        Assert.Equal(gold, after.Gold);
        Assert.Equal(state.Casts, after.Casts);
        Assert.Equal(state.TotalGoldEarned, after.TotalGoldEarned);
        Assert.Single(after.Applied);
    }

    [Fact]
    public void a_redelivery_after_a_later_command_does_not_apply_the_cast_again()
    {
        var w = new World();
        var logic = Logic();
        var first = w.Send(logic, World.Alice, "!fish", "evt-1")!;
        var gold = w.Player(World.Alice).Gold;
        Cool(w, World.Alice);
        w.Send(logic, World.Alice, "!fish", "evt-2");
        var afterTwo = w.Player(World.Alice).Gold;
        Assert.True(afterTwo >= gold);

        Assert.Equal(first, w.Send(logic, World.Alice, "!fish", "evt-1"));
        Assert.Equal(afterTwo, w.Player(World.Alice).Gold);
    }

    [Fact]
    public void a_crash_between_the_record_and_the_state_write_is_repaired_from_the_recorded_seed()
    {
        var w = new World();
        var logic = Logic();
        w.Kv.BeforeSet = key =>
        {
            if (key.StartsWith("p.", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("simulated crash before the player write");
            }
        };

        Assert.Throws<InvalidOperationException>(() => w.Send(logic, World.Alice, "!fish 2", "evt-1"));
        Assert.Equal(0, w.Player(World.Alice).Casts); // nothing applied
        w.Kv.BeforeSet = null;

        var redelivered = w.Send(logic, World.Alice, "!fish 2", "evt-1")!;
        var state = w.Player(World.Alice);
        Assert.InRange(state.Casts, 1, 2);
        Assert.Equal(state.TotalGoldEarned, state.Gold);

        // The recorded seed makes a third delivery print the very same text and change nothing.
        Assert.Equal(redelivered, w.Send(logic, World.Alice, "!fish 2", "evt-1"));
        Assert.Equal(state.Gold, w.Player(World.Alice).Gold);
    }

    [Fact]
    public void a_record_that_belongs_to_another_actor_or_command_is_not_a_replay()
    {
        var w = new World();
        var host = w.HostFor(World.Alice, "evt-1");
        var key = ReplayGuard.KeyFor("fish.out", World.Msg("!fish"), host, World.Alice, "fish");
        new KvStateStore(w.Kv).SetJson(key, new CastOutcome(World.Bob, "fish", 1, 1, "stolen text", []), FishingJsonContext.Default.CastOutcome, 60);

        var text = w.Send(Logic(), World.Alice, "!fish", "evt-1")!;

        Assert.DoesNotContain("stolen", text);
        Assert.Equal(1, w.Player(World.Alice).Casts);
    }

    [Fact]
    public void a_blank_message_id_is_refused_loudly_for_every_mutating_command()
    {
        var w = new World();
        foreach (var text in new[] { "!fish", "!fishbuy worms", "!fishsell 1" })
        {
            Assert.Throws<InvalidOperationException>(() => w.Send(Logic(), World.Alice, text, ""));
        }
    }

    [Fact]
    public void a_cast_while_the_player_lock_is_held_is_refused_and_returns_the_cooldown()
    {
        var w = new World();
        Assert.True(w.Store.TryLock(World.Alice));

        Assert.Contains("One cast at a time", w.Say(Logic(Cat.Of()), World.Alice, "!fish"));
        w.Store.Unlock(World.Alice);

        // The cooldown taken for the refused attempt was handed back, so the player can cast right away.
        Assert.Contains("lb)", w.Say(Logic(Cat.Of()), World.Alice, "!fish"));
    }

    [Fact]
    public void accidents_are_reported_and_cost_equipped_gear()
    {
        var w = new World();
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 100);
        var catalog = Cat.Of(Cat.Calm(rod: 1), [rod]);
        w.SetPlayer(World.Alice, new PlayerState { Gold = 10, NextItemId = 2, Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true }] });

        var text = w.Say(Logic(catalog), World.Alice, "!fish")!;

        Assert.Contains("ROD SNAPPED! lost rod (100g)", text);
        var p = w.Player(World.Alice);
        Assert.Empty(p.Items);
        Assert.Equal(100, p.TotalGoldLost);
        Assert.Equal(10, p.Gold);
    }

    [Theory]
    [InlineData("reel", EquipmentSlot.Reel, "REEL JAMMED")]
    [InlineData("box", EquipmentSlot.TackleBox, "TACKLE BOX LOST")]
    [InlineData("net", EquipmentSlot.Net, "NET BROKEN")]
    public void every_accident_kind_has_its_own_message(string id, EquipmentSlot slot, string message)
    {
        var w = new World();
        var item = Cat.Gear(id, slot);
        var settings = id == "reel" ? Cat.Calm(reel: 1) : id == "box" ? Cat.Calm(box: 1) : Cat.Calm(net: 1);
        w.SetPlayer(World.Alice, new PlayerState { Items = [new OwnedItem { Id = 1, ShopId = id, Equipped = true }] });
        Assert.Contains(message + "!", w.Say(Logic(Cat.Of(settings, [item])), World.Alice, "!fish"));
    }

    [Fact]
    public void a_line_snap_with_nothing_equipped_says_nothing_was_lost()
    {
        var w = new World();
        var text = w.Say(Logic(Cat.Of(Cat.Calm(line: 1))), World.Alice, "!fish")!;
        Assert.Contains("LINE SNAPPED! nothing equipped was lost", text);
    }

    [Fact]
    public void a_broken_item_is_reported_in_the_catch_line()
    {
        var w = new World();
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, maxDurability: 1, loss: 1);
        w.SetPlayer(World.Alice, new PlayerState { Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true, Durability = 1 }, new OwnedItem { Id = 2, ShopId = "rod", Equipped = false }] });
        var text = w.Say(Logic(Cat.Of(Cat.Calm(), [rod])), World.Alice, "!fish")!;
        Assert.Contains("[rod broke, spare equipped]", text);

        var w2 = new World();
        w2.SetPlayer(World.Alice, new PlayerState { Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true, Durability = 1 }] });
        Assert.Contains("[rod broke]", w2.Say(Logic(Cat.Of(Cat.Calm(), [rod])), World.Alice, "!fish"));
    }

    // ------------------------------------------------------------------ shop and inventory

    private static World Rich(long gold = 1000)
    {
        var w = new World();
        w.SetPlayer(World.Alice, new PlayerState { Gold = gold });
        return w;
    }

    [Fact]
    public void the_shop_lists_sections_and_each_section_shows_prices_tiers_and_upgrade_hints()
    {
        var w = Rich();
        var logic = Logic();
        var sections = w.Say(logic, World.Alice, "!fishshop")!;
        Assert.Contains("rod (4)", sections);
        Assert.Contains("lure (5)", sections);

        var rods = w.Say(logic, World.Alice, "!fishshop rod")!;
        Assert.Contains("Bamboo Rod 150g (Entry, new)", rods);
        Assert.Contains("Legendary Rod 2500g (Top, new)", rods);

        w.Say(logic, World.Alice, "!fishbuy bamboo rod");
        w.Say(logic, World.Alice, "!fishequip 1");
        var after = w.Say(logic, World.Alice, "!fishshop ROD")!;
        Assert.Contains("Bamboo Rod 150g (Entry, equipped)", after);
        Assert.Contains("Fiberglass Rod 400g (Mid, upgrade)", after);
        Assert.Contains("Worms 75g (Consumable)", w.Say(logic, World.Alice, "!fishshop bait")!);
        Assert.Contains("No shop section 'nope'", w.Say(logic, World.Alice, "!fishshop nope"));
    }

    [Fact]
    public void buying_charges_gold_once_and_names_the_new_item()
    {
        var w = Rich();
        var logic = Logic();
        var text = w.Say(logic, World.Alice, "!fishbuy Bamboo Rod")!;
        Assert.Contains("Bought Bamboo Rod x1 for 150g (item #1)", text);
        Assert.Contains("!fishequip 1", text);
        Assert.Equal(850, w.Player(World.Alice).Gold);

        var bait = w.Say(logic, World.Alice, "!fishbuy worms 3")!;
        Assert.Contains("x3 for 225g", bait);
        Assert.Equal(625, w.Player(World.Alice).Gold);
        Assert.Equal(4, w.Player(World.Alice).Items.Count);
        Assert.Contains("Bought", w.Say(logic, World.Alice, "!fishbuy bamboo")!); // unique substring
    }

    [Theory]
    [InlineData("!fishbuy", "Usage: !fishbuy")]
    [InlineData("!fishbuy unicorn", "No such shop item")]
    [InlineData("!fishbuy rod", "No such shop item")] // ambiguous substring is not guessed
    [InlineData("!fishbuy legendary rod 2", "multiples")]
    [InlineData("!fishbuy legendary rod", "Not enough gold")]
    public void buy_refusals_charge_nothing_and_leave_no_record(string text, string expected)
    {
        var w = Rich(10);
        Assert.Contains(expected, w.Say(Logic(), World.Alice, text));
        Assert.Equal(10, w.Player(World.Alice).Gold);
        Assert.Empty(w.Player(World.Alice).Applied);
    }

    [Fact]
    public void a_redelivered_purchase_never_charges_twice_even_after_other_actions()
    {
        var w = Rich();
        var logic = Logic();
        var first = w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1")!;
        w.Send(logic, World.Alice, "!fishequip 1", "eq-1");
        w.Send(logic, World.Alice, "!fishbuy worms 2", "buy-2");
        var gold = w.Player(World.Alice).Gold;
        var items = w.Player(World.Alice).Items.Count;

        Assert.Equal(first, w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1"));
        Assert.Equal(gold, w.Player(World.Alice).Gold);
        Assert.Equal(items, w.Player(World.Alice).Items.Count);
    }

    [Fact]
    public void a_purchase_interrupted_before_the_state_write_is_applied_exactly_once_on_redelivery()
    {
        var w = Rich();
        var logic = Logic();
        w.Kv.BeforeSet = key =>
        {
            if (key.StartsWith("p.", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("simulated crash");
            }
        };

        Assert.Throws<InvalidOperationException>(() => w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1"));
        Assert.Equal(1000, w.Player(World.Alice).Gold);
        w.Kv.BeforeSet = null;

        var text = w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1")!;
        Assert.Contains("Bought Bamboo Rod", text);
        Assert.Equal(850, w.Player(World.Alice).Gold);
        Assert.Equal(text, w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1"));
        Assert.Equal(850, w.Player(World.Alice).Gold);
    }

    [Fact]
    public void a_replayed_purchase_that_can_no_longer_be_afforded_is_refused_not_forced()
    {
        var w = Rich();
        var logic = Logic();
        var host = w.HostFor(World.Alice, "buy-1");
        var key = ReplayGuard.KeyFor("fish.out", World.Msg("!fishbuy legendary rod"), host, World.Alice, "fishbuy");
        new KvStateStore(w.Kv).SetJson(key, new ShopOutcome(World.Alice, "fishbuy", "Bought it"), FishingJsonContext.Default.ShopOutcome, 60);

        // A record exists but the state was never marked applied (crash) and the money is gone: the op refuses.
        Assert.Contains("Not enough gold", w.Send(logic, World.Alice, "!fishbuy legendary rod", "buy-1"));
        Assert.Equal(1000, w.Player(World.Alice).Gold);
    }

    [Fact]
    public void equip_sell_unequip_and_inventory_round_trip()
    {
        var w = Rich(2000);
        var logic = Logic();
        w.Say(logic, World.Alice, "!fishbuy carbon fiber rod");
        w.Say(logic, World.Alice, "!fishbuy worms");

        Assert.Contains("Equipped Carbon Fiber Rod (#1)", w.Say(logic, World.Alice, "!fishequip #1"));
        Assert.Contains("Cannot sell an equipped item", w.Say(logic, World.Alice, "!fishsell 1"));
        var inv = w.Say(logic, World.Alice, "!fishinv")!;
        Assert.Contains("#1 Carbon Fiber Rod [E]", inv);
        Assert.Contains("#2 Worms 5 uses", inv);
        Assert.Equal(inv, w.Say(logic, World.Alice, "!fishinventory"));

        Assert.Contains("Unequipped #1", w.Say(logic, World.Alice, "!fishunequip 1"));
        Assert.Contains("Sold Carbon Fiber Rod for 150g", w.Say(logic, World.Alice, "!fishsell 1"));
        Assert.Contains("not found", w.Say(logic, World.Alice, "!fishsell 1"));
        Assert.Contains("Usage: !fishsell", w.Say(logic, World.Alice, "!fishsell"));
        Assert.Contains("Usage: !fishequip", w.Say(logic, World.Alice, "!fishequip x"));
        Assert.Contains("Usage: !fishunequip", w.Say(logic, World.Alice, "!fishunequip 1 2"));
    }

    [Fact]
    public void an_empty_inventory_says_so_and_durability_shows_in_the_listing()
    {
        var w = Rich(5);
        Assert.Contains("tackle box is empty", w.Say(Logic(), World.Alice, "!fishinv"));

        var rod = Cat.Gear("rod", EquipmentSlot.Rod, maxDurability: 10);
        w.SetPlayer(World.Alice, new PlayerState { Items = [new OwnedItem { Id = 1, ShopId = "rod", Durability = 7.5 }, new OwnedItem { Id = 2, ShopId = "gone" }] });
        var inv = w.Say(Logic(Cat.Of(shop: [rod])), World.Alice, "!fishinv")!;
        Assert.Contains("#1 rod 7.5/10", inv);
        Assert.Contains("#2 gone", inv);
    }

    [Fact]
    public void repair_works_when_enabled_and_is_off_by_default()
    {
        var w = Rich();
        Assert.Contains("disabled", w.Say(Logic(), World.Alice, "!fishrepair"));

        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 1000, maxDurability: 10);
        w.SetPlayer(World.Alice, new PlayerState { Gold = 5000, NextItemId = 2, Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true, Durability = 5 }] });
        var logic = Logic(Cat.Of(Cat.Calm(repair: 1.0), [rod]));
        Assert.Contains("Repaired 1 item(s) for 500g", w.Say(logic, World.Alice, "!fishrepair"));
        Assert.Equal(4500, w.Player(World.Alice).Gold);
        Assert.Contains("Nothing to repair", w.Say(logic, World.Alice, "!fishrepair all"));
        Assert.Contains("full durability", w.Say(logic, World.Alice, "!fishrepair 1"));
        Assert.Contains("Usage: !fishrepair", w.Say(logic, World.Alice, "!fishrepair 1 2"));
    }

    [Fact]
    public void a_shop_action_while_the_lock_is_held_is_refused_without_changing_anything()
    {
        var w = Rich();
        w.Store.TryLock(World.Alice);
        Assert.Contains("One action at a time", w.Say(Logic(), World.Alice, "!fishbuy bamboo rod"));
        w.Store.Unlock(World.Alice);
        Assert.Equal(1000, w.Player(World.Alice).Gold);
    }

    [Fact]
    public void read_only_commands_report_gold_log_leaderboards_and_help()
    {
        var w = new World();
        var logic = Logic();
        Assert.Contains("0g in hand", w.Say(logic, World.Alice, "!fishgold"));
        Assert.Contains("0 of 30 species caught.", w.Say(logic, World.Alice, "!fishlog"));
        Assert.Equal("Nobody has caught anything yet.", w.Say(logic, World.Alice, "!fishtop"));

        w.Say(logic, World.Alice, "!fish 4");
        Cool(w, World.Bob);
        w.Say(logic, World.Bob, "!fish 4");
        var gold = w.Say(logic, World.Alice, "!fishgold")!;
        Assert.Matches(@"\d+g in hand, \d+g earned over \d+ casts, \d+g lost", gold);
        Assert.Contains("species caught. Best:", w.Say(logic, World.Alice, "!fishlog"));
        var top = w.Say(logic, World.Alice, "!fishtop")!;
        Assert.StartsWith("Top earners: 1. {user:", top);
        Assert.Contains("!fish [1-10] cast", w.Say(logic, World.Alice, "!fishhelp"));
        Assert.Contains("!fishtourney", w.Say(logic, World.Alice, "!fishhelp"));
    }

    [Fact]
    public void the_loss_leaderboard_appears_once_someone_has_lost_gear()
    {
        var w = new World();
        var rod = Cat.Gear("rod", EquipmentSlot.Rod, cost: 100);
        var logic = Logic(Cat.Of(Cat.Calm(rod: 1), [rod]));
        w.SetPlayer(World.Alice, new PlayerState { Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true }] });
        w.Say(logic, World.Alice, "!fish");
        // Nobody has earned anything yet, so the board still reports that, even though alice lost 100g.
        Assert.Equal("Nobody has caught anything yet.", w.Say(logic, World.Alice, "!fishtop"));

        var w2 = new World();
        w2.SetPlayer(World.Alice, new PlayerState { TotalGoldEarned = 5, Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true }] });
        w2.Say(logic, World.Alice, "!fish");
        Assert.Equal($"Top earners: 1. {AliceTag} 5g. Biggest losses: 1. {AliceTag} 100g.", w2.Say(logic, World.Alice, "!fishtop"));
    }

    // ------------------------------------------------------------------ replay marks (regression: no count-based eviction)

    [Fact]
    public void replay_marks_outlive_the_outcome_record_so_they_are_never_evicted_by_count()
    {
        Assert.True(AppliedMarks.RetentionMillis >= FishingLogic.OutcomeTtlSeconds * 1000UL);
        Assert.True(AppliedMarks.MaxLive > 100);
    }

    [Fact]
    public void a_purchase_replayed_after_far_more_than_24_later_actions_is_still_not_applied_twice()
    {
        var w = Rich();
        var logic = Logic();
        var first = w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1")!;
        var gold = w.Player(World.Alice).Gold;
        for (var i = 0; i < 60; i++) // the old cap was 24 marks
        {
            w.Send(logic, World.Alice, "!fishequip 1", "eq-" + i);
        }

        Assert.True(w.Player(World.Alice).Applied.Count >= 61);
        Assert.Equal(first, w.Send(logic, World.Alice, "!fishbuy bamboo rod", "buy-1"));
        Assert.Equal(gold, w.Player(World.Alice).Gold);
        Assert.Single(w.Player(World.Alice).Items);
    }

    [Fact]
    public void a_cast_replayed_after_far_more_than_24_later_casts_is_still_not_applied_twice()
    {
        var w = new World();
        var logic = Logic(Cat.Of());
        var first = w.Send(logic, World.Alice, "!fish", "evt-first")!;
        for (var i = 0; i < 40; i++)
        {
            Cool(w, World.Alice);
            w.Send(logic, World.Alice, "!fish", "evt-" + i);
        }

        var before = w.Player(World.Alice);
        Assert.Equal(first, w.Send(logic, World.Alice, "!fish", "evt-first"));
        var after = w.Player(World.Alice);
        Assert.Equal(before.Gold, after.Gold);
        Assert.Equal(before.Casts, after.Casts);
    }

    [Fact]
    public void a_tournament_catch_replayed_after_far_more_than_8_later_catches_is_not_double_counted()
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog());
        w.Send(logic, World.Alice, "!fish", "evt-first");
        var window = FishingTournament.ActiveWindow(Cup(), w.Clock.Now)!;
        for (var i = 0; i < 30; i++)
        {
            Cool(w, World.Alice);
            w.Send(logic, World.Alice, "!fish", "evt-" + i);
        }

        var count = w.Store.LoadAggregate(window.Id, World.Alice).Count;
        Assert.Equal(31, count);
        w.Send(logic, World.Alice, "!fish", "evt-first");
        Assert.Equal(31, w.Store.LoadAggregate(window.Id, World.Alice).Count);
    }

    [Fact]
    public void marks_age_out_only_after_the_retention_window()
    {
        var marks = new List<string>();
        AppliedMarks.Add(marks, "old", 60_000UL * 10);
        AppliedMarks.Add(marks, "new", 60_000UL * 10 + AppliedMarks.RetentionMillis - 60_000UL);
        Assert.True(AppliedMarks.Has(marks, "old")); // still inside the window
        AppliedMarks.Add(marks, "newer", 60_000UL * 10 + AppliedMarks.RetentionMillis + 120_000UL);
        Assert.False(AppliedMarks.Has(marks, "old")); // aged out
        Assert.True(AppliedMarks.Has(marks, "new"));
        Assert.True(AppliedMarks.Has(marks, "newer"));
        AppliedMarks.Add(marks, "newer", 60_000UL * 10 + AppliedMarks.RetentionMillis + 120_000UL); // re-adding is a no-op
        Assert.Equal(2, marks.Count);
        Assert.False(AppliedMarks.Has(marks, "new2")); // no prefix confusion
        Assert.False(AppliedMarks.Has(["abc|1"], "ab"));
    }

    [Fact]
    public void a_saturated_retention_window_fails_closed_instead_of_dropping_a_live_mark()
    {
        var w = Rich();
        var p = w.Player(World.Alice);
        var now = w.Clock.Now;
        for (var i = 0; i < AppliedMarks.MaxLive; i++)
        {
            p.Applied.Add($"k{i}|{now / 60_000UL}");
        }

        w.SetPlayer(World.Alice, p);
        Assert.Throws<InvalidOperationException>(() => w.Say(Logic(), World.Alice, "!fishbuy bamboo rod"));
        var after = w.Player(World.Alice);
        Assert.Equal(1000, after.Gold);
        Assert.Empty(after.Items);
        Assert.Equal(AppliedMarks.MaxLive, after.Applied.Count);
    }

    [Fact]
    public void settlement_payout_marks_are_capped_by_count_and_drop_the_oldest()
    {
        var p = new PlayerState();
        for (var i = 0; i < PlayerState.MaxPaid + 5; i++)
        {
            p.MarkPaid("t" + i);
        }

        Assert.Equal(PlayerState.MaxPaid, p.Paid.Count);
        Assert.False(p.HasPaid("t0"));
        Assert.True(p.HasPaid("t" + (PlayerState.MaxPaid + 4)));
    }

    // ------------------------------------------------------------------ lifetime boards

    [Fact]
    public void the_monotone_top_n_board_keeps_the_highest_values_exactly()
    {
        var rows = new List<LifetimeRow>();
        Assert.False(FishingLogic.Upsert(rows, "u", 0));
        for (var i = 1; i <= FishingLogic.LifetimeBoardSize; i++)
        {
            Assert.True(FishingLogic.Upsert(rows, "u" + i, i * 10));
        }

        Assert.Equal(FishingLogic.LifetimeBoardSize, rows.Count);
        Assert.Equal("u25", rows[0].User);
        Assert.False(FishingLogic.Upsert(rows, "low", 5)); // below the lowest (10)
        Assert.False(FishingLogic.Upsert(rows, "u25", 250)); // unchanged
        Assert.True(FishingLogic.Upsert(rows, "u1", 1000)); // existing user rises
        Assert.Equal("u1", rows[0].User);
        Assert.True(FishingLogic.Upsert(rows, "new", 25)); // evicts the lowest (u2 at 20; u1 rose to 1000)
        Assert.Equal(FishingLogic.LifetimeBoardSize, rows.Count);
        Assert.Contains(rows, r => r.User == "new");
        Assert.DoesNotContain(rows, r => r.User == "u2");
    }

    // ------------------------------------------------------------------ tournaments

    [Fact]
    public void tournaments_are_off_unless_their_own_flag_is_on()
    {
        var w = new World(tournaments: false);
        Assert.Contains("not enabled", w.Say(Logic(CupCatalog()), World.Alice, "!fishtourney"));
        var text = w.Say(Logic(CupCatalog()), World.Alice, "!fish")!;
        Assert.DoesNotContain("[Test Cup", text);
        Assert.Null(w.Store.LoadBoard("cup." + (HourStart(w) / 60_000UL)));
    }

    [Fact]
    public void a_catch_inside_an_active_window_ranks_the_player_and_schedules_settlement_once()
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog());
        var text = w.Say(logic, World.Alice, "!fish")!;
        Assert.EndsWith("[Test Cup: you are #1]", text);

        Cool(w, World.Bob);
        Assert.Contains("[Test Cup: you are #", w.Say(logic, World.Bob, "!fish")!);
        Cool(w, World.Alice);
        w.Say(logic, World.Alice, "!fish");

        var pending = new TickScheduler(new KvStateStore(w.Kv), w.Clock, "fishing").Pending();
        var task = Assert.Single(pending);
        Assert.Equal("settle", task.Kind);
        Assert.Equal("cup", task.PayloadJson);
        var window = FishingTournament.ActiveWindow(Cup(), w.Clock.Now)!;
        Assert.Equal(window.EndMillis, task.DueAtMillis);

        var board = w.Store.LoadBoard(window.Id)!;
        Assert.Equal(2, board.Rows.Count);
        Assert.True(board.Scheduled);
        Assert.False(board.Settled);
        Assert.Equal(2, w.Store.LoadAggregate(window.Id, World.Alice).Count);
    }

    [Fact]
    public void a_replayed_cast_does_not_double_count_in_the_tournament()
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog());
        w.Send(logic, World.Alice, "!fish", "evt-1");
        var window = FishingTournament.ActiveWindow(Cup(), w.Clock.Now)!;
        Assert.Equal(1, w.Store.LoadAggregate(window.Id, World.Alice).Count);

        w.Send(logic, World.Alice, "!fish", "evt-1");
        w.Send(logic, World.Alice, "!fish", "evt-1");

        Assert.Equal(1, w.Store.LoadAggregate(window.Id, World.Alice).Count);
    }

    [Fact]
    public void an_interrupted_cast_is_counted_in_the_tournament_exactly_once_after_redelivery()
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog());
        w.Kv.BeforeSet = key =>
        {
            if (key.StartsWith("ta.", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("crash before the aggregate write");
            }
        };

        Assert.Throws<InvalidOperationException>(() => w.Send(logic, World.Alice, "!fish", "evt-1"));
        w.Kv.BeforeSet = null;
        w.Send(logic, World.Alice, "!fish", "evt-1");

        var window = FishingTournament.ActiveWindow(Cup(), w.Clock.Now)!;
        Assert.Equal(1, w.Store.LoadAggregate(window.Id, World.Alice).Count);
        Assert.Equal(1, w.Player(World.Alice).Casts);
    }

    [Fact]
    public void ineligible_catches_and_catches_outside_a_window_are_not_recorded()
    {
        var w = new World(tournaments: true);
        var other = Cup(fish: ["not-minnow"]);
        w.Say(Logic(CupCatalog(other)), World.Alice, "!fish");
        var window = FishingTournament.ActiveWindow(other, w.Clock.Now)!;
        Assert.Null(w.Store.LoadBoard(window.Id));

        var closed = Cup() with { PeriodMinutes = 1440, DurationMinutes = 1, OffsetMinutes = 1439 };
        Cool(w, World.Alice);
        Assert.DoesNotContain("[Test Cup", w.Say(Logic(CupCatalog(closed)), World.Alice, "!fish"));

        // An accident-only cast has nothing to record either.
        var w2 = new World(tournaments: true);
        var text = w2.Say(Logic(Cat.Of(Cat.Calm(line: 1), tournaments: [Cup()])), World.Alice, "!fish")!;
        Assert.DoesNotContain("[Test Cup", text);
    }

    [Fact]
    public void a_player_outside_the_bounded_board_is_told_they_are_not_on_it_yet()
    {
        var w = new World(tournaments: true);
        var smallest = Cup(score: ScoreCategory.Smallest);
        var logic = Logic(CupCatalog(smallest));
        var window = FishingTournament.ActiveWindow(smallest, w.Clock.Now)!;
        var board = new TournamentBoard { Scheduled = true };
        for (var i = 0; i < FishingTournament.BoardSize; i++)
        {
            FishingTournament.Upsert(board, "z" + i.ToString("D2"), new TournamentAggregate { Count = 1, MinWeight = 0.001, TotalWeight = 0.001 }, ScoreCategory.Smallest);
        }

        w.Store.SaveBoard(window.Id, board);
        Assert.Contains("[Test Cup: not on the board yet]", w.Say(logic, World.Alice, "!fish"));
    }

    [Fact]
    public void the_tourney_command_shows_standings_time_left_and_the_next_run()
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog());
        Assert.Contains("Test Cup (Largest, ", w.Say(logic, World.Alice, "!fishtourney"));
        Assert.Contains("no catches yet", w.Say(logic, World.Alice, "!fishtourney"));

        w.Say(logic, World.Alice, "!fish");
        var shown = w.Say(logic, World.Alice, "!fishtourney")!;
        Assert.Matches(@"Test Cup \(Largest, \d+ min left\): 1\. \{user:" + World.Alice + @"\} [\d.]+ lb", shown);

        var closed = Cup() with { PeriodMinutes = 1440, DurationMinutes = 60, OffsetMinutes = 600 };
        var text = w.Say(Logic(CupCatalog(closed)), World.Alice, "!fishtourney")!;
        Assert.Matches(@"Test Cup: next run in \d+ min", text);
        Assert.Equal("No fishing tournaments are configured.", w.Say(Logic(Cat.Of()), World.Alice, "!fishtourney"));
    }

    [Theory]
    [InlineData(ScoreCategory.MostCatches, @"\d+\b(?! lb)")]
    [InlineData(ScoreCategory.MostValuable, @"\d+g")]
    [InlineData(ScoreCategory.TotalWeight, @"[\d.]+ lb")]
    public void standings_format_scores_by_category(ScoreCategory score, string pattern)
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog(Cup(score: score)));
        w.Say(logic, World.Alice, "!fish");
        Assert.Matches(@"1\. \{user:[^}]+\} " + pattern, w.Say(logic, World.Alice, "!fishtourney"));
    }

    private static (World World, FishingLogic Logic, TournamentWindow Window) Played(TournamentTemplate? cup = null, params string[] players)
    {
        var w = new World(tournaments: true);
        var template = cup ?? Cup();
        var logic = Logic(CupCatalog(template));
        string[] who = players.Length == 0 ? [World.Alice, World.Bob] : players;
        foreach (var p in who)
        {
            Cool(w, p);
            w.Say(logic, p, "!fish");
        }

        var window = FishingTournament.ActiveWindow(template, w.Clock.Now)!;
        return (w, logic, window);
    }

    private static void AdvancePastWindow(World w, TournamentWindow window) => w.Clock.Now = window.EndMillis + 60_000UL;

    [Fact]
    public void settlement_pays_points_through_the_economy_service_and_gold_to_the_ranked_winners_once()
    {
        var (w, logic, window) = Played();
        var board = w.Store.LoadBoard(window.Id)!;
        var first = board.Rows[0].User;
        var second = board.Rows[1].User;
        var goldBefore = (first: w.Player(first).Gold, second: w.Player(second).Gold);
        AdvancePastWindow(w, window);

        var text = w.Say(logic, World.Alice, "!fishgold")!;

        Assert.Contains("[Test Cup ended! #1 {user:" + first + "} (50 pts + 500g), #2 {user:" + second + "} (25 pts + 250g)]", text);
        Assert.Equal(50, w.Tools.Balance(first));
        Assert.Equal(25, w.Tools.Balance(second));
        Assert.Equal(goldBefore.first + 500, w.Player(first).Gold);
        Assert.Equal(goldBefore.second + 250, w.Player(second).Gold);
        Assert.All(w.Tools.Credits, c => Assert.NotNull(c.Key));
        Assert.Equal(2, w.Tools.Credits.Count);
        Assert.True(w.Store.LoadBoard(window.Id)!.Settled);
        Assert.Empty(new TickScheduler(new KvStateStore(w.Kv), w.Clock, "fishing").Pending());

        // A later command neither re-announces nor re-pays.
        Assert.DoesNotContain("ended", w.Say(logic, World.Alice, "!fishgold")!);
        Assert.Equal(2, w.Tools.Credits.Count);
    }

    [Fact]
    public void a_settlement_replayed_after_a_crash_pays_nothing_twice()
    {
        var (w, logic, window) = Played();
        AdvancePastWindow(w, window);
        w.Say(logic, World.Alice, "!fishgold");
        var credits = w.Tools.Credits.Count;
        var gold = (a: w.Player(World.Alice).Gold, b: w.Player(World.Bob).Gold);

        // Simulate the crash window: rewards were paid but the board was never marked settled and the task is still pending.
        var board = w.Store.LoadBoard(window.Id)!;
        board.Settled = false;
        w.Store.SaveBoard(window.Id, board);
        new TickScheduler(new KvStateStore(w.Kv), w.Clock, "fishing").Schedule(new ScheduledTask(window.Id, window.EndMillis, "settle", "cup"));

        w.Say(logic, World.Alice, "!fishgold");

        Assert.Equal(credits, w.Tools.Credits.Count); // same idempotency keys: the economy service moved nothing
        Assert.Equal(gold, (w.Player(World.Alice).Gold, w.Player(World.Bob).Gold)); // per-player applied marks
        Assert.Equal(2, w.Tools.Keys.Distinct().Count());
        Assert.All(w.Tools.Keys, k => Assert.DoesNotContain(':', k!));
    }

    [Fact]
    public void an_economy_outage_leaves_settlement_pending_and_the_current_command_unharmed_then_it_retries()
    {
        var (w, logic, window) = Played();
        AdvancePastWindow(w, window);
        w.Tools.Failure = new WaddleToolException(ToolErrorKind.Forbidden, "denied");

        var text = w.Say(logic, World.Alice, "!fishgold")!;

        Assert.DoesNotContain("ended", text);
        Assert.Contains("g in hand", text);
        Assert.False(w.Store.LoadBoard(window.Id)!.Settled);
        Assert.Single(new TickScheduler(new KvStateStore(w.Kv), w.Clock, "fishing").Pending());
        Assert.Contains(w.Log.Writes, l => l.Message == "fishing.settle_failed");

        w.Tools.Failure = null;
        Assert.Contains("ended!", w.Say(logic, World.Alice, "!fishgold"));
        Assert.Equal(2, w.Tools.Credits.Count);
    }

    [Fact]
    public void a_busy_winner_defers_the_gold_grant_to_the_next_event_without_double_paying_points()
    {
        var (w, logic, window) = Played();
        var board = w.Store.LoadBoard(window.Id)!;
        var winner = board.Rows[0].User;
        AdvancePastWindow(w, window);
        w.Store.TryLock(winner);

        Assert.DoesNotContain("ended", w.Say(logic, winner == World.Alice ? World.Bob : World.Alice, "!fishgold")!);
        w.Store.Unlock(winner);
        Assert.Contains("ended!", w.Say(logic, World.Alice, "!fishgold"));

        Assert.Equal(50, w.Tools.Balance(winner)); // credited exactly once despite two settlement attempts
    }

    [Fact]
    public void a_winner_who_left_the_community_is_skipped_for_points_but_still_gets_gold()
    {
        var (w, logic, window) = Played();
        var board = w.Store.LoadBoard(window.Id)!;
        var gone = board.Rows[0].User;
        w.Tools.NonMembers.Add(gone);
        AdvancePastWindow(w, window);

        Assert.Contains("ended!", w.Say(logic, World.Alice, "!fishgold"));

        Assert.Equal(0, w.Tools.Balance(gone));
        Assert.Contains(w.Log.Writes, l => l.Message == "fishing.reward_skipped");
        Assert.True(w.Player(gone).Gold >= 500);
    }

    [Fact]
    public void a_tournament_with_no_reward_winners_announces_that()
    {
        var (w, logic, window) = Played(Cup(rewards: []));
        AdvancePastWindow(w, window);
        Assert.Contains("[Test Cup ended: no winners.]", w.Say(logic, World.Alice, "!fishgold"));
        Assert.Empty(w.Tools.Credits);
    }

    [Fact]
    public void a_gold_only_reward_never_touches_the_economy_service()
    {
        var (w, logic, window) = Played(Cup(rewards: [new TournamentReward(1, 0, 40)]), World.Alice);
        AdvancePastWindow(w, window);
        Assert.Contains("(40g)", w.Say(logic, World.Alice, "!fishgold")!);
        Assert.Empty(w.Tools.Credits);
    }

    [Fact]
    public void orphaned_settlement_tasks_are_cancelled_with_a_warning()
    {
        var w = new World(tournaments: true);
        var scheduler = new TickScheduler(new KvStateStore(w.Kv), w.Clock, "fishing");
        scheduler.Schedule(new ScheduledTask("ghost.1", 1, "settle", "ghost"));
        scheduler.Schedule(new ScheduledTask("cup.1", 1, "settle", "cup"));
        scheduler.Schedule(new ScheduledTask("other.1", 1, "other-kind", "cup"));

        w.Say(Logic(CupCatalog()), World.Alice, "!fishgold");

        var left = scheduler.Pending();
        Assert.Equal(["other.1"], left.Select(t => t.Id)); // unknown kinds are left alone
        Assert.Equal(2, w.Log.Writes.Count(l => l.Message == "fishing.settle_orphan"));
    }

    [Fact]
    public void settlement_runs_only_when_tournaments_are_enabled()
    {
        var (w, logic, window) = Played();
        AdvancePastWindow(w, window);
        w.Flags.Overrides[FishingLogic.TournamentFlagKey] = false;
        Assert.DoesNotContain("ended", w.Say(logic, World.Alice, "!fishgold")!);
        Assert.Empty(w.Tools.Credits);
        Assert.Single(new TickScheduler(new KvStateStore(w.Kv), w.Clock, "fishing").Pending());
    }

    // ------------------------------------------------------------------ overlay

    [Fact]
    public void a_fresh_cast_pushes_one_tokenized_alert_and_registers_the_widget_once()
    {
        var w = new World(overlay: true);
        var logic = Logic(Cat.Of());
        w.Say(logic, World.Alice, "!fish");
        Cool(w, World.Alice);
        w.Say(logic, World.Alice, "!fish");

        Assert.Single(w.Overlay.Registered);
        Assert.Equal(FishingLogic.WidgetId, w.Overlay.Registered[0].WidgetId);
        Assert.Equal(OverlaySurface.AlertBox, w.Overlay.Registered[0].Surface);
        Assert.Equal(2, w.Overlay.Pushes.Count);

        using var doc = JsonDocument.Parse(w.Overlay.Pushes[0].Payload);
        var alert = doc.RootElement.GetProperty("alert");
        Assert.Equal("fishing_catch", alert.GetProperty("alert_type").GetString());
        Assert.Equal(World.Alice, alert.GetProperty("user").GetString()); // a uuid, never a name
        Assert.True(alert.GetProperty("amount").GetInt32() > 0);
        Assert.False(string.IsNullOrWhiteSpace(doc.RootElement.GetProperty("title").GetString()));
        using var descriptor = JsonDocument.Parse(w.Overlay.Registered[0].DescriptorJson);
        Assert.Equal("bottom-center", descriptor.RootElement.GetProperty("anchor").GetString());
    }

    [Fact]
    public void the_overlay_headlines_an_accident_and_the_best_catch_of_a_multi_cast()
    {
        var w = new World(overlay: true);
        var rod = Cat.Gear("rod", EquipmentSlot.Rod);
        w.SetPlayer(World.Alice, new PlayerState { Items = [new OwnedItem { Id = 1, ShopId = "rod", Equipped = true }] });
        w.Say(Logic(Cat.Of(Cat.Calm(rod: 1), [rod])), World.Alice, "!fish");
        using var doc = JsonDocument.Parse(w.Overlay.Pushes[0].Payload);
        Assert.Equal("fishing_accident", doc.RootElement.GetProperty("alert").GetProperty("alert_type").GetString());
        Assert.Equal("RodSnapped", doc.RootElement.GetProperty("title").GetString());
        Assert.Contains("lost rod", doc.RootElement.GetProperty("body").GetString());

        var w2 = new World(overlay: true);
        w2.SetPlayer(World.Alice, new PlayerState());
        w2.Say(Logic(Cat.Of(Cat.Calm(line: 1))), World.Alice, "!fish");
        using var empty = JsonDocument.Parse(w2.Overlay.Pushes[0].Payload);
        Assert.Equal("nothing equipped was lost", empty.RootElement.GetProperty("body").GetString());

        var w3 = new World(overlay: true);
        w3.Say(Logic(), World.Alice, "!fish 6");
        Assert.Single(w3.Overlay.Pushes);
    }

    [Fact]
    public void a_replayed_cast_does_not_push_the_overlay_again()
    {
        var w = new World(overlay: true);
        var logic = Logic();
        w.Send(logic, World.Alice, "!fish", "evt-1");
        w.Send(logic, World.Alice, "!fish", "evt-1");
        Assert.Single(w.Overlay.Pushes);
    }

    [Fact]
    public void the_overlay_is_opt_in_and_silent_when_off()
    {
        var w = new World();
        var text = w.Say(Logic(Cat.Of()), World.Alice, "!fish")!;
        Assert.DoesNotContain("overlay", text);
        Assert.Empty(w.Overlay.Pushes);
        Assert.Empty(w.Overlay.Registered);
        Assert.Contains(w.Flags.Calls, c => c.Key == FishingLogic.OverlayFlagKey && c.DefaultValue == false);
    }

    [Fact]
    public void an_enabled_overlay_that_the_host_refuses_is_reported_in_the_reply_and_logged_as_an_error_but_the_cast_stands()
    {
        var w = new World(overlay: true);
        w.Overlay.Failure = new WaddleTransportException(TransportErrorInfo.RetryableError("denied", "no overlay.media grant"));

        var text = w.Say(Logic(Cat.Of()), World.Alice, "!fish");

        Assert.Contains("lb)", text);
        Assert.Contains("[overlay unavailable (denied)", text);
        Assert.Contains(FishingLogic.OverlayFlagKey, text);
        Assert.Equal(1, w.Player(World.Alice).Casts);
        Assert.Contains(w.Log.Writes, l => l.Level == WaddleSdk.Log.WaddleLogLevel.Error && l.Message == "fishing.overlay_failed" && l.FieldsJson.Contains("denied"));

    }

    // ------------------------------------------------------------------ limits, hygiene

    [Fact]
    public void a_cast_stays_far_below_the_64_op_kv_cap_even_with_tournaments_and_ten_casts()
    {
        var w = new World(tournaments: true);
        var logic = Logic(Cat.Of(tournaments: [Cup(), Cup("cup2", ScoreCategory.MostCatches)]));
        w.Kv.ResetCounters();
        w.Say(logic, World.Alice, "!fish 10");
        Assert.InRange(w.Kv.Ops, 1, 40);

        var shop = new World();
        shop.SetPlayer(World.Alice, new PlayerState { Gold = 999 });
        shop.Kv.ResetCounters();
        shop.Say(Logic(), World.Alice, "!fishbuy worms 5");
        Assert.InRange(shop.Kv.Ops, 1, 20);
    }

    [Fact]
    public void every_kv_key_obeys_the_charset_and_length_rules_and_logs_carry_no_identities()
    {
        var w = new World(tournaments: true);
        var logic = Logic(CupCatalog());
        w.Say(logic, World.Alice, "!fish 3");
        w.Say(logic, World.Alice, "!fishbuy worms");
        w.Say(logic, World.Alice, "!fishequip 1");
        w.Say(logic, World.Alice, "!fishtourney");

        Assert.NotEmpty(w.Kv.Inner.SetCalls);
        Assert.All(w.Kv.Inner.SetCalls, c =>
        {
            Assert.Matches("^[A-Za-z0-9_.-]{1,256}$", c.Key);
            Assert.DoesNotContain(':', c.Key);
        });
        Assert.NotEmpty(w.Log.Writes);
        Assert.All(w.Log.Writes, l =>
        {
            Assert.DoesNotContain(World.Alice, l.FieldsJson);
            Assert.DoesNotContain("platform-actor", l.FieldsJson);
        });
    }

    [Fact]
    public void clip_never_cuts_through_a_user_placeholder()
    {
        Assert.Equal("short", FishingLogic.Clip("short"));
        var filler = new string('x', FishingLogic.MaxReplyLength - 10);
        var clipped = FishingLogic.Clip(filler + " {user:" + World.Alice + "} tail");
        Assert.True(clipped.Length <= FishingLogic.MaxReplyLength);
        Assert.DoesNotContain("{user", clipped);
        Assert.EndsWith("...", clipped);

        var whole = FishingLogic.Clip(new string('y', 200) + " {user:" + World.Alice + "} " + new string('z', 400));
        Assert.Contains("{user:" + World.Alice + "}", whole);
        Assert.True(whole.Length <= FishingLogic.MaxReplyLength);
    }

    [Fact]
    public void construction_validates_the_catalog_loudly()
    {
        Assert.Throws<ArgumentException>(() => new FishingLogic(Cat.Of(fish: [])));
        Assert.Throws<ArgumentException>(() => new FishingLogic(Cat.Of(shop: [Cat.Gear("bad", EquipmentSlot.Rod, amount: 9)])));
        Assert.Throws<ArgumentException>(() => new FishingLogic(Cat.Of(tournaments: [Cup() with { PeriodMinutes = 0 }])));
        _ = new FishingLogic(); // default catalog, real CSPRNG seed source
    }

    [Fact]
    public void the_real_seed_source_yields_working_casts()
    {
        var w = new World();
        Assert.Contains("lb)", w.Say(new FishingLogic(Cat.Of()), World.Alice, "!fish"));
    }

    [Fact]
    public void the_json_state_round_trips_including_a_fresh_aggregates_sentinel_minimum()
    {
        var w = new World();
        var store = w.Store;
        var agg = new TournamentAggregate();
        store.SaveAggregate("win", World.Alice, agg);
        Assert.Equal(double.MaxValue, store.LoadAggregate("win", World.Alice).MinWeight);
        Assert.Equal(0, store.LoadAggregate("win", World.Bob).Count);
        Assert.False(store.WidgetRegistered());
        store.MarkWidgetRegistered();
        Assert.True(store.WidgetRegistered());
        Assert.Empty(store.LoadLifetime().Earned);
        var boards = new LifetimeBoards { Earned = [new LifetimeRow("u", 5)] };
        store.SaveLifetime(boards);
        Assert.Equal(5, store.LoadLifetime().Earned[0].Value);
        // A corrupt value reads as empty rather than throwing.
        new KvStateStore(w.Kv).Set(StateKey.Of("p", StateKey.Hash(World.Carol)), [1, 2, 3], 0);
        Assert.Equal(0, w.Player(World.Carol).Casts);
    }
}

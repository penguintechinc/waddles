using System.Text.Json;
using SuperPenguin.DropGameCore;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinUfoclaw.Tests;

/// <summary>End-to-end `!ufo` behaviour over the shared host base: money, replay, cooldown, refusals and the overlay envelope.</summary>
public class UfoClawLogicTests
{
    private const string Alice = World.Alice;
    private const ulong Entropy = 7;

    private static (World World, UfoClawLogic Game) Setup(long balance = 1000, UfoClawConfig? config = null)
    {
        var world = new World(UfoClawLogic.FlagKey);
        world.Ledger.Seed(Alice, balance);
        return (world, new UfoClawLogic(() => Entropy, config));
    }

    private static UfoClawConfig OnePrize(string label, int pct) => new([new UfoClawPrize(label, pct, 1, "#ffd60a")]);

    private static ulong SeedFor(string messageId, string command = "ufo", string platform = "twitch") =>
        SeedDerivation.Derive(messageId, platform, Alice, command, Entropy);

    private static void ReleaseCooldown(World world, string command = "ufo") =>
        StateCooldown.Release(new KvStateStore(world.Kv), command, Alice);

    [Fact]
    public void A_play_settles_exactly_the_prize_the_recorded_seed_produces()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!ufo 100", "m1")!;
        var expected = UfoClawSimulation.Run(SeedFor("m1"), UfoClawConfig.Default);
        var payout = UfoClawLogic.PayoutFor(100, expected.Winner);
        Assert.Equal(1000 - 100 + (long)payout, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains($"the claw grabbed {expected.Winner.Label}", text);
        Assert.Contains($"now has {1000 - 100 + (long)payout} stars", text);
    }

    [Fact]
    public void Every_prize_tier_occurs_and_pays_its_multiplier_of_the_stake()
    {
        var (world, game) = Setup(balance: 10_000_000);
        var seen = new HashSet<string>();
        for (var i = 0; i < 400; i++)
        {
            var before = world.Ledger.Balance(Alice);
            var id = $"msg-{i}";
            var text = world.Send(game, Alice, "!ufo 100", id)!;
            var prize = UfoClawSimulation.Run(SeedFor(id), UfoClawConfig.Default).Winner;
            Assert.Equal(prize.MultiplierPct - 100, world.Ledger.Balance(Alice) - before);
            Assert.Contains(prize.Label, text);
            Assert.Contains(prize.MultiplierPct switch { > 100 => " won ", 100 => " got back ", _ => " lost " }, text);
            seen.Add(prize.Label);
            ReleaseCooldown(world);
        }

        Assert.Equal(UfoClawConfig.Default.Prizes.Select(p => p.Label).Order(), seen.Order());
    }

    [Theory]
    [InlineData("!ufo 100", 150, 100, "the claw grabbed Cookie (x1.5)", " won 50 ")]
    [InlineData("!ufo 3", 150, 3, "the claw grabbed Cookie (x1.5)", " won 1 ")]
    [InlineData("!ufo 100", 0, 100, "the claw grabbed Cookie", " lost 100 ")]
    [InlineData("!ufo 100", 100, 100, "the claw grabbed Cookie (x1)", " got back 100 ")]
    [InlineData("!ufo 100", 50, 100, "the claw grabbed Cookie (x0.5)", " lost 50 ")]
    [InlineData("!ufoclaw 200", 10000, 200, "the claw grabbed Cookie (x100)", " won 19800 ")]
    public void Payout_math_is_the_multiplier_percent_of_the_stake_rounded_down(string command, int pct, int stake, string outcome, string wording)
    {
        var (world, game) = Setup(balance: 1000, OnePrize("Cookie", pct));
        var text = world.Send(game, Alice, command, "m1")!;
        var payout = (long)(stake * (long)pct / 100);
        Assert.Equal(1000 - stake + payout, world.Ledger.Balance(Alice));
        Assert.Equal((ulong)stake, world.Ledger.Moved.Single().Amount);
        Assert.Contains(outcome, text);
        Assert.Contains(wording, text);
    }

    [Fact]
    public void Payout_is_never_above_the_hosts_100x_ceiling_for_any_default_prize()
    {
        foreach (var prize in UfoClawConfig.Default.Prizes)
        {
            Assert.True(UfoClawLogic.PayoutFor(UfoClawLogic.MaxBet, prize) <= UfoClawLogic.MaxBet * 100);
        }

        Assert.Equal(UfoClawLogic.MaxBet * 100, UfoClawLogic.PayoutFor(UfoClawLogic.MaxBet, new UfoClawPrize("x", UfoClawConfig.MaxMultiplierPct, 1, "#ffffff")));
    }

    [Fact]
    public void Overlay_envelope_carries_the_seed_pool_winner_and_decoys()
    {
        var (world, game) = Setup(config: UfoClawConfig.Default with { SlipChancePercent = 100 });
        world.Send(game, Alice, "!ufo 100", "m1");
        var (widgetId, surface, payload) = Assert.Single(world.Overlay.Pushes);
        Assert.Equal("ufoclaw", widgetId);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var expected = UfoClawSimulation.Run(SeedFor("m1"), UfoClawConfig.Default with { SlipChancePercent = 100 });
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal("ufoclaw", extra.GetProperty("game").GetString());
        Assert.Equal("settled", extra.GetProperty("kind").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedFor("m1")), extra.GetProperty("seed").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        Assert.Equal(100, extra.GetProperty("stake").GetInt64());
        Assert.Equal((long)UfoClawLogic.PayoutFor(100, expected.Winner), extra.GetProperty("payout").GetInt64());
        var data = extra.GetProperty("data");
        Assert.Equal(1920, data.GetProperty("scene").GetProperty("w").GetInt32());
        Assert.Equal(1080, data.GetProperty("scene").GetProperty("h").GetInt32());
        Assert.Equal(expected.WinningIndex, data.GetProperty("winningIndex").GetInt32());
        Assert.Equal(expected.Winner.Label, data.GetProperty("label").GetString());
        Assert.Equal(expected.Winner.MultiplierPct, data.GetProperty("multiplierPct").GetInt32());
        Assert.Equal(expected.Winner.MultiplierPct > 0, data.GetProperty("win").GetBoolean());
        Assert.True(data.GetProperty("slip").GetBoolean());
        Assert.Equal(expected.Feints, data.GetProperty("feints").EnumerateArray().Select(e => e.GetInt32()));
        var pool = data.GetProperty("pool");
        Assert.Equal(7, pool.GetArrayLength());
        Assert.Equal("Dud", pool[0].GetProperty("label").GetString());
        Assert.Equal(1500, pool[6].GetProperty("multiplierPct").GetInt32());
        Assert.Equal(10m, pool[6].GetProperty("weight").GetDecimal());
        Assert.Equal("#ffd60a", pool[6].GetProperty("color").GetString());
        var rules = data.GetProperty("rules");
        Assert.True(rules.GetProperty("tease").GetBoolean());
        Assert.Equal(100, rules.GetProperty("slipChancePercent").GetInt32());
        Assert.Equal(3, rules.GetProperty("maxSuspenseDrops").GetInt32());
        Assert.Equal(20, rules.GetProperty("durationSeconds").GetDouble());
        Assert.Equal(4, rules.GetProperty("revealSeconds").GetDouble());
        Assert.Equal(1, rules.GetProperty("movementSpeed").GetDouble());
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("UFO Claw", world.Overlay.Registered[0].DisplayName);
    }

    [Fact]
    public void A_straight_grab_publishes_an_empty_feint_list()
    {
        var (world, game) = Setup(config: UfoClawConfig.Default with { Tease = false });
        world.Send(game, Alice, "!ufo 10", "m1");
        var data = JsonDocument.Parse(world.Overlay.Pushes.Single().Payload).RootElement.GetProperty("extra").GetProperty("data");
        Assert.False(data.GetProperty("slip").GetBoolean());
        Assert.Equal(0, data.GetProperty("feints").GetArrayLength());
    }

    [Fact]
    public void Redelivery_replays_the_same_grab_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!ufo 100", "m1");
        var balance = world.Ledger.Balance(Alice);
        Assert.Equal(first, world.Send(game, Alice, "!ufo 100", "m1"));
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Fact]
    public void Cooldown_is_ten_seconds_and_silent()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!ufo 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!ufo 10", "m2"));
        Assert.Equal(10U, UfoClawLogic.CooldownSeconds);
    }

    [Fact]
    public void Max_bet_clamps_to_500()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!ufo 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Theory]
    [InlineData("!ufo", "To play the UFO claw please do")]
    [InlineData("!ufo abc", "The amount must be a number, max, or all")]
    [InlineData("!ufo 0", "The amount needs to be greater then 0")]
    [InlineData("!ufo 99999", "You don't have that many.")]
    [InlineData("!ufo 10 extra", "To play the UFO claw please do")]
    public void Refusals_use_the_originals_wording(string text, string contains)
    {
        var (world, game) = Setup();
        Assert.Contains(contains, world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void Both_command_names_are_answered_and_others_are_ignored()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!ufo 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!ufoclaw 10", "m1b")); // alias shares the primary cooldown
        ReleaseCooldown(world);
        Assert.NotNull(world.Send(game, Alice, "!ufoclaw 10", "m2"));
        Assert.Null(world.Send(game, Alice, "!claw 10", "m3"));
        Assert.Null(world.Send(game, Alice, "hello", "m4"));
        Assert.Equal(2, world.Ledger.Moved.Count);
    }

    [Fact]
    public void Insufficient_funds_at_settlement_replies_and_releases_the_cooldown()
    {
        var (world, game) = Setup(balance: 50);
        world.Ledger.BeforeCall = (_, _) => world.Ledger.Seed(Alice, 5);
        var text = world.Send(game, Alice, "!ufo 50", "m1")!;
        Assert.Contains("you don't have 50 stars to bet", text);
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
        world.Ledger.BeforeCall = null;
        world.Ledger.Seed(Alice, 500);
        Assert.NotNull(world.Send(game, Alice, "!ufo 5", "m2"));
    }

    [Fact]
    public void Flag_off_no_community_and_unlinked_paths()
    {
        var (world, game) = Setup();
        world.Flags.Overrides[UfoClawLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!ufo 10", "m1"));
        world.Flags.Overrides[UfoClawLogic.FlagKey] = true;
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!ufo 10"), world.HostFor(Alice, "m2", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!ufo 10", "m3"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        Assert.NotNull(world.Send(game, Alice, "!ufo 100", "m1"));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, w => w.Message == "ufoclaw.overlay_push_failed");
    }

    [Fact]
    public void Kv_failure_propagates_and_moves_no_money()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!ufo 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_grabs_differ()
    {
        var world = new World(UfoClawLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new UfoClawLogic();
        world.Send(game, Alice, "!ufo 10", "m1");
        ReleaseCooldown(world);
        world.Send(game, Alice, "!ufo 10", "m1b");
        var seeds = world.Overlay.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString()).ToHashSet();
        Assert.Equal(2, seeds.Count);
    }

    [Fact]
    public void A_broken_pool_fails_at_construction_not_at_settlement()
    {
        Assert.Throws<ArgumentException>(() => new UfoClawLogic(() => Entropy, new UfoClawConfig([])));
    }
}

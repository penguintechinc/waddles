using System.Text.Json;
using SuperPenguin.DropGameCore;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinPlinko.Tests;

/// <summary>End-to-end `!plinko` behaviour over the shared host base: money, replay, cooldown, refusals and the overlay envelope.</summary>
public class PlinkoLogicTests
{
    private const string Alice = World.Alice;
    private const ulong Entropy = 7;

    private static (World World, PlinkoLogic Game) Setup(long balance = 1000)
    {
        var world = new World(PlinkoLogic.FlagKey);
        world.Ledger.Seed(Alice, balance);
        return (world, new PlinkoLogic(() => Entropy));
    }

    private static PlinkoResult Expected(string messageId, string platform = "twitch") =>
        PlinkoSimulation.Run(SeedDerivation.Derive(messageId, platform, Alice, "plinko", Entropy), PlinkoConfig.Default);

    [Fact]
    public void A_play_settles_exactly_the_bucket_the_recorded_seed_produces()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!plinko 100", "m1")!;
        var expected = Expected("m1");
        var payout = PlinkoLogic.PayoutFor(100, expected.Outcome);
        Assert.Equal(1000 - 100 + (long)payout, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains($"the puck landed in {expected.Outcome.Label}", text);
        Assert.Contains($"now has {1000 - 100 + (long)payout} stars", text);
    }

    [Fact]
    public void Winning_and_losing_seeds_both_occur_and_pay_correctly()
    {
        var (world, game) = Setup(balance: 1_000_000);
        var wins = 0;
        var losses = 0;
        for (var i = 0; i < 60; i++)
        {
            var before = world.Ledger.Balance(Alice);
            var id = $"msg-{i}";
            var text = world.Send(game, Alice, "!plinko 100", id)!;
            var expected = Expected(id);
            var delta = world.Ledger.Balance(Alice) - before;
            if (expected.Outcome.IsWin)
            {
                wins++;
                Assert.Equal(400, delta);
                Assert.Contains("x5", text);
                Assert.Contains(" won ", text);
            }
            else
            {
                losses++;
                Assert.Equal(-100, delta);
                Assert.Contains(" lost ", text);
            }

            StateCooldownRelease(world);
        }

        Assert.True(wins > 0 && losses > 0);
    }

    private static void StateCooldownRelease(World world) =>
        WaddleSdk.State.StateCooldown.Release(new WaddleSdk.State.KvStateStore(world.Kv), "plinko", Alice);

    [Fact]
    public void Overlay_envelope_carries_the_seed_result_and_keyframes()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!plinko 100", "m1");
        var (widgetId, surface, payload) = Assert.Single(world.Overlay.Pushes);
        Assert.Equal("plinko", widgetId);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var expected = Expected("m1");
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal("plinko", extra.GetProperty("game").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedDerivation.Derive("m1", "twitch", Alice, "plinko", Entropy)), extra.GetProperty("seed").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        var data = extra.GetProperty("data");
        Assert.Equal(expected.BucketIndex, data.GetProperty("bucket").GetInt32());
        Assert.Equal(expected.Outcome.Label, data.GetProperty("label").GetString());
        Assert.Equal(expected.Outcome.IsWin, data.GetProperty("win").GetBoolean());
        Assert.Equal(expected.Keyframes.Count, data.GetProperty("keyframes").GetArrayLength());
        Assert.Equal(expected.PegHits.Count, data.GetProperty("pegHits").GetArrayLength());
        Assert.Equal(expected.Keyframes[^1].X, data.GetProperty("keyframes")[expected.Keyframes.Count - 1][1].GetInt32());
        var board = data.GetProperty("board");
        Assert.Equal(5, board.GetProperty("buckets").GetArrayLength());
        Assert.Equal(4, board.GetProperty("rows").GetInt32());
        Assert.Equal(1920, board.GetProperty("w").GetInt32());
        Assert.Equal((long)(PlinkoLogic.PayoutFor(100, expected.Outcome) * 100 / 100), data.GetProperty("multiplierPct").GetInt64());
        Assert.False(data.GetProperty("timedOut").GetBoolean());
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("Plinko Bounce", world.Overlay.Registered[0].DisplayName);
    }

    [Fact]
    public void Redelivery_replays_the_same_drop_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!plinko 100", "m1");
        var balance = world.Ledger.Balance(Alice);
        Assert.Equal(first, world.Send(game, Alice, "!plinko 100", "m1"));
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Fact]
    public void Cooldown_is_ten_seconds_and_silent()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!plinko 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!plinko 10", "m2"));
        Assert.Equal(10U, PlinkoLogic.CooldownSeconds);
    }

    [Fact]
    public void Max_bet_clamps_to_500()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!plinko 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Theory]
    [InlineData("!plinko", "To plinko please do")]
    [InlineData("!plinko abc", "The amount must be a number, max, or all")]
    [InlineData("!plinko 0", "The amount needs to be greater then 0")]
    [InlineData("!plinko 99999", "You don't have that many.")]
    [InlineData("!plinko 10 sad_emote spring", "To plinko please do")]
    public void Refusals_use_the_originals_wording(string text, string contains)
    {
        var (world, game) = Setup();
        Assert.Contains(contains, world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void Flag_off_no_community_and_unlinked_paths()
    {
        var (world, game) = Setup();
        world.Flags.Overrides[PlinkoLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!plinko 10", "m1"));
        world.Flags.Overrides[PlinkoLogic.FlagKey] = true;
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!plinko 10"), world.HostFor(Alice, "m2", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!plinko 10", "m3"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        Assert.NotNull(world.Send(game, Alice, "!plinko 100", "m1"));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, w => w.Message == "plinko.overlay_push_failed");
    }

    [Fact]
    public void Kv_failure_propagates_and_moves_no_money()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!plinko 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_drops_differ()
    {
        var world = new World(PlinkoLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new PlinkoLogic();
        world.Send(game, Alice, "!plinko 10", "m1");
        StateCooldownRelease(world);
        world.Send(game, Alice, "!plinko 10", "m1b");
        var seeds = world.Overlay.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString()).ToHashSet();
        Assert.Equal(2, seeds.Count);
    }

    [Fact]
    public void Custom_board_and_scored_buckets_pay_score_percent_of_the_stake()
    {
        PlinkoBucket[] row = [new("JACKPOT", 2000, true, 1, "#fff")];
        var world = new World(PlinkoLogic.FlagKey);
        world.Ledger.Seed(Alice, 1000);
        var game = new PlinkoLogic(() => Entropy, PlinkoConfig.Default with { Buckets = row });
        var text = world.Send(game, Alice, "!plinko 10", "m1")!;
        Assert.Equal(1000 - 10 + 200, world.Ledger.Balance(Alice));
        Assert.Contains("JACKPOT (x20)", text);
    }

    [Fact]
    public void Json_data_is_valid_for_a_timed_out_result()
    {
        var r = new PlinkoResult(0.5, 1, new BucketOutcome(0, false, "MISS", false), [new PlinkoKeyframe(0, 5000, -600)], [], true);
        var json = PlinkoLogic.DataJson(r, PlinkoConfig.Default, 0, 0);
        var data = JsonDocument.Parse(json).RootElement;
        Assert.True(data.GetProperty("timedOut").GetBoolean());
        Assert.Equal(0, data.GetProperty("multiplierPct").GetInt64());
    }
}

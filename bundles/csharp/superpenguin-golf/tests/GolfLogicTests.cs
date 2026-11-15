using System.Text.Json;
using SuperPenguin.DropGameCore;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinGolf.Tests;

/// <summary>End-to-end `!golf` behaviour over the shared host base: money, replay, cooldown, refusals and the overlay envelope.</summary>
public class GolfLogicTests
{
    private const string Alice = World.Alice;
    private const ulong Entropy = 7;

    private static (World World, GolfLogic Game) Setup(long balance = 1000, GolfConfig? config = null)
    {
        var world = new World(GolfLogic.FlagKey);
        world.Ledger.Seed(Alice, balance);
        return (world, new GolfLogic(() => Entropy, config));
    }

    private static GolfShot Expected(string messageId, int? power, string platform = "twitch", GolfConfig? config = null) =>
        GolfEngine.Shoot(SeedDerivation.Derive(messageId, platform, Alice, "golf", Entropy), power, config ?? GolfConfig.Default);

    private static void ReleaseCooldown(World world) =>
        WaddleSdk.State.StateCooldown.Release(new WaddleSdk.State.KvStateStore(world.Kv), "golf", Alice);

    [Fact]
    public void A_play_settles_exactly_the_shot_the_recorded_seed_produces()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!golf 100 79", "m1")!;
        var expected = Expected("m1", 79);
        var payout = GolfLogic.PayoutFor(100, expected);
        Assert.Equal(1000 - 100 + (long)payout, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains("79 power", text);
        Assert.Contains($"now has {1000 - 100 + (long)payout} stars", text);
    }

    [Fact]
    public void Sunk_green_and_missed_shots_all_occur_and_pay_by_the_documented_mapping()
    {
        var (world, game) = Setup(balance: 1_000_000);
        int sunk = 0, green = 0, miss = 0;
        for (var i = 0; i < 80; i++)
        {
            var before = world.Ledger.Balance(Alice);
            var id = $"msg-{i}";
            var text = world.Send(game, Alice, "!golf 100 79", id)!;
            var shot = Expected(id, 79);
            var delta = world.Ledger.Balance(Alice) - before;
            if (shot.IsSunk)
            {
                sunk++;
                Assert.Equal(200, delta);
                Assert.Contains("HOLE-IN-ONE", text);
                Assert.Contains("x3", text);
            }
            else if (shot.IsWin)
            {
                green++;
                Assert.Equal((long)shot.BaseScore * 2 - 100, delta);
                Assert.Contains($"{shot.BaseScore} pts", text);
                Assert.Contains("on the green", text);
            }
            else
            {
                miss++;
                Assert.Equal(-100, delta);
                Assert.Contains("off the green", text);
                Assert.Contains(" lost ", text);
            }

            ReleaseCooldown(world);
        }

        Assert.True(sunk > 0 && green > 0 && miss > 0, $"sunk {sunk} green {green} miss {miss}");
    }

    [Fact]
    public void Omitting_the_power_draws_upstreams_default_power_from_the_seed()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!golf 100", "m1");
        var expected = Expected("m1", null);
        var data = JsonDocument.Parse(world.Overlay.Pushes.Single().Payload).RootElement.GetProperty("extra").GetProperty("data");
        Assert.InRange(expected.Power, 65, 85);
        Assert.Equal(expected.Power, data.GetProperty("power").GetInt32());
    }

    [Fact]
    public void Overlay_envelope_carries_the_seed_result_and_keyframes()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!golf 100 79", "m1");
        var (widgetId, surface, payload) = Assert.Single(world.Overlay.Pushes);
        Assert.Equal("golf", widgetId);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var shot = Expected("m1", 79);
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal("golf", extra.GetProperty("game").GetString());
        Assert.Equal("settled", extra.GetProperty("kind").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedDerivation.Derive("m1", "twitch", Alice, "golf", Entropy)), extra.GetProperty("seed").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        Assert.Equal(100, extra.GetProperty("stake").GetInt32());
        Assert.Equal((long)GolfLogic.PayoutFor(100, shot), extra.GetProperty("payout").GetInt64());
        var data = extra.GetProperty("data");
        Assert.Equal(shot.Condition.ToString(), data.GetProperty("condition").GetString());
        Assert.Equal(GolfEngine.Label(shot.Condition), data.GetProperty("conditionLabel").GetString());
        Assert.Equal("Driver", data.GetProperty("club").GetString());
        Assert.Equal(1, data.GetProperty("strokeNumber").GetInt32());
        Assert.Equal(1, data.GetProperty("maxStrokes").GetInt32());
        Assert.Equal(79, data.GetProperty("power").GetInt32());
        Assert.Equal(shot.IsSunk, data.GetProperty("sunk").GetBoolean());
        Assert.Equal(shot.IsLipOut, data.GetProperty("lipOut").GetBoolean());
        Assert.Equal(shot.IsWin, data.GetProperty("win").GetBoolean());
        Assert.Equal(shot.BaseScore, data.GetProperty("score").GetInt32());
        Assert.Equal((int)Math.Round(shot.RestPosition * 10000), data.GetProperty("rest").GetInt32());
        Assert.Equal(shot.TotalTicks, data.GetProperty("totalTicks").GetInt32());
        var frames = data.GetProperty("keyframes");
        Assert.Equal(shot.Keyframes.Count, frames.GetArrayLength());
        Assert.Equal(4, frames[0].GetArrayLength());
        Assert.Equal(shot.TotalTicks, frames[shot.Keyframes.Count - 1][0].GetInt32());
        var course = data.GetProperty("course");
        Assert.Equal(8400, course.GetProperty("target").GetInt32());
        Assert.Equal(140, course.GetProperty("cupRadius").GetInt32());
        Assert.Equal(350, course.GetProperty("greenRadius").GetInt32());
        Assert.Equal(500, course.GetProperty("tee").GetInt32());
        Assert.Equal(100, course.GetProperty("maxScore").GetInt32());
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("Golf Chip and Putt", world.Overlay.Registered[0].DisplayName);
    }

    [Fact]
    public void Redelivery_replays_the_same_shot_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!golf 100 79", "m1");
        var balance = world.Ledger.Balance(Alice);
        Assert.Equal(first, world.Send(game, Alice, "!golf 100 79", "m1"));
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Fact]
    public void Cooldown_is_five_seconds_and_silent()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!golf 10 79", "m1"));
        Assert.Null(world.Send(game, Alice, "!golf 10 79", "m2"));
        Assert.Equal(5U, GolfLogic.CooldownSeconds);
    }

    [Fact]
    public void Max_bet_clamps_to_500()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!golf 900 79", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Theory]
    [InlineData("!golf", "To golf please do")]
    [InlineData("!golf abc", "The amount must be a number, max, or all")]
    [InlineData("!golf 0", "The amount needs to be greater then 0")]
    [InlineData("!golf 99999", "You don't have that many.")]
    [InlineData("!golf 10 0", "To golf please do")]
    [InlineData("!golf 10 101", "To golf please do")]
    [InlineData("!golf 10 -5", "To golf please do")]
    [InlineData("!golf 10 abc", "To golf please do")]
    [InlineData("!golf 10 78 PenguinHype", "To golf please do")]
    public void Refusals_leave_the_ledger_and_overlay_untouched(string text, string contains)
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
        world.Flags.Overrides[GolfLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!golf 10 79", "m1"));
        world.Flags.Overrides[GolfLogic.FlagKey] = true;
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!golf 10 79"), world.HostFor(Alice, "m2", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!golf 10 79", "m3"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        Assert.NotNull(world.Send(game, Alice, "!golf 100 79", "m1"));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, w => w.Message == "golf.overlay_push_failed");
    }

    [Fact]
    public void Kv_failure_propagates_and_moves_no_money()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!golf 10 79", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_shots_differ()
    {
        var world = new World(GolfLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new GolfLogic();
        world.Send(game, Alice, "!golf 10 79", "m1");
        ReleaseCooldown(world);
        world.Send(game, Alice, "!golf 10 79", "m1b");
        var seeds = world.Overlay.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString()).ToHashSet();
        Assert.Equal(2, seeds.Count);
    }

    [Fact]
    public void Payout_mapping_is_sunk_three_times_green_score_times_two_and_zero_off_the_green()
    {
        GolfShot Make(bool sunk, int score) => new(79, 79, GreenCondition.StandardGreen, 0.6, 0.84, 0.84, 0, sunk, false, score, 100, []);
        Assert.Equal(300, GolfLogic.PercentFor(Make(true, 100)));
        Assert.Equal(200, GolfLogic.PercentFor(Make(false, 100)));
        Assert.Equal(2, GolfLogic.PercentFor(Make(false, 1)));
        Assert.Equal(0, GolfLogic.PercentFor(Make(false, 0)));
        Assert.Equal(300UL, GolfLogic.PayoutFor(100, Make(true, 100)));
        Assert.Equal(2UL, GolfLogic.PayoutFor(100, Make(false, 1)));
        Assert.Equal(0UL, GolfLogic.PayoutFor(1, Make(false, 1)));
        Assert.True(GolfLogic.PercentFor(Make(true, 1000)) < 10_000);
    }

    [Fact]
    public void No_power_choice_pays_back_more_than_the_stake_over_many_shots()
    {
        // The host ledger refuses more than 100x per wager; the mapping must also leave the best skilled strategy below break-even.
        const int Shots = 4000;
        double best = 0;
        var bestPower = 0;
        for (var power = 1; power <= 100; power++)
        {
            ulong paid = 0;
            for (ulong seed = 1; seed <= Shots; seed++)
            {
                paid += GolfLogic.PayoutFor(100, GolfEngine.Shoot(seed * 2654435761UL, power, GolfConfig.Default));
            }

            var rtp = paid / (Shots * 100.0);
            if (rtp > best)
            {
                best = rtp;
                bestPower = power;
            }
        }

        Assert.InRange(bestPower, 74, 84);
        Assert.InRange(best, 0.6, 1.0);
    }

    [Fact]
    public void Describe_covers_every_outcome_wording()
    {
        GolfShot Make(bool sunk, bool lip, int score, double dist) =>
            new(79, 79, GreenCondition.HeavyDew, 0.6, 0.84, 0.84 + dist, dist, sunk, lip, score, 100, []);
        Assert.Contains("HOLE-IN-ONE", GolfLogic.Describe(Make(true, false, 100, 0), 300, 100));
        Assert.Contains("lipped out and stopped 2.1yd from the pin on the green, 65 pts", GolfLogic.Describe(Make(false, true, 65, 0.0214), 195, 100));
        Assert.Contains("stopped 3.2yd from the pin on the green, 16 pts", GolfLogic.Describe(Make(false, false, 16, 0.0317), 48, 100));
        Assert.Contains("lipped out and stopped 3.9yd from the pin, off the green", GolfLogic.Describe(Make(false, true, 0, 0.0392), 0, 100));
        Assert.Contains("stopped 20yd from the pin, off the green (79 power on a Heavy Dew)", GolfLogic.Describe(Make(false, false, 0, 0.2), 0, 100));
        Assert.Contains("x0", GolfLogic.Describe(Make(true, false, 100, 0), 0, 0));
    }

    [Fact]
    public void Power_parsing_accepts_only_one_to_a_hundred()
    {
        Assert.Null(GolfLogic.ParsePower([], out var ok0));
        Assert.True(ok0);
        Assert.Equal(1, GolfLogic.ParsePower(["1"], out var ok1));
        Assert.True(ok1);
        Assert.Equal(100, GolfLogic.ParsePower(["100"], out _));
        foreach (var bad in new[] { "0", "101", "-3", "7.5", "x", "" })
        {
            Assert.Null(GolfLogic.ParsePower([bad], out var valid));
            Assert.False(valid);
        }

        Assert.Null(GolfLogic.ParsePower(["5", "6"], out var two));
        Assert.False(two);
    }

    [Fact]
    public void A_malformed_power_reaching_the_simulation_is_a_loud_bug_not_a_silent_default()
    {
        var game = new GolfLogic(() => Entropy);
        Assert.Throws<InvalidOperationException>(() => game.PlayShot(1, 100, ["abc"]));
    }

    [Fact]
    public void Json_data_is_valid_for_a_zero_stake()
    {
        var shot = GolfEngine.Shoot(3, 79, GolfConfig.Default);
        var data = JsonDocument.Parse(GolfLogic.DataJson(shot, GolfConfig.Default, 0, 0)).RootElement;
        Assert.Equal(0, data.GetProperty("multiplierPct").GetInt64());
    }

    [Fact]
    public void A_custom_config_flows_through_to_the_play()
    {
        var config = GolfConfig.Default with { FixedCondition = GreenCondition.FastLinks, PowerVariancePercent = 0, MaxScore = 200 };
        var (world, game) = Setup(config: config);
        world.Send(game, Alice, "!golf 10 70", "m1");
        var data = JsonDocument.Parse(world.Overlay.Pushes.Single().Payload).RootElement.GetProperty("extra").GetProperty("data");
        Assert.Equal("FastLinks", data.GetProperty("condition").GetString());
        Assert.Equal(200, data.GetProperty("course").GetProperty("maxScore").GetInt32());
        Assert.Equal(70.0, data.GetProperty("effectivePower").GetInt32() / 100.0);
    }
}

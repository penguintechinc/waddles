using System.Text.Json;
using SuperPenguin.DropGameCore;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinParachute.Tests;

/// <summary>End-to-end `!parachute` behaviour over the shared host base: money, replay, cooldown, refusals and the overlay envelope.</summary>
public class ParachuteLogicTests
{
    private const string Alice = World.Alice;
    private const ulong Entropy = 7;

    private static (World World, ParachuteLogic Game) Setup(long balance = 1000, ParachuteConfig? config = null)
    {
        var world = new World(ParachuteLogic.FlagKey);
        world.Ledger.Seed(Alice, balance);
        return (world, new ParachuteLogic(() => Entropy, config));
    }

    private static ParachuteResult Expected(string messageId, string platform = "twitch", ParachuteConfig? config = null) =>
        ParachuteSimulationEngine.Run(SeedDerivation.Derive(messageId, platform, Alice, "parachute", Entropy), config ?? ParachuteConfig.Default);

    private static void ReleaseCooldown(World world) =>
        StateCooldown.Release(new KvStateStore(world.Kv), "parachute", Alice);

    [Fact]
    public void A_play_settles_exactly_the_payout_the_recorded_seed_produces()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!parachute 100", "m1")!;
        var expected = Expected("m1");
        var payout = ParachuteLogic.PayoutFor(100, expected.Score);
        Assert.Equal(1000 - 100 + (long)payout, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains($"now has {1000 - 100 + (long)payout} stars", text);
        Assert.Contains(expected.IsWin ? $"score of {expected.Score}" : "platform", text);
    }

    [Fact]
    public void Winning_and_losing_drops_both_occur_and_pay_the_documented_multiple()
    {
        var (world, game) = Setup(balance: 1_000_000);
        var wins = 0;
        var losses = 0;
        for (var i = 0; i < 80; i++)
        {
            var before = world.Ledger.Balance(Alice);
            var id = $"msg-{i}";
            var text = world.Send(game, Alice, "!parachute 100", id)!;
            var expected = Expected(id);
            var delta = world.Ledger.Balance(Alice) - before;
            if (expected.IsWin)
            {
                wins++;
                Assert.Equal((expected.Score * 7) - 100, delta);
                Assert.Contains($"score of {expected.Score}", text);
            }
            else
            {
                losses++;
                Assert.Equal(-100, delta);
                Assert.Contains(" lost ", text);
            }

            ReleaseCooldown(world);
        }

        Assert.True(wins > 0 && losses > 0);
    }

    [Fact]
    public void Overlay_envelope_carries_the_seed_result_keyframes_and_hazards()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!parachute 100", "m1");
        var (widgetId, surface, payload) = Assert.Single(world.Overlay.Pushes);
        Assert.Equal("parachute", widgetId);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var expected = Expected("m1");
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal("parachute", extra.GetProperty("game").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedDerivation.Derive("m1", "twitch", Alice, "parachute", Entropy)), extra.GetProperty("seed").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        Assert.Equal(100, extra.GetProperty("stake").GetInt32());
        var data = extra.GetProperty("data");
        Assert.Equal(expected.Score, data.GetProperty("score").GetInt32());
        Assert.Equal(expected.LandingTick, data.GetProperty("landingTick").GetInt32());
        Assert.Equal(expected.Destroyed, data.GetProperty("destroyed").GetBoolean());
        Assert.Equal(expected.Keyframes.Count, data.GetProperty("keyframes").GetArrayLength());
        Assert.Equal(expected.Collisions.Count, data.GetProperty("collisions").GetArrayLength());
        Assert.Equal(expected.Droplets.Count, data.GetProperty("droplets").GetArrayLength());
        Assert.Equal(expected.Rockets.Count, data.GetProperty("rockets").GetArrayLength());
        var last = data.GetProperty("keyframes")[expected.Keyframes.Count - 1];
        Assert.Equal(5, last.GetArrayLength());
        Assert.Equal(expected.Keyframes[^1].X, last[1].GetInt32());
        Assert.Equal(9400, last[2].GetInt32());
        var board = data.GetProperty("board");
        Assert.Equal(1920, board.GetProperty("w").GetInt32());
        Assert.Equal(60, board.GetProperty("tickRate").GetInt32());
        Assert.Equal(expected.TotalTicks, board.GetProperty("totalTicks").GetInt32());
        Assert.Equal(5000, board.GetProperty("targetCenterX").GetInt32());
        Assert.Equal(900, board.GetProperty("targetHalfWidth").GetInt32());
        Assert.Equal(225, board.GetProperty("bullseyeHalfWidth").GetInt32());
        Assert.Equal((long)(ParachuteLogic.PayoutFor(100, expected.Score) * 100 / 100), data.GetProperty("multiplierPct").GetInt64());
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("Parachute Drop", world.Overlay.Registered[0].DisplayName);
    }

    [Fact]
    public void Envelope_lists_every_droplet_and_rocket_with_its_replay_fields()
    {
        var cfg = ParachuteConfig.Default with { RocketVolleyChancePct = 100, RocketCount = 2 };
        var (world, game) = Setup(config: cfg);
        world.Send(game, Alice, "!parachute 100", "m1");
        var data = JsonDocument.Parse(world.Overlay.Pushes[0].Payload).RootElement.GetProperty("extra").GetProperty("data");
        var droplet = data.GetProperty("droplets")[0];
        foreach (var field in new[] { "i", "t", "x", "y", "vx", "vy", "g", "r", "m", "size", "speed" })
        {
            Assert.True(droplet.TryGetProperty(field, out _), field);
        }

        var rocket = data.GetProperty("rockets")[1];
        foreach (var field in new[] { "i", "t", "x", "y", "tx", "ty", "vx", "vy", "ay", "fuse", "color", "pattern", "targeted", "explodeTick", "ex", "ey", "hit" })
        {
            Assert.True(rocket.TryGetProperty(field, out _), field);
        }

        Assert.Equal("Targeted", data.GetProperty("rocketPattern").GetString());
        Assert.Equal(2, data.GetProperty("rockets").GetArrayLength());
        Assert.Equal(5, data.GetProperty("droplets").GetArrayLength());
    }

    [Fact]
    public void Redelivery_replays_the_same_drop_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!parachute 100", "m1");
        var balance = world.Ledger.Balance(Alice);
        Assert.Equal(first, world.Send(game, Alice, "!parachute 100", "m1"));
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Fact]
    public void Cooldown_is_ten_seconds_and_silent()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!parachute 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!parachute 10", "m2"));
        Assert.Equal(10U, ParachuteLogic.CooldownSeconds);
    }

    [Fact]
    public void Max_bet_clamps_to_500()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!parachute 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Fact]
    public void All_and_max_bet_the_whole_balance_up_to_the_clamp()
    {
        var (world, game) = Setup(balance: 300);
        world.Send(game, Alice, "!parachute all", "m1");
        Assert.Equal(300UL, world.Ledger.Moved.Single().Amount);
    }

    [Theory]
    [InlineData("!parachute", "To parachute please do")]
    [InlineData("!parachute abc", "The amount must be a number, max, or all")]
    [InlineData("!parachute 0", "The amount needs to be greater then 0")]
    [InlineData("!parachute 99999", "You don't have that many.")]
    [InlineData("!parachute 10 sad_emote spring", "To parachute please do")]
    public void Refusals_use_the_originals_wording(string text, string contains)
    {
        var (world, game) = Setup();
        Assert.Contains(contains, world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void Over_cap_answers_in_chat_and_releases_the_cooldown()
    {
        var (world, game) = Setup(balance: 1000);
        world.Ledger.MaxBetCap = 50;
        Assert.Contains("over the limit", world.Send(game, Alice, "!parachute 100", "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
        world.Ledger.MaxBetCap = 500;
        Assert.NotNull(world.Send(game, Alice, "!parachute 100", "m2"));
        Assert.Single(world.Ledger.Moved);
    }

    [Fact]
    public void Other_commands_stay_silent_and_the_flag_gates_the_game()
    {
        var (world, game) = Setup();
        Assert.Null(world.Send(game, Alice, "!plinko 10", "m0"));
        Assert.Null(world.Send(game, Alice, "hello", "m00"));
        world.Flags.Overrides[ParachuteLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!parachute 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void No_community_and_unlinked_paths()
    {
        var (world, game) = Setup();
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!parachute 10"), world.HostFor(Alice, "m2", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!parachute 10", "m3"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        Assert.NotNull(world.Send(game, Alice, "!parachute 100", "m1"));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, w => w.Message == "parachute.overlay_push_failed");
    }

    [Fact]
    public void Kv_failure_propagates_and_moves_no_money()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!parachute 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_drops_differ()
    {
        var world = new World(ParachuteLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new ParachuteLogic();
        world.Send(game, Alice, "!parachute 10", "m1");
        ReleaseCooldown(world);
        world.Send(game, Alice, "!parachute 10", "m1b");
        var seeds = world.Overlay.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString()).ToHashSet();
        Assert.Equal(2, seeds.Count);
    }

    [Fact]
    public void The_replay_record_carries_the_game_config_revision()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!parachute 10", "m1");
        var store = new KvStateStore(world.Kv);
        var key = DropReplayGuard.KeyFor("parachute", World.Msg("!parachute 10"), world.HostFor(Alice, "m1"), Alice, "parachute");
        var record = DropReplayGuard.TryReplay(store, key, Alice, "parachute", StateKey.Hash("!parachute 10"));
        Assert.NotNull(record);
        Assert.Equal(ParachuteLogic.ConfigId, record!.Config);
    }

    [Theory]
    [InlineData(0, 0UL)]
    [InlineData(-5, 0UL)]
    [InlineData(100, 700UL)]
    [InlineData(50, 350UL)]
    [InlineData(1, 7UL)]
    [InlineData(150, 700UL)]
    public void Payout_math_at_a_stake_of_100(int score, ulong expected)
    {
        Assert.Equal(expected, ParachuteLogic.PayoutFor(100, score));
    }

    [Fact]
    public void Payout_rounds_down_and_never_exceeds_the_host_cap()
    {
        Assert.Equal(1UL, ParachuteLogic.PayoutFor(7, 3));
        Assert.Equal(0UL, ParachuteLogic.PayoutFor(1, 10));
        foreach (var score in new[] { 0, 1, 100, 10000, int.MaxValue })
        {
            Assert.True(ParachuteLogic.PayoutFor(500, score) <= 500 * 100);
        }
    }

    [Fact]
    public void Outcome_text_describes_wins_misses_crashes_and_hazard_hits()
    {
        var r = ParachuteSimulationEngine.Run(1, ParachuteConfig.Default);
        Assert.Equal("missed the platform (3 droplet and 0 rocket hits)", ParachuteLogic.OutcomeText(r, 100, 0));
        var calm = r with { DropletHits = 0, RocketHits = 0 };
        Assert.Equal("missed the platform", ParachuteLogic.OutcomeText(calm, 100, 0));
        var win = calm with { IsWin = true, IsHit = true, Score = 50 };
        Assert.Equal("landed on the platform with a score of 50 (x3.5)", ParachuteLogic.OutcomeText(win, 100, 350));
        var crashWin = win with { Destroyed = true };
        Assert.Equal("landed on the platform with a score of 50 after a rocket destroyed the parachute (x3.5)", ParachuteLogic.OutcomeText(crashWin, 100, 350));
        var crashMiss = calm with { Destroyed = true };
        Assert.Equal("crash landed off the platform", ParachuteLogic.OutcomeText(crashMiss, 100, 0));
        var crashZero = crashMiss with { IsHit = true };
        Assert.Equal("crash landed on the platform and scored nothing", ParachuteLogic.OutcomeText(crashZero, 100, 0));
    }

    [Fact]
    public void Data_json_is_valid_for_a_zero_stake_result()
    {
        var r = ParachuteSimulationEngine.Run(5, ParachuteConfig.Default);
        var data = JsonDocument.Parse(ParachuteLogic.DataJson(r, 0, 0)).RootElement;
        Assert.Equal(0, data.GetProperty("multiplierPct").GetInt64());
        Assert.Equal(r.Score, data.GetProperty("score").GetInt32());
    }

    [Fact]
    public void A_zero_score_crash_config_pays_nothing_for_a_destroyed_parachute()
    {
        var cfg = ParachuteConfig.Default with { RocketVolleyChancePct = 100, CrashMode = CrashLandingScoreMode.ZeroScore };
        var (world, game) = Setup(balance: 1_000_000, config: cfg);
        var checkedCrash = 0;
        for (var i = 0; i < 60; i++)
        {
            var id = $"z{i}";
            var before = world.Ledger.Balance(Alice);
            world.Send(game, Alice, "!parachute 100", id);
            var expected = Expected(id, config: cfg);
            if (expected.Destroyed)
            {
                checkedCrash++;
                Assert.Equal(-100, world.Ledger.Balance(Alice) - before);
            }

            ReleaseCooldown(world);
        }

        Assert.True(checkedCrash > 5);
    }
}

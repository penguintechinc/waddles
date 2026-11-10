using System.Text.Json;
using SuperPenguin.DropGameCore;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinBalloon.Tests;

/// <summary>End-to-end `!balloon` behaviour over the shared host base: money, replay, cooldown, refusals and the overlay envelope.</summary>
public class BalloonLogicTests
{
    private const string Alice = World.Alice;
    private const ulong Entropy = 7;

    private static (World World, BalloonLogic Game) Setup(long balance = 1000)
    {
        var world = new World(BalloonLogic.FlagKey);
        world.Ledger.Seed(Alice, balance);
        return (world, new BalloonLogic(() => Entropy));
    }

    private static BalloonResult Expected(string messageId, string platform = "twitch") =>
        BalloonSimulation.Run(SeedDerivation.Derive(messageId, platform, Alice, "balloon", Entropy), new BalloonConfig());

    private static void ReleaseCooldown(World world) =>
        WaddleSdk.State.StateCooldown.Release(new WaddleSdk.State.KvStateStore(world.Kv), "balloon", Alice);

    [Fact]
    public void A_play_settles_exactly_the_flight_the_recorded_seed_produces()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!balloon 100", "m1")!;
        var expected = Expected("m1");
        var payout = BalloonLogic.PayoutFor(100, expected, new BalloonConfig());
        Assert.Equal(1000 - 100 + (long)payout, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(BalloonLogic.DescribeOutcome(expected, 100, payout), text);
        Assert.Contains($"now has {1000 - 100 + (long)payout} stars", text);
    }

    [Fact]
    public void Docks_pops_and_misses_all_occur_and_pay_by_the_mapping()
    {
        var (world, game) = Setup(balance: 10_000_000);
        var cfg = new BalloonConfig();
        int docks = 0, pops = 0, misses = 0;
        for (var i = 0; i < 400; i++)
        {
            var before = world.Ledger.Balance(Alice);
            var id = $"msg-{i}";
            var text = world.Send(game, Alice, "!balloon 100", id)!;
            var expected = Expected(id);
            var delta = world.Ledger.Balance(Alice) - before;
            var pct = cfg.PayoutPercent(expected.Hit, expected.Bullseye, expected.Score);
            Assert.Equal(pct - 100, delta);
            if (expected.Popped)
            {
                pops++;
                Assert.Contains("a dart popped the balloon", text);
                Assert.Contains(" lost ", text);
            }
            else if (expected.Hit)
            {
                docks++;
                Assert.Contains($"for {expected.Score} points", text);
                Assert.Contains(" won ", text);
                Assert.Equal(expected.Bullseye, text.Contains("GOLDEN BULLSEYE"));
            }
            else
            {
                misses++;
                Assert.Contains("missed the cloud", text);
                Assert.Contains(" lost ", text);
            }

            ReleaseCooldown(world);
        }

        Assert.True(docks > 0 && pops > 0 && misses > 0, $"docks {docks}, pops {pops}, misses {misses}");
    }

    [Fact]
    public void Overlay_envelope_carries_the_seed_result_wave_and_keyframes()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!balloon 100", "m1");
        var (widgetId, surface, payload) = Assert.Single(world.Overlay.Pushes);
        Assert.Equal("balloon", widgetId);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var expected = Expected("m1");
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal("balloon", extra.GetProperty("game").GetString());
        Assert.Equal("settled", extra.GetProperty("kind").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedDerivation.Derive("m1", "twitch", Alice, "balloon", Entropy)), extra.GetProperty("seed").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        Assert.Equal(100, extra.GetProperty("stake").GetInt64());
        var payout = BalloonLogic.PayoutFor(100, expected, new BalloonConfig());
        Assert.Equal((long)payout, extra.GetProperty("payout").GetInt64());
        var data = extra.GetProperty("data");
        Assert.Equal(1920, data.GetProperty("field").GetProperty("w").GetInt32());
        Assert.Equal(60, data.GetProperty("field").GetProperty("tickHz").GetInt32());
        Assert.Equal(expected.Hit, data.GetProperty("hit").GetBoolean());
        Assert.Equal(expected.Popped, data.GetProperty("popped").GetBoolean());
        Assert.Equal(expected.Score, data.GetProperty("score").GetInt32());
        Assert.Equal(expected.EndTick, data.GetProperty("endTick").GetInt32());
        Assert.Equal(expected.Keyframes.Count, data.GetProperty("keyframes").GetArrayLength());
        var last = data.GetProperty("keyframes")[expected.Keyframes.Count - 1];
        Assert.Equal(expected.Keyframes[^1].X, last[1].GetInt32());
        Assert.Equal(expected.Keyframes[^1].Y, last[2].GetInt32());
        var wave = data.GetProperty("wave");
        Assert.Equal(expected.Pattern.ToString(), wave.GetProperty("pattern").GetString());
        Assert.Equal(expected.Hazards.Count, wave.GetProperty("hazards").GetArrayLength());
        Assert.Equal(3, wave.GetProperty("hazards").GetArrayLength());
        Assert.Equal(expected.Hazards[0].HitTick, wave.GetProperty("hazards")[0].GetProperty("hitTick").GetInt32());
        Assert.Equal(expected.Launch.TotalTicks, data.GetProperty("launch").GetProperty("totalTicks").GetInt32());
        Assert.Equal((int)Math.Round(expected.Cloud.HalfWidth * 10000), data.GetProperty("cloud").GetProperty("halfWidth").GetInt32());
        Assert.Equal((long)(payout * 100 / 100), data.GetProperty("multiplierPct").GetInt64());
        Assert.False(data.GetProperty("timedOut").GetBoolean());
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("Balloon Ascent", world.Overlay.Registered[0].DisplayName);
    }

    [Fact]
    public void Redelivery_replays_the_same_flight_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!balloon 100", "m1");
        var balance = world.Ledger.Balance(Alice);
        Assert.Equal(first, world.Send(game, Alice, "!balloon 100", "m1"));
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Fact]
    public void Cooldown_is_ten_seconds_and_silent()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!balloon 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!balloon 10", "m2"));
        Assert.Equal(10U, BalloonLogic.CooldownSeconds);
    }

    [Fact]
    public void Max_bet_clamps_to_500()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!balloon 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Theory]
    [InlineData("!balloon", "To balloon please do")]
    [InlineData("!balloon abc", "The amount must be a number, max, or all")]
    [InlineData("!balloon 0", "The amount needs to be greater then 0")]
    [InlineData("!balloon 99999", "You don't have that many.")]
    [InlineData("!balloon 10 sad_emote spring", "To balloon please do")]
    public void Refusals_use_the_originals_wording(string text, string contains)
    {
        var (world, game) = Setup();
        Assert.Contains(contains, world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Theory]
    [InlineData("!float 10")]
    [InlineData("!ascent 10")]
    [InlineData("!plinko 10")]
    public void Upstream_aliases_are_not_registered_so_the_cooldown_cannot_be_dodged(string text)
    {
        var (world, game) = Setup();
        Assert.Null(world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Flag_off_no_community_and_unlinked_paths()
    {
        var (world, game) = Setup();
        world.Flags.Overrides[BalloonLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!balloon 10", "m1"));
        world.Flags.Overrides[BalloonLogic.FlagKey] = true;
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!balloon 10"), world.HostFor(Alice, "m2", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!balloon 10", "m3"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        Assert.NotNull(world.Send(game, Alice, "!balloon 100", "m1"));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, w => w.Message == "balloon.overlay_push_failed");
    }

    [Fact]
    public void Kv_failure_propagates_and_moves_no_money()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!balloon 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_flights_differ()
    {
        var world = new World(BalloonLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new BalloonLogic();
        world.Send(game, Alice, "!balloon 10", "m1");
        ReleaseCooldown(world);
        world.Send(game, Alice, "!balloon 10", "m1b");
        var seeds = world.Overlay.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString()).ToHashSet();
        Assert.Equal(2, seeds.Count);
    }

    [Fact]
    public void A_custom_config_changes_the_payout_mapping()
    {
        var cfg = new BalloonConfig { HazardCount = 0, TargetWidthPercent = 80, TargetSpeed = 0, HitBasePercent = 500, PercentPerPoint = 0, BullseyePercent = 500 };
        var world = new World(BalloonLogic.FlagKey);
        world.Ledger.Seed(Alice, 1_000_000);
        var game = new BalloonLogic(() => Entropy, cfg);
        var paid = false;
        for (var i = 0; i < 40 && !paid; i++)
        {
            var id = $"c{i}";
            var before = world.Ledger.Balance(Alice);
            world.Send(game, Alice, "!balloon 10", id);
            var r = BalloonSimulation.Run(SeedDerivation.Derive(id, "twitch", Alice, "balloon", Entropy), cfg);
            if (r.Hit)
            {
                Assert.Equal(40, world.Ledger.Balance(Alice) - before);
                paid = true;
            }

            ReleaseCooldown(world);
        }

        Assert.True(paid, "a wide stationary cloud should be docked within 40 flights");
    }

    [Fact]
    public void Outcome_text_covers_pop_bullseye_dock_timeout_and_miss()
    {
        var baseResult = BalloonSimulation.Run(1, new BalloonConfig { HazardCount = 0 });
        Assert.Contains("popped", BalloonLogic.DescribeOutcome(baseResult with { Popped = true }, 100, 0));
        var dock = BalloonLogic.DescribeOutcome(baseResult with { Hit = true, Score = 60, Bullseye = false }, 100, 490);
        Assert.Contains("docked on the cloud for 60 points (x4.9)", dock);
        Assert.DoesNotContain("GOLDEN", dock);
        Assert.Contains("GOLDEN BULLSEYE! the balloon docked on the cloud for 99 points (x10)", BalloonLogic.DescribeOutcome(baseResult with { Hit = true, Score = 99, Bullseye = true }, 100, 1000));
        Assert.Contains("never reached", BalloonLogic.DescribeOutcome(baseResult with { Hit = false, TimedOut = true }, 100, 0));
        Assert.Contains("missed the cloud", BalloonLogic.DescribeOutcome(baseResult with { Hit = false }, 100, 0));
    }

    [Fact]
    public void Payout_is_the_stake_times_the_mapped_percent()
    {
        var cfg = new BalloonConfig();
        var baseResult = BalloonSimulation.Run(1, new BalloonConfig { HazardCount = 0 });
        Assert.Equal(0UL, BalloonLogic.PayoutFor(100, baseResult with { Hit = false }, cfg));
        Assert.Equal(100UL * (250 + 4 * 50) / 100, BalloonLogic.PayoutFor(100, baseResult with { Hit = true, Score = 50, Bullseye = false }, cfg));
        Assert.Equal(1000UL, BalloonLogic.PayoutFor(100, baseResult with { Hit = true, Score = 100, Bullseye = true }, cfg));
        Assert.Equal(5000UL, BalloonLogic.PayoutFor(500, baseResult with { Hit = true, Score = 100, Bullseye = true }, cfg) / 1);
    }
}

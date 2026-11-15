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

namespace WaddleBundleSuperpenguinCurling.Tests;

/// <summary>End-to-end `!curl` behaviour over the shared host base: money, replay, cooldown, refusals and the overlay envelope.</summary>
public class CurlingLogicTests
{
    private const string Alice = World.Alice;
    private const ulong Entropy = 7;

    private static readonly CurlingConfig Calm = CurlingConfig.Default with { DynamicIce = false, PowerVariancePercent = 0.0 };

    private static (World World, CurlingLogic Game) Setup(long balance = 1000, CurlingConfig? config = null)
    {
        var world = new World(CurlingLogic.FlagKey);
        world.Ledger.Seed(Alice, balance);
        return (world, new CurlingLogic(() => Entropy, config));
    }

    private static CurlingResult Expected(string messageId, int? power, string command = "curl", string platform = "twitch", CurlingConfig? config = null) =>
        CurlingSimulation.Run(SeedDerivation.Derive(messageId, platform, Alice, command, Entropy), config ?? CurlingConfig.Default, power);

    private static void ReleaseCooldown(World world, string command = "curl") =>
        StateCooldown.Release(new KvStateStore(world.Kv), command, Alice);

    [Fact]
    public void A_scoring_slide_settles_the_payout_the_recorded_seed_produces()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!curl 100", "m1")!;
        var expected = Expected("m1", null);
        Assert.Equal(75, expected.Score);
        Assert.Equal(1000 - 100 + 200, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains("your stone slid at power 67.4 on Slick Ice and stopped in the 4-foot ring (x2)", text);
        Assert.Contains("won 100 stars", text);
        Assert.Contains("now has 1100 stars", text);
    }

    [Fact]
    public void A_back_board_hit_loses_the_stake()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!curl 100 82", "m1")!;
        var expected = Expected("m1", 82);
        Assert.True(expected.WallHit);
        Assert.Equal(900, world.Ledger.Balance(Alice));
        Assert.Contains("hit the back board", text);
        Assert.Contains("lost 100 stars", text);
    }

    [Fact]
    public void Every_score_pays_its_table_amount_across_many_plays()
    {
        var (world, game) = Setup(balance: 1_000_000);
        var scores = new HashSet<int>();
        for (var i = 0; i < 60; i++)
        {
            var before = world.Ledger.Balance(Alice);
            var id = $"msg-{i}";
            var text = world.Send(game, Alice, "!curl 100 82", id)!;
            var expected = Expected(id, 82);
            scores.Add(expected.Score);
            var payout = CurlingLogic.PayoutFor(100, expected.Score, ScoringType.CurlingHouse);
            Assert.Equal((long)payout - 100, world.Ledger.Balance(Alice) - before);
            Assert.Equal(payout > 100, text.Contains(" won "));
            ReleaseCooldown(world);
        }

        Assert.Equal(new[] { 0, 25, 50, 75, 100 }, scores.Order().ToArray());
    }

    [Theory]
    [InlineData(100, 400)]
    [InlineData(75, 200)]
    [InlineData(50, 125)]
    [InlineData(25, 75)]
    [InlineData(0, 0)]
    [InlineData(-10, 0)]
    [InlineData(30, 0)]
    public void House_payout_percent_table(int score, int percent)
    {
        Assert.Equal(percent, CurlingLogic.PayoutPercent(score, ScoringType.CurlingHouse));
    }

    [Theory]
    [InlineData(30, 550)]
    [InlineData(20, 225)]
    [InlineData(10, 110)]
    [InlineData(0, 0)]
    [InlineData(-10, 0)]
    [InlineData(100, 0)]
    public void Shuffleboard_payout_percent_table(int score, int percent)
    {
        Assert.Equal(percent, CurlingLogic.PayoutPercent(score, ScoringType.Shuffleboard));
    }

    [Fact]
    public void Payout_rounds_down_and_never_exceeds_the_host_cap()
    {
        Assert.Equal(2UL, CurlingLogic.PayoutFor(3, 25, ScoringType.CurlingHouse));
        Assert.Equal(1250UL, CurlingLogic.PayoutFor(1000, 50, ScoringType.CurlingHouse));
        Assert.All(new ulong[] { 1, 7, 500 }, stake => Assert.True(CurlingLogic.PayoutFor(stake, 100, ScoringType.CurlingHouse) <= stake * 100));
        Assert.Equal(0UL, CurlingLogic.PayoutFor(100, 0, ScoringType.CurlingHouse));
    }

    [Theory]
    [InlineData(72, "stopped in the 12-foot ring (x0.75)", 75)]
    [InlineData(74, "stopped in the 8-foot ring (x1.25)", 125)]
    [InlineData(76, "stopped in the 4-foot ring (x2)", 200)]
    [InlineData(77, "stopped on the button (x4)", 400)]
    [InlineData(60, "stopped outside the house", 0)]
    [InlineData(100, "hit the back board", 0)]
    public void House_sheet_chat_text_and_payout_by_power(int power, string contains, long payout)
    {
        var (world, game) = Setup(config: Calm);
        var text = world.Send(game, Alice, $"!curl 100 {power}", "m1")!;
        Assert.Contains(contains, text);
        Assert.Equal(1000 - 100 + payout, world.Ledger.Balance(Alice));
    }

    [Theory]
    [InlineData(73, "stopped in zone 10 (x1.1)", 110)]
    [InlineData(75, "stopped in zone 20 (x2.25)", 225)]
    [InlineData(77, "stopped dead centre in zone 30 (x5.5)", 550)]
    [InlineData(79, "slid into the kitchen", 0)]
    [InlineData(60, "stopped short of the zones", 0)]
    [InlineData(100, "hit the back board", 0)]
    public void Shuffleboard_sheet_chat_text_and_payout_by_power(int power, string contains, long payout)
    {
        var (world, game) = Setup(config: Calm with { Scoring = ScoringType.Shuffleboard, KitchenPenalty = true });
        var text = world.Send(game, Alice, $"!curl 100 {power}", "m1")!;
        Assert.Contains(contains, text);
        Assert.Equal(1000 - 100 + payout, world.Ledger.Balance(Alice));
    }

    [Fact]
    public void Overlay_envelope_carries_the_seed_sheet_result_and_keyframes()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!curl 100 80", "m1");
        var (widgetId, surface, payload) = Assert.Single(world.Overlay.Pushes);
        Assert.Equal("curling", widgetId);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var expected = Expected("m1", 80);
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal("curling", extra.GetProperty("game").GetString());
        Assert.Equal("settled", extra.GetProperty("kind").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedDerivation.Derive("m1", "twitch", Alice, "curl", Entropy)), extra.GetProperty("seed").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        Assert.Equal(100, extra.GetProperty("stake").GetInt32());
        Assert.Equal((long)CurlingLogic.PayoutFor(100, expected.Score, ScoringType.CurlingHouse), extra.GetProperty("payout").GetInt64());
        var data = extra.GetProperty("data");
        Assert.Equal(expected.Ice.ToString(), data.GetProperty("condition").GetString());
        Assert.Equal(CurlingSimulation.Label(expected.Ice), data.GetProperty("conditionLabel").GetString());
        Assert.Equal(80, data.GetProperty("power").GetInt32());
        Assert.Equal(Math.Round(expected.EffectivePower, 4), data.GetProperty("effectivePower").GetDouble());
        Assert.Equal(expected.Score, data.GetProperty("score").GetInt32());
        Assert.Equal(expected.IsBullseye, data.GetProperty("isBullseye").GetBoolean());
        Assert.Equal(expected.IsWin, data.GetProperty("isWin").GetBoolean());
        Assert.Equal(expected.WallHit, data.GetProperty("wallHit").GetBoolean());
        Assert.Equal(expected.TotalTicks, data.GetProperty("totalTicks").GetInt32());
        Assert.Equal(CurlingLogic.PayoutPercent(expected.Score, ScoringType.CurlingHouse), data.GetProperty("multiplierPct").GetInt32());
        Assert.Equal((int)Math.Round(expected.RestPos * 10000.0), data.GetProperty("restX").GetInt32());
        Assert.Equal((int)Math.Round(expected.Lateral * 10000.0), data.GetProperty("restY").GetInt32());
        Assert.Equal(expected.Keyframes.Count, data.GetProperty("keyframes").GetArrayLength());
        var last = data.GetProperty("keyframes")[expected.Keyframes.Count - 1];
        Assert.Equal(expected.TotalTicks, last[0].GetInt32());
        Assert.Equal(0, last[3].GetInt32());
        var sheet = data.GetProperty("sheet");
        Assert.Equal("Bottom", sheet.GetProperty("stripPosition").GetString());
        Assert.Equal("LeftToRight", sheet.GetProperty("slideDirection").GetString());
        Assert.Equal("CurlingHouse", sheet.GetProperty("scoringType").GetString());
        Assert.Equal("LockOnRest", sheet.GetProperty("collisionMode").GetString());
        Assert.Equal(8400, sheet.GetProperty("targetRatio").GetInt32());
        Assert.Equal(60, sheet.GetProperty("tickRateHz").GetInt32());
        Assert.False(sheet.GetProperty("isReversed").GetBoolean());
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("Curling / Shuffleboard", world.Overlay.Registered[0].DisplayName);
    }

    [Fact]
    public void A_vertical_strip_swaps_the_axes_in_rest_and_keyframes()
    {
        var cfg = Calm with { Strip = StripPosition.Left, Direction = SlideDirection.TopToBottom };
        var result = CurlingSimulation.Run(1, cfg, 77);
        var data = JsonDocument.Parse(CurlingLogic.DataJson(result, cfg)).RootElement;
        Assert.Equal(5000, data.GetProperty("restX").GetInt32());
        Assert.Equal((int)Math.Round(result.RestPos * 10000.0), data.GetProperty("restY").GetInt32());
        var first = data.GetProperty("keyframes")[1];
        Assert.Equal(5000, first[1].GetInt32());
        Assert.Equal((int)Math.Round(result.Keyframes[1].AxisPos * 10000.0), first[2].GetInt32());
        Assert.Equal("Left", data.GetProperty("sheet").GetProperty("stripPosition").GetString());
    }

    [Fact]
    public void Redelivery_replays_the_same_slide_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!curl 100 80", "m1");
        var balance = world.Ledger.Balance(Alice);
        Assert.Equal(first, world.Send(game, Alice, "!curl 100 80", "m1"));
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Theory]
    [InlineData("curl")]
    [InlineData("curling")]
    [InlineData("slide")]
    [InlineData("shuffle")]
    public void Every_command_alias_plays(string alias)
    {
        var (world, game) = Setup();
        Assert.Contains("your stone slid", world.Send(game, Alice, $"!{alias} 10", "m1"));
        var seed = SeedDerivation.ToHex(SeedDerivation.Derive("m1", "twitch", Alice, "curl", Entropy));
        Assert.Equal(seed, JsonDocument.Parse(world.Overlay.Pushes[0].Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString());
    }

    [Fact]
    public void Cooldown_is_ten_seconds_and_silent()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!curl 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!curl 10", "m2"));
        Assert.Equal(10U, CurlingLogic.CooldownSeconds);
    }

    [Fact]
    public void Max_bet_clamps_to_500()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!curl 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Theory]
    [InlineData("!curl", "To curl please do")]
    [InlineData("!curl abc", "The amount must be a number, max, or all")]
    [InlineData("!curl 0", "The amount needs to be greater then 0")]
    [InlineData("!curl 99999", "You don't have that many.")]
    [InlineData("!curl 10 0", "To curl please do")]
    [InlineData("!curl 10 101", "To curl please do")]
    [InlineData("!curl 10 -5", "To curl please do")]
    [InlineData("!curl 10 5.5", "To curl please do")]
    [InlineData("!curl 10 :Kappa:", "To curl please do")]
    [InlineData("!curl 10 50 60", "To curl please do")]
    public void Refusals_use_the_originals_wording(string text, string contains)
    {
        var (world, game) = Setup();
        Assert.Contains(contains, world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void A_refused_power_does_not_cost_the_cooldown_slot()
    {
        var (world, game) = Setup();
        Assert.Contains("To curl please do", world.Send(game, Alice, "!curl 10 500", "m1"));
        Assert.Contains("your stone slid", world.Send(game, Alice, "!curl 10 80", "m2"));
    }

    [Fact]
    public void With_viewer_power_control_off_a_power_argument_is_refused_and_the_default_draw_plays()
    {
        var (world, game) = Setup(config: CurlingConfig.Default with { ViewerPowerControl = false });
        Assert.Contains("To curl please do", world.Send(game, Alice, "!curl 10 80", "m1"));
        Assert.Contains("your stone slid", world.Send(game, Alice, "!curl 10", "m2"));
    }

    [Fact]
    public void Flag_off_no_community_and_unlinked_paths()
    {
        var (world, game) = Setup();
        world.Flags.Overrides[CurlingLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!curl 10", "m1"));
        world.Flags.Overrides[CurlingLogic.FlagKey] = true;
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!curl 10"), world.HostFor(Alice, "m2", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!curl 10", "m3"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Other_commands_are_ignored()
    {
        var (world, game) = Setup();
        Assert.Null(world.Send(game, Alice, "!plinko 10", "m1"));
        Assert.Null(world.Send(game, Alice, "hello", "m2"));
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        Assert.NotNull(world.Send(game, Alice, "!curl 100 80", "m1"));
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, w => w.Message == "curling.overlay_push_failed");
    }

    [Fact]
    public void Kv_failure_propagates_and_moves_no_money()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!curl 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_plays_differ()
    {
        var world = new World(CurlingLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new CurlingLogic();
        world.Send(game, Alice, "!curl 10", "m1");
        ReleaseCooldown(world);
        world.Send(game, Alice, "!curl 10", "m1b");
        var seeds = world.Overlay.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("extra").GetProperty("seed").GetString()).ToHashSet();
        Assert.Equal(2, seeds.Count);
    }

    [Fact]
    public void Power_parser_accepts_only_whole_numbers_from_one_to_one_hundred()
    {
        Assert.True(CurlingLogic.TryParsePower("1", out var low));
        Assert.Equal(1, low);
        Assert.True(CurlingLogic.TryParsePower("100", out var high));
        Assert.Equal(100, high);
        Assert.All(new[] { "0", "101", "-1", "+5", " 5", "5 ", "5.0", "", "abc", "99999999999" }, text => Assert.False(CurlingLogic.TryParsePower(text, out _)));
    }

    [Fact]
    public void Json_data_is_valid_for_a_hand_built_result()
    {
        var r = new CurlingResult(75, 75, IceCondition.Normal, false, 0.84, 0.05, 0.84, 0.5, 30, false, 100, true, true, [new SlidePoint(0, 0.05, 0.5, 0.5), new SlidePoint(30, 0.84, 0.5, 0)]);
        var data = JsonDocument.Parse(CurlingLogic.DataJson(r, CurlingConfig.Default)).RootElement;
        Assert.Equal(2, data.GetProperty("keyframes").GetArrayLength());
        Assert.Equal(400, data.GetProperty("multiplierPct").GetInt32());
        Assert.Equal(500, data.GetProperty("keyframes")[0][1].GetInt32());
    }
}

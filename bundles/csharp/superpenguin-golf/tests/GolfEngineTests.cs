using SuperPenguin.DropGameCore;
using Xunit;

namespace WaddleBundleSuperpenguinGolf.Tests;

/// <summary>Golden, determinism, physics-branch and scoring tests for the pure Golf simulation.</summary>
public class GolfEngineTests
{
    /// <summary>No variance and a pinned standard green: the shot is a pure function of the power, derivable by hand from upstream's formulas.</summary>
    private static readonly GolfConfig Calm = GolfConfig.Default with { PowerVariancePercent = 0.0, FixedCondition = GreenCondition.StandardGreen };

    internal static ulong Checksum(GolfShot s)
    {
        var h = 0xcbf29ce484222325UL;
        foreach (var k in s.Keyframes)
        {
            foreach (var v in new[] { k.Tick, (int)Math.Round(k.Axis * 10000), (int)Math.Round(k.Lateral * 10000), (int)Math.Round(k.Height * 10000) })
            {
                h = (h ^ (ulong)(uint)v) * 0x100000001b3UL;
            }
        }

        return h;
    }

    [Theory]
    [InlineData(1UL, 79, GreenCondition.FastLinks, 79, false, true, 36, 210, 6, 0.8675535153281851, 79.12261971963314, 0xf183af9918d44e38UL)]
    [InlineData(42UL, null, GreenCondition.HeavyDew, 80, false, false, 0, 131, 6, 0.7241875710227272, 81.08026046616884, 0x72898743327838a0UL)]
    [InlineData(3735928559UL, 70, GreenCondition.CrossBreeze, 70, false, false, 0, 137, 6, 0.6518296320346321, 69.38504501018556, 0xb075b395d2350f26UL)]
    public void Golden_seeds_reproduce_the_exact_shot(ulong seed, int? power, GreenCondition cond, int usedPower, bool sunk, bool lip, int score, int ticks, int frames, double rest, double effective, ulong sum)
    {
        var s = GolfEngine.Shoot(seed, power, GolfConfig.Default);
        Assert.Equal(cond, s.Condition);
        Assert.Equal(usedPower, s.Power);
        Assert.Equal(sunk, s.IsSunk);
        Assert.Equal(lip, s.IsLipOut);
        Assert.Equal(score, s.BaseScore);
        Assert.Equal(ticks, s.TotalTicks);
        Assert.Equal(frames, s.Keyframes.Count);
        Assert.Equal(rest, s.Keyframes[^1].Axis);
        Assert.Equal(rest, s.RestPosition);
        Assert.Equal(effective, s.EffectivePower);
        Assert.Equal(sum, Checksum(s));
    }

    [Fact]
    public void Same_seed_is_byte_identical_and_different_seeds_differ()
    {
        var a = GolfEngine.Shoot(777, 79, GolfConfig.Default);
        var b = GolfEngine.Shoot(777, 79, GolfConfig.Default);
        Assert.Equal(Checksum(a), Checksum(b));
        Assert.Equal(GolfLogic.DataJson(a, GolfConfig.Default, 10, 0), GolfLogic.DataJson(b, GolfConfig.Default, 10, 0));
        Assert.NotEqual(Checksum(a), Checksum(GolfEngine.Shoot(778, 79, GolfConfig.Default)));
    }

    [Theory]
    [InlineData(78, true, false, 100)]   // v0 .5928 -> rest .8487, inside the cup radius of the pin at .84: sunk
    [InlineData(77, true, false, 100)]   // rest .8283, .0117 from the pin: sunk
    [InlineData(76, false, false, 16)]   // rest .8083, .0317 from the pin: on the green, near the fringe
    [InlineData(79, false, true, 65)]    // rolls across the cup too fast (.1135 > .070): lips out to .8614, .0214 from the pin
    [InlineData(60, false, false, 0)]    // short of the green
    public void Variance_free_shots_match_upstreams_formulas_by_hand(int power, bool sunk, bool lip, int score)
    {
        var s = GolfEngine.Shoot(1, power, Calm);
        Assert.Equal(power, s.EffectivePower);
        Assert.Equal(sunk, s.IsSunk);
        Assert.Equal(lip, s.IsLipOut);
        Assert.Equal(score, s.BaseScore);
        Assert.Equal(score > 0, s.IsWin);
        Assert.Equal(sunk ? 0.0 : s.DistanceFromPin, s.DistanceFromPin);
        Assert.Equal(6, s.Keyframes.Count);
    }

    [Fact]
    public void A_faster_cup_capture_speed_lets_a_rolling_ball_drop_in_instead_of_lipping_out()
    {
        var capture = Calm with { CupCaptureSpeedMultiplier = 2.5 };
        var s = GolfEngine.Shoot(1, 79, capture);
        Assert.True(s.IsSunk);
        Assert.False(s.IsLipOut);
        Assert.Equal(s.Target, s.RestPosition);
    }

    [Fact]
    public void A_ball_that_flies_past_the_cup_neither_drops_in_nor_lips_out()
    {
        var long_ = Calm with { DriverPowerMultiplier = 2.5 };
        var s = GolfEngine.Shoot(1, 100, long_);
        Assert.False(s.IsSunk);
        Assert.False(s.IsLipOut);
        Assert.Equal(0.98, s.RestPosition);
        Assert.Equal(0, s.BaseScore);
    }

    [Fact]
    public void A_very_soft_shot_is_a_two_keyframe_ground_roll()
    {
        var s = GolfEngine.Shoot(1, 1, Calm);
        Assert.Equal(2, s.Keyframes.Count);
        Assert.Equal(66, s.TotalTicks); // the 1.1 s minimum roll at 60 Hz
        Assert.Equal(0, s.Keyframes[0].Tick);
        Assert.Equal(0.50, s.Keyframes[0].Lateral);
        Assert.Equal(0, s.BaseScore);
    }

    [Fact]
    public void Cross_breeze_pushes_the_ball_sideways_and_lip_outs_deflect_off_the_rim()
    {
        var breeze = GolfEngine.Shoot(1, 79, Calm with { FixedCondition = GreenCondition.CrossBreeze });
        Assert.NotEqual(0.50, breeze.Keyframes[^1].Lateral);
        Assert.InRange(breeze.Keyframes[^1].Lateral, 0.16, 0.84);

        var rim = GolfEngine.Shoot(1, 79, Calm);
        Assert.True(rim.IsLipOut);
        Assert.Contains(Math.Round(rim.Keyframes[^1].Lateral, 2), new[] { 0.44, 0.56 });

        var straight = GolfEngine.Shoot(1, 60, Calm);
        Assert.Equal(0.50, straight.Keyframes[^1].Lateral);
    }

    [Fact]
    public void Keyframes_are_ordered_start_on_the_tee_and_end_at_rest()
    {
        for (ulong seed = 1; seed <= 300; seed++)
        {
            var s = GolfEngine.Shoot(seed, null, GolfConfig.Default);
            Assert.Equal(0, s.Keyframes[0].Tick);
            Assert.Equal(SlideShotPhysics.TeePosition, s.Keyframes[0].Axis);
            Assert.Equal(s.TotalTicks, s.Keyframes[^1].Tick);
            Assert.Equal(s.RestPosition, s.Keyframes[^1].Axis);
            for (var i = 1; i < s.Keyframes.Count; i++)
            {
                Assert.True(s.Keyframes[i].Tick > s.Keyframes[i - 1].Tick);
            }

            Assert.InRange(s.Power, 65, 85);
            Assert.InRange(s.EffectivePower, 1.0, 100.0);
        }
    }

    [Fact]
    public void The_condition_is_drawn_from_the_seed_unless_pinned_and_a_given_power_is_kept()
    {
        var seen = new HashSet<GreenCondition>();
        for (ulong seed = 1; seed <= 200; seed++)
        {
            var s = GolfEngine.Shoot(seed, 79, GolfConfig.Default);
            seen.Add(s.Condition);
            Assert.Equal(79, s.Power);
        }

        Assert.Equal(4, seen.Count);
        Assert.Equal(GreenCondition.HeavyDew, GolfEngine.Shoot(5, 79, GolfConfig.Default with { FixedCondition = GreenCondition.HeavyDew }).Condition);
    }

    [Fact]
    public void Default_power_is_drawn_inside_the_configured_range()
    {
        var narrow = GolfConfig.Default with { DefaultPowerMin = 70, DefaultPowerMax = 70 };
        for (ulong seed = 1; seed <= 20; seed++)
        {
            Assert.Equal(70, GolfEngine.Shoot(seed, null, narrow).Power);
        }
    }

    [Theory]
    [InlineData(0.0, true, 100, 100)]
    [InlineData(0.014, false, 100, 100)]
    [InlineData(0.035, false, 100, 1)]
    [InlineData(0.0351, false, 100, 0)]
    [InlineData(0.0246, false, 100, 50)]
    [InlineData(0.014, false, 250, 250)]
    [InlineData(0.0, true, 250, 250)]
    public void Closest_to_the_pin_score_matches_the_overlays_formula(double dist, bool sunk, int max, int expected) =>
        Assert.Equal(expected, GolfEngine.Score(dist, sunk, max));

    [Fact]
    public void Score_never_increases_as_the_ball_ends_further_from_the_pin()
    {
        var last = int.MaxValue;
        for (var d = 0.0; d <= 0.05; d += 0.0005)
        {
            var score = GolfEngine.Score(d, false, 100);
            Assert.True(score <= last);
            last = score;
        }
    }

    [Fact]
    public void Drag_and_labels_follow_upstreams_condition_table()
    {
        Assert.Equal(1.00, GolfEngine.DragMultiplier(GreenCondition.StandardGreen));
        Assert.Equal(0.78, GolfEngine.DragMultiplier(GreenCondition.FastLinks));
        Assert.Equal(1.28, GolfEngine.DragMultiplier(GreenCondition.HeavyDew));
        Assert.Equal(1.05, GolfEngine.DragMultiplier(GreenCondition.CrossBreeze));
        Assert.Equal("Standard Green", GolfEngine.Label(GreenCondition.StandardGreen));
        Assert.Equal("Fast Links", GolfEngine.Label(GreenCondition.FastLinks));
        Assert.Equal("Heavy Dew", GolfEngine.Label(GreenCondition.HeavyDew));
        Assert.Equal("Cross-Breeze", GolfEngine.Label(GreenCondition.CrossBreeze));
    }

    [Fact]
    public void Config_is_clamped_to_the_ranges_upstream_accepts()
    {
        var c = new GolfConfig(-5, 99, 0.0, 10, 5000, 0, 500, null).Clamped();
        Assert.Equal(0.0, c.PowerVariancePercent);
        Assert.Equal(2.5, c.CupCaptureSpeedMultiplier);
        Assert.Equal(0.4, c.DriverPowerMultiplier);
        Assert.Equal(40.0, c.TargetDistancePercent);
        Assert.Equal(1000, c.MaxScore);
        Assert.Equal(1, c.DefaultPowerMin);
        Assert.Equal(100, c.DefaultPowerMax);
        Assert.Equal(0.50, SlideShotPhysics.NormalizeTarget(10));
        Assert.Equal(0.95, SlideShotPhysics.NormalizeTarget(500));
    }

    [Fact]
    public void Every_outcome_class_occurs_across_seeds_for_the_best_power()
    {
        int sunk = 0, lip = 0, green = 0, miss = 0;
        for (ulong seed = 1; seed <= 400; seed++)
        {
            var s = GolfEngine.Shoot(seed, 79, GolfConfig.Default);
            if (s.IsSunk) { sunk++; }
            else if (s.IsLipOut) { lip++; }
            else if (s.IsWin) { green++; }
            else { miss++; }
        }

        Assert.True(sunk > 0 && lip > 0 && green > 0 && miss > 0, $"sunk {sunk} lip {lip} green {green} miss {miss}");
    }

    [Fact]
    public void Velocity_variance_never_pushes_power_outside_one_to_a_hundred()
    {
        var wild = GolfConfig.Default with { PowerVariancePercent = 50 };
        for (ulong seed = 1; seed <= 200; seed++)
        {
            Assert.InRange(GolfEngine.Shoot(seed, 100, wild).EffectivePower, 1.0, 100.0);
            Assert.InRange(GolfEngine.Shoot(seed, 1, wild).EffectivePower, 1.0, 100.0);
        }
    }
}

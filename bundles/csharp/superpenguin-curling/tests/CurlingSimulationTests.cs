using SuperPenguin.DropGameCore;
using Xunit;

namespace WaddleBundleSuperpenguinCurling.Tests;

/// <summary>Golden, determinism, scoring, keyframe and payout-math tests for the pure Curling simulation and its vendored slide physics.</summary>
public class CurlingSimulationTests
{
    private static readonly CurlingConfig Calm = CurlingConfig.Default with { DynamicIce = false, PowerVariancePercent = 0.0 };

    private static int Scaled(double v) => (int)Math.Round(v * 10000.0);

    /// <summary>FNV over (tick, x, y, v) of every keyframe, mapped to x/y like the envelope does; the golden values come from an independent python oracle.</summary>
    private static ulong Checksum(CurlingResult r, CurlingConfig c)
    {
        var h = 0xcbf29ce484222325UL;
        foreach (var k in r.Keyframes)
        {
            var x = Scaled(c.IsHorizontal ? k.AxisPos : k.LateralPos);
            var y = Scaled(c.IsHorizontal ? k.LateralPos : k.AxisPos);
            foreach (var v in new[] { k.Tick, x, y, Scaled(k.AxisVelocity) })
            {
                h = (h ^ (ulong)(uint)v) * 0x100000001b3UL;
            }
        }

        return h;
    }

    [Theory]
    [InlineData(1UL, -1, 2, 75.0, 76.0375, 6430, 4977, 123, false, 0, 22, 0x4bc2cffd62d577a0UL)]
    [InlineData(42UL, 78, 2, 78.0, 80.5206, 7150, 5089, 130, false, 25, 23, 0x11d684c5a4cd8361UL)]
    [InlineData(0xDEADBEEFUL, 100, 0, 100.0, 97.6466, 9800, 4950, 202, true, 0, 35, 0x5f1e8652bed01788UL)]
    [InlineData(7UL, 1, 2, 1.0, 5.7548, 534, 5101, 30, false, 0, 6, 0x930c3a49b93e0157UL)]
    public void Golden_seeds_reproduce_the_exact_slide(ulong seed, int power, int ice, double raw, double effective, int rest, int lateral, int ticks, bool wall, int score, int frames, ulong sum)
    {
        var r = CurlingSimulation.Run(seed, CurlingConfig.Default, power < 0 ? null : power);
        Assert.Equal((IceCondition)ice, r.Ice);
        Assert.Equal(raw, r.RawPower);
        Assert.Equal(effective, Math.Round(r.EffectivePower, 4));
        Assert.Equal(rest, Scaled(r.RestPos));
        Assert.Equal(lateral, Scaled(r.Lateral));
        Assert.Equal(ticks, r.TotalTicks);
        Assert.Equal(wall, r.WallHit);
        Assert.Equal(score, r.Score);
        Assert.Equal(frames, r.Keyframes.Count);
        Assert.Equal(sum, Checksum(r, CurlingConfig.Default));
        Assert.False(r.IsReversed);
    }

    [Fact]
    public void Golden_reversed_vertical_shuffleboard_wall_hit()
    {
        var cfg = CurlingConfig.Default with
        {
            Strip = StripPosition.Left, Direction = SlideDirection.BottomToTop, Scoring = ScoringType.Shuffleboard, KitchenPenalty = true,
            PowerVariancePercent = 0.0, DynamicIce = false, FixedIce = IceCondition.Slick,
        };
        var r = CurlingSimulation.Run(11, cfg, 80);
        Assert.True(r.IsReversed);
        Assert.Equal(0.95, r.StartPos);
        Assert.True(r.WallHit);
        Assert.Equal(200, Scaled(r.RestPos));
        Assert.Equal(213, r.TotalTicks);
        Assert.Equal(37, r.Keyframes.Count);
        Assert.Equal(0, r.Score);
        Assert.Equal(0xd3469004ed4aa141UL, Checksum(r, cfg));
        Assert.Equal(-5908, Scaled(r.Keyframes[1].AxisVelocity));
    }

    [Fact]
    public void Golden_right_to_left_and_alternating_agree_on_the_reversed_shot()
    {
        var cfg = CurlingConfig.Default with { Direction = SlideDirection.RightToLeft, DynamicIce = false, PowerVariancePercent = 0.0 };
        var r = CurlingSimulation.Run(5, cfg, 78);
        Assert.True(r.IsReversed);
        Assert.Equal(1513, Scaled(r.RestPos));
        Assert.Equal(100, r.Score);
        Assert.Equal(0x8e5d19e1f61fd17eUL, Checksum(r, cfg));
        var alt = cfg with { Direction = SlideDirection.Alternating };
        Assert.Equal(0x8e5d19e1f61fd17eUL, Checksum(CurlingSimulation.Run(5, alt, 78), alt));
        var forward = CurlingSimulation.Run(7, alt, 78);
        Assert.False(forward.IsReversed);
        Assert.Equal(0xa6f4f260527b61cUL, Checksum(forward, alt));
    }

    [Fact]
    public void Golden_cross_drift_on_a_top_strip_pulls_the_stone_sideways()
    {
        var cfg = CurlingConfig.Default with { Strip = StripPosition.Top, Direction = SlideDirection.Alternating, DynamicIce = false, FixedIce = IceCondition.CrossDrift };
        var r = CurlingSimulation.Run(99, cfg, 70);
        Assert.Equal(IceCondition.CrossDrift, r.Ice);
        Assert.Equal(6276, Scaled(r.Lateral));
        Assert.Equal(6784, Scaled(r.RestPos));
        Assert.Equal(0xedbc072ceefa8212UL, Checksum(r, cfg));
    }

    [Fact]
    public void Same_seed_is_byte_identical_and_different_seeds_differ()
    {
        var a = CurlingSimulation.Run(777, CurlingConfig.Default, 80);
        var b = CurlingSimulation.Run(777, CurlingConfig.Default, 80);
        Assert.Equal(Checksum(a, CurlingConfig.Default), Checksum(b, CurlingConfig.Default));
        Assert.Equal(CurlingLogic.DataJson(a, CurlingConfig.Default), CurlingLogic.DataJson(b, CurlingConfig.Default));
        Assert.NotEqual(Checksum(a, CurlingConfig.Default), Checksum(CurlingSimulation.Run(778, CurlingConfig.Default, 80), CurlingConfig.Default));
    }

    [Fact]
    public void Keyframes_are_well_formed_for_many_seeds()
    {
        for (ulong s = 1; s <= 300; s++)
        {
            var r = CurlingSimulation.Run(SplitMix64.Mix(s), CurlingConfig.Default, 60 + (int)(s % 41));
            var k = r.Keyframes;
            Assert.Equal(0, k[0].Tick);
            Assert.Equal(r.StartPos, k[0].AxisPos);
            Assert.Equal(r.TotalTicks, k[^1].Tick);
            Assert.Equal(r.RestPos, k[^1].AxisPos);
            Assert.Equal(0.0, k[^1].AxisVelocity);
            for (var i = 1; i < k.Count; i++)
            {
                Assert.True(k[i].Tick > k[i - 1].Tick);
                Assert.True(k[i].AxisPos >= k[i - 1].AxisPos - 1e-9, "forward slide never moves backwards");
                Assert.InRange(k[i].AxisPos, 0.02 - 1e-9, 0.98 + 1e-9);
                Assert.True(Math.Abs(k[i].AxisVelocity) <= Math.Abs(k[i - 1].AxisVelocity) + 1e-9);
            }
        }
    }

    [Fact]
    public void Reversed_keyframes_never_move_forward()
    {
        var cfg = CurlingConfig.Default with { Direction = SlideDirection.RightToLeft };
        for (ulong s = 1; s <= 60; s++)
        {
            var k = CurlingSimulation.Run(SplitMix64.Mix(s), cfg, 80).Keyframes;
            for (var i = 1; i < k.Count; i++)
            {
                Assert.True(k[i].AxisPos <= k[i - 1].AxisPos + 1e-9);
            }
        }
    }

    [Fact]
    public void Default_power_is_drawn_from_the_configured_range_and_a_given_power_is_respected()
    {
        for (ulong s = 1; s <= 300; s++)
        {
            var seed = SplitMix64.Mix(s);
            var drawn = CurlingSimulation.Run(seed, CurlingConfig.Default, null);
            Assert.InRange(drawn.RawPower, 65, 85);
            Assert.InRange(drawn.EffectivePower, drawn.RawPower - 7.0, drawn.RawPower + 7.0);
            Assert.Equal(55.0, CurlingSimulation.Run(seed, CurlingConfig.Default, 55).RawPower);
        }
    }

    [Fact]
    public void Zero_variance_keeps_the_exact_power_and_a_centred_lane()
    {
        var r = CurlingSimulation.Run(3, Calm, 78);
        Assert.Equal(78.0, r.EffectivePower);
        Assert.Equal(0.50, r.Lateral);
    }

    [Fact]
    public void Variance_jitters_the_lane_within_bounds_and_cross_drift_pulls_wider()
    {
        var drift = CurlingConfig.Default with { DynamicIce = false, FixedIce = IceCondition.CrossDrift };
        var sides = new HashSet<bool>();
        for (ulong s = 1; s <= 200; s++)
        {
            var seed = SplitMix64.Mix(s);
            var normal = CurlingSimulation.Run(seed, CurlingConfig.Default with { DynamicIce = false }, 80);
            Assert.InRange(normal.Lateral, 0.50 - 0.0106, 0.50 + 0.0106);
            var d = CurlingSimulation.Run(seed, drift, 80);
            Assert.InRange(d.Lateral, 0.15, 0.85);
            Assert.True(Math.Abs(d.Lateral - 0.50) > 0.10, "cross drift moves the lane well off centre");
            sides.Add(d.Lateral > 0.5);
        }

        Assert.Equal(2, sides.Count);
    }

    [Fact]
    public void Dynamic_ice_rolls_all_four_conditions_and_a_fixed_ice_is_respected()
    {
        var seen = new HashSet<IceCondition>();
        for (ulong s = 1; s <= 400; s++)
        {
            seen.Add(CurlingSimulation.Run(SplitMix64.Mix(s), CurlingConfig.Default, 80).Ice);
            Assert.Equal(IceCondition.HeavyFrost, CurlingSimulation.Run(SplitMix64.Mix(s), CurlingConfig.Default with { DynamicIce = false, FixedIce = IceCondition.HeavyFrost }, 80).Ice);
        }

        Assert.Equal(4, seen.Count);
    }

    [Theory]
    [InlineData(IceCondition.Slick, 0.78, "Slick Ice")]
    [InlineData(IceCondition.Normal, 1.00, "Normal Ice")]
    [InlineData(IceCondition.HeavyFrost, 1.28, "Heavy Frost")]
    [InlineData(IceCondition.CrossDrift, 1.05, "Cross-Drift")]
    public void Ice_drag_and_labels_match_upstream(IceCondition ice, double drag, string label)
    {
        Assert.Equal(drag, CurlingSimulation.DragMultiplier(ice));
        Assert.Equal(label, CurlingSimulation.Label(ice));
    }

    [Theory]
    [InlineData(0.84, 0.84, 100, true, true)]
    [InlineData(0.85, 0.84, 100, true, true)]
    [InlineData(0.88, 0.84, 75, false, true)]
    [InlineData(0.91, 0.84, 50, false, true)]
    [InlineData(0.95, 0.84, 25, false, true)]
    [InlineData(0.50, 0.84, 0, false, false)]
    public void House_rings_score_like_upstream(double rest, double target, int score, bool bullseye, bool win)
    {
        Assert.Equal((score, bullseye, win), CurlingSimulation.CalculateScore(rest, target, ScoringType.CurlingHouse, false, false));
    }

    [Theory]
    [InlineData(0.84, 30, true, true)]
    [InlineData(0.80, 20, false, true)]
    [InlineData(0.76, 10, false, true)]
    [InlineData(0.88, -10, false, false)]
    [InlineData(0.40, 0, false, false)]
    public void Shuffleboard_zones_score_like_upstream(double rest, int score, bool bullseye, bool win)
    {
        Assert.Equal((score, bullseye, win), CurlingSimulation.CalculateScore(rest, 0.84, ScoringType.Shuffleboard, false, true));
    }

    [Fact]
    public void Shuffleboard_kitchen_penalty_only_applies_when_enabled_and_mirrors_when_reversed()
    {
        Assert.Equal((0, false, false), CurlingSimulation.CalculateScore(0.88, 0.84, ScoringType.Shuffleboard, false, false));
        Assert.Equal((-10, false, false), CurlingSimulation.CalculateScore(0.12, 0.16, ScoringType.Shuffleboard, true, true));
        Assert.Equal((20, false, true), CurlingSimulation.CalculateScore(0.20, 0.16, ScoringType.Shuffleboard, true, true));
    }

    [Fact]
    public void Power_sweep_on_calm_normal_ice_walks_through_every_house_ring()
    {
        int ScoreAt(int power) => CurlingSimulation.Run(1, Calm, power).Score;
        Assert.Equal(0, ScoreAt(60));
        Assert.Equal(25, ScoreAt(72));
        Assert.Equal(50, ScoreAt(74));
        Assert.Equal(75, ScoreAt(76));
        Assert.Equal(100, ScoreAt(77));
        Assert.True(CurlingSimulation.Run(1, Calm, 100).WallHit);
        Assert.Equal(0, ScoreAt(100));
        Assert.True(CurlingSimulation.Run(1, Calm, 77).IsBullseye);
        Assert.False(CurlingSimulation.Run(1, Calm, 76).IsBullseye);
    }

    [Fact]
    public void Power_sweep_on_the_shuffleboard_hits_every_zone_and_the_kitchen()
    {
        var cfg = Calm with { Scoring = ScoringType.Shuffleboard, KitchenPenalty = true };
        Assert.Equal(10, CurlingSimulation.Run(1, cfg, 73).Score);
        Assert.Equal(20, CurlingSimulation.Run(1, cfg, 75).Score);
        Assert.Equal(30, CurlingSimulation.Run(1, cfg, 77).Score);
        Assert.Equal(-10, CurlingSimulation.Run(1, cfg, 79).Score);
        Assert.False(CurlingSimulation.Run(1, cfg, 79).IsWin);
        Assert.Equal(0, CurlingSimulation.Run(1, cfg, 60).Score);
    }

    [Fact]
    public void Center_target_puts_the_house_in_the_middle_of_the_strip()
    {
        var cfg = Calm with { Direction = SlideDirection.CenterTarget };
        var r = CurlingSimulation.Run(1, cfg, 58);
        Assert.Equal(0.5, r.TargetRatio);
        Assert.Equal(100, r.Score);
    }

    [Fact]
    public void Config_clamps_target_variance_and_power_range()
    {
        var c = (CurlingConfig.Default with { TargetDistancePercent = 10, PowerVariancePercent = 99, DefaultPowerMin = 90, DefaultPowerMax = 20 }).Clamped();
        Assert.Equal(60.0, c.TargetDistancePercent);
        Assert.Equal(25.0, c.PowerVariancePercent);
        Assert.Equal((20, 90), (c.DefaultPowerMin, c.DefaultPowerMax));
        var hi = (CurlingConfig.Default with { TargetDistancePercent = 500, PowerVariancePercent = -3, DefaultPowerMin = -5, DefaultPowerMax = 500 }).Clamped();
        Assert.Equal((95.0, 0.0, 1, 100), (hi.TargetDistancePercent, hi.PowerVariancePercent, hi.DefaultPowerMin, hi.DefaultPowerMax));
    }

    [Theory]
    [InlineData(StripPosition.Bottom, true)]
    [InlineData(StripPosition.Top, true)]
    [InlineData(StripPosition.Left, false)]
    [InlineData(StripPosition.Right, false)]
    public void Strip_orientation(StripPosition strip, bool horizontal)
    {
        Assert.Equal(horizontal, (CurlingConfig.Default with { Strip = strip }).IsHorizontal);
    }

    [Fact]
    public void Slide_physics_effective_power_clamps_and_survives_a_non_finite_input()
    {
        var rng = new Xoshiro256(1);
        Assert.Equal(78.5, SlideShotPhysics.CalculateEffectivePower(78.5, 0.0, rng));
        Assert.Equal(100.0, SlideShotPhysics.CalculateEffectivePower(250.0, 0.0, rng));
        Assert.Equal(1.0, SlideShotPhysics.CalculateEffectivePower(-5.0, 0.0, rng));
        Assert.Equal(75.0, SlideShotPhysics.CalculateEffectivePower(double.NaN, 0.0, rng));
        for (var i = 0; i < 100; i++)
        {
            Assert.InRange(SlideShotPhysics.CalculateEffectivePower(60.0, 3.0, rng), 57.0, 63.0);
        }
    }

    [Fact]
    public void Slide_physics_speed_distance_and_geometry()
    {
        Assert.True(SlideShotPhysics.PowerToInitialSpeed(100) > SlideShotPhysics.PowerToInitialSpeed(50));
        Assert.Equal(0.0076, SlideShotPhysics.PowerToInitialSpeed(0));
        Assert.Equal(0.76, SlideShotPhysics.PowerToInitialSpeed(500));
        var v = 0.50;
        Assert.True(SlideShotPhysics.CalculateTravelDistance(v, 0.78) > SlideShotPhysics.CalculateTravelDistance(v, 1.0));
        Assert.True(SlideShotPhysics.CalculateTravelDistance(v, 1.0) > SlideShotPhysics.CalculateTravelDistance(v, 1.28));
        Assert.Equal(0.05, SlideShotPhysics.DefaultTeePosition(false));
        Assert.Equal(0.95, SlideShotPhysics.DefaultTeePosition(true));
        var p = SlideShotPhysics.CreatePoint(3, 0.2, 0.5, -0.4);
        Assert.Equal(new SlidePoint(3, 0.2, 0.5, -0.4), p);
    }

    [Theory]
    [InlineData(SlideDirection.LeftToRight, false, false)]
    [InlineData(SlideDirection.TopToBottom, true, false)]
    [InlineData(SlideDirection.RightToLeft, false, true)]
    [InlineData(SlideDirection.BottomToTop, false, true)]
    [InlineData(SlideDirection.Alternating, true, true)]
    [InlineData(SlideDirection.Alternating, false, false)]
    [InlineData(SlideDirection.CenterTarget, true, false)]
    public void Direction_resolves_to_reversed_or_not(SlideDirection direction, bool coin, bool reversed)
    {
        Assert.Equal(reversed, SlideShotPhysics.ResolveIsReversed(direction, coin));
    }

    [Fact]
    public void Normalized_target_mirrors_when_reversed_and_centres_on_center_target()
    {
        Assert.Equal(0.84, SlideShotPhysics.ResolveNormalizedTarget(SlideDirection.LeftToRight, 84.0, false), 6);
        Assert.Equal(0.16, SlideShotPhysics.ResolveNormalizedTarget(SlideDirection.RightToLeft, 84.0, true), 6);
        Assert.Equal(0.50, SlideShotPhysics.ResolveNormalizedTarget(SlideDirection.CenterTarget, 84.0, false), 6);
        Assert.Equal(0.50, SlideShotPhysics.ResolveNormalizedTarget(SlideDirection.LeftToRight, 10.0, false), 6);
        Assert.Equal(0.95, SlideShotPhysics.ResolveNormalizedTarget(SlideDirection.LeftToRight, 99.0, false), 6);
    }

    [Fact]
    public void Return_to_player_stays_under_one_for_every_power_on_both_sheets()
    {
        foreach (var scoring in new[] { ScoringType.CurlingHouse, ScoringType.Shuffleboard })
        {
            var cfg = CurlingConfig.Default with { Scoring = scoring };
            const int n = 3000;
            for (var power = 70; power <= 90; power += 4)
            {
                double paid = 0;
                for (ulong s = 1; s <= n; s++)
                {
                    paid += CurlingLogic.PayoutFor(100, CurlingSimulation.Run(SplitMix64.Mix(s), cfg, power).Score, scoring);
                }

                Assert.True(paid / (n * 100.0) < 1.0, $"{scoring} at power {power} returned {paid / (n * 100.0):0.000}");
            }

            double byDefault = 0;
            for (ulong s = 1; s <= n; s++)
            {
                byDefault += CurlingLogic.PayoutFor(100, CurlingSimulation.Run(SplitMix64.Mix(s), cfg, null).Score, scoring);
            }

            Assert.InRange(byDefault / (n * 100.0), 0.45, 0.85);
        }
    }
}

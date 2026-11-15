using SuperPenguin.DropGameCore;
using Xunit;

namespace WaddleBundleSuperpenguinParachute.Tests;

/// <summary>Upstream-table and formula tests for the ported droplet and rocket generators.</summary>
public class ParachuteGeneratorTests
{
    private static GlideBaseline Baseline(double startX = 0.5, double vx = 0.0) =>
        ParachuteSimulationEngine.BuildBaseline(startX, vx, 0.145, 372, 0.5, 0.09);

    private static ulong NextOf(Xoshiro256 rng) => rng.NextULong();

    [Theory]
    [InlineData("Small", "Small", 0.60, 0.018, 1.80)]
    [InlineData("small", "Small", 0.60, 0.018, 1.80)]
    [InlineData("Medium", "Medium", 1.00, 0.028, 3.20)]
    [InlineData("Large", "Large", 1.50, 0.042, 5.50)]
    [InlineData("Giant", "Giant", 2.00, 0.058, 8.50)]
    [InlineData("nonsense", "Medium", 1.00, 0.028, 3.20)]
    public void Fixed_size_presets_match_upstreams_table_without_drawing(string preset, string label, double scale, double radius, double mass)
    {
        var rng = new Xoshiro256(3);
        Assert.Equal((label, scale, radius, mass), DropletPatternGenerator.ResolveSize(preset, null, rng));
        Assert.Equal(NextOf(new Xoshiro256(3)), NextOf(rng));
    }

    [Fact]
    public void Random_size_pools_use_upstreams_weights_and_restrictions()
    {
        var rng = new Xoshiro256(11);
        var counts = new Dictionary<string, int>();
        for (var i = 0; i < 4000; i++)
        {
            var label = DropletPatternGenerator.ResolveSize("Random", ["Small", "Medium", "Large", "Giant"], rng).Label;
            counts[label] = counts.GetValueOrDefault(label) + 1;
        }

        Assert.InRange(counts["Small"], 800, 1200);
        Assert.InRange(counts["Medium"], 1800, 2200);
        Assert.InRange(counts["Large"], 500, 850);
        Assert.InRange(counts["Giant"], 200, 450);

        var restricted = Enumerable.Range(0, 200).Select(_ => DropletPatternGenerator.ResolveSize("Random:Large, Giant", null, rng).Label).Distinct().Order();
        Assert.Equal(["Giant", "Large"], restricted);
        var listed = Enumerable.Range(0, 200).Select(_ => DropletPatternGenerator.ResolveSize("Small,Medium", null, rng).Label).Distinct().Order();
        Assert.Equal(["Medium", "Small"], listed);
        var bogus = Enumerable.Range(0, 400).Select(_ => DropletPatternGenerator.ResolveSize("Random", ["x", " ", "Tiny"], rng).Label).Distinct();
        Assert.Equal(4, bogus.Count());
        var emptyPool = DropletPatternGenerator.ResolveSize("Random", null, rng).Label;
        Assert.Contains(emptyPool, new[] { "Small", "Medium", "Large", "Giant" });
        var single = new Xoshiro256(4);
        Assert.Equal("Large", DropletPatternGenerator.PickFromAllowed(["large", "LARGE"], single));
        Assert.Equal(NextOf(new Xoshiro256(4)), NextOf(single));
    }

    [Theory]
    [InlineData("Slow", "Slow", 0.65)]
    [InlineData("Normal", "Normal", 1.0)]
    [InlineData("fast", "Fast", 1.60)]
    [InlineData("Hyper", "Hyper", 2.20)]
    [InlineData("bogus", "Normal", 1.0)]
    public void Fixed_speed_presets_match_upstreams_table(string preset, string label, double scale)
    {
        Assert.Equal((label, scale), DropletPatternGenerator.ResolveSpeed(preset, new Xoshiro256(1)));
    }

    [Fact]
    public void Random_speed_covers_all_four_presets()
    {
        var rng = new Xoshiro256(2);
        var labels = Enumerable.Range(0, 200).Select(_ => DropletPatternGenerator.ResolveSpeed("Random", rng).Label).Distinct().Order();
        Assert.Equal(["Fast", "Hyper", "Normal", "Slow"], labels);
    }

    [Fact]
    public void Targeted_droplets_lead_the_baseline_path_to_the_intercept_point()
    {
        var baseline = Baseline();
        var rng = new Xoshiro256(1);
        var (x, y, vx, vy, g) = DropletPatternGenerator.CalculateTrajectory("Targeted", 0, 1.0, baseline, 60, 0, rng);
        // index 0: point.X (0.5) > 0.35 so it comes from the left, 0.28 away, 0.06 above the path point 24 ticks ahead.
        var point = ParachuteSimulationEngine.GlidePosition(baseline, 84);
        Assert.Equal(point.X - 0.28, x, 9);
        Assert.Equal(point.Y - 0.06, y, 9);
        Assert.Equal(0.08, g);
        var t = 24.0 / 60.0;
        Assert.Equal((point.X - x) / t, vx, 9);
        Assert.Equal((point.Y - y) / t, vy, 9);
        Assert.Equal(NextOf(new Xoshiro256(1)), NextOf(rng));

        var farRight = Baseline(0.8);
        var (x1, y1, _, _, _) = DropletPatternGenerator.CalculateTrajectory("targeted", 1, 1.0, farRight, 60, 4, new Xoshiro256(1));
        Assert.Equal(0.96, x1);
        Assert.Equal(ParachuteSimulationEngine.GlidePosition(farRight, 88).Y + 0.08, y1, 9);
        var (_, y2, _, _, _) = DropletPatternGenerator.CalculateTrajectory("Targeted", 2, 1.0, baseline, 60, 8, new Xoshiro256(1));
        Assert.Equal(ParachuteSimulationEngine.GlidePosition(baseline, 92).Y - 0.18, y2, 9);
    }

    [Fact]
    public void Side_wind_enters_from_alternating_edges()
    {
        var rng = new Xoshiro256(5);
        var left = DropletPatternGenerator.CalculateTrajectory("SideWind", 0, 2.0, Baseline(), 50, 0, rng);
        var right = DropletPatternGenerator.CalculateTrajectory("SideWind", 1, 2.0, Baseline(), 50, 4, rng);
        Assert.Equal(0.02, left.StartX);
        Assert.Equal(0.98, right.StartX);
        Assert.InRange(left.Vx, 0.32 * 2.0, 0.52 * 2.0);
        Assert.InRange(right.Vx, -0.52 * 2.0, -0.32 * 2.0);
        Assert.InRange(left.StartY, 0.12, 0.62);
        Assert.Equal(0.14 * 2.0, left.Gravity);
    }

    [Fact]
    public void Fountain_erupts_upward_beneath_the_player_and_top_rain_falls_from_above()
    {
        var baseline = Baseline();
        var f = DropletPatternGenerator.CalculateTrajectory("Fountain", 0, 1.0, baseline, 60, 0, new Xoshiro256(7));
        var point = ParachuteSimulationEngine.GlidePosition(baseline, 76);
        Assert.Equal(0.94, f.StartY);
        Assert.Equal(point.X - 0.015, f.StartX, 9);
        Assert.InRange(f.Vy, -1.15, -0.85);
        Assert.Equal(0.38, f.Gravity);
        var f2 = DropletPatternGenerator.CalculateTrajectory("Fountain", 1, 1.0, baseline, 60, 4, new Xoshiro256(7));
        Assert.True(f2.StartX > f.StartX);

        var rain = DropletPatternGenerator.CalculateTrajectory("TopRain", 0, 1.0, baseline, 60, 0, new Xoshiro256(7));
        Assert.Equal(-0.06, rain.StartY);
        Assert.InRange(rain.StartX, 0.06, 0.94);
        Assert.InRange(rain.Vx, -0.35, 0.35);
        Assert.InRange(rain.Vy, 0.06, 0.20);
        Assert.Equal(0.32, rain.Gravity);
    }

    [Fact]
    public void Late_waves_clamp_to_the_last_aimable_tick()
    {
        var baseline = Baseline();
        var (x, y, _, _, _) = DropletPatternGenerator.CalculateTrajectory("Targeted", 0, 1.0, baseline, 5000, 0, new Xoshiro256(1));
        Assert.InRange(x, 0.04, 0.96);
        Assert.InRange(y, 0.04, 0.88);
    }

    [Fact]
    public void Targeted_rockets_launch_from_under_the_landing_spot_with_staggered_fuses()
    {
        var baseline = Baseline(0.3, 0.0);
        var rockets = RocketTrajectoryGenerator.Generate("Targeted", 3, baseline, 100, new Xoshiro256(9));
        Assert.Equal(3, rockets.Count);
        Assert.Equal([100, 108, 116], rockets.Select(r => r.SpawnTick).ToArray());
        Assert.All(rockets, r =>
        {
            Assert.True(r.IsTargeted);
            Assert.Equal("Targeted", r.Pattern);
            Assert.Equal(0.0, r.Vx);
            Assert.Equal(1.02, r.StartY);
            Assert.InRange(r.FuseTicks, 72, 91);
            Assert.InRange(r.TargetY, 0.10, 0.25);
            Assert.Contains(r.Color, RocketTrajectoryGenerator.Colors);
            Assert.Equal(r.StartX, r.TargetX);
            Assert.InRange(r.StartX, 0.08, 0.92);
            Assert.Equal(0.04, r.AccelerationY);
        });
        Assert.Equal(baseline.LandingX - 0.04, rockets[0].StartX, 3);
        Assert.Equal(baseline.LandingX + 0.04, rockets[2].StartX, 3);
    }

    [Theory]
    [InlineData("Targeted")]
    [InlineData("Barrage")]
    [InlineData("Random")]
    public void A_rocket_reaches_its_apex_height_at_the_end_of_its_fuse(string pattern)
    {
        foreach (var r in RocketTrajectoryGenerator.Generate(pattern, 5, Baseline(), 50, new Xoshiro256(21)))
        {
            var t = r.FuseTicks / 60.0;
            var apex = r.StartY + (r.Vy * t) + (0.5 * r.AccelerationY * t * t);
            Assert.InRange(apex, r.TargetY - 0.002, r.TargetY + 0.002);
        }
    }

    [Fact]
    public void Barrage_fans_across_the_sky_and_angles_toward_the_centre()
    {
        var rockets = RocketTrajectoryGenerator.Generate("barrage", 5, Baseline(), 50, new Xoshiro256(4));
        Assert.All(rockets, r =>
        {
            Assert.Equal("Barrage", r.Pattern);
            Assert.False(r.IsTargeted);
            Assert.InRange(r.TargetY, 0.06, 0.18);
            Assert.InRange(r.StartX, 0.08, 0.92);
            Assert.True(r.Vx * (0.5 - r.StartX) >= 0, "angled toward the screen centre");
        });
        Assert.True(rockets[4].StartX > rockets[0].StartX);
    }

    [Fact]
    public void Unknown_patterns_are_random_and_the_count_is_clamped_to_1_through_10()
    {
        Assert.All(RocketTrajectoryGenerator.Generate("???", 1, Baseline(), 50, new Xoshiro256(1)), r => Assert.Equal("Random", r.Pattern));
        Assert.Single(RocketTrajectoryGenerator.Generate("Random", -4, Baseline(), 50, new Xoshiro256(1)));
        Assert.Equal(10, RocketTrajectoryGenerator.Generate("Random", 99, Baseline(), 50, new Xoshiro256(1)).Count);
    }
}

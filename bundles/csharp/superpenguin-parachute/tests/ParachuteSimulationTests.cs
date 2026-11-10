using SuperPenguin.DropGameCore;
using Xunit;

namespace WaddleBundleSuperpenguinParachute.Tests;

/// <summary>Golden, determinism, scoring, path-shape and hazard tests for the pure Parachute Drop simulation.</summary>
public class ParachuteSimulationTests
{
    private static ulong Checksum(ParachuteResult r)
    {
        var h = 0xcbf29ce484222325UL;
        foreach (var k in r.Keyframes)
        {
            foreach (var v in new[] { k.Tick, k.X, k.Y, k.Vx, k.Vy })
            {
                h = (h ^ (ulong)(uint)v) * 0x100000001b3UL;
            }
        }

        return h;
    }

    private static IEnumerable<ParachuteResult> Scan(ParachuteConfig config, int count) =>
        Enumerable.Range(1, count).Select(i => ParachuteSimulationEngine.Run(SplitMix64.Mix((ulong)i), config));

    private static ParachuteConfig Volley(string pattern, int count = 1) =>
        ParachuteConfig.Default with { RocketVolleyChancePct = 100, RocketPattern = pattern, RocketCount = count };

    [Theory]
    [InlineData(1UL, 352, 0.1207, 0, false, 3, 0, 62, 3, 0xc0ae36d291dd2e28UL)]
    [InlineData(42UL, 334, 0.6477, 0, false, 3, 0, 60, 3, 0x7cbb92e52222a675UL)]
    [InlineData(0xDEADBEEFUL, 349, 0.5514, 43, false, 3, 0, 62, 3, 0x42bb45699d7c4481UL)]
    [InlineData(12587370737594032228UL, 206, 0.5785, 7, true, 1, 1, 39, 2, 0x73aff63fcc920bcfUL)]
    [InlineData(7958955049054603978UL, 199, 0.4826, 41, true, 0, 1, 36, 1, 0x53cccd80d59afee9UL)]
    [InlineData(14180207640020093695UL, 392, 0.4961, 96, false, 2, 0, 71, 2, 0xfb59fbe7772be26aUL)]
    public void Golden_seeds_reproduce_the_exact_drop(ulong seed, int landTick, double landX, int score, bool destroyed, int dropletHits, int rocketHits, int frames, int collisions, ulong sum)
    {
        var r = ParachuteSimulationEngine.Run(seed, ParachuteConfig.Default);
        Assert.Equal(landTick, r.LandingTick);
        Assert.Equal(landX, r.LandingX);
        Assert.Equal(score, r.Score);
        Assert.Equal(destroyed, r.Destroyed);
        Assert.Equal(dropletHits, r.DropletHits);
        Assert.Equal(rocketHits, r.RocketHits);
        Assert.Equal(frames, r.Keyframes.Count);
        Assert.Equal(collisions, r.Collisions.Count);
        Assert.Equal(sum, Checksum(r));
        Assert.Equal(372, r.TotalTicks);
    }

    [Fact]
    public void Same_seed_is_byte_identical_and_different_seeds_differ()
    {
        var a = ParachuteSimulationEngine.Run(777, ParachuteConfig.Default);
        var b = ParachuteSimulationEngine.Run(777, ParachuteConfig.Default);
        Assert.Equal(Checksum(a), Checksum(b));
        Assert.Equal(a.Collisions, b.Collisions);
        Assert.Equal(ParachuteLogic.DataJson(a, 10, 0), ParachuteLogic.DataJson(b, 10, 0));
        Assert.NotEqual(Checksum(a), Checksum(ParachuteSimulationEngine.Run(778, ParachuteConfig.Default)));
    }

    [Fact]
    public void Path_is_well_formed_and_ends_on_the_ground_at_the_landing_tick()
    {
        foreach (var r in Scan(ParachuteConfig.Default, 600))
        {
            Assert.Equal(0, r.Keyframes[0].Tick);
            Assert.Equal(400, r.Keyframes[0].Y);
            for (var i = 1; i < r.Keyframes.Count; i++)
            {
                Assert.True(r.Keyframes[i].Tick > r.Keyframes[i - 1].Tick);
            }

            var last = r.Keyframes[^1];
            Assert.Equal(r.LandingTick, last.Tick);
            Assert.Equal(9400, last.Y);
            Assert.Equal((int)Math.Round(r.LandingX * 10000), last.X);
            Assert.All(r.Keyframes, k => Assert.InRange(k.X, 500, 9500));
            Assert.All(r.Collisions, c => Assert.InRange(c.Tick, 1, r.LandingTick));
            Assert.InRange(r.LandingX, 0.05, 0.95);
            Assert.Equal(r.Score > 0, r.IsWin);
        }
    }

    [Fact]
    public void Without_hazards_the_player_glides_to_the_baseline_landing_time()
    {
        var calm = ParachuteConfig.Default with { DropletCount = 0, RocketVolleyChancePct = 0 };
        foreach (var r in Scan(calm, 300))
        {
            Assert.Empty(r.Collisions);
            Assert.Empty(r.Droplets);
            Assert.Empty(r.Rockets);
            Assert.False(r.Destroyed);
            Assert.Equal(-1, r.FreeFallStartTick);
            Assert.InRange(Math.Abs(r.LandingTick - r.TotalTicks), 0, 8);
            Assert.Equal(string.Empty, r.RocketPattern);
        }
    }

    [Fact]
    public void Return_to_player_stays_close_to_one_but_below_it_and_wins_are_common_enough()
    {
        double paid = 0;
        var wins = 0;
        const int n = 8000;
        foreach (var r in Scan(ParachuteConfig.Default, n))
        {
            paid += ParachuteLogic.PayoutFor(100, r.Score);
            wins += r.IsWin ? 1 : 0;
        }

        Assert.InRange(paid / (n * 100.0), 0.85, 1.00);
        Assert.InRange(wins / (double)n, 0.20, 0.36);
    }

    [Fact]
    public void A_targeted_volley_hits_and_destroys_canopies_and_free_fall_lands_sooner()
    {
        var results = Scan(Volley("Targeted"), 400).ToList();
        var hit = results.Where(r => r.RocketHits > 0).ToList();
        Assert.True(hit.Count > 50, "targeted rockets must hit often");
        var destroyed = hit.Where(r => r.Destroyed).ToList();
        Assert.NotEmpty(destroyed);
        Assert.All(destroyed, r =>
        {
            Assert.True(r.FreeFallStartTick > 0);
            Assert.True(r.LandingTick > r.FreeFallStartTick);
            Assert.Contains(r.Collisions, c => c.Kind is 1 or 2);
            Assert.Contains(r.Rockets, f => f.Hit != 0);
        });
        Assert.Contains(results, r => r.Collisions.Any(c => c.Kind == 1));
        Assert.Contains(results, r => r.Collisions.Any(c => c.Kind == 2));
        Assert.Contains(destroyed, r => r.LandingTick < r.TotalTicks - 40);
    }

    [Fact]
    public void Canopy_kill_chance_and_direct_hit_setting_decide_whether_a_hit_destroys()
    {
        var never = Scan(Volley("Targeted") with { CanopyKillChance = 0.0 }, 300).ToList();
        Assert.Contains(never, r => r.RocketHits > 0);
        Assert.All(never, r => Assert.False(r.Destroyed));

        var softBody = Scan(Volley("Targeted") with { DirectHitKillsParachute = false }, 300).ToList();
        Assert.Contains(softBody, r => r.Collisions.Any(c => c.Kind == 2) && !r.Destroyed);
        Assert.All(softBody, r =>
        {
            if (r.Destroyed)
            {
                Assert.Contains(r.Collisions, c => c.Kind == 1);
            }
        });
    }

    [Fact]
    public void Crash_landing_score_modes_change_only_the_score_not_the_flight()
    {
        var preserve = Scan(Volley("Targeted") with { CrashMode = CrashLandingScoreMode.PreserveScore }, 300).ToList();
        var half = Scan(Volley("Targeted") with { CrashMode = CrashLandingScoreMode.HalfScore }, 300).ToList();
        var zero = Scan(Volley("Targeted") with { CrashMode = CrashLandingScoreMode.ZeroScore }, 300).ToList();
        var compared = 0;
        for (var i = 0; i < preserve.Count; i++)
        {
            Assert.Equal(Checksum(preserve[i]), Checksum(half[i]));
            Assert.Equal(Checksum(preserve[i]), Checksum(zero[i]));
            if (preserve[i].Destroyed && preserve[i].IsHit)
            {
                compared++;
                Assert.Equal(Math.Max(1, (int)Math.Round(preserve[i].Score * 0.5, MidpointRounding.AwayFromZero)), half[i].Score);
                Assert.Equal(0, zero[i].Score);
                Assert.False(zero[i].IsWin);
                Assert.True(zero[i].IsHit);
            }
        }

        Assert.True(compared > 0, "some destroyed parachute must land on the platform");
    }

    [Theory]
    [InlineData("Random")]
    [InlineData("Barrage")]
    public void Other_rocket_patterns_launch_staggered_volleys(string pattern)
    {
        var results = Scan(Volley(pattern, 4), 200).ToList();
        Assert.All(results, r =>
        {
            Assert.Equal(pattern, r.RocketPattern);
            Assert.Equal(4, r.Rockets.Count);
            Assert.Equal([0, 8, 16, 24], r.Rockets.Select(f => f.Spec.SpawnTick - r.Rockets[0].Spec.SpawnTick).ToArray());
            Assert.All(r.Rockets, f => Assert.False(f.Spec.IsTargeted));
        });
        Assert.Contains(results, r => r.RocketHits > 0);
        Assert.Contains(results, r => r.Rockets.Any(f => f.Hit == 0 && f.ExplodeTick > 0));
    }

    [Fact]
    public void A_rocket_still_in_the_air_at_touchdown_reports_explode_tick_minus_one()
    {
        var results = Scan(Volley("Random", 10), 300).ToList();
        Assert.Contains(results, r => r.Rockets.Any(f => f.ExplodeTick == -1));
    }

    [Theory]
    [InlineData("TopRain")]
    [InlineData("Targeted")]
    [InlineData("SideWind")]
    [InlineData("Fountain")]
    public void Every_droplet_pattern_runs_and_reports_its_wave(string pattern)
    {
        var cfg = ParachuteConfig.Default with { DropletPattern = pattern, DropletCount = 8, RocketVolleyChancePct = 0 };
        var results = Scan(cfg, 250).ToList();
        Assert.All(results, r =>
        {
            Assert.Equal(pattern, r.DropletPattern);
            Assert.Equal(8, r.Droplets.Count);
            Assert.All(r.Droplets, d => Assert.Equal(pattern, d.Pattern));
            Assert.Equal([0, 4, 8, 12, 16, 20, 24, 28], r.Droplets.Select(d => d.SpawnTick - r.Droplets[0].SpawnTick).ToArray());
        });
        Assert.Contains(results, r => r.DropletHits > 0);
    }

    [Fact]
    public void Aimed_droplet_patterns_hit_far_more_often_than_top_rain()
    {
        double Rate(string p) => Scan(ParachuteConfig.Default with { DropletPattern = p, RocketVolleyChancePct = 0 }, 300).Count(r => r.DropletHits > 0) / 300.0;
        Assert.True(Rate("Targeted") > 0.9);
        Assert.True(Rate("Fountain") > 0.9);
        Assert.True(Rate("TopRain") < Rate("Targeted"));
    }

    [Fact]
    public void Mixed_patterns_cover_every_upstream_pattern_and_unknown_names_fall_back()
    {
        var results = Scan(Volley("Mixed"), 300).ToList();
        Assert.Equal(ParachuteConfig.RocketPatterns.Order(), results.Select(r => r.RocketPattern).Distinct().Order());
        Assert.Equal(ParachuteConfig.DropletPatterns.Order(), results.Select(r => r.DropletPattern).Distinct().Order());
        var fallback = Scan(Volley("???") with { DropletPattern = "???" }, 5).First();
        Assert.Equal("Targeted", fallback.RocketPattern);
        Assert.Equal("TopRain", fallback.DropletPattern);
        Assert.Equal("Barrage", ParachuteSimulationEngine.ResolvePattern(" barrage ", ParachuteConfig.RocketPatterns, "Targeted", new Xoshiro256(1)));
    }

    [Fact]
    public void Droplet_hits_are_rate_limited_and_knock_the_player_sideways()
    {
        var cfg = ParachuteConfig.Default with { DropletPattern = "Targeted", DropletCount = 30, DropletSize = "Giant", RocketVolleyChancePct = 0 };
        var results = Scan(cfg, 150).ToList();
        foreach (var r in results)
        {
            var ticks = r.Collisions.Where(c => c.Kind == 0).Select(c => c.Tick).ToList();
            for (var i = 1; i < ticks.Count; i++)
            {
                Assert.True(ticks[i] - ticks[i - 1] >= 9);
            }

            Assert.All(r.Collisions.Where(c => c.Kind == 0), c => Assert.InRange(c.Dvx, -6500, 6500));
        }

        Assert.Contains(results, r => r.DropletHits >= 3);
    }

    [Theory]
    [InlineData(0.1, 8.5)]
    [InlineData(1.0, 6.2)]
    [InlineData(10.0, 3.5)]
    public void Gravity_sets_the_descent_time_like_upstream(double gravity, double seconds)
    {
        var r = ParachuteSimulationEngine.Run(9, ParachuteConfig.Default with { Gravity = gravity, DropletCount = 0, RocketVolleyChancePct = 0 });
        Assert.InRange(r.TotalTicks, (int)Math.Round(seconds * 60) - 8, (int)Math.Round(seconds * 60) + 8);
        Assert.InRange(r.LandingTick, r.TotalTicks - 2, r.TotalTicks + 8);
    }

    [Fact]
    public void Target_width_and_centre_are_clamped_and_a_wide_platform_wins_more()
    {
        var narrow = Scan(ParachuteConfig.Default with { TargetWidthPercent = 1 }, 400).ToList();
        var wide = Scan(ParachuteConfig.Default with { TargetWidthPercent = 70, TargetCenterX = 0.99 }, 400).ToList();
        Assert.All(narrow, r => Assert.Equal(0.02, r.TargetHalfWidth));
        Assert.All(wide, r => Assert.Equal(0.35, r.TargetHalfWidth));
        Assert.All(wide, r => Assert.Equal(0.62, r.TargetCenterX, 9));
        Assert.True(wide.Count(r => r.IsWin) > narrow.Count(r => r.IsWin));
        Assert.All(narrow, r => Assert.Equal(0.02 * 0.25, r.BullseyeHalfWidth));
    }

    [Fact]
    public void Wall_bounces_keep_every_keyframe_inside_the_board_even_with_extreme_bounciness()
    {
        var bouncy = ParachuteConfig.Default with { Bounciness = 5.0, Mass = 0.1, DropletKnockbackMultiplier = 5.0, RocketKnockbackMultiplier = 5.0, RocketVolleyChancePct = 100 };
        foreach (var r in Scan(bouncy, 400))
        {
            Assert.All(r.Keyframes, k => Assert.InRange(k.X, 500, 9500));
        }
    }

    [Theory]
    [InlineData(0.0, 0.5, 100, true, true, true)]
    [InlineData(0.09, 0.5, 1, true, false, true)]
    [InlineData(0.0225, 0.5, 75, true, false, true)]
    [InlineData(0.10, 0.5, 0, false, false, false)]
    public void Landing_score_follows_the_0_to_100_scale(double offset, double centre, int score, bool win, bool bullseye, bool hit)
    {
        var r = ParachuteSimulationEngine.CalculateLandingScore(centre + offset, centre, 0.09, false, CrashLandingScoreMode.PreserveScore);
        Assert.Equal((score, bullseye, win, hit), (r.Score, r.IsBullseye, r.IsWin, r.IsHit));
    }

    [Fact]
    public void Crash_landing_modes_score_like_upstream()
    {
        var preserve = ParachuteSimulationEngine.CalculateLandingScore(0.5, 0.5, 0.09, true, CrashLandingScoreMode.PreserveScore);
        Assert.Equal((100, true, true, true), preserve);
        var half = ParachuteSimulationEngine.CalculateLandingScore(0.5, 0.5, 0.09, true, CrashLandingScoreMode.HalfScore);
        Assert.Equal((50, false, true, true), half);
        var tiny = ParachuteSimulationEngine.CalculateLandingScore(0.59, 0.5, 0.09, true, CrashLandingScoreMode.HalfScore);
        Assert.Equal((1, false, true, true), tiny);
        var zero = ParachuteSimulationEngine.CalculateLandingScore(0.5, 0.5, 0.09, true, CrashLandingScoreMode.ZeroScore);
        Assert.Equal((0, false, false, true), zero);
        var miss = ParachuteSimulationEngine.CalculateLandingScore(0.9, 0.5, 0.09, true, CrashLandingScoreMode.HalfScore);
        Assert.Equal((0, false, false, false), miss);
    }

    [Fact]
    public void Tan_approximation_tracks_the_real_tangent_over_the_drop_angle_range()
    {
        for (var x = -0.95; x <= 0.9501; x += 0.01)
        {
            Assert.InRange(Math.Abs(ParachuteSimulationEngine.Tan(x) - Math.Tan(x)), 0, 2e-4);
        }

        Assert.Equal(0.0, ParachuteSimulationEngine.Tan(0.0));
    }

    [Fact]
    public void Baseline_bounces_off_the_edges_and_interpolates_like_upstream()
    {
        var straight = ParachuteSimulationEngine.BuildBaseline(0.5, 0.0, 0.145, 372, 0.5, 0.09);
        Assert.Equal(2, straight.Keyframes.Count);
        Assert.Equal(0.5, straight.LandingX);

        var left = ParachuteSimulationEngine.BuildBaseline(0.2, -0.2, 0.145, 372, 0.5, 0.09);
        Assert.Equal(3, left.Keyframes.Count);
        Assert.Equal(0.05, left.Keyframes[1].X);
        Assert.True(left.LandingX > 0.05);

        var right = ParachuteSimulationEngine.BuildBaseline(0.8, 0.2, 0.145, 372, 0.5, 0.09);
        Assert.Equal(0.95, right.Keyframes[1].X);
        Assert.True(right.LandingX < 0.95);

        Assert.Equal((0.2, 0.04), ParachuteSimulationEngine.GlidePosition(left, -5));
        Assert.Equal((left.LandingX, 0.94), ParachuteSimulationEngine.GlidePosition(left, 9999));
        var mid = ParachuteSimulationEngine.GlidePosition(straight, 186);
        Assert.Equal(0.5, mid.X);
        Assert.Equal(0.49, mid.Y);
    }

    [Fact]
    public void Config_is_clamped_like_upstream_settings()
    {
        var wild = new ParachuteConfig(0.5, 1, 0, 0, 0, CrashLandingScoreMode.ZeroScore, false, 9, 0, -3, "x", [], "x", "x", 0, 500, 99, "x").Clamped();
        Assert.Equal((4.0, 0.1, 0.1, 0.1, 1.0, 0.1, 0, 0.1, 100, 10), (wild.TargetWidthPercent, wild.Gravity, wild.Bounciness, wild.Mass, wild.CanopyKillChance, wild.RocketKnockbackMultiplier, wild.DropletCount, wild.DropletKnockbackMultiplier, wild.RocketVolleyChancePct, wild.RocketCount));
        var big = ParachuteConfig.Default with { Gravity = 99, Bounciness = 99, Mass = 99, DropletCount = 99, RocketCount = 0 };
        var c = big.Clamped();
        Assert.Equal((10.0, 5.0, 10.0, 30, 1), (c.Gravity, c.Bounciness, c.Mass, c.DropletCount, c.RocketCount));
    }
}

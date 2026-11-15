using System.Text.Json;
using SuperPenguin.DropGameCore;
using Xunit;

namespace WaddleBundleSuperpenguinBalloon.Tests;

/// <summary>The pure flight simulation: golden results, determinism, every hazard kind and pattern, the cloud, the baseline and the watchdog.</summary>
public class BalloonSimulationTests
{
    private static string Digest(BalloonResult r) =>
        $"end={r.EndTick} x={r.Keyframes[^1].X} y={r.Keyframes[^1].Y} cloud={(int)Math.Round(r.CloudXAtEnd * 10000)} popped={r.Popped}@{r.PopTick} hit={r.Hit} score={r.Score} bull={r.Bullseye} " +
        $"{r.Pattern}/{r.SpeedLabel}@{r.WaveTick} kf={r.Keyframes.Count} hz=[{string.Join(",", r.Hazards.Select(h => $"{h.Hazard.Kind}:{h.HitTick}"))}]";

    [Fact]
    public void Golden_seeds_give_exactly_these_flights()
    {
        var expected = new Dictionary<ulong, string>
        {
            [1] = "end=466 x=1276 y=2614 cloud=1383 popped=False@-1 hit=True score=88 bull=False SideWind/Normal@78 kf=79 hz=[HeavyPush:112,HeavyPush:-1,DartPop:-1]",
            [2] = "end=299 x=7463 y=2732 cloud=1305 popped=False@-1 hit=False score=0 bull=False Targeted/Normal@133 kf=51 hz=[WindGust:-1,WindGust:-1,DartPop:-1]",
            [4] = "end=277 x=8830 y=2808 cloud=8646 popped=False@-1 hit=True score=80 bull=False SideWind/Slow@145 kf=48 hz=[WindGust:-1,DartPop:-1,WindGust:-1]",
            [6] = "end=304 x=3194 y=2532 cloud=8698 popped=False@-1 hit=False score=0 bull=False TopRain/Hyper@41 kf=52 hz=[Nudge:-1,WindGust:67,WindGust:-1]",
            [12] = "end=251 x=8189 y=2794 cloud=8214 popped=False@-1 hit=True score=97 bull=False Targeted/Slow@165 kf=43 hz=[DartPop:-1,Nudge:-1,DartPop:-1]",
            [13] = "end=425 x=2631 y=2520 cloud=1323 popped=False@-1 hit=False score=0 bull=False Targeted/Hyper@21 kf=72 hz=[HeavyPush:28,Nudge:28,HeavyPush:28]",
        };
        foreach (var (seed, digest) in expected)
        {
            Assert.Equal(digest, Digest(BalloonSimulation.Run(seed, new BalloonConfig())));
        }

        var darts = new BalloonConfig { HazardCount = 25, EnableAnvils = false, EnableNudges = false, EnableWindGusts = false };
        var popped = BalloonSimulation.Run(1, darts);
        Assert.True(popped.Popped);
        Assert.False(popped.Hit);
        Assert.Equal(142, popped.EndTick);
        Assert.Equal(new BalloonKeyframe(142, 3065, 10590), popped.Keyframes[^1]);
        Assert.InRange(popped.PopTick, 1, 141);
    }

    [Fact]
    public void Same_seed_and_config_is_byte_identical_and_different_seeds_differ()
    {
        var a = BalloonLogic.DataJson(BalloonSimulation.Run(99, new BalloonConfig()), 100, 0);
        Assert.Equal(a, BalloonLogic.DataJson(BalloonSimulation.Run(99, new BalloonConfig()), 100, 0));
        Assert.NotEqual(a, BalloonLogic.DataJson(BalloonSimulation.Run(100, new BalloonConfig()), 100, 0));
    }

    [Fact]
    public void Sin_matches_the_platform_sine_and_the_reference_points()
    {
        Assert.Equal(0.0, BalloonSimulation.Sin(0), 12);
        for (var x = -20.0; x <= 20.0; x += 0.137)
        {
            Assert.Equal(Math.Sin(x), BalloonSimulation.Sin(x), 8);
        }
    }

    [Theory]
    [InlineData(0.49, 0)]
    [InlineData(0.5, 1)]
    [InlineData(98.5, 99)]
    [InlineData(99.4, 99)]
    public void Round_half_up_matches_javascript(double value, int expected) =>
        Assert.Equal(expected, BalloonSimulation.RoundHalfUp(value));

    [Fact]
    public void A_clear_sky_flight_has_no_hazards_and_only_docks_or_misses()
    {
        var docked = 0;
        var missed = 0;
        for (ulong s = 0; s < 400; s++)
        {
            var r = BalloonSimulation.Run(s * 31 + 5, new BalloonConfig { HazardCount = 0 });
            Assert.Empty(r.Hazards);
            Assert.False(r.Popped);
            Assert.False(r.TimedOut);
            Assert.Equal(0, r.WaveTick);
            Assert.Equal(r.Hit, r.Score > 0);
            Assert.True(r.Keyframes.Count >= 2);
            Assert.Equal(r.EndTick, r.Keyframes[^1].Tick);
            Assert.Equal(0, r.Keyframes[0].Tick);
            if (r.Hit)
            {
                docked++;
            }
            else
            {
                missed++;
            }
        }

        Assert.True(docked > 0 && missed > 0, $"docked {docked}, missed {missed}");
    }

    [Fact]
    public void With_every_hazard_kind_disabled_the_wave_is_empty()
    {
        var cfg = new BalloonConfig { EnableDarts = false, EnableAnvils = false, EnableNudges = false, EnableWindGusts = false };
        var r = BalloonSimulation.Run(5, cfg);
        Assert.Empty(r.Hazards);
        Assert.Empty(cfg.EnabledKinds());
    }

    [Fact]
    public void Each_hazard_kind_can_strike_and_each_pattern_occurs()
    {
        var struck = new HashSet<HazardKind>();
        var patterns = new HashSet<HazardPattern>();
        var speeds = new HashSet<string>();
        var popped = 0;
        for (ulong s = 0; s < 1500; s++)
        {
            var r = BalloonSimulation.Run(s * 977 + 3, new BalloonConfig { HazardCount = 12 });
            patterns.Add(r.Pattern);
            speeds.Add(r.SpeedLabel);
            foreach (var h in r.Hazards.Where(h => h.HitTick >= 0))
            {
                struck.Add(h.Hazard.Kind);
            }

            if (r.Popped)
            {
                popped++;
                Assert.False(r.Hit);
                Assert.Equal(0, r.Score);
                Assert.True(r.PopTick > 0 && r.PopTick <= r.EndTick);
                Assert.NotEqual(0.0, r.PopSpin);
                Assert.True(r.Keyframes[^1].Y >= 10500, "a popped avatar ends below the field");
            }
        }

        Assert.Equal(4, struck.Count);
        Assert.Equal(3, patterns.Count);
        Assert.Equal(4, speeds.Count);
        Assert.True(popped > 0);
    }

    [Fact]
    public void Wall_rebounds_clamp_the_balloon_inside_the_field()
    {
        var cfg = new BalloonConfig { HazardCount = 25, EnableDarts = false, EnableAnvils = false, EnableNudges = false };
        var wall = 0;
        for (ulong s = 0; s < 800; s++)
        {
            var r = BalloonSimulation.Run(s * 131 + 1, cfg);
            Assert.All(r.Keyframes, k => Assert.InRange(k.X, 500, 9500));
            if (r.Keyframes.Any(k => k.X is 500 or 9500))
            {
                wall++;
            }
        }

        Assert.True(wall > 0, "no flight reached a wall");
    }

    [Fact]
    public void A_stationary_cloud_never_moves_and_buoyancy_sets_the_flight_time()
    {
        var cfg = new BalloonConfig { TargetSpeed = 0, HazardCount = 0 };
        var cloud = BalloonSimulation.BuildCloud(cfg, 1.234);
        Assert.Equal(0.0, cloud.Omega);
        Assert.Equal(BalloonSimulation.CloudCenter(cloud, 0), BalloonSimulation.CloudCenter(cloud, 30), 12);
        var slow = BalloonSimulation.Run(1, cfg with { Buoyancy = 0.1 });
        var fast = BalloonSimulation.Run(1, cfg with { Buoyancy = 10 });
        Assert.True(slow.Launch.DurationSeconds > fast.Launch.DurationSeconds);
        Assert.Equal(8.5, slow.Launch.DurationSeconds);
        Assert.Equal(5.8 / Math.Sqrt(Math.Sqrt(10)), fast.Launch.DurationSeconds, 9);
        Assert.True(slow.EndTick > fast.EndTick);
    }

    [Fact]
    public void The_cloud_stays_inside_the_field_and_sweeps_around_its_base()
    {
        var cloud = BalloonSimulation.BuildCloud(new BalloonConfig(), 0);
        Assert.Equal(0.5, cloud.Base);
        Assert.Equal(0.09, cloud.HalfWidth, 12);
        Assert.Equal(0.37, cloud.Span, 12);
        for (var t = 0.0; t < 200; t += 0.5)
        {
            var c = BalloonSimulation.CloudCenter(cloud, t);
            Assert.InRange(c, cloud.HalfWidth, 1 - cloud.HalfWidth);
        }

        // An off-centre base keeps the smaller side's span, never below 0.04.
        var edge = BalloonSimulation.BuildCloud(new BalloonConfig { TargetCenterX = 0.0 }, 0);
        Assert.Equal(0.09, edge.Base, 12);
        Assert.Equal(0.04, edge.Span, 12);
    }

    [Fact]
    public void The_watchdog_ends_an_unresolved_flight_as_a_timed_out_miss()
    {
        var r = BalloonSimulation.Run(1, new BalloonConfig { HazardCount = 0 }, maxTicks: 10);
        Assert.True(r.TimedOut);
        Assert.False(r.Hit);
        Assert.Equal(10, r.EndTick);
        Assert.Equal(0, r.Score);
    }

    [Fact]
    public void A_dart_wave_aimed_at_the_balloon_pops_it_and_the_pop_ends_the_flight_scoring_zero()
    {
        var cfg = new BalloonConfig { HazardCount = 25, EnableAnvils = false, EnableNudges = false, EnableWindGusts = false };
        var pops = Enumerable.Range(0, 300).Select(i => BalloonSimulation.Run((ulong)(i * 17 + 2), cfg)).Where(r => r.Popped).ToList();
        Assert.NotEmpty(pops);
        Assert.All(pops, r =>
        {
            Assert.False(r.Hit);
            Assert.Equal(0, cfg.PayoutPercent(r.Hit, r.Bullseye, r.Score));
        });
    }

    [Fact]
    public void An_anvil_stalls_the_climb_so_the_flight_takes_longer_than_the_clear_sky_flight()
    {
        var anvils = new BalloonConfig { HazardCount = 25, EnableDarts = false, EnableNudges = false, EnableWindGusts = false };
        var clear = new BalloonConfig { HazardCount = 0 };
        var slower = 0;
        for (ulong s = 0; s < 200; s++)
        {
            var hit = BalloonSimulation.Run(s * 53 + 9, anvils);
            if (hit.Hazards.Any(h => h.HitTick >= 0))
            {
                Assert.True(hit.EndTick > BalloonSimulation.Run(s * 53 + 9, clear).EndTick - 40);
                slower++;
            }
        }

        Assert.True(slower > 0);
    }

    [Fact]
    public void Baseline_climbs_straight_with_no_rebound_when_the_shot_stays_in_the_field()
    {
        var b = BalloonBaseline.Build(0.5, 0.94, 0.10, 0.02, -0.14, 348);
        Assert.Equal(0, b.PositionAt(-5).Tick);
        Assert.Equal(348, b.PositionAt(1000).Tick);
        var mid = b.PositionAt(174);
        Assert.Equal(174, mid.Tick);
        Assert.Equal(0.5, b.PositionAt(0).X);
        Assert.Equal(0.616, b.PositionAt(348).X, 3);
        Assert.InRange(mid.Y, 0.10, 0.94);
    }

    [Theory]
    [InlineData(0.3)]
    [InlineData(-0.3)]
    public void Baseline_rebounds_once_off_the_nearer_edge(double vx)
    {
        var b = BalloonBaseline.Build(0.5, 0.94, 0.10, vx, -0.14, 348);
        var edgeX = vx > 0 ? 0.95 : 0.05;
        var seen = Enumerable.Range(0, 349).Select(t => b.PositionAt(t)).ToList();
        Assert.Contains(seen, p => Math.Abs(p.X - edgeX) < 1e-9);
        var landing = b.PositionAt(348).X;
        Assert.True(vx > 0 ? landing < 0.95 : landing > 0.05, $"landing {landing} did not come back off the edge");
    }

    [Fact]
    public void Baseline_with_zero_velocity_uses_upstreams_fallback_direction_at_an_edge()
    {
        var b = BalloonBaseline.Build(0.04, 0.94, 0.10, 0.0, -0.14, 348);
        Assert.InRange(b.PositionAt(348).X, 0.05, 0.95);
        var right = BalloonBaseline.Build(0.96, 0.94, 0.10, 0.0, -0.14, 348);
        Assert.InRange(right.PositionAt(348).X, 0.05, 0.95);
    }

    [Fact]
    public void Hazard_patterns_follow_upstreams_trajectory_rules()
    {
        var baseline = BalloonBaseline.Build(0.5, 0.94, 0.10, 0.0, -0.14, 348);
        var rng = new Xoshiro256(1);

        var rain = BalloonHazardPattern.Trajectory(HazardPattern.TopRain, HazardKind.Nudge, 0, 1.0, baseline, 348, 10, rng);
        Assert.InRange(rain.StartX, 0.08, 0.92);
        Assert.Equal(0.02, rain.StartY);
        Assert.InRange(rain.Vy, 0.12, 0.24);
        Assert.Equal(0.14, rain.Gravity, 12);

        var left = BalloonHazardPattern.Trajectory(HazardPattern.SideWind, HazardKind.Nudge, 0, 1.0, baseline, 348, 10, rng);
        var right = BalloonHazardPattern.Trajectory(HazardPattern.SideWind, HazardKind.Nudge, 1, 1.0, baseline, 348, 10, rng);
        Assert.Equal(0.02, left.StartX);
        Assert.True(left.Vx > 0);
        Assert.Equal(0.98, right.StartX);
        Assert.True(right.Vx < 0);
        Assert.Equal(0.01, left.Gravity);

        // A wind gust always streams in from the side, whatever the pattern.
        var gust = BalloonHazardPattern.Trajectory(HazardPattern.TopRain, HazardKind.WindGust, 0, 1.0, baseline, 348, 10, rng);
        Assert.Equal(0.02, gust.StartX);

        // Targeted aims a short-lived hazard at the balloon's position 20 ticks on; a balloon at 0.5 is approached from the left by both parities.
        var t0 = BalloonHazardPattern.Trajectory(HazardPattern.Targeted, HazardKind.DartPop, 0, 1.0, baseline, 348, 10, rng);
        var t1 = BalloonHazardPattern.Trajectory(HazardPattern.Targeted, HazardKind.DartPop, 1, 1.0, baseline, 348, 10, rng);
        Assert.True(t0.StartX < 0.5 && t0.Vx > 0);
        Assert.True(t1.StartX < 0.5 && t1.Vx > 0);
        Assert.Equal(0.08, t0.Gravity, 12);
        var target = baseline.PositionAt(30);
        var flightTime = 20.0 * BalloonSimulation.Dt;
        Assert.Equal(target.X, t0.StartX + t0.Vx * flightTime, 9);
        Assert.Equal(target.Y, t0.StartY + t0.Vy * flightTime, 9);

        // An even index with the balloon at or left of 0.35 aims from the right; an odd index with it at or right of 0.65 also aims from the right.
        var leftBaseline = BalloonBaseline.Build(0.2, 0.94, 0.10, 0.0, -0.14, 348);
        var fromRight = BalloonHazardPattern.Trajectory(HazardPattern.Targeted, HazardKind.DartPop, 0, 1.0, leftBaseline, 348, 10, rng);
        Assert.True(fromRight.StartX > 0.2 && fromRight.Vx < 0);
        var rightBaseline = BalloonBaseline.Build(0.8, 0.94, 0.10, 0.0, -0.14, 348);
        var alsoFromRight = BalloonHazardPattern.Trajectory(HazardPattern.Targeted, HazardKind.DartPop, 1, 1.0, rightBaseline, 348, 10, rng);
        Assert.True(alsoFromRight.StartX > 0.8 && alsoFromRight.Vx < 0);
    }

    [Fact]
    public void Hazard_radius_per_kind_is_upstreams_medium_preset()
    {
        Assert.Equal(0.038, BalloonHazardPattern.RadiusOf(HazardKind.HeavyPush));
        Assert.Equal(0.022, BalloonHazardPattern.RadiusOf(HazardKind.DartPop));
        Assert.Equal(0.045, BalloonHazardPattern.RadiusOf(HazardKind.WindGust));
        Assert.Equal(0.028, BalloonHazardPattern.RadiusOf(HazardKind.Nudge));
    }

    [Fact]
    public void The_wave_is_rounded_to_four_decimals_and_uses_only_enabled_kinds()
    {
        var baseline = BalloonBaseline.Build(0.5, 0.94, 0.10, 0.0, -0.14, 348);
        var wave = BalloonHazardPattern.Wave(10, [HazardKind.DartPop], baseline, 348, 30, new Xoshiro256(4), out var pattern, out var speed);
        Assert.Equal(10, wave.Count);
        Assert.All(wave, h =>
        {
            Assert.Equal(HazardKind.DartPop, h.Kind);
            Assert.Equal(h.X, Math.Round(h.X, 4));
            Assert.Equal(h.Vy, Math.Round(h.Vy, 4));
        });
        Assert.Contains(speed, BalloonHazardPattern.SpeedPresets.Select(p => p.Label));
        Assert.True(Enum.IsDefined(pattern));
    }

    [Fact]
    public void Config_is_clamped_and_pays_by_the_documented_mapping()
    {
        var wild = new BalloonConfig { Buoyancy = 99, TargetWidthPercent = 1, TargetCenterX = 7, TargetSpeed = -1, HazardCount = 99, HitBasePercent = 99999, PercentPerPoint = -4, BullseyePercent = 99999 }.Clamped();
        Assert.Equal(10.0, wild.Buoyancy);
        Assert.Equal(6.0, wild.TargetWidthPercent);
        Assert.Equal(1.0, wild.TargetCenterX);
        Assert.Equal(0.0, wild.TargetSpeed);
        Assert.Equal(25, wild.HazardCount);
        Assert.Equal(10000, wild.HitBasePercent);
        Assert.Equal(0, wild.PercentPerPoint);
        Assert.Equal(10000, wild.BullseyePercent);

        var cfg = new BalloonConfig();
        Assert.Equal(0, cfg.PayoutPercent(false, false, 0));
        Assert.Equal(0, cfg.PayoutPercent(false, true, 99));
        Assert.Equal(254, cfg.PayoutPercent(true, false, 1));
        Assert.Equal(650, cfg.PayoutPercent(true, false, 100));
        Assert.Equal(1000, cfg.PayoutPercent(true, true, 99));
        Assert.Equal(10000, (cfg with { HitBasePercent = 10000, PercentPerPoint = 5 }).PayoutPercent(true, false, 100));
    }

    [Fact]
    public void Return_to_player_over_many_seeds_stays_in_a_house_edge_band()
    {
        var cfg = new BalloonConfig();
        long percent = 0;
        const int n = 6000;
        for (ulong s = 0; s < n; s++)
        {
            var r = BalloonSimulation.Run((s * 7919) + 13, cfg);
            percent += cfg.PayoutPercent(r.Hit, r.Bullseye, r.Score);
        }

        var rtp = percent / 100.0 / n;
        Assert.InRange(rtp, 0.80, 1.00);
    }

    [Fact]
    public void Data_json_is_valid_for_a_timed_out_result_and_a_zero_stake()
    {
        var r = BalloonSimulation.Run(1, new BalloonConfig { HazardCount = 0 }, maxTicks: 10);
        var data = JsonDocument.Parse(BalloonLogic.DataJson(r, 0, 0)).RootElement;
        Assert.True(data.GetProperty("timedOut").GetBoolean());
        Assert.Equal(0, data.GetProperty("multiplierPct").GetInt64());
        Assert.Equal(0, data.GetProperty("wave").GetProperty("hazards").GetArrayLength());
    }
}

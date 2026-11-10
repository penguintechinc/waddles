using Xunit;

namespace WaddleBundleSuperpenguinUfoclaw.Tests;

/// <summary>The weighted draw (upstream's golden boundaries), config validation and the seeded claw-run simulation, with golden seeds.</summary>
public class UfoClawGameTests
{
    private static UfoClawPrize P(string label, decimal weight, int pct = 100) => new(label, pct, weight, "#2ec4b6");

    [Theory]
    [InlineData("0", 0)]
    [InlineData("0.249", 0)]
    [InlineData("0.25", 2)]
    [InlineData("0.999", 2)]
    public void Weighted_draw_honours_upstreams_boundaries_and_skips_zero_weights(string sample, int expected)
    {
        UfoClawPrize[] entries = [P("a", 0.25m), P("off", 0), P("b", 0.75m), P("zero", 0)];
        Assert.Equal(expected, UfoClawDraw.Select(entries, decimal.Parse(sample, System.Globalization.CultureInfo.InvariantCulture)));
    }

    [Fact]
    public void Empty_and_invalid_pools_are_rejected_loudly()
    {
        Assert.Throws<InvalidOperationException>(() => UfoClawDraw.Select([], 0));
        Assert.Throws<InvalidOperationException>(() => UfoClawDraw.Select([P("z", 0)], 0));
        Assert.Throws<ArgumentException>(() => UfoClawDraw.Select([P("neg", -1)], 0));
        Assert.Throws<ArgumentException>(() => UfoClawDraw.Select([P("big", 1_000_001)], 0));
        Assert.Throws<ArgumentOutOfRangeException>(() => UfoClawDraw.Select([P("a", 1)], 1));
        Assert.Throws<ArgumentOutOfRangeException>(() => UfoClawDraw.Select([P("a", 1)], -0.1m));
    }

    [Fact]
    public void The_default_pit_returns_91_percent_to_the_player()
    {
        var pool = UfoClawConfig.Default.Prizes;
        var total = pool.Sum(p => p.Weight);
        var returnPerMille = pool.Sum(p => p.Weight * p.MultiplierPct) / total * 10;
        Assert.Equal(1000m, total);
        Assert.Equal(910m, returnPerMille);
        Assert.All(pool, p => Assert.InRange(p.MultiplierPct, 0, UfoClawConfig.MaxMultiplierPct));
    }

    [Fact]
    public void Default_config_is_valid_and_clamping_is_idempotent()
    {
        var once = UfoClawConfig.Default.Clamped();
        Assert.Equal(20, once.SlipChancePercent);
        Assert.Equal(once, once.Clamped());
    }

    [Fact]
    public void Presentation_values_are_clamped_to_upstreams_ranges_and_non_finite_falls_back()
    {
        var c = (UfoClawConfig.Default with
        {
            SlipChancePercent = 250,
            MaxSuspenseDrops = -3,
            DurationSeconds = 1,
            RevealSeconds = 600,
            MovementSpeed = 9,
        }).Clamped();
        Assert.Equal((100, 0, 3d, 60d, 3d), (c.SlipChancePercent, c.MaxSuspenseDrops, c.DurationSeconds, c.RevealSeconds, c.MovementSpeed));
        var nan = (UfoClawConfig.Default with { DurationSeconds = double.NaN, RevealSeconds = double.PositiveInfinity, MovementSpeed = double.NaN }).Clamped();
        Assert.Equal((20d, 4d, 1d), (nan.DurationSeconds, nan.RevealSeconds, nan.MovementSpeed));
        Assert.Equal(5, (UfoClawConfig.Default with { MaxSuspenseDrops = 40 }).Clamped().MaxSuspenseDrops);
    }

    public static IEnumerable<object[]> BadPools() =>
    [
        [new UfoClawPrize[0]],
        [new[] { new UfoClawPrize("", 100, 1, "#ffffff") }],
        [new[] { new UfoClawPrize(new string('x', 301), 100, 1, "#ffffff") }],
        [new[] { new UfoClawPrize("a", 100, -1, "#ffffff") }],
        [new[] { new UfoClawPrize("a", 100, 1_000_001, "#ffffff") }],
        [new[] { new UfoClawPrize("a", -1, 1, "#ffffff") }],
        [new[] { new UfoClawPrize("a", UfoClawConfig.MaxMultiplierPct + 1, 1, "#ffffff") }],
        [new[] { new UfoClawPrize("a", 100, 1, "red") }],
        [new[] { new UfoClawPrize("a", 100, 1, "#12345g") }],
        [new[] { new UfoClawPrize("a", 100, 0, "#ffffff") }],
    ];

    [Theory]
    [MemberData(nameof(BadPools))]
    public void A_bad_prize_pool_fails_loudly(UfoClawPrize[] prizes) =>
        Assert.Throws<ArgumentException>(() => new UfoClawConfig(prizes).Clamped());

    [Fact]
    public void A_pool_over_ten_thousand_prizes_is_refused()
    {
        var prizes = Enumerable.Range(0, 10001).Select(i => P($"p{i}", 1)).ToArray();
        Assert.Throws<ArgumentException>(() => new UfoClawConfig(prizes).Clamped());
    }

    [Theory]
    [InlineData(42UL, 0, "Dud", false, "")]
    [InlineData(12345UL, 2, "Fish", true, "3,4,1")]
    [InlineData(0xDEADBEEFUL, 3, "Cookie", false, "")]
    [InlineData(99UL, 1, "Pebble", false, "")]
    public void Golden_seeds_give_exactly_this_grab(ulong seed, int winner, string label, bool slip, string feints)
    {
        var r = UfoClawSimulation.Run(seed, UfoClawConfig.Default);
        Assert.Equal(winner, r.WinningIndex);
        Assert.Equal(label, r.Winner.Label);
        Assert.Equal(slip, r.Slip);
        Assert.Equal(feints, string.Join(',', r.Feints));
        Assert.Equal(7, r.Pool.Count);
    }

    [Theory]
    [InlineData(0UL, 2, "0")]
    [InlineData(42UL, 0, "4,3,5")]
    [InlineData(7UL, 2, "3,0,4")]
    [InlineData(10UL, 4, "3")]
    public void Golden_seeds_with_a_forced_slip_give_exactly_these_decoys(ulong seed, int winner, string feints)
    {
        var r = UfoClawSimulation.Run(seed, UfoClawConfig.Default with { SlipChancePercent = 100 });
        Assert.True(r.Slip);
        Assert.Equal(winner, r.WinningIndex);
        Assert.Equal(feints, string.Join(',', r.Feints));
    }

    [Fact]
    public void The_same_seed_and_config_always_give_the_identical_result()
    {
        for (ulong seed = 0; seed < 200; seed++)
        {
            var a = UfoClawSimulation.Run(seed, UfoClawConfig.Default);
            var b = UfoClawSimulation.Run(seed, UfoClawConfig.Default);
            Assert.Equal(a.WinningIndex, b.WinningIndex);
            Assert.Equal(a.Slip, b.Slip);
            Assert.Equal(a.Feints, b.Feints);
            Assert.Equal(UfoClawLogic.DataJson(a, UfoClawConfig.Default), UfoClawLogic.DataJson(b, UfoClawConfig.Default));
        }
    }

    [Fact]
    public void Decoys_are_distinct_never_the_winner_and_at_most_the_configured_maximum()
    {
        var config = UfoClawConfig.Default with { SlipChancePercent = 100, MaxSuspenseDrops = 3 };
        var counts = new HashSet<int>();
        for (ulong seed = 0; seed < 500; seed++)
        {
            var r = UfoClawSimulation.Run(seed, config);
            Assert.True(r.Slip);
            Assert.DoesNotContain(r.WinningIndex, r.Feints);
            Assert.Equal(r.Feints.Count, r.Feints.Distinct().Count());
            Assert.InRange(r.Feints.Count, 1, 3);
            Assert.All(r.Feints, i => Assert.InRange(i, 0, r.Pool.Count - 1));
            counts.Add(r.Feints.Count);
        }

        Assert.Equal(new HashSet<int> { 1, 2, 3 }, counts);
    }

    [Fact]
    public void No_tease_no_drops_or_a_single_ball_never_slips_and_never_changes_the_winner()
    {
        var baseline = UfoClawSimulation.Run(12345, UfoClawConfig.Default with { SlipChancePercent = 100 });
        var noTease = UfoClawSimulation.Run(12345, UfoClawConfig.Default with { Tease = false, SlipChancePercent = 100 });
        var noDrops = UfoClawSimulation.Run(12345, UfoClawConfig.Default with { MaxSuspenseDrops = 0, SlipChancePercent = 100 });
        Assert.True(baseline.Slip);
        Assert.False(noTease.Slip);
        Assert.False(noDrops.Slip);
        Assert.Empty(noTease.Feints);
        Assert.Empty(noDrops.Feints);
        Assert.Equal(baseline.WinningIndex, noTease.WinningIndex);
        Assert.Equal(baseline.WinningIndex, noDrops.WinningIndex);

        var single = UfoClawSimulation.Run(5, new UfoClawConfig([P("only", 1)], SlipChancePercent: 100));
        Assert.Equal(0, single.WinningIndex);
        Assert.False(single.Slip);
        Assert.Empty(single.Feints);
    }

    [Fact]
    public void Zero_weight_prizes_are_not_in_the_pit_and_can_never_win()
    {
        var config = new UfoClawConfig([P("never", 0), P("a", 1), P("also-never", 0), P("b", 1)]);
        for (ulong seed = 0; seed < 100; seed++)
        {
            var r = UfoClawSimulation.Run(seed, config);
            Assert.Equal(["a", "b"], r.Pool.Select(p => p.Label));
            Assert.Contains(r.Winner.Label, new[] { "a", "b" });
        }
    }

    [Fact]
    public void Winner_frequencies_follow_the_weights()
    {
        var wins = new int[7];
        const int n = 20000;
        for (ulong seed = 1; seed <= n; seed++)
        {
            wins[UfoClawSimulation.Run(seed, UfoClawConfig.Default).WinningIndex]++;
        }

        // Expected 32.0%, 25.0%, 20.0%, 13.0%, 6.0%, 3.0%, 1.0% (+/- 1.5 points at n = 20000).
        decimal[] expected = [0.32m, 0.25m, 0.20m, 0.13m, 0.06m, 0.03m, 0.01m];
        for (var i = 0; i < 7; i++)
        {
            Assert.InRange(Math.Abs((decimal)wins[i] / n - expected[i]), 0m, 0.015m);
        }
    }

    [Fact]
    public void Slip_rate_follows_the_configured_chance()
    {
        var slips = 0;
        const int n = 10000;
        for (ulong seed = 1; seed <= n; seed++)
        {
            slips += UfoClawSimulation.Run(seed, UfoClawConfig.Default).Slip ? 1 : 0;
        }

        Assert.InRange(slips / (double)n, 0.17, 0.23);
    }
}

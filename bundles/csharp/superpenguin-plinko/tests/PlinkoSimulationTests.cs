using SuperPenguin.DropGameCore;
using Xunit;

namespace WaddleBundleSuperpenguinPlinko.Tests;

/// <summary>Golden, determinism, weighting and payout-math tests for the pure Plinko simulation.</summary>
public class PlinkoSimulationTests
{
    private static ulong Checksum(PlinkoResult r)
    {
        var h = 0xcbf29ce484222325UL;
        foreach (var k in r.Keyframes)
        {
            foreach (var v in new[] { k.TimeMs, k.X, k.Y })
            {
                h = (h ^ (ulong)(uint)v) * 0x100000001b3UL;
            }
        }

        return h;
    }

    [Theory]
    [InlineData(1UL, 2, 0.286158919914404, 246, 19, 8158, 4420, 9630, 0x99313c33a37f4d17UL)]
    [InlineData(42UL, 0, 0.07250883189474026, 83, 5, 2729, 893, 9630, 0xead99972c6a6e74bUL)]
    [InlineData(0xDEADBEEFUL, 0, 0.3561342618402124, 94, 3, 3070, 1413, 9625, 0x11c61825491bcc2fUL)]
    public void Golden_seeds_reproduce_the_exact_drop(ulong seed, int bucket, double startX, int frames, int hits, int lastMs, int lastX, int lastY, ulong sum)
    {
        var r = PlinkoSimulation.Run(seed, PlinkoConfig.Default);
        Assert.Equal(bucket, r.BucketIndex);
        Assert.Equal(startX, r.StartX);
        Assert.Equal(frames, r.Keyframes.Count);
        Assert.Equal(hits, r.PegHits.Count);
        Assert.Equal(new PlinkoKeyframe(lastMs, lastX, lastY), r.Keyframes[^1]);
        Assert.Equal(sum, Checksum(r));
        Assert.False(r.TimedOut);
    }

    [Fact]
    public void Same_seed_is_byte_identical_and_different_seeds_differ()
    {
        var a = PlinkoSimulation.Run(777, PlinkoConfig.Default);
        var b = PlinkoSimulation.Run(777, PlinkoConfig.Default);
        Assert.Equal(Checksum(a), Checksum(b));
        Assert.Equal(a.PegHits, b.PegHits);
        Assert.Equal(PlinkoLogic.DataJson(a, PlinkoConfig.Default, 10, 0), PlinkoLogic.DataJson(b, PlinkoConfig.Default, 10, 0));
        Assert.NotEqual(Checksum(a), Checksum(PlinkoSimulation.Run(778, PlinkoConfig.Default)));
    }

    [Fact]
    public void Path_is_well_formed_and_ends_on_the_floor_inside_the_landing_bucket()
    {
        for (ulong s = 1; s <= 200; s++)
        {
            var r = PlinkoSimulation.Run(s, PlinkoConfig.Default);
            Assert.Equal(0, r.Keyframes[0].TimeMs);
            Assert.True(r.Keyframes[0].Y < 0, "starts above the board");
            for (var i = 1; i < r.Keyframes.Count; i++)
            {
                Assert.True(r.Keyframes[i].TimeMs > r.Keyframes[i - 1].TimeMs);
            }

            var last = r.Keyframes[^1];
            Assert.InRange(last.Y, 9600, 9630);
            var slot = PlinkoConfig.Width / 5.0;
            Assert.Equal(r.BucketIndex, (int)Math.Floor(last.X / 10000.0 * PlinkoConfig.Width / slot));
            Assert.All(r.PegHits, h => Assert.InRange(h.Row, 0, 3));
        }
    }

    [Fact]
    public void Distribution_follows_the_weight_biased_release_and_physics()
    {
        var counts = new int[5];
        const int n = 6000;
        for (ulong s = 1; s <= n; s++)
        {
            counts[PlinkoSimulation.Run(SplitMix64.Mix(s), PlinkoConfig.Default).BucketIndex]++;
        }

        // Upstream weights 25/25/15/25/25: the WINNER centre slot is the rarest one, and every slot is reachable.
        Assert.All(counts, c => Assert.True(c > n / 10));
        Assert.True(counts[2] < counts[0] && counts[2] < counts[4]);
    }

    [Fact]
    public void Return_to_player_stays_close_to_one_but_below_it()
    {
        double paid = 0;
        double staked = 0;
        const int n = 6000;
        for (ulong s = 1; s <= n; s++)
        {
            var r = PlinkoSimulation.Run(SplitMix64.Mix(s), PlinkoConfig.Default);
            paid += PlinkoLogic.PayoutFor(100, r.Outcome);
            staked += 100;
        }

        var rtp = paid / staked;
        Assert.InRange(rtp, 0.80, 1.00);
    }

    [Fact]
    public void Weighted_pick_golden_and_floor_of_one()
    {
        var rng = new Xoshiro256(5);
        var picks = Enumerable.Range(0, 2000).Select(_ => PlinkoSimulation.PickWeightedBucketIndex(PlinkoConfig.DefaultBuckets, rng)).ToArray();
        Assert.Equal(5, picks.Distinct().Count());
        Assert.True(picks.Count(p => p == 2) < picks.Count(p => p == 0));

        PlinkoBucket[] zero = [new("A", 0, false, 0, "#000"), new("B", 0, true, -4, "#000")];
        var counts = new int[2];
        var rng2 = new Xoshiro256(6);
        for (var i = 0; i < 1000; i++)
        {
            counts[PlinkoSimulation.PickWeightedBucketIndex(zero, rng2)]++;
        }

        Assert.InRange(counts[0], 400, 600);
        Assert.Equal(0, PlinkoSimulation.PickWeightedBucketIndex([], new Xoshiro256(1)));
    }

    [Fact]
    public void Evaluate_follows_upstream_rules()
    {
        Assert.Equal(new BucketOutcome(0, false, "MISS", false), PlinkoSimulation.EvaluateBucketOutcome([], 3));
        PlinkoBucket[] row =
        [
            new("MISS", 50, false, 1, "#000"),
            new("SMALL", 100, true, 1, "#000"),
            new("BIG", 300, true, 1, "#000"),
        ];
        Assert.Equal(new BucketOutcome(0, false, "MISS", false), PlinkoSimulation.EvaluateBucketOutcome(row, 0));
        Assert.Equal(new BucketOutcome(100, true, "SMALL", false), PlinkoSimulation.EvaluateBucketOutcome(row, 1));
        Assert.Equal(new BucketOutcome(300, true, "BIG", true), PlinkoSimulation.EvaluateBucketOutcome(row, 2));
        Assert.Equal(new BucketOutcome(300, true, "BIG", true), PlinkoSimulation.EvaluateBucketOutcome(row, 99));
        Assert.Equal(new BucketOutcome(0, false, "MISS", false), PlinkoSimulation.EvaluateBucketOutcome(row, -5));
        Assert.True(PlinkoSimulation.EvaluateBucketOutcome(PlinkoConfig.DefaultBuckets, 2).IsBullseye);
    }

    [Fact]
    public void Config_is_clamped_like_upstream_settings_and_every_board_size_runs()
    {
        var wild = new PlinkoConfig(1, 1, 1, 1, 0, 0, []).Clamped();
        Assert.Equal((4, 4, 10, 60, 0.1, 0.1), (wild.RowCount, wild.PegRadius, wild.PuckRadius, wild.BucketHeight, wild.Gravity, wild.Bounciness));
        Assert.Same(PlinkoConfig.DefaultBuckets, wild.Buckets);
        var big = new PlinkoConfig(99, 99, 99, 999, 99, 99, PlinkoConfig.DefaultBuckets).Clamped();
        Assert.Equal((14, 30, 50, 220, 10.0, 5.0), (big.RowCount, big.PegRadius, big.PuckRadius, big.BucketHeight, big.Gravity, big.Bounciness));

        foreach (var rows in new[] { 4, 9, 14 })
        {
            var r = PlinkoSimulation.Run(11, PlinkoConfig.Default with { RowCount = rows });
            Assert.InRange(r.BucketIndex, 0, 4);
        }

        foreach (var g in new[] { 0.1, 1.0, 10.0 })
        {
            Assert.InRange(PlinkoSimulation.Run(12, PlinkoConfig.Default with { Gravity = g }).BucketIndex, 0, 4);
        }

        PlinkoBucket[] ten = [.. Enumerable.Range(0, 10).Select(i => new PlinkoBucket($"B{i}", 100, i % 2 == 0, 5, "#111"))];
        Assert.InRange(PlinkoSimulation.Run(13, PlinkoConfig.Default with { Buckets = ten }).BucketIndex, 0, 9);
    }

    [Theory]
    [InlineData(100UL, 0, false, 0UL)]
    [InlineData(100UL, 250, true, 250UL)]
    [InlineData(100UL, 0, true, 500UL)]
    [InlineData(7UL, 0, true, 35UL)]
    [InlineData(100UL, 99999, true, 10000UL)]
    [InlineData(100UL, 300, false, 0UL)]
    public void Payout_math(ulong stake, int score, bool win, ulong expected)
    {
        Assert.Equal(expected, PlinkoLogic.PayoutFor(stake, new BucketOutcome(score, win, "x", false)));
    }

    [Fact]
    public void Payout_never_exceeds_the_host_cap_for_any_bucket_score()
    {
        foreach (var score in new[] { 0, 1, 100, 10000, int.MaxValue })
        {
            Assert.True(PlinkoLogic.PayoutFor(500, new BucketOutcome(score, true, "x", false)) <= 500 * 100);
        }
    }
}

using WaddleBundleSuperpenguinRacing;
using Xunit;

namespace WaddleBundleSuperpenguinRacing.Tests;

/// <summary>Golden, determinism, DNF and invariant tests of the pure race simulation and the pot split.</summary>
public class RacingSimulationTests
{
    private static string Fingerprint(RaceOutcome o) =>
        $"order={string.Join(',', o.Order)};finish={string.Join(',', o.FinishMs)};total={o.TotalMs};kf={o.Keyframes.Sum(k => k.Count)};ev={o.Events.Count};power={string.Join(',', o.PowerMilli)}";

    [Fact]
    public void Golden_seed_1_three_racers_is_exactly_this_race()
    {
        Assert.Equal("order=1,0,2;finish=58825,57997,58857;total=58857;kf=179;ev=60;power=1024,1002,1009", Fingerprint(RacingSimulation.Run(1UL, 3, RaceConfig.Default)));
    }

    [Fact]
    public void Golden_seed_2_twelve_racers_is_exactly_this_race()
    {
        Assert.Equal("order=2,7,4,0,6,11,8,9,5,10,1,3;finish=55955,58379,55450,60517,55922,58307,56412,55454,57209,57672,58367,56449;total=60517;kf=704;ev=229;power=1032,988,980,971,1058,1014,1054,980,1049,1019,987,993", Fingerprint(RacingSimulation.Run(0xdeadbeefUL, 12, RaceConfig.Default)));
    }

    [Fact]
    public void Golden_keyframes_and_hazards_of_seed_1()
    {
        var o = RacingSimulation.Run(1UL, 3, RaceConfig.Default);
        Assert.Equal("debris=707,2730,3751,5333,7361,9595;boosts=1787,6009,9400;kf0=0:0;1000:588;2000:797;3000:1264", $"debris={string.Join(',', o.DebrisPos10)};boosts={string.Join(',', o.BoostPos10)};kf0={string.Join(';', o.Keyframes[0].Take(4).Select(k => $"{k.TimeMs}:{k.Pos10}"))}");
    }

    [Fact]
    public void The_same_seed_gives_a_byte_identical_race_and_other_seeds_differ()
    {
        var a = RacingSimulation.Run(99UL, 6, RaceConfig.Default);
        var b = RacingSimulation.Run(99UL, 6, RaceConfig.Default);
        Assert.Equal(Fingerprint(a), Fingerprint(b));
        Assert.Equal(a.Keyframes.Select(k => string.Join(';', k)), b.Keyframes.Select(k => string.Join(';', k)));
        Assert.Equal(a.Events, b.Events);
        Assert.NotEqual(Fingerprint(a), Fingerprint(RacingSimulation.Run(100UL, 6, RaceConfig.Default)));
    }

    [Fact]
    public void Fewer_than_two_cars_is_refused()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => RacingSimulation.Run(1UL, 1, RaceConfig.Default));
    }

    [Fact]
    public void Invariants_hold_across_many_seeds_and_every_mechanic_occurs()
    {
        var kinds = new HashSet<string>();
        for (ulong seed = 1; seed <= 150; seed++)
        {
            var racers = 2 + (int)(seed % 11);
            var o = RacingSimulation.Run(seed, racers, RaceConfig.Default);
            Assert.Equal(Enumerable.Range(0, racers), o.Order.OrderBy(x => x));
            Assert.All(o.Dnf, d => Assert.False(d));
            Assert.All(o.FinishMs, ms => Assert.True(ms > 0));
            for (var i = 1; i < o.Order.Count; i++)
            {
                Assert.True(o.FinishMs[o.Order[i - 1]] <= o.FinishMs[o.Order[i]]);
            }

            foreach (var lane in Enumerable.Range(0, racers))
            {
                var frames = o.Keyframes[lane];
                Assert.Equal(RaceConfig.Default.Laps * 10000, frames[^1].Pos10);
                Assert.Equal((int)o.FinishMs[lane], frames[^1].TimeMs);
                for (var i = 1; i < frames.Count; i++)
                {
                    Assert.True(frames[i].TimeMs > frames[i - 1].TimeMs);
                }
            }

            foreach (var e in o.Events)
            {
                kinds.Add(e.Kind);
            }
        }

        Assert.Contains("spin", kinds);
        Assert.Contains("boost", kinds);
    }

    [Fact]
    public void A_hard_time_limit_ranks_the_unfinished_by_distance_as_dnf()
    {
        var o = RacingSimulation.Run(5UL, 4, RaceConfig.Default with { MaxRaceMillis = 4000 });
        Assert.All(o.Dnf, d => Assert.True(d));
        Assert.All(o.FinishMs, ms => Assert.Equal(-1, ms));
        var distances = o.Order.Select(l => o.Keyframes[l][^1].Pos10).ToList();
        Assert.Equal(distances.OrderByDescending(d => d), distances);
        Assert.True(o.TotalMs >= 3950);
    }

    [Fact]
    public void A_mixed_field_ranks_finishers_before_dnf()
    {
        // 25 s: a 3-lap race (about 54 s) cannot finish, one lap (about 18 s) can.
        var o = RacingSimulation.Run(5UL, 3, RaceConfig.Default with { Laps = 1, MaxRaceMillis = 18_500 });
        var finishedFirst = o.Order.TakeWhile(l => !o.Dnf[l]).Count();
        Assert.Equal(o.Dnf.Count(d => !d), finishedFirst);
    }

    [Fact]
    public void Config_is_clamped_to_supported_ranges()
    {
        var c = (RaceConfig.Default with { Laps = 99, MinRacers = 1, MaxRacers = 99, LobbySeconds = 1 }).Clamped();
        Assert.Equal(10, c.Laps);
        Assert.Equal(2, c.MinRacers);
        Assert.Equal(30, c.MaxRacers);
        Assert.Equal(15U, c.LobbySeconds);
        var low = (RaceConfig.Default with { Laps = 0, LobbySeconds = 9999, MaxRacers = 1 }).Clamped();
        Assert.Equal(1, low.Laps);
        Assert.Equal(180U, low.LobbySeconds);
        Assert.Equal(2, low.MaxRacers);
    }

    [Theory]
    [InlineData(0, new int[0])]
    [InlineData(1, new int[0])]
    [InlineData(2, new[] { 100 })]
    [InlineData(3, new[] { 70, 30 })]
    [InlineData(4, new[] { 60, 30, 10 })]
    [InlineData(12, new[] { 60, 30, 10 })]
    public void Shares_by_field_size(int racers, int[] expected)
    {
        Assert.Equal(expected, RacingPayouts.Shares(racers));
    }

    [Theory]
    [InlineData(200UL, 2)]
    [InlineData(301UL, 3)]
    [InlineData(1UL, 4)]
    [InlineData(999UL, 5)]
    [InlineData(6000UL, 12)]
    public void The_split_pays_out_exactly_the_pot_and_rounding_dust_goes_to_first(ulong pot, int racers)
    {
        var payouts = RacingPayouts.Split(pot, racers);
        Assert.Equal(pot, payouts.Aggregate(0UL, (a, b) => a + b));
        Assert.True(payouts[0] >= payouts.Skip(1).DefaultIfEmpty(0UL).Max());
    }

    [Fact]
    public void Split_for_a_single_car_pays_nothing()
    {
        Assert.Equal(new ulong[] { 0 }, RacingPayouts.Split(500, 1));
        Assert.Equal(new ulong[] { 60, 30, 10, 0 }, RacingPayouts.Split(100, 4));
        Assert.Equal(new ulong[] { 70, 30, 0 }, RacingPayouts.Split(100, 3));
    }
}

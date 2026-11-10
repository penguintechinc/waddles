using System.Security.Cryptography;
using System.Text;
using Xunit;

namespace WaddleBundleSuperpenguinGiveaway.Tests;

public class Sha256Tests
{
    [Fact]
    public void matches_the_platform_implementation_across_every_padding_boundary()
    {
        for (var length = 0; length <= 200; length++)
        {
            var data = new byte[length];
            for (var i = 0; i < length; i++)
            {
                data[i] = (byte)(i * 31 + length);
            }

            Assert.Equal(SHA256.HashData(data), Sha256.Hash(data));
        }
    }

    [Theory]
    [InlineData("", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")]
    [InlineData("abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")]
    [InlineData("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq", "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1")]
    public void matches_the_fips_test_vectors(string text, string expected) =>
        Assert.Equal(expected, Sha256.Hex(Sha256.Hash(text)));

    [Fact]
    public void a_million_a_matches_the_fips_vector()
    {
        var data = Encoding.ASCII.GetBytes(new string('a', 1_000_000));
        Assert.Equal("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", Sha256.Hex(Sha256.Hash(data)));
    }

    [Fact]
    public void hex_round_trips_and_rejects_malformed_input()
    {
        var bytes = Sha256.Hash("round trip");
        Assert.Equal(bytes, Sha256.FromHex(Sha256.Hex(bytes)));
        Assert.Equal(bytes, Sha256.FromHex(Sha256.Hex(bytes).ToUpperInvariant()));
        Assert.Null(Sha256.FromHex("abc"));
        Assert.Null(Sha256.FromHex("zz"));
        Assert.Null(Sha256.FromHex("0g"));
        Assert.Null(Sha256.FromHex("g0"));
        Assert.Empty(Sha256.FromHex("")!);
    }
}

public class FairnessTests
{
    private static readonly List<Ticket> Pool = Fairness.Sorted([new Ticket("c", 60), new Ticket("a", 10), new Ticket("b", 30)]);

    private static byte[] Seed(int n) => Sha256.Hash("seed-" + n);

    [Fact]
    public void the_pool_is_sorted_by_uuid_and_totalled()
    {
        Assert.Equal(["a", "b", "c"], Pool.Select(t => t.Actor));
        Assert.Equal(100, Fairness.TotalTickets(Pool));
    }

    [Fact]
    public void the_fingerprint_is_deterministic_and_binds_round_and_tickets()
    {
        var fp = Fairness.Fingerprint(1, Pool);

        Assert.Equal(fp, Fairness.Fingerprint(1, Pool));
        Assert.NotEqual(fp, Fairness.Fingerprint(2, Pool));
        Assert.NotEqual(fp, Fairness.Fingerprint(1, Fairness.Sorted([new Ticket("a", 11), new Ticket("b", 30), new Ticket("c", 60)])));
        Assert.Equal(Sha256.Hex(SHA256.HashData(Encoding.UTF8.GetBytes("tgw1\n1\na:10\nb:30\nc:60"))), fp);
    }

    [Fact]
    public void a_commitment_opens_only_with_the_real_seed_and_fingerprint()
    {
        var seed = Seed(1);
        var fp = Fairness.Fingerprint(1, Pool);
        var commit = Fairness.Commitment(seed, fp);

        Assert.True(Fairness.VerifyCommitment(Sha256.Hex(seed), fp, commit));
        Assert.False(Fairness.VerifyCommitment(Sha256.Hex(Seed(2)), fp, commit));
        Assert.False(Fairness.VerifyCommitment(Sha256.Hex(seed), Fairness.Fingerprint(2, Pool), commit));
        Assert.False(Fairness.VerifyCommitment("not hex", fp, commit));
        var tampered = Sha256.Hex(seed);
        tampered = (tampered[0] == '0' ? "1" : "0") + tampered[1..];
        Assert.False(Fairness.VerifyCommitment(tampered, fp, commit));
        Assert.Equal(Sha256.Hex(SHA256.HashData([.. seed, .. Encoding.ASCII.GetBytes(fp)])), commit);
    }

    [Fact]
    public void seeds_are_32_bytes_and_depend_on_every_input()
    {
        void Fill(byte[] b) => Array.Fill(b, (byte)9);
        var a = Fairness.NewSeed(1, "m1", 1, Fill);

        Assert.Equal(32, a.Length);
        Assert.Equal(a, Fairness.NewSeed(1, "m1", 1, Fill));
        Assert.NotEqual(a, Fairness.NewSeed(2, "m1", 1, Fill));
        Assert.NotEqual(a, Fairness.NewSeed(1, "m2", 1, Fill));
        Assert.NotEqual(a, Fairness.NewSeed(1, "m1", 2, Fill));
        Assert.Equal(32, Fairness.NewSeed(1, "m1", 1).Length);
        Assert.NotEqual(Fairness.NewSeed(1, "m1", 1), Fairness.NewSeed(1, "m1", 1));
    }

    [Fact]
    public void the_derived_index_is_deterministic_in_range_and_rejects_an_empty_pool()
    {
        var seed = Seed(1);
        var index = Fairness.DeriveIndex(seed, "fp", 1, 100);

        Assert.Equal(index, Fairness.DeriveIndex(seed, "fp", 1, 100));
        Assert.InRange(index, 0UL, 99UL);
        Assert.Equal(0UL, Fairness.DeriveIndex(seed, "fp", 1, 1));
        Assert.Throws<ArgumentOutOfRangeException>(() => Fairness.DeriveIndex(seed, "fp", 1, 0));
    }

    [Fact]
    public void a_huge_pool_exercises_rejection_sampling_and_stays_in_range()
    {
        const ulong total = 0x9000000000000001UL;
        var rejected = 0;
        for (var i = 0; i < 200; i++)
        {
            Assert.True(Fairness.DeriveIndex(Seed(i), "fp", 1, total) < total);
            rejected++;
        }

        Assert.Equal(200, rejected);
    }

    [Fact]
    public void the_derived_index_is_statistically_uniform()
    {
        const int n = 7;
        const int draws = 7000;
        var counts = new int[n];
        for (var i = 0; i < draws; i++)
        {
            counts[Fairness.DeriveIndex(Seed(i), "fp", 1, n)]++;
        }

        foreach (var count in counts)
        {
            Assert.InRange(count, 1000 - 150, 1000 + 150);
        }
    }

    [Theory]
    [InlineData(0UL, "a", 0, 9, 100)]
    [InlineData(9UL, "a", 0, 9, 100)]
    [InlineData(10UL, "b", 10, 39, 100)]
    [InlineData(39UL, "b", 10, 39, 100)]
    [InlineData(40UL, "c", 40, 99, 100)]
    [InlineData(99UL, "c", 40, 99, 100)]
    public void a_ticket_index_resolves_to_the_holder_of_that_range(ulong index, string actor, long start, long end, long total)
    {
        var result = Fairness.Locate(Pool, index);

        Assert.Equal((actor, start, end, total), (result.Actor, result.RangeStart, result.RangeEnd, result.Total));
    }

    [Fact]
    public void the_same_seed_and_pool_always_draw_the_same_winner_and_wins_follow_ticket_share()
    {
        var first = Fairness.Draw(Seed(5), "fp", 1, Pool);
        Assert.Equal(first, Fairness.Draw(Seed(5), "fp", 1, Pool));

        var wins = new Dictionary<string, int> { ["a"] = 0, ["b"] = 0, ["c"] = 0 };
        for (var i = 0; i < 3000; i++)
        {
            wins[Fairness.Draw(Seed(i), "fp", 1, Pool).Actor]++;
        }

        Assert.InRange(wins["a"] / 3000.0, 0.06, 0.14);
        Assert.InRange(wins["b"] / 3000.0, 0.26, 0.34);
        Assert.InRange(wins["c"] / 3000.0, 0.56, 0.64);
    }

    [Fact]
    public void picks_without_replacement_are_distinct_deterministic_and_bounded()
    {
        List<string> entrants = ["a", "b", "c", "d", "e"];

        var picks = Fairness.PickWithoutReplacement(Seed(1), "fp", entrants, 3);

        Assert.Equal(3, picks.Count);
        Assert.Equal(3, picks.Distinct().Count());
        Assert.Equal(picks, Fairness.PickWithoutReplacement(Seed(1), "fp", entrants, 3));
        Assert.Equal(5, Fairness.PickWithoutReplacement(Seed(1), "fp", entrants, 9).Distinct().Count());
        Assert.Empty(Fairness.PickWithoutReplacement(Seed(1), "fp", [], 3));
    }

    [Fact]
    public void the_monte_carlo_report_tracks_expected_shares_and_is_deterministic()
    {
        var a = Fairness.MonteCarlo(Pool, 100_000, 42);

        Assert.Equal(a, Fairness.MonteCarlo(Pool, 100_000, 42));
        Assert.Equal((100_000, 3, 100L), (a.Iterations, a.Entrants, a.TotalTickets));
        Assert.True(a.MaxAbsDeltaPercent < 1.0, $"max delta {a.MaxAbsDeltaPercent}");
        Assert.NotEqual(a.MaxAbsDeltaPercent, Fairness.MonteCarlo(Pool, 100_000, 43).MaxAbsDeltaPercent);
        Assert.Throws<ArgumentException>(() => Fairness.MonteCarlo([], 1000, 1));
    }
}

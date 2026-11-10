using Xunit;

namespace SuperPenguin.DropGameCore.Tests;

/// <summary>
/// Golden tests for the PRNG and the seed derivation. The expected values were produced by an independent Python implementation of
/// the published algorithms (splitmix64 seed 0 -> 0xE220A8397B1DCDAF is the well-known reference vector), not by this code.
/// </summary>
public class PrngTests
{
    [Fact]
    public void SplitMix64_matches_the_reference_vector()
    {
        ulong state = 0;
        Assert.Equal(0xE220A8397B1DCDAFUL, SplitMix64.Next(ref state));
        Assert.Equal(0x6E789E6AA1B965F4UL, SplitMix64.Next(ref state));
        Assert.Equal(0x06C45D188009454FUL, SplitMix64.Next(ref state));
        Assert.Equal(0xE220A8397B1DCDAFUL, SplitMix64.Mix(0));
    }

    [Theory]
    [InlineData(0UL, new[] { 0x99ec5f36cb75f2b4UL, 0xbf6e1f784956452aUL, 0x1a5f849d4933e6e0UL, 0x6aa594f1262d2d2cUL })]
    [InlineData(0xDEADBEEFUL, new[] { 0xc5555444a74d7e83UL, 0x65c30d37b4b16e38UL, 0x54f773200a4efa23UL, 0x429aed75fb958af7UL })]
    [InlineData(ulong.MaxValue, new[] { 0x8f5520d52a7ead08UL, 0xc476a018caa1802dUL, 0x81de31c0d260469eUL, 0xbf658d7e065f3c2fUL })]
    public void Xoshiro_golden_outputs(ulong seed, ulong[] expected)
    {
        var rng = new Xoshiro256(seed);
        foreach (var e in expected)
        {
            Assert.Equal(e, rng.NextULong());
        }
    }

    [Fact]
    public void Same_seed_same_stream_different_seed_different_stream()
    {
        var a = new Xoshiro256(99);
        var b = new Xoshiro256(99);
        var c = new Xoshiro256(100);
        var sa = Enumerable.Range(0, 50).Select(_ => a.NextULong()).ToArray();
        Assert.Equal(sa, Enumerable.Range(0, 50).Select(_ => b.NextULong()).ToArray());
        Assert.NotEqual(sa, Enumerable.Range(0, 50).Select(_ => c.NextULong()).ToArray());
    }

    [Fact]
    public void NextDouble_golden_and_in_unit_interval()
    {
        var rng = new Xoshiro256(42);
        Assert.Equal(0.08386297105988216, rng.NextDouble());
        Assert.Equal(0.3789802506626686, rng.NextDouble());
        Assert.Equal(0.6800434110281394, rng.NextDouble());
        for (var i = 0; i < 1000; i++)
        {
            var d = rng.NextDouble();
            Assert.InRange(d, 0.0, 0.9999999999999999);
        }
    }

    [Fact]
    public void NextDouble_range_scales_and_offsets()
    {
        var rng = new Xoshiro256(5);
        for (var i = 0; i < 500; i++)
        {
            Assert.InRange(rng.NextDouble(-0.003, 0.003), -0.003, 0.003);
        }
    }

    [Fact]
    public void NextInt_golden_sequences()
    {
        var a = new Xoshiro256(42);
        Assert.Equal(new[] { 1, 1, 6, 6, 5, 1, 5, 4, 5, 6 }, Enumerable.Range(0, 10).Select(_ => a.NextInt(1, 6)).ToArray());
        var b = new Xoshiro256(7);
        Assert.Equal(new[] { -3, 3, -2, -2, 3, 2, -2, -1, 0, 2 }, Enumerable.Range(0, 10).Select(_ => b.NextInt(-3, 3)).ToArray());
    }

    [Fact]
    public void NextInt_single_value_range_and_full_int_range()
    {
        var rng = new Xoshiro256(1);
        Assert.Equal(7, rng.NextInt(7, 7));
        var v = rng.NextInt(int.MinValue, int.MaxValue);
        Assert.InRange(v, int.MinValue, int.MaxValue);
    }

    [Fact]
    public void NextInt_rejects_an_empty_range()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new Xoshiro256(1).NextInt(5, 4));
    }

    [Fact]
    public void NextInt_is_roughly_uniform()
    {
        var rng = new Xoshiro256(2026);
        var counts = new int[6];
        for (var i = 0; i < 60_000; i++)
        {
            counts[rng.NextInt(0, 5)]++;
        }

        Assert.All(counts, c => Assert.InRange(c, 9_000, 11_000));
    }

    [Fact]
    public void NextBool_golden()
    {
        var rng = new Xoshiro256(7);
        Assert.Equal(new[] { true, false, true, true, true, true, false, false, false, false }, Enumerable.Range(0, 10).Select(_ => rng.NextBool()).ToArray());
    }

    [Fact]
    public void Derive_golden_and_identity_sensitive()
    {
        const ulong entropy = 0x0123456789ABCDEFUL;
        Assert.Equal(0x7ad6e2a458754409UL, SeedDerivation.Derive("msg-1", "twitch", "actor-1", "plinko", entropy));
        Assert.Equal(0xc24b85bbcebba109UL, SeedDerivation.Derive("msg-2", "twitch", "actor-1", "plinko", entropy));
        var baseSeed = SeedDerivation.Derive("m", "p", "a", "c", 1);
        Assert.NotEqual(baseSeed, SeedDerivation.Derive("m", "p", "a", "c", 2));
        Assert.NotEqual(baseSeed, SeedDerivation.Derive("m", "p", "a2", "c", 1));
        Assert.NotEqual(baseSeed, SeedDerivation.Derive("m", "p", "a", "c2", 1));
        Assert.NotEqual(baseSeed, SeedDerivation.Derive("m", "p2", "a", "c", 1));
    }

    [Fact]
    public void Derive_length_prefixes_fields_so_boundaries_cannot_collide()
    {
        Assert.NotEqual(SeedDerivation.Derive("ab", "c", "x", "y", 0), SeedDerivation.Derive("a", "bc", "x", "y", 0));
    }

    [Fact]
    public void NewEntropy_packs_four_16_bit_draws_big_endian()
    {
        var queue = new Queue<int>([0x0123, 0x4567, 0x89AB, 0xCDEF]);
        Assert.Equal(0x0123456789ABCDEFUL, SeedDerivation.NewEntropy((_, _) => queue.Dequeue()));
    }

    [Fact]
    public void NewEntropy_uses_the_host_rng_by_default()
    {
        var a = SeedDerivation.NewEntropy();
        var b = SeedDerivation.NewEntropy();
        Assert.NotEqual(a, b);
    }

    [Fact]
    public void Hex_round_trips_and_rejects_bad_input()
    {
        Assert.Equal("00000000000000ff", SeedDerivation.ToHex(255));
        Assert.True(SeedDerivation.TryParseHex("deadbeefcafef00d", out var seed));
        Assert.Equal(0xDEADBEEFCAFEF00DUL, seed);
        Assert.False(SeedDerivation.TryParseHex("zz", out _));
        Assert.False(SeedDerivation.TryParseHex(null, out _));
        Assert.False(SeedDerivation.TryParseHex("xxxxxxxxxxxxxxxx", out _));
        Assert.False(SeedDerivation.TryParseHex("-eadbeefcafef00d", out _));
    }
}

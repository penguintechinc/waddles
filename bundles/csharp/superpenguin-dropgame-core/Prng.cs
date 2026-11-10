using System.Numerics;

namespace SuperPenguin.DropGameCore;

/// <summary>
/// splitmix64: the 64-bit mixer used both to expand one seed into the four xoshiro256** state words and as the final avalanche of
/// <see cref="SeedDerivation"/>. Pure integer arithmetic, so the same input gives the same output on every platform.
/// </summary>
public static class SplitMix64
{
    /// <summary>Advances <paramref name="state"/> by the splitmix64 gamma and returns the next mixed 64-bit output.</summary>
    public static ulong Next(ref ulong state)
    {
        state += 0x9E3779B97F4A7C15UL;
        var z = state;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
        return z ^ (z >> 31);
    }

    /// <summary>The stateless mix of one value (one <see cref="Next"/> step from <paramref name="value"/>).</summary>
    public static ulong Mix(ulong value) => Next(ref value);
}

/// <summary>
/// xoshiro256** (Blackman/Vigna): a small, fast, statistically strong generator whose whole state is four 64-bit words, seeded
/// from one 64-bit value through <see cref="SplitMix64"/>. Every drop game draws ALL of its randomness from one instance made
/// from the recorded seed, so a result is reproducible byte-for-byte by anyone holding the seed and the game config.
/// </summary>
public sealed class Xoshiro256
{
    private ulong _s0;
    private ulong _s1;
    private ulong _s2;
    private ulong _s3;

    /// <summary>Seeds the four state words with four consecutive splitmix64 outputs of <paramref name="seed"/> (never all zero).</summary>
    public Xoshiro256(ulong seed)
    {
        var sm = seed;
        _s0 = SplitMix64.Next(ref sm);
        _s1 = SplitMix64.Next(ref sm);
        _s2 = SplitMix64.Next(ref sm);
        _s3 = SplitMix64.Next(ref sm);
    }

    /// <summary>The next raw 64-bit output (the `**` scrambler: rotl(s1 * 5, 7) * 9).</summary>
    public ulong NextULong()
    {
        var result = BitOperations.RotateLeft(_s1 * 5, 7) * 9;
        var t = _s1 << 17;
        _s2 ^= _s0;
        _s3 ^= _s1;
        _s1 ^= _s2;
        _s0 ^= _s3;
        _s2 ^= t;
        _s3 = BitOperations.RotateLeft(_s3, 45);
        return result;
    }

    /// <summary>A double uniformly distributed in [0, 1) with 53 bits of precision.</summary>
    public double NextDouble() => (NextULong() >> 11) * (1.0 / 9007199254740992.0);

    /// <summary>A double uniformly distributed in [<paramref name="min"/>, <paramref name="max"/>).</summary>
    public double NextDouble(double min, double max) => min + ((max - min) * NextDouble());

    /// <summary>True with probability one half.</summary>
    public bool NextBool() => (NextULong() >> 63) == 1;

    /// <summary>
    /// An integer uniformly distributed in the inclusive range [<paramref name="minInclusive"/>, <paramref name="maxInclusive"/>],
    /// drawn by rejection sampling so there is no modulo bias. Throws when the range is empty.
    /// </summary>
    public int NextInt(int minInclusive, int maxInclusive)
    {
        if (maxInclusive < minInclusive)
        {
            throw new ArgumentOutOfRangeException(nameof(maxInclusive), maxInclusive, $"must be >= minInclusive ({minInclusive})");
        }

        var range = (ulong)((long)maxInclusive - minInclusive + 1);
        var threshold = (0UL - range) % range;
        ulong draw;
        do
        {
            draw = NextULong();
        }
        while (draw < threshold);

        return (int)(minInclusive + (long)(draw % range));
    }
}

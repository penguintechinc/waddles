using System.Text;

namespace WaddleBundleSuperpenguinGiveaway;

/// <summary>One entrant of a frozen pool: the community user uuid and the tickets they hold.</summary>
public sealed record Ticket(string Actor, long Tickets);

/// <summary>The outcome of one provable draw: the winning ticket index, the entrant holding it and that entrant's ticket range.</summary>
public sealed record DrawResult(ulong TicketIndex, string Actor, long RangeStart, long RangeEnd, long Total);

/// <summary>Summary of a Monte Carlo run of the draw algorithm against a pool.</summary>
public sealed record FairnessSummary(int Iterations, int Entrants, long TotalTickets, double MaxAbsDeltaPercent);

/// <summary>
/// The provably-fair draw protocol (version <see cref="Version"/>), ported from PenguinTwitchBot's `GiveawayFeature` weighted draw,
/// `BuildWeightedPoolFingerprint` and `GenerateMonteCarloFairnessReport`, made verifiable.
///
/// <para>1. At close the pool is frozen into <c>fingerprint = SHA-256("tgw1\n{round}\n" + sorted "uuid:tickets" lines joined by "\n")</c>.
/// 2. A secret 32-byte seed is generated and its <c>commitment = SHA-256(seed || ascii(fingerprint))</c> is published BEFORE the draw.
/// 3. At draw the seed is revealed; the winning ticket is <c>index = first accepted (u64 mod total)</c> of
/// <c>SHA-256(seed || ascii(fingerprint) || BE32(draw) || BE32(counter))</c> (draw numbers are 1-based) for counter = 0,1,2... with rejection sampling (values
/// above the largest multiple of <c>total</c> are skipped, so there is no modulo bias). 4. The index selects the entrant whose
/// cumulative ticket range (entrants sorted by uuid) contains it. Anyone with the published values and a SHA-256 tool can
/// re-derive the index; an entrant can check their own ticket count and that the winning range is consistent with the total.</para>
/// </summary>
public static class Fairness
{
    /// <summary>Protocol label baked into every fingerprint so a future change cannot collide with this one.</summary>
    public const string Version = "tgw1";

    /// <summary>Entrants ordered by uuid (ordinal): the canonical order for fingerprints and ticket ranges.</summary>
    public static List<Ticket> Sorted(IEnumerable<Ticket> entries) =>
        entries.OrderBy(e => e.Actor, StringComparer.Ordinal).ToList();

    /// <summary>Total tickets across <paramref name="sorted"/>.</summary>
    public static long TotalTickets(IReadOnlyList<Ticket> sorted) => sorted.Sum(e => e.Tickets);

    /// <summary>Hex SHA-256 of the canonical pool text (the entrants must already be <see cref="Sorted"/>).</summary>
    public static string Fingerprint(long round, IReadOnlyList<Ticket> sorted) =>
        Sha256.Hex(Sha256.Hash(Version + "\n" + round + "\n" + string.Join("\n", sorted.Select(e => e.Actor + ":" + e.Tickets))));

    /// <summary>A fresh 32-byte seed from 32 random bytes mixed with the clock, message id and round through SHA-256.</summary>
    public static byte[] NewSeed(ulong clockMillis, string messageId, long round, Action<byte[]>? fill = null)
    {
        var random = new byte[32];
        (fill ?? System.Random.Shared.NextBytes)(random);
        return Sha256.Hash(Concat(random, Encoding.UTF8.GetBytes("|" + clockMillis + "|" + messageId + "|" + round)));
    }

    /// <summary>The published commitment: hex SHA-256 of the seed bytes followed by the ASCII fingerprint.</summary>
    public static string Commitment(byte[] seed, string fingerprint) =>
        Sha256.Hex(Sha256.Hash(Concat(seed, Encoding.ASCII.GetBytes(fingerprint))));

    /// <summary>True when <paramref name="seedHex"/> opens <paramref name="commitment"/> for <paramref name="fingerprint"/>.</summary>
    public static bool VerifyCommitment(string seedHex, string fingerprint, string commitment)
    {
        var seed = Sha256.FromHex(seedHex);
        return seed is not null && string.Equals(Commitment(seed, fingerprint), commitment, StringComparison.Ordinal);
    }

    /// <summary>The unbiased index in [0, <paramref name="total"/>) for draw number <paramref name="draw"/> (see the class remarks).</summary>
    public static ulong DeriveIndex(byte[] seed, string fingerprint, uint draw, ulong total)
    {
        if (total == 0)
        {
            throw new ArgumentOutOfRangeException(nameof(total), "the pool must hold at least one ticket");
        }

        var prefix = Concat(seed, Encoding.ASCII.GetBytes(fingerprint), BigEndian(draw));
        var zone = ulong.MaxValue - (ulong.MaxValue % total + 1) % total;
        for (uint counter = 0; ; counter++)
        {
            var digest = Sha256.Hash(Concat(prefix, BigEndian(counter)));
            ulong value = 0;
            for (var i = 0; i < 8; i++)
            {
                value = value << 8 | digest[i];
            }

            if (value <= zone)
            {
                return value % total;
            }
        }
    }

    /// <summary>Resolves a ticket index to its holder by binary search over cumulative ticket counts (entrants sorted).</summary>
    public static DrawResult Locate(IReadOnlyList<Ticket> sorted, ulong ticketIndex)
    {
        var cumulative = Cumulative(sorted);
        var entry = FirstGreater(cumulative, (long)ticketIndex);
        var start = entry == 0 ? 0 : cumulative[entry - 1];
        return new DrawResult(ticketIndex, sorted[entry].Actor, start, cumulative[entry] - 1, cumulative[^1]);
    }

    /// <summary>Draw number <paramref name="draw"/> over the (sorted, eligible) pool.</summary>
    public static DrawResult Draw(byte[] seed, string fingerprint, uint draw, IReadOnlyList<Ticket> sorted) =>
        Locate(sorted, DeriveIndex(seed, fingerprint, draw, (ulong)TotalTickets(sorted)));

    /// <summary>Picks up to <paramref name="winners"/> distinct entrants without replacement, one provable index per pick.</summary>
    public static List<string> PickWithoutReplacement(byte[] seed, string fingerprint, IReadOnlyList<string> sortedEntrants, int winners)
    {
        var remaining = sortedEntrants.ToList();
        List<string> picked = [];
        for (uint pick = 1; pick <= winners && remaining.Count > 0; pick++)
        {
            var index = (int)DeriveIndex(seed, fingerprint, pick, (ulong)remaining.Count);
            picked.Add(remaining[index]);
            remaining.RemoveAt(index);
        }

        return picked;
    }

    /// <summary>
    /// Runs the weighted draw <paramref name="iterations"/> times with a deterministic SplitMix64 stream (same seed, same report) and
    /// compares each entrant's observed share with their expected share; ported from upstream's Monte Carlo fairness report.
    /// </summary>
    public static FairnessSummary MonteCarlo(IReadOnlyList<Ticket> sorted, int iterations, ulong seed)
    {
        if (sorted.Count == 0)
        {
            throw new ArgumentException("cannot run a fairness report without eligible entries", nameof(sorted));
        }

        var cumulative = Cumulative(sorted);
        var total = (ulong)cumulative[^1];
        var hits = new long[sorted.Count];
        var state = seed;
        var zone = ulong.MaxValue - (ulong.MaxValue % total + 1) % total;
        for (var i = 0; i < iterations; i++)
        {
            ulong value;
            do
            {
                value = SplitMix64(ref state);
            }
            while (value > zone);

            hits[FirstGreater(cumulative, (long)(value % total))]++;
        }

        var maxDelta = 0.0;
        for (var i = 0; i < sorted.Count; i++)
        {
            var expected = (double)sorted[i].Tickets / total * 100.0;
            var observed = (double)hits[i] / iterations * 100.0;
            maxDelta = Math.Max(maxDelta, Math.Abs(expected - observed));
        }

        return new FairnessSummary(iterations, sorted.Count, (long)total, maxDelta);
    }

    private static long[] Cumulative(IReadOnlyList<Ticket> sorted)
    {
        var cumulative = new long[sorted.Count];
        long running = 0;
        for (var i = 0; i < sorted.Count; i++)
        {
            running += sorted[i].Tickets;
            cumulative[i] = running;
        }

        return cumulative;
    }

    private static int FirstGreater(long[] cumulative, long ticketIndex)
    {
        int lo = 0, hi = cumulative.Length - 1;
        while (lo < hi)
        {
            var mid = (lo + hi) / 2;
            if (cumulative[mid] > ticketIndex)
            {
                hi = mid;
            }
            else
            {
                lo = mid + 1;
            }
        }

        return lo;
    }

    private static ulong SplitMix64(ref ulong state)
    {
        state += 0x9E3779B97F4A7C15UL;
        var z = state;
        z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9UL;
        z = (z ^ (z >> 27)) * 0x94D049BB133111EBUL;
        return z ^ (z >> 31);
    }

    private static byte[] BigEndian(uint value) => [(byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value];

    private static byte[] Concat(params byte[][] parts)
    {
        var result = new byte[parts.Sum(p => p.Length)];
        var offset = 0;
        foreach (var part in parts)
        {
            part.CopyTo(result, offset);
            offset += part.Length;
        }

        return result;
    }
}

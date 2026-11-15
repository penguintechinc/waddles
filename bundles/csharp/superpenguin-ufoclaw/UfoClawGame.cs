using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinUfoclaw;

/// <summary>
/// One ball in the claw's pit: the upstream `UfoClawEntry` reduced to what a wagered grab needs. <see cref="Label"/> is the
/// on-stream prize name, <see cref="Weight"/> the relative draw weight (0 = never drawn, like upstream's disabled/zero-weight
/// entries), <see cref="MultiplierPct"/> the payout as a percent of the stake (0 = a dud that pays nothing, 150 = x1.5) and
/// <see cref="Color"/> the ball's base colour (`#rrggbb`, as upstream validates it).
/// </summary>
public sealed record UfoClawPrize(string Label, int MultiplierPct, decimal Weight, string Color);

/// <summary>
/// The game config a seed must be replayed under (recorded as <see cref="UfoClawLogic.ConfigId"/>): the prize pool plus the
/// upstream presentation rules that shape the claw run (suspense drops, durations, speed). The numeric ranges are exactly
/// upstream's `UfoClawStore.ValidateSettings` ranges, enforced by <see cref="Clamped"/>.
/// </summary>
public sealed record UfoClawConfig(
    IReadOnlyList<UfoClawPrize> Prizes,
    bool Tease = true,
    int SlipChancePercent = 20,
    int MaxSuspenseDrops = 3,
    double DurationSeconds = 20,
    double RevealSeconds = 4,
    double MovementSpeed = 1)
{
    /// <summary>The host ledger refuses a payout above 100x the stake; 10000 percent is that ceiling.</summary>
    public const int MaxMultiplierPct = 10000;

    /// <summary>
    /// The default pit: seven balls whose weights (per mille) and multipliers give a 91% return to player. Upstream ships NO default
    /// prize set (streamers author their own) and no payouts, so this table is this port's own design.
    /// </summary>
    public static UfoClawConfig Default { get; } = new(
    [
        new("Dud", 0, 320, "#7a7f87"),
        new("Pebble", 50, 250, "#2ec4b6"),
        new("Fish", 100, 200, "#3a86ff"),
        new("Cookie", 150, 130, "#ff9f1c"),
        new("Gem", 200, 60, "#9b5de5"),
        new("Crown", 400, 30, "#f15bb5"),
        new("Golden Penguin", 1500, 10, "#ffd60a"),
    ]);

    /// <summary>
    /// Validates the config with upstream's rules and returns it normalised: out-of-range presentation values are clamped,
    /// while a prize with a bad weight/colour/multiplier or a pool with no drawable prize throws (a broken pool must fail loudly,
    /// never silently pay something else).
    /// </summary>
    public UfoClawConfig Clamped()
    {
        if (Prizes.Count is < 1 or > 10000)
        {
            throw new ArgumentException("the prize pool needs between 1 and 10,000 prizes");
        }

        foreach (var p in Prizes)
        {
            if (string.IsNullOrWhiteSpace(p.Label) || p.Label.Length > 300)
            {
                throw new ArgumentException("every prize needs a label of at most 300 characters");
            }

            if (p.Weight is < 0 or > 1_000_000)
            {
                throw new ArgumentException("prize weights must be between 0 and 1,000,000");
            }

            if (p.MultiplierPct is < 0 or > MaxMultiplierPct)
            {
                throw new ArgumentException($"prize multipliers must be between 0 and {MaxMultiplierPct} percent");
            }

            if (!IsHexColor(p.Color))
            {
                throw new ArgumentException("every prize needs a six-digit hex colour");
            }
        }

        if (!Prizes.Any(p => p.Weight > 0))
        {
            throw new ArgumentException("no eligible prizes: at least one prize needs a weight above zero");
        }

        return this with
        {
            SlipChancePercent = Math.Clamp(SlipChancePercent, 0, 100),
            MaxSuspenseDrops = Math.Clamp(MaxSuspenseDrops, 0, 5),
            DurationSeconds = Finite(DurationSeconds, 3, 120, 20),
            RevealSeconds = Finite(RevealSeconds, 1, 60, 4),
            MovementSpeed = Finite(MovementSpeed, 0.25, 3, 1),
        };
    }

    private static double Finite(double value, double min, double max, double fallback) =>
        double.IsFinite(value) ? Math.Clamp(value, min, max) : fallback;

    private static bool IsHexColor(string color) =>
        color.Length == 7 && color[0] == '#' && color.Skip(1).All(Uri.IsHexDigit);
}

/// <summary>
/// What one deterministic grab decided: the <see cref="Pool"/> of drawable prizes (the pit, in config order), the
/// <see cref="WinningIndex"/> into it, whether the claw <see cref="Slip"/>s (tease: it first picks up decoys and drops them) and
/// the pool indexes of those <see cref="Feints"/> (empty when there is no slip). The result is fixed before any animation.
/// </summary>
public sealed record UfoClawResult(IReadOnlyList<UfoClawPrize> Pool, int WinningIndex, bool Slip, IReadOnlyList<int> Feints)
{
    /// <summary>The prize the claw finally lifts to the UFO.</summary>
    public UfoClawPrize Winner => Pool[WinningIndex];
}

/// <summary>
/// The weighted draw ported from upstream's `UfoClawDraw.Select` (golden-tested upstream at the 0 / 0.249 / 0.25 / 0.999 sample
/// boundaries): a sample in [0, 1) scaled by the total weight of the eligible entries picks the first whose cumulative weight
/// exceeds it. Exact decimal arithmetic, so the pick is identical on every platform.
/// </summary>
public static class UfoClawDraw
{
    /// <summary>The index of the entry selected by <paramref name="sample"/>; throws on a bad sample, a bad weight or an empty pool.</summary>
    public static int Select(IReadOnlyList<UfoClawPrize> entries, decimal sample)
    {
        if (sample is < 0 or >= 1)
        {
            throw new ArgumentOutOfRangeException(nameof(sample));
        }

        if (entries.Any(e => e.Weight is < 0 or > 1_000_000))
        {
            throw new ArgumentException("Weights must be between zero and 1,000,000.");
        }

        var total = entries.Sum(e => e.Weight);
        if (total <= 0)
        {
            throw new InvalidOperationException("No eligible entries remain.");
        }

        var target = sample * total;
        decimal cumulative = 0;
        for (var i = 0; i < entries.Count; i++)
        {
            cumulative += entries[i].Weight;
            if (target < cumulative)
            {
                return i;
            }
        }

        throw new InvalidOperationException("Unable to select an entry.");
    }
}

/// <summary>
/// The pure claw-run simulation: one <see cref="Xoshiro256"/> seeded from the recorded seed, drawn in a fixed order (winner,
/// slip, suspense-drop count, decoy shuffle), so the same seed + config always gives the same <see cref="UfoClawResult"/>.
/// Upstream draws the winner with `RandomNumberGenerator` and derives the tease from a hash of the grab id in the browser; here
/// both come from the seed, so the whole run is auditable and replayable server-side.
/// </summary>
public static class UfoClawSimulation
{
    private const decimal Two53 = 9007199254740992m;

    /// <summary>Runs one grab for <paramref name="seed"/> under <paramref name="config"/> (already <see cref="UfoClawConfig.Clamped"/>).</summary>
    public static UfoClawResult Run(ulong seed, UfoClawConfig config)
    {
        var rng = new Xoshiro256(seed);
        var pool = config.Prizes.Where(p => p.Weight > 0).ToList();
        var sample = (rng.NextULong() >> 11) / Two53;
        var winner = UfoClawDraw.Select(pool, sample);

        var slipUnit = rng.NextDouble();
        var dropUnit = rng.NextDouble();
        var order = Enumerable.Range(0, pool.Count).Where(i => i != winner).ToList();
        var dropCount = config.MaxSuspenseDrops == 0 || order.Count == 0
            ? 0
            : Math.Min(order.Count, 1 + (int)(dropUnit * config.MaxSuspenseDrops));
        for (var i = order.Count - 1; i > 0; i--)
        {
            var j = rng.NextInt(0, i);
            (order[i], order[j]) = (order[j], order[i]);
        }

        var slip = config.Tease && dropCount > 0 && config.SlipChancePercent > slipUnit * 100;
        return new UfoClawResult(pool, winner, slip, slip ? order.Take(dropCount).ToList() : []);
    }
}

namespace WaddleBundleSuperpenguinPlinko;

/// <summary>
/// One landing slot at the bottom of the board, ported from PenguinTwitchBot's `PlinkoBucket`: a display label, the upstream
/// score, whether the slot is a winning one, its drop weight and its fill colour. <see cref="Weight"/> biases where the puck is
/// dropped from (see <see cref="PlinkoSimulation"/>); the physics then decides the actual landing slot.
/// </summary>
public sealed record PlinkoBucket(string Label, int Score, bool IsWin, int Weight, string Color);

/// <summary>What landing in a bucket means, per upstream's `PlinkoSimulationEngine.EvaluateBucketOutcome`.</summary>
public sealed record BucketOutcome(int Score, bool IsWin, string Label, bool IsBullseye);

/// <summary>
/// The board configuration. Defaults are upstream's `PlinkoGameSettings` defaults (4 pegged rows, 12 px pegs, 36 px puck,
/// 120 px buckets, gravity 1.0, bounciness 0.75) and its default bucket row. The simulation runs on a fixed virtual 1920x1080
/// board so a result never depends on a client's window size.
/// </summary>
public sealed record PlinkoConfig(
    int RowCount,
    int PegRadius,
    int PuckRadius,
    int BucketHeight,
    double Gravity,
    double Bounciness,
    IReadOnlyList<PlinkoBucket> Buckets)
{
    /// <summary>Virtual board width in px.</summary>
    public const int Width = 1920;

    /// <summary>Virtual board height in px.</summary>
    public const int Height = 1080;

    /// <summary>Peg columns across the board (upstream: round(width / 137) = 14 at 1920 px).</summary>
    public const int PegColumns = 14;

    /// <summary>The percent of the stake a winning bucket with score 0 pays (see README, "Money").</summary>
    public const int DefaultWinPercent = 500;

    /// <summary>Upstream's default five-bucket row: four MISS slots (weight 25) around one WINNER slot (weight 15).</summary>
    public static IReadOnlyList<PlinkoBucket> DefaultBuckets { get; } =
    [
        new("MISS", 0, false, 25, "#64748B"),
        new("MISS", 0, false, 25, "#64748B"),
        new("WINNER", 0, true, 15, "#F59E0B"),
        new("MISS", 0, false, 25, "#64748B"),
        new("MISS", 0, false, 25, "#64748B"),
    ];

    /// <summary>The default config, as `PlinkoGameSettings` ships it.</summary>
    public static PlinkoConfig Default { get; } = new(4, 12, 36, 120, 1.0, 0.75, DefaultBuckets);

    /// <summary>The config with upstream's setting clamps applied (rows 4..14, peg 4..30, puck 10..50, bucket height 60..220, gravity 0.1..10, bounciness 0.1..5).</summary>
    public PlinkoConfig Clamped() => this with
    {
        RowCount = Math.Clamp(RowCount, 4, 14),
        PegRadius = Math.Clamp(PegRadius, 4, 30),
        PuckRadius = Math.Clamp(PuckRadius, 10, 50),
        BucketHeight = Math.Clamp(BucketHeight, 60, 220),
        Gravity = Math.Clamp(Gravity, 0.1, 10.0),
        Bounciness = Math.Clamp(Bounciness, 0.1, 5.0),
        Buckets = Buckets.Count > 0 ? Buckets : DefaultBuckets,
    };
}

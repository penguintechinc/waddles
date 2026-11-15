namespace WaddleBundleSuperpenguinParachute;

/// <summary>
/// What happens to the player's parachute when it is destroyed by a rocket and lands on the platform anyway, ported from
/// PenguinTwitchBot's `CrashLandingScoreMode`: keep the landing score, score nothing, or score half.
/// </summary>
public enum CrashLandingScoreMode
{
    /// <summary>The crash landing keeps the score of the landing spot.</summary>
    PreserveScore = 0,

    /// <summary>A crash landing scores nothing.</summary>
    ZeroScore = 1,

    /// <summary>A crash landing scores half of the landing spot's score (at least 1 when it hit the platform).</summary>
    HalfScore = 2,
}

/// <summary>
/// The Parachute Drop configuration: the physics and target settings of upstream's `ParachuteGameSettings`, plus the knobs of the
/// three upstream sub-actions (Parachute Drop / Parachute Droplets / Parachute Rockets) that this port runs as part of every
/// wagered drop. Every value is clamped by <see cref="Clamped"/> exactly like upstream clamps its settings and sub-action fields.
///
/// <para><b>Port-specific fields.</b> Upstream's droplets and rockets are fired by a streamer/mod action on a timed overlay; a
/// wagered drop has no such actor, so each drop carries its own seeded hazard wave: <see cref="DropletCount"/> droplets in
/// <see cref="DropletPattern"/> (`Mixed` = one of the four upstream patterns drawn from the seed), and with probability
/// <see cref="RocketVolleyChancePct"/> percent a volley of <see cref="RocketCount"/> rockets in <see cref="RocketPattern"/>
/// (`Mixed` = one of the three upstream patterns).</para>
/// </summary>
public sealed record ParachuteConfig(
    double TargetCenterX,
    double TargetWidthPercent,
    double Gravity,
    double Bounciness,
    double Mass,
    CrashLandingScoreMode CrashMode,
    bool DirectHitKillsParachute,
    double CanopyKillChance,
    double RocketKnockbackMultiplier,
    int DropletCount,
    string DropletSize,
    IReadOnlyList<string> DropletRandomSizes,
    string DropletSpeed,
    string DropletPattern,
    double DropletKnockbackMultiplier,
    int RocketVolleyChancePct,
    int RocketCount,
    string RocketPattern)
{
    /// <summary>Virtual board width in px (collision geometry is in px like upstream's canvas; the board is fixed so a result never depends on a window size).</summary>
    public const int Width = 1920;

    /// <summary>Virtual board height in px.</summary>
    public const int Height = 1080;

    /// <summary>Upstream's four droplet spawn patterns.</summary>
    public static IReadOnlyList<string> DropletPatterns { get; } = ["TopRain", "Targeted", "SideWind", "Fountain"];

    /// <summary>Upstream's three rocket launch patterns.</summary>
    public static IReadOnlyList<string> RocketPatterns { get; } = ["Targeted", "Random", "Barrage"];

    /// <summary>
    /// The shipped defaults: upstream's `ParachuteGameSettings` values (target centre 0.50 and 18% wide, gravity 1.0, bounciness 0.85,
    /// mass 1.0, rockets on with a 100% canopy-kill chance, direct hits kill, 5 droplets, rocket knockback 1.0), with the port's
    /// choices: random droplet size/speed (upstream's `Random` presets) and `Mixed` droplet pattern, a 20% chance of a single
    /// `Targeted` rocket, and a crash landing scoring half (upstream's default `PreserveScore` would pay a destroyed parachute
    /// in full, which makes no sense for a wager).
    /// </summary>
    public static ParachuteConfig Default { get; } = new(
        0.50, 18.0, 1.0, 0.85, 1.0,
        CrashLandingScoreMode.HalfScore, true, 1.0, 1.0,
        5, "Random", ["Small", "Medium", "Large", "Giant"], "Random", "Mixed", 1.0,
        20, 1, "Targeted");

    /// <summary>The config with upstream's clamps applied (gravity 0.1..10, bounciness 0.1..5, mass 0.1..10, droplets 0..30, rockets 1..10, knockbacks 0.1..5, chances 0..1 / 0..100).</summary>
    public ParachuteConfig Clamped() => this with
    {
        TargetWidthPercent = Math.Clamp(TargetWidthPercent, 4.0, 70.0),
        Gravity = Math.Clamp(Gravity, 0.1, 10.0),
        Bounciness = Math.Clamp(Bounciness, 0.1, 5.0),
        Mass = Math.Clamp(Mass, 0.1, 10.0),
        CanopyKillChance = Math.Clamp(CanopyKillChance, 0.0, 1.0),
        RocketKnockbackMultiplier = Math.Clamp(RocketKnockbackMultiplier, 0.1, 5.0),
        DropletCount = Math.Clamp(DropletCount, 0, 30),
        DropletKnockbackMultiplier = Math.Clamp(DropletKnockbackMultiplier, 0.1, 5.0),
        RocketVolleyChancePct = Math.Clamp(RocketVolleyChancePct, 0, 100),
        RocketCount = Math.Clamp(RocketCount, 1, 10),
    };
}

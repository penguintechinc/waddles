namespace WaddleBundleSuperpenguinBalloon;

/// <summary>The kinds of hazard upstream's Balloon Ascent can throw at a balloon (`BalloonHazardsType.HazardType`, minus `Random`).</summary>
public enum HazardKind
{
    /// <summary>`Nudge`: a light droplet that knocks the balloon sideways and briefly stalls its climb.</summary>
    Nudge = 0,

    /// <summary>`HeavyPush`: an anvil that slams the balloon down and stalls its climb for most of a second.</summary>
    HeavyPush = 1,

    /// <summary>`WindGust`: a fast sideways gust that shoves the balloon horizontally.</summary>
    WindGust = 2,

    /// <summary>`DartPop`: a sharp dart that pops the balloon; the avatar then free-falls and the flight scores zero.</summary>
    DartPop = 3,
}

/// <summary>
/// The Balloon Ascent configuration. Defaults are upstream's client/settings defaults (see `balloon.js` `targetCloud` and
/// `BalloonAscentService.TriggerBalloonAscentAsync`): buoyancy 1.0, an 18 percent wide cloud centred at 0.5 drifting at speed 0.5,
/// all four hazard kinds enabled. <see cref="HazardCount"/> (upstream: streamer-triggered waves of 1-25, `!balloonhazards` uses 6; this port defaults to a lighter 3)
/// is the size of the one seeded hazard wave each flight faces. The simulation runs on a fixed virtual 1920x1080 field.
/// The payout fields are this port's mapping (upstream is free play), see README "Money".
/// </summary>
public sealed record BalloonConfig
{
    /// <summary>Virtual field width in px.</summary>
    public const int Width = 1920;

    /// <summary>Virtual field height in px.</summary>
    public const int Height = 1080;

    /// <summary>Upstream `Buoyancy` setting, clamped to 0.1..10; higher climbs faster (flight time = 5.8 s / buoyancy^0.25, 3.2..8.5 s).</summary>
    public double Buoyancy { get; init; } = 1.0;

    /// <summary>Upstream `TargetWidthPercent`: the cloud's full width as a percent of the field (half width clamped to 0.03..0.40).</summary>
    public double TargetWidthPercent { get; init; } = 18.0;

    /// <summary>Upstream `TargetCenterX`: the cloud's resting centre (0..1), kept at least a half width from either edge.</summary>
    public double TargetCenterX { get; init; } = 0.5;

    /// <summary>Upstream `TargetSpeed`: how fast the cloud sweeps (angular rate = speed * 0.14 rad/s); 0 is stationary.</summary>
    public double TargetSpeed { get; init; } = 0.5;

    /// <summary>Hazards in the flight's seeded wave, 0..25 (0 = a clear sky).</summary>
    public int HazardCount { get; init; } = 3;

    /// <summary>Upstream `EnableDarts`.</summary>
    public bool EnableDarts { get; init; } = true;

    /// <summary>Upstream `EnableAnvils`.</summary>
    public bool EnableAnvils { get; init; } = true;

    /// <summary>Upstream `EnableNudges`.</summary>
    public bool EnableNudges { get; init; } = true;

    /// <summary>Upstream `EnableWindGusts`.</summary>
    public bool EnableWindGusts { get; init; } = true;

    /// <summary>Percent of the stake a docked flight pays before its score bonus (see <see cref="PayoutPercent"/>).</summary>
    public int HitBasePercent { get; init; } = 250;

    /// <summary>Extra percent of the stake per score point (1..100) on a docked flight.</summary>
    public int PercentPerPoint { get; init; } = 4;

    /// <summary>Flat percent of the stake paid for a golden bullseye (score 98 or more), replacing the base plus per-point amount.</summary>
    public int BullseyePercent { get; init; } = 1000;

    /// <summary>The hazard kinds currently enabled, in a fixed order (this order feeds the seeded type pick).</summary>
    public IReadOnlyList<HazardKind> EnabledKinds()
    {
        var kinds = new List<HazardKind>(4);
        if (EnableNudges)
        {
            kinds.Add(HazardKind.Nudge);
        }

        if (EnableAnvils)
        {
            kinds.Add(HazardKind.HeavyPush);
        }

        if (EnableWindGusts)
        {
            kinds.Add(HazardKind.WindGust);
        }

        if (EnableDarts)
        {
            kinds.Add(HazardKind.DartPop);
        }

        return kinds;
    }

    /// <summary>This config with every value clamped into the range upstream's settings and this port's payout cap allow.</summary>
    public BalloonConfig Clamped() => this with
    {
        Buoyancy = Math.Clamp(Buoyancy, 0.1, 10.0),
        TargetWidthPercent = Math.Clamp(TargetWidthPercent, 6.0, 80.0),
        TargetCenterX = Math.Clamp(TargetCenterX, 0.0, 1.0),
        TargetSpeed = Math.Clamp(TargetSpeed, 0.0, 10.0),
        HazardCount = Math.Clamp(HazardCount, 0, 25),
        HitBasePercent = Math.Clamp(HitBasePercent, 0, 10000),
        PercentPerPoint = Math.Clamp(PercentPerPoint, 0, 10000),
        BullseyePercent = Math.Clamp(BullseyePercent, 0, 10000),
    };

    /// <summary>
    /// The percent of the stake this flight pays: 0 unless it docked on the cloud; a bullseye pays <see cref="BullseyePercent"/>;
    /// any other dock pays <see cref="HitBasePercent"/> + score * <see cref="PercentPerPoint"/>. Capped at 10000 (the host ledger's 100x ceiling).
    /// </summary>
    public int PayoutPercent(bool hit, bool bullseye, int score)
    {
        if (!hit)
        {
            return 0;
        }

        var percent = bullseye ? (long)BullseyePercent : HitBasePercent + (long)score * PercentPerPoint;
        return (int)Math.Min(percent, 10000);
    }
}

using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinGolf;

/// <summary>
/// The green condition of a play, upstream's `GolfGreenCondition`: it only scales turf friction (and adds a breeze push for
/// <see cref="CrossBreeze"/>). Upstream picks one at random per round; here it is drawn from the play's seed.
/// </summary>
public enum GreenCondition
{
    /// <summary>Standard green, drag x1.00.</summary>
    StandardGreen = 0,

    /// <summary>Firm links turf, drag x0.78: rolls further for the same power.</summary>
    FastLinks = 1,

    /// <summary>Damp morning grass, drag x1.28: rolls less far.</summary>
    HeavyDew = 2,

    /// <summary>Cross-breeze, drag x1.05, with a lateral push on the ball.</summary>
    CrossBreeze = 3,
}

/// <summary>
/// Golf tuning, upstream's `GolfGameSettings` defaults for the single-stroke closest-to-the-pin game: 3% power variance, no club
/// multipliers, cup capture speed x1.0, a pin 84% down the course, 100 max score and a default power drawn from 65-85. The
/// condition list is the four upstream conditions; <see cref="FixedCondition"/> pins one (null = draw from the seed).
/// </summary>
public sealed record GolfConfig(
    double PowerVariancePercent,
    double CupCaptureSpeedMultiplier,
    double DriverPowerMultiplier,
    double TargetDistancePercent,
    int MaxScore,
    int DefaultPowerMin,
    int DefaultPowerMax,
    GreenCondition? FixedCondition)
{
    /// <summary>The default config, as upstream ships it.</summary>
    public static GolfConfig Default { get; } = new(3.0, 1.0, 1.0, 84.0, 100, 65, 85, null);

    /// <summary>Clamps every field to the range upstream's settings accept so a caller can never build an unsimulatable config.</summary>
    public GolfConfig Clamped()
    {
        var min = Math.Clamp(DefaultPowerMin, 1, 100);
        var max = Math.Clamp(DefaultPowerMax, min, 100);
        return this with
        {
            PowerVariancePercent = Math.Max(0.0, PowerVariancePercent),
            CupCaptureSpeedMultiplier = Math.Clamp(CupCaptureSpeedMultiplier, 0.4, 2.5),
            DriverPowerMultiplier = Math.Clamp(DriverPowerMultiplier, 0.4, 2.5),
            TargetDistancePercent = Math.Clamp(TargetDistancePercent, 40.0, 95.0),
            MaxScore = Math.Clamp(MaxScore, 1, 1000),
            DefaultPowerMin = min,
            DefaultPowerMax = max,
        };
    }
}

/// <summary>
/// The result of one tee shot: the inputs that were actually used (raw and effective power, condition, launch speed), where the ball
/// stopped, what happened at the cup, the closest-to-the-pin score and the keyframes the overlay replays.
/// </summary>
public sealed record GolfShot(
    int Power,
    double EffectivePower,
    GreenCondition Condition,
    double LaunchSpeed,
    double Target,
    double RestPosition,
    double DistanceFromPin,
    bool IsSunk,
    bool IsLipOut,
    int BaseScore,
    int TotalTicks,
    IReadOnlyList<SlideKeyframe> Keyframes)
{
    /// <summary>True when the ball finished on the green or in the cup (upstream's `IsWin`: a score above zero).</summary>
    public bool IsWin => BaseScore > 0;
}

/// <summary>
/// The pure, deterministic Golf simulation, ported from upstream's `GolfSimulationEngine.SimulateGolfShot` (stroke 1, Driver) and the
/// closest-to-the-pin scoring of its overlay (`calculateGolfScore` in golf.js). Same seed + power + config gives a byte-identical
/// <see cref="GolfShot"/>: all randomness comes from one <see cref="Xoshiro256"/> drawn in a fixed order (condition, default power,
/// power variance, breeze or rim direction) and only + - * / and <see cref="Math.Sqrt"/> decide a result.
/// </summary>
public static class GolfEngine
{
    /// <summary>Radius of the sunk-in-the-cup zone, as a fraction of the course.</summary>
    public const double CupRadius = 0.014;

    /// <summary>Radius of the putting green around the pin, as a fraction of the course.</summary>
    public const double GreenRadius = 0.035;

    /// <summary>Baseline highest rolling speed at which the cup still captures the ball.</summary>
    public const double BaseMaxCaptureSpeed = 0.070;

    /// <summary>Course length in yards, so a distance of 0.84 reads as 84 yd like upstream's chat text.</summary>
    public const double YardsPerCourse = 100.0;

    /// <summary>Upstream's turf friction multiplier for a <paramref name="condition"/>.</summary>
    public static double DragMultiplier(GreenCondition condition) => condition switch
    {
        GreenCondition.FastLinks => 0.78,
        GreenCondition.HeavyDew => 1.28,
        GreenCondition.CrossBreeze => 1.05,
        _ => 1.00,
    };

    /// <summary>Upstream's display label for a <paramref name="condition"/>.</summary>
    public static string Label(GreenCondition condition) => condition switch
    {
        GreenCondition.FastLinks => "Fast Links",
        GreenCondition.HeavyDew => "Heavy Dew",
        GreenCondition.CrossBreeze => "Cross-Breeze",
        _ => "Standard Green",
    };

    /// <summary>
    /// Upstream overlay's closest-to-the-pin score for a single stroke (no stroke penalty): sunk = <paramref name="maxScore"/>; on the
    /// green it scales from <paramref name="maxScore"/> next to the cup down to 1 on the fringe; off the green 0.
    /// </summary>
    public static int Score(double distanceFromPin, bool sunk, int maxScore)
    {
        if (sunk)
        {
            return maxScore;
        }

        if (distanceFromPin > GreenRadius)
        {
            return 0;
        }

        var span = Math.Max(1e-9, GreenRadius - CupRadius);
        var t = Math.Clamp((distanceFromPin - CupRadius) / span, 0.0, 1.0);
        return Math.Max(1, Math.Min(maxScore, (int)Math.Round(maxScore - (t * (maxScore - 1)), MidpointRounding.AwayFromZero)));
    }

    /// <summary>
    /// Plays one single-stroke tee shot. <paramref name="power"/> is the viewer's 1-100 swing power, or null to draw upstream's default
    /// (a uniform integer in [DefaultPowerMin, DefaultPowerMax]). The draw order is fixed: condition (unless pinned), default power
    /// (if omitted), power variance (if any), then one breeze/rim direction bit when the shot needs it.
    /// </summary>
    public static GolfShot Shoot(ulong seed, int? power, GolfConfig config)
    {
        var cfg = config.Clamped();
        var rng = new Xoshiro256(seed);

        var condition = cfg.FixedCondition ?? (GreenCondition)rng.NextInt(0, 3);
        var rawPower = power ?? rng.NextInt(cfg.DefaultPowerMin, cfg.DefaultPowerMax);
        var effectivePower = SlideShotPhysics.CalculateEffectivePower(rawPower, cfg.PowerVariancePercent, rng);

        var drag = DragMultiplier(condition);
        var decel = Math.Max(0.01, SlideShotPhysics.BaseDecel * drag);
        var target = SlideShotPhysics.NormalizeTarget(cfg.TargetDistancePercent);
        var start = SlideShotPhysics.TeePosition;

        var v0 = SlideShotPhysics.PowerToInitialSpeed(effectivePower) * Math.Sqrt(Math.Max(0.01, cfg.DriverPowerMultiplier));
        var travel = SlideShotPhysics.CalculateTravelDistance(v0, drag);

        var uncapturedRest = Math.Clamp(start + travel, 0.02, 0.98);
        var rest = uncapturedRest;
        var sunk = false;
        var lipOut = false;
        var maxCaptureSpeed = BaseMaxCaptureSpeed * cfg.CupCaptureSpeedMultiplier;

        if (Math.Abs(uncapturedRest - target) <= CupRadius)
        {
            rest = target;
            sunk = true;
        }
        else if (start - target < -CupRadius && uncapturedRest - target > CupRadius)
        {
            // The ball rolls across the cup: only a ground-rolling ball can drop in or lip out (a chip must land before the cup).
            var overshoot = Math.Abs(uncapturedRest - target);
            var speedAtCup = Math.Sqrt(2.0 * decel * overshoot);
            var carry = travel * 0.60;
            var rolling = carry <= Math.Abs(target - start) + CupRadius;
            if (rolling && speedAtCup <= maxCaptureSpeed)
            {
                rest = target;
                sunk = true;
            }
            else if (rolling)
            {
                lipOut = true;
                var excess = Math.Clamp((speedAtCup - maxCaptureSpeed) / 0.35, 0.0, 1.0);
                rest = Math.Clamp(target + (CupRadius * (1.35 + (excess * 1.45))), 0.02, 0.98);
            }
        }

        var actualTravel = Math.Abs(rest - start);
        var durationSec = Math.Clamp(v0 / decel, 1.1, 4.2);
        var totalTicks = (int)Math.Max(36, Math.Round(durationSec * SlideShotPhysics.TickRateHz));

        var lateral = 0.50;
        if (condition == GreenCondition.CrossBreeze)
        {
            var dir = rng.NextBool() ? -1.0 : 1.0;
            lateral = Math.Clamp(0.50 + (dir * 0.16 * (effectivePower / 100.0)), 0.16, 0.84);
        }
        else if (lipOut)
        {
            var dir = rng.NextBool() ? -1.0 : 1.0;
            lateral = Math.Clamp(0.50 + (dir * 0.06), 0.20, 0.80);
        }

        var keyframes = actualTravel > 0.04
            ? AirborneKeyframes(start, rest, actualTravel, totalTicks, v0, effectivePower, lateral)
            : [SlideShotPhysics.CreatePoint(0, start, 0.50, 0.0, v0, 0.0), SlideShotPhysics.CreatePoint(totalTicks, rest, lateral, 0.0, 0.0, 0.0)];

        var distance = sunk ? 0.0 : Math.Abs(rest - target);
        return new GolfShot(rawPower, effectivePower, condition, v0, target, rest, distance, sunk, lipOut, Score(distance, sunk, cfg.MaxScore), totalTicks, keyframes);
    }

    /// <summary>Upstream's 2.5D flight: parabolic arc, two bounces and the ground roll to rest, as six keyframes.</summary>
    private static List<SlideKeyframe> AirborneKeyframes(double start, double rest, double travel, int totalTicks, double v0, double effectivePower, double lateral)
    {
        var bounce1 = Math.Max(12, (int)Math.Round(totalTicks * 0.46));
        var apex1 = Math.Max(6, bounce1 / 2);
        var bounce2 = Math.Max(bounce1 + 6, (int)Math.Round(totalTicks * 0.66));
        var apex2 = (bounce1 + bounce2) / 2;

        var peak1 = Math.Round(Math.Clamp(0.32 * (effectivePower / 100.0), 0.08, 0.36), 4);
        var peak2 = Math.Round(peak1 * 0.28, 4);
        var midLateral = (0.50 + lateral) * 0.5;

        return
        [
            SlideShotPhysics.CreatePoint(0, start, 0.50, 0.0, v0, 0.0),
            SlideShotPhysics.CreatePoint(apex1, start + (travel * 0.28), midLateral, peak1, v0 * 0.82, 0.0),
            SlideShotPhysics.CreatePoint(bounce1, start + (travel * 0.55), lateral, 0.0, v0 * 0.58, -peak1 * 2.0),
            SlideShotPhysics.CreatePoint(apex2, start + (travel * 0.65), lateral, peak2, v0 * 0.44, 0.0),
            SlideShotPhysics.CreatePoint(bounce2, start + (travel * 0.74), lateral, 0.0, v0 * 0.32, -peak2 * 2.0),
            SlideShotPhysics.CreatePoint(totalTicks, rest, lateral, 0.0, 0.0, 0.0),
        ];
    }
}

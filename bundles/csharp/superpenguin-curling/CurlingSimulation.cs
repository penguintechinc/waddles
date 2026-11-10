using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinCurling;

/// <summary>
/// Pure, deterministic port of PenguinTwitchBot's `CurlingSimulationEngine` (+ the slide setup in `CurlingGameService`): ice drag,
/// power variance, launch speed, constant-deceleration slide, rest position, cross-drift/jitter lateral offset and the two scoring
/// modes. All randomness comes from one <see cref="Xoshiro256"/> seeded with the recorded seed, drawn in a fixed order, and the
/// arithmetic is only + - * / and sqrt (IEEE-exact everywhere), so one seed + config always yields the same result and keyframes.
/// Upstream's engine has no stone-on-stone collisions (the browser overlay resolves those between concurrent stones); a wagered
/// shot is a single stone, so the only contact modelled is the back board (see <see cref="CurlingResult.WallHit"/>).
/// </summary>
public static class CurlingSimulation
{
    /// <summary>Radius of the button (100 pts), normalised to the strip.</summary>
    public const double ButtonBullseyeRadius = 0.016;

    /// <summary>Radius of the 4-foot ring (75 pts).</summary>
    public const double FourFootRadius = 0.048;

    /// <summary>Radius of the 8-foot ring (50 pts).</summary>
    public const double EightFootRadius = 0.082;

    /// <summary>Radius of the 12-foot ring (25 pts).</summary>
    public const double TwelveFootRadius = 0.125;

    /// <summary>Width of one shuffleboard zone band, normalised to the strip.</summary>
    public const double ShuffleboardBandWidth = 0.038;

    /// <summary>Keyframes are sampled every this many ticks (10 Hz at 60 Hz ticks), plus the final rest keyframe.</summary>
    public const int KeyframeEveryTicks = 6;

    private const double WallMin = 0.02;
    private const double WallMax = 0.98;

    /// <summary>Maps an ice condition to a friction multiplier: slick glides further, heavy frost stops sooner (upstream's `ResolveIceDragMultiplier`).</summary>
    public static double DragMultiplier(IceCondition condition) => condition switch
    {
        IceCondition.Slick => 0.78,
        IceCondition.HeavyFrost => 1.28,
        IceCondition.CrossDrift => 1.05,
        _ => 1.00,
    };

    /// <summary>The label chat and the overlay show for an ice condition (upstream's strings).</summary>
    public static string Label(IceCondition condition) => condition switch
    {
        IceCondition.Slick => "Slick Ice",
        IceCondition.HeavyFrost => "Heavy Frost",
        IceCondition.CrossDrift => "Cross-Drift",
        _ => "Normal Ice",
    };

    /// <summary>
    /// Runs one slide. <paramref name="requestedPower"/> is the viewer's 1-100 power, or null for the default draw (upstream draws
    /// uniformly from the configured range). The default-power draw is always consumed first so the draw order never depends on
    /// whether a power was given.
    /// </summary>
    public static CurlingResult Run(ulong seed, CurlingConfig config, int? requestedPower)
    {
        var rng = new Xoshiro256(seed);
        var defaultPower = rng.NextInt(config.DefaultPowerMin, config.DefaultPowerMax);
        double rawPower = requestedPower ?? defaultPower;
        var ice = config.DynamicIce ? (IceCondition)rng.NextInt(0, 3) : config.FixedIce;

        var drag = DragMultiplier(ice);
        var effectivePower = SlideShotPhysics.CalculateEffectivePower(rawPower, config.PowerVariancePercent, rng);
        var v0 = SlideShotPhysics.PowerToInitialSpeed(effectivePower);
        var decel = SlideShotPhysics.BaseDecel * drag;
        var slideDistance = SlideShotPhysics.CalculateTravelDistance(v0, drag);

        var alternatingBit = config.Direction == SlideDirection.Alternating && rng.NextBool();
        var isReversed = SlideShotPhysics.ResolveIsReversed(config.Direction, alternatingBit);
        var startPos = SlideShotPhysics.DefaultTeePosition(isReversed);
        var directionSign = isReversed ? -1.0 : 1.0;
        var unclamped = startPos + (directionSign * slideDistance);
        var restPos = Math.Clamp(unclamped, WallMin, WallMax);
        var wallHit = restPos != unclamped;

        var totalTicks = (int)Math.Max(30, Math.Round(v0 / decel * SlideShotPhysics.TickRateHz));

        var lateral = 0.50;
        if (ice == IceCondition.CrossDrift)
        {
            var driftDir = rng.NextBool() ? 1.0 : -1.0;
            lateral = Math.Clamp(0.50 + (driftDir * 0.18 * (effectivePower / 100.0)), 0.15, 0.85);
        }
        else if (config.PowerVariancePercent > 0)
        {
            var jitter = ((rng.NextDouble() * 2.0) - 1.0) * (config.PowerVariancePercent / 100.0) * 0.15;
            lateral = Math.Clamp(0.50 + jitter, 0.15, 0.85);
        }

        var targetRatio = SlideShotPhysics.ResolveNormalizedTarget(config.Direction, config.TargetDistancePercent, isReversed);
        var (score, bullseye, win) = CalculateScore(restPos, targetRatio, config.Scoring, isReversed, config.KitchenPenalty);

        var stopSeconds = wallHit
            ? (v0 - Math.Sqrt(Math.Max(0.0, (v0 * v0) - (2.0 * decel * Math.Abs(restPos - startPos))))) / decel
            : v0 / decel;
        var keyframes = BuildKeyframes(startPos, restPos, lateral, directionSign, v0, decel, stopSeconds, totalTicks);

        return new CurlingResult(
            rawPower, effectivePower, ice, isReversed, targetRatio, startPos, restPos, lateral, totalTicks, wallHit,
            score, bullseye, win, keyframes);
    }

    /// <summary>
    /// Samples the constant-deceleration slide every <see cref="KeyframeEveryTicks"/> ticks (position = start + dir * (v0 t - a t^2 / 2),
    /// frozen at the rest position once the stone stops or meets the back board) and ends with a keyframe at <paramref name="totalTicks"/>.
    /// </summary>
    internal static List<SlidePoint> BuildKeyframes(double startPos, double restPos, double lateral, double directionSign, double v0, double decel, double stopSeconds, int totalTicks)
    {
        var points = new List<SlidePoint>();
        for (var tick = 0; tick < totalTicks; tick += KeyframeEveryTicks)
        {
            var t = tick / SlideShotPhysics.TickRateHz;
            if (t >= stopSeconds)
            {
                points.Add(SlideShotPhysics.CreatePoint(tick, restPos, lateral, 0.0));
                continue;
            }

            var pos = startPos + (directionSign * ((v0 * t) - (0.5 * decel * t * t)));
            points.Add(SlideShotPhysics.CreatePoint(tick, pos, lateral, directionSign * (v0 - (decel * t))));
        }

        points.Add(SlideShotPhysics.CreatePoint(totalTicks, restPos, lateral, 0.0));
        return points;
    }

    /// <summary>
    /// Scores a stone resting at <paramref name="restPos"/> against <paramref name="targetPos"/> (upstream's `CalculateScore`):
    /// curling house = concentric rings round the button (100/75/50/25, else 0); shuffleboard = 30/20/10 bands short of and on the
    /// target, a -10 "kitchen" penalty just past it when enabled, else 0. Win = score above 0.
    /// </summary>
    public static (int Score, bool IsBullseye, bool IsWin) CalculateScore(double restPos, double targetPos, ScoringType scoring, bool isReversed, bool kitchenPenalty)
    {
        if (scoring == ScoringType.Shuffleboard)
        {
            var offset = (isReversed ? -1.0 : 1.0) * (restPos - targetPos);
            const double band = ShuffleboardBandWidth;
            if (offset >= -0.5 * band && offset <= 0.5 * band)
            {
                return (30, true, true);
            }

            if (offset >= -1.5 * band && offset < -0.5 * band)
            {
                return (20, false, true);
            }

            if (offset >= -2.5 * band && offset < -1.5 * band)
            {
                return (10, false, true);
            }

            return kitchenPenalty && offset > 0.5 * band && offset <= 1.7 * band ? (-10, false, false) : (0, false, false);
        }

        var delta = Math.Abs(restPos - targetPos);
        if (delta <= ButtonBullseyeRadius)
        {
            return (100, true, true);
        }

        if (delta <= FourFootRadius)
        {
            return (75, false, true);
        }

        if (delta <= EightFootRadius)
        {
            return (50, false, true);
        }

        return delta <= TwelveFootRadius ? (25, false, true) : (0, false, false);
    }
}

using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinCurling;

/// <summary>Which way a shot travels along the strip; upstream's `CurlingSlideDirection`, shared by every slide-shot game.</summary>
public enum SlideDirection
{
    LeftToRight = 0,
    RightToLeft = 1,
    TopToBottom = 2,
    BottomToTop = 3,
    Alternating = 4,
    CenterTarget = 5,
}

/// <summary>One keyframe of a slide: tick, the axis position, the lateral position and the axis velocity (all normalised to the strip).</summary>
public readonly record struct SlidePoint(int Tick, double AxisPos, double LateralPos, double AxisVelocity);

/// <summary>
/// This bundle's own copy of PenguinTwitchBot's shared `Common/SlideShotPhysics.cs` (MIT, Psychoboy/PenguinTwitchBot): the
/// launch-along-a-strip helpers Curling and Golf share upstream. It is vendored per bundle (the Golf port keeps its own copy) so
/// neither bundle depends on the other. The constants and formulas are upstream's; the differences are (1) randomness is drawn from
/// the recorded-seed <see cref="Xoshiro256"/> instead of `System.Random`, and (2) "alternating" direction is decided by a seed bit,
/// because upstream's `string.GetHashCode()` is neither stable across runs nor across platforms. Only + - * / and sqrt are used.
/// </summary>
public static class SlideShotPhysics
{
    /// <summary>Keyframe/step rate of the slide, in ticks per second.</summary>
    public const double TickRateHz = 60.0;

    /// <summary>Baseline friction deceleration on a neutral surface (drag multiplier 1.0).</summary>
    public const double BaseDecel = 0.22;

    /// <summary>Maximum initial launch speed (power 100).</summary>
    public const double MaxLaunchSpeed = 0.76;

    /// <summary>Applies variance (+/- <paramref name="variancePercent"/> power points) to a raw 1-100 power and clamps; a non-finite power counts as 75.</summary>
    public static double CalculateEffectivePower(double rawPower, double variancePercent, Xoshiro256 rng)
    {
        if (!double.IsFinite(rawPower))
        {
            rawPower = 75.0;
        }

        var clampedRaw = Math.Clamp(rawPower, 1.0, 100.0);
        if (variancePercent <= 0.0)
        {
            return clampedRaw;
        }

        var delta = ((rng.NextDouble() * 2.0) - 1.0) * variancePercent;
        return Math.Clamp(clampedRaw + delta, 1.0, 100.0);
    }

    /// <summary>Converts an effective power (1-100) into the initial launch speed (4 decimals, as upstream).</summary>
    public static double PowerToInitialSpeed(double effectivePower)
    {
        var powerFraction = Math.Clamp(effectivePower / 100.0, 0.01, 1.0);
        return Math.Round(powerFraction * MaxLaunchSpeed, 4);
    }

    /// <summary>Total travel distance from launch speed and surface drag: d = v^2 / (2a).</summary>
    public static double CalculateTravelDistance(double initialSpeed, double dragMultiplier)
    {
        var decel = Math.Max(0.01, BaseDecel * dragMultiplier);
        return (initialSpeed * initialSpeed) / (2.0 * decel);
    }

    /// <summary>Whether the shot runs in the reverse axis direction; `Alternating` uses <paramref name="alternatingBit"/> (a seed-derived coin).</summary>
    public static bool ResolveIsReversed(SlideDirection direction, bool alternatingBit) => direction switch
    {
        SlideDirection.Alternating => alternatingBit,
        SlideDirection.RightToLeft or SlideDirection.BottomToTop => true,
        _ => false,
    };

    /// <summary>The target position normalised along the slide axis, mirrored for reversed shots; `CenterTarget` is always the middle.</summary>
    public static double ResolveNormalizedTarget(SlideDirection direction, double targetDistancePercent, bool isReversed)
    {
        var normalizedTarget = direction == SlideDirection.CenterTarget
            ? 0.50
            : Math.Clamp(targetDistancePercent / 100.0, 0.50, 0.95);
        return isReversed ? 1.0 - normalizedTarget : normalizedTarget;
    }

    /// <summary>The default tee/launch position along the slide axis.</summary>
    public static double DefaultTeePosition(bool isReversed) => isReversed ? 0.95 : 0.05;

    /// <summary>Builds a keyframe from axis/lateral values (orientation is applied when the keyframe is serialised).</summary>
    public static SlidePoint CreatePoint(int tick, double axisPos, double lateralPos, double axisVelocity) =>
        new(tick, axisPos, lateralPos, axisVelocity);
}

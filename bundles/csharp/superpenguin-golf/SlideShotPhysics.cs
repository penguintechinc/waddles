using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinGolf;

/// <summary>
/// One keyframe of a slide shot along the strip: the simulation <see cref="Tick"/> (60 Hz), the position along the slide axis
/// (<see cref="Axis"/>, 0..1) and across it (<see cref="Lateral"/>, 0..1), the ball <see cref="Height"/> above the turf and the axis
/// and vertical velocities. Mirrors upstream's `ParachuteTrajectoryPointDto` with the strip orientation already resolved.
/// </summary>
public sealed record SlideKeyframe(int Tick, double Axis, double Lateral, double Height, double AxisVelocity, double VerticalVelocity);

/// <summary>
/// This bundle's own vendored copy of upstream's shared "launch along a strip toward a target" physics helpers
/// (`Bot/Commands/Games/Common/SlideShotPhysics.cs`, Psychoboy/PenguinTwitchBot, MIT), the part Golf uses. Constants and formulas are
/// upstream's; the two deviations are that variance draws from the one seeded <see cref="Xoshiro256"/> instead of `Random.Shared`,
/// and that the direction is fixed left-to-right (upstream's `Alternating` mode hashes a per-process string, which is not replayable).
/// </summary>
public static class SlideShotPhysics
{
    /// <summary>Simulation tick rate in Hz.</summary>
    public const double TickRateHz = 60.0;

    /// <summary>Baseline friction deceleration on a neutral surface (drag multiplier 1.0).</summary>
    public const double BaseDecel = 0.22;

    /// <summary>Maximum initial launch speed (power 100).</summary>
    public const double MaxLaunchSpeed = 0.76;

    /// <summary>Default tee/launch position along the slide axis for a left-to-right shot.</summary>
    public const double TeePosition = 0.05;

    /// <summary>
    /// Applies the streamer variance (+/- <paramref name="variancePercent"/> of power, drawn from <paramref name="rng"/>) to a raw
    /// 1-100 power and clamps the result. A non-positive variance draws nothing, so the number of draws is fixed per config.
    /// </summary>
    public static double CalculateEffectivePower(double rawPower, double variancePercent, Xoshiro256 rng)
    {
        var clampedRaw = Math.Clamp(rawPower, 1.0, 100.0);
        if (variancePercent <= 0.0)
        {
            return clampedRaw;
        }

        var delta = ((rng.NextDouble() * 2.0) - 1.0) * variancePercent;
        return Math.Clamp(clampedRaw + delta, 1.0, 100.0);
    }

    /// <summary>Converts an effective power (1-100) into initial launch speed, rounded to 4 decimals as upstream does.</summary>
    public static double PowerToInitialSpeed(double effectivePower)
    {
        var powerFraction = Math.Clamp(effectivePower / 100.0, 0.01, 1.0);
        return Math.Round(powerFraction * MaxLaunchSpeed, 4);
    }

    /// <summary>Total travel distance from launch speed and surface drag: d = v^2 / (2a).</summary>
    public static double CalculateTravelDistance(double initialSpeed, double dragMultiplier)
    {
        var decel = Math.Max(0.01, BaseDecel * dragMultiplier);
        return initialSpeed * initialSpeed / (2.0 * decel);
    }

    /// <summary>Target position along the slide axis (a 0..100 percent of the course, clamped to the 0.50-0.95 band upstream allows).</summary>
    public static double NormalizeTarget(double targetDistancePercent) => Math.Clamp(targetDistancePercent / 100.0, 0.50, 0.95);

    /// <summary>Builds a horizontal-strip keyframe (axis maps to X, lateral to Y), the only orientation this bundle simulates.</summary>
    public static SlideKeyframe CreatePoint(int tick, double axisPos, double lateralPos, double heightZ, double vAxis, double vz) =>
        new(tick, axisPos, lateralPos, heightZ, vAxis, vz);
}

using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinBalloon;

/// <summary>One point of the undisturbed (baseline) flight: tick, position and velocity, as upstream's `ParachuteTrajectoryPointDto`.</summary>
public readonly record struct BaselinePoint(int Tick, double X, double Y, double Vx, double Vy);

/// <summary>
/// Upstream's baseline trajectory for one flight (`BalloonAscentSimulationEngine.SimulateBalloonTrajectory` and
/// `GetBalloonPositionAtTick`): the straight climb with at most one edge rebound, with no hazards. Upstream uses it only to aim
/// `Targeted` hazards; the live flight is simulated tick by tick in <see cref="BalloonSimulation"/>.
/// </summary>
public sealed class BalloonBaseline
{
    private readonly IReadOnlyList<BaselinePoint> _keyframes;

    private BalloonBaseline(IReadOnlyList<BaselinePoint> keyframes) => _keyframes = keyframes;

    /// <summary>Builds the baseline keyframes for a launch (upstream rounds every stored value to 4 decimals).</summary>
    public static BalloonBaseline Build(double startX, double startY, double landingY, double vx, double vy, int totalTicks)
    {
        var duration = totalTicks > 0 ? totalTicks * BalloonSimulation.Dt : 6.0;
        var uncollided = startX + (vx * duration);
        var frames = new List<BaselinePoint> { new(0, Math.Round(startX, 4), Math.Round(startY, 4), Math.Round(vx, 4), Math.Round(vy, 4)) };
        var minY = Math.Min(landingY, startY);
        var maxY = Math.Max(landingY, startY);

        if (uncollided < 0.05 || uncollided > 0.95)
        {
            var edge = uncollided < 0.05 ? 0.05 : 0.95;
            var fallback = uncollided < 0.05 ? -0.01 : 0.01;
            var rawTick = Math.Round((edge - startX) / (vx != 0 ? vx : fallback) * BalloonSimulation.TickRateHz);
            var bounceTick = (int)Math.Clamp(rawTick, 1, Math.Max(2, totalTicks - 1));
            var bounceY = Math.Clamp(startY + (vy * (bounceTick * BalloonSimulation.Dt)), minY, maxY);
            frames.Add(new BaselinePoint(bounceTick, edge, Math.Round(bounceY, 4), Math.Round(-vx, 4), Math.Round(vy, 4)));
            var remaining = Math.Max(0.0, (totalTicks - bounceTick) * BalloonSimulation.Dt);
            var rebound = Math.Abs(vx) * 0.85 * 0.58;
            uncollided = uncollided < 0.05 ? edge + (rebound * remaining) : edge - (rebound * remaining);
        }

        var landingX = Math.Round(Math.Clamp(uncollided, 0.05, 0.95), 4);
        frames.Add(new BaselinePoint(totalTicks, landingX, landingY, Math.Round(vx, 4), Math.Round(vy, 4)));
        return new BalloonBaseline(frames);
    }

    /// <summary>The baseline position at <paramref name="tick"/>: clamped to the first/last keyframe, linearly interpolated between (upstream rounds to 4 decimals).</summary>
    public BaselinePoint PositionAt(int tick)
    {
        if (tick <= _keyframes[0].Tick)
        {
            return _keyframes[0];
        }

        if (tick >= _keyframes[^1].Tick)
        {
            return _keyframes[^1];
        }

        for (var i = 0; i < _keyframes.Count - 1; i++)
        {
            var a = _keyframes[i];
            var b = _keyframes[i + 1];
            if (tick >= a.Tick && tick <= b.Tick)
            {
                var r = (double)(tick - a.Tick) / Math.Max(1, b.Tick - a.Tick);
                return new BaselinePoint(
                    tick,
                    Math.Round(a.X + ((b.X - a.X) * r), 4),
                    Math.Round(a.Y + ((b.Y - a.Y) * r), 4),
                    Math.Round(a.Vx + ((b.Vx - a.Vx) * r), 4),
                    Math.Round(a.Vy + ((b.Vy - a.Vy) * r), 4));
            }
        }

        return _keyframes[^1];
    }
}

/// <summary>One hazard of a flight's wave: its kind, launch state (rounded to 4 decimals like upstream's `DropletInstanceDto`) and radius.</summary>
public sealed record Hazard(int Index, HazardKind Kind, double X, double Y, double Vx, double Vy, double Gravity, double Radius);

/// <summary>The three spawn patterns upstream's `BalloonHazardsType.SpawnPattern` offers.</summary>
public enum HazardPattern
{
    /// <summary>`TopRain`: hazards fall from the top of the field across its width.</summary>
    TopRain = 0,

    /// <summary>`Targeted`: hazards are aimed at where the balloon will be about 20 ticks later.</summary>
    Targeted = 1,

    /// <summary>`SideWind`: hazards stream in horizontally from one edge (wind gusts always use this).</summary>
    SideWind = 2,
}

/// <summary>
/// Port of `BalloonHazardPatternGenerator`: hazard physics presets, speed presets and the three trajectory patterns. Upstream draws
/// from `RandomNumberGenerator`/`DropGameMath`; here every draw comes from the flight's one <see cref="Xoshiro256"/>, in a fixed order,
/// so a seed fixes the whole wave.
/// </summary>
public static class BalloonHazardPattern
{
    /// <summary>The four speed presets in draw order (`Random` picks one): label and speed scale, as upstream's `ResolveHazardSpeed`.</summary>
    public static IReadOnlyList<(string Label, double Scale)> SpeedPresets { get; } =
        [("Slow", 0.65), ("Normal", 1.0), ("Fast", 1.60), ("Hyper", 2.20)];

    /// <summary>Upstream's `ResolveHazardPhysics` at the Medium size preset (scale 1): the hazard's collision radius per kind.</summary>
    public static double RadiusOf(HazardKind kind) => kind switch
    {
        HazardKind.HeavyPush => 0.038,
        HazardKind.DartPop => 0.022,
        HazardKind.WindGust => 0.045,
        _ => 0.028,
    };

    /// <summary>
    /// Upstream's `CalculateTrajectory` for one hazard: (startX, startY, vx, vy, gravity). <see cref="HazardPattern.Targeted"/> aims at the
    /// baseline position about 20 ticks after the hazard's own spawn tick; a SideWind pattern (or any wind gust) streams in from alternating
    /// edges; anything else rains from the top.
    /// </summary>
    public static (double StartX, double StartY, double Vx, double Vy, double Gravity) Trajectory(
        HazardPattern pattern, HazardKind kind, int index, double speed, BalloonBaseline baseline, int totalTicks, int waveTick, Xoshiro256 rng)
    {
        if (pattern == HazardPattern.Targeted)
        {
            var maxTick = Math.Max(0, totalTicks - 10);
            var current = Math.Clamp(waveTick + (index * 4), 0, maxTick);
            var target = baseline.PositionAt(Math.Min(Math.Max(0, totalTicks - 5), current + 20));
            var fromLeft = index % 2 == 0 ? target.X > 0.35 : target.X < 0.65;
            var sx = fromLeft ? Math.Max(0.04, target.X - 0.25) : Math.Min(0.96, target.X + 0.25);
            var sy = Math.Clamp(target.Y - 0.25, 0.02, 0.70);
            var intercept = 20.0 * BalloonSimulation.Dt / Math.Max(0.6, speed);
            return (sx, sy, (target.X - sx) / intercept, (target.Y - sy) / intercept, 0.08 * speed);
        }

        if (pattern == HazardPattern.SideWind || kind == HazardKind.WindGust)
        {
            var fromLeft = index % 2 == 0;
            var sy = rng.NextDouble(0.15, 0.75);
            var vx = (fromLeft ? 1.0 : -1.0) * rng.NextDouble(0.35, 0.55) * speed;
            var vy = rng.NextDouble(-0.04, 0.08) * speed;
            return (fromLeft ? 0.02 : 0.98, sy, vx, vy, 0.01);
        }

        var rx = rng.NextDouble(0.08, 0.92);
        var rvx = rng.NextDouble(-0.08, 0.08) * speed;
        var rvy = rng.NextDouble(0.12, 0.24) * speed;
        return (rx, 0.02, rvx, rvy, 0.14 * speed);
    }

    /// <summary>
    /// Builds one flight's hazard wave: the pattern and speed preset are drawn once, then each hazard draws its kind (uniform over
    /// <paramref name="kinds"/>, upstream's `Random`) and its trajectory. Every value is rounded to 4 decimals like upstream's DTO.
    /// </summary>
    public static IReadOnlyList<Hazard> Wave(
        int count, IReadOnlyList<HazardKind> kinds, BalloonBaseline baseline, int totalTicks, int waveTick, Xoshiro256 rng, out HazardPattern pattern, out string speedLabel)
    {
        pattern = (HazardPattern)rng.NextInt(0, 2);
        var preset = SpeedPresets[rng.NextInt(0, SpeedPresets.Count - 1)];
        speedLabel = preset.Label;
        var wave = new List<Hazard>(count);
        for (var i = 0; i < count; i++)
        {
            var kind = kinds[rng.NextInt(0, kinds.Count - 1)];
            var (x, y, vx, vy, g) = Trajectory(pattern, kind, i, preset.Scale, baseline, totalTicks, waveTick, rng);
            wave.Add(new Hazard(i, kind, Math.Round(x, 4), Math.Round(y, 4), Math.Round(vx, 4), Math.Round(vy, 4), Math.Round(g, 4), Math.Round(RadiusOf(kind), 4)));
        }

        return wave;
    }
}

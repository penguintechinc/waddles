using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinBalloon;

/// <summary>One recorded balloon position: the tick (60 per second) and the avatar position in 1/10000 of the field (x right, y down).</summary>
public readonly record struct BalloonKeyframe(int Tick, int X, int Y);

/// <summary>What happened to one hazard: it was never resolved (<see cref="HitTick"/> = -1, it left the field or the flight ended first) or struck the balloon at <see cref="HitTick"/>.</summary>
public sealed record HazardOutcome(Hazard Hazard, int HitTick);

/// <summary>The launch state of a flight: cannon shot, flight time and the per-flight size/speed variation.</summary>
public sealed record BalloonLaunch(double Vx, double Vy, double DurationSeconds, int TotalTicks, double SizeScale, double SpeedScale);

/// <summary>The target cloud's motion: its centre is `Base + sin(Phase + Omega * t) * Span` (t in seconds), half width and the cloud's y.</summary>
public sealed record CloudMotion(double Base, double HalfWidth, double Span, double Phase, double Omega, double Y);

/// <summary>
/// The deterministic result of one flight. <see cref="Popped"/> is true when a dart punctured the balloon (score 0). Otherwise the
/// balloon docked (<see cref="Hit"/>, score 1..100, <see cref="Bullseye"/> at 98+) or flew past the cloud (a miss, score 0).
/// <see cref="TimedOut"/> is true only when the tick watchdog ended a flight that never resolved (counted as a miss).
/// </summary>
public sealed record BalloonResult(
    BalloonLaunch Launch,
    CloudMotion Cloud,
    HazardPattern Pattern,
    string SpeedLabel,
    int WaveTick,
    IReadOnlyList<HazardOutcome> Hazards,
    IReadOnlyList<BalloonKeyframe> Keyframes,
    int EndTick,
    double EndX,
    double EndY,
    double CloudXAtEnd,
    bool Popped,
    int PopTick,
    double PopSpin,
    bool Hit,
    int Score,
    bool Bullseye,
    bool TimedOut);

/// <summary>
/// Pure, deterministic port of PenguinTwitchBot's Balloon Ascent: `BalloonAscentService.TriggerBalloonAscentAsync` (flight time from
/// buoyancy, launch at the cannon, per-flight size/speed variation), `BalloonAscentSimulationEngine` (constants, baseline) and
/// `BalloonHazardPatternGenerator` (hazard wave) plus the overlay's physics from `balloon.js` (cloud sweep, hazard flight and collision,
/// balloon climb/recoil/pop/free-fall, wall rebounds, arrival and score), run server-side at a fixed 60 Hz step on a fixed 1920x1080
/// field. Upstream lets each browser's wall clock and `Math.random` decide the flight; here ONE <see cref="Xoshiro256"/> seeded with the
/// recorded seed decides everything, drawn in a fixed order (cannon phase, cannon jitter, speed, size, cloud phase, wave tick, wave
/// pattern/speed, per hazard its kind then its trajectory, then pop spin/jitter and knock directions as they occur). Only + - * / and
/// sqrt decide anything (the two sines are <see cref="Sin"/>, a polynomial), so a seed and config always give the same result.
/// </summary>
public static class BalloonSimulation
{
    /// <summary>Simulation rate (upstream `TickRateHz`).</summary>
    public const double TickRateHz = 60.0;

    /// <summary>One tick in seconds.</summary>
    public const double Dt = 1.0 / TickRateHz;

    /// <summary>Upstream `LaunchY`: where the avatar starts (0 = top, 1 = bottom).</summary>
    public const double LaunchY = 0.94;

    /// <summary>Upstream `LandingY`: the cloud platform's y.</summary>
    public const double LandingY = 0.10;

    /// <summary>Upstream `CannonX`: the launch x.</summary>
    public const double CannonX = 0.5;

    /// <summary>Upstream `CannonMaxVx`: the cannon's maximum sideways launch speed.</summary>
    public const double CannonMaxVx = 0.075;

    /// <summary>Upstream `CannonAimVariance`: random jitter added to the cannon's aim.</summary>
    public const double CannonAimVariance = 0.010;

    /// <summary>Watchdog: a flight still unresolved after this many ticks (40 s) ends as a timed-out miss.</summary>
    public const int DefaultMaxTicks = 40 * 60;

    /// <summary>Keyframes are recorded every this many ticks (10 per second), plus the first and the last.</summary>
    public const int KeyframeEvery = 6;

    private const double Pi = 3.14159265358979323846;

    /// <summary>
    /// Sine from + - * / only (range reduction by floor, then a degree-21 Taylor polynomial, error under 1e-9). `Math.Sin` is not
    /// guaranteed bit-identical across platforms (wasi-wasm vs host CLR), and this value decides where the cloud and the cannon point.
    /// </summary>
    public static double Sin(double x)
    {
        var twoPi = 2.0 * Pi;
        var r = x - (twoPi * Math.Floor((x / twoPi) + 0.5));
        var x2 = r * r;
        var term = r;
        var sum = r;
        for (var n = 1; n <= 10; n++)
        {
            term = -term * x2 / ((2.0 * n) * ((2.0 * n) + 1.0));
            sum += term;
        }

        return sum;
    }

    /// <summary>Rounds like JavaScript's `Math.round` for positive values (half up), the rounding upstream's overlay uses for the score.</summary>
    public static int RoundHalfUp(double v) => (int)Math.Floor(v + 0.5);

    /// <summary>The cloud's centre at <paramref name="seconds"/> after the flight starts.</summary>
    public static double CloudCenter(CloudMotion cloud, double seconds) => cloud.Base + (Sin(cloud.Phase + (cloud.Omega * seconds)) * cloud.Span);

    /// <summary>Builds the cloud's motion from the config the way upstream's overlay does (`updateTargetCloud`): sinusoidal drift around the base centre.</summary>
    public static CloudMotion BuildCloud(BalloonConfig cfg, double phase)
    {
        var half = Math.Clamp(cfg.TargetWidthPercent / 100.0 / 2.0, 0.03, 0.40);
        var center = Math.Clamp(cfg.TargetCenterX, half, 1.0 - half);
        var lo = half + 0.04;
        var hi = 1.0 - half - 0.04;
        var span = Math.Max(0.04, Math.Min(center - lo, hi - center));
        return new CloudMotion(center, half, span, phase, cfg.TargetSpeed * 0.14, LandingY);
    }

    /// <summary>
    /// Runs one flight from <paramref name="seed"/> under <paramref name="config"/> (clamped like upstream's settings).
    /// <paramref name="maxTicks"/> is the watchdog (tests lower it to exercise the timeout path).
    /// </summary>
    public static BalloonResult Run(ulong seed, BalloonConfig config, int maxTicks = DefaultMaxTicks)
    {
        var cfg = config.Clamped();
        var rng = new Xoshiro256(seed);

        var duration = Math.Clamp(5.8 / Math.Sqrt(Math.Sqrt(cfg.Buoyancy)), 3.2, 8.5);
        var totalTicks = Math.Max(30, (int)Math.Round(duration * TickRateHz));
        var nominalVy = (LandingY - LaunchY) / duration;

        var aim = Sin(2.0 * Pi * rng.NextDouble());
        var vx0 = (aim * CannonMaxVx) + (((rng.NextDouble() * 2.0) - 1.0) * CannonAimVariance);
        var speedScale = 0.88 + (rng.NextDouble() * 0.24);
        var sizeScale = 0.90 + (rng.NextDouble() * 0.20);
        var launch = new BalloonLaunch(vx0, nominalVy * speedScale, duration, totalTicks, sizeScale, speedScale);
        var cloud = BuildCloud(cfg, rng.NextDouble() * 2.0 * Pi);

        var kinds = cfg.EnabledKinds();
        var baseline = BalloonBaseline.Build(CannonX, LaunchY, LandingY, Math.Round(vx0, 4), Math.Round(nominalVy, 4), totalTicks);
        var waveTick = 0;
        IReadOnlyList<Hazard> wave = [];
        var pattern = HazardPattern.TopRain;
        var speedLabel = BalloonHazardPattern.SpeedPresets[1].Label;
        if (cfg.HazardCount > 0 && kinds.Count > 0)
        {
            waveTick = rng.NextInt(0, 180);
            wave = BalloonHazardPattern.Wave(cfg.HazardCount, kinds, baseline, totalTicks, waveTick, rng, out pattern, out speedLabel);
        }

        var canopyR = 0.044 * sizeScale;
        var offset = 0.165 * sizeScale;
        var arrivalY = cloud.Y + offset;

        // Live state of the balloon (avatar position) and of each hazard.
        double x = CannonX, y = LaunchY, vx = vx0, vy = launch.Vy;
        var recoilVy = 0.0;
        var recoilTimer = 0.0;
        var popped = false;
        var popTick = -1;
        var popSpin = 0.0;
        var hx = wave.Select(h => h.X).ToArray();
        var hy = wave.Select(h => h.Y).ToArray();
        var hvx = wave.Select(h => h.Vx).ToArray();
        var hvy = wave.Select(h => h.Vy).ToArray();
        var alive = wave.Select(_ => false).ToArray();
        var hitTick = wave.Select(_ => -1).ToArray();

        var keyframes = new List<BalloonKeyframe> { Key(0, x, y) };
        var tick = 0;
        var endTick = 0;
        var done = false;
        var hit = false;
        var score = 0;
        var bullseye = false;
        var timedOut = false;
        var cloudAtEnd = CloudCenter(cloud, 0);

        while (!done)
        {
            if (tick == waveTick)
            {
                for (var i = 0; i < alive.Length; i++)
                {
                    alive[i] = true;
                }
            }

            tick++;
            var seconds = tick * Dt;
            cloudAtEnd = CloudCenter(cloud, seconds);

            // Hazards: fly, then collide with the balloon's canopy (processed last-to-first like upstream's reverse loop).
            for (var i = alive.Length - 1; i >= 0; i--)
            {
                if (!alive[i])
                {
                    continue;
                }

                var h = wave[i];
                hvy[i] += h.Gravity * Dt;
                hx[i] += hvx[i] * Dt;
                hy[i] += hvy[i] * Dt;
                if (!popped)
                {
                    var dx = (hx[i] * BalloonConfig.Width) - (x * BalloonConfig.Width);
                    var dy = (hy[i] * BalloonConfig.Height) - ((y - offset) * BalloonConfig.Height);
                    var reach = (canopyR + h.Radius) * BalloonConfig.Height;
                    if (Math.Sqrt((dx * dx) + (dy * dy)) < reach)
                    {
                        alive[i] = false;
                        hitTick[i] = tick;
                        switch (h.Kind)
                        {
                            case HazardKind.DartPop:
                                popped = true;
                                popTick = tick;
                                vy = 0.22;
                                popSpin = (rng.NextBool() ? -1.0 : 1.0) * (7.0 + (rng.NextDouble() * 4.0));
                                vx += (rng.NextDouble() - 0.5) * 0.12;
                                break;
                            case HazardKind.HeavyPush:
                                recoilVy = 0.28;
                                recoilTimer = 0.9;
                                y = Math.Min(0.92, y + 0.08);
                                break;
                            default:
                                vx += (hvx[i] != 0 ? Math.Sign(hvx[i]) : rng.NextDouble() - 0.5) * 0.12;
                                recoilVy = 0.08;
                                recoilTimer = 0.45;
                                break;
                        }

                        continue;
                    }
                }

                if (hy[i] > 1.1 || hy[i] < -0.15 || hx[i] < -0.1 || hx[i] > 1.1)
                {
                    alive[i] = false;
                }
            }

            // Balloon: free-fall when popped, stalled by a recoil, otherwise the steady climb.
            if (popped)
            {
                vy += 2.2 * Dt;
                y += vy * Dt;
            }
            else if (recoilTimer > 0)
            {
                recoilTimer -= Dt;
                y += recoilVy * Dt;
            }
            else
            {
                vy = launch.Vy;
                y += vy * Dt;
            }

            x += vx * Dt;
            if (x < 0.05)
            {
                x = 0.05;
                vx = Math.Abs(vx) * 0.75;
            }
            else if (x > 0.95)
            {
                x = 0.95;
                vx = -Math.Abs(vx) * 0.75;
            }

            if (!popped && y <= arrivalY)
            {
                y = arrivalY;
                var dist = Math.Abs(x - cloudAtEnd);
                hit = dist <= cloud.HalfWidth;
                if (hit)
                {
                    var ratio = Math.Min(1.0, dist / Math.Max(0.0001, cloud.HalfWidth));
                    score = Math.Max(1, Math.Min(100, RoundHalfUp(100.0 - (ratio * 99.0))));
                    bullseye = score >= 98;
                }

                done = true;
            }
            else if (popped && y >= 1.05)
            {
                done = true;
            }
            else if (tick >= maxTicks)
            {
                timedOut = true;
                done = true;
            }

            if (done || tick % KeyframeEvery == 0)
            {
                keyframes.Add(Key(tick, x, y));
            }

            endTick = tick;
        }

        var outcomes = wave.Select((h, i) => new HazardOutcome(h, hitTick[i])).ToList();
        return new BalloonResult(launch, cloud, pattern, speedLabel, waveTick, outcomes, keyframes, endTick, x, y, cloudAtEnd, popped, popTick, popSpin, hit, score, bullseye, timedOut);
    }

    private static BalloonKeyframe Key(int tick, double x, double y) =>
        new(tick, (int)Math.Round(x * 10000.0), (int)Math.Round(y * 10000.0));
}

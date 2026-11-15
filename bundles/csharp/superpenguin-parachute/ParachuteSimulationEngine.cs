using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinParachute;

/// <summary>
/// Parachute Drop simulation, ported from PenguinTwitchBot's `ParachuteSimulationEngine` (`Commands/Games/Parachute/`,
/// github.com/Psychoboy/PenguinTwitchBot, MIT) and the physics of its overlay (`wwwroot/js/parachute.js`).
///
/// <para>Upstream computes the straight-glide baseline and landing score on the server and simulates droplet/rocket collisions
/// live in the browser. Here the whole drop runs server-side from one recorded seed, in fixed 1/60 s ticks, with only
/// `+ - * /`, `Sqrt` and comparisons deciding results (no libm calls), so the same seed and config give byte-identical output on
/// the host CLR and in the wasm component: <see cref="Run"/> draws the drop parameters, the droplet wave and the optional rocket
/// volley (in that fixed order), then steps droplets, rockets and the parachutist (canopy drag, wall and target-rim bounces,
/// droplet knock-back, rocket kills, free fall) until the player lands.</para>
///
/// <para><b>Deviations from the overlay's physics.</b> Droplet-to-droplet bounces and the random spin/jitter terms are not simulated
/// (they never change the parachutist); random draws that upstream makes with `Math.random()` at collision time come from the seeded generator.</para>
/// </summary>
public static class ParachuteSimulationEngine
{
    /// <summary>Upstream's fixed tick rate.</summary>
    public const double TickRateHz = 60.0;

    /// <summary>Seconds per tick.</summary>
    public const double Dt = 1.0 / TickRateHz;

    /// <summary>Highest sideways speed of a gliding parachutist (upstream `MaxVx`).</summary>
    public const double MaxVx = 0.55;

    /// <summary>Terminal fall speed of a parachutist whose canopy is destroyed (the overlay's free-fall cap).</summary>
    public const double TerminalFreeFallVy = 0.98;

    private const double StartY = 0.04;
    private const double LandingY = 0.94;
    private const double Restitution = 0.82;
    private const double BoardW = ParachuteConfig.Width;
    private const double BoardH = ParachuteConfig.Height;
    private const double MinDim = ParachuteConfig.Height;
    private const double PlatformTopY = (ParachuteConfig.Height - 58.0) / ParachuteConfig.Height;
    private const int KeyframeEvery = 6;
    private const int DropletHitCooldownTicks = 9;

    /// <summary>Rounds to 4 decimals with plain arithmetic (identical on every platform).</summary>
    public static double Round4(double v) => Math.Round(v * 10000.0) / 10000.0;

    /// <summary>Converts a normalized value to the 1/10000 integer used in results and the overlay envelope.</summary>
    public static int Scaled(double v) => (int)Math.Round(v * 10000.0);

    /// <summary>`tan(x)` by the [5/4] Pade approximant (error under 2e-4 for |x| &lt;= 0.95): built from + - * / only, so it is exact-reproducible where `Math.Tan` is not.</summary>
    public static double Tan(double x)
    {
        var x2 = x * x;
        return x * (945.0 - (105.0 * x2) + (x2 * x2)) / (945.0 - (420.0 * x2) + (15.0 * x2 * x2));
    }

    /// <summary>
    /// Builds the straight-glide baseline of a drop (upstream `SimulateParachuteTrajectory` without rockets): edge bounces at
    /// x = 0.05 / 0.95 with the 0.85 * 0.58 rebound, landing x clamped to [0.05, 0.95], baseline keyframes at tick 0, the bounce and the last tick.
    /// </summary>
    public static GlideBaseline BuildBaseline(double startX, double initialVx, double initialVy, int totalTicks, double targetCenterX, double targetHalfWidth)
    {
        var durationSec = totalTicks * Dt;
        var uncollidedX = startX + (initialVx * durationSec);
        var keyframes = new List<GlidePoint> { new(0, Round4(startX), Round4(StartY)) };
        if (uncollidedX < 0.05 || uncollidedX > 0.95)
        {
            var edge = uncollidedX < 0.05 ? 0.05 : 0.95;
            var bounceTick = (int)Math.Clamp(Math.Round((edge - startX) / initialVx * TickRateHz), 1, Math.Max(2, totalTicks - 1));
            var bounceY = Math.Clamp(StartY + (initialVy * (bounceTick * Dt)), StartY, LandingY);
            keyframes.Add(new GlidePoint(bounceTick, edge, Round4(bounceY)));
            var remaining = Math.Max(0.0, (totalTicks - bounceTick) * Dt);
            var rebound = Math.Abs(initialVx) * 0.85 * 0.58;
            uncollidedX = uncollidedX < 0.05 ? 0.05 + (rebound * remaining) : 0.95 - (rebound * remaining);
        }

        var landingX = Round4(Math.Clamp(uncollidedX, 0.05, 0.95));
        keyframes.Add(new GlidePoint(totalTicks, landingX, LandingY));
        return new GlideBaseline(startX, StartY, LandingY, initialVx, initialVy, totalTicks, targetCenterX, targetHalfWidth, landingX, keyframes);
    }

    /// <summary>Interpolates the baseline path at <paramref name="tick"/> (upstream `GetParachutePositionAtTick`): clamped to the first/last keyframe, linear in between.</summary>
    public static (double X, double Y) GlidePosition(GlideBaseline baseline, int tick)
    {
        var k = baseline.Keyframes;
        var x = k[^1].X;
        var y = k[^1].Y;
        if (tick <= k[0].Tick)
        {
            return (k[0].X, k[0].Y);
        }

        for (var i = 0; i < k.Count - 1; i++)
        {
            if (tick >= k[i].Tick && tick <= k[i + 1].Tick)
            {
                var ratio = (double)(tick - k[i].Tick) / Math.Max(1, k[i + 1].Tick - k[i].Tick);
                x = Round4(k[i].X + ((k[i + 1].X - k[i].X) * ratio));
                y = Round4(k[i].Y + ((k[i + 1].Y - k[i].Y) * ratio));
                break;
            }
        }

        return (x, y);
    }

    /// <summary>
    /// Scores a landing on the 0..100 scale (upstream `CalculateLandingScore`): 0 missed the platform, 1 its outer edge, 100 dead centre
    /// (a bullseye); a destroyed parachute scores per <paramref name="mode"/>. Returns (score, bullseye, win, hit).
    /// </summary>
    public static (int Score, bool IsBullseye, bool IsWin, bool IsHit) CalculateLandingScore(
        double landingX, double targetCenterX, double targetHalfWidth, bool parachuteDestroyed, CrashLandingScoreMode mode)
    {
        var distance = Round4(Math.Abs(landingX - targetCenterX));
        if (distance > targetHalfWidth)
        {
            return (0, false, false, false);
        }

        var ratio = Math.Clamp(distance / Math.Max(0.0001, targetHalfWidth), 0.0, 1.0);
        var score = Math.Clamp((int)Math.Round(100.0 - (ratio * 99.0), MidpointRounding.AwayFromZero), 1, 100);
        if (!parachuteDestroyed)
        {
            return (score, score == 100, true, true);
        }

        return mode switch
        {
            CrashLandingScoreMode.ZeroScore => (0, false, false, true),
            CrashLandingScoreMode.HalfScore => (Math.Max(1, (int)Math.Round(score * 0.5, MidpointRounding.AwayFromZero)), false, true, true),
            _ => (score, score == 100, true, true),
        };
    }

    /// <summary>
    /// Plays one drop from <paramref name="seed"/>: the whole result is a pure function of (seed, config). Randomness is drawn in a
    /// fixed order (start x, glide angle, wave start, droplet pattern/specs, volley roll, volley pattern/launch/specs, then rocket
    /// kill rolls and wall-bounce directions during the steps).
    /// </summary>
    public static ParachuteResult Run(ulong seed, ParachuteConfig config)
    {
        var cfg = config.Clamped();
        var rng = new Xoshiro256(seed);

        var duration = Math.Clamp(6.2 / Math.Sqrt(Math.Sqrt(cfg.Gravity)), 3.5, 8.5);
        var totalTicks = Math.Max(30, (int)Math.Round(duration * TickRateHz));
        var startX = rng.NextDouble(0.10, 0.90);
        var baseVy = (LandingY - StartY) / duration;
        var angle = rng.NextDouble(-0.95, 0.95);
        var initialVx = Round4(Math.Clamp(baseVy * Tan(angle) * 1.15, -MaxVx, MaxVx));

        var halfWidth = Math.Clamp(cfg.TargetWidthPercent / 200.0, 0.02, 0.35);
        var targetCenterX = Math.Clamp(cfg.TargetCenterX, Math.Max(0.12, halfWidth + 0.03), Math.Min(0.88, 1.0 - halfWidth - 0.03));
        var baseline = BuildBaseline(startX, initialVx, baseVy, totalTicks, targetCenterX, halfWidth);

        var waveStart = rng.NextInt(30, 120);
        var dropletPattern = ResolvePattern(cfg.DropletPattern, ParachuteConfig.DropletPatterns, "TopRain", rng);
        var droplets = new List<DropletSpec>(cfg.DropletCount);
        for (var i = 0; i < cfg.DropletCount; i++)
        {
            var (sizeLabel, sizeScale, radius, mass) = DropletPatternGenerator.ResolveSize(cfg.DropletSize, cfg.DropletRandomSizes, rng);
            var (speedLabel, speedScale) = DropletPatternGenerator.ResolveSpeed(cfg.DropletSpeed, rng);
            var (x, y, vx, vy, gravity) = DropletPatternGenerator.CalculateTrajectory(dropletPattern, i, speedScale, baseline, waveStart, i * 4, rng);
            droplets.Add(new DropletSpec(
                i, waveStart + (i * 4), Round4(x), Round4(y), Round4(vx), Round4(vy), Round4(gravity), Round4(radius),
                Math.Round(mass * cfg.DropletKnockbackMultiplier * 1000.0) / 1000.0, sizeLabel, Math.Round(sizeScale * 100.0) / 100.0, speedLabel, Math.Round(speedScale * 100.0) / 100.0, dropletPattern));
        }

        var volley = rng.NextInt(1, 100) <= cfg.RocketVolleyChancePct;
        var rocketPattern = volley ? ResolvePattern(cfg.RocketPattern, ParachuteConfig.RocketPatterns, "Targeted", rng) : string.Empty;
        List<RocketSpec> rockets = volley
            ? RocketTrajectoryGenerator.Generate(rocketPattern, cfg.RocketCount, baseline, rng.NextInt(40, 150), rng)
            : [];

        var sim = new Stepper(cfg, baseline, droplets, rockets, rng, totalTicks);
        return sim.Run(dropletPattern, rocketPattern);
    }

    /// <summary>Resolves a pattern name against a pattern list: `Mixed` draws one from the seed, a known name is normalized to its canonical casing, anything else falls back to <paramref name="fallback"/>.</summary>
    internal static string ResolvePattern(string name, IReadOnlyList<string> patterns, string fallback, Xoshiro256 rng)
    {
        var trimmed = name.Trim();
        if (trimmed.Equals("Mixed", StringComparison.OrdinalIgnoreCase))
        {
            return patterns[rng.NextInt(0, patterns.Count - 1)];
        }

        return patterns.FirstOrDefault(p => p.Equals(trimmed, StringComparison.OrdinalIgnoreCase)) ?? fallback;
    }

    /// <summary>The mutable state of one drop's step simulation (the parachutist, the live droplets and rockets) and its per-tick update order: droplets, rockets, then the parachutist.</summary>
    private sealed class Stepper
    {
        private readonly ParachuteConfig _cfg;
        private readonly GlideBaseline _base;
        private readonly Xoshiro256 _rng;
        private readonly int _totalTicks;
        private readonly List<DropletState> _droplets;
        private readonly List<RocketState> _rockets;
        private readonly List<KeyPoint> _keyframes = [];
        private readonly List<CollisionEvent> _collisions = [];
        private double _x;
        private double _y = StartY;
        private double _vx;
        private double _vy;
        private bool _destroyed;
        private bool _rimBounced;
        private bool _mark;
        private int _freeFall = -1;
        private int _lastDropletHit = -1000;
        private int _dropletHits;
        private int _rocketHits;

        public Stepper(ParachuteConfig cfg, GlideBaseline baseline, List<DropletSpec> droplets, List<RocketSpec> rockets, Xoshiro256 rng, int totalTicks)
        {
            _cfg = cfg;
            _base = baseline;
            _rng = rng;
            _totalTicks = totalTicks;
            _x = baseline.StartX;
            _vx = baseline.InitialVx;
            _vy = baseline.InitialVy;
            _droplets = [.. droplets.Select(d => new DropletState(d))];
            _rockets = [.. rockets.Select(r => new RocketState(r))];
        }

        /// <summary>Steps until the parachutist reaches the ground (a hard cap of 3x the glide time forces the landing), then scores it.</summary>
        public ParachuteResult Run(string dropletPattern, string rocketPattern)
        {
            _keyframes.Add(Key(0));
            var maxTicks = _totalTicks * 3;
            var tick = 0;
            while (tick < maxTicks)
            {
                tick++;
                _mark = false;
                foreach (var d in _droplets)
                {
                    StepDroplet(d, tick);
                }

                foreach (var r in _rockets)
                {
                    StepRocket(r, tick);
                }

                StepParachutist(tick);
                if (_y >= LandingY - 1e-9)
                {
                    break;
                }

                if (_mark || tick % KeyframeEvery == 0)
                {
                    _keyframes.Add(Key(tick));
                }
            }

            _y = LandingY;
            _keyframes.Add(Key(tick));
            var (score, bullseye, win, hit) = CalculateLandingScore(_x, _base.TargetCenterX, _base.TargetHalfWidth, _destroyed, _cfg.CrashMode);
            var flights = _rockets.Select(r => r.ToFlight()).ToList();
            return new ParachuteResult(
                _base.StartX, _base.InitialVx, _base.InitialVy, _totalTicks, _base.TargetCenterX, _base.TargetHalfWidth, _base.TargetHalfWidth * 0.25,
                Round4(_x), tick, score, hit, bullseye, win, _destroyed, _freeFall, _dropletHits, _rocketHits,
                dropletPattern, rocketPattern, _keyframes, _collisions, _droplets.Select(d => d.Spec).ToList(), flights);
        }

        private KeyPoint Key(int tick) => new(tick, Scaled(_x), Scaled(_y), Scaled(_vx), Scaled(_vy));

        private void Collide(int tick, int kind, int source, double x, double y, double dvx, double dvy)
        {
            _collisions.Add(new CollisionEvent(tick, kind, source, Scaled(x), Scaled(y), Scaled(dvx), Scaled(dvy)));
            _mark = true;
        }

        /// <summary>One droplet tick: gravity, walls, the target platform and the floor (bouncing at restitution 0.82), then contact with the parachutist's body, which knocks it sideways.</summary>
        private void StepDroplet(DropletState d, int tick)
        {
            if (!d.Alive || tick < d.Spec.SpawnTick)
            {
                return;
            }

            var r = d.Spec.Radius;
            d.Vy += d.Spec.Gravity * 1.5 * Dt;
            d.X += d.Vx * Dt;
            d.Y += d.Vy * Dt;
            if (d.X < r)
            {
                d.X = r;
                d.Vx = Math.Abs(d.Vx) * Restitution;
            }
            else if (d.X > 1.0 - r)
            {
                d.X = 1.0 - r;
                d.Vx = -Math.Abs(d.Vx) * Restitution;
            }

            if (d.Vy > 0 && d.Y + r >= PlatformTopY && d.Y - r <= PlatformTopY + 0.03 && Math.Abs(d.X - _base.TargetCenterX) <= _base.TargetHalfWidth + (r * 0.5))
            {
                d.Y = PlatformTopY - r;
                Bounce(d, Restitution, 1.0, 0.75);
            }

            if (d.Y + r >= 0.995)
            {
                d.Y = 0.995 - r;
                Bounce(d, Restitution * 0.85, 0.85, 0.70);
            }

            if (!d.Alive)
            {
                return;
            }

            var bodyR = Math.Max(24.0, MinDim * 0.032);
            var minDist = bodyR + Math.Max(12.0, r * MinDim);
            var dx = (_x - d.X) * BoardW;
            var dy = (_y - d.Y) * BoardH;
            var distSq = (dx * dx) + (dy * dy);
            if (distSq >= minDist * minDist || distSq <= 0.0001)
            {
                return;
            }

            var dist = Math.Sqrt(distSq);
            var nx = dx / dist;
            var ny = dy / dist;
            d.X = ((_x * BoardW) - (nx * minDist)) / BoardW;
            d.Y = ((_y * BoardH) - (ny * minDist)) / BoardH;
            var vDot = ((_vx - d.Vx) * nx) + ((_vy - d.Vy) * ny);
            if (vDot >= 0)
            {
                return;
            }

            var impulse = -(1 + Restitution) * vDot;
            d.Vx -= impulse * nx * 0.4;
            d.Vy -= impulse * ny * 0.4;
            if (tick - _lastDropletHit < DropletHitCooldownTicks)
            {
                return;
            }

            _lastDropletHit = tick;
            var massFactor = Math.Clamp(d.Spec.Mass / Math.Max(0.2, _cfg.Mass), 0.5, 6.0);
            var bounceScale = Math.Clamp(_cfg.Bounciness, 0.1, 2.5) / 0.85;
            var dvx = Math.Clamp(nx * impulse * 0.85 * massFactor * bounceScale, -0.65, 0.65);
            var dvy = Math.Clamp(ny * impulse * 0.65 * massFactor * bounceScale, -0.25, 0.40);
            _vx = Math.Clamp(_vx + dvx, -0.65, 0.65);
            _vy = Math.Clamp(_vy + dvy, -0.15, 0.55);
            _dropletHits++;
            Collide(tick, 0, d.Spec.Index, _x, _y, dvx, dvy);
        }

        /// <summary>A droplet bouncing off a surface: a fast fall reflects by <paramref name="restitution"/> (sideways speed scaled by <paramref name="bounceVx"/>), a slow one settles (sideways speed scaled by <paramref name="settleVx"/>) and stops being simulated once it stops sliding.</summary>
        private static void Bounce(DropletState d, double restitution, double bounceVx, double settleVx)
        {
            if (Math.Abs(d.Vy) > 0.05)
            {
                d.Vy = -d.Vy * restitution;
                d.Vx *= bounceVx;
                return;
            }

            d.Vy = 0;
            d.Vx *= settleVx;
            if (Math.Abs(d.Vx) < 0.01)
            {
                d.Vx = 0;
                d.Alive = false;
            }
        }

        /// <summary>One rocket tick: launch (a targeted rocket re-aims at the live parachutist with a lead), climb under the deceleration, hit-test canopy then body along its path, else detonate on the fuse/apex rules.</summary>
        private void StepRocket(RocketState r, int tick)
        {
            if (r.Done || tick < r.Spec.SpawnTick)
            {
                return;
            }

            if (!r.Launched)
            {
                Launch(r, tick);
            }

            var prevX = r.X;
            var prevY = r.Y;
            r.Vy += r.Spec.AccelerationY * Dt;
            r.X += r.Vx * Dt;
            r.Y += r.Vy * Dt;

            var charR = Math.Max(24.0, MinDim * 0.032);
            var rocketR = Math.Max(14.0, MinDim * 0.018);
            var ax = prevX * BoardW;
            var ay = prevY * BoardH;
            var bx = r.X * BoardW;
            var by = r.Y * BoardH;
            var bodyX = _x * BoardW;
            var bodyY = _y * BoardH;
            var hitCanopy = !_destroyed && SegmentHits(ax, ay, bx, by, bodyX, bodyY - (charR * 2.1), (charR * 1.65) + rocketR);
            var hitBody = !hitCanopy && SegmentHits(ax, ay, bx, by, bodyX, bodyY, charR + rocketR);
            if (hitCanopy || hitBody)
            {
                var kill = hitCanopy ? Roll() : (_cfg.DirectHitKillsParachute && Roll());
                if (kill && !_destroyed)
                {
                    _destroyed = true;
                    _freeFall = tick;
                    _mark = true;
                }

                var push = Math.Abs(r.Vx) > 0.05 ? Math.Sign(r.Vx) : (_rng.NextBool() ? 1 : -1);
                var dvx = Math.Clamp(((push * 0.18) + (r.Vx * 0.15)) * _cfg.RocketKnockbackMultiplier, -0.45, 0.45);
                var dvy = Math.Clamp(0.08 * _cfg.RocketKnockbackMultiplier, -0.20, 0.35);
                _vx = Math.Clamp(_vx + dvx, -0.65, 0.65);
                _vy = Math.Clamp(_vy + dvy, 0.04, 0.60);
                _rocketHits++;
                Collide(tick, hitCanopy ? 1 : 2, r.Spec.Index, _x, _y, dvx, dvy);
                r.Explode(tick, hitCanopy ? 1 : 2);
                return;
            }

            if (tick >= r.FuseTick || (!r.Spec.IsTargeted && r.Y <= r.TargetY) || r.Y <= 0.04 || (r.Vy >= 0 && r.Y < 0.85))
            {
                r.Explode(tick, 0);
            }
        }

        /// <summary>The overlay's launch-time re-aim: a targeted rocket leads the parachutist by the time to close the climb, and fuses 0.45 s after that.</summary>
        private void Launch(RocketState r, int tick)
        {
            r.Launched = true;
            if (r.Spec.IsTargeted && _y < LandingY - 0.05)
            {
                var canopyOffsetY = (Math.Max(24.0, MinDim * 0.032) * 2.1) / BoardH;
                var canopyY = _y - canopyOffsetY;
                var dy = Math.Max(0.08, Math.Abs(r.Spec.StartY - canopyY));
                var vAscent = Math.Max(0.45, Math.Abs(r.Vy));
                var vDescent = Math.Max(0.04, _vy);
                var lead = Math.Clamp(dy / (vAscent + vDescent), 0.40, 1.25);
                var predX = Math.Clamp(_x + (_vx * lead), 0.06, 0.94);
                var predY = Math.Max(0.02, Math.Min(LandingY - 0.04, canopyY + (_vy * lead)));
                r.TargetX = predX;
                r.TargetY = predY;
                r.Vx = (predX - r.Spec.StartX) / lead;
                r.Vy = ((predY - r.Spec.StartY) - (0.5 * r.Spec.AccelerationY * lead * lead)) / lead;
                r.FuseTick = tick + (int)Math.Round((lead + 0.45) * TickRateHz);
            }

            r.LaunchVx = r.Vx;
            r.LaunchVy = r.Vy;
        }

        /// <summary>A canopy-kill roll against <see cref="ParachuteConfig.CanopyKillChance"/> (always one seeded draw, even at 100%).</summary>
        private bool Roll() => _rng.NextDouble() < _cfg.CanopyKillChance;

        /// <summary>True when the segment a-b passes within <paramref name="minDist"/> px of the circle centre c (continuous collision, so a fast rocket cannot tunnel).</summary>
        private static bool SegmentHits(double ax, double ay, double bx, double by, double cx, double cy, double minDist)
        {
            var sx = bx - ax;
            var sy = by - ay;
            var u = Math.Clamp((((cx - ax) * sx) + ((cy - ay) * sy)) / Math.Max((sx * sx) + (sy * sy), 1e-9), 0.0, 1.0);
            var ex = cx - (ax + (u * sx));
            var ey = cy - (ay + (u * sy));
            return (ex * ex) + (ey * ey) <= minDist * minDist;
        }

        /// <summary>One parachutist tick: canopy drag and glide recovery (or free fall once the canopy is destroyed), motion, ceiling, wall bounces and the one-time target-rim bounce.</summary>
        private void StepParachutist(int tick)
        {
            if (_destroyed)
            {
                _vy = Math.Min(TerminalFreeFallVy, _vy + (1.10 * Dt));
                _vx = Math.Clamp(_vx * 0.994, -MaxVx * 1.5, MaxVx * 1.5);
            }
            else
            {
                _vx = Math.Clamp(_vx * 0.998, -MaxVx, MaxVx);
                _vy = _vy < _base.InitialVy
                    ? Math.Min(_base.InitialVy, _vy + (1.20 * Dt))
                    : Math.Clamp(_base.InitialVy + ((_vy - _base.InitialVy) * 0.96), 0.04, 0.45);
            }

            _x += _vx * Dt;
            _y += _vy * Dt;
            if (_y < StartY)
            {
                _y = StartY;
                _vy = Math.Max(0.0, _vy);
            }

            var wall = Math.Clamp(_cfg.Bounciness * 1.05, 0.15, 2.5);
            if (_x < 0.05 || _x > 0.95)
            {
                var left = _x < 0.05;
                _x = left ? 0.05 : 0.95;
                var speed = Math.Min(MaxVx, Math.Abs(_vx != 0 ? _vx : 0.12) * wall);
                _vx = left ? speed : -speed;
                _mark = true;
            }

            if (!_rimBounced && _y >= LandingY - 0.038 && _y < LandingY)
            {
                RimBounce();
            }
        }

        /// <summary>The target's side lip: a parachutist crossing it just outside the platform edge is bounced back or tipped over it, once per drop.</summary>
        private void RimBounce()
        {
            var dx = _x - _base.TargetCenterX;
            var dist = Math.Abs(dx);
            if (dist < _base.TargetHalfWidth * 0.93 || dist > _base.TargetHalfWidth * 1.07)
            {
                return;
            }

            _rimBounced = true;
            var bounce = _cfg.Bounciness;
            _vx = Math.Sign(_vx) == Math.Sign(dx) && Math.Abs(_vx) > 0.01
                ? Math.Clamp(-_vx * bounce, -MaxVx, MaxVx)
                : Math.Clamp((dx > 0 ? -1 : 1) * Math.Max(0.04, Math.Abs(_vx) * 0.95 * bounce), -MaxVx, MaxVx);
            _vy = Math.Max(-0.65, _vy * Math.Max(0.25, Math.Min(1.0, 0.65 * bounce)));
            _mark = true;
        }
    }

    /// <summary>Live state of one droplet (<see cref="DropletSpec"/> is its immutable start).</summary>
    private sealed class DropletState(DropletSpec spec)
    {
        public DropletSpec Spec { get; } = spec;
        public double X { get; set; } = spec.StartX;
        public double Y { get; set; } = spec.StartY;
        public double Vx { get; set; } = spec.Vx;
        public double Vy { get; set; } = spec.Vy;
        public bool Alive { get; set; } = true;
    }

    /// <summary>Live state of one rocket: its position and velocity, the launch-time re-aim, and how it ended.</summary>
    private sealed class RocketState(RocketSpec spec)
    {
        public RocketSpec Spec { get; } = spec;
        public double X { get; set; } = spec.StartX;
        public double Y { get; set; } = spec.StartY;
        public double Vx { get; set; } = spec.Vx;
        public double Vy { get; set; } = spec.Vy;
        public double TargetX { get; set; } = spec.TargetX;
        public double TargetY { get; set; } = spec.TargetY;
        public double LaunchVx { get; set; } = spec.Vx;
        public double LaunchVy { get; set; } = spec.Vy;
        public int FuseTick { get; set; } = spec.SpawnTick + spec.FuseTicks;
        public bool Launched { get; set; }
        public bool Done { get; private set; }
        private int _explodeTick = -1;
        private double _explodeX;
        private double _explodeY;
        private int _hit;

        /// <summary>Marks the rocket exploded at <paramref name="tick"/> where it is now, with the given hit kind (0 nothing, 1 canopy, 2 body).</summary>
        public void Explode(int tick, int hit)
        {
            Done = true;
            _explodeTick = tick;
            _explodeX = X;
            _explodeY = Y;
            _hit = hit;
        }

        /// <summary>The reportable flight: the effective (re-aimed) launch values and how it ended; a rocket that never exploded before touchdown reports explode tick -1.</summary>
        public RocketFlight ToFlight() => new(
            Spec with { TargetX = Round4(TargetX), TargetY = Round4(TargetY), Vx = Round4(LaunchVx), Vy = Round4(LaunchVy), FuseTicks = FuseTick - Spec.SpawnTick },
            _explodeTick, Round4(_explodeX), Round4(_explodeY), _hit);
    }
}

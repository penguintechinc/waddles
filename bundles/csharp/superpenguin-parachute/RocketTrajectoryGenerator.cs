using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinParachute;

/// <summary>
/// Ported from PenguinTwitchBot's `RocketTrajectoryGenerator` (`Commands/Games/Parachute/RocketTrajectoryGenerator.cs`,
/// github.com/Psychoboy/PenguinTwitchBot, MIT): builds the rockets of a volley for the `Targeted`, `Barrage` and `Random`
/// patterns (launch from below the screen, apex detonation heights, 72..91 tick fuses, 8-tick stagger). Draws come from the one
/// seeded generator; the target is always the player's own drop, so upstream's "pick a random mid-air drop" fallback is not needed.
/// </summary>
public static class RocketTrajectoryGenerator
{
    /// <summary>Upstream's firework palette.</summary>
    public static IReadOnlyList<string> Colors { get; } =
        ["#FF3366", "#FF9900", "#00F0FF", "#7000FF", "#39FF14", "#FFFF00", "#FF3333"];

    /// <summary>Launch height: below the bottom of the screen (upstream's `startY`).</summary>
    public const double StartY = 1.02;

    /// <summary>The slight deceleration a rocket feels as it climbs (upstream's `accelY`).</summary>
    public const double AccelerationY = 0.04;

    /// <summary>
    /// Creates <paramref name="count"/> rockets (1..10) of <paramref name="pattern"/>, the first launching at
    /// <paramref name="launchTick"/> on the parachutist's timeline and each next one 8 ticks later. An unknown pattern is `Random`, as upstream.
    /// </summary>
    public static List<RocketSpec> Generate(string pattern, int count, GlideBaseline target, int launchTick, Xoshiro256 rng)
    {
        var total = Math.Clamp(count, 1, 10);
        var rockets = new List<RocketSpec>(total);
        for (var i = 0; i < total; i++)
        {
            rockets.Add(Create(pattern, i, total, launchTick + (i * 8), target, rng));
        }

        return rockets;
    }

    private static RocketSpec Create(string pattern, int index, int total, int spawnTick, GlideBaseline target, Xoshiro256 rng)
    {
        var color = Colors[rng.NextInt(0, Colors.Count - 1)];
        if (pattern.Equals("Targeted", StringComparison.OrdinalIgnoreCase))
        {
            var groundSpread = (index - ((total - 1) / 2.0)) * 0.04;
            var startX = Math.Clamp(target.LandingX + groundSpread, 0.08, 0.92);
            var apex = rng.NextDouble(0.10, 0.25);
            var fuse = rng.NextInt(72, 91);
            var rounded = Round4(startX);
            return Build("Targeted", true, index, spawnTick, rounded, rounded, apex, 0.0, fuse, color);
        }

        if (pattern.Equals("Barrage", StringComparison.OrdinalIgnoreCase))
        {
            var step = 0.70 / Math.Max(1, total);
            var startX = Math.Clamp(0.15 + (index * step) + rng.NextDouble(-0.02, 0.02), 0.08, 0.92);
            var vx = (0.50 - startX) * rng.NextDouble(0.15, 0.30);
            var apex = rng.NextDouble(0.06, 0.18);
            var fuse = rng.NextInt(72, 91);
            return Build("Barrage", false, index, spawnTick, startX, Math.Clamp(startX + (vx * fuse * ParachuteSimulationEngine.Dt), 0.05, 0.95), apex, vx, fuse, color);
        }

        var randomX = rng.NextDouble(0.12, 0.88);
        var randomVx = rng.NextDouble(-0.20, 0.20);
        var randomApex = rng.NextDouble(0.06, 0.18);
        var randomFuse = rng.NextInt(72, 91);
        return Build("Random", false, index, spawnTick, randomX, Math.Clamp(randomX + (randomVx * randomFuse * ParachuteSimulationEngine.Dt), 0.05, 0.95), randomApex, randomVx, randomFuse, color);
    }

    /// <summary>Builds a spec, solving the launch velocity so the rocket reaches the apex height in exactly <paramref name="fuse"/> ticks under the climb deceleration.</summary>
    private static RocketSpec Build(string pattern, bool targeted, int index, int spawnTick, double startX, double targetX, double apex, double vx, int fuse, string color)
    {
        var duration = fuse * ParachuteSimulationEngine.Dt;
        var vy = ((apex - StartY) - (0.5 * AccelerationY * duration * duration)) / duration;
        return new RocketSpec(index, spawnTick, Round4(startX), StartY, Round4(targetX), Round4(apex), Round4(vx), Round4(vy), AccelerationY, fuse, color, pattern, targeted);
    }

    private static double Round4(double v) => ParachuteSimulationEngine.Round4(v);
}

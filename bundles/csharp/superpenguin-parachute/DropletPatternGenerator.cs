using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinParachute;

/// <summary>
/// Ported from PenguinTwitchBot's `DropletPatternGenerator` (`Commands/Games/Parachute/DropletPatternGenerator.cs`,
/// github.com/Psychoboy/PenguinTwitchBot, MIT): resolves the size/speed presets of a droplet and computes its spawn position and
/// velocity per pattern (`Targeted`, `SideWind`, `Fountain`, default `TopRain`). Upstream draws from the OS CSPRNG and the wall
/// clock; here every draw comes from the one seeded generator and "now" is the wave's tick on the parachutist's own timeline, so
/// the same seed gives the same wave. Upstream's untargeted-fountain branch (no parachutist in the air) cannot occur: a wave is
/// always aimed at the player's own drop.
/// </summary>
public static class DropletPatternGenerator
{
    private static readonly string[] AllSizes = ["Small", "Medium", "Large", "Giant"];
    private static readonly string[] SpeedChoices = ["Slow", "Normal", "Fast", "Hyper"];

    /// <summary>
    /// Resolves a size preset (`Small`/`Medium`/`Large`/`Giant`, `Random`, `Random:A,B` or a comma list) to (label, scale, radius, mass),
    /// exactly upstream's table; random pools pick with upstream's weights Small 25, Medium 50, Large 17, Giant 8.
    /// </summary>
    public static (string Label, double Scale, double Radius, double Mass) ResolveSize(string preset, IReadOnlyList<string>? allowedRandomSizes, Xoshiro256 rng)
    {
        var normalized = preset.Trim();
        if (normalized.StartsWith("Random:", StringComparison.OrdinalIgnoreCase))
        {
            normalized = PickFromAllowed(normalized["Random:".Length..].Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries), rng);
        }
        else if (normalized.Equals("Random", StringComparison.OrdinalIgnoreCase))
        {
            normalized = PickFromAllowed(allowedRandomSizes ?? [], rng);
        }
        else if (normalized.Contains(','))
        {
            normalized = PickFromAllowed(normalized.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries), rng);
        }

        return normalized.ToLowerInvariant() switch
        {
            "small" => ("Small", 0.60, 0.018, 1.80),
            "large" => ("Large", 1.50, 0.042, 5.50),
            "giant" => ("Giant", 2.00, 0.058, 8.50),
            _ => ("Medium", 1.00, 0.028, 3.20),
        };
    }

    /// <summary>Picks one valid size from <paramref name="candidates"/> by upstream's weights; an empty/invalid pool means all four sizes, a single one is returned without a draw.</summary>
    internal static string PickFromAllowed(IEnumerable<string> candidates, Xoshiro256 rng)
    {
        var valid = new List<string>();
        foreach (var raw in candidates)
        {
            var match = AllSizes.FirstOrDefault(s => s.Equals(raw?.Trim(), StringComparison.OrdinalIgnoreCase));
            if (match is not null && !valid.Contains(match))
            {
                valid.Add(match);
            }
        }

        if (valid.Count == 0)
        {
            valid.AddRange(AllSizes);
        }

        if (valid.Count == 1)
        {
            return valid[0];
        }

        var total = valid.Sum(Weight);
        var roll = rng.NextInt(0, total - 1);
        var pick = valid[^1];
        var accum = 0;
        foreach (var size in valid)
        {
            accum += Weight(size);
            if (roll < accum)
            {
                pick = size;
                break;
            }
        }

        return pick;
    }

    private static int Weight(string size) => size switch
    {
        "Small" => 25,
        "Medium" => 50,
        "Large" => 17,
        _ => 8,
    };

    /// <summary>Resolves a speed preset (`Slow` 0.65, `Normal` 1.0, `Fast` 1.6, `Hyper` 2.2, or `Random` = uniform over the four) to (label, scale).</summary>
    public static (string Label, double Scale) ResolveSpeed(string preset, Xoshiro256 rng)
    {
        var normalized = preset.Trim();
        if (normalized.Equals("Random", StringComparison.OrdinalIgnoreCase))
        {
            normalized = SpeedChoices[rng.NextInt(0, SpeedChoices.Length - 1)];
        }

        return normalized.ToLowerInvariant() switch
        {
            "slow" => ("Slow", 0.65),
            "fast" => ("Fast", 1.60),
            "hyper" => ("Hyper", 2.20),
            _ => ("Normal", 1.0),
        };
    }

    /// <summary>
    /// Computes (startX, startY, vx, vy, gravity) of droplet <paramref name="index"/> for <paramref name="pattern"/>.
    /// <paramref name="elapsedTick"/> is the parachutist's tick when the wave starts and <paramref name="spawnTickOffset"/> the
    /// droplet's stagger; aimed patterns lead the baseline path exactly like upstream (`Targeted` 24 ticks ahead, `Fountain` 16).
    /// </summary>
    public static (double StartX, double StartY, double Vx, double Vy, double Gravity) CalculateTrajectory(
        string pattern, int index, double speedScale, GlideBaseline target, int elapsedTick, int spawnTickOffset, Xoshiro256 rng)
    {
        var dt = ParachuteSimulationEngine.Dt;
        var currentTick = Math.Clamp(elapsedTick + spawnTickOffset, 0, target.TotalTicks - 10);
        if (pattern.Equals("Targeted", StringComparison.OrdinalIgnoreCase))
        {
            var point = ParachuteSimulationEngine.GlidePosition(target, Math.Min(target.TotalTicks - 5, currentTick + 24));
            var fromLeft = (index % 2 == 0) ? (point.X > 0.35) : (point.X < 0.65);
            var startX = fromLeft ? Math.Max(0.04, point.X - 0.28) : Math.Min(0.96, point.X + 0.28);
            var yOffset = (index % 3 == 0) ? -0.06 : ((index % 3 == 1) ? 0.08 : -0.18);
            var startY = Math.Clamp(point.Y + yOffset, 0.04, 0.88);
            var intercept = 24.0 * dt / Math.Max(0.6, speedScale);
            return (startX, startY, (point.X - startX) / intercept, (point.Y - startY) / intercept, 0.08 * speedScale);
        }

        if (pattern.Equals("SideWind", StringComparison.OrdinalIgnoreCase))
        {
            var fromLeft = index % 2 == 0;
            var startY = rng.NextDouble(0.12, 0.62);
            var vx = (fromLeft ? 1.0 : -1.0) * rng.NextDouble(0.32, 0.52) * speedScale;
            var vy = rng.NextDouble(0.12, 0.26) * speedScale;
            return (fromLeft ? 0.02 : 0.98, startY, vx, vy, 0.14 * speedScale);
        }

        if (pattern.Equals("Fountain", StringComparison.OrdinalIgnoreCase))
        {
            var point = ParachuteSimulationEngine.GlidePosition(target, Math.Min(target.TotalTicks - 5, currentTick + 16));
            var startX = Math.Clamp(point.X + (index % 2 == 0 ? -0.015 : 0.015), 0.06, 0.94);
            var intercept = 16.0 * dt / Math.Max(0.6, speedScale);
            var vy = -rng.NextDouble(0.85, 1.15) * speedScale;
            return (startX, 0.94, (point.X - startX) / intercept, vy, 0.38 * speedScale);
        }

        var x = rng.NextDouble(0.06, 0.94);
        var rainVx = rng.NextDouble(-0.35, 0.35) * speedScale;
        var rainVy = rng.NextDouble(0.06, 0.20) * speedScale;
        return (x, -0.06, rainVx, rainVy, 0.32 * speedScale);
    }
}

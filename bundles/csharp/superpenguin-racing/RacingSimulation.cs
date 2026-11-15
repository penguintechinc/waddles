using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinRacing;

/// <summary>
/// The tunable parameters of one race, recorded by id (<see cref="Id"/>) with every race so a seed is only ever replayed under the
/// config it was run with. Defaults follow upstream's Racing settings (2-3 laps, lobby 60 s, 12 racers per heat, drafting 6 %,
/// rubber-banding 0.25, 6 debris hazards, boost pads); the upstream tournament/derby/elimination modes are not ported.
/// </summary>
public sealed record RaceConfig
{
    /// <summary>The game-config revision recorded with every race and join (a seed must be replayed under the same revision).</summary>
    public const string Id = "racing.v1";

    /// <summary>The default configuration (upstream defaults).</summary>
    public static RaceConfig Default { get; } = new();

    /// <summary>Laps to run; clamped to 1..10 like upstream.</summary>
    public int Laps { get; init; } = 3;

    /// <summary>Fewest racers for a real race; with fewer the window is voided and every stake refunded.</summary>
    public int MinRacers { get; init; } = 2;

    /// <summary>Largest starting grid (upstream MaxRacersPerHeat default 12; the multi-heat tournament is not ported).</summary>
    public int MaxRacers { get; init; } = 12;

    /// <summary>How long the join window stays open after the first joiner (upstream default 60 s).</summary>
    public uint LobbySeconds { get; init; } = 60;

    /// <summary>A window picked up more than this long after its deadline is voided and refunded instead of raced (tick-on-event lateness).</summary>
    public uint StaleVoidSeconds { get; init; } = 900;

    /// <summary>Track length of one lap in track units (the overlay scales it to its track).</summary>
    public double TrackLength { get; init; } = 1000.0;

    /// <summary>Seconds a car of average power needs for one clear lap.</summary>
    public double LapSeconds { get; init; } = 18.0;

    /// <summary>Fixed simulation step in milliseconds.</summary>
    public int StepMillis { get; init; } = 50;

    /// <summary>Keyframe sampling interval in milliseconds (the overlay interpolates between samples).</summary>
    public int SampleMillis { get; init; } = 1000;

    /// <summary>Hard time limit of a race; racers still running are ranked by distance as DNF (never reached with defaults).</summary>
    public int MaxRaceMillis { get; init; } = 600_000;

    /// <summary>Oil/debris hazards per lap.</summary>
    public int DebrisPerLap { get; init; } = 6;

    /// <summary>Turbo boost pads per lap.</summary>
    public int BoostsPerLap { get; init; } = 3;

    /// <summary>Distance behind another car within which a car gets the slipstream boost.</summary>
    public double DraftRange { get; init; } = 25.0;

    /// <summary>Speed bonus of a slipstream.</summary>
    public double DraftBoost { get; init; } = 0.06;

    /// <summary>Catch-up strength: speed factor is 1 + this x (field average - own position) / track length, clamped to +-10 %.</summary>
    public double RubberBand { get; init; } = 0.25;

    /// <summary>Chance per second of a spontaneous spinout (upstream CollisionSpinoutChance).</summary>
    public double SpinChancePerSecond { get; init; } = 0.012;

    /// <summary>Chance a car spins out when it crosses a debris hazard.</summary>
    public double DebrisSpinChance { get; init; } = 0.55;

    /// <summary>How long a spinout lasts, in seconds.</summary>
    public double SpinSeconds { get; init; } = 1.0;

    /// <summary>How long a boost pad lasts, in seconds.</summary>
    public double BoostSeconds { get; init; } = 1.5;

    /// <summary>Speed factor while boosted.</summary>
    public double BoostFactor { get; init; } = 1.25;

    /// <summary>Distance between starting-grid slots (cars start staggered behind the line).</summary>
    public double GridGap { get; init; } = 3.0;

    /// <summary>The config with its ranges clamped to what the simulation and the host limits support.</summary>
    public RaceConfig Clamped() => this with
    {
        Laps = Math.Clamp(Laps, 1, 10),
        MinRacers = Math.Clamp(MinRacers, 2, 30),
        MaxRacers = Math.Clamp(MaxRacers, Math.Max(2, MinRacers), 30),
        LobbySeconds = Math.Clamp(LobbySeconds, 15u, 180u),
    };
}

/// <summary>One lap-periodic event on the track (a debris hazard or a boost pad) with the unit position it sits at.</summary>
public sealed record RaceEvent(int TimeMs, int Lane, string Kind);

/// <summary>
/// The full outcome of one simulated race: <see cref="Order"/> is lanes (0-based) from first to last; <see cref="FinishMs"/> and
/// <see cref="Dnf"/> are per lane; <see cref="Keyframes"/> per lane are `[ms, position x10]` samples; positions are track units x10
/// so the overlay needs no floating point on the wire.
/// </summary>
public sealed record RaceOutcome(
    IReadOnlyList<int> Order,
    IReadOnlyList<long> FinishMs,
    IReadOnlyList<bool> Dnf,
    IReadOnlyList<int> PowerMilli,
    IReadOnlyList<int> DebrisPos10,
    IReadOnlyList<int> BoostPos10,
    IReadOnlyList<IReadOnlyList<(int TimeMs, int Pos10)>> Keyframes,
    IReadOnlyList<RaceEvent> Events,
    int TotalMs);

/// <summary>
/// The pure, deterministic race simulation (replaces upstream's in-browser physics, which made the overlay the source of truth):
/// the same seed, racer count and config always give byte-identical placements, finish times and keyframes. All randomness comes
/// from ONE <see cref="Xoshiro256"/> drawn in a fixed order; only + - * / and floor are used so the result is platform-exact.
/// Model: each car has a power (0.94-1.06) and a slowly varying form (+-4 %, redrawn every second), gets a slipstream bonus when
/// close behind another car, catch-up rubber-banding, boost pads, and spins out on debris or at random (upstream's drafting,
/// rubber-banding, debris, boost and collision-spinout mechanics, without car-to-car contact physics).
/// </summary>
public static class RacingSimulation
{
    /// <summary>Runs the race for <paramref name="racers"/> cars (lane = index) from <paramref name="seed"/> under <paramref name="config"/>.</summary>
    public static RaceOutcome Run(ulong seed, int racers, RaceConfig config)
    {
        if (racers < 2)
        {
            throw new ArgumentOutOfRangeException(nameof(racers), "a race needs at least two cars");
        }

        var rng = new Xoshiro256(seed);
        var length = config.TrackLength;
        var target = config.Laps * length;
        var baseSpeed = length / config.LapSeconds;
        var dt = config.StepMillis / 1000.0;

        var power = new double[racers];
        for (var i = 0; i < racers; i++)
        {
            power[i] = 0.94 + (0.12 * rng.NextDouble());
        }

        var debris = Hazards(rng, config.DebrisPerLap, length);
        var boosts = Hazards(rng, config.BoostsPerLap, length);

        var pos = new double[racers];
        var form = new double[racers];
        var spin = new double[racers];
        var boost = new double[racers];
        var done = new bool[racers];
        var finishMs = new long[racers];
        var keyframes = new List<(int, int)>[racers];
        for (var i = 0; i < racers; i++)
        {
            pos[i] = -i * config.GridGap;
            keyframes[i] = [];
        }

        List<RaceEvent> events = [];
        var stepsPerSample = Math.Max(1, config.SampleMillis / config.StepMillis);
        var stepsPerForm = Math.Max(1, 1000 / config.StepMillis);
        var finished = 0;
        var step = 0;
        var tMs = 0;
        while (finished < racers && tMs < config.MaxRaceMillis)
        {
            tMs = step * config.StepMillis;
            if (step % stepsPerSample == 0)
            {
                for (var i = 0; i < racers; i++)
                {
                    if (!done[i])
                    {
                        keyframes[i].Add((tMs, Pos10(pos[i])));
                    }
                }
            }

            var average = 0.0;
            for (var i = 0; i < racers; i++)
            {
                average += pos[i];
            }

            average /= racers;
            var next = new double[racers];
            for (var i = 0; i < racers; i++)
            {
                next[i] = pos[i];
                if (done[i])
                {
                    continue;
                }

                if (step % stepsPerForm == 0)
                {
                    form[i] = (rng.NextDouble() - 0.5) * 0.08;
                }

                double speed;
                if (spin[i] > 0)
                {
                    speed = baseSpeed * 0.15;
                    spin[i] -= dt;
                }
                else
                {
                    speed = baseSpeed * power[i] * (1.0 + form[i]);
                    for (var j = 0; j < racers; j++)
                    {
                        var gap = pos[j] - pos[i];
                        if (j != i && gap > 0 && gap < config.DraftRange)
                        {
                            speed *= 1.0 + config.DraftBoost;
                            break;
                        }
                    }

                    var catchUp = Math.Clamp(config.RubberBand * (average - pos[i]) / length, -0.1, 0.1);
                    speed *= 1.0 + catchUp;
                    if (boost[i] > 0)
                    {
                        speed *= config.BoostFactor;
                        boost[i] -= dt;
                    }

                    if (rng.NextDouble() < config.SpinChancePerSecond * dt)
                    {
                        spin[i] = config.SpinSeconds;
                        events.Add(new RaceEvent(tMs, i, "spin"));
                    }
                }

                next[i] = pos[i] + (speed * dt);
                if (spin[i] <= 0)
                {
                    foreach (var d in debris)
                    {
                        if (Crossed(pos[i], next[i], d, length) && rng.NextDouble() < config.DebrisSpinChance)
                        {
                            spin[i] = config.SpinSeconds;
                            events.Add(new RaceEvent(tMs, i, "spin"));
                            break;
                        }
                    }

                    foreach (var b in boosts)
                    {
                        if (Crossed(pos[i], next[i], b, length))
                        {
                            boost[i] = config.BoostSeconds;
                            events.Add(new RaceEvent(tMs, i, "boost"));
                            break;
                        }
                    }
                }

                if (next[i] >= target)
                {
                    var fraction = (target - pos[i]) / (next[i] - pos[i]);
                    finishMs[i] = (long)Math.Floor((((step + fraction) * config.StepMillis)) + 0.5);
                    done[i] = true;
                    finished++;
                    keyframes[i].Add(((int)finishMs[i], Pos10(target)));
                    next[i] = target;
                }
            }

            pos = next;
            step++;
        }

        var dnf = new bool[racers];
        var order = Enumerable.Range(0, racers)
            .OrderBy(i => done[i] ? 0 : 1)
            .ThenBy(i => done[i] ? finishMs[i] : 0)
            .ThenByDescending(i => done[i] ? 0.0 : pos[i])
            .ThenBy(i => i)
            .ToList();
        for (var i = 0; i < racers; i++)
        {
            if (!done[i])
            {
                dnf[i] = true;
                finishMs[i] = -1;
                keyframes[i].Add((tMs, Pos10(pos[i])));
            }
        }

        var total = (int)Math.Max(finishMs.Max(), dnf.Any(x => x) ? tMs : 0);
        return new RaceOutcome(
            order,
            finishMs,
            dnf,
            power.Select(p => (int)Math.Floor((p * 1000.0) + 0.5)).ToList(),
            debris.Select(Pos10).ToList(),
            boosts.Select(Pos10).ToList(),
            keyframes.Select(k => (IReadOnlyList<(int, int)>)k).ToList(),
            events,
            total);
    }

    /// <summary>`count` positions spread over one lap, each jittered inside its own slice so hazards never overlap.</summary>
    private static double[] Hazards(Xoshiro256 rng, int count, double length)
    {
        var slice = length / Math.Max(1, count);
        var result = new double[count];
        for (var k = 0; k < count; k++)
        {
            result[k] = (k * slice) + (slice * (0.15 + (0.7 * rng.NextDouble())));
        }

        return result;
    }

    /// <summary>True when moving from <paramref name="from"/> to <paramref name="to"/> crossed the lap-periodic marker at <paramref name="marker"/>.</summary>
    private static bool Crossed(double from, double to, double marker, double length) =>
        Math.Floor((to - marker) / length) > Math.Floor((from - marker) / length);

    private static int Pos10(double position) => (int)Math.Floor((position * 10.0) + 0.5);
}

/// <summary>
/// The pot split. The pot is the sum of the racers' stakes (settled zero-sum: nothing is minted); 1st/2nd/3rd take 60/30/10 percent
/// with four or more racers, 70/30 with three, and the winner takes all with two; integer rounding dust goes to first place.
/// </summary>
public static class RacingPayouts
{
    /// <summary>Percent shares by finishing place for <paramref name="racers"/> cars (empty for fewer than two).</summary>
    public static IReadOnlyList<int> Shares(int racers) => racers switch
    {
        < 2 => [],
        2 => [100],
        3 => [70, 30],
        _ => [60, 30, 10],
    };

    /// <summary>Payout per finishing place (index 0 = winner) for a <paramref name="pot"/> over <paramref name="racers"/> cars; sums to the pot exactly.</summary>
    public static IReadOnlyList<ulong> Split(ulong pot, int racers)
    {
        var shares = Shares(racers);
        var payouts = new ulong[racers];
        ulong paid = 0;
        for (var place = 0; place < shares.Count; place++)
        {
            payouts[place] = pot * (ulong)shares[place] / 100;
            paid += payouts[place];
        }

        if (shares.Count > 0)
        {
            payouts[0] += pot - paid;
        }

        return payouts;
    }
}

namespace WaddleBundleSuperpenguinParachute;

/// <summary>
/// The straight-glide ("uncollided") flight of one parachutist, ported from `ParachuteSimulationEngine.SimulateParachuteTrajectory`:
/// where the drop starts, how fast it glides, how many 60 Hz ticks the descent takes, the target platform, and the baseline
/// keyframes. The hazard generators aim at this path (upstream aims at the same baseline via `GetParachutePositionAtTick`); the
/// authoritative result comes from the collision-aware step simulation in <see cref="ParachuteSimulationEngine.Run"/>.
/// </summary>
public sealed record GlideBaseline(
    double StartX,
    double StartY,
    double LandingY,
    double InitialVx,
    double InitialVy,
    int TotalTicks,
    double TargetCenterX,
    double TargetHalfWidth,
    double LandingX,
    IReadOnlyList<GlidePoint> Keyframes);

/// <summary>One baseline keyframe: tick and normalized board position (0..1 on both axes).</summary>
public readonly record struct GlidePoint(int Tick, double X, double Y);

/// <summary>
/// One droplet of the seeded hazard wave (upstream `DropletInstanceDto`): when it spawns (<see cref="SpawnTick"/>), its start
/// position and velocity (normalized board units, per second), gravity, radius, knockback mass, and the size/speed presets it
/// was resolved from.
/// </summary>
public sealed record DropletSpec(
    int Index,
    int SpawnTick,
    double StartX,
    double StartY,
    double Vx,
    double Vy,
    double Gravity,
    double Radius,
    double Mass,
    string SizeLabel,
    double SizeScale,
    string SpeedLabel,
    double SpeedScale,
    string Pattern);

/// <summary>
/// One rocket of the seeded volley (upstream `RocketInstanceDto`): launch tick, start position, velocity, the upward drag
/// acceleration, the fuse length in ticks, colour and the pattern it belongs to. A targeted rocket's velocity and fuse are
/// re-aimed at launch from the live parachutist (as upstream's overlay does); the re-aimed values are what a result reports.
/// </summary>
public sealed record RocketSpec(
    int Index,
    int SpawnTick,
    double StartX,
    double StartY,
    double TargetX,
    double TargetY,
    double Vx,
    double Vy,
    double AccelerationY,
    int FuseTicks,
    string Color,
    string Pattern,
    bool IsTargeted);

/// <summary>What a rocket did: its effective <see cref="Spec"/>, where and when it exploded and what it hit (0 nothing, 1 canopy, 2 body).</summary>
public sealed record RocketFlight(RocketSpec Spec, int ExplodeTick, double ExplodeX, double ExplodeY, int Hit);

/// <summary>One path keyframe in 1/10000 board units (ints so the result is byte-exact), 60 Hz ticks.</summary>
public readonly record struct KeyPoint(int Tick, int X, int Y, int Vx, int Vy);

/// <summary>
/// One recorded impulse on the parachutist: <see cref="Kind"/> is 0 droplet, 1 rocket-on-canopy, 2 rocket-on-body;
/// <see cref="Source"/> the droplet/rocket index; position and velocity change in 1/10000 board units.
/// </summary>
public readonly record struct CollisionEvent(int Tick, int Kind, int Source, int X, int Y, int Dvx, int Dvy);

/// <summary>
/// The full, deterministic outcome of one drop: the glide parameters, the target, the landing (tick, x, score, hit/bullseye/win),
/// whether and when the canopy was destroyed, how many droplets/rockets hit, and the path keyframes, collision events and the
/// hazard specs a client replays. Scores follow upstream (0 miss, 1 outer edge .. 100 dead centre; crash landing per mode).
/// </summary>
public sealed record ParachuteResult(
    double StartX,
    double InitialVx,
    double InitialVy,
    int TotalTicks,
    double TargetCenterX,
    double TargetHalfWidth,
    double BullseyeHalfWidth,
    double LandingX,
    int LandingTick,
    int Score,
    bool IsHit,
    bool IsBullseye,
    bool IsWin,
    bool Destroyed,
    int FreeFallStartTick,
    int DropletHits,
    int RocketHits,
    string DropletPattern,
    string RocketPattern,
    IReadOnlyList<KeyPoint> Keyframes,
    IReadOnlyList<CollisionEvent> Collisions,
    IReadOnlyList<DropletSpec> Droplets,
    IReadOnlyList<RocketFlight> Rockets);

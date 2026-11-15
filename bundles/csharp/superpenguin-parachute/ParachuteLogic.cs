using System.Buffers;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinParachute;

/// <summary>
/// `!parachute amount|all|max`, ported from PenguinTwitchBot's Parachute Drop game (`Commands/Games/Parachute/`:
/// `ParachuteDropService.cs`, `ParachuteSimulationEngine.cs`, `DropletPatternGenerator.cs`, `RocketTrajectoryGenerator.cs`, the
/// Parachute Drop/Droplets/Rockets sub-actions and the overlay `wwwroot/parachute.html` + `js/parachute.js`;
/// github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`).
/// A thin game on the shared <see cref="WageredGameBase"/>: the host plumbing (flag, identity, stake, cooldown, seed, replay record,
/// the single wager, overlay push, logs) lives in `superpenguin-dropgame-core`; this class maps a seed to a drop and a score to a payout.
///
/// <para><b>Deviations.</b> Upstream is free play: the viewer's `!drop` glides down, droplets/rockets are fired by a streamer or mod
/// action, and the 0..100 landing score feeds trigger variables. Here every wagered drop carries its own seeded hazard wave and
/// the score pays a multiple of the stake, see <see cref="PayoutFor"/>. Upstream's emote/theme arguments, timed sessions and
/// multi-parachutist rounds need overlay/host support that does not exist yet and are not ported; the cooldown is upstream's 10 s.</para>
/// </summary>
public sealed class ParachuteLogic : WageredGameBase
{
    internal const string Game = "parachute";
    internal const string FlagKey = "waddles.command-superpenguin-parachute";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 10;
    internal const string ConfigId = "parachute.v1";

    /// <summary>
    /// The payout in hundredths of a percent of the stake per score point: payout = stake * score * 700 / 10000, so score 100 (a
    /// bullseye) pays 7x, score 50 pays 3.5x, score 1 pays 0.07x. Tuned so the return to player is about 0.93 (see README, "Money").
    /// </summary>
    internal const ulong PayoutPerScorePointBp = 700;

    private readonly ParachuteConfig _config;

    /// <summary>Creates the game with upstream's default settings (and the port's hazard defaults); tests inject the entropy source and a config.</summary>
    public ParachuteLogic(Func<ulong>? entropy = null, ParachuteConfig? config = null)
        : base(Spec, entropy)
    {
        _config = (config ?? ParachuteConfig.Default).Clamped();
    }

    private static GameSpec Spec { get; } = new(
        Game,
        ["parachute"],
        FlagKey,
        MaxBet,
        CooldownSeconds,
        "To parachute please do !parachute Amount/All/Max replacing amount with how many you would like to risk.",
        ConfigId,
        new OverlayWidget(Game, "Parachute Drop", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, ParachuteConfig.Width, ParachuteConfig.Height)));

    /// <summary>
    /// The payout for a landing <paramref name="score"/> (0 missed, 1..100 on the platform, already halved/zeroed for a crash
    /// landing by the simulation): `stake * score * 7 / 100`, rounded down. Never above 7x, far below the host ledger's 100x ceiling.
    /// </summary>
    internal static ulong PayoutFor(ulong stake, int score) =>
        score <= 0 ? 0 : stake * (ulong)Math.Min(score, 100) * PayoutPerScorePointBp / 10000;

    /// <inheritdoc />
    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        var result = ParachuteSimulationEngine.Run(seed, _config);
        var payout = PayoutFor(stake, result.Score);
        return new GameRound(payout, OutcomeText(result, stake, payout), DataJson(result, stake, payout));
    }

    /// <summary>The chat description of a drop: where it landed, what hit it, and the payout multiple when it paid.</summary>
    internal static string OutcomeText(ParachuteResult r, ulong stake, ulong payout)
    {
        var crash = r.Destroyed ? " after a rocket destroyed the parachute" : string.Empty;
        var hazards = r.DropletHits + r.RocketHits > 0 ? $" ({r.DropletHits} droplet and {r.RocketHits} rocket hits)" : string.Empty;
        if (r.IsWin)
        {
            return $"landed on the platform with a score of {r.Score}{crash}{hazards} (x{payout * 100 / stake / 100.0:0.##})";
        }

        var miss = r.Destroyed ? (r.IsHit ? "crash landed on the platform and scored nothing" : "crash landed off the platform") : "missed the platform";
        return miss + hazards;
    }

    /// <summary>Serializes the game-specific part of the overlay envelope (documented in `overlay/SCHEMA.md`): positions are ints in 1/10000 board units.</summary>
    internal static string DataJson(ParachuteResult r, ulong stake, ulong payout)
    {
        var buffer = new ArrayBufferWriter<byte>(8192);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("board");
            w.WriteNumber("w", ParachuteConfig.Width);
            w.WriteNumber("h", ParachuteConfig.Height);
            w.WriteNumber("tickRate", 60);
            w.WriteNumber("totalTicks", r.TotalTicks);
            w.WriteNumber("targetCenterX", Scaled(r.TargetCenterX));
            w.WriteNumber("targetHalfWidth", Scaled(r.TargetHalfWidth));
            w.WriteNumber("bullseyeHalfWidth", Scaled(r.BullseyeHalfWidth));
            w.WriteEndObject();
            w.WriteStartObject("drop");
            w.WriteNumber("startX", Scaled(r.StartX));
            w.WriteNumber("initialVx", Scaled(r.InitialVx));
            w.WriteNumber("initialVy", Scaled(r.InitialVy));
            w.WriteEndObject();
            w.WriteNumber("landingX", Scaled(r.LandingX));
            w.WriteNumber("landingTick", r.LandingTick);
            w.WriteNumber("score", r.Score);
            w.WriteBoolean("hit", r.IsHit);
            w.WriteBoolean("bullseye", r.IsBullseye);
            w.WriteBoolean("win", r.IsWin);
            w.WriteBoolean("destroyed", r.Destroyed);
            w.WriteNumber("freeFallTick", r.FreeFallStartTick);
            w.WriteNumber("dropletHits", r.DropletHits);
            w.WriteNumber("rocketHits", r.RocketHits);
            w.WriteNumber("multiplierPct", stake == 0 ? 0 : (long)(payout * 100 / stake));
            w.WriteString("dropletPattern", r.DropletPattern);
            w.WriteString("rocketPattern", r.RocketPattern);
            WriteKeyframes(w, r);
            WriteCollisions(w, r);
            WriteDroplets(w, r);
            WriteRockets(w, r);
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    private static int Scaled(double v) => ParachuteSimulationEngine.Scaled(v);

    private static void WriteKeyframes(Utf8JsonWriter w, ParachuteResult r)
    {
        w.WriteStartArray("keyframes");
        foreach (var k in r.Keyframes)
        {
            w.WriteStartArray();
            w.WriteNumberValue(k.Tick);
            w.WriteNumberValue(k.X);
            w.WriteNumberValue(k.Y);
            w.WriteNumberValue(k.Vx);
            w.WriteNumberValue(k.Vy);
            w.WriteEndArray();
        }

        w.WriteEndArray();
    }

    private static void WriteCollisions(Utf8JsonWriter w, ParachuteResult r)
    {
        w.WriteStartArray("collisions");
        foreach (var c in r.Collisions)
        {
            w.WriteStartArray();
            w.WriteNumberValue(c.Tick);
            w.WriteNumberValue(c.Kind);
            w.WriteNumberValue(c.Source);
            w.WriteNumberValue(c.X);
            w.WriteNumberValue(c.Y);
            w.WriteNumberValue(c.Dvx);
            w.WriteNumberValue(c.Dvy);
            w.WriteEndArray();
        }

        w.WriteEndArray();
    }

    private static void WriteDroplets(Utf8JsonWriter w, ParachuteResult r)
    {
        w.WriteStartArray("droplets");
        foreach (var d in r.Droplets)
        {
            w.WriteStartObject();
            w.WriteNumber("i", d.Index);
            w.WriteNumber("t", d.SpawnTick);
            w.WriteNumber("x", Scaled(d.StartX));
            w.WriteNumber("y", Scaled(d.StartY));
            w.WriteNumber("vx", Scaled(d.Vx));
            w.WriteNumber("vy", Scaled(d.Vy));
            w.WriteNumber("g", Scaled(d.Gravity));
            w.WriteNumber("r", Scaled(d.Radius));
            w.WriteNumber("m", Scaled(d.Mass));
            w.WriteString("size", d.SizeLabel);
            w.WriteString("speed", d.SpeedLabel);
            w.WriteEndObject();
        }

        w.WriteEndArray();
    }

    private static void WriteRockets(Utf8JsonWriter w, ParachuteResult r)
    {
        w.WriteStartArray("rockets");
        foreach (var f in r.Rockets)
        {
            var s = f.Spec;
            w.WriteStartObject();
            w.WriteNumber("i", s.Index);
            w.WriteNumber("t", s.SpawnTick);
            w.WriteNumber("x", Scaled(s.StartX));
            w.WriteNumber("y", Scaled(s.StartY));
            w.WriteNumber("tx", Scaled(s.TargetX));
            w.WriteNumber("ty", Scaled(s.TargetY));
            w.WriteNumber("vx", Scaled(s.Vx));
            w.WriteNumber("vy", Scaled(s.Vy));
            w.WriteNumber("ay", Scaled(s.AccelerationY));
            w.WriteNumber("fuse", s.FuseTicks);
            w.WriteString("color", s.Color);
            w.WriteString("pattern", s.Pattern);
            w.WriteBoolean("targeted", s.IsTargeted);
            w.WriteNumber("explodeTick", f.ExplodeTick);
            w.WriteNumber("ex", Scaled(f.ExplodeX));
            w.WriteNumber("ey", Scaled(f.ExplodeY));
            w.WriteNumber("hit", f.Hit);
            w.WriteEndObject();
        }

        w.WriteEndArray();
    }
}

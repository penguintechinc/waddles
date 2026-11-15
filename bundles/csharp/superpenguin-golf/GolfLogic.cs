using System.Buffers;
using System.Globalization;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinGolf;

/// <summary>
/// `!golf amount|all|max [power]`, ported from PenguinTwitchBot's Golf (Chip and Putt) game (`Commands/Games/Golf/GolfGameService.cs`,
/// `GolfSimulationEngine.cs`, `PlayerGolfRoundState.cs`, `Common/SlideShotPhysics.cs`, sub-actions `GolfPutt`/`GolfStartSession`/
/// `GolfStopSession`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). A thin game on
/// the shared <see cref="WageredGameBase"/>: the host plumbing (flag, identity, stake, cooldown, seed, replay record, the single
/// wager, overlay push, logs) lives in `superpenguin-dropgame-core`; this class maps a seed (and the viewer's power) to one tee shot.
///
/// <para><b>Deviations.</b> Upstream Golf is free play and its overlay runs the physics (the server engine is only a headless test
/// harness) and scores closest-to-the-pin into trigger variables. Here the upstream engine runs server-side from the recorded seed and
/// the score pays a multiple of the stake, see <see cref="PercentFor"/>. Upstream's default is a single stroke per entry
/// (`MaxStrokesPerPlayer = 1`); this port is that single-stroke mode, one wagered tee shot per command. Multi-stroke rounds (a ball that
/// stays on the course for follow-up Wedge/Putter strokes with a stroke penalty), timed moderator sessions (`!golf start|stop`), `!ball`,
/// emote/theme arguments and per-session drop limits depend on per-user round state and overlay/host support that does not exist yet
/// and are not ported. The green condition, which upstream draws once per session, is drawn from each play's seed.</para>
/// </summary>
public sealed class GolfLogic : WageredGameBase
{
    internal const string Game = "golf";
    internal const string FlagKey = "waddles.command-superpenguin-golf";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 5;
    internal const string ConfigId = "golf.v1";

    /// <summary>The percent of the stake a ball sunk in the cup (hole-in-one) pays: 3x.</summary>
    internal const int SunkPercent = 300;

    /// <summary>Percent of the stake paid per closest-to-the-pin point for a ball that stays on the green (score 1..100, so up to 2x).</summary>
    internal const int PercentPerPoint = 2;

    internal const int OverlayWidth = 1920;
    internal const int OverlayHeight = 1080;
    private const string UsageText = "To golf please do !golf Amount/All/Max [Power 1-100] replacing amount with how many you would like to risk, e.g. !golf 100 78.";

    private readonly GolfConfig _config;

    /// <summary>Creates the game with upstream's default tuning; tests inject the entropy source and a config.</summary>
    public GolfLogic(Func<ulong>? entropy = null, GolfConfig? config = null)
        : base(Spec, entropy)
    {
        _config = (config ?? GolfConfig.Default).Clamped();
    }

    private static GameSpec Spec { get; } = new(
        Game,
        ["golf"],
        FlagKey,
        MaxBet,
        CooldownSeconds,
        UsageText,
        ConfigId,
        new OverlayWidget(Game, "Golf Chip and Putt", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, OverlayWidth, OverlayHeight)));

    /// <summary>
    /// The payout percent of the stake for <paramref name="shot"/>: a ball sunk in the cup pays <see cref="SunkPercent"/>; a ball that
    /// stays on the green pays its closest-to-the-pin score x <see cref="PercentPerPoint"/> (so 1..100 points = 2%..200%); anything else
    /// pays 0. Far below the host ledger's 100x ceiling.
    /// </summary>
    internal static int PercentFor(GolfShot shot) =>
        shot.IsSunk ? SunkPercent : shot.IsWin ? shot.BaseScore * PercentPerPoint : 0;

    /// <summary>The payout for <paramref name="shot"/> on a <paramref name="stake"/> (integer percent math, floored).</summary>
    internal static ulong PayoutFor(ulong stake, GolfShot shot) => stake * (ulong)PercentFor(shot) / 100;

    /// <summary>Parses the optional power argument; null when omitted, a refusal-worthy value is reported through <paramref name="valid"/>.</summary>
    internal static int? ParsePower(IReadOnlyList<string> extraArgs, out bool valid)
    {
        valid = true;
        if (extraArgs.Count == 0)
        {
            return null;
        }

        if (extraArgs.Count == 1
            && int.TryParse(extraArgs[0], NumberStyles.None, CultureInfo.InvariantCulture, out var power)
            && power is >= 1 and <= 100)
        {
            return power;
        }

        valid = false;
        return null;
    }

    /// <inheritdoc />
    protected override string? CheckExtraArgs(IReadOnlyList<string> extraArgs)
    {
        ParsePower(extraArgs, out var valid);
        return valid ? null : UsageText;
    }

    /// <inheritdoc />
    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs) => PlayShot(seed, stake, extraArgs);

    /// <summary>The pure body of <see cref="Play"/>, reachable from tests: parse the power, run the engine, map the score to a payout.</summary>
    internal GameRound PlayShot(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        var power = ParsePower(extraArgs, out var valid);
        if (!valid)
        {
            throw new InvalidOperationException("golf received a malformed power argument that CheckExtraArgs should have refused");
        }

        var shot = GolfEngine.Shoot(seed, power, _config);
        var payout = PayoutFor(stake, shot);
        return new GameRound(payout, Describe(shot, payout, stake), DataJson(shot, _config, stake, payout));
    }

    /// <summary>The human outcome sentence for the chat reply: where the ball finished, the score and the green condition.</summary>
    internal static string Describe(GolfShot shot, ulong payout, ulong stake)
    {
        var yards = shot.DistanceFromPin * GolfEngine.YardsPerCourse;
        var multiplier = stake == 0 ? 0 : payout * 100 / stake / 100.0;
        var conditions = $"{shot.Power} power on a {GolfEngine.Label(shot.Condition)}";
        if (shot.IsSunk)
        {
            return $"HOLE-IN-ONE! the ball dropped in the cup ({conditions}, x{multiplier:0.##})";
        }

        var lip = shot.IsLipOut ? "lipped out and " : string.Empty;
        return shot.IsWin
            ? $"the ball {lip}stopped {yards:0.#}yd from the pin on the green, {shot.BaseScore} pts ({conditions}, x{multiplier:0.##})"
            : $"the ball {lip}stopped {yards:0.#}yd from the pin, off the green ({conditions})";
    }

    /// <summary>Serializes the game-specific part of the overlay envelope (documented in `overlay/SCHEMA.md`).</summary>
    internal static string DataJson(GolfShot shot, GolfConfig config, ulong stake, ulong payout)
    {
        var buffer = new ArrayBufferWriter<byte>(1024);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("course");
            w.WriteString("strip", "Bottom");
            w.WriteString("slide", "LeftToRight");
            w.WriteNumber("tee", Scaled(SlideShotPhysics.TeePosition));
            w.WriteNumber("target", Scaled(shot.Target));
            w.WriteNumber("cupRadius", Scaled(GolfEngine.CupRadius));
            w.WriteNumber("greenRadius", Scaled(GolfEngine.GreenRadius));
            w.WriteNumber("maxScore", config.MaxScore);
            w.WriteNumber("yardsPerCourse", (int)GolfEngine.YardsPerCourse);
            w.WriteEndObject();
            w.WriteString("condition", shot.Condition.ToString());
            w.WriteString("conditionLabel", GolfEngine.Label(shot.Condition));
            w.WriteString("club", "Driver");
            w.WriteNumber("strokeNumber", 1);
            w.WriteNumber("maxStrokes", 1);
            w.WriteNumber("power", shot.Power);
            w.WriteNumber("effectivePower", Scaled(shot.EffectivePower / 100.0));
            w.WriteNumber("launchSpeed", Scaled(shot.LaunchSpeed));
            w.WriteNumber("rest", Scaled(shot.RestPosition));
            w.WriteNumber("distanceFromPin", Scaled(shot.DistanceFromPin));
            w.WriteBoolean("sunk", shot.IsSunk);
            w.WriteBoolean("lipOut", shot.IsLipOut);
            w.WriteBoolean("win", shot.IsWin);
            w.WriteBoolean("holeInOne", shot.IsSunk);
            w.WriteNumber("score", shot.BaseScore);
            w.WriteNumber("multiplierPct", stake == 0 ? 0 : (long)(payout * 100 / stake));
            w.WriteNumber("totalTicks", shot.TotalTicks);
            w.WriteStartArray("keyframes");
            foreach (var k in shot.Keyframes)
            {
                w.WriteStartArray();
                w.WriteNumberValue(k.Tick);
                w.WriteNumberValue(Scaled(k.Axis));
                w.WriteNumberValue(Scaled(k.Lateral));
                w.WriteNumberValue(Scaled(k.Height));
                w.WriteEndArray();
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    /// <summary>Course fractions and speeds go out as integers in 1/10000 units, so the envelope carries no floating point.</summary>
    private static int Scaled(double value) => (int)Math.Round(value * 10000.0);
}

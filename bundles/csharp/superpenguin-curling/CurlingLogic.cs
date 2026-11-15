using System.Buffers;
using System.Globalization;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinCurling;

/// <summary>
/// `!curl amount|all|max [power 1-100]` (aliases `!curling`, `!slide`, `!shuffle`), ported from PenguinTwitchBot's Curling /
/// Shuffleboard game (`Commands/Games/Curling/CurlingGameService.cs`, `CurlingSimulationEngine.cs`, `Common/SlideShotPhysics.cs`;
/// github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). A thin game on the shared
/// <see cref="WageredGameBase"/>: the host plumbing (flag, identity, stake, cooldown, seed, replay record, the single wager, overlay
/// push, logs) lives in `superpenguin-dropgame-core`; this class only maps a seed to a slide and a score to a payout.
///
/// <para><b>Deviations.</b> Upstream is free play: a chat throw (`!curl [power] [emote]`, or the CurlingSlide sub-action) slides a
/// stone and its score feeds trigger variables. Here the same physics and scoring run server-side from the recorded seed and the
/// score pays a multiple of the stake, see <see cref="PayoutPercent"/>. Timed rounds (the CurlingStart/Stop session sub-actions),
/// emote/decal/theme arguments and shared-sheet stone collisions need overlay/host support that does not exist yet and are not
/// ported. The per-user cooldown is upstream's 10 s.</para>
/// </summary>
public sealed class CurlingLogic : WageredGameBase
{
    internal const string Game = "curling";
    internal const string FlagKey = "waddles.command-superpenguin-curling";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 10;
    internal const string ConfigId = "curling.v1";
    internal const string UsageText = "To curl please do !curl Amount/All/Max [Power 1-100] replacing amount with how many you would like to risk.";

    private readonly CurlingConfig _config;

    /// <summary>Creates the game with upstream's default sheet; tests inject the entropy source and a config.</summary>
    public CurlingLogic(Func<ulong>? entropy = null, CurlingConfig? config = null)
        : base(Spec, entropy)
    {
        _config = (config ?? CurlingConfig.Default).Clamped();
    }

    private static GameSpec Spec { get; } = new(
        Game,
        ["curl", "curling", "slide", "shuffle"],
        FlagKey,
        MaxBet,
        CooldownSeconds,
        UsageText,
        ConfigId,
        new OverlayWidget(Game, "Curling / Shuffleboard", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, 1920, 1080, "bottom")));

    /// <summary>
    /// The payout, as a percent of the stake, for a resting score. Curling house: 100 (button) 400%, 75 (4-foot) 200%, 50 (8-foot) 125%,
    /// 25 (12-foot) 75%. Shuffleboard: 30 550%, 20 225%, 10 110%. Anything else (a miss, the -10 kitchen penalty) pays nothing.
    /// </summary>
    internal static int PayoutPercent(int score, ScoringType scoring) => scoring == ScoringType.Shuffleboard
        ? score switch { 30 => 550, 20 => 225, 10 => 110, _ => 0 }
        : score switch { 100 => 400, 75 => 200, 50 => 125, 25 => 75, _ => 0 };

    /// <summary>The payout for <paramref name="stake"/> at a score: <c>stake * percent / 100</c>, rounded down.</summary>
    internal static ulong PayoutFor(ulong stake, int score, ScoringType scoring) => stake * (ulong)PayoutPercent(score, scoring) / 100;

    /// <inheritdoc />
    protected override string? CheckExtraArgs(IReadOnlyList<string> extraArgs)
    {
        if (extraArgs.Count == 0)
        {
            return null;
        }

        return extraArgs.Count == 1 && _config.ViewerPowerControl && TryParsePower(extraArgs[0], out _) ? null : UsageText;
    }

    /// <summary>Parses a whole-number power in 1..100 (invariant culture); false for anything else.</summary>
    internal static bool TryParsePower(string text, out int power) =>
        int.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out power) && power is >= 1 and <= 100;

    /// <inheritdoc />
    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        int? requested = extraArgs.Count == 1 && TryParsePower(extraArgs[0], out var p) ? p : null;
        var result = CurlingSimulation.Run(seed, _config, requested);
        var payout = PayoutFor(stake, result.Score, _config.Scoring);
        var percent = PayoutPercent(result.Score, _config.Scoring);
        var outcome = $"your stone slid at power {result.EffectivePower.ToString("0.#", CultureInfo.InvariantCulture)} on {CurlingSimulation.Label(result.Ice)} and {ZoneText(result, _config.Scoring)}"
            + (percent > 0 ? $" (x{(percent / 100.0).ToString("0.##", CultureInfo.InvariantCulture)})" : string.Empty);
        return new GameRound(payout, outcome, DataJson(result, _config));
    }

    /// <summary>Describes where the stone stopped, in the sheet's own vocabulary (house ring / shuffleboard zone / kitchen / miss).</summary>
    internal static string ZoneText(CurlingResult result, ScoringType scoring) => scoring == ScoringType.Shuffleboard
        ? result.Score switch
        {
            30 => "stopped dead centre in zone 30",
            20 => "stopped in zone 20",
            10 => "stopped in zone 10",
            -10 => "slid into the kitchen",
            _ => result.WallHit ? "hit the back board" : "stopped short of the zones",
        }
        : result.Score switch
        {
            100 => "stopped on the button",
            75 => "stopped in the 4-foot ring",
            50 => "stopped in the 8-foot ring",
            25 => "stopped in the 12-foot ring",
            _ => result.WallHit ? "hit the back board" : "stopped outside the house",
        };

    /// <summary>Serializes the game-specific part of the overlay envelope (documented in `overlay/SCHEMA.md`).</summary>
    internal static string DataJson(CurlingResult result, CurlingConfig config)
    {
        var buffer = new ArrayBufferWriter<byte>(2048);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("sheet");
            w.WriteString("stripPosition", config.Strip.ToString());
            w.WriteString("slideDirection", config.Direction.ToString());
            w.WriteBoolean("isReversed", result.IsReversed);
            w.WriteString("scoringType", config.Scoring.ToString());
            w.WriteBoolean("enableShuffleboardKitchenPenalty", config.KitchenPenalty);
            w.WriteString("collisionMode", "LockOnRest");
            w.WriteNumber("targetDistancePercent", config.TargetDistancePercent);
            w.WriteNumber("targetRatio", Scaled(result.TargetRatio));
            w.WriteNumber("powerVariancePercent", config.PowerVariancePercent);
            w.WriteNumber("tickRateHz", (int)SlideShotPhysics.TickRateHz);
            w.WriteNumber("scale", 10000);
            w.WriteEndObject();
            w.WriteString("condition", result.Ice.ToString());
            w.WriteString("conditionLabel", CurlingSimulation.Label(result.Ice));
            w.WriteNumber("power", result.RawPower);
            w.WriteNumber("effectivePower", Math.Round(result.EffectivePower, 4));
            w.WriteNumber("start", Scaled(result.StartPos));
            w.WriteNumber("restX", Scaled(config.IsHorizontal ? result.RestPos : result.Lateral));
            w.WriteNumber("restY", Scaled(config.IsHorizontal ? result.Lateral : result.RestPos));
            w.WriteNumber("totalTicks", result.TotalTicks);
            w.WriteBoolean("wallHit", result.WallHit);
            w.WriteNumber("score", result.Score);
            w.WriteBoolean("isBullseye", result.IsBullseye);
            w.WriteBoolean("isWin", result.IsWin);
            w.WriteNumber("multiplierPct", PayoutPercent(result.Score, config.Scoring));
            w.WriteStartArray("keyframes");
            foreach (var k in result.Keyframes)
            {
                w.WriteStartArray();
                w.WriteNumberValue(k.Tick);
                w.WriteNumberValue(Scaled(config.IsHorizontal ? k.AxisPos : k.LateralPos));
                w.WriteNumberValue(Scaled(config.IsHorizontal ? k.LateralPos : k.AxisPos));
                w.WriteNumberValue(Scaled(k.AxisVelocity));
                w.WriteEndArray();
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    /// <summary>Converts a normalised 0..1 value to the envelope's integer unit (1/10000), rounding half to even.</summary>
    private static int Scaled(double value) => (int)Math.Round(value * 10000.0);
}

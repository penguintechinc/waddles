using System.Buffers;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinBalloon;

/// <summary>
/// `!balloon amount|all|max`, ported from PenguinTwitchBot's Balloon Ascent (`Commands/Games/Balloon/BalloonAscentService.cs`,
/// `BalloonAscentSimulationEngine.cs`, `BalloonHazardPatternGenerator.cs`, the `BalloonAscent`/`BalloonHazards` sub-action types,
/// overlay `balloon.html` + `js/balloon.js`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s
/// `notice`). A thin game on the shared <see cref="WageredGameBase"/>: the host plumbing (flag, identity, stake, cooldown, seed, replay
/// record, the single wager, overlay push, logs) lives in `superpenguin-dropgame-core`; this class only maps a seed to a flight.
///
/// <para><b>Deviations.</b> Upstream is free play and its outcome is decided in each viewer's browser (wall clock, `Math.random`,
/// scores reported back with an overlay key). Here the same physics run server-side from the recorded seed (the overlay replays the
/// keyframes) and a docked flight pays a multiple of the stake, see <see cref="BalloonConfig.PayoutPercent"/>. Upstream's emote/theme
/// arguments, timed sessions (`!balloon start|stop`), the moderator hazard command and multi-balloon collisions need overlay/host
/// support that does not exist yet and are not ported: each flight instead faces one seeded hazard wave. Upstream's `!float` and
/// `!ascent` aliases are not registered because the shared cooldown and replay key use the command word, so an alias would bypass the
/// per-user cooldown. The per-user cooldown is upstream's 10 s.</para>
/// </summary>
public sealed class BalloonLogic : WageredGameBase
{
    internal const string Game = "balloon";
    internal const string FlagKey = "waddles.command-superpenguin-balloon";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 10;
    internal const string ConfigId = "balloon.v1";

    private readonly BalloonConfig _config;

    /// <summary>Creates the game with upstream's default settings; tests inject the entropy source and a config.</summary>
    public BalloonLogic(Func<ulong>? entropy = null, BalloonConfig? config = null)
        : base(Spec, entropy)
    {
        _config = (config ?? new BalloonConfig()).Clamped();
    }

    private static GameSpec Spec { get; } = new(
        Game,
        ["balloon"],
        FlagKey,
        MaxBet,
        CooldownSeconds,
        "To balloon please do !balloon Amount/All/Max replacing amount with how many you would like to risk.",
        ConfigId,
        new OverlayWidget(Game, "Balloon Ascent", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, BalloonConfig.Width, BalloonConfig.Height)));

    /// <summary>The payout for <paramref name="result"/>: <paramref name="stake"/> times the config's percent (0 for a pop or a miss), at most 100x.</summary>
    internal static ulong PayoutFor(ulong stake, BalloonResult result, BalloonConfig config) =>
        stake * (ulong)config.PayoutPercent(result.Hit, result.Bullseye, result.Score) / 100;

    /// <summary>The chat wording of a flight's outcome (the reply base appends won/lost, amounts and the new balance).</summary>
    internal static string DescribeOutcome(BalloonResult result, ulong stake, ulong payout)
    {
        if (result.Popped)
        {
            return "a dart popped the balloon on the way up";
        }

        if (result.Hit)
        {
            var multiplier = $"x{payout * 100 / stake / 100.0:0.##}";
            return result.Bullseye
                ? $"GOLDEN BULLSEYE! the balloon docked on the cloud for {result.Score} points ({multiplier})"
                : $"the balloon docked on the cloud for {result.Score} points ({multiplier})";
        }

        return result.TimedOut ? "the balloon drifted off and never reached the cloud" : "the balloon missed the cloud";
    }

    /// <inheritdoc />
    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        var result = BalloonSimulation.Run(seed, _config);
        var payout = PayoutFor(stake, result, _config);
        return new GameRound(payout, DescribeOutcome(result, stake, payout), DataJson(result, stake, payout));
    }

    private static long Fixed(double v) => (long)Math.Round(v * 10000.0);

    /// <summary>Serializes the game-specific part of the overlay envelope (documented in `overlay/SCHEMA.md`).</summary>
    internal static string DataJson(BalloonResult r, ulong stake, ulong payout)
    {
        var buffer = new ArrayBufferWriter<byte>(8192);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("field");
            w.WriteNumber("w", BalloonConfig.Width);
            w.WriteNumber("h", BalloonConfig.Height);
            w.WriteNumber("tickHz", (int)BalloonSimulation.TickRateHz);
            w.WriteEndObject();

            w.WriteStartObject("launch");
            w.WriteNumber("x", Fixed(BalloonSimulation.CannonX));
            w.WriteNumber("y", Fixed(BalloonSimulation.LaunchY));
            w.WriteNumber("vx", Fixed(r.Launch.Vx));
            w.WriteNumber("vy", Fixed(r.Launch.Vy));
            w.WriteNumber("seconds", Fixed(r.Launch.DurationSeconds));
            w.WriteNumber("totalTicks", r.Launch.TotalTicks);
            w.WriteNumber("size", Fixed(r.Launch.SizeScale));
            w.WriteNumber("speed", Fixed(r.Launch.SpeedScale));
            w.WriteEndObject();

            w.WriteStartObject("cloud");
            w.WriteNumber("base", Fixed(r.Cloud.Base));
            w.WriteNumber("halfWidth", Fixed(r.Cloud.HalfWidth));
            w.WriteNumber("span", Fixed(r.Cloud.Span));
            w.WriteNumber("phase", Fixed(r.Cloud.Phase));
            w.WriteNumber("omega", Fixed(r.Cloud.Omega));
            w.WriteNumber("y", Fixed(r.Cloud.Y));
            w.WriteNumber("xAtEnd", Fixed(r.CloudXAtEnd));
            w.WriteEndObject();

            w.WriteStartObject("wave");
            w.WriteString("pattern", r.Pattern.ToString());
            w.WriteString("speed", r.SpeedLabel);
            w.WriteNumber("spawnTick", r.WaveTick);
            w.WriteStartArray("hazards");
            foreach (var o in r.Hazards)
            {
                var h = o.Hazard;
                w.WriteStartObject();
                w.WriteString("kind", h.Kind.ToString());
                w.WriteNumber("x", Fixed(h.X));
                w.WriteNumber("y", Fixed(h.Y));
                w.WriteNumber("vx", Fixed(h.Vx));
                w.WriteNumber("vy", Fixed(h.Vy));
                w.WriteNumber("g", Fixed(h.Gravity));
                w.WriteNumber("r", Fixed(h.Radius));
                w.WriteNumber("hitTick", o.HitTick);
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteEndObject();

            w.WriteStartArray("keyframes");
            foreach (var k in r.Keyframes)
            {
                w.WriteStartArray();
                w.WriteNumberValue(k.Tick);
                w.WriteNumberValue(k.X);
                w.WriteNumberValue(k.Y);
                w.WriteEndArray();
            }

            w.WriteEndArray();

            w.WriteNumber("endTick", r.EndTick);
            w.WriteBoolean("popped", r.Popped);
            w.WriteNumber("popTick", r.PopTick);
            w.WriteNumber("popSpin", Fixed(r.PopSpin));
            w.WriteBoolean("hit", r.Hit);
            w.WriteNumber("score", r.Score);
            w.WriteBoolean("bullseye", r.Bullseye);
            w.WriteBoolean("timedOut", r.TimedOut);
            w.WriteNumber("multiplierPct", stake == 0 ? 0 : (long)(payout * 100 / stake));
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }
}

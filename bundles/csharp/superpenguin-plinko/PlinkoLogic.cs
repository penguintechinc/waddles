using System.Buffers;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinPlinko;

/// <summary>
/// `!plinko amount|all|max`, ported from PenguinTwitchBot's Plinko game (`Commands/Games/Plinko/PlinkoDropService.cs`,
/// `PlinkoSimulationEngine.cs`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`).
/// A thin game on the shared <see cref="WageredGameBase"/>: the host plumbing (flag, identity, stake, cooldown, seed, replay record,
/// the single wager, overlay push, logs) lives in `superpenguin-dropgame-core`; this class only maps a seed to a drop.
///
/// <para><b>Deviations.</b> Upstream Plinko is free play: the overlay's physics picks the bucket and the bucket's score feeds trigger
/// variables. Here the same physics and weights run server-side from the recorded seed (the overlay replays the keyframes) and the
/// bucket pays a multiple of the stake, see <see cref="PayoutFor"/>. Upstream's emote/theme arguments, timed rounds and shared-board
/// collisions need overlay/host support that does not exist yet and are not ported; the per-user cooldown is upstream's 10 s.</para>
/// </summary>
public sealed class PlinkoLogic : WageredGameBase
{
    internal const string Game = "plinko";
    internal const string FlagKey = "waddles.command-superpenguin-plinko";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 10;
    internal const string ConfigId = "plinko.v1";

    private readonly PlinkoConfig _config;

    /// <summary>Creates the game with upstream's default board; tests inject the entropy source and a config.</summary>
    public PlinkoLogic(Func<ulong>? entropy = null, PlinkoConfig? config = null)
        : base(Spec, entropy)
    {
        _config = (config ?? PlinkoConfig.Default).Clamped();
    }

    private static GameSpec Spec { get; } = new(
        Game,
        ["plinko"],
        FlagKey,
        MaxBet,
        CooldownSeconds,
        "To plinko please do !plinko Amount/All/Max replacing amount with how many you would like to risk.",
        ConfigId,
        new OverlayWidget(Game, "Plinko Bounce", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, PlinkoConfig.Width, PlinkoConfig.Height)));

    /// <summary>
    /// The payout for landing in <paramref name="outcome"/>: a losing bucket pays 0; a winning bucket pays its upstream score read as a
    /// percent of the stake (score 250 = 2.5x), and a winning bucket with score 0 (upstream's default WINNER slot) pays
    /// <see cref="PlinkoConfig.DefaultWinPercent"/>. Capped at 10000 percent (the host ledger's 100x ceiling).
    /// </summary>
    internal static ulong PayoutFor(ulong stake, BucketOutcome outcome)
    {
        if (!outcome.IsWin)
        {
            return 0;
        }

        var percent = (ulong)Math.Min(outcome.Score > 0 ? outcome.Score : PlinkoConfig.DefaultWinPercent, 10000);
        return stake * percent / 100;
    }

    /// <inheritdoc />
    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        var result = PlinkoSimulation.Run(seed, _config);
        var payout = PayoutFor(stake, result.Outcome);
        var outcome = result.Outcome.IsWin
            ? $"the puck landed in {result.Outcome.Label} (x{payout * 100 / stake / 100.0:0.##})"
            : $"the puck landed in {result.Outcome.Label}";
        return new GameRound(payout, outcome, DataJson(result, _config, stake, payout));
    }

    /// <summary>Serializes the game-specific part of the overlay envelope (documented in `overlay/SCHEMA.md`).</summary>
    internal static string DataJson(PlinkoResult result, PlinkoConfig config, ulong stake, ulong payout)
    {
        var buffer = new ArrayBufferWriter<byte>(4096);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("board");
            w.WriteNumber("w", PlinkoConfig.Width);
            w.WriteNumber("h", PlinkoConfig.Height);
            w.WriteNumber("rows", config.RowCount);
            w.WriteNumber("pegColumns", PlinkoConfig.PegColumns);
            w.WriteNumber("pegRadius", config.PegRadius);
            w.WriteNumber("puckRadius", config.PuckRadius);
            w.WriteNumber("bucketHeight", config.BucketHeight);
            w.WriteStartArray("buckets");
            foreach (var b in config.Buckets)
            {
                w.WriteStartObject();
                w.WriteString("label", b.Label);
                w.WriteNumber("score", b.Score);
                w.WriteBoolean("win", b.IsWin);
                w.WriteNumber("weight", b.Weight);
                w.WriteString("color", b.Color);
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteEndObject();
            w.WriteNumber("startX", (int)Math.Round(result.StartX * 10000.0));
            w.WriteNumber("bucket", result.BucketIndex);
            w.WriteString("label", result.Outcome.Label);
            w.WriteBoolean("win", result.Outcome.IsWin);
            w.WriteBoolean("bullseye", result.Outcome.IsBullseye);
            w.WriteNumber("score", result.Outcome.Score);
            w.WriteNumber("multiplierPct", stake == 0 ? 0 : (long)(payout * 100 / stake));
            w.WriteBoolean("timedOut", result.TimedOut);
            w.WriteStartArray("keyframes");
            foreach (var k in result.Keyframes)
            {
                w.WriteStartArray();
                w.WriteNumberValue(k.TimeMs);
                w.WriteNumberValue(k.X);
                w.WriteNumberValue(k.Y);
                w.WriteEndArray();
            }

            w.WriteEndArray();
            w.WriteStartArray("pegHits");
            foreach (var p in result.PegHits)
            {
                w.WriteStartArray();
                w.WriteNumberValue(p.TimeMs);
                w.WriteNumberValue(p.Row);
                w.WriteNumberValue(p.Col);
                w.WriteEndArray();
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }
}

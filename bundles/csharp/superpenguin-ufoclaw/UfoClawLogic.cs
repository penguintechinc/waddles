using System.Buffers;
using System.Globalization;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinUfoclaw;

/// <summary>
/// `!ufo amount|all|max` (alias `!ufoclaw`), ported from PenguinTwitchBot's UFO Claw (`Bot/Commands/Games/UfoClaw/UfoClawService.cs`,
/// `UfoClawModels.cs`, `UfoClawStore.cs`, `UfoClawControlType.cs`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission,
/// see `bundle.yaml`'s `notice`). A thin game on the shared <see cref="WageredGameBase"/>: the host plumbing (flag, identity,
/// stake, cooldown, seed, replay record, the single wager, overlay push, logs) lives in `superpenguin-dropgame-core`; this class
/// only maps a seed to a claw grab.
///
/// <para><b>Decision: single wagered command.</b> Upstream's grab needs no persisted state to be decided (the server picks the
/// winner from the weighted pool and the animation is a pure function of grab id + pool), so one `!ufo &lt;stake&gt;` = one
/// deterministic grab = one `Economy.Wager` fits the base exactly. Upstream's persisted control model (show/hide, viewer
/// entries, remove-winners, result triggers) is operator tooling for a free draw and is deferred, see the README.</para>
///
/// <para><b>Payout.</b> Upstream grabs award no points (the prize label/value feeds trigger variables). Each pit ball carries a
/// multiplier percent of the stake (see <see cref="UfoClawConfig.Default"/>; 0 = a dud); the payout is
/// <c>stake * pct / 100</c> rounded down, capped by the host's 100x ceiling.</para>
/// </summary>
public sealed class UfoClawLogic : WageredGameBase
{
    internal const string Game = "ufoclaw";
    internal const string FlagKey = "waddles.command-superpenguin-ufoclaw";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 10;
    internal const string ConfigId = "ufoclaw.v1";
    internal const int SceneWidth = 1920;
    internal const int SceneHeight = 1080;

    private readonly UfoClawConfig _config;

    /// <summary>Creates the game with the default pit; tests inject the entropy source and a config.</summary>
    public UfoClawLogic(Func<ulong>? entropy = null, UfoClawConfig? config = null)
        : base(Spec, entropy)
    {
        _config = (config ?? UfoClawConfig.Default).Clamped();
    }

    private static GameSpec Spec { get; } = new(
        Game,
        ["ufo", "ufoclaw"],
        FlagKey,
        MaxBet,
        CooldownSeconds,
        "To play the UFO claw please do !ufo Amount/All/Max replacing amount with how many you would like to risk.",
        ConfigId,
        new OverlayWidget(Game, "UFO Claw", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, SceneWidth, SceneHeight)));

    /// <summary>The payout for <paramref name="prize"/> on <paramref name="stake"/>: the multiplier percent of the stake, rounded down.</summary>
    internal static ulong PayoutFor(ulong stake, UfoClawPrize prize) => stake * (ulong)prize.MultiplierPct / 100;

    /// <inheritdoc />
    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        var result = UfoClawSimulation.Run(seed, _config);
        var prize = result.Winner;
        var payout = PayoutFor(stake, prize);
        var outcome = prize.MultiplierPct > 0
            ? $"the claw grabbed {prize.Label} (x{(prize.MultiplierPct / 100m).ToString("0.##", CultureInfo.InvariantCulture)})"
            : $"the claw grabbed {prize.Label}";
        return new GameRound(payout, outcome, DataJson(result, _config));
    }

    /// <summary>Serializes the game-specific part of the overlay envelope (documented in `overlay/SCHEMA.md`).</summary>
    internal static string DataJson(UfoClawResult result, UfoClawConfig config)
    {
        var buffer = new ArrayBufferWriter<byte>(2048);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("scene");
            w.WriteNumber("w", SceneWidth);
            w.WriteNumber("h", SceneHeight);
            w.WriteEndObject();
            w.WriteStartObject("rules");
            w.WriteBoolean("tease", config.Tease);
            w.WriteNumber("slipChancePercent", config.SlipChancePercent);
            w.WriteNumber("maxSuspenseDrops", config.MaxSuspenseDrops);
            w.WriteNumber("durationSeconds", config.DurationSeconds);
            w.WriteNumber("revealSeconds", config.RevealSeconds);
            w.WriteNumber("movementSpeed", config.MovementSpeed);
            w.WriteEndObject();
            w.WriteStartArray("pool");
            foreach (var p in result.Pool)
            {
                w.WriteStartObject();
                w.WriteString("label", p.Label);
                w.WriteNumber("multiplierPct", p.MultiplierPct);
                w.WriteNumber("weight", p.Weight);
                w.WriteString("color", p.Color);
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteNumber("winningIndex", result.WinningIndex);
            w.WriteString("label", result.Winner.Label);
            w.WriteNumber("multiplierPct", result.Winner.MultiplierPct);
            w.WriteBoolean("win", result.Winner.MultiplierPct > 0);
            w.WriteBoolean("slip", result.Slip);
            w.WriteStartArray("feints");
            foreach (var index in result.Feints)
            {
                w.WriteNumberValue(index);
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }
}

using System.Buffers;
using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinRacing;

/// <summary>
/// Builds the game-specific `data` objects of the racing overlay envelopes (documented in `overlay/SCHEMA.md`): `join` (lobby
/// state after a stake is taken), `race` (one finished race: per-lane keyframes, events, placements, payouts) and `void`
/// (a refunded window). The fixed envelope fields come from the core's <see cref="OverlayEnvelope"/>; nothing here carries a name.
/// </summary>
public static class RacingOverlay
{
    /// <summary>The widget id and asset prefix (`drop-games/racing/`).</summary>
    public const string Game = "racing";

    /// <summary>Lane colours (the overlay's fallback car tint; upstream tints cars with the chat colour, which carries a name and is not available).</summary>
    private static readonly string[] Palette =
    [
        "#FF5252", "#448AFF", "#69F0AE", "#FFD740", "#E040FB", "#FF6E40",
        "#18FFFF", "#B2FF59", "#EEFF41", "#FF4081", "#7C4DFF", "#00E676",
    ];

    /// <summary>The widget the bundle registers once a day.</summary>
    public static OverlayWidget Widget { get; } = new(Game, "Racing", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor(Game, 1920, 1080));

    /// <summary>The lobby snapshot pushed after each join (kind `join`).</summary>
    public static string JoinData(string raceId, ulong closesAt, int lane, int racers, int maxRacers, int laps)
    {
        var buffer = new ArrayBufferWriter<byte>(256);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteString("raceId", raceId);
            w.WriteString("phase", "lobby");
            w.WriteNumber("closesAt", closesAt);
            w.WriteNumber("lane", lane);
            w.WriteNumber("racers", racers);
            w.WriteNumber("maxRacers", maxRacers);
            w.WriteNumber("laps", laps);
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    /// <summary>The finished-race payload (kind `race`): everything the overlay needs to replay the race without any physics.</summary>
    public static string RaceData(string raceId, RaceConfig config, RaceOutcome outcome, IReadOnlyList<RacerResult> racers, ulong pot)
    {
        var buffer = new ArrayBufferWriter<byte>(16384);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteString("raceId", raceId);
            w.WriteStartObject("track");
            w.WriteString("type", "perimeter");
            w.WriteNumber("length", config.TrackLength);
            w.WriteNumber("laps", config.Laps);
            w.WriteNumber("sampleMs", config.SampleMillis);
            w.WriteNumber("totalMs", outcome.TotalMs);
            WriteInts(w, "debris", outcome.DebrisPos10);
            WriteInts(w, "boosts", outcome.BoostPos10);
            w.WriteEndObject();
            w.WriteNumber("pot", pot);
            w.WriteStartArray("racers");
            foreach (var r in racers)
            {
                w.WriteStartObject();
                w.WriteNumber("lane", r.Lane);
                w.WriteString("actor", r.Actor);
                w.WriteString("color", Palette[r.Lane % Palette.Length]);
                w.WriteNumber("stake", r.Stake);
                w.WriteNumber("powerMilli", outcome.PowerMilli[r.Lane]);
                w.WriteNumber("place", r.Place);
                w.WriteNumber("payout", r.Payout);
                w.WriteNumber("finishMs", r.FinishMs);
                w.WriteBoolean("dnf", r.Dnf);
                w.WriteStartArray("keyframes");
                foreach (var (timeMs, pos10) in outcome.Keyframes[r.Lane])
                {
                    w.WriteStartArray();
                    w.WriteNumberValue(timeMs);
                    w.WriteNumberValue(pos10);
                    w.WriteEndArray();
                }

                w.WriteEndArray();
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteStartArray("events");
            foreach (var e in outcome.Events)
            {
                w.WriteStartArray();
                w.WriteNumberValue(e.TimeMs);
                w.WriteNumberValue(e.Lane);
                w.WriteStringValue(e.Kind);
                w.WriteEndArray();
            }

            w.WriteEndArray();
            WriteInts(w, "order", outcome.Order);
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    /// <summary>The refunded-window payload (kind `void`): why, and who got which stake back.</summary>
    public static string VoidData(string raceId, string reason, IReadOnlyList<RacerResult> racers)
    {
        var buffer = new ArrayBufferWriter<byte>(512);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteString("raceId", raceId);
            w.WriteString("reason", reason);
            w.WriteStartArray("refunds");
            foreach (var r in racers)
            {
                w.WriteStartObject();
                w.WriteString("actor", r.Actor);
                w.WriteNumber("amount", r.Payout);
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    private static void WriteInts(Utf8JsonWriter w, string name, IReadOnlyList<int> values)
    {
        w.WriteStartArray(name);
        foreach (var v in values)
        {
            w.WriteNumberValue(v);
        }

        w.WriteEndArray();
    }
}

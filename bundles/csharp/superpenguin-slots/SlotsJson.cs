using System.Text.Json;
using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinSlots;

/// <summary>
/// The three reels, stake and chosen message indices of one spin, recorded in `kv` under the event's replay key before the
/// `wager` call. A redelivered event reprints this outcome instead of re-spinning, but only when actor, command and the raw
/// command text all match what is recorded here.
/// </summary>
public sealed record SlotsOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("e1")] int Reel1,
    [property: JsonPropertyName("e2")] int Reel2,
    [property: JsonPropertyName("e3")] int Reel3,
    [property: JsonPropertyName("win_msg")] int WinMessage,
    [property: JsonPropertyName("lose_msg")] int LoseMessage);

/// <summary>
/// Per-activation settings: the upstream `Slots` game-setting rows (3 of a kind / first-2 / last-2 multipliers, emotes) plus
/// the minimum bet and the per-command cooldown, which upstream hardcodes. Every field is optional (upstream defaults).
/// </summary>
public sealed class SlotsConfig
{
    /// <summary>The host-enforced ceiling on a payout multiple of the stake (`economy.wager`).</summary>
    internal const double MaxMultiplier = 100;

    [JsonPropertyName("min_bet")] public long MinBet { get; set; } = 25;

    [JsonPropertyName("three_of_a_kind")] public double ThreeOfAKind { get; set; } = 3.5;

    [JsonPropertyName("first_two")] public double FirstTwo { get; set; } = 2.5;

    [JsonPropertyName("last_two")] public double LastTwo { get; set; } = 1.5;

    [JsonPropertyName("cooldown_seconds")] public uint CooldownSeconds { get; set; } = 600;

    [JsonPropertyName("emotes")] public string[] Emotes { get; set; } = ["Kappa", "PogChamp", "LUL", "4Head", "BloodTrail"];

    /// <summary>Parses the activation config JSON (blank = all defaults) and validates it; an invalid config fails loud.</summary>
    public static SlotsConfig Load(string json)
    {
        var config = string.IsNullOrWhiteSpace(json)
            ? new SlotsConfig()
            : JsonSerializer.Deserialize(json, SlotsJsonContext.Default.SlotsConfig) ?? throw new InvalidOperationException("slots config is JSON null");
        config.Validate();
        return config;
    }

    private void Validate()
    {
        if (MinBet < 1)
        {
            throw new InvalidOperationException("slots config: min_bet must be >= 1");
        }

        foreach (var (name, value) in new[] { ("three_of_a_kind", ThreeOfAKind), ("first_two", FirstTwo), ("last_two", LastTwo) })
        {
            if (!double.IsFinite(value) || value <= 0 || value > MaxMultiplier)
            {
                throw new InvalidOperationException($"slots config: {name} must be in (0, {MaxMultiplier}]");
            }
        }

        if (Emotes is null || Emotes.Length < 2 || Emotes.Length > 20 || Emotes.Any(string.IsNullOrWhiteSpace))
        {
            throw new InvalidOperationException("slots config: emotes needs 2..20 non-blank entries");
        }

        if (CooldownSeconds == 0)
        {
            throw new InvalidOperationException("slots config: cooldown_seconds must be >= 1");
        }
    }
}

/// <summary>This bundle's own source-generated JSON context (a bundle cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(SlotsOutcome))]
[JsonSerializable(typeof(SlotsConfig))]
public sealed partial class SlotsJsonContext : JsonSerializerContext;

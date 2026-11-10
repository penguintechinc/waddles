using System.Text.Json;
using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinDefuse;

/// <summary>
/// The wire the user cut, the wire that defuses the bomb and the reward of one `!defuse`, recorded in `kv` under the event's
/// replay key before the `wager` call. A redelivered event reprints this outcome instead of re-drawing, but only when actor,
/// command and the raw command text all match what is recorded here. <see cref="Reward"/> is the gross payout (0 when the bomb
/// exploded), fixed at draw time so a replay pays and prints the same amount.
/// </summary>
public sealed record DefuseOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("wire")] string Wire,
    [property: JsonPropertyName("cost")] ulong Cost,
    [property: JsonPropertyName("reward")] ulong Reward);

/// <summary>
/// Per-activation settings: the upstream `Defuse` game-setting rows (wires, cost, win multiplier and the message templates)
/// plus the cooldown. Every field is optional (upstream defaults).
/// </summary>
public sealed class DefuseConfig
{
    /// <summary>Largest win multiplier: the reward can reach cost x multiplier + cost / multiplier, which must stay within the host's 100x payout cap.</summary>
    internal const int MaxWinMultiplier = 50;

    /// <summary>Largest cost, so cost x multiplier always fits the 32-bit random draw.</summary>
    internal const long MaxCost = 10_000_000;

    [JsonPropertyName("wires")] public string[] Wires { get; set; } = ["red", "blue", "yellow"];

    [JsonPropertyName("cost")] public long Cost { get; set; } = 500;

    [JsonPropertyName("win_multiplier")] public int WinMultiplier { get; set; } = 3;

    [JsonPropertyName("cooldown_seconds")] public uint CooldownSeconds { get; set; } = 10;

    [JsonPropertyName("no_args")] public string NoArgs { get; set; } = "you need to choose one of these wires to cut: {Wires}";

    [JsonPropertyName("not_enough")] public string NotEnough { get; set; } = "Sorry it costs {Cost} {PointType} to defuse the bomb which you do not have.";

    [JsonPropertyName("starting")] public string Starting { get; set; } = "The bomb is beeping and {Name} cuts the {Wire} wire... ";

    [JsonPropertyName("success")] public string Success { get; set; } = "The bomb goes silent. As a thank for saving the day you got awarded {Points} {PointType}";

    [JsonPropertyName("failure")] public string Failure { get; set; } = "BOOM!!! The bomb explodes, you lose {Points} {PointType}.";

    /// <summary>Parses the activation config JSON (blank = all defaults) and validates it; an invalid config fails loud.</summary>
    public static DefuseConfig Load(string json)
    {
        var config = string.IsNullOrWhiteSpace(json)
            ? new DefuseConfig()
            : JsonSerializer.Deserialize(json, DefuseJsonContext.Default.DefuseConfig) ?? throw new InvalidOperationException("defuse config is JSON null");
        config.Validate();
        return config;
    }

    private void Validate()
    {
        if (Wires is null || Wires.Length < 2 || Wires.Length > 20 || Wires.Any(string.IsNullOrWhiteSpace)
            || Wires.Select(w => w.Trim().ToLowerInvariant()).Distinct().Count() != Wires.Length)
        {
            throw new InvalidOperationException("defuse config: wires needs 2..20 distinct non-blank entries");
        }

        if (Cost is < 1 or > MaxCost || WinMultiplier < 2 || WinMultiplier > MaxWinMultiplier || CooldownSeconds == 0)
        {
            throw new InvalidOperationException($"defuse config: cost 1..{MaxCost}, win_multiplier 2..{MaxWinMultiplier}, cooldown_seconds >= 1");
        }

        foreach (var template in new[] { NoArgs, NotEnough, Starting, Success, Failure })
        {
            if (string.IsNullOrWhiteSpace(template))
            {
                throw new InvalidOperationException("defuse config: message templates must not be blank");
            }
        }
    }
}

/// <summary>This bundle's own source-generated JSON context (a bundle cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(DefuseOutcome))]
[JsonSerializable(typeof(DefuseConfig))]
public sealed partial class DefuseJsonContext : JsonSerializerContext;

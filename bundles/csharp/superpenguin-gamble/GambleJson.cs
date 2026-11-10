using System.Text.Json;
using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinGamble;

/// <summary>
/// The d100 roll, stake and jackpot share of one `!gamble`, recorded in `kv` under the event's replay key before the `wager`
/// call. A redelivered event reprints this outcome instead of re-rolling, but only when actor, command and the raw command
/// text all match what is recorded here. <see cref="Share"/> is the part of the jackpot this win pays (0 unless the jackpot
/// number was rolled), fixed at roll time so a replay pays and prints the same amount.
/// </summary>
public sealed record GambleOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("stake")] ulong Stake,
    [property: JsonPropertyName("rolled")] int Rolled,
    [property: JsonPropertyName("share")] ulong Share);

/// <summary>
/// Per-activation settings: the upstream `Gamble` game-setting rows (jackpot number, starting jackpot, minimum for win, minimum
/// bet, winning multiplier, jackpot contribution and the message templates) plus the cooldown and the bet that earns the full
/// jackpot share (the original's max bet). Every field is optional (upstream defaults).
/// </summary>
public sealed class GambleConfig
{
    /// <summary>The host-enforced ceiling on a payout multiple of the stake (`economy.wager`).</summary>
    internal const int MaxMultiplier = 100;

    [JsonPropertyName("min_bet")] public long MinBet { get; set; } = 5;

    /// <summary>A bet of at least this much wins the whole jackpot when the jackpot number rolls; smaller bets win a proportional share.</summary>
    [JsonPropertyName("max_bet")] public long MaxBet { get; set; } = 500;

    [JsonPropertyName("jackpot_number")] public int JackpotNumber { get; set; } = 69;

    [JsonPropertyName("minimum_for_win")] public int MinimumForWin { get; set; } = 48;

    [JsonPropertyName("winning_multiplier")] public int WinningMultiplier { get; set; } = 2;

    [JsonPropertyName("jackpot_contribution")] public double JackpotContribution { get; set; } = 0.10;

    [JsonPropertyName("starting_jackpot")] public long StartingJackpot { get; set; } = 1000;

    [JsonPropertyName("cooldown_seconds")] public uint CooldownSeconds { get; set; } = 180;

    [JsonPropertyName("win_message")] public string WinMessage { get; set; } = "{Name} rolled {Rolled} and won {Points} {PointType}!";

    [JsonPropertyName("lose_message")] public string LoseMessage { get; set; } = "{Name} rolled {Rolled} and lost {Points} {PointType}";

    [JsonPropertyName("jackpot_message")] public string JackpotMessage { get; set; } = "{Name} rolled {Rolled} and won the jackpot of {Points} {PointType}!";

    [JsonPropertyName("current_jackpot_message")] public string CurrentJackpotMessage { get; set; } = "The current jackpot is {jackpot} {PointType}";

    [JsonPropertyName("incorrect_args")]
    public string IncorrectArgs { get; set; } =
        "To gamble, do !{Command} amount to specify amount or do !{Command} max or all to do the max bet. You can also do it by percentage like !{Command} 50%";

    [JsonPropertyName("incorrect_bet")] public string IncorrectBet { get; set; } = "The max bet is {MaxBet} {PointType} and must be greater then {MinBet} {PointType}";

    [JsonPropertyName("not_enough")] public string NotEnough { get; set; } = "You don't have enough to gamble with.";

    /// <summary>Parses the activation config JSON (blank = all defaults) and validates it; an invalid config fails loud.</summary>
    public static GambleConfig Load(string json)
    {
        var config = string.IsNullOrWhiteSpace(json)
            ? new GambleConfig()
            : JsonSerializer.Deserialize(json, GambleJsonContext.Default.GambleConfig) ?? throw new InvalidOperationException("gamble config is JSON null");
        config.Validate();
        return config;
    }

    private void Validate()
    {
        if (MinBet < 1 || MaxBet < MinBet)
        {
            throw new InvalidOperationException("gamble config: need 1 <= min_bet <= max_bet");
        }

        if (JackpotNumber is < 1 or > 100 || MinimumForWin is < 0 or > 100)
        {
            throw new InvalidOperationException("gamble config: jackpot_number must be 1..100 and minimum_for_win 0..100");
        }

        if (WinningMultiplier is < 1 or > MaxMultiplier)
        {
            throw new InvalidOperationException($"gamble config: winning_multiplier must be 1..{MaxMultiplier}");
        }

        if (!double.IsFinite(JackpotContribution) || JackpotContribution is < 0 or > 1 || StartingJackpot < 0 || CooldownSeconds == 0)
        {
            throw new InvalidOperationException("gamble config: jackpot_contribution must be 0..1, starting_jackpot >= 0, cooldown_seconds >= 1");
        }

        foreach (var template in new[] { WinMessage, LoseMessage, JackpotMessage, CurrentJackpotMessage, IncorrectArgs, IncorrectBet, NotEnough })
        {
            if (string.IsNullOrWhiteSpace(template))
            {
                throw new InvalidOperationException("gamble config: message templates must not be blank");
            }
        }
    }
}

/// <summary>This bundle's own source-generated JSON context (a bundle cannot add types to the SDK's context).</summary>
[JsonSerializable(typeof(GambleOutcome))]
[JsonSerializable(typeof(GambleConfig))]
public sealed partial class GambleJsonContext : JsonSerializerContext;

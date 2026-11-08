using System.Globalization;

namespace SuperPenguin.DropGameCore;

/// <summary>The outcome of parsing a stake argument: a usable stake, or the chat refusal explaining why not.</summary>
public sealed record StakeResult(ulong Stake, string? Refusal)
{
    /// <summary>True when <see cref="Stake"/> may be wagered.</summary>
    public bool Ok => Refusal is null;
}

/// <summary>
/// Parses and clamps a stake argument (`amount`, `all` or `max`) the way the TicketGames ports do: an over-limit amount is clamped to
/// the per-bet limit rather than refused, but bad input and amounts the user cannot cover are refused with a chat message.
/// </summary>
public static class StakeParser
{
    /// <summary>Parses <paramref name="arg"/> against the user's <paramref name="held"/> balance and the per-bet <paramref name="maxBet"/>.</summary>
    public static StakeResult Parse(string arg, long held, ulong maxBet)
    {
        ulong stake;
        if (string.Equals(arg, "all", StringComparison.OrdinalIgnoreCase) || string.Equals(arg, "max", StringComparison.OrdinalIgnoreCase))
        {
            stake = held > 0 ? (ulong)held : 0;
        }
        else if (!long.TryParse(arg, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var parsed))
        {
            return new StakeResult(0, "The amount must be a number, max, or all");
        }
        else if (parsed <= 0)
        {
            return new StakeResult(0, "The amount needs to be greater then 0");
        }
        else
        {
            stake = (ulong)parsed;
        }

        if (stake == 0 || stake > (ulong)Math.Max(held, 0))
        {
            return new StakeResult(0, "You don't have that many.");
        }

        return new StakeResult(Math.Min(stake, maxBet), null);
    }
}

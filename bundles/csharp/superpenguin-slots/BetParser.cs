using System.Globalization;

namespace WaddleBundleSuperpenguinSlots;

/// <summary>Outcome of parsing a bet argument.</summary>
internal enum BetParse
{
    Ok,
    Invalid,
    TooMuch,
    TooLow,
}

/// <summary>
/// Parses the upstream `MaxBetCalculator` bet grammar: an integer amount, `max`/`all` (the largest allowed bet) or a
/// percentage such as `50%` of that largest bet. Pure, so it is unit-testable without a host.
/// </summary>
internal static class BetParser
{
    /// <summary>
    /// Parses <paramref name="args"/> against <paramref name="maxBet"/> and <paramref name="minBet"/>. With no argument the
    /// result is <paramref name="defaultBet"/> when given, otherwise <see cref="BetParse.Invalid"/>.
    /// </summary>
    internal static BetParse Parse(IReadOnlyList<string> args, long maxBet, long minBet, long? defaultBet, out long amount)
    {
        amount = 0;
        if (args.Count > 1)
        {
            return BetParse.Invalid;
        }

        if (args.Count == 0)
        {
            if (defaultBet is null)
            {
                return BetParse.Invalid;
            }

            amount = defaultBet.Value;
        }
        else
        {
            var token = args[0];
            if (token.Equals("all", StringComparison.OrdinalIgnoreCase) || token.Equals("max", StringComparison.OrdinalIgnoreCase))
            {
                amount = maxBet;
            }
            else if (token.EndsWith('%'))
            {
                if (!double.TryParse(token[..^1], NumberStyles.Float, CultureInfo.InvariantCulture, out var percent)
                    || !double.IsFinite(percent) || percent <= 0 || percent > 100)
                {
                    return BetParse.Invalid;
                }

                amount = (long)(maxBet * (percent / 100d));
            }
            else if (!long.TryParse(token, NumberStyles.None, CultureInfo.InvariantCulture, out amount))
            {
                return BetParse.Invalid;
            }
        }

        if (amount > maxBet)
        {
            return BetParse.TooMuch;
        }

        return amount < minBet ? BetParse.TooLow : BetParse.Ok;
    }
}

using System.Globalization;
using System.Text;
using WaddleSdk.State;

namespace WaddleBundleSuperpenguinChannelpoints;

/// <summary>
/// Parsing and text safety for the redemption policy: the `!redeem map` reference/action grammar, mapping keys, and the sanitising
/// every viewer- or streamer-supplied string goes through before it is echoed to chat or the overlay. Pure functions, no host calls.
/// </summary>
internal static class RedeemGrammar
{
    internal const int MaxSteps = 3;
    internal const long MaxAward = 1000;
    internal const int MaxTemplate = 200;
    internal const int MaxReference = 64;
    internal const int MaxViewerText = 100;

    /// <summary>Characters stripped from the front of viewer text so the bot can never be made to emit a chat command.</summary>
    private static readonly char[] CommandLeaders = ['!', '/', '.', '\\', '~', '$', '%', ' '];

    /// <summary>
    /// Splits `text` into a reward reference and the remaining action text. A reference is a double-quoted title (spaces allowed)
    /// or a single token (a reward UUID or a one-word title).
    /// </summary>
    internal static bool TryParseReference(string text, out string reference, out string rest)
    {
        reference = string.Empty;
        rest = string.Empty;
        var trimmed = text.Trim();
        if (trimmed.Length == 0)
        {
            return false;
        }

        if (trimmed[0] == '"')
        {
            var close = trimmed.IndexOf('"', 1);
            if (close < 0)
            {
                return false;
            }

            reference = trimmed[1..close].Trim();
            rest = trimmed[(close + 1)..].Trim();
        }
        else
        {
            var space = trimmed.IndexOfAny([' ', '\t']);
            reference = space < 0 ? trimmed : trimmed[..space];
            rest = space < 0 ? string.Empty : trimmed[(space + 1)..].Trim();
        }

        return reference.Length is > 0 and <= MaxReference;
    }

    /// <summary>The mapping key for a reference: `id.&lt;guid&gt;` for a UUID, else `title.&lt;hash of the lower-cased title&gt;`.</summary>
    internal static string KeyFor(string reference) =>
        Guid.TryParseExact(reference.Trim(), "D", out var id) ? "id." + id.ToString("D") : KeyForTitle(reference);

    /// <summary>The title-form key (case-insensitive, so `Hydrate` and `hydrate` are the same reward).</summary>
    internal static string KeyForTitle(string title) => "title." + StateKey.Hash(title.Trim().ToLowerInvariant());

    /// <summary>
    /// Parses `award &lt;1..1000&gt;`, `say &lt;template&gt;`, `alert &lt;text&gt;` steps joined by `&amp;&amp;` (at most
    /// <see cref="MaxSteps"/>, at most one award). Returns false with a user-facing reason in <paramref name="error"/>.
    /// </summary>
    internal static bool TryParseSteps(string text, out List<RedeemStep> steps, out string error)
    {
        steps = [];
        error = string.Empty;
        if (string.IsNullOrWhiteSpace(text))
        {
            error = "Usage: !redeem map <reward-id|\"title\"> award <n> && say <text> && alert <text>";
            return false;
        }

        var parts = text.Split("&&", StringSplitOptions.TrimEntries);
        if (parts.Length > MaxSteps)
        {
            error = $"At most {MaxSteps} steps per mapping.";
            return false;
        }

        foreach (var part in parts)
        {
            var space = part.IndexOf(' ');
            var kind = (space < 0 ? part : part[..space]).ToLowerInvariant();
            var arg = space < 0 ? string.Empty : part[(space + 1)..].Trim();
            switch (kind)
            {
                case "award":
                    if (steps.Any(s => s.Kind == "award"))
                    {
                        error = "Only one award step per mapping.";
                        return false;
                    }

                    if (!long.TryParse(arg, NumberStyles.None, CultureInfo.InvariantCulture, out var amount) || amount < 1 || amount > MaxAward)
                    {
                        error = $"award needs a whole amount from 1 to {MaxAward}.";
                        return false;
                    }

                    steps.Add(new RedeemStep("award", amount, string.Empty));
                    break;
                case "say":
                case "alert":
                    var template = StripControl(arg);
                    if (template.Length == 0 || template.Length > MaxTemplate)
                    {
                        error = $"{kind} needs text of 1 to {MaxTemplate} characters.";
                        return false;
                    }

                    steps.Add(new RedeemStep(kind, 0, template));
                    break;
                default:
                    error = "Steps are: award <n>, say <text>, alert <text> (joined with &&).";
                    return false;
            }
        }

        return true;
    }

    /// <summary>One-line description of a step list for chat replies and `!redeem list`.</summary>
    internal static string Describe(IEnumerable<RedeemStep> steps) =>
        string.Join(" + ", steps.Select(s => s.Kind == "award" ? $"award {s.Amount}" : $"{s.Kind} \"{Truncate(s.Text, 30)}\""));

    /// <summary>
    /// Makes viewer- or reward-supplied text safe to echo: no control characters, no braces (so it cannot forge a `{user:...}`
    /// placeholder), no `@` (no mass mentions), no leading command characters, whitespace collapsed, at most
    /// <see cref="MaxViewerText"/> characters.
    /// </summary>
    internal static string Clean(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        var sb = new StringBuilder(value.Length);
        foreach (var c in value)
        {
            if (char.IsControl(c))
            {
                sb.Append(' ');
            }
            else if (c is not ('{' or '}' or '@'))
            {
                sb.Append(c);
            }
        }

        var collapsed = string.Join(' ', sb.ToString().Split(' ', StringSplitOptions.RemoveEmptyEntries)).TrimStart(CommandLeaders);
        return Truncate(collapsed, MaxViewerText);
    }

    /// <summary>Control characters replaced by spaces and whitespace collapsed (for streamer-authored templates).</summary>
    internal static string StripControl(string value)
    {
        var sb = new StringBuilder(value.Length);
        foreach (var c in value)
        {
            sb.Append(char.IsControl(c) ? ' ' : c);
        }

        return string.Join(' ', sb.ToString().Split(' ', StringSplitOptions.RemoveEmptyEntries));
    }

    /// <summary>Expands the documented placeholders of a template; unknown `{tokens}` are left untouched.</summary>
    internal static string Render(string template, string userPlaceholder, string title, long cost, string input, long amount, string currency)
    {
        var text = template;
        foreach (var token in new[] { "{user}", "{displayname}" })
        {
            text = text.Replace(token, userPlaceholder, StringComparison.OrdinalIgnoreCase);
        }

        foreach (var token in new[] { "{reward}", "{rewardtitle}" })
        {
            text = text.Replace(token, title, StringComparison.OrdinalIgnoreCase);
        }

        foreach (var token in new[] { "{cost}", "{rewardcost}" })
        {
            text = text.Replace(token, cost.ToString(CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase);
        }

        foreach (var token in new[] { "{input}", "{userinput}", "{message}" })
        {
            text = text.Replace(token, input, StringComparison.OrdinalIgnoreCase);
        }

        text = text.Replace("{amount}", amount.ToString(CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase);
        return text.Replace("{currency}", currency, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>`value` cut to at most `max` characters.</summary>
    internal static string Truncate(string value, int max) => value.Length <= max ? value : value[..max];
}

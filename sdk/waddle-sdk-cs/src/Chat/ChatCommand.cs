using WaddleSdk.Json;

namespace WaddleSdk.Chat;

/// <summary>
/// A parsed chat command: `{prefix}{name} {args...}`, e.g. `!roll 20` under
/// prefix `!` parses to <c>Name = "roll"</c>, <c>Args = ["20"]</c>. Every
/// Tier-1 SDK sibling and the C# spike (`bundles/csharp/csping`) re-implements
/// this exact matching logic by hand against raw `JsonNode` -- this is the
/// shared, tested version every C# bundle should use instead.
/// </summary>
public sealed record ChatCommand(string Prefix, string Name, IReadOnlyList<string> Args, string? ChannelId, string RawText)
{
    /// <summary>
    /// Parses <paramref name="payloadJson"/> (a `chat.message` event's `payload-json`) as a
    /// command under <paramref name="prefix"/> (e.g. `"!"`). Returns null -- never throws --
    /// for non-JSON, non-object, missing-`text`, or non-matching-prefix input: a bundle must
    /// never fail the pipeline over an event it was never meant to react to.
    /// </summary>
    public static ChatCommand? TryParse(string payloadJson, string prefix)
    {
        if (string.IsNullOrEmpty(prefix))
        {
            throw new ArgumentException("prefix must be non-empty", nameof(prefix));
        }

        ChatMessagePayload? payload;
        try
        {
            payload = System.Text.Json.JsonSerializer.Deserialize(payloadJson, WaddleSdkJsonContext.Default.ChatMessagePayload);
        }
        catch (System.Text.Json.JsonException)
        {
            return null;
        }

        if (payload is null)
        {
            return null;
        }

        var text = payload.Text.Trim();
        if (!text.StartsWith(prefix, StringComparison.Ordinal) || text.Length == prefix.Length)
        {
            return null;
        }

        // `withoutPrefix` can never be empty-after-splitting here: `text` was
        // `Trim()`-ed above (stripping any trailing whitespace from the WHOLE
        // string), so if `withoutPrefix` (a suffix of `text`) consisted
        // entirely of spaces, `text` itself would have had trailing
        // whitespace -- contradicting the `Trim()` above. Combined with the
        // length check just above (ruling out an empty `withoutPrefix`),
        // `Split(' ', RemoveEmptyEntries)` always yields at least one part.
        var withoutPrefix = text[prefix.Length..];
        var parts = withoutPrefix.Split(' ', StringSplitOptions.RemoveEmptyEntries);

        return new ChatCommand(prefix, parts[0], parts[1..], payload.ChannelId, text);
    }

    /// <summary>True if <see cref="Name"/> equals <paramref name="name"/>, case-sensitively --
    /// command names are matched exactly, same convention as every Tier-1 SDK sibling.</summary>
    public bool Is(string name) => string.Equals(Name, name, StringComparison.Ordinal);

    /// <summary>True if <see cref="Name"/> equals any of <paramref name="names"/> -- for bundles
    /// registering multiple aliases for one command (e.g. `!roll`/`!dice`).</summary>
    public bool IsAny(params ReadOnlySpan<string> names)
    {
        foreach (var name in names)
        {
            if (Is(name))
            {
                return true;
            }
        }

        return false;
    }
}

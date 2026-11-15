using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRaidtracker;

/// <summary>Role flags the host stamps on a chat payload (`is_mod`, `is_broadcaster`); anything but a real JSON boolean is "unknown".</summary>
internal sealed record RoleFlags(
    [property: JsonPropertyName("is_mod")] bool? IsMod,
    [property: JsonPropertyName("is_broadcaster")] bool? IsBroadcaster);

/// <summary>Source-generated JSON context for <see cref="RoleFlags"/> (no reflection JSON, ever).</summary>
[JsonSerializable(typeof(RoleFlags))]
internal sealed partial class RoleJsonContext : JsonSerializerContext;

/// <summary>
/// Shared helpers every SuperPenguinTV utility port uses: strict role reads (fail CLOSED when role info is missing or is not a
/// real boolean), chat replies, a replay claim keyed on the host message id, and the numeric kv codec.
/// </summary>
internal static class Kit
{
    /// <summary>Reads the role flags; a payload that is not JSON or carries non-boolean flags yields "unknown" for both.</summary>
    internal static RoleFlags Roles(string payloadJson)
    {
        try
        {
            return JsonSerializer.Deserialize(payloadJson, RoleJsonContext.Default.RoleFlags) ?? new RoleFlags(null, null);
        }
        catch (JsonException)
        {
            return new RoleFlags(null, null);
        }
    }

    /// <summary>True only when the host explicitly flagged the sender as moderator or broadcaster.</summary>
    internal static bool IsModerator(string payloadJson)
    {
        var r = Roles(payloadJson);
        return r.IsMod == true || r.IsBroadcaster == true;
    }

    /// <summary>True only when the host explicitly flagged the sender as the broadcaster (the original's `Rank.Streamer`).</summary>
    internal static bool IsBroadcaster(string payloadJson) => Roles(payloadJson).IsBroadcaster == true;

    /// <summary>A chat reply on the triggering event's own platform/channel.</summary>
    internal static PlatformEventInfo Reply(PlatformEventInfo @event, string? channelId, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, channelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>Reply helper for a parsed command.</summary>
    internal static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) => Reply(@event, command.ChannelId, text);

    /// <summary>
    /// Resolves the invoking actor, or returns null after logging when the account is not linked / not a member (callers decide
    /// whether to tell the user). Every other identity failure propagates (fail loud).
    /// </summary>
    internal static string? TryActor(IWaddleHostNext host, string logEvent)
    {
        try
        {
            return host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info(logEvent, new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return null;
        }
    }

    /// <summary>
    /// First-writer-wins claim on this exact invocation (message id + platform + actor + command). Returns false on a redelivery of
    /// an event whose mutation already ran, so a replay can never apply a change twice. A blank message id is refused with an
    /// exception: without it every event would collide on one key.
    /// </summary>
    internal static bool ClaimOnce(IWaddleHostNext host, IStateStore store, string prefix, PlatformEventInfo @event, string actor, string command)
    {
        var messageId = host.Context.MessageId;
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new InvalidOperationException("the host supplied no message id; refusing to mutate state without a replay key");
        }

        return store.TryClaim(StateKey.Of(prefix, StateKey.Hash($"{messageId}|{@event.Platform}|{actor}|{command}")), 3600);
    }

    /// <summary>Reads a decimal long stored as ASCII; absent or unparsable is 0.</summary>
    internal static long ReadLong(IStateStore store, string key)
    {
        var bytes = store.Get(key);
        return bytes is not null && long.TryParse(System.Text.Encoding.UTF8.GetString(bytes), NumberStyles.Integer, CultureInfo.InvariantCulture, out var v) ? v : 0;
    }

    /// <summary>Stores a long as ASCII decimal.</summary>
    internal static void WriteLong(IStateStore store, string key, long value, uint ttlSeconds = 0) =>
        store.Set(key, System.Text.Encoding.UTF8.GetBytes(value.ToString(CultureInfo.InvariantCulture)), ttlSeconds);

    /// <summary>Strips control characters and the `{`/`}` placeholder delimiters from free text and caps its length.</summary>
    internal static string Clean(string text, int max)
    {
        var sb = new System.Text.StringBuilder(text.Length);
        foreach (var c in text)
        {
            if (!char.IsControl(c) && c is not '{' and not '}')
            {
                sb.Append(c);
            }
        }

        var s = sb.ToString().Trim();
        return s.Length > max ? s[..max] : s;
    }

    /// <summary>The unparsed argument text after the command name, joined and trimmed.</summary>
    internal static string RestOf(ChatCommand command) => string.Join(' ', command.Args).Trim();
}

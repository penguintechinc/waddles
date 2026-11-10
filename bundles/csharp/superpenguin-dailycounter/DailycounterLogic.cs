using System.Globalization;
using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinDailycounter;

/// <summary>
/// The streamer's daily counter, ported from PenguinTwitchBot's `Misc/DailyCounter.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT,
/// used with permission -- see `bundle.yaml`'s `notice`): `!setdailycountertext FORMAT` (`{value}` and `{nextgoal}` placeholders) and
/// `!setdailyvalue N`, both broadcaster-only like the original's `Rank.Streamer`. The next goal is the next multiple of ten above the
/// current value, exactly as in the original.
///
/// <para><b>Deviations (documented).</b> (1) The original increments on subscription / gift-sub events and writes
/// `Data/DailyCounter.txt` for an OBS text source. A bundle receives chat events only and has no file system, so: `!dailycounter`
/// (anyone) answers with the rendered text, and a moderator-only `!dailyadd [N]` (default 1, max 1000) stands in for the sub events
/// until a subscription event contract reaches bundles. (2) `!setdailyvalue` clamps to 0..1,000,000,000. (3) Each setter now answers
/// with the rendered text (the original is silent). (4) Role flags are the host-stamped `is_mod` / `is_broadcaster` booleans and fail
/// CLOSED when absent.</para>
///
/// <para><b>Replay.</b> Every mutation is claimed once per (message id, platform, actor, command). kv has no compare-and-swap, so two
/// different `!dailyadd` messages racing can lose an update.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class DailycounterLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-dailycounter";
    internal const string ValueKey = "dd.value";
    internal const string FormatKey = "dd.format";
    internal const string DefaultFormat = "Daily count: {value} (next goal: {nextgoal})";
    internal const long MaxValue = 1_000_000_000;
    internal const int MaxFormatLength = 200;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.IsAny("dailycounter", "setdailycountertext", "setdailyvalue", "dailyadd"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("daily.refused", new LogFields().With("reason", "no_community").Build());
            return Kit.Reply(@event, command, "The daily counter needs a community; this channel is not linked to one.");
        }

        var store = new KvStateStore(host.Kv);
        if (command.Is("dailycounter"))
        {
            return Kit.Reply(@event, command, Render(store));
        }

        var allowed = command.Is("dailyadd") ? Kit.IsModerator(@event.PayloadJson) : Kit.IsBroadcaster(@event.PayloadJson);
        if (!allowed)
        {
            host.Log.Info("daily.denied", new LogFields().With("command", command.Name).Build());
            return null;
        }

        var actor = Kit.TryActor(host, "daily.actor_unlinked");
        if (actor is null)
        {
            return Kit.Reply(@event, command, "Link your account to this community before changing the daily counter.");
        }

        if (Kit.ClaimOnce(host, store, "dd.m", @event, actor, command.Name))
        {
            Apply(store, command);
            host.Log.Info("daily.mutated", new LogFields().With("command", command.Name).Build());
        }

        return Kit.Reply(@event, command, Render(store));
    }

    private static void Apply(IStateStore store, ChatCommand command)
    {
        switch (command.Name)
        {
            case "setdailycountertext":
                var format = Sanitize(Kit.RestOf(command));
                if (format.Length > 0)
                {
                    store.Set(FormatKey, Encoding.UTF8.GetBytes(format), 0);
                }

                break;
            case "setdailyvalue":
                if (command.Args.Count > 0 && long.TryParse(command.Args[0], NumberStyles.Integer, CultureInfo.InvariantCulture, out var value))
                {
                    Kit.WriteLong(store, ValueKey, Math.Clamp(value, 0, MaxValue));
                }

                break;
            default:
                var delta = 1L;
                if (command.Args.Count > 0 && (!long.TryParse(command.Args[0], NumberStyles.Integer, CultureInfo.InvariantCulture, out delta) || delta < 1 || delta > 1000))
                {
                    return;
                }

                Kit.WriteLong(store, ValueKey, Math.Min(MaxValue, Kit.ReadLong(store, ValueKey) + delta));
                break;
        }
    }

    private static string Render(IStateStore store)
    {
        var bytes = store.Get(FormatKey);
        var format = bytes is null ? DefaultFormat : Encoding.UTF8.GetString(bytes);
        var value = Kit.ReadLong(store, ValueKey);
        return format
            .Replace("{value}", value.ToString(CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase)
            .Replace("{nextgoal}", NextValue(value).ToString(CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>The next multiple of ten above <paramref name="current"/> (the original's `NextValue`; 0 and below give 10).</summary>
    internal static long NextValue(long current)
    {
        var next = current + 1;
        var goal = (long)Math.Ceiling(next / 10.0) * 10;
        return goal == 0 ? 10 : goal;
    }

    private static string Sanitize(string text)
    {
        var sb = new StringBuilder(text.Length);
        foreach (var c in text)
        {
            if (!char.IsControl(c))
            {
                sb.Append(c);
            }
        }

        var s = sb.ToString().Trim();
        return s.Length > MaxFormatLength ? s[..MaxFormatLength] : s;
    }
}

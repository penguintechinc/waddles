using System.Globalization;
using WaddleSdk.Chat;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinViewers;

/// <summary>
/// Viewer presence policy, ported from PenguinTwitchBot's `Features/ViewerFeature.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used
/// with permission -- see `bundle.yaml`'s `notice`). The original is the bot's viewer directory; the part that is app POLICY is the
/// presence model: a viewer is ACTIVE for 15 minutes after they chat, `!lurk` keeps them counted for an hour, and stale entries are
/// dropped after two hours. Every chat message from a linked member refreshes their entry; `!lurk` marks a lurker (silent, as in the
/// original); `!viewers` reports the current active count (an addition: the original exposes the same numbers to its web UI and metrics).
///
/// <para><b>Not ported (skipped as redundant with the platform, none silent).</b> The viewer table itself (names, display names, Twitch
/// user ids, sub/mod/vip/editor flags, follower and account-age lookups) is the platform's identity service and the Twitch SERVICE bundle's
/// `get_chatters` / `get_followers` / `get_subscriptions` tools, so no private viewer database is created; the follow thank-you and the
/// hourly subscriber/editor sync need platform events and a scheduler no app bundle has; the 5-minute viewer cache and Prometheus gauges
/// are host concerns. Last-seen reads live in `superpenguin-lastseen`.</para>
///
/// <para><b>Bounds.</b> At most 200 viewers are tracked (the stalest is evicted first), so a `!viewers` read is one kv read. A refresh is
/// throttled to once per 30 seconds per viewer (a chatty viewer costs one write per 30 s, not per message). The presence document is
/// one kv value with no compare-and-swap: two messages in the same instant can lose one refresh, which only shifts a viewer's
/// timestamp by seconds. Every chat message must reach this bundle, so the manifest declares no command-prefix filter.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class ViewersLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-viewers";
    internal const string DocKey = "vw.doc";
    internal const int MaxTracked = 200;
    internal const long ActiveWindowMillis = 15 * 60 * 1000;
    internal const long LurkWindowMillis = 60 * 60 * 1000;
    internal const long StaleMillis = 2 * 60 * 60 * 1000;
    internal const long RefreshThrottleMillis = 30 * 1000;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (!host.Flags.Enabled(FlagKey, false) || host.Context.Community is null)
        {
            return null;
        }

        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        var now = (long)host.Clock.NowMillis();
        var store = new KvStateStore(host.Kv);
        var doc = store.GetJson(DocKey, ViewersJsonContext.Default.ViewerDoc) ?? new ViewerDoc([]);

        var actor = Kit.TryActor(host, "viewers.actor_unlinked");
        if (actor is not null && Refresh(doc, actor, now, command is not null && command.Is("lurk")))
        {
            store.SetJson(DocKey, doc, ViewersJsonContext.Default.ViewerDoc);
        }

        if (command is not null && command.Is("viewers"))
        {
            var (active, lurking) = Count(doc, now);
            host.Log.Info("viewers.reported", new LogFields().With("active", active).With("lurking", lurking).Build());
            return Kit.Reply(@event, command, $"Active viewers: {active.ToString(CultureInfo.InvariantCulture)} ({lurking.ToString(CultureInfo.InvariantCulture)} lurking)");
        }

        return null;
    }

    /// <summary>
    /// Updates (or adds) <paramref name="user"/>, drops stale entries and enforces the size cap. Returns false when nothing changed
    /// (a refresh inside the throttle window that is not a lurk), so the caller can skip the write.
    /// </summary>
    internal static bool Refresh(ViewerDoc doc, string user, long now, bool lurk)
    {
        var index = doc.Viewers.FindIndex(v => v.User == user);
        if (index >= 0 && !lurk && now - doc.Viewers[index].LastActiveMillis < RefreshThrottleMillis && now >= doc.Viewers[index].LastActiveMillis)
        {
            return false;
        }

        var previous = index >= 0 ? doc.Viewers[index] : null;
        var entry = new ViewerEntry(user, now, lurk ? now : previous?.LurkMillis ?? 0);
        if (index >= 0)
        {
            doc.Viewers[index] = entry;
        }
        else
        {
            doc.Viewers.Add(entry);
        }

        doc.Viewers.RemoveAll(v => now - Math.Max(v.LastActiveMillis, v.LurkMillis) > StaleMillis);
        while (doc.Viewers.Count > MaxTracked)
        {
            var oldest = doc.Viewers.MinBy(v => v.LastActiveMillis)!;
            doc.Viewers.Remove(oldest);
        }

        return true;
    }

    /// <summary>Active viewers (chatted within 15 minutes, or lurked within the hour) and how many of them are lurking.</summary>
    internal static (int Active, int Lurking) Count(ViewerDoc doc, long now)
    {
        var active = 0;
        var lurking = 0;
        foreach (var v in doc.Viewers)
        {
            var chatted = now - v.LastActiveMillis <= ActiveWindowMillis;
            var lurker = v.LurkMillis > 0 && now - v.LurkMillis <= LurkWindowMillis;
            if (chatted || lurker)
            {
                active++;
            }

            if (lurker)
            {
                lurking++;
            }
        }

        return (active, lurking);
    }
}

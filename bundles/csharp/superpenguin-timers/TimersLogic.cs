using System.Globalization;
using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Log;
using WaddleSdk.Random;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinTimers;

/// <summary>
/// Announcement timers, ported from PenguinTwitchBot's `Misc/AutoTimers.cs` and `TimerGroup` (github.com/Psychoboy/PenguinTwitchBot,
/// MIT, used with permission -- see `bundle.yaml`'s `notice`). A timer group has an interval range (a random delay between its minimum
/// and maximum seconds), a minimum number of chat messages that must pass since its last run, a shuffle bag, a repeat flag, and the
/// announcement lines it cycles. Moderators manage groups with `!timer create|addline|delline|enable|disable|delete|list|run`.
///
/// <para><b>NO SCHEDULER (the load-bearing limitation).</b> No WIT world gives an app bundle a timer, so nothing fires on its own: the
/// original's 1-second timer thread is emulated by checking the due groups on EVERY chat message this bundle receives (the same
/// tick-on-event idea as the SDK's `TickScheduler`, kept inline because the whole schedule is one small document). Consequences: (1) a
/// group is announced no earlier than its due time and only when a chat message arrives, so a quiet channel announces late (which also
/// matches the minimum-message rule, which needs traffic anyway); (2) at most ONE announcement rides each event, and an event that is
/// itself a `!timer` command answers the command instead, so a second due group waits for the next message; (3) the announcement is sent
/// to the channel of the message that triggered it, so a community with several channels announces where people are talking. Real
/// scheduled delivery needs the `scheduled-stage` host export.</para>
///
/// <para><b>Other deviations (documented).</b> (1) The original runs arbitrary ACTIONS from a timer group; this port announces plain
/// chat lines (the action engine is not an app concern). (2) The original's online-only flag needs the stream-online signal that no world
/// exposes, so every group runs whenever there is chat activity; the original's reset of counters at stream start is likewise absent.
/// (3) Limits: 10 groups, 15 lines of 200 characters each, intervals 10..86400 s, minimum messages 0..1000.</para>
///
/// <para><b>Replay and races.</b> A due window is claimed by (group, due time) so racing events announce a group once; a redelivered
/// management command is claimed once per invocation (kv has no compare-and-swap, so two moderators editing in the same instant can lose
/// an edit). Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).</para>
/// </summary>
public sealed class TimersLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-timers";
    internal const string DocKey = "tm.cfg";
    internal const string CounterKey = "tm.mc";
    internal const int MaxGroups = 10;
    internal const int MaxLines = 15;
    internal const int MaxLineLength = 200;
    internal const int MaxNameLength = 32;
    internal const int MaxDocBytes = 60_000;
    internal const int DefaultMin = 300;
    internal const int DefaultMax = 900;
    internal const int DefaultMessages = 15;
    internal const string Usage =
        "Usage: !timer create <name> [minSec maxSec minMessages] [once] | addline <name> <text> | delline <name> <n> | shuffle <name> on|off | enable|disable|delete|run <name> | list";

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (!host.Flags.Enabled(FlagKey, false) || host.Context.Community is null)
        {
            return null;
        }

        var store = new KvStateStore(host.Kv);
        var count = host.Kv.Increment(CounterKey, 1, 7 * 24 * 3600);
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is not null && command.Is("timer"))
        {
            return Kit.Reply(@event, command, Manage(@event, host, store, command, count));
        }

        var announcement = AnnounceDue(host, store, count);
        return announcement is null ? null : Kit.Reply(@event, ChannelOf(@event), announcement);
    }

    private static string? ChannelOf(PlatformEventInfo @event) =>
        @event.Payload(WaddleSdk.Json.WaddleSdkJsonContext.Default.ChatMessagePayload)?.ChannelId;

    /// <summary>Announces the first due, claimable group (if any) and advances its schedule.</summary>
    private static string? AnnounceDue(IWaddleHostNext host, IStateStore store, long count)
    {
        var doc = Load(store);
        var now = (long)host.Clock.NowMillis();
        foreach (var group in doc.Groups.Where(g => g.Active && g.Lines.Count > 0 && now >= g.NextRun && count - g.LastCount >= g.MinimumMessages).OrderBy(g => g.NextRun))
        {
            if (!store.TryClaim(StateKey.Of("tm.run", StateKey.Hash(group.Name), group.NextRun.ToString(CultureInfo.InvariantCulture)), 3600))
            {
                continue;
            }

            var (line, next) = Fire(group, now, count);
            doc.Groups[doc.Groups.FindIndex(g => g.Name == group.Name)] = next;
            Save(store, doc);
            host.Log.Info("timers.announced", new LogFields().With("lines", group.Lines.Count).With("repeat", group.Repeat).Build());
            return line;
        }

        return null;
    }

    /// <summary>Picks the next line (shuffle bag or sequential) and returns the group advanced to its next run.</summary>
    internal static (string Line, TimerGroupState Next) Fire(TimerGroupState group, long now, long count)
    {
        string line;
        var bag = new List<int>(group.Bag.Where(i => i >= 0 && i < group.Lines.Count));
        var cursor = group.Cursor;
        if (group.Shuffle)
        {
            if (bag.Count == 0)
            {
                bag = Enumerable.Range(0, group.Lines.Count).ToList();
                for (var i = bag.Count - 1; i > 0; i--)
                {
                    var j = WaddleRandom.NextInt(0, i);
                    (bag[i], bag[j]) = (bag[j], bag[i]);
                }
            }

            line = group.Lines[bag[0]];
            bag.RemoveAt(0);
        }
        else
        {
            line = group.Lines[cursor % group.Lines.Count];
            cursor = (cursor + 1) % group.Lines.Count;
        }

        var delaySeconds = WaddleRandom.NextInt(group.IntervalMinimumSeconds, group.IntervalMaximumSeconds);
        return (line, group with { NextRun = now + delaySeconds * 1000L, LastCount = count, Bag = bag, Cursor = cursor, Active = group.Active && group.Repeat });
    }

    private static string Manage(PlatformEventInfo @event, IWaddleHostNext host, IStateStore store, ChatCommand command, long count)
    {
        var verb = command.Args.Count > 0 ? command.Args[0].ToLowerInvariant() : "list";
        if (verb == "list")
        {
            return List(Load(store));
        }

        if (verb is not ("create" or "addline" or "delline" or "enable" or "disable" or "delete" or "run" or "shuffle"))
        {
            return Usage;
        }

        if (!Kit.IsModerator(@event.PayloadJson))
        {
            host.Log.Info("timers.denied", new LogFields().With("verb", verb).Build());
            return "Only moderators can manage timers.";
        }

        var actor = Kit.TryActor(host, "timers.actor_unlinked");
        if (actor is null)
        {
            return "Link your account to this community before managing timers.";
        }

        if (!Kit.ClaimOnce(host, store, "tm.m", @event, actor, command.Name))
        {
            return "Already applied.";
        }

        var doc = Load(store);
        var reply = Apply(host, verb, command, doc, count);
        if (reply.Changed)
        {
            Save(store, doc);
            host.Log.Info("timers.managed", new LogFields().With("verb", verb).With("groups", doc.Groups.Count).Build());
        }

        return reply.Text;
    }

    private static (bool Changed, string Text) Apply(IWaddleHostNext host, string verb, ChatCommand command, TimerDoc doc, long count)
    {
        if (command.Args.Count < 2)
        {
            return (false, Usage);
        }

        var name = ParseName(command.Args[1]);
        if (name is null)
        {
            return (false, $"Timer names are 1-{MaxNameLength} characters of a-z, 0-9, _ and -.");
        }

        var index = doc.Groups.FindIndex(g => g.Name == name);
        if (verb == "create")
        {
            return Create(host, command, doc, name, index, count);
        }

        if (index < 0)
        {
            return (false, "No such timer.");
        }

        var group = doc.Groups[index];
        switch (verb)
        {
            case "delete":
                doc.Groups.RemoveAt(index);
                return (true, $"Timer {name} deleted.");
            case "enable":
            case "disable":
                var on = verb == "enable";
                doc.Groups[index] = group with { Active = on, NextRun = on ? (long)host.Clock.NowMillis() + group.IntervalMinimumSeconds * 1000L : group.NextRun, LastCount = on ? count : group.LastCount };
                return (true, $"Timer {name} {(on ? "enabled" : "disabled")}.");
            case "run":
                if (group.Lines.Count == 0)
                {
                    return (false, "That timer has no lines yet.");
                }

                var (line, next) = Fire(group, (long)host.Clock.NowMillis(), count);
                doc.Groups[index] = next with { Active = group.Active };
                return (true, line);
            case "shuffle":
                if (command.Args.Count < 3 || command.Args[2].ToLowerInvariant() is not ("on" or "off"))
                {
                    return (false, "Use !timer shuffle <name> on|off.");
                }

                var shuffle = command.Args[2].Equals("on", StringComparison.OrdinalIgnoreCase);
                doc.Groups[index] = group with { Shuffle = shuffle, Bag = [], Cursor = 0 };
                return (true, $"Timer {name} shuffle {(shuffle ? "on" : "off")}.");
            case "addline":
                return AddLine(command, doc, index, group);
            default:
                return DeleteLine(command, doc, index, group);
        }
    }

    private static (bool Changed, string Text) Create(IWaddleHostNext host, ChatCommand command, TimerDoc doc, string name, int index, long count)
    {
        if (index >= 0)
        {
            return (false, "That timer already exists.");
        }

        if (doc.Groups.Count >= MaxGroups)
        {
            return (false, $"This community already has the maximum of {MaxGroups} timers.");
        }

        var rest = command.Args.Skip(2).ToList();
        var repeat = true;
        if (rest.Count > 0 && rest[^1].Equals("once", StringComparison.OrdinalIgnoreCase))
        {
            repeat = false;
            rest.RemoveAt(rest.Count - 1);
        }

        int min = DefaultMin, max = DefaultMax, msgs = DefaultMessages;
        if (rest.Count > 0 &&
            !(rest.Count == 3 && TryInt(rest[0], 10, 86400, out min) && TryInt(rest[1], 10, 86400, out max) && TryInt(rest[2], 0, 1000, out msgs) && max >= min))
        {
            return (false, "Intervals are 10-86400 seconds with max >= min, and minimum messages 0-1000.");
        }

        var now = (long)host.Clock.NowMillis();
        doc.Groups.Add(new TimerGroupState(name, true, repeat, min, max, msgs, true, [], now + WaddleRandom.NextInt(min, max) * 1000L, count, [], 0));
        return (true, $"Timer {name} created ({min}-{max}s, {msgs} messages{(repeat ? string.Empty : ", once")}). Add lines with !timer addline {name} <text>.");
    }

    private static (bool Changed, string Text) AddLine(ChatCommand command, TimerDoc doc, int index, TimerGroupState group)
    {
        var text = Kit.Clean(string.Join(' ', command.Args.Skip(2)), MaxLineLength);
        if (text.Length == 0)
        {
            return (false, "You forgot the announcement text.");
        }

        if (group.Lines.Count >= MaxLines)
        {
            return (false, $"A timer holds at most {MaxLines} lines.");
        }

        var updated = group with { Lines = [.. group.Lines, text] };
        doc.Groups[index] = updated;
        if (System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(doc, TimersJsonContext.Default.TimerDoc).Length > MaxDocBytes)
        {
            doc.Groups[index] = group;
            return (false, "Timer storage is full.");
        }

        return (true, $"Line {updated.Lines.Count} added to {group.Name}.");
    }

    private static (bool Changed, string Text) DeleteLine(ChatCommand command, TimerDoc doc, int index, TimerGroupState group)
    {
        if (command.Args.Count < 3 || !TryInt(command.Args[2], 1, MaxLines, out var n) || n > group.Lines.Count)
        {
            return (false, $"Give the line number to delete (1-{Math.Max(1, group.Lines.Count)}).");
        }

        var lines = new List<string>(group.Lines);
        lines.RemoveAt(n - 1);
        doc.Groups[index] = group with { Lines = lines, Bag = [], Cursor = 0 };
        return (true, $"Line {n} removed from {group.Name}.");
    }

    private static string List(TimerDoc doc)
    {
        if (doc.Groups.Count == 0)
        {
            return "No timers are set.";
        }

        var sb = new StringBuilder("Timers: ");
        sb.AppendJoin("; ", doc.Groups.Select(g => $"{g.Name} ({(g.Active ? "on" : "off")}, {g.Lines.Count} lines, {g.IntervalMinimumSeconds}-{g.IntervalMaximumSeconds}s, {g.MinimumMessages} msgs)"));
        return sb.ToString();
    }

    private static TimerDoc Load(IStateStore store) =>
        store.GetJson(DocKey, TimersJsonContext.Default.TimerDoc) ?? new TimerDoc([]);

    private static void Save(IStateStore store, TimerDoc doc) =>
        store.SetJson(DocKey, doc, TimersJsonContext.Default.TimerDoc);

    private static bool TryInt(string raw, int min, int max, out int value) =>
        int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out value) && value >= min && value <= max;

    /// <summary>The validated lowercase timer name, or null.</summary>
    internal static string? ParseName(string raw)
    {
        var name = raw.ToLowerInvariant();
        return name.Length is >= 1 and <= MaxNameLength && name.All(c => c is >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '-') ? name : null;
    }
}

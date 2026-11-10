using System.Globalization;
using System.Text;
using WaddleSdk.Kv;
using WaddleSdk.Stage;
using WaddleSdk.State;

namespace WaddleBundleSuperpenguinRacing;

/// <summary>
/// The `kv` layout of racing and the small read/write helpers over it. Every key is `race.<32-hex race id>.<part>` (or `race.cur`),
/// hashed identifiers only, never a name. All of a race's keys are written with <see cref="Ttl"/> so an abandoned race cleans itself
/// up, and the race record outlives the longest tick-on-event delay the bundle supports (see README, host limits).
/// </summary>
internal static class RacingStore
{
    /// <summary>Seven days, the same lifetime as the SDK's scheduler board: a window must be picked up within it or its stakes cannot be refunded.</summary>
    internal const uint Ttl = 7 * 24 * 60 * 60;

    internal static string CurrentKey => StateKey.Of("race", "cur");

    internal static string RecordKey(string id) => StateKey.Of("race", id, "rec");

    internal static string ResultKey(string id) => StateKey.Of("race", id, "res");

    internal static string CountKey(string id) => StateKey.Of("race", id, "n");

    internal static string SlotKey(string id, int position) => StateKey.Of("race", id, "s", position.ToString(CultureInfo.InvariantCulture));

    internal static string MemberKey(string id, string actor) => StateKey.Of("race", id, "m", StateKey.Hash(actor));

    internal static string LaneKey(string id, string actor) => StateKey.Of("race", id, "p", StateKey.Hash(actor));

    internal static string RefundedKey(string id, string actor) => StateKey.Of("race", id, "r", StateKey.Hash(actor));

    /// <summary>The race a new joiner would join (the one the `race.cur` pointer names), or null.</summary>
    internal static RaceRecord? Current(IStateStore store)
    {
        var id = store.Get(CurrentKey);
        return id is null ? null : Load(store, Encoding.UTF8.GetString(id));
    }

    /// <summary>The stored record of race <paramref name="id"/>, or null when it never existed or has expired.</summary>
    internal static RaceRecord? Load(IStateStore store, string id) => store.GetJson(RecordKey(id), RacingJsonContext.Default.RaceRecord);

    /// <summary>Persists <paramref name="record"/> under its own id.</summary>
    internal static void Save(IStateStore store, RaceRecord record) =>
        store.SetJson(RecordKey(record.Id), record, RacingJsonContext.Default.RaceRecord, Ttl);

    /// <summary>Points `race.cur` at race <paramref name="id"/>.</summary>
    internal static void SetCurrent(IStateStore store, string id) => store.Set(CurrentKey, Encoding.UTF8.GetBytes(id), Ttl);

    /// <summary>Clears `race.cur` if (and only if) it still names race <paramref name="id"/>: a newer race must keep its pointer.</summary>
    internal static void ClearCurrent(IStateStore store, string id)
    {
        var current = store.Get(CurrentKey);
        if (current is not null && Encoding.UTF8.GetString(current) == id)
        {
            store.Delete(CurrentKey);
        }
    }

    /// <summary>
    /// The race's roster in grid order. Slots are claimed with the atomic kv counter, so a crash can leave a hole (skipped) or, on a
    /// redelivered join, a repeated actor (the first slot wins): every actor appears at most once, the lane is the index here.
    /// </summary>
    internal static List<RosterEntry> Roster(IKvClient kv, IStateStore store, RaceRecord record)
    {
        var count = (int)Math.Min(kv.Increment(CountKey(record.Id), 0, Ttl), 256);
        List<RosterEntry> roster = [];
        HashSet<string> seen = [];
        for (var position = 1; position <= count; position++)
        {
            var entry = store.GetJson(SlotKey(record.Id, position), RacingJsonContext.Default.RosterEntry);
            if (entry is not null && seen.Add(entry.Actor))
            {
                roster.Add(entry);
            }
        }

        return roster;
    }
}

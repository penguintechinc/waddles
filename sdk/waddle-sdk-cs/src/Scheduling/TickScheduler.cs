using WaddleSdk.Clock;
using WaddleSdk.State;

namespace WaddleSdk.Scheduling;

/// <summary>One deferred action: <see cref="Id"/> is unique per board, <see cref="Kind"/> tells the game what to do, <see cref="PayloadJson"/> carries its data.</summary>
public sealed record ScheduledTask(string Id, ulong DueAtMillis, string Kind, string PayloadJson);

/// <summary>The persisted pending-task list of one <see cref="TickScheduler"/> board.</summary>
public sealed record ScheduleBoard(List<ScheduledTask> Tasks);

/// <summary>
/// Tick-on-event EMULATION of a scheduler. No WIT world has a scheduler/timer export for app bundles yet
/// (`scheduled-stage` is planned for `stage-next`, not landed), so a deadline (heist/FFA join window, duel expiry, raffle close)
/// can only be observed when an event reaches the bundle. Call <see cref="TakeDue"/> at the top of every `transform`: it removes
/// and returns every task whose deadline has passed (lazy expiry), exactly once across racing events (the board mutation is a
/// compare-and-swap on <see cref="DbStateStore"/>; on kv a per-task <see cref="IStateStore.TryClaim"/> arbitrates).
///
/// <para><b>Limitation (document it in every bundle that uses this).</b> The bundle only receives events matching its own
/// `consumes` filters, i.e. its own command prefixes. A deadline therefore resolves when the NEXT command for the bundle
/// arrives, not at the deadline: a join window nobody touches again is settled late. Fixing this needs the host `scheduled-stage`
/// export (see docs/sdk-cs.md "Needs from S0"). Tasks overdue at pickup report <see cref="LateByMillis"/> so a game can choose
/// to void rather than pay out a stale window.</para>
/// </summary>
public sealed class TickScheduler(IStateStore store, IClockClient clock, string board)
{
    /// <summary>Upper bound on pending tasks per board (the board is one stored value, 64 KiB host cap).</summary>
    public const int MaxTasks = 64;

    private const uint BoardTtlSeconds = 7 * 24 * 60 * 60;
    private const uint ClaimTtlSeconds = 60 * 60;

    private string BoardKey => StateKey.Of("sched", board);

    /// <summary>Adds (or replaces, by id) a task; throws <see cref="InvalidOperationException"/> when the board is full -- never drops silently.</summary>
    public void Schedule(ScheduledTask task)
    {
        var full = false;
        var ok = store.Mutate(BoardKey, current =>
        {
            full = false;
            var tasks = Read(current);
            tasks.RemoveAll(t => t.Id == task.Id);
            if (tasks.Count >= MaxTasks)
            {
                full = true;
                return current;
            }

            tasks.Add(task);
            return Write(tasks);
        }, BoardTtlSeconds);

        if (full)
        {
            throw new InvalidOperationException($"scheduler board '{board}' is full ({MaxTasks} pending tasks)");
        }

        if (!ok)
        {
            throw new InvalidOperationException($"scheduler board '{board}' stayed contended; task '{task.Id}' was not stored");
        }
    }

    /// <summary>Removes a pending task; true if it was pending.</summary>
    public bool Cancel(string id)
    {
        var removed = false;
        store.Mutate(BoardKey, current =>
        {
            removed = false;
            var tasks = Read(current);
            removed = tasks.RemoveAll(t => t.Id == id) > 0;
            return tasks.Count == 0 ? null : Write(tasks);
        }, BoardTtlSeconds);
        return removed;
    }

    /// <summary>A snapshot of every pending task (due or not).</summary>
    public IReadOnlyList<ScheduledTask> Pending() => Read(store.Get(BoardKey));

    /// <summary>Removes and returns every task due at or before now; call at the top of every `transform`.</summary>
    public IReadOnlyList<ScheduledTask> TakeDue()
    {
        var now = clock.NowMillis();
        List<ScheduledTask> due = [];
        var ok = store.Mutate(BoardKey, current =>
        {
            due = [];
            var tasks = Read(current);
            if (tasks.Count == 0)
            {
                return current;
            }

            due = tasks.Where(t => t.DueAtMillis <= now).ToList();
            if (due.Count == 0)
            {
                return current;
            }

            var remaining = tasks.Where(t => t.DueAtMillis > now).ToList();
            return remaining.Count == 0 ? null : Write(remaining);
        }, BoardTtlSeconds);

        if (!ok)
        {
            return [];
        }

        // Exactly-once across racing events even on a store whose Mutate is not a real CAS (kv).
        return due.Where(t => store.TryClaim(StateKey.Of("sched.claim", board, StateKey.Hash(t.Id + "." + t.DueAtMillis)), ClaimTtlSeconds)).ToList();
    }

    /// <summary>How many milliseconds past its deadline <paramref name="task"/> is being picked up (0 if on time).</summary>
    public long LateByMillis(ScheduledTask task)
    {
        var now = (long)clock.NowMillis();
        return Math.Max(0, now - (long)task.DueAtMillis);
    }

    private static List<ScheduledTask> Read(byte[]? bytes)
    {
        if (bytes is null)
        {
            return [];
        }

        try
        {
            return System.Text.Json.JsonSerializer.Deserialize(bytes, Json.WaddleSdkJsonContext.Default.ScheduleBoard)?.Tasks ?? [];
        }
        catch (System.Text.Json.JsonException)
        {
            return [];
        }
    }

    private static byte[] Write(List<ScheduledTask> tasks) =>
        System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(new ScheduleBoard(tasks), Json.WaddleSdkJsonContext.Default.ScheduleBoard);
}

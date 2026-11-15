using System.Text;
using SuperPenguin.DropGameCore;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Scheduling;
using WaddleSdk.Stage;
using WaddleSdk.State;

namespace WaddleBundleSuperpenguinRacing;

/// <summary>
/// Settles race windows that have passed their deadline. It runs on the process stage at the top of every racing command
/// ("tick on event": the host has no timer, so a window is only noticed when the NEXT `!race` arrives; the stage that can move
/// money is process-only, so an action-stage/timer settlement is impossible today). The result is decided exactly once and frozen
/// in a result record BEFORE any money moves; every credit then uses a deterministic idempotency key (the race's opening message id
/// + `race.pay`/`race.refund` + the user), so a settlement that is retried from any later event, or re-run after a crash, can only
/// ever pay each racer once. Failures propagate (never an unpaid "success").
/// </summary>
internal sealed class RacingSettlement(IWaddleHostNext host, KvStateStore store, TickScheduler scheduler, RaceConfig config, string actor)
{
    /// <summary>The scheduler board name racing windows are tracked on.</summary>
    internal const string Board = "race";

    /// <summary>The task kind of a window deadline.</summary>
    internal const string CloseKind = "close";

    /// <summary>
    /// Settles every due window: each task the scheduler hands back (exactly once across racing events) plus, as a crash backstop,
    /// the current race if it is mid-settlement or overdue but its task was already claimed. Returns the chat notices to prepend.
    /// A task whose settlement throws is put back on the board (due now) before the exception propagates.
    /// </summary>
    internal List<string> Tick()
    {
        List<string> notices = [];
        foreach (var task in scheduler.TakeDue())
        {
            if (task.Kind != CloseKind)
            {
                continue;
            }

            try
            {
                var record = RacingStore.Load(store, task.Id);
                if (record is null)
                {
                    host.Log.Warn("racing.window_expired", new LogFields().With("race", task.Id).Build());
                    continue;
                }

                AddNotice(notices, Settle(record, Math.Max(scheduler.LateByMillis(task), Late(record))));
            }
            catch (Exception)
            {
                scheduler.Schedule(task with { DueAtMillis = host.Clock.NowMillis() });
                throw;
            }
        }

        var current = RacingStore.Current(store);
        if (current is not null && (current.Status == RaceStatus.Settling || (current.Status == RaceStatus.Open && host.Clock.NowMillis() >= current.ClosesAt)))
        {
            AddNotice(notices, Settle(current, Late(current)));
        }

        return notices;
    }

    /// <summary>Milliseconds past the window's deadline right now (0 when still open).</summary>
    private long Late(RaceRecord record) => Math.Max(0, (long)host.Clock.NowMillis() - (long)record.ClosesAt);

    private static void AddNotice(List<string> notices, string? text)
    {
        if (!string.IsNullOrEmpty(text))
        {
            notices.Add(text);
        }
    }

    /// <summary>
    /// Settles one window: decide (or reload) the result, pay it, close the race, publish the overlay envelope and return the chat
    /// notice. Null when the race was already finished or had nobody in it. A race recorded under a different game config is refused.
    /// </summary>
    internal string? Settle(RaceRecord record, long lateMs)
    {
        if (record.Status is RaceStatus.Done or RaceStatus.Void)
        {
            return null;
        }

        if (record.Config != RaceConfig.Id)
        {
            throw new InvalidOperationException($"race was recorded under game config '{record.Config}' but this build plays '{RaceConfig.Id}'; refusing to settle a different result");
        }

        var roster = RacingStore.Roster(host.Kv, store, record);
        var result = store.GetJson(RacingStore.ResultKey(record.Id), RacingJsonContext.Default.RaceResultRecord)
            ?? Decide(record, roster, lateMs);
        if (record.Status != RaceStatus.Settling)
        {
            record = record with { Status = RaceStatus.Settling };
            RacingStore.Save(store, record);
        }

        Pay(record, result);

        var voided = result.VoidReason.Length > 0;
        RacingStore.Save(store, record with { Status = voided ? RaceStatus.Void : RaceStatus.Done });
        RacingStore.ClearCurrent(store, record.Id);
        host.Log.Info("racing.settled", new LogFields()
            .With("race", record.Id)
            .With("racers", result.Racers.Count)
            .With("pot", (long)result.Pot)
            .With("void", result.VoidReason)
            .With("late_ms", lateMs)
            .With("seed", record.Seed)
            .Build());

        if (result.Racers.Count == 0)
        {
            return null;
        }

        var alias = DropSupport.Alias(host, actor, RacingOverlay.Game);
        var winner = voided ? result.Racers[0] : result.Racers.First(r => r.Place == 1);
        string data;
        string kind;
        if (voided)
        {
            kind = "void";
            data = RacingOverlay.VoidData(record.Id, result.VoidReason, result.Racers);
        }
        else
        {
            kind = "race";
            var outcome = RacingSimulation.Run(ParseSeed(record.Seed), result.Racers.Count, config with { Laps = record.Laps });
            data = RacingOverlay.RaceData(record.Id, config with { Laps = record.Laps }, outcome, result.Racers, result.Pot);
        }

        OverlayPublisher.Publish(host, store, RacingOverlay.Game, RacingOverlay.Widget,
            OverlayEnvelope.Build(RacingOverlay.Game, kind, record.Seed, winner.Actor, result.Pot, winner.Payout, data));
        return Describe(result, alias);
    }

    /// <summary>
    /// Decides the window once and freezes it: a window picked up later than the stale limit, or with fewer than the minimum racers,
    /// is voided (every stake refunded); otherwise the recorded seed runs the race and the pot is split by place.
    /// </summary>
    private RaceResultRecord Decide(RaceRecord record, List<RosterEntry> roster, long lateMs)
    {
        var pot = roster.Aggregate(0UL, (sum, r) => sum + r.Stake);
        var seed = ParseSeed(record.Seed);
        RaceResultRecord result;
        string voidReason = lateMs > config.StaleVoidSeconds * 1000L ? "late" : roster.Count < config.MinRacers ? "too_few" : string.Empty;
        if (voidReason.Length > 0)
        {
            var refunds = roster.Select((r, lane) => new RacerResult(r.Actor, r.Stake, lane, 0, r.Stake, -1, false)).ToList();
            result = new RaceResultRecord(voidReason, record.Seed, pot, refunds, []);
        }
        else
        {
            var outcome = RacingSimulation.Run(seed, roster.Count, config with { Laps = record.Laps });
            var payouts = RacingPayouts.Split(pot, roster.Count);
            var racers = new List<RacerResult>();
            for (var lane = 0; lane < roster.Count; lane++)
            {
                var place = outcome.Order.ToList().IndexOf(lane);
                racers.Add(new RacerResult(roster[lane].Actor, roster[lane].Stake, lane, place + 1, payouts[place], outcome.FinishMs[lane], outcome.Dnf[lane]));
            }

            result = new RaceResultRecord(string.Empty, record.Seed, pot, racers, []);
        }

        store.SetJson(RacingStore.ResultKey(record.Id), result, RacingJsonContext.Default.RaceResultRecord, RacingStore.Ttl);
        return result;
    }

    /// <summary>
    /// Credits every racer their payout (or refund) exactly once: the key is bound to the race's OPENING message id, so it is the same
    /// whichever event runs the settlement, and the progress is saved after each credit so a resumed settlement skips what is paid.
    /// </summary>
    private void Pay(RaceRecord record, RaceResultRecord result)
    {
        var operation = result.VoidReason.Length > 0 ? "race.refund" : "race.pay";
        var client = new EconomyServiceClient(host.Tool, record.OpenMessageId, operation);
        foreach (var racer in result.Racers)
        {
            if (racer.Payout == 0 || result.Paid.Contains(racer.Actor))
            {
                continue;
            }

            client.Credit(racer.Actor, racer.Payout);
            result.Paid.Add(racer.Actor);
            store.SetJson(RacingStore.ResultKey(record.Id), result, RacingJsonContext.Default.RaceResultRecord, RacingStore.Ttl);
        }
    }

    /// <summary>The chat notice for a settled window (names are host `{user:uuid}` placeholders).</summary>
    private static string Describe(RaceResultRecord result, string alias)
    {
        var count = result.Racers.Count;
        if (result.VoidReason.Length > 0)
        {
            var why = result.VoidReason == "late"
                ? "the join window was settled too long after it closed"
                : "it needed at least two racers";
            return $"The race was cancelled ({why}); {count} stake(s) refunded.";
        }

        var text = new StringBuilder("Race finished! ");
        var placed = result.Racers.Where(r => r.Place is >= 1 and <= 3).OrderBy(r => r.Place).ToList();
        for (var i = 0; i < placed.Count; i++)
        {
            var r = placed[i];
            text.Append(i == 0 ? string.Empty : ", ")
                .Append(Ordinal(r.Place)).Append(' ').Append(UserPlaceholder.For(r.Actor));
            if (r.Payout > 0)
            {
                text.Append(" (+").Append(r.Payout).Append(' ').Append(alias).Append(')');
            }
        }

        return text.Append($". {count} racers, pot {result.Pot} {alias}.").ToString();
    }

    private static string Ordinal(int place) => place switch { 1 => "1st", 2 => "2nd", _ => "3rd" };

    private static ulong ParseSeed(string hex) =>
        SeedDerivation.TryParseHex(hex, out var seed) ? seed : throw new InvalidOperationException("race record carries a malformed seed");
}

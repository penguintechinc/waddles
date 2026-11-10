using System.Globalization;
using System.Text.Json;
using SuperPenguinShared;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinGiveaway;

/// <summary>
/// The raffle half of the giveaway app, ported from PenguinTwitchBot's `TicketGames/RaffleRuntimeService.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission). Keyed raffles: a moderator starts one with a winner
/// count and a total award, everyone enters once for free, a moderator ends it; winners are drawn WITHOUT replacement with the
/// same provable derivation as the giveaway (the seed is committed at `raffle start`, before anyone has entered, and revealed at
/// `raffle end`), and each winner is paid <c>ceil(total / winners)</c> through the economy service.
///
/// <para><b>Retry safety.</b> The winners and per-winner award are persisted BEFORE any payout and every paid winner is marked
/// right after their credit, so a retried `raffle end` resumes instead of paying twice. Credits use a stable per-raffle id
/// (`raffle.&lt;key&gt;.&lt;round&gt;`), not the invocation's message id, so the host's idempotency key is the same on a retry; a
/// per-raffle lease claim reports <c>ending_in_progress</c> to a concurrent `end`. A winner who left the community or hit a cap is
/// recorded as failed and reported as <c>payout_failed</c>; any other economy failure propagates and releases the lease.</para>
/// </summary>
public sealed partial class GiveawayLogic
{
    internal const int MaxRaffleEntries = 1000;
    internal const int MaxRaffleWinners = 50;
    internal const long MaxRaffleAward = 1_000_000;
    internal const int MaxRaffleKeyLength = 24;
    internal const uint RaffleLeaseSeconds = 60;
    internal const uint RaffleSequenceTtlSeconds = 365 * 24 * 60 * 60;
    internal const string RaffleUsage =
        "Usage: !tgw raffle enter|count <key>. Moderators: raffle start <key> <winners> <totalAward> | end <key> | winners <key> <n> | award <key> <n>";

    private PlatformEventInfo? RaffleCommand(Ctx c, List<string> args)
    {
        if (args.Count == 0 || !TryRaffleKey(args.Count > 1 ? args[1] : string.Empty, out var key))
        {
            return Say(c, RaffleUsage);
        }

        var rest = args.Skip(2).ToList();
        return args[0].ToLowerInvariant() switch
        {
            "start" => RaffleStart(c, key, rest),
            "enter" => RaffleEnter(c, key),
            "count" => RaffleCount(c, key),
            "end" => RaffleEnd(c, key),
            "winners" => RaffleSetWinners(c, key, rest),
            "award" => RaffleSetAward(c, key, rest),
            _ => Say(c, RaffleUsage),
        };
    }

    private static bool TryRaffleKey(string raw, out string key)
    {
        key = raw.ToLowerInvariant();
        return key.Length is >= 1 and <= MaxRaffleKeyLength && key.All(ch => ch is >= 'a' and <= 'z' or >= '0' and <= '9');
    }

    private static string RaffleDocKey(string key) => StateKey.Of("tgw", "raffle", key);

    private static RaffleDoc? LoadRaffle(Ctx c, string key) => c.Store.GetJson(RaffleDocKey(key), GiveawayJsonContext.Default.RaffleDoc);

    private static void SaveRaffle(Ctx c, RaffleDoc doc) =>
        c.Store.SetJson(RaffleDocKey(doc.Key), doc, GiveawayJsonContext.Default.RaffleDoc, StateTtlSeconds);

    /// <summary>The label a raffle's seed commitment is bound to (the entrants are unknown at start).</summary>
    internal static string RaffleLabel(string key, long round) => $"{Fairness.Version}-raffle|{key}|{round.ToString(CultureInfo.InvariantCulture)}";

    /// <summary>The fingerprint the raffle winners are derived from: the label plus the hash of the sorted entrant uuids.</summary>
    internal static string RaffleDrawFingerprint(string key, long round, IReadOnlyList<string> sortedEntrants) =>
        RaffleLabel(key, round) + "|" + Sha256.Hex(Sha256.Hash(string.Join("\n", sortedEntrants)));

    private static string NotRunning(string key) => $"There is no running raffle '{key}' (not_running).";

    private static string Ending(string key) => $"Raffle '{key}' is being ended (ending_in_progress).";

    private PlatformEventInfo? RaffleStart(Ctx c, string key, List<string> rest)
    {
        if (rest.Count != 2
            || !int.TryParse(rest[0], NumberStyles.None, CultureInfo.InvariantCulture, out var winners) || winners is < 1 or > MaxRaffleWinners
            || !long.TryParse(rest[1], NumberStyles.None, CultureInfo.InvariantCulture, out var total) || total > MaxRaffleAward)
        {
            return Say(c, $"Usage: !tgw raffle start <key> <winners 1-{MaxRaffleWinners}> <totalAward 0-{MaxRaffleAward}> (invalid_request)");
        }

        var tag = EventTag(c);
        var doc = LoadRaffle(c, key);
        if (doc is null)
        {
            var round = c.Host.Kv.Increment(StateKey.Of("tgw", "rseq", key), 1, RaffleSequenceTtlSeconds);
            var seed = Fairness.NewSeed(c.Host.Clock.NowMillis(), c.Host.Context.MessageId, round, _seedFill);
            doc = new RaffleDoc(key, round, winners, total, [], Sha256.Hex(seed), Fairness.Commitment(seed, RaffleLabel(key, round)), tag, null, 0, null, null);
            SaveRaffle(c, doc);
            c.Host.Log.Info("raffle.started", new LogFields().With("winners", winners).With("total", total).Build());
        }
        else if (doc.StartTag != tag)
        {
            return Say(c, $"Raffle '{key}' is already running (already_running) with {doc.Entries.Count} entries.");
        }

        var alias = Replies.Alias(c.Host, c.Actor, "raffle");
        return Say(c, $"Raffle '{doc.Key}' started: {doc.Winners} winner(s) share {doc.Total} {alias}. Type !tgw raffle enter {doc.Key} to join. Commitment: {doc.Commitment}");
    }

    private PlatformEventInfo? RaffleEnter(Ctx c, string key)
    {
        var doc = LoadRaffle(c, key);
        if (doc is null)
        {
            return Say(c, NotRunning(key));
        }

        if (doc.Pending is not null)
        {
            return Say(c, Ending(key));
        }

        if (ChatPrivileges.IsBot(c.Event.PayloadJson) || IsExcluded(c, c.Actor))
        {
            return Say(c, $"{User(c)}, you cannot enter this raffle.");
        }

        for (var attempt = 0; attempt < 3; attempt++)
        {
            doc = LoadRaffle(c, key)!;
            if (doc.Entries.Contains(c.Actor))
            {
                return Say(c, $"{User(c)}, you are already entered in raffle '{key}' (already_entered).");
            }

            if (doc.Entries.Count >= MaxRaffleEntries)
            {
                return Say(c, $"Raffle '{key}' is full.");
            }

            SaveRaffle(c, doc with { Entries = [.. doc.Entries, c.Actor] });
            var check = LoadRaffle(c, key);
            if (check is not null && check.Entries.Contains(c.Actor))
            {
                return Say(c, $"{User(c)}, you entered raffle '{key}' ({check.Entries.Count} entries).");
            }
        }

        throw new InvalidOperationException("could not record the raffle entry after 3 attempts");
    }

    private PlatformEventInfo? RaffleCount(Ctx c, string key)
    {
        var doc = LoadRaffle(c, key);
        return Say(c, doc is null
            ? NotRunning(key)
            : $"Raffle '{key}': {doc.Entries.Count} entries, {doc.Winners} winner(s), {doc.Total} total award.");
    }

    private PlatformEventInfo? RaffleSetWinners(Ctx c, string key, List<string> rest)
    {
        if (rest.Count != 1 || !int.TryParse(rest[0], NumberStyles.None, CultureInfo.InvariantCulture, out var winners) || winners is < 1 or > MaxRaffleWinners)
        {
            return Say(c, $"Usage: !tgw raffle winners <key> <1-{MaxRaffleWinners}> (invalid_request)");
        }

        return RaffleAdjust(c, key, doc => doc with { Winners = winners }, $"Raffle '{key}' will have {winners} winner(s).");
    }

    private PlatformEventInfo? RaffleSetAward(Ctx c, string key, List<string> rest)
    {
        if (rest.Count != 1 || !long.TryParse(rest[0], NumberStyles.None, CultureInfo.InvariantCulture, out var total) || total > MaxRaffleAward)
        {
            return Say(c, $"Usage: !tgw raffle award <key> <0-{MaxRaffleAward}> (invalid_request)");
        }

        return RaffleAdjust(c, key, doc => doc with { Total = total }, $"Raffle '{key}' total award is now {total}.");
    }

    private PlatformEventInfo? RaffleAdjust(Ctx c, string key, Func<RaffleDoc, RaffleDoc> change, string confirmation)
    {
        var doc = LoadRaffle(c, key);
        if (doc is null)
        {
            return Say(c, NotRunning(key));
        }

        if (doc.Pending is not null)
        {
            return Say(c, Ending(key));
        }

        SaveRaffle(c, change(doc));
        return Say(c, confirmation);
    }

    private PlatformEventInfo? RaffleEnd(Ctx c, string key)
    {
        var recordKey = ReplayGuard.KeyFor("tgw.rend", c.Event, c.Host, c.Actor, "raffle-end");
        var recorded = c.Store.GetJson(recordKey, GiveawayJsonContext.Default.RaffleEndRecord);
        if (recorded is not null && recorded.Key == key)
        {
            PushRaffleOverlay(c, recorded);
            return Say(c, recorded.Text);
        }

        var doc = LoadRaffle(c, key);
        if (doc is null)
        {
            return Say(c, NotRunning(key));
        }

        if (doc.Entries.Count == 0)
        {
            c.Store.Delete(RaffleDocKey(key));
            return Say(c, $"Raffle '{key}' ended with no entries (no_entries).");
        }

        var lease = StateKey.Of("tgw", "rlease", key, Num(doc.Round));
        if (!c.Store.TryClaim(lease, RaffleLeaseSeconds))
        {
            return Say(c, Ending(key));
        }

        try
        {
            var record = FinishRaffle(c, doc);
            c.Store.SetJson(recordKey, record, GiveawayJsonContext.Default.RaffleEndRecord, OutcomeTtlSeconds);
            c.Store.Delete(RaffleDocKey(key));
            c.Store.Delete(lease);
            PushRaffleOverlay(c, record);
            return Say(c, record.Text);
        }
        catch
        {
            c.Store.Delete(lease);
            throw;
        }
    }

    /// <summary>Fixes the winners (once), pays each exactly once and builds the final announcement.</summary>
    private RaffleEndRecord FinishRaffle(Ctx c, RaffleDoc doc)
    {
        var seed = Sha256.FromHex(doc.Seed) ?? throw new InvalidOperationException("stored raffle seed is not valid hex");
        if (doc.Pending is null)
        {
            var sorted = doc.Entries.OrderBy(e => e, StringComparer.Ordinal).ToList();
            var picks = Fairness.PickWithoutReplacement(seed, RaffleDrawFingerprint(doc.Key, doc.Round, sorted), sorted, Math.Min(doc.Winners, sorted.Count));
            var each = doc.Total > 0 ? (doc.Total + picks.Count - 1) / picks.Count : 0;
            doc = doc with { Pending = picks, Each = each, Awarded = [], Failed = [] };
            SaveRaffle(c, doc);
        }

        var client = new EconomyServiceClient(c.Host.Tool, $"raffle.{doc.Key}.{Num(doc.Round)}", "w");
        var paid = doc.Awarded ?? [];
        var failed = doc.Failed ?? [];
        foreach (var winner in doc.Pending!)
        {
            if (paid.Contains(winner) || failed.Contains(winner))
            {
                continue;
            }

            if (doc.Each > 0)
            {
                try
                {
                    client.Credit(winner, (ulong)doc.Each);
                }
                catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.OverCap or EconomyErrorKind.NotAMember)
                {
                    c.Host.Log.Warn("raffle.payout_skipped", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "not_a_member").Build());
                    failed = [.. failed, winner];
                    SaveRaffle(c, doc with { Awarded = paid, Failed = failed });
                    continue;
                }
            }

            paid = [.. paid, winner];
            SaveRaffle(c, doc with { Awarded = paid, Failed = failed });
        }

        var alias = Replies.Alias(c.Host, c.Actor, "raffle");
        var names = string.Join(", ", doc.Pending.Select(UserPlaceholder.For));
        var status = failed.Count == 0 ? "ended" : $"ended, payout_failed for {failed.Count}";
        var text = $"Raffle '{doc.Key}' {status}: {names} win {doc.Each} {alias} each. Verify: seed={doc.Seed} commit={doc.Commitment} entries={doc.Entries.Count} protocol={Fairness.Version}";
        c.Host.Log.Info("raffle.ended", new LogFields().With("winners", doc.Pending.Count).With("failed", failed.Count).With("entries", doc.Entries.Count).Build());
        return new RaffleEndRecord(doc.Key, text, doc.Pending, doc.Each);
    }

    private void PushRaffleOverlay(Ctx c, RaffleEndRecord record)
    {
        if (!LoadConfig(c).Overlay)
        {
            return;
        }

        GiveawayOverlay.Push(c.Host.Overlay, WidgetId, GiveawayOverlay.RafflePrefix + "winner", w =>
        {
            w.WriteString("key", record.Key);
            w.WriteNumber("each", record.Each);
            w.WriteStartArray("winners");
            foreach (var winner in record.Winners)
            {
                w.WriteStringValue(winner);
            }

            w.WriteEndArray();
        }, $"Raffle '{record.Key}' winners!", record.Winners.Count == 1 ? record.Winners[0] : null);
    }
}

using System.Globalization;
using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRaidtracker;

/// <summary>
/// Incoming-raid history, ported from PenguinTwitchBot's `Misc/RaidTracker.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used with
/// permission -- see `bundle.yaml`'s `notice`). A `channel.raid` event bumps the raiding user's total incoming raids and viewers and
/// their last-raid time; `!raids` lists the top raiders.
///
/// <para><b>What is NOT ported (documented, none silent).</b> (1) The outgoing `!raid TARGET` (Streamer-only) starts a raid through the
/// Twitch API with the broadcaster's token. Credentials stay host-side and no platform raid tool exists yet, so the broadcaster gets a loud
/// refusal (see <see cref="RaidRefusal"/>) and nobody else gets anything; outgoing counters therefore do not exist. (2) The 60-second
/// "is this raider's stream online" poll, the raider's last game and the raid-history prune UI are Twitch lookups plus a scheduler (no
/// WIT world has one). (3) The original stores Twitch user ids and display names; this port stores only the raider's community UUID.
/// A raid whose raider cannot be attributed to a UUID (not a community member) is counted in an aggregate "unattributed" tally.</para>
///
/// <para><b>Replay.</b> A raid event is claimed once per (message id, platform, raider), so a redelivery cannot double count (a crash
/// between the claim and the write loses that raid, never doubles it). Only the 50 most recent raiders are kept.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class RaidtrackerLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-raidtracker";
    internal const string IndexKey = "rt.idx";
    internal const string UnattributedRaidsKey = "rt.una.n";
    internal const string UnattributedViewersKey = "rt.una.v";
    internal const int MaxTracked = 50;
    internal const int TopCount = 5;
    internal const long MaxViewers = 10_000_000;
    internal const string RaidEventType = "channel.raid";

    internal const string RaidRefusal =
        "!raid is unavailable: starting a raid needs a platform raid tool that does not exist yet (Twitch credentials stay on the host).";

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (!host.Flags.Enabled(FlagKey, false) || host.Context.Community is null)
        {
            return null;
        }

        var store = new KvStateStore(host.Kv);
        if (@event.EventType == RaidEventType)
        {
            RecordRaid(@event, host, store);
            return null;
        }

        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null)
        {
            return null;
        }

        if (command.Is("raids"))
        {
            return Kit.Reply(@event, command, TopRaiders(store));
        }

        if (command.Is("raid") && Kit.IsBroadcaster(@event.PayloadJson))
        {
            host.Log.Warn("raid.outgoing_unavailable", new LogFields().With("reason", "no_platform_raid_tool").Build());
            return Kit.Reply(@event, command, RaidRefusal);
        }

        return null;
    }

    private static void RecordRaid(PlatformEventInfo @event, IWaddleHostNext host, IStateStore store)
    {
        var viewers = Math.Clamp(@event.Payload(RaidtrackerJsonContext.Default.RaidPayload)?.Metadata?.Viewers ?? 0, 0, MaxViewers);
        var raider = RaiderOf(@event, host);
        var messageId = host.Context.MessageId;
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new InvalidOperationException("the host supplied no message id; refusing to count a raid without a replay key");
        }

        var claim = StateKey.Of("rt.m", StateKey.Hash($"{messageId}|{@event.Platform}|{raider ?? "none"}"));
        if (!store.TryClaim(claim, 24 * 3600))
        {
            host.Log.Info("raid.replay", new LogFields().Build());
            return;
        }

        if (raider is null)
        {
            Kit.WriteLong(store, UnattributedRaidsKey, Kit.ReadLong(store, UnattributedRaidsKey) + 1);
            Kit.WriteLong(store, UnattributedViewersKey, Kit.ReadLong(store, UnattributedViewersKey) + viewers);
            host.Log.Warn("raid.unattributed", new LogFields().With("viewers", viewers).Build());
            return;
        }

        var previous = store.GetJson(RecordKey(raider), RaidtrackerJsonContext.Default.RaidRecord);
        var record = new RaidRecord(raider, (previous?.Raids ?? 0) + 1, (previous?.Viewers ?? 0) + viewers, (long)host.Clock.NowMillis());
        store.SetJson(RecordKey(raider), record, RaidtrackerJsonContext.Default.RaidRecord);

        var index = store.GetJson(IndexKey, RaidtrackerJsonContext.Default.RaidIndex) ?? new RaidIndex([]);
        index.Users.Remove(raider);
        index.Users.Insert(0, raider);
        while (index.Users.Count > MaxTracked)
        {
            store.Delete(RecordKey(index.Users[^1]));
            index.Users.RemoveAt(index.Users.Count - 1);
        }

        store.SetJson(IndexKey, index, RaidtrackerJsonContext.Default.RaidIndex);
        host.Log.Info("raid.recorded", new LogFields().With("viewers", viewers).With("raids", record.Raids).Build());
    }

    /// <summary>The raider's UUID: the pipeline's `{user:id}` actor token when present, else the host identity; null when not attributable.</summary>
    private static string? RaiderOf(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (TryUserToken(@event.Actor, out var token))
        {
            return token;
        }

        return Kit.TryActor(host, "raid.raider_unlinked");
    }

    /// <summary>Extracts the bare id from a `{user:id}` PII token (id chars `[A-Za-z0-9_.:-]`); false for anything else.</summary>
    internal static bool TryUserToken(string? actor, out string id)
    {
        id = string.Empty;
        var raw = actor?.Trim();
        if (raw is null || !raw.StartsWith("{user:", StringComparison.Ordinal) || !raw.EndsWith('}'))
        {
            return false;
        }

        var inner = raw["{user:".Length..^1];
        if (inner.Length == 0 || !inner.All(c => c is >= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or '_' or '.' or ':' or '-'))
        {
            return false;
        }

        id = inner;
        return true;
    }

    private static string TopRaiders(IStateStore store)
    {
        var index = store.GetJson(IndexKey, RaidtrackerJsonContext.Default.RaidIndex);
        var records = (index?.Users ?? [])
            .Select(u => store.GetJson(RecordKey(u), RaidtrackerJsonContext.Default.RaidRecord))
            .OfType<RaidRecord>()
            .OrderByDescending(r => r.Raids)
            .ThenByDescending(r => r.LastMillis)
            .Take(TopCount)
            .ToList();
        var unattributed = Kit.ReadLong(store, UnattributedRaidsKey);
        if (records.Count == 0)
        {
            return unattributed > 0 ? $"No attributed raids yet ({unattributed} from unlinked raiders)." : "No incoming raids recorded yet.";
        }

        var sb = new StringBuilder("Top raiders: ");
        for (var i = 0; i < records.Count; i++)
        {
            var r = records[i];
            sb.Append(CultureInfo.InvariantCulture, $"{(i > 0 ? ", " : string.Empty)}{i + 1}. {UserPlaceholder.For(r.User)} {r.Raids} raids ({r.Viewers} viewers)");
        }

        return sb.ToString();
    }

    internal static string RecordKey(string user) => StateKey.Of("rt.u", StateKey.Hash(user));
}

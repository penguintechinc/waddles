using System.Globalization;
using System.Text.Json;
using SuperPenguinShared;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinBonustickets;

/// <summary>
/// `!bonus` and `!addactive`, ported from PenguinTwitchBot's `TicketGames/BonusTickets.cs`, `BonusTicketsStreamStartedHandler.cs`
/// and `TicketGames/AddActive.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT -- see `bundle.yaml`'s `notice`). This is the
/// event-award policy: when and how much to award; the economy SERVICE owns the ledger, caps and idempotency.
///
/// <para><b>`!bonus`.</b> Each viewer may claim once per UTC day an award drawn uniformly from
/// [<see cref="MinAward"/>, <see cref="MaxAward"/>] (the original's 25..50 defaults). Roll and claim are recorded under the
/// event's replay key BEFORE the single `adjust_balance` credit, so a redelivered event re-sends the identical credit
/// (the host de-duplicates it) and reprints the same message. A second claim the same day is silently ignored, as upstream.
/// Deviations: upstream resets per STREAM and refuses while offline; no stream-online capability exists in any WIT world, so
/// the claim list resets per UTC day and the offline gate is dropped.</para>
///
/// <para><b>`!addactive &lt;amount&gt;`</b> (moderator/broadcaster only). The amount is clamped to <see cref="MaxAddActive"/>
/// and pooled; once <see cref="DelaySeconds"/> pass without another add the pool is paid, per member, to everyone seen chatting
/// in the last <see cref="ActiveWindowSeconds"/> seconds. "Active" is tracked by this bundle itself (it consumes every chat
/// line, bots excluded via the `is_bot` badge) in sixteen roster shards, because no host capability lists active users.
/// A payout of more than <see cref="ChunkSize"/> recipients is drained across consecutive events (one chunk each, recorded
/// before paying) instead of one call that could exceed the 2 s invocation budget.</para>
///
/// <para><b>Known limits (documented, none silent).</b> kv has no compare-and-swap: two simultaneous `!addactive` or roster writes
/// can lose an update (a lost add under-pays, never over-pays). The pool settles only when ANOTHER chat event reaches the bundle
/// (no timer exists for app bundles). Settlement announcements go to the channel of the event that triggered the payout.
/// Moderator badges come from the event payload; absent badges deny (fail-closed).</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class BonusticketsLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-bonustickets";
    internal const long MinAward = 25;
    internal const long MaxAward = 50;
    internal const long MaxAddActive = 100;
    internal const long DelaySeconds = 5;
    internal const long ActiveWindowSeconds = 600;
    internal const long RosterRefreshSeconds = 60;
    internal const int RosterShardCount = 16;
    internal const int RosterShardCap = 400;
    internal const int MaxRecipients = 100;
    internal const int ChunkSize = 20;
    internal const uint DayTtlSeconds = 2 * 24 * 60 * 60;
    internal const uint OutcomeTtlSeconds = 3600;

    internal const string ModOnly = "only moderators and the broadcaster can do that.";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (command is not null && command.Is("bonus"))
        {
            return Bonus(@event, command, host);
        }

        if (command is not null && command.Is("addactive"))
        {
            return AddActive(@event, command, host);
        }

        return Passive(@event, host);
    }

    private PlatformEventInfo? Bonus(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host)
    {
        if (host.Context.Community is null)
        {
            host.Log.Warn("bonus.refused", new LogFields().With("reason", "no_community").Build());
            return Replies.Reply(@event, command, "!bonus needs a community; this channel is not linked to one.");
        }

        var actor = TryActor(host, "bonus");
        if (actor is null)
        {
            return Replies.Reply(@event, command, "Link your account to this community before claiming a bonus.");
        }

        var store = new KvStateStore(host.Kv);
        var key = ReplayGuard.KeyFor("bonus.out", @event, host, actor, command.Name);
        var recorded = store.GetJson(key, BonusticketsJsonContext.Default.BonusOutcome);
        var replay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name;
        BonusOutcome outcome;
        if (replay)
        {
            outcome = recorded!;
        }
        else
        {
            var day = Day(host);
            if (!store.TryClaim(StateKey.Of("bonus.u", day, StateKey.Hash(actor)), DayTtlSeconds))
            {
                return null; // already claimed today: the original ignores a second claim silently.
            }

            outcome = new BonusOutcome(actor, command.Name, _nextInt((int)MinAward, (int)MaxAward));
            store.SetJson(key, outcome, BonusticketsJsonContext.Default.BonusOutcome, OutcomeTtlSeconds);
        }

        long balance;
        try
        {
            balance = EconomyServiceClient.For(host, "bonus").Credit(actor, (ulong)outcome.Award);
        }
        catch (WaddleEconomyException ex) when (ex.Kind == EconomyErrorKind.OverCap)
        {
            store.Delete(key);
            store.Delete(StateKey.Of("bonus.u", Day(host), StateKey.Hash(actor)));
            host.Log.Warn("bonus.refused", new LogFields().With("reason", "over_cap").Build());
            return Replies.Reply(@event, command, $"{UserPlaceholder.For(actor)}, the community's bonus limit was reached; try again later.");
        }

        var alias = Replies.Alias(host, actor, "bonus");
        host.Log.Info("bonus.awarded", new LogFields().With("award", outcome.Award).With("replay", replay).Build());
        return Replies.Reply(@event, command,
            $"{UserPlaceholder.For(actor)} just got {outcome.Award} bonus {alias} and now has {balance} {alias}.");
    }

    private PlatformEventInfo? AddActive(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host)
    {
        if (!ChatPrivileges.IsModerator(@event.PayloadJson))
        {
            host.Log.Info("addactive.refused", new LogFields().With("reason", "not_moderator").Build());
            return Replies.Reply(@event, command, ModOnly);
        }

        if (command.Args.Count != 1
            || !long.TryParse(command.Args[0], NumberStyles.None, CultureInfo.InvariantCulture, out var requested)
            || requested < 1)
        {
            return Replies.Reply(@event, command, "Usage: !addactive <amount>");
        }

        var actor = TryActor(host, "addactive");
        if (actor is null)
        {
            return Replies.Reply(@event, command, "Link your account to this community before using !addactive.");
        }

        var amount = Math.Min(requested, MaxAddActive);
        var store = new KvStateStore(host.Kv);
        var claim = ReplayGuard.KeyFor("bonus.add", @event, host, actor, command.Name);
        if (store.TryClaim(claim, OutcomeTtlSeconds))
        {
            var pool = store.GetJson(PoolKey, BonusticketsJsonContext.Default.BonusPool) ?? new BonusPool(0, 0, 0);
            store.SetJson(PoolKey, pool with { Amount = pool.Amount + amount, LastAddMillis = (long)host.Clock.NowMillis() },
                BonusticketsJsonContext.Default.BonusPool, DayTtlSeconds);
        }

        var alias = Replies.Alias(host, actor, "addactive");
        host.Log.Info("addactive.queued", new LogFields().With("amount", amount).Build());
        return Replies.Reply(@event, command, $"Queued {amount} {alias} for everyone who has been active recently.");
    }

    /// <summary>Non-command chat: record the chatter as active and, when due, pay one chunk of a settlement.</summary>
    private PlatformEventInfo? Passive(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (host.Context.Community is null)
        {
            return null;
        }

        var actor = TryActor(host, "active");
        if (actor is null)
        {
            return null;
        }

        var store = new KvStateStore(host.Kv);
        var now = (long)host.Clock.NowMillis();
        if (!ChatPrivileges.IsBot(@event.PayloadJson))
        {
            Touch(store, actor, now);
        }

        var channel = ChannelOf(@event);
        if (channel is null)
        {
            return null; // cannot announce without a channel; leave the pool for a later event.
        }

        var announcement = Settle(store, host, @event, actor, now);
        return announcement is null
            ? null
            : PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(announcement, channel), WaddleSdkJsonContext.Default.ChatReplyPayload);
    }

    private static string PoolKey => StateKey.Of("bonus.pool");

    private static string SettlementKey => StateKey.Of("bonus.settlement");

    /// <summary>Records <paramref name="actor"/> as seen now in their roster shard (written at most once a minute per chatter).</summary>
    private static void Touch(KvStateStore store, string actor, long now)
    {
        var key = ShardKey(actor);
        var shard = store.GetJson(key, BonusticketsJsonContext.Default.RosterShard) ?? new RosterShard([]);
        var existing = shard.Entries.FirstOrDefault(e => e.Actor == actor);
        if (existing is not null && now - existing.SeenMillis < RosterRefreshSeconds * 1000)
        {
            return;
        }

        var cutoff = now - ActiveWindowSeconds * 1000;
        var kept = shard.Entries.Where(e => e.Actor != actor && e.SeenMillis >= cutoff).OrderByDescending(e => e.SeenMillis).Take(RosterShardCap - 1).ToList();
        kept.Add(new RosterEntry(actor, now));
        store.SetJson(key, new RosterShard(kept), BonusticketsJsonContext.Default.RosterShard, (uint)ActiveWindowSeconds);
    }

    private static string ShardKey(string actor) =>
        StateKey.Of("bonus.roster", StateKey.Hash(actor)[..1]);

    /// <summary>The distinct chatters seen within the active window, most recent first, capped at <see cref="MaxRecipients"/>.</summary>
    private static List<string> ActiveRecipients(KvStateStore store, long now)
    {
        var cutoff = now - ActiveWindowSeconds * 1000;
        List<RosterEntry> all = [];
        for (var i = 0; i < RosterShardCount; i++)
        {
            var shard = store.GetJson(StateKey.Of("bonus.roster", i.ToString("x", CultureInfo.InvariantCulture)), BonusticketsJsonContext.Default.RosterShard);
            if (shard is not null)
            {
                all.AddRange(shard.Entries.Where(e => e.SeenMillis >= cutoff));
            }
        }

        return all.OrderByDescending(e => e.SeenMillis).ThenBy(e => e.Actor, StringComparer.Ordinal).Select(e => e.Actor).Distinct().Take(MaxRecipients).ToList();
    }

    /// <summary>Starts a due settlement (once) and pays the next chunk; returns the announcement text, or null when nothing was paid.</summary>
    private static string? Settle(KvStateStore store, IWaddleHostNext host, PlatformEventInfo @event, string actor, long now)
    {
        var messageId = host.Context.MessageId;
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new InvalidOperationException("the host supplied no message id; refusing to pay without a replay key");
        }

        var chunkKey = StateKey.Of("bonus.chunk", StateKey.Hash($"{messageId}|{@event.Platform}"));
        var chunk = store.GetJson(chunkKey, BonusticketsJsonContext.Default.BonusChunk);
        if (chunk is null)
        {
            var settlement = store.GetJson(SettlementKey, BonusticketsJsonContext.Default.BonusSettlement);
            var started = false;
            if (settlement is null)
            {
                settlement = TryStart(store, host, now);
                if (settlement is null)
                {
                    return null;
                }

                started = true;
                if (settlement.Recipients.Count == 0)
                {
                    store.Delete(SettlementKey);
                    host.Log.Info("addactive.settled", new LogFields().With("recipients", 0).Build());
                    return $"No active users to send {settlement.Each} {Replies.Alias(host, actor, "addactive")} to.";
                }
            }

            var to = Math.Min(settlement.Recipients.Count, settlement.Next + ChunkSize);
            chunk = new BonusChunk(settlement.Id, settlement.Each, settlement.Recipients.GetRange(settlement.Next, to - settlement.Next), started, settlement.Recipients.Count);
            store.SetJson(chunkKey, chunk, BonusticketsJsonContext.Default.BonusChunk, OutcomeTtlSeconds);
            if (to >= settlement.Recipients.Count)
            {
                store.Delete(SettlementKey);
            }
            else
            {
                store.SetJson(SettlementKey, settlement with { Next = to }, BonusticketsJsonContext.Default.BonusSettlement, DayTtlSeconds);
            }
        }

        var paid = Pay(host, chunk.SettlementId, chunk.Each, chunk.Recipients);
        host.Log.Info("addactive.chunk", new LogFields().With("paid", paid).With("size", chunk.Recipients.Count).Build());
        return chunk.Started
            ? $"Sending {chunk.Each} {Replies.Alias(host, actor, "addactive")} to {chunk.Total} active users."
            : null;
    }

    /// <summary>Freezes the due pool into a settlement (exactly one claimant per sequence number), or returns null when nothing is due.</summary>
    private static BonusSettlement? TryStart(KvStateStore store, IWaddleHostNext host, long now)
    {
        var pool = store.GetJson(PoolKey, BonusticketsJsonContext.Default.BonusPool);
        if (pool is null || pool.Amount < 1 || now - pool.LastAddMillis < DelaySeconds * 1000)
        {
            return null;
        }

        if (!store.TryClaim(StateKey.Of("bonus.start", pool.Seq.ToString(CultureInfo.InvariantCulture)), DayTtlSeconds))
        {
            return null;
        }

        var settlement = new BonusSettlement(pool.Seq, pool.Amount, ActiveRecipients(store, now), 0);
        store.SetJson(SettlementKey, settlement, BonusticketsJsonContext.Default.BonusSettlement, DayTtlSeconds);
        store.SetJson(PoolKey, new BonusPool(0, pool.LastAddMillis, pool.Seq + 1), BonusticketsJsonContext.Default.BonusPool, DayTtlSeconds);
        return settlement;
    }

    /// <summary>Credits each recipient once (stable per-settlement keys); a recipient who left the community or hit a cap is skipped and logged.</summary>
    private static int Pay(IWaddleHostNext host, long settlementId, long each, List<string> recipients)
    {
        var paid = 0;
        var client = new EconomyServiceClient(host.Tool, host.Context.MessageId, "active" + settlementId.ToString(CultureInfo.InvariantCulture));
        foreach (var user in recipients)
        {
            try
            {
                client.Credit(user, (ulong)each);
                paid++;
            }
            catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.OverCap or EconomyErrorKind.NotAMember)
            {
                host.Log.Warn("addactive.skipped", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "not_a_member").Build());
            }
        }

        return paid;
    }

    private static string? TryActor(IWaddleHostNext host, string op)
    {
        try
        {
            return host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info(op + ".refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return null;
        }
    }

    private static string Day(IWaddleHostNext host) =>
        DateTimeOffset.FromUnixTimeMilliseconds((long)host.Clock.NowMillis()).UtcDateTime.ToString("yyyyMMdd", CultureInfo.InvariantCulture);

    private static string? ChannelOf(PlatformEventInfo @event)
    {
        try
        {
            return JsonSerializer.Deserialize(@event.PayloadJson, WaddleSdkJsonContext.Default.ChatMessagePayload)?.ChannelId;
        }
        catch (JsonException)
        {
            return null;
        }
    }
}

using System.Globalization;
using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Scheduling;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinDuel;

/// <summary>
/// `!duel target amount` / `!accept` / `!deny`, ported from PenguinTwitchBot's `TicketGames/DuelGame.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the original:
/// stake 1..100, a 50% win chance for the challenger, a 2 minute challenge window, a 600 s challenger cooldown, one pending duel
/// per player (as challenger or defender), and the original's chat wording.
///
/// <para><b>Money model (the point of this port).</b> The economy debits only the INVOKING actor, so a stake can only leave a
/// player's balance on their own command: the challenger's stake is debited on `!duel`, the opponent's on `!accept` (each with
/// host `economy.debit`, a burn). While a duel is open the burned stakes ARE the escrow, tracked by the open <see cref="DuelRecord"/>.
/// The winner is paid twice the stake with `economy.credit` (a mint of exactly what was burned: supply-neutral), and an expired or
/// denied duel refunds the challenger's stake the same way. The community POT (`pay-pot`) is not used: no bundle path can fund it
/// today. Two things follow: a winner/refund credit is subject to the host's mint caps (an `over-cap` propagates loudly and the
/// redelivered event retries the identical credit); and a stake is never at risk from the opponent's cooperation, because it is
/// only ever debited by its owner.</para>
///
/// <para><b>Exactly-once money.</b> Every event writes a PLAN (ordered money ops + state effects, with the win roll already made)
/// under a replay key (message id + platform + actor UUID + command, <see cref="ReplayGuard"/>) BEFORE its first economy call, and
/// a redelivered event re-executes the same plan; the host de-duplicates each call per (event, kind, ordinal), so money moves once.
/// A plan counts as a replay only on an exact actor + command + raw-arguments match; a blank message id throws. Settling a duel
/// (accept, deny, expiry refund) first takes an atomic per-duel claim whose owner token is the event's plan key, so two racing
/// events can never both settle one duel, and only the owning event's redelivery may resume it. A debit refused for insufficient
/// funds as the FIRST op of an event is cleanly aborted (claims released, nothing moved, reply replaced).</para>
///
/// <para><b>Expiry (documented limitation).</b> No timer exists for app bundles, so the SDK <see cref="TickScheduler"/>
/// emulation is used: expired duels are refunded when the NEXT `!duel`/`!accept`/`!deny` reaches this bundle (the sweep runs at
/// the top of every command), not at the deadline. An `!accept` after the deadline is always refused. kv has no compare-and-swap:
/// a crash between a settle claim and its plan record strands that duel's stakes until the claim expires (24 h), and the stake is
/// then recoverable only by an operator -- chosen over any path that could pay twice.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class DuelLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const ulong MinStake = 1;
    internal const ulong MaxStake = 100;
    internal const int WinPercent = 50;
    internal const ulong TimeoutMillis = 2 * 60 * 1000;
    internal const uint CooldownSeconds = 600;
    internal const uint LockTtlSeconds = 180;
    internal const uint DayTtlSeconds = 24 * 60 * 60;
    internal const uint PlanTtlSeconds = 3600;
    internal const int MaxSweepPerEvent = 4;
    internal const string FlagKey = "waddles.command-superpenguin-duel";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("duel", "accept", "deny"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("duel.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "Duels need a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("duel.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before dueling.");
        }

        var store = new KvStateStore(host.Kv);
        var sched = new TickScheduler(store, host.Clock, "duel");
        var planKey = ReplayGuard.KeyFor("duel.ev", @event, host, actor, command.Name);
        var argsHash = StateKey.Hash(command.RawText);

        var recorded = store.GetJson(planKey, DuelJsonContext.Default.EventPlan);
        if (recorded is not null && recorded.Actor == actor && recorded.Command == command.Name && recorded.ArgsHash == argsHash)
        {
            var replayReply = Execute(recorded, planKey, host, store, sched);
            host.Log.Info("duel.replayed", new LogFields().With("command", command.Name).Build());
            return replayReply.Length == 0 ? null : Reply(@event, command, replayReply);
        }

        var sweep = Sweep(host, store, sched, planKey);
        var plan = command.Name switch
        {
            "duel" => Challenge(command, host, store, sched, actor, argsHash, sweep, planKey),
            "accept" => Accept(command, host, store, actor, argsHash, sweep, planKey),
            _ => Deny(command, host, store, actor, argsHash, sweep, planKey),
        };

        if (plan.Sweep.Ops.Count == 0 && plan.Sweep.Effects.Count == 0 && plan.Own is null)
        {
            return plan.Reply.Length == 0 ? null : Reply(@event, command, plan.Reply);
        }

        store.SetJson(planKey, plan, DuelJsonContext.Default.EventPlan, PlanTtlSeconds);
        var reply = Execute(plan, planKey, host, store, sched);
        return reply.Length == 0 ? null : Reply(@event, command, reply);
    }

    private sealed record SweepResult(PlanSection Section, List<string> Notices, HashSet<string> Ids);

    /// <summary>Claims every duel past its deadline (at most <see cref="MaxSweepPerEvent"/>) and plans a refund of the challenger's stake for each.</summary>
    private static SweepResult Sweep(IWaddleHostNext host, KvStateStore store, TickScheduler sched, string token)
    {
        var section = new PlanSection([], []);
        List<string> notices = [];
        HashSet<string> ids = [];
        var now = host.Clock.NowMillis();
        foreach (var task in sched.Pending().Where(t => t.DueAtMillis <= now).Take(MaxSweepPerEvent))
        {
            var rec = store.GetJson(RecKey(task.Id), DuelJsonContext.Default.DuelRecord);
            if (rec is null)
            {
                sched.Cancel(task.Id);
                continue;
            }

            if (!TryOwn(store, task.Id, token))
            {
                continue;
            }

            section.Ops.Add(new MoneyOp("credit", rec.Challenger, rec.Stake));
            section.Effects.Add(new DuelEffect("close", rec.Id, rec));
            notices.Add($"{UserPlaceholder.For(rec.Challenger)}'s duel against {UserPlaceholder.For(rec.Defender)} expired and the stake was refunded.");
            ids.Add(rec.Id);
        }

        return new SweepResult(section, notices, ids);
    }

    private EventPlan Challenge(ChatCommand command, IWaddleHostNext host, KvStateStore store, TickScheduler sched, string actor, string argsHash, SweepResult sweep, string planKey)
    {
        EventPlan Refuse(string text) => Plan(actor, command, argsHash, sweep, null, [], text, text);

        if (command.Args.Count != 2)
        {
            return Refuse("To use duel, !duel target amount");
        }

        if (!ulong.TryParse(command.Args[1], NumberStyles.None, CultureInfo.InvariantCulture, out var stake))
        {
            return Refuse("Please enter a proper amount. !duel target amount");
        }

        if (stake < MinStake || stake > MaxStake)
        {
            return Refuse($"To duel you must choose an amount between {MinStake} and {MaxStake}");
        }

        string defender;
        try
        {
            defender = host.Identity.ResolveMention(command.Args[0]);
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotFound or IdentityErrorKind.Ambiguous or IdentityErrorKind.Invalid or IdentityErrorKind.NotAMember or IdentityErrorKind.NotLinked)
        {
            host.Log.Info("duel.refused", new LogFields().With("reason", "target_" + ex.Kind.ToString().ToLowerInvariant()).Build());
            return Refuse("Could not find that viewer");
        }

        var alias = Alias(host, actor);
        if (defender == actor)
        {
            return Refuse("You can't duel yourself.");
        }

        if (EconomyServiceClient.For(host).Balance(actor) < (long)stake)
        {
            return Refuse("You don't have that much.");
        }

        if (EconomyServiceClient.For(host).Balance(defender) < (long)stake)
        {
            return Refuse($"They don't have that many {alias}.");
        }

        if (sched.Pending().Count + sweep.Ids.Count >= TickScheduler.MaxTasks - MaxSweepPerEvent)
        {
            return Refuse("Too many duels are in progress, try again shortly.");
        }

        var cooldownKey = StateKey.Of("duel.cd", StateKey.Hash(actor));
        if (!store.TryClaim(cooldownKey, CooldownSeconds))
        {
            return Plan(actor, command, argsHash, sweep, null, [], string.Empty, string.Empty); // original: silent drop inside the cooldown
        }

        var actorLock = LockKey(actor);
        var defenderLock = LockKey(defender);
        if (!store.TryClaim(actorLock, LockTtlSeconds))
        {
            store.Delete(cooldownKey);
            return Refuse("You are already in a duel; wait for it to be accepted, denied or to time out.");
        }

        if (!store.TryClaim(defenderLock, LockTtlSeconds))
        {
            store.Delete(actorLock);
            store.Delete(cooldownKey);
            return Refuse($"{UserPlaceholder.For(defender)} has a pending duel already. Please wait for that duel to end or time out.");
        }

        var id = StateKey.Hash(planKey);
        var duel = new DuelRecord(id, actor, defender, stake, WinPercent, host.Clock.NowMillis() + TimeoutMillis);
        var own = new PlanSection([new MoneyOp("debit", actor, stake)], [new DuelEffect("open", id, duel)]);
        var minutes = TimeoutMillis / 60000;
        return Plan(
            actor, command, argsHash, sweep, own, [cooldownKey, actorLock, defenderLock],
            $"{UserPlaceholder.For(defender)}, {UserPlaceholder.For(actor)} has challenged you to a duel for {stake} {alias}. You have {minutes} minutes to !accept or !deny the duel.",
            $"{UserPlaceholder.For(actor)}, you don't have {stake} {alias} to duel with.");
    }

    private EventPlan Accept(ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string argsHash, SweepResult sweep, string planKey)
    {
        EventPlan Refuse(string text) => Plan(actor, command, argsHash, sweep, null, [], text, text);

        var duel = PendingFor(store, actor);
        if (duel is null)
        {
            return Refuse("You don't have any pending duels targeting you.");
        }

        if (sweep.Ids.Contains(duel.Id) || host.Clock.NowMillis() >= duel.DueAt)
        {
            return Refuse("That duel challenge has expired.");
        }

        var alias = Alias(host, actor);
        if (EconomyServiceClient.For(host).Balance(actor) < (long)duel.Stake)
        {
            return Refuse($"{UserPlaceholder.For(actor)}, you don't have {duel.Stake} {alias} to accept this duel.");
        }

        if (!TryOwn(store, duel.Id, planKey))
        {
            return Refuse("That duel was already resolved.");
        }

        var winner = _nextInt(0, 99) < duel.WinPercent ? duel.Challenger : duel.Defender;
        var loser = winner == duel.Challenger ? duel.Defender : duel.Challenger;
        var own = new PlanSection(
            [new MoneyOp("debit", actor, duel.Stake), new MoneyOp("credit", winner, duel.Stake * 2)],
            [new DuelEffect("close", duel.Id, duel)]);
        var text = $"{UserPlaceholder.For(winner)} won the Duel vs {UserPlaceholder.For(loser)} PogChamp {UserPlaceholder.For(winner)} won {duel.Stake} {alias} FeelsGoodMan";
        return Plan(
            actor, command, argsHash, sweep, own, [ClaimKey(duel.Id), OwnerKey(duel.Id)],
            text, $"{UserPlaceholder.For(actor)}, you don't have {duel.Stake} {alias} to accept this duel.");
    }

    private EventPlan Deny(ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string argsHash, SweepResult sweep, string planKey)
    {
        EventPlan Refuse(string text) => Plan(actor, command, argsHash, sweep, null, [], text, text);

        var duel = PendingFor(store, actor);
        if (duel is null)
        {
            return Refuse("You don't have any pending duels targeting you.");
        }

        if (sweep.Ids.Contains(duel.Id) || host.Clock.NowMillis() >= duel.DueAt)
        {
            return Refuse("That duel challenge has expired.");
        }

        if (!TryOwn(store, duel.Id, planKey))
        {
            return Refuse("That duel was already resolved.");
        }

        var own = new PlanSection([new MoneyOp("credit", duel.Challenger, duel.Stake)], [new DuelEffect("close", duel.Id, duel)]);
        var text = $"{UserPlaceholder.For(duel.Defender)} has denied your duel, {UserPlaceholder.For(duel.Challenger)}; your stake was refunded.";
        return Plan(actor, command, argsHash, sweep, own, [], text, text);
    }

    /// <summary>Joins the command's own section with the sweep's into one plan; sweep notices are appended to every reply.</summary>
    private static EventPlan Plan(string actor, ChatCommand command, string argsHash, SweepResult sweep, PlanSection? own, List<string> release, string reply, string abortReply)
    {
        string Join(string text) => string.Join(" ", new[] { text }.Concat(sweep.Notices).Where(s => s.Length > 0));
        return new EventPlan(actor, command.Name, argsHash, sweep.Section, own, release, Join(reply), Join(abortReply));
    }

    /// <summary>
    /// Runs a plan: sweep ops and effects first (always committed), then the command's own ops. Returns the final reply.
    /// Only a refused FIRST debit may abort the own section; any later failure propagates so the redelivered event retries it.
    /// </summary>
    private static string Execute(EventPlan plan, string planKey, IWaddleHostNext host, KvStateStore store, TickScheduler sched)
    {
        for (var i = 0; i < plan.Sweep.Ops.Count; i++)
        {
            Run(plan.Sweep.Ops[i], host, planKey + ".sweep." + i);
        }

        foreach (var effect in plan.Sweep.Effects)
        {
            Apply(effect, store, sched);
        }

        if (plan.Own is null)
        {
            return plan.Reply;
        }

        for (var i = 0; i < plan.Own.Ops.Count; i++)
        {
            try
            {
                Run(plan.Own.Ops[i], host, planKey + ".own." + i);
            }
            catch (WaddleEconomyException ex) when (i == 0 && plan.Own.Ops[i].Kind == "debit" && ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
            {
                // Persist the terminal decision before releasing ownership. If cleanup fails,
                // retry must never resurrect the old money plan. Stranded claims expire at their
                // existing TTL; availability is preferable to replaying an aborted debit.
                var aborted = plan with { Own = null, ReleaseOnAbort = [], Reply = plan.AbortReply };
                store.SetJson(planKey, aborted, DuelJsonContext.Default.EventPlan, PlanTtlSeconds);
                foreach (var key in plan.ReleaseOnAbort)
                {
                    store.Delete(key);
                }
                host.Log.Info("duel.aborted", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
                return aborted.Reply;
            }
        }

        foreach (var effect in plan.Own.Effects)
        {
            Apply(effect, store, sched);
        }

        host.Log.Info("duel.settled", new LogFields().With("ops", plan.Own.Ops.Count).Build());
        return plan.Reply;
    }

    private static void Run(MoneyOp op, IWaddleHostNext host, string operationId)
    {
        switch (op.Kind)
        {
            case "debit":
                EconomyServiceClient.For(host, operationId).Debit(op.User, op.Amount);
                break;
            case "credit":
                EconomyServiceClient.For(host, operationId).Credit(op.User, op.Amount);
                break;
            default:
                throw new InvalidOperationException($"unknown money op '{op.Kind}' in a duel plan");
        }
    }

    /// <summary>Applies one state effect; every effect is idempotent so a replay may re-apply it.</summary>
    private static void Apply(DuelEffect effect, KvStateStore store, TickScheduler sched)
    {
        var duel = effect.Duel ?? throw new InvalidOperationException("duel effect without a duel");
        var idBytes = Encoding.UTF8.GetBytes(duel.Id);
        if (effect.Type == "open")
        {
            if (store.Get(ClaimKey(duel.Id)) is not null)
            {
                return; // already settled: a replay of the challenge must never re-open a closed duel.
            }

            store.SetJson(RecKey(duel.Id), duel, DuelJsonContext.Default.DuelRecord, DayTtlSeconds);
            store.Set(DefenderKey(duel.Defender), idBytes, DayTtlSeconds);
            store.Set(LockOwnerKey(duel.Challenger), idBytes, DayTtlSeconds);
            store.Set(LockOwnerKey(duel.Defender), idBytes, DayTtlSeconds);
            sched.Schedule(new ScheduledTask(duel.Id, duel.DueAt, "expire", string.Empty));
            return;
        }

        if (effect.Type != "close")
        {
            throw new InvalidOperationException($"unknown duel effect '{effect.Type}'");
        }

        store.Delete(RecKey(duel.Id));
        if (IdAt(store, DefenderKey(duel.Defender)) == duel.Id)
        {
            store.Delete(DefenderKey(duel.Defender));
        }

        foreach (var user in new[] { duel.Challenger, duel.Defender })
        {
            if (IdAt(store, LockOwnerKey(user)) == duel.Id)
            {
                store.Delete(LockOwnerKey(user));
                store.Delete(LockKey(user));
            }
        }

        sched.Cancel(duel.Id);
    }

    /// <summary>The open duel naming <paramref name="defender"/> as its target, or null.</summary>
    private static DuelRecord? PendingFor(KvStateStore store, string defender)
    {
        var id = IdAt(store, DefenderKey(defender));
        return id is null ? null : store.GetJson(RecKey(id), DuelJsonContext.Default.DuelRecord);
    }

    /// <summary>
    /// First-writer-wins ownership of settling a duel. The winner of the atomic claim records <paramref name="token"/> as owner; a
    /// later call is the owner only if that token matches (so the owning event's own redelivery resumes, nobody else does).
    /// </summary>
    private static bool TryOwn(KvStateStore store, string id, string token)
    {
        if (store.TryClaim(ClaimKey(id), DayTtlSeconds))
        {
            store.Set(OwnerKey(id), Encoding.UTF8.GetBytes(token), DayTtlSeconds);
            return true;
        }

        return IdAt(store, OwnerKey(id)) == token;
    }

    private static string? IdAt(KvStateStore store, string key)
    {
        var bytes = store.Get(key);
        return bytes is null ? null : Encoding.UTF8.GetString(bytes);
    }

    private static string RecKey(string id) => StateKey.Of("duel.rec", id);

    private static string ClaimKey(string id) => StateKey.Of("duel.settle", id);

    private static string OwnerKey(string id) => StateKey.Of("duel.sown", id);

    private static string DefenderKey(string user) => StateKey.Of("duel.def", StateKey.Hash(user));

    private static string LockKey(string user) => StateKey.Of("duel.lock", StateKey.Hash(user));

    private static string LockOwnerKey(string user) => StateKey.Of("duel.lown", StateKey.Hash(user));

    private static string Alias(IWaddleHostNext host, string actor)
    {
        try
        {
            var alias = EconomyServiceTools.PointsView(host.Tool, actor).Alias;
            if (!string.IsNullOrWhiteSpace(alias))
            {
                return alias;
            }
        }
        catch (WaddleToolException ex)
        {
            host.Log.Warn("duel.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
}

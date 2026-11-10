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

namespace WaddleBundleSuperpenguinFfa;

/// <summary>
/// `!ffa`, ported from PenguinTwitchBot's `PastyGames/FFA.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission --
/// see `bundle.yaml`'s `notice`). Faithful to the original: the first `!ffa` opens a join window (default 180 s) and pays the fee
/// (default 100), later `!ffa` calls join, a double join is refused, one entrant gets the fees back ("not enough players"),
/// otherwise ONE uniformly random entrant wins `entrants * fee`, then a 300 s cooldown; the original's chat wording is kept
/// (including its "you are to late" typo). The app owns every rule; the economy SERVICE is only I/O.
///
/// <para><b>Money model.</b> The economy debits only the INVOKING actor, so a fee leaves a balance only on that player's own
/// `!ffa` (service `adjust_balance` debit, a burn). While the game is open the burned fees ARE the pot, tracked by the open
/// <see cref="GameRecord"/> and the entrant slots. The winner is paid the pot (a mint of exactly what was burned: supply-neutral);
/// a lone entrant is refunded the same way. A credit refused over the mint cap propagates loudly and the redelivered event retries.</para>
///
/// <para><b>Exactly-once money.</b> Every event writes a PLAN (ordered money ops + state effects, the winner already rolled)
/// under a replay key BEFORE its first economy call; a redelivered event re-executes the same plan and the host de-duplicates
/// each call per (event, kind, ordinal). A plan is a replay only on an exact actor + command + raw-argument match; a blank message
/// id throws. Entries claim their own slot key atomically (no read-modify-write on a shared list, kv has no CAS), settlement
/// takes an atomic claim whose owner is the event's plan key, and a join whose game was settled before it registered is refunded.</para>
///
/// <para><b>Settlement (documented limitation).</b> No timer exists for app bundles, so the SDK <see cref="TickScheduler"/>
/// emulation is used: the window is settled when the NEXT `!ffa` reaches this bundle after the deadline, not at the deadline; that
/// command is answered "too late" (it never joins). A settlement is deferred up to <see cref="InflightGraceMillis"/> while a
/// slot is claimed but not yet written (a join in flight). Fees of a game nobody settles within 24 h need an operator.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class FfaLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const ulong EntryCost = 100;
    internal const ulong JoinMillis = 180_000;
    internal const uint CooldownSeconds = 300;
    internal const int MaxEntrants = 30;
    internal const ulong InflightGraceMillis = 300_000;
    internal const uint DayTtlSeconds = 24 * 60 * 60;
    internal const uint PlanTtlSeconds = 3600;
    internal const string FlagKey = "waddles.command-superpenguin-ffa";

    internal const string MsgStarting = "{Name} is starting a FFA battle! Type !{CommandName} to join now!";
    internal const string MsgJoined = "{Name} joined the FFA!";
    internal const string MsgLate = "The FFA has already started, you are to late to join this one.";
    internal const string MsgAlreadyJoined = "You have already joined the FFA!";
    internal const string MsgNotEnoughPoints = "Sorry it costs {Cost} {PointType} to join the FFA game which you do not have.";
    internal const string MsgNotEnoughPlayers = "Not enough viewers joined the FFA, returning the fees.";
    internal const string MsgWinner = "The dust finally settled and the last one standing is {Name} and gets {Points} {PointType}!";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("ffa"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("ffa.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "FFA needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("ffa.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before joining the FFA.");
        }

        var store = new KvStateStore(host.Kv);
        var sched = new TickScheduler(store, host.Clock, "ffa");
        var planKey = ReplayGuard.KeyFor("ffa.ev", @event, host, actor, command.Name);
        var argsHash = StateKey.Hash(command.RawText);

        var recorded = store.GetJson(planKey, FfaJsonContext.Default.EventPlan);
        if (recorded is not null && recorded.Actor == actor && recorded.Command == command.Name && recorded.ArgsHash == argsHash)
        {
            var replayReply = Execute(recorded, planKey, host, store, sched);
            host.Log.Info("ffa.replayed", new LogFields().Build());
            return replayReply.Length == 0 ? null : Reply(@event, command, replayReply);
        }

        var now = host.Clock.NowMillis();
        var due = DueGame(store, sched, now);
        var plan = due is not null
            ? Settle(due, command, host, store, actor, argsHash, planKey, now)
            : Join(command, host, store, actor, argsHash, planKey, now);

        if (plan.Sweep.Ops.Count == 0 && plan.Sweep.Effects.Count == 0 && plan.Own is null)
        {
            return plan.Reply.Length == 0 ? null : Reply(@event, command, plan.Reply);
        }

        store.SetJson(planKey, plan, FfaJsonContext.Default.EventPlan, PlanTtlSeconds);
        var reply = Execute(plan, planKey, host, store, sched);
        return reply.Length == 0 ? null : Reply(@event, command, reply);
    }

    /// <summary>The game whose join window has ended (scheduler first, the record's own deadline as a backstop), or null.</summary>
    private static GameRecord? DueGame(KvStateStore store, TickScheduler sched, ulong now)
    {
        var current = store.GetJson(RecKey, FfaJsonContext.Default.GameRecord);
        foreach (var task in sched.Pending().Where(t => t.DueAtMillis <= now))
        {
            if (current is null || current.Id != task.Id)
            {
                sched.Cancel(task.Id);
                continue;
            }

            return current;
        }

        return current is not null && now >= current.Due ? current : null;
    }

    /// <summary>Plans the settlement of an ended game; the invoking command is answered "too late" and never joins.</summary>
    private EventPlan Settle(GameRecord game, ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string argsHash, string planKey, ulong now)
    {
        var late = MsgLate;
        EventPlan Plain(string text) => new(actor, command.Name, argsHash, new PlanSection([], []), null, [], text, text, null, text);

        List<string> entrants = [];
        var inflight = false;
        for (var i = 0; i < MaxEntrants; i++)
        {
            var ent = store.GetJson(EntKey(game.Id, i), FfaJsonContext.Default.Entrant);
            if (ent is not null)
            {
                entrants.Add(ent.User);
            }
            else if (store.Get(SlotKey(game.Id, i)) is not null)
            {
                inflight = true;
            }
        }

        if (inflight && now < game.Due + InflightGraceMillis)
        {
            return Plain(late);
        }

        if (!TryOwn(store, SettleKey(game.Id), SettleOwnerKey(game.Id), planKey))
        {
            return Plain(late);
        }

        var alias = Alias(host, actor);
        var section = new PlanSection([], [new Effect("close", game.Id, 0, string.Empty, null)]);
        string notice;
        switch (entrants.Count)
        {
            case 0:
                notice = string.Empty;
                break;
            case 1:
                section.Ops.Add(new MoneyOp("credit", entrants[0], game.Cost));
                notice = MsgNotEnoughPlayers;
                break;
            default:
                var winner = entrants[_nextInt(0, entrants.Count - 1)];
                var winnings = (ulong)entrants.Count * game.Cost;
                section.Ops.Add(new MoneyOp("credit", winner, winnings));
                notice = MsgWinner
                    .Replace("{Name}", UserPlaceholder.For(winner), StringComparison.OrdinalIgnoreCase)
                    .Replace("{Points}", winnings.ToString("N0", CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase)
                    .Replace("{PointType}", alias, StringComparison.OrdinalIgnoreCase);
                break;
        }

        var text = notice.Length == 0 ? late : notice + " " + late;
        return new EventPlan(actor, command.Name, argsHash, section, null, [], text, text, null, text);
    }

    /// <summary>Plans the invoking actor's entry: opens the game if none is running, otherwise joins it.</summary>
    private static EventPlan Join(ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string argsHash, string planKey, ulong now)
    {
        EventPlan Refuse(string text) => new(actor, command.Name, argsHash, new PlanSection([], []), null, [], text, text, null, text);

        var game = store.GetJson(RecKey, FfaJsonContext.Default.GameRecord);
        if (game is null && store.Get(CooldownKey) is not null)
        {
            return Refuse(string.Empty); // original: a command inside the cooldown is dropped silently
        }

        var cost = game?.Cost ?? EntryCost;
        var alias = Alias(host, actor);
        var broke = MsgNotEnoughPoints
            .Replace("{Cost}", cost.ToString("N0", CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase)
            .Replace("{PointType}", alias, StringComparison.OrdinalIgnoreCase);
        if (EconomyServiceClient.For(host).Balance(actor) < (long)cost)
        {
            return Refuse(broke);
        }

        List<string> release = [];
        var opener = game is null;
        if (opener)
        {
            if (!TryOwn(store, LiveKey, LiveOwnerKey, planKey))
            {
                return Refuse("The FFA is starting, try again in a moment.");
            }

            release.AddRange([LiveKey, LiveOwnerKey]);
            game = new GameRecord(StateKey.Hash(planKey), cost, now + JoinMillis);
        }

        void Abort(string text, EventPlan[] holder)
        {
            foreach (var key in release)
            {
                store.Delete(key);
            }

            holder[0] = Refuse(text);
        }

        var id = game!.Id;
        EventPlan[] refused = [null!];
        if (!TryOwn(store, InKey(id, actor), InOwnerKey(id, actor), planKey))
        {
            Abort(MsgAlreadyJoined, refused);
            return refused[0];
        }

        release.AddRange([InKey(id, actor), InOwnerKey(id, actor)]);
        var slot = -1;
        for (var i = 0; i < MaxEntrants && slot < 0; i++)
        {
            if (TryOwn(store, SlotKey(id, i), SlotOwnerKey(id, i), planKey))
            {
                slot = i;
            }
        }

        if (slot < 0)
        {
            Abort("The FFA is full.", refused);
            return refused[0];
        }

        release.AddRange([SlotKey(id, slot), SlotOwnerKey(id, slot)]);
        List<Effect> effects = [];
        if (opener)
        {
            effects.Add(new Effect("open", id, 0, string.Empty, game));
        }

        effects.Add(new Effect("put", id, slot, actor, null));
        var text = opener
            ? MsgStarting.Replace("{Name}", UserPlaceholder.For(actor), StringComparison.OrdinalIgnoreCase).Replace("{CommandName}", command.Name, StringComparison.OrdinalIgnoreCase)
            : MsgJoined.Replace("{Name}", UserPlaceholder.For(actor), StringComparison.OrdinalIgnoreCase);
        var closed = $"{UserPlaceholder.For(actor)}, the FFA ended before your entry registered; your {cost} {alias} was refunded.";
        return new EventPlan(actor, command.Name, argsHash, new PlanSection([], []),
            new PlanSection([new MoneyOp("debit", actor, cost)], effects), release, text, broke, id, closed);
    }

    /// <summary>
    /// Runs a plan: sweep ops and effects first (always committed), then the command's own ops. Only a refused FIRST debit may abort
    /// the own section; any later failure propagates so the redelivered event retries it.
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
            if (plan.Sweep.Ops.Count > 0)
            {
                host.Log.Info("ffa.settled", new LogFields().With("ops", plan.Sweep.Ops.Count).Build());
            }

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
                // Persist the terminal decision before releasing claims: a failed cleanup must never resurrect the aborted debit.
                var aborted = plan with { Own = null, ReleaseOnAbort = [], Reply = plan.AbortReply };
                store.SetJson(planKey, aborted, FfaJsonContext.Default.EventPlan, PlanTtlSeconds);
                foreach (var key in plan.ReleaseOnAbort)
                {
                    store.Delete(key);
                }

                host.Log.Info("ffa.aborted", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
                return aborted.Reply;
            }
        }

        var put = plan.Own.Effects.FirstOrDefault(e => e.Type == "put");
        if (plan.GameId is not null && put is not null && store.Get(SettleKey(plan.GameId)) is not null && store.Get(EntKey(plan.GameId, put.Slot)) is null)
        {
            // The game was settled before this entry registered (a redelivery after the window ended): give the fee back.
            // An entry that DID register (its slot is written) is a plain replay and must never be refunded.
            Run(new MoneyOp("credit", plan.Actor, plan.Own.Ops[0].Amount), host, planKey + ".own.refund");
            host.Log.Info("ffa.late_refund", new LogFields().Build());
            return plan.ClosedReply;
        }

        foreach (var effect in plan.Own.Effects)
        {
            Apply(effect, store, sched);
        }

        host.Log.Info("ffa.joined", new LogFields().With("ops", plan.Own.Ops.Count).Build());
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
                throw new InvalidOperationException($"unknown money op '{op.Kind}' in an ffa plan");
        }
    }

    /// <summary>Applies one state effect; every effect is idempotent so a replay may re-apply it.</summary>
    private static void Apply(Effect effect, KvStateStore store, TickScheduler sched)
    {
        switch (effect.Type)
        {
            case "open":
                var game = effect.Game ?? throw new InvalidOperationException("ffa open effect without a game");
                if (store.Get(SettleKey(game.Id)) is not null)
                {
                    return; // already settled: a replay of the opener must never re-open a closed game.
                }

                store.SetJson(RecKey, game, FfaJsonContext.Default.GameRecord, DayTtlSeconds);
                sched.Schedule(new ScheduledTask(game.Id, game.Due, "settle", string.Empty));
                return;
            case "put":
                store.SetJson(EntKey(effect.Id, effect.Slot), new Entrant(effect.User), FfaJsonContext.Default.Entrant, DayTtlSeconds);
                return;
            case "close":
                var current = store.GetJson(RecKey, FfaJsonContext.Default.GameRecord);
                if (current is not null && current.Id == effect.Id)
                {
                    store.Delete(RecKey);
                }

                store.Delete(LiveKey);
                store.Delete(LiveOwnerKey);
                store.TryClaim(CooldownKey, CooldownSeconds);
                sched.Cancel(effect.Id);
                return;
            default:
                throw new InvalidOperationException($"unknown ffa effect '{effect.Type}'");
        }
    }

    /// <summary>First-writer-wins ownership of <paramref name="claimKey"/>; a later call is the owner only if its token matches (the owning event's redelivery resumes, nobody else does).</summary>
    private static bool TryOwn(KvStateStore store, string claimKey, string ownerKey, string token)
    {
        if (store.TryClaim(claimKey, DayTtlSeconds))
        {
            store.Set(ownerKey, Encoding.UTF8.GetBytes(token), DayTtlSeconds);
            return true;
        }

        var owner = store.Get(ownerKey);
        return owner is not null && Encoding.UTF8.GetString(owner) == token;
    }

    internal const string RecKey = "ffa.rec";
    internal const string LiveKey = "ffa.live";
    private const string LiveOwnerKey = "ffa.liveown";
    internal const string CooldownKey = "ffa.cd";

    private static string SettleKey(string id) => StateKey.Of("ffa.settle", id);

    private static string SettleOwnerKey(string id) => StateKey.Of("ffa.sown", id);

    private static string SlotKey(string id, int slot) => StateKey.Of("ffa.slot", id, slot.ToString(CultureInfo.InvariantCulture));

    private static string SlotOwnerKey(string id, int slot) => StateKey.Of("ffa.slotown", id, slot.ToString(CultureInfo.InvariantCulture));

    private static string EntKey(string id, int slot) => StateKey.Of("ffa.ent", id, slot.ToString(CultureInfo.InvariantCulture));

    private static string InKey(string id, string user) => StateKey.Of("ffa.in", id, StateKey.Hash(user));

    private static string InOwnerKey(string id, string user) => StateKey.Of("ffa.inown", id, StateKey.Hash(user));

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
            host.Log.Warn("ffa.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
}

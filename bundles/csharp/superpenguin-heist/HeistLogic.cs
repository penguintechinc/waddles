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

namespace WaddleBundleSuperpenguinHeist;

/// <summary>
/// `!heist AMOUNT|ALL|MAX|N%`, ported from PenguinTwitchBot's `PastyGames/Heist.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT,
/// used with permission -- see `bundle.yaml`'s `notice`). Faithful to the original: the first call opens a join window (default
/// 300 s), every entrant stakes their own bet (min 10, max 500, `all`/`max`/percentages of the smaller of the cap and their balance), each entrant
/// then survives with a 60% chance (a 1..100 roll, over 40 survives), survivors are paid `bet + round(bet * 1.5)` (the win
/// multiplier clamped to 1..10), caught entrants lose the bet, a 300 s cooldown follows, and the original's chat wording is kept.
/// The app owns every rule; the economy SERVICE is only I/O.
///
/// <para><b>Money model.</b> The economy debits only the INVOKING actor, so a bet leaves a balance only on that player's own
/// `!heist` (service `adjust_balance` debit, a burn). The burned bets ARE the stake pool, tracked by the open
/// <see cref="GameRecord"/> and the entrant slots. Survivor payouts are credits (mints) and are subject to the economy's mint caps:
/// an `over_cap` propagates loudly and the redelivered event retries the identical credit.</para>
///
/// <para><b>Exactly-once money.</b> Every event writes a PLAN (ordered money ops + state effects, every survival roll already made)
/// under a replay key BEFORE its first economy call; a redelivered event re-executes the same plan and the host de-duplicates
/// each call per (event, kind, ordinal). A plan is a replay only on an exact actor + command + raw-argument match; a blank message id
/// throws. Entries claim their own slot key atomically (kv has no CAS, so no shared list is ever rewritten), settlement takes an
/// atomic claim owned by the event's plan key, and a join whose game was settled before it registered is refunded.</para>
///
/// <para><b>Settlement and deviations (documented).</b> No timer exists for app bundles, so the SDK <see cref="TickScheduler"/>
/// emulation settles the heist when the NEXT `!heist` reaches this bundle after the deadline; that command is answered "you can
/// not join the heist now" and never joins. A settlement is deferred up to <see cref="InflightGraceMillis"/> while a slot is
/// claimed but not yet written. The original's staged story (four messages five seconds apart) becomes ONE reply (the caught line and
/// the survivor summary): an event answers once and nothing may sleep. Later joiners get no chat line, as upstream. The original's
/// "512 character" survivor list guard becomes "more than 8 survivors -> counts only" (names render at egress). The host does not
/// forward per-community settings yet, so the original's defaults apply. Bets of a heist nobody settles within 24 h need an operator.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class HeistLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const ulong MinBet = 10;
    internal const ulong MaxBet = 500;
    internal const ulong JoinMillis = 300_000;
    internal const uint CooldownSeconds = 300;
    internal const int MaxEntrants = 25;
    internal const int SurvivePercentOver = 40;
    internal const double WinMultiplier = 1.5;
    internal const double MaxWinMultiplier = 10;
    internal const int MaxNamedSurvivors = 8;
    internal const ulong InflightGraceMillis = 300_000;
    internal const uint DayTtlSeconds = 24 * 60 * 60;
    internal const uint PlanTtlSeconds = 3600;
    internal const string FlagKey = "waddles.command-superpenguin-heist";

    internal const string MsgStarting = "{Name} is trying to get a team together for some serious heist business! use \"!{Command} AMOUNT/ALL/MAX\" to join!";
    internal const string MsgFinishing = "you can not join the heist now.";
    internal const string MsgAlreadyJoined = "you have already joined the heist.";
    internal const string MsgInvalidArgs = "To Enter/Start a heist do !{Command} AMOUNT/ALL/MAX/%";
    internal const string MsgInvalidBet = "The max amount to join the heist is {MaxBet} {PointType} and must be greater then {MinBet} {PointType}";
    internal const string MsgNotEnoughPoints = "sorry you don't have that amount of {PointType} to enter the heist.";
    internal const string MsgCaught = "Look out! The troll captured {Caught}";
    internal const string MsgNoOneWon = "The heist ended! There are no survivors.";
    internal const string MsgNamesTooLong = "The heist ended with {SurvivorsCount} survivor(s) and {CaughtCount} death(s).";
    internal const string MsgSurvivors = "The heist ended! Survivors are: {Payouts}.";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("heist"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("heist.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "Heists need a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("heist.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before joining a heist.");
        }

        var store = new KvStateStore(host.Kv);
        var sched = new TickScheduler(store, host.Clock, "heist");
        var planKey = ReplayGuard.KeyFor("heist.ev", @event, host, actor, command.Name);
        var argsHash = StateKey.Hash(command.RawText);

        var recorded = store.GetJson(planKey, HeistJsonContext.Default.EventPlan);
        if (recorded is not null && recorded.Actor == actor && recorded.Command == command.Name && recorded.ArgsHash == argsHash)
        {
            var replayReply = Execute(recorded, planKey, host, store, sched);
            host.Log.Info("heist.replayed", new LogFields().Build());
            return replayReply.Length == 0 ? null : Reply(@event, command, replayReply);
        }

        var now = host.Clock.NowMillis();
        var due = DueGame(store, sched, now);
        var plan = due is not null
            ? Settle(due, command, store, actor, argsHash, planKey, now)
            : Join(command, host, store, actor, argsHash, planKey, now);

        if (plan.Sweep.Ops.Count == 0 && plan.Sweep.Effects.Count == 0 && plan.Own is null)
        {
            return plan.Reply.Length == 0 ? null : Reply(@event, command, plan.Reply);
        }

        store.SetJson(planKey, plan, HeistJsonContext.Default.EventPlan, PlanTtlSeconds);
        var reply = Execute(plan, planKey, host, store, sched);
        return reply.Length == 0 ? null : Reply(@event, command, reply);
    }

    /// <summary>The heist whose join window has ended (scheduler first, the record's own deadline as a backstop), or null.</summary>
    private static GameRecord? DueGame(KvStateStore store, TickScheduler sched, ulong now)
    {
        var current = store.GetJson(RecKey, HeistJsonContext.Default.GameRecord);
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

    /// <summary>Plans the settlement of an ended heist (rolling every survival now); the invoking command is answered "can not join" and never joins.</summary>
    private EventPlan Settle(GameRecord game, ChatCommand command, KvStateStore store, string actor, string argsHash, string planKey, ulong now)
    {
        EventPlan Plain(string text) => new(actor, command.Name, argsHash, new PlanSection([], []), null, [], text, text, null, text);

        List<Entrant> entrants = [];
        var inflight = false;
        for (var i = 0; i < MaxEntrants; i++)
        {
            var ent = store.GetJson(EntKey(game.Id, i), HeistJsonContext.Default.Entrant);
            if (ent is not null)
            {
                entrants.Add(ent);
            }
            else if (store.Get(SlotKey(game.Id, i)) is not null)
            {
                inflight = true;
            }
        }

        if (inflight && now < game.Due + InflightGraceMillis)
        {
            return Plain(MsgFinishing);
        }

        if (!TryOwn(store, SettleKey(game.Id), SettleOwnerKey(game.Id), planKey))
        {
            return Plain(MsgFinishing);
        }

        var section = new PlanSection([], [new Effect("close", game.Id, 0, string.Empty, 0, null)]);
        List<Entrant> survivors = [];
        List<Entrant> caught = [];
        foreach (var entrant in entrants)
        {
            // Every roll is made here, before the first money call, and lives in the plan: a redelivery repeats the same fate.
            (_nextInt(1, 100) > SurvivePercentOver ? survivors : caught).Add(entrant);
        }

        List<string> payouts = [];
        var multiplier = Math.Clamp(WinMultiplier, 1d, MaxWinMultiplier);
        foreach (var s in survivors)
        {
            var total = s.Bet + (ulong)Convert.ToInt64(s.Bet * multiplier);
            section.Ops.Add(new MoneyOp("credit", s.User, total));
            payouts.Add($"{UserPlaceholder.For(s.User)} ({total.ToString("N0", CultureInfo.InvariantCulture)})");
        }

        List<string> lines = [];
        if (caught.Count > 0)
        {
            lines.Add(MsgCaught.Replace("{Caught}", string.Join(", ", caught.Select(c => UserPlaceholder.For(c.User))), StringComparison.OrdinalIgnoreCase));
        }

        if (entrants.Count == 0 || payouts.Count == 0)
        {
            lines.Add(MsgNoOneWon);
        }
        else if (payouts.Count > MaxNamedSurvivors)
        {
            lines.Add(MsgNamesTooLong
                .Replace("{SurvivorsCount}", survivors.Count.ToString(CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase)
                .Replace("{CaughtCount}", caught.Count.ToString(CultureInfo.InvariantCulture), StringComparison.OrdinalIgnoreCase));
        }
        else
        {
            lines.Add(MsgSurvivors.Replace("{Payouts}", string.Join(", ", payouts), StringComparison.OrdinalIgnoreCase));
        }

        lines.Add(UserPlaceholder.For(actor) + ", " + MsgFinishing);
        var text = string.Join(" ", lines);
        return new EventPlan(actor, command.Name, argsHash, section, null, [], text, text, null, text);
    }

    /// <summary>Plans the invoking actor's entry: parses and bounds the bet, then opens the heist if none is running, otherwise joins it.</summary>
    private static EventPlan Join(ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string argsHash, string planKey, ulong now)
    {
        EventPlan Refuse(string text) => new(actor, command.Name, argsHash, new PlanSection([], []), null, [], text, text, null, text);

        var game = store.GetJson(RecKey, HeistJsonContext.Default.GameRecord);
        if (game is null && store.Get(CooldownKey) is not null)
        {
            return Refuse(string.Empty); // original: a command inside the cooldown is dropped silently
        }

        if (command.Args.Count == 0)
        {
            return Refuse(MsgInvalidArgs.Replace("{Command}", command.Name, StringComparison.OrdinalIgnoreCase));
        }

        var alias = Alias(host, actor);
        var balance = EconomyServiceClient.For(host).Balance(actor);
        var maxBet = Math.Min(MaxBet, (ulong)Math.Max(0, balance)); // what `all`/`max`/percentages resolve against
        var token = command.Args[0];
        ulong bet;
        if (token.Equals("all", StringComparison.OrdinalIgnoreCase) || token.Equals("max", StringComparison.OrdinalIgnoreCase))
        {
            bet = maxBet;
        }
        else if (token.Contains('%'))
        {
            if (!TryParsePercent(token, out var fraction))
            {
                return Refuse(MsgInvalidArgs.Replace("{Command}", command.Name, StringComparison.OrdinalIgnoreCase));
            }

            bet = (ulong)(maxBet * fraction);
        }
        else if (long.TryParse(token, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed))
        {
            bet = parsed < 0 ? 0 : (ulong)parsed;
        }
        else
        {
            return Refuse(MsgInvalidArgs.Replace("{Command}", command.Name, StringComparison.OrdinalIgnoreCase));
        }

        string Fmt(ulong v) => v.ToString("N0", CultureInfo.InvariantCulture);
        if (bet < MinBet)
        {
            return Refuse(MsgInvalidBet.Replace("{MaxBet}", Fmt(MaxBet), StringComparison.OrdinalIgnoreCase).Replace("{MinBet}", Fmt(MinBet), StringComparison.OrdinalIgnoreCase).Replace("{PointType}", alias, StringComparison.OrdinalIgnoreCase));
        }

        var broke = MsgNotEnoughPoints.Replace("{PointType}", alias, StringComparison.OrdinalIgnoreCase);
        if (balance < (long)bet)
        {
            return Refuse(broke);
        }

        if (bet > MaxBet)
        {
            return Refuse(MsgInvalidBet.Replace("{MaxBet}", Fmt(MaxBet), StringComparison.OrdinalIgnoreCase).Replace("{MinBet}", Fmt(MinBet), StringComparison.OrdinalIgnoreCase).Replace("{PointType}", alias, StringComparison.OrdinalIgnoreCase));
        }

        List<string> release = [];
        var opener = game is null;
        if (opener)
        {
            if (!TryOwn(store, LiveKey, LiveOwnerKey, planKey))
            {
                return Refuse("The heist is starting, try again in a moment.");
            }

            release.AddRange([LiveKey, LiveOwnerKey]);
            game = new GameRecord(StateKey.Hash(planKey), now + JoinMillis);
        }

        var id = game!.Id;

        EventPlan Abort(string text)
        {
            foreach (var key in release)
            {
                store.Delete(key);
            }

            return Refuse(text);
        }

        if (!TryOwn(store, InKey(id, actor), InOwnerKey(id, actor), planKey))
        {
            return Abort(MsgAlreadyJoined);
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
            return Abort("The heist crew is full.");
        }

        release.AddRange([SlotKey(id, slot), SlotOwnerKey(id, slot)]);
        List<Effect> effects = [];
        if (opener)
        {
            effects.Add(new Effect("open", id, 0, string.Empty, 0, game));
        }

        effects.Add(new Effect("put", id, slot, actor, bet, null));
        var text = opener
            ? MsgStarting.Replace("{Name}", UserPlaceholder.For(actor), StringComparison.OrdinalIgnoreCase).Replace("{Command}", command.Name, StringComparison.OrdinalIgnoreCase)
            : string.Empty; // original: later joiners get no chat line
        var closed = $"{UserPlaceholder.For(actor)}, the heist ended before your entry registered; your {bet} {alias} was refunded.";
        return new EventPlan(actor, command.Name, argsHash, new PlanSection([], []),
            new PlanSection([new MoneyOp("debit", actor, bet)], effects), release, text, broke, id, closed);
    }

    /// <summary>Parses `N%` (0 &lt; N &lt;= 100) into a fraction.</summary>
    internal static bool TryParsePercent(string token, out double fraction)
    {
        fraction = 0;
        var digits = token.Replace("%", string.Empty, StringComparison.Ordinal);
        if (!double.TryParse(digits, NumberStyles.Float, CultureInfo.InvariantCulture, out var percent) || !double.IsFinite(percent) || percent <= 0 || percent > 100 || token.Count(c => c == '%') != 1)
        {
            return false;
        }

        fraction = percent / 100d;
        return true;
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
            if (plan.Sweep.Effects.Count > 0)
            {
                host.Log.Info("heist.settled", new LogFields().With("ops", plan.Sweep.Ops.Count).Build());
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
                store.SetJson(planKey, aborted, HeistJsonContext.Default.EventPlan, PlanTtlSeconds);
                foreach (var key in plan.ReleaseOnAbort)
                {
                    store.Delete(key);
                }

                host.Log.Info("heist.aborted", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
                return aborted.Reply;
            }
        }

        var put = plan.Own.Effects.FirstOrDefault(e => e.Type == "put");
        if (plan.GameId is not null && put is not null && store.Get(SettleKey(plan.GameId)) is not null && store.Get(EntKey(plan.GameId, put.Slot)) is null)
        {
            // The heist was settled before this entry registered (a redelivery after the window ended): give the bet back.
            // An entry that DID register (its slot is written) is a plain replay and must never be refunded.
            Run(new MoneyOp("credit", plan.Actor, plan.Own.Ops[0].Amount), host, planKey + ".own.refund");
            host.Log.Info("heist.late_refund", new LogFields().Build());
            return plan.ClosedReply;
        }

        foreach (var effect in plan.Own.Effects)
        {
            Apply(effect, store, sched);
        }

        host.Log.Info("heist.joined", new LogFields().With("ops", plan.Own.Ops.Count).Build());
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
                throw new InvalidOperationException($"unknown money op '{op.Kind}' in a heist plan");
        }
    }

    /// <summary>Applies one state effect; every effect is idempotent so a replay may re-apply it.</summary>
    private static void Apply(Effect effect, KvStateStore store, TickScheduler sched)
    {
        switch (effect.Type)
        {
            case "open":
                var game = effect.Game ?? throw new InvalidOperationException("heist open effect without a game");
                if (store.Get(SettleKey(game.Id)) is not null)
                {
                    return; // already settled: a replay of the opener must never re-open a closed heist.
                }

                store.SetJson(RecKey, game, HeistJsonContext.Default.GameRecord, DayTtlSeconds);
                sched.Schedule(new ScheduledTask(game.Id, game.Due, "settle", string.Empty));
                return;
            case "put":
                store.SetJson(EntKey(effect.Id, effect.Slot), new Entrant(effect.User, effect.Bet), HeistJsonContext.Default.Entrant, DayTtlSeconds);
                return;
            case "close":
                var current = store.GetJson(RecKey, HeistJsonContext.Default.GameRecord);
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
                throw new InvalidOperationException($"unknown heist effect '{effect.Type}'");
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

    internal const string RecKey = "heist.rec";
    internal const string LiveKey = "heist.live";
    private const string LiveOwnerKey = "heist.liveown";
    internal const string CooldownKey = "heist.cd";

    private static string SettleKey(string id) => StateKey.Of("heist.settle", id);

    private static string SettleOwnerKey(string id) => StateKey.Of("heist.sown", id);

    private static string SlotKey(string id, int slot) => StateKey.Of("heist.slot", id, slot.ToString(CultureInfo.InvariantCulture));

    private static string SlotOwnerKey(string id, int slot) => StateKey.Of("heist.slotown", id, slot.ToString(CultureInfo.InvariantCulture));

    private static string EntKey(string id, int slot) => StateKey.Of("heist.ent", id, slot.ToString(CultureInfo.InvariantCulture));

    private static string InKey(string id, string user) => StateKey.Of("heist.in", id, StateKey.Hash(user));

    private static string InOwnerKey(string id, string user) => StateKey.Of("heist.inown", id, StateKey.Hash(user));

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
            host.Log.Warn("heist.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
}

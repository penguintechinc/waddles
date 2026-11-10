using System.Globalization;
using SuperPenguinShared;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinGiveaway;

/// <summary>
/// The ticket giveaway (`!tgw` / `!ticketgiveaway`), ported from PenguinTwitchBot's `Features/GiveawayFeature.cs` and
/// `GiveawayFairnessReport.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s
/// `notice`), plus `TicketGames/RaffleRuntimeService.cs` (see <c>Raffle.cs</c>). This is the policy layer: who may enter, how
/// the draw is made provable, who is excluded. The economy SERVICE owns the ledger (entries debit `adjust_balance`).
///
/// <para><b>Commands.</b> Everyone: `enter &lt;n|max|all&gt;`, `entries`, `status`, `audit`, `raffle enter|count`. Moderators and the
/// broadcaster (badges from the event payload, fail-closed): `open`, `close`, `draw`, `reset`, `prize &lt;text&gt;`, `tier &lt;1-3&gt;`,
/// `exclude &lt;mention&gt; [hours]`, `unexclude &lt;mention&gt;`, `fairness [iterations]`, `overlay on|off|register`, and the raffle
/// administration commands. The upstream root commands `!enter`/`!draw`/... collide with other bundles and are namespaced here.</para>
///
/// <para><b>Provable draw.</b> `close` freezes the eligible pool into a fingerprint and publishes
/// <c>commitment = SHA-256(seed || fingerprint)</c> before the draw; `draw` reveals the seed and derives the winning ticket with
/// unbiased rejection sampling (<see cref="Fairness"/>), then publishes an audit record. Repeated draws exclude earlier winners of
/// the round. Re-closing an unchanged pool reuses the same commitment; a changed pool necessarily publishes a new one, so viewers
/// should watch for more than one commitment per round.</para>
///
/// <para><b>Exclusions.</b> Moderators, the broadcaster and `is_bot`-badged users cannot enter; the explicit list (optional expiry)
/// and recent winners (sit out <c>tier</c> following rounds, the upstream cooldown tiers) cannot enter either, and the exclusions
/// are re-applied when the pool is frozen. Tickets of a user excluded after entering are NOT refunded.</para>
///
/// <para><b>Money and replay.</b> The entry is recorded under the event's replay key BEFORE the single debit, which the host
/// de-duplicates per event; ticket application is idempotent through an event tag stored in the entrant's pool entry (last four
/// tags), so a redelivery neither double-charges nor double-credits tickets.</para>
///
/// <para><b>Known limits (documented, none silent).</b> kv has no compare-and-swap: concurrent entries by different users in the
/// same shard can overwrite each other; every write is read back and retried up to three times, and a failure after that throws
/// (the redelivered event re-applies exactly once). A redelivered entry that arrives after the pool was frozen still applies its
/// tickets, which then changes the fingerprint; `draw` refuses with a "pool changed, close again" message instead of drawing.
/// There is no timer: nothing happens until a moderator command arrives. Hours of an exclusion are wall-clock from the host clock.
/// Overlay pushes are opt-in (`!tgw overlay on`) because the host's overlay capability is not wired in every deployment; when
/// enabled a refused push fails the command loudly.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed partial class GiveawayLogic(Action<byte[]>? seedFill = null) : WaddleProcessStageNext
{
    internal const string Prefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-giveaway";
    internal const string WidgetId = "superpenguin.giveaway";
    internal const long PointsPerEntry = 1;
    internal const long MaxEntriesPerCommand = 1_000_000;
    internal const long MaxTicketsPerUser = 100_000_000;
    internal const int ShardCap = 400;
    internal const int EventTagsKept = 4;
    internal const int MaxExclusions = 400;
    internal const int MaxWinnerLog = 100;
    internal const int MaxPrizeLength = 200;
    internal const int DefaultFairnessIterations = 20_000;
    internal const int MinFairnessIterations = 1_000;
    internal const int MaxFairnessIterations = 200_000;
    internal const uint StateTtlSeconds = 30 * 24 * 60 * 60;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const string HexDigits = "0123456789abcdef";

    internal const string ModOnly = "only moderators and the broadcaster can do that.";
    internal const string Usage =
        "Usage: !tgw enter <n|max|all> | entries | status | audit | raffle enter|count <key>. Moderators: open close draw reset prize <text> tier <1-3> exclude <user> [hours] unexclude <user> fairness [n] overlay on|off|register raffle start|end|winners|award <key> ...";

    private static readonly string[] ModSubcommands = ["open", "close", "draw", "reset", "prize", "tier", "exclude", "unexclude", "fairness", "overlay"];

    private readonly Action<byte[]>? _seedFill = seedFill;

    /// <summary>Everything one command needs: the event, parsed command, host, store, resolved actor and the clock reading.</summary>
    private sealed record Ctx(PlatformEventInfo Event, ChatCommand Command, IWaddleHostNext Host, KvStateStore Store, string Actor, long Now);

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, Prefix);
        if (command is null || !command.IsAny("tgw", "ticketgiveaway"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("tgw.refused", new LogFields().With("reason", "no_community").Build());
            return Replies.Reply(@event, command, "The giveaway needs a community; this channel is not linked to one.");
        }

        var sub = command.Args.Count == 0 ? string.Empty : command.Args[0].ToLowerInvariant();
        var args = command.Args.Skip(1).ToList();
        var isMod = ChatPrivileges.IsModerator(@event.PayloadJson);
        var raffleAdmin = sub == "raffle" && args.Count > 0 && args[0].ToLowerInvariant() is "start" or "end" or "winners" or "award";
        if ((Array.IndexOf(ModSubcommands, sub) >= 0 || raffleAdmin) && !isMod)
        {
            host.Log.Info("tgw.refused", new LogFields().With("reason", "not_moderator").Build());
            return Replies.Reply(@event, command, ModOnly);
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("tgw.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Replies.Reply(@event, command, "Link your account to this community to use the giveaway.");
        }

        var c = new Ctx(@event, command, host, new KvStateStore(host.Kv), actor, (long)host.Clock.NowMillis());
        return sub switch
        {
            "enter" => Enter(c, args),
            "entries" => Say(c, $"{User(c)} has {EntryTickets(c, LoadState(c).Round)} entries into the giveaway."),
            "status" => Status(c),
            "audit" => Audit(c),
            "open" => Open(c),
            "close" => Close(c),
            "draw" => Draw(c),
            "reset" => Reset(c),
            "prize" => Prize(c, args),
            "tier" => Tier(c, args),
            "exclude" => Exclude(c, args),
            "unexclude" => Unexclude(c, args),
            "fairness" => FairnessReport(c, args),
            "overlay" => OverlayConfig(c, args),
            "raffle" => RaffleCommand(c, args),
            _ => Say(c, Usage),
        };
    }

    private static PlatformEventInfo Say(Ctx c, string text) => Replies.Reply(c.Event, c.Command, text);

    private static string User(Ctx c) => UserPlaceholder.For(c.Actor);

    private static string Num(long value) => value.ToString(CultureInfo.InvariantCulture);

    private static string StateKeyOf() => StateKey.Of("tgw", "state");

    private static GiveawayState LoadState(Ctx c) =>
        c.Store.GetJson(StateKeyOf(), GiveawayJsonContext.Default.GiveawayState) ?? new GiveawayState(1, true, string.Empty, 1, null);

    private static void SaveState(Ctx c, GiveawayState state) =>
        c.Store.SetJson(StateKeyOf(), state, GiveawayJsonContext.Default.GiveawayState, StateTtlSeconds);

    private static string PrizeText(GiveawayState state) => string.IsNullOrWhiteSpace(state.Prize) ? "No Prize" : state.Prize;

    private static string ShardKey(long round, string actor) => StateKey.Of("tgw", "p", Num(round), StateKey.Hash(actor)[..1]);

    private static string EventTag(Ctx c) => StateKey.Hash($"{c.Host.Context.MessageId}|{c.Event.Platform}|{c.Actor}|{c.Command.Name}")[..8];

    private static long EntryTickets(Ctx c, long round) =>
        c.Store.GetJson(ShardKey(round, c.Actor), GiveawayJsonContext.Default.PoolShard)?.Entries.FirstOrDefault(e => e.Actor == c.Actor)?.Tickets ?? 0;

    private static List<PoolEntry> ReadPool(KvStateStore store, long round)
    {
        List<PoolEntry> all = [];
        foreach (var digit in HexDigits)
        {
            var shard = store.GetJson(StateKey.Of("tgw", "p", Num(round), digit.ToString()), GiveawayJsonContext.Default.PoolShard);
            if (shard is not null)
            {
                all.AddRange(shard.Entries);
            }
        }

        return all;
    }

    private static ExclusionList LoadExclusions(Ctx c) =>
        c.Store.GetJson(StateKey.Of("tgw", "excl"), GiveawayJsonContext.Default.ExclusionList) ?? new ExclusionList([]);

    private static WinnerLog LoadWinners(Ctx c) =>
        c.Store.GetJson(StateKey.Of("tgw", "win"), GiveawayJsonContext.Default.WinnerLog) ?? new WinnerLog([]);

    private static GiveawayConfig LoadConfig(Ctx c) =>
        c.Store.GetJson(StateKey.Of("tgw", "cfg"), GiveawayJsonContext.Default.GiveawayConfig) ?? new GiveawayConfig(false);

    /// <summary>The refusal text for an actor who may not enter, or null when they are eligible (badges, exclusions, cooldown).</summary>
    private static string? EntryRefusal(Ctx c, GiveawayState state)
    {
        if (ChatPrivileges.IsModerator(c.Event.PayloadJson) || ChatPrivileges.IsBot(c.Event.PayloadJson))
        {
            return $"{User(c)}, moderators, the broadcaster and bots cannot enter the giveaway.";
        }

        if (IsExcluded(c, c.Actor))
        {
            return $"{User(c)}, you are excluded from giveaways.";
        }

        return CoolingDown(LoadWinners(c), state.Round).Contains(c.Actor)
            ? $"{User(c)}, you won recently and are sitting out this giveaway."
            : null;
    }

    private static bool IsExcluded(Ctx c, string actor) =>
        LoadExclusions(c).Items.Any(i => i.Actor == actor && (i.UntilMillis == 0 || i.UntilMillis > c.Now));

    private static HashSet<string> CoolingDown(WinnerLog log, long round) =>
        log.Items.Where(w => round > w.Round && round - w.Round <= w.Cooldown).Select(w => w.Actor).ToHashSet(StringComparer.Ordinal);

    /// <summary>The sorted pool of entrants who are still eligible now; earlier winners of the round are removed only when asked.</summary>
    private static List<Ticket> EligiblePool(Ctx c, GiveawayState state, bool withoutDrawn)
    {
        var excluded = LoadExclusions(c).Items.Where(i => i.UntilMillis == 0 || i.UntilMillis > c.Now).Select(i => i.Actor).ToHashSet(StringComparer.Ordinal);
        var cooling = CoolingDown(LoadWinners(c), state.Round);
        HashSet<string> drawn = withoutDrawn && state.Freeze is not null ? state.Freeze.Won.ToHashSet(StringComparer.Ordinal) : [];
        return Fairness.Sorted(ReadPool(c.Store, state.Round)
            .Where(e => e.Tickets > 0 && !excluded.Contains(e.Actor) && !cooling.Contains(e.Actor) && !drawn.Contains(e.Actor))
            .Select(e => new Ticket(e.Actor, e.Tickets)));
    }

    private PlatformEventInfo? Enter(Ctx c, List<string> args)
    {
        if (args.Count != 1)
        {
            return Say(c, "To enter tickets, please use !tgw enter AMOUNT/MAX/ALL");
        }

        var state = LoadState(c);
        var key = ReplayGuard.KeyFor("tgw.out", c.Event, c.Host, c.Actor, "enter");
        var recorded = c.Store.GetJson(key, GiveawayJsonContext.Default.EntryOutcome);
        var replay = recorded is not null && recorded.Actor == c.Actor && recorded.Command == "enter";
        EntryOutcome outcome;
        if (replay)
        {
            outcome = recorded!;
        }
        else
        {
            if (!state.Open)
            {
                return Say(c, $"{User(c)}, the giveaway is closed and not accepting entries.");
            }

            var refusal = EntryRefusal(c, state);
            if (refusal is not null)
            {
                return Say(c, refusal);
            }

            if (!TryEntries(c, args[0], out var entries))
            {
                return Say(c, $"{User(c)}, please use a number or max/all when entering.");
            }

            if (entries < 1 || entries > MaxEntriesPerCommand)
            {
                return Say(c, $"{User(c)}, you do not have enough or that many tickets to enter.");
            }

            var preflight = Preflight(c, state, entries);
            if (preflight is not null)
            {
                return Say(c, preflight);
            }

            outcome = new EntryOutcome(c.Actor, "enter", state.Round, entries, EventTag(c));
            c.Store.SetJson(key, outcome, GiveawayJsonContext.Default.EntryOutcome, OutcomeTtlSeconds);
        }

        try
        {
            EconomyServiceClient.For(c.Host, "enter").Debit(c.Actor, (ulong)(outcome.Tickets * PointsPerEntry));
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            c.Store.Delete(key);
            c.Host.Log.Info("tgw.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            return Say(c, $"{User(c)}, you do not have enough or that many tickets to enter.");
        }

        var total = AddTickets(c, outcome);
        c.Host.Log.Info("tgw.entered", new LogFields().With("tickets", outcome.Tickets).With("replay", replay).Build());
        return Say(c, $"{User(c)}, you have bought {Num(outcome.Tickets)} entries ({Num(total)} in total).");
    }

    /// <summary>Parses `n`, `max` or `all`; max/all read the balance from the economy service's `points_view`.</summary>
    private static bool TryEntries(Ctx c, string arg, out long entries)
    {
        entries = 0;
        if (arg.Equals("max", StringComparison.OrdinalIgnoreCase) || arg.Equals("all", StringComparison.OrdinalIgnoreCase))
        {
            entries = Math.Min(EconomyServiceTools.PointsView(c.Host.Tool, c.Actor).Balance / PointsPerEntry, MaxEntriesPerCommand);
            return true;
        }

        return long.TryParse(arg, NumberStyles.None, CultureInfo.InvariantCulture, out entries);
    }

    /// <summary>Checks, before any money moves, that the entry fits the per-user ticket cap and the shard's capacity.</summary>
    private static string? Preflight(Ctx c, GiveawayState state, long entries)
    {
        var shard = c.Store.GetJson(ShardKey(state.Round, c.Actor), GiveawayJsonContext.Default.PoolShard);
        var existing = shard?.Entries.FirstOrDefault(e => e.Actor == c.Actor);
        if (existing is null && (shard?.Entries.Count ?? 0) >= ShardCap)
        {
            return $"{User(c)}, the giveaway is full.";
        }

        return (existing?.Tickets ?? 0) + entries > MaxTicketsPerUser
            ? $"{User(c)}, that would exceed the per-person ticket limit."
            : null;
    }

    /// <summary>Adds the tickets once (event-tag idempotent) with read-back verification; throws when three attempts fail.</summary>
    private static long AddTickets(Ctx c, EntryOutcome outcome)
    {
        var key = ShardKey(outcome.Round, outcome.Actor);
        for (var attempt = 0; attempt < 3; attempt++)
        {
            var shard = c.Store.GetJson(key, GiveawayJsonContext.Default.PoolShard) ?? new PoolShard([]);
            var entry = shard.Entries.FirstOrDefault(e => e.Actor == outcome.Actor);
            if (entry is not null && entry.Events.Contains(outcome.Tag))
            {
                return entry.Tickets;
            }

            if (entry is null && shard.Entries.Count >= ShardCap)
            {
                throw new InvalidOperationException("giveaway pool shard is full; operator recovery required");
            }

            var tags = (entry?.Events ?? []).TakeLast(EventTagsKept - 1).Append(outcome.Tag).ToList();
            var updated = new PoolEntry(outcome.Actor, (entry?.Tickets ?? 0) + outcome.Tickets, tags);
            var entries = shard.Entries.Where(e => e.Actor != outcome.Actor).Append(updated).ToList();
            c.Store.SetJson(key, new PoolShard(entries), GiveawayJsonContext.Default.PoolShard, StateTtlSeconds);

            var check = c.Store.GetJson(key, GiveawayJsonContext.Default.PoolShard)?.Entries.FirstOrDefault(e => e.Actor == outcome.Actor);
            if (check is not null && check.Events.Contains(outcome.Tag))
            {
                return check.Tickets;
            }

            c.Host.Log.Warn("tgw.shard_race", new LogFields().With("attempt", attempt).Build());
        }

        throw new InvalidOperationException("could not record tickets after 3 attempts; the redelivered event will re-apply them");
    }

    private PlatformEventInfo? Status(Ctx c)
    {
        var state = LoadState(c);
        var pool = ReadPool(c.Store, state.Round).Where(e => e.Tickets > 0).ToList();
        var mine = pool.FirstOrDefault(e => e.Actor == c.Actor)?.Tickets ?? 0;
        var phase = state.Open ? "open" : state.Freeze is null ? "closed" : $"closed (commitment {state.Freeze.Commitment})";
        return Say(c, $"Giveaway round {Num(state.Round)} is {phase}: prize {PrizeText(state)}, {pool.Count} entrants with {Num(pool.Sum(e => e.Tickets))} tickets, {User(c)} has {Num(mine)}.");
    }

    private PlatformEventInfo? Audit(Ctx c)
    {
        var audit = c.Store.GetJson(StateKey.Of("tgw", "audit"), GiveawayJsonContext.Default.AuditRecord);
        return Say(c, audit is null ? "No draw has been made yet." : AuditText(audit));
    }

    /// <summary>The one-line audit record: every value a viewer needs to verify the commitment and re-derive the winning index.</summary>
    internal static string AuditText(AuditRecord a) =>
        $"Audit round {Num(a.Round)} draw {a.Draw}: fp={a.Fingerprint} commit={a.Commitment} seed={a.Seed} total={Num(a.Total)} index={Num(a.Index)} range={Num(a.RangeStart)}-{Num(a.RangeEnd)} protocol={Fairness.Version}";

    private PlatformEventInfo? Open(Ctx c)
    {
        var state = LoadState(c);
        if (state.Open)
        {
            return Say(c, "The giveaway is already open.");
        }

        SaveState(c, state with { Open = true, Freeze = null });
        PushOverlay(c, "open", w => { w.WriteString("prize", PrizeText(state)); w.WriteNumber("round", state.Round); }, "The giveaway is open!", null);
        return Say(c, $"The giveaway is open! Type !tgw enter AMOUNT to enter. Prize: {PrizeText(state)}");
    }

    private PlatformEventInfo? Close(Ctx c)
    {
        var state = LoadState(c);
        var pool = EligiblePool(c, state, withoutDrawn: false);
        if (pool.Count == 0)
        {
            SaveState(c, state with { Open = false, Freeze = null });
            return Say(c, "The giveaway is closed with no eligible entries.");
        }

        var fingerprint = Fairness.Fingerprint(state.Round, pool);
        var freeze = state.Freeze;
        if (freeze is null || freeze.Fingerprint != fingerprint)
        {
            var seed = Fairness.NewSeed(c.Host.Clock.NowMillis(), c.Host.Context.MessageId, state.Round, _seedFill);
            freeze = new FreezeInfo(fingerprint, Fairness.Commitment(seed, fingerprint), Sha256.Hex(seed), Fairness.TotalTickets(pool), pool.Count, 0, freeze?.Won ?? []);
        }

        SaveState(c, state with { Open = false, Freeze = freeze });
        c.Host.Log.Info("tgw.closed", new LogFields().With("entrants", freeze.Entrants).With("tickets", freeze.Total).Build());
        PushOverlay(c, "closed", w =>
        {
            w.WriteString("prize", PrizeText(state));
            w.WriteNumber("tickets", freeze.Total);
            w.WriteNumber("entrants", freeze.Entrants);
            w.WriteString("commitment", freeze.Commitment);
            w.WriteString("fingerprint", freeze.Fingerprint);
        }, "The giveaway is closed. The draw is committed.", null);
        return Say(c, $"The giveaway is closed with {freeze.Entrants} eligible entrants and {Num(freeze.Total)} tickets. Commitment: {freeze.Commitment} (pool fp {freeze.Fingerprint}). The seed is revealed at the draw.");
    }

    private PlatformEventInfo? Draw(Ctx c)
    {
        var state = LoadState(c);
        var key = ReplayGuard.KeyFor("tgw.draw", c.Event, c.Host, c.Actor, "draw");
        var recorded = c.Store.GetJson(key, GiveawayJsonContext.Default.DrawRecord);
        if (recorded is not null && recorded.Actor == c.Actor && recorded.Command == "draw")
        {
            return Announce(c, state, recorded);
        }

        if (state.Freeze is null)
        {
            return Say(c, "Close the giveaway first (!tgw close) so the pool and commitment are published before the draw.");
        }

        var full = EligiblePool(c, state, withoutDrawn: false);
        if (full.Count == 0 || Fairness.Fingerprint(state.Round, full) != state.Freeze.Fingerprint)
        {
            return Say(c, "The pool changed since it was closed (entries, exclusions or expiries); run !tgw close again to publish a new commitment.");
        }

        var pool = EligiblePool(c, state, withoutDrawn: true);
        if (pool.Count == 0)
        {
            return Say(c, "No eligible entries for the giveaway draw.");
        }

        var seed = Sha256.FromHex(state.Freeze.Seed) ?? throw new InvalidOperationException("stored giveaway seed is not valid hex");
        var drawNumber = state.Freeze.Draws + 1;
        var result = Fairness.Draw(seed, state.Freeze.Fingerprint, (uint)drawNumber, pool);
        var audit = new AuditRecord(state.Round, drawNumber, state.Freeze.Fingerprint, state.Freeze.Commitment, state.Freeze.Seed,
            result.Total, (long)result.TicketIndex, result.RangeStart, result.RangeEnd, result.Actor, pool.Count);
        var record = new DrawRecord(c.Actor, "draw", PrizeText(state), state.Tier, audit);
        c.Store.SetJson(key, record, GiveawayJsonContext.Default.DrawRecord, OutcomeTtlSeconds);
        return Announce(c, state, record);
    }

    /// <summary>Applies a recorded draw to the state (idempotently), publishes the overlay message and builds the announcement.</summary>
    private PlatformEventInfo? Announce(Ctx c, GiveawayState state, DrawRecord record)
    {
        var audit = record.Audit;
        var freeze = state.Freeze;
        if (freeze is not null && !freeze.Won.Contains(audit.Winner))
        {
            SaveState(c, state with { Freeze = freeze with { Draws = Math.Max(freeze.Draws, audit.Draw), Won = [.. freeze.Won, audit.Winner] } });
        }

        var log = LoadWinners(c);
        if (!log.Items.Any(w => w.Actor == audit.Winner && w.Round == audit.Round))
        {
            var items = log.Items.Append(new WinnerItem(audit.Winner, audit.Round, record.Tier)).TakeLast(MaxWinnerLog).ToList();
            c.Store.SetJson(StateKey.Of("tgw", "win"), new WinnerLog(items), GiveawayJsonContext.Default.WinnerLog, StateTtlSeconds);
        }

        c.Store.SetJson(StateKey.Of("tgw", "audit"), audit, GiveawayJsonContext.Default.AuditRecord, StateTtlSeconds);
        var held = audit.RangeEnd - audit.RangeStart + 1;
        var chance = (double)held / audit.Total * 100.0;
        var chanceText = chance.ToString("0.00", CultureInfo.InvariantCulture);
        c.Host.Log.Info("tgw.drawn", new LogFields().With("draw", audit.Draw).With("eligible", audit.Eligible).With("tickets", audit.Total).Build());
        PushOverlay(c, "winner", w =>
        {
            w.WriteString("prize", record.Prize);
            w.WriteString("chance", chanceText);
            w.WriteNumber("tickets_held", held);
            w.WriteNumber("total", audit.Total);
            w.WriteString("fingerprint", audit.Fingerprint);
            w.WriteString("commitment", audit.Commitment);
            w.WriteNumber("round", audit.Round);
            w.WriteNumber("draw", audit.Draw);
        }, $"Winner of the {record.Prize}!", audit.Winner);
        return Say(c, $"{UserPlaceholder.For(audit.Winner)} won the {record.Prize} with a {chanceText}% chance! {AuditText(audit)}");
    }

    private PlatformEventInfo? Reset(Ctx c)
    {
        var state = LoadState(c);
        var claim = ReplayGuard.KeyFor("tgw.rst", c.Event, c.Host, c.Actor, "reset");
        if (c.Store.TryClaim(claim, OutcomeTtlSeconds))
        {
            SaveState(c, state with { Round = state.Round + 1, Open = true, Freeze = null });
            state = LoadState(c);
        }

        PushOverlay(c, "open", w => { w.WriteString("prize", PrizeText(state)); w.WriteNumber("round", state.Round); }, "A new giveaway round is open!", null);
        return Say(c, $"Giveaway reset: round {Num(state.Round)} is open with a fresh pool. Prize: {PrizeText(state)}");
    }

    private PlatformEventInfo? Prize(Ctx c, List<string> args)
    {
        var text = string.Join(' ', args).Trim();
        if (text.Length == 0 || text.Length > MaxPrizeLength)
        {
            return Say(c, $"Usage: !tgw prize <text> (1-{MaxPrizeLength} characters)");
        }

        SaveState(c, LoadState(c) with { Prize = text });
        return Say(c, $"The giveaway prize is now: {text}");
    }

    private PlatformEventInfo? Tier(Ctx c, List<string> args)
    {
        if (args.Count != 1 || !int.TryParse(args[0], NumberStyles.None, CultureInfo.InvariantCulture, out var tier) || tier is < 1 or > 3)
        {
            return Say(c, "Usage: !tgw tier <1-3> (the winner sits out that many following rounds)");
        }

        SaveState(c, LoadState(c) with { Tier = tier });
        return Say(c, $"Prize tier {tier}: winners sit out the next {tier} round(s).");
    }

    private static string? ResolveTarget(Ctx c, string token)
    {
        try
        {
            return c.Host.Identity.ResolveMention(token);
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotFound or IdentityErrorKind.Ambiguous or IdentityErrorKind.Invalid or IdentityErrorKind.NotAMember or IdentityErrorKind.NotLinked)
        {
            c.Host.Log.Info("tgw.refused", new LogFields().With("reason", "target_" + ex.Kind.ToString().ToLowerInvariant()).Build());
            return null;
        }
    }

    private PlatformEventInfo? Exclude(Ctx c, List<string> args)
    {
        long hours = 0;
        if (args.Count is < 1 or > 2 || (args.Count == 2 && (!long.TryParse(args[1], NumberStyles.None, CultureInfo.InvariantCulture, out hours) || hours is < 1 or > 8760)))
        {
            return Say(c, "Usage: !tgw exclude <user> [hours 1-8760] (no hours = permanent)");
        }

        var target = ResolveTarget(c, args[0]);
        if (target is null)
        {
            return Say(c, "Could not find that viewer.");
        }

        var list = LoadExclusions(c);
        var items = list.Items.Where(i => i.Actor != target).ToList();
        if (items.Count >= MaxExclusions)
        {
            return Say(c, "The exclusion list is full.");
        }

        items.Add(new ExclusionItem(target, hours == 0 ? 0 : c.Now + hours * 3_600_000));
        c.Store.SetJson(StateKey.Of("tgw", "excl"), new ExclusionList(items), GiveawayJsonContext.Default.ExclusionList, StateTtlSeconds);
        return Say(c, $"{UserPlaceholder.For(target)} is excluded from giveaways {(hours == 0 ? "permanently" : $"for {hours} hour(s)")}.");
    }

    private PlatformEventInfo? Unexclude(Ctx c, List<string> args)
    {
        if (args.Count != 1)
        {
            return Say(c, "Usage: !tgw unexclude <user>");
        }

        var target = ResolveTarget(c, args[0]);
        if (target is null)
        {
            return Say(c, "Could not find that viewer.");
        }

        var list = LoadExclusions(c);
        var items = list.Items.Where(i => i.Actor != target).ToList();
        if (items.Count == list.Items.Count)
        {
            return Say(c, $"{UserPlaceholder.For(target)} was not excluded.");
        }

        c.Store.SetJson(StateKey.Of("tgw", "excl"), new ExclusionList(items), GiveawayJsonContext.Default.ExclusionList, StateTtlSeconds);
        return Say(c, $"{UserPlaceholder.For(target)} can enter giveaways again.");
    }

    private PlatformEventInfo? FairnessReport(Ctx c, List<string> args)
    {
        var iterations = DefaultFairnessIterations;
        if (args.Count > 1 || (args.Count == 1 && !int.TryParse(args[0], NumberStyles.None, CultureInfo.InvariantCulture, out iterations)))
        {
            return Say(c, "Usage: !tgw fairness [iterations]");
        }

        iterations = Math.Clamp(iterations, MinFairnessIterations, MaxFairnessIterations);
        var pool = EligiblePool(c, LoadState(c), withoutDrawn: false);
        if (pool.Count == 0)
        {
            return Say(c, "No eligible entries to report on.");
        }

        var fingerprint = Fairness.Fingerprint(LoadState(c).Round, pool);
        var seed = BitConverter.ToUInt64(Sha256.Hash(fingerprint), 0);
        var summary = Fairness.MonteCarlo(pool, iterations, seed);
        return Say(c, $"Fairness report: {summary.Iterations} simulated draws over {summary.Entrants} entrants / {Num(summary.TotalTickets)} tickets, max |expected - observed| = {summary.MaxAbsDeltaPercent.ToString("0.000", CultureInfo.InvariantCulture)}% (deterministic seed from pool fp {fingerprint[..16]}).");
    }

    private PlatformEventInfo? OverlayConfig(Ctx c, List<string> args)
    {
        var mode = args.Count == 1 ? args[0].ToLowerInvariant() : string.Empty;
        switch (mode)
        {
            case "register":
                GiveawayOverlay.Register(c.Host.Overlay, WidgetId);
                return Say(c, "Giveaway overlay widget registered.");
            case "on" or "off":
                c.Store.SetJson(StateKey.Of("tgw", "cfg"), new GiveawayConfig(mode == "on"), GiveawayJsonContext.Default.GiveawayConfig, StateTtlSeconds);
                return Say(c, $"Giveaway overlay messages are {mode}.");
            default:
                return Say(c, "Usage: !tgw overlay on|off|register");
        }
    }

    /// <summary>Pushes an overlay message when moderators enabled overlays; a refused push throws (never swallowed).</summary>
    private void PushOverlay(Ctx c, string suffix, Action<System.Text.Json.Utf8JsonWriter> amount, string message, string? user)
    {
        if (LoadConfig(c).Overlay)
        {
            GiveawayOverlay.Push(c.Host.Overlay, WidgetId, GiveawayOverlay.GiveawayPrefix + suffix, amount, message, user);
        }
    }
}

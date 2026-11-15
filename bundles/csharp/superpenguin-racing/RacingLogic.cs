using System.Globalization;
using System.Text;
using SuperPenguin.DropGameCore;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Scheduling;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRacing;

/// <summary>
/// `!race amount|all|max` (aliases `!racing`, `!kart`, `!grandprix`), ported from PenguinTwitchBot's Racing game
/// (`Commands/Games/Racing/RacingGameService.cs`, `RacingMessageSettings.cs`, sub-actions RacingStartSession/Join/StopSession, overlay
/// `racing.html` + `js/racing.js`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`).
/// The first joiner opens a join window (<see cref="RaceConfig.LobbySeconds"/>); later joiners line up on the grid; when the window
/// closes the race is simulated deterministically from a seed recorded with the race and the pot is split by place.
///
/// <para><b>Money (settlement after a delay).</b> Each join is ONE actor-bound `economy.wager(stake, 0)` at join time (the stake is
/// debited; a wager with payout 0 is how the host ledger books a stake that is paid out later). The pot is paid by the settlement
/// via the economy service's `adjust_balance` credit with deterministic idempotency keys (<see cref="RacingSettlement"/>); the total
/// credited always equals the pot (nothing is minted). The deadline is a <see cref="TickScheduler"/> task, which is tick-on-event:
/// the window is only noticed when the NEXT racing command reaches the bundle, so a window nobody touches again is settled late
/// (and voided + refunded when later than <see cref="RaceConfig.StaleVoidSeconds"/>). Settlement can only run in the process stage
/// (economy calls are process-only), never from a timer or the action stage.</para>
///
/// <para><b>Idempotency.</b> A join writes its replay record (race id, stake, race seed) BEFORE its wager; a redelivery finishes the
/// same join (the host de-duplicates the wager, the roster slot is claimed once). One settlement per racer exactly once: result frozen
/// first, per-racer progress saved, deterministic credit keys.</para>
///
/// <para><b>Deviations.</b> Upstream is free play and the browser overlay is authoritative for placements; here the race is simulated
/// server-side from the recorded seed (replayable/auditable) and stakes fund the pot. Not ported (no host surface): moderator
/// commands (`!race start|launch|stop`), the Start/Join/Stop sub-actions, solo `!timetrial`, derby/elimination/tournament modes, emote or
/// theme arguments and per-racer participation points. Flag <see cref="FlagKey"/> default OFF, checked after the command match.</para>
/// </summary>
public sealed class RacingLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-racing";
    internal const ulong MaxBet = 500;
    internal const uint CooldownSeconds = 5;
    internal const uint ReplayRecordTtlSeconds = 3600;
    internal const string Usage = "To race do !race Amount/All/Max replacing amount with how many you would like to risk. The first racer opens a join window and the pot is split between the top finishers.";

    private readonly Func<ulong> _entropy;
    private readonly RaceConfig _config;

    /// <summary>Creates the game; tests inject the entropy source and a config.</summary>
    public RacingLogic(Func<ulong>? entropy = null, RaceConfig? config = null)
    {
        _entropy = entropy ?? (() => SeedDerivation.NewEntropy());
        _config = (config ?? RaceConfig.Default).Clamped();
    }

    /// <inheritdoc />
    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, DropSupport.CommandPrefix);
        if (command is null || !command.IsAny("race", "racing", "kart", "grandprix"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("racing.refused", new LogFields().With("reason", "no_community").Build());
            return DropSupport.Reply(@event, command, "Racing needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("racing.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return DropSupport.Reply(@event, command, "Link your account to this community before racing.");
        }

        var store = new KvStateStore(host.Kv);
        var scheduler = new TickScheduler(store, host.Clock, RacingSettlement.Board);
        var key = DropReplayGuard.KeyFor(RacingOverlay.Game, @event, host, actor, command.Name);
        var notices = new RacingSettlement(host, store, scheduler, _config, actor).Tick();

        var recorded = DropReplayGuard.TryReplay(store, key, actor, command.Name, StateKey.Hash(command.RawText));
        string? reply;
        if (recorded is not null)
        {
            reply = Replay(recorded, host, store, key, actor, command, @event.Platform);
        }
        else
        {
            reply = Fresh(@event, command, host, store, scheduler, key, actor);
        }

        List<string> parts = reply is null ? notices : [.. notices, reply];
        return parts.Count == 0 ? null : DropSupport.Reply(@event, command, string.Join(' ', parts));
    }

    /// <summary>Finishes a redelivered join from its replay record (same race, same stake, same wager, same reply).</summary>
    private string Replay(DropReplayRecord recorded, IWaddleHostNext host, KvStateStore store, string key, string actor, ChatCommand command, string platform)
    {
        if (recorded.Config != RaceConfig.Id)
        {
            throw new InvalidOperationException($"join record was made under game config '{recorded.Config}' but this build plays '{RaceConfig.Id}'; refusing to settle a different result");
        }

        var raceId = recorded.Extra ?? throw new InvalidOperationException("join replay record carries no race id");
        var race = RacingStore.Load(store, raceId) ?? throw new InvalidOperationException("join replay record names a race that no longer exists");
        var opener = race.Id == StateKey.Hash($"{host.Context.MessageId}|{platform}|{actor}|race");
        return Enroll(host, store, null, race, recorded, key, actor, command, opener, replay: true);
    }

    /// <summary>A fresh `!race`: status/usage, stake validation, cooldown, open-or-join, then the enrolment.</summary>
    private string? Fresh(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, TickScheduler scheduler, string key, string actor)
    {
        if (command.Args.Count == 0)
        {
            return StatusText(host, store);
        }

        if (command.Args.Count > 1)
        {
            return Usage;
        }

        var held = EconomyServiceClient.For(host).Balance(actor);
        var parsed = StakeParser.Parse(command.Args[0], held, MaxBet);
        if (!parsed.Ok)
        {
            return parsed.Refusal;
        }

        if (!StateCooldown.TryAcquire(store, command.Name, actor, CooldownSeconds))
        {
            return null;
        }

        var name = UserPlaceholder.For(actor);
        var now = host.Clock.NowMillis();
        var current = RacingStore.Current(store);
        if (current is not null && (current.Status != RaceStatus.Open || now >= current.ClosesAt))
        {
            StateCooldown.Release(store, command.Name, actor);
            return $"{name}, the last race is still being settled; try again in a moment.";
        }

        var opener = current is null;
        var race = current ?? OpenRace(@event.Platform, host, actor, now);
        if (!opener)
        {
            if (store.Get(RacingStore.MemberKey(race.Id, actor)) is not null)
            {
                StateCooldown.Release(store, command.Name, actor);
                return $"{name}, you are already registered on the starting grid!";
            }

            if (host.Kv.Increment(RacingStore.CountKey(race.Id), 0, RacingStore.Ttl) >= race.MaxRacers)
            {
                StateCooldown.Release(store, command.Name, actor);
                return $"{name}, the starting grid is full for this race ({race.MaxRacers} max racers)!";
            }
        }

        if (!store.TryClaim(RacingStore.MemberKey(race.Id, actor), RacingStore.Ttl))
        {
            StateCooldown.Release(store, command.Name, actor);
            return $"{name}, you are already registered on the starting grid!";
        }

        if (opener)
        {
            RacingStore.Save(store, race);
            RacingStore.SetCurrent(store, race.Id);
            scheduler.Schedule(new ScheduledTask(race.Id, race.ClosesAt, RacingSettlement.CloseKind, string.Empty));
        }

        var record = new DropReplayRecord(actor, command.Name, StateKey.Hash(command.RawText), parsed.Stake, race.Seed, RaceConfig.Id, race.Id);
        store.SetJson(key, record, DropCoreJsonContext.Default.DropReplayRecord, ReplayRecordTtlSeconds);
        return Enroll(host, store, scheduler, race, record, key, actor, command, opener, replay: false);
    }

    /// <summary>
    /// Opens a new race: the id binds the opening invocation, and ONE host-entropy value is recorded with the race (the seed derives
    /// from race id + entropy), all before the first stake is taken.
    /// </summary>
    private RaceRecord OpenRace(string platform, IWaddleHostNext host, string actor, ulong now)
    {
        var id = StateKey.Hash($"{host.Context.MessageId}|{platform}|{actor}|race");
        var entropy = _entropy();
        var seed = SeedDerivation.Derive(id, platform, "race", "race", entropy);
        return new RaceRecord(id, now, now + (_config.LobbySeconds * 1000UL), host.Context.MessageId, platform,
            SeedDerivation.ToHex(seed), SeedDerivation.ToHex(entropy), RaceConfig.Id, _config.Laps, _config.MaxRacers, RaceStatus.Open);
    }

    /// <summary>
    /// Takes the stake (one `wager(stake, 0)`), seats the racer and pushes the lobby frame. Refusals the user can act on
    /// (insufficient funds, over the bet cap) undo the join and answer in chat; a racer who cannot be seated (window closed or frozen,
    /// grid full) is refunded; everything else propagates.
    /// </summary>
    private string Enroll(IWaddleHostNext host, KvStateStore store, TickScheduler? scheduler, RaceRecord race, DropReplayRecord record, string key, string actor, ChatCommand command, bool opener, bool replay)
    {
        var name = UserPlaceholder.For(actor);
        try
        {
            host.Economy.Wager(actor, record.Stake, 0);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            store.Delete(key);
            store.Delete(RacingStore.MemberKey(race.Id, actor));
            StateCooldown.Release(store, command.Name, actor);
            if (opener && scheduler is not null && host.Kv.Increment(RacingStore.CountKey(race.Id), 0, RacingStore.Ttl) == 0)
            {
                scheduler.Cancel(race.Id);
                RacingStore.ClearCurrent(store, race.Id);
                store.Delete(RacingStore.RecordKey(race.Id));
            }

            host.Log.Info("racing.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            var refusedAlias = DropSupport.Alias(host, actor, RacingOverlay.Game);
            return ex.Kind == EconomyErrorKind.OverCap
                ? $"{name}, that bet is over the limit for {refusedAlias}."
                : $"{name}, you don't have {record.Stake} {refusedAlias} to bet.";
        }

        var lane = Seat(host, store, race, actor, record.Stake);
        var alias = DropSupport.Alias(host, actor, RacingOverlay.Game);
        if (lane <= 0)
        {
            Refund(host, store, race, actor, record.Stake);
            host.Log.Info("racing.refused", new LogFields().With("reason", lane == 0 ? "window_closed" : "grid_full").With("race", race.Id).Build());
            return lane == 0
                ? $"{name}, the race closed before you could line up; your {record.Stake} {alias} were refunded."
                : $"{name}, the starting grid is full for this race ({race.MaxRacers} max racers); your {record.Stake} {alias} were refunded.";
        }

        var racers = (int)host.Kv.Increment(RacingStore.CountKey(race.Id), 0, RacingStore.Ttl);
        host.Log.Info("racing.joined", new LogFields()
            .With("race", race.Id)
            .With("lane", lane)
            .With("stake", (long)record.Stake)
            .With("opener", opener)
            .With("replay", replay)
            .Build());
        OverlayPublisher.Publish(host, store, RacingOverlay.Game, RacingOverlay.Widget,
            OverlayEnvelope.Build(RacingOverlay.Game, "join", race.Seed, actor, record.Stake, 0,
                RacingOverlay.JoinData(race.Id, race.ClosesAt, lane, racers, race.MaxRacers, race.Laps)));

        var now = host.Clock.NowMillis();
        var seconds = race.ClosesAt > now ? (race.ClosesAt - now + 999) / 1000 : 0;
        return opener
            ? $"{name} opened a race with {record.Stake} {alias} on the line! Type !race Amount to join, the green flag drops in {seconds}s."
            : $"{name} lined up on the starting grid in position #{lane} with {record.Stake} {alias} on the line! {seconds}s until the green flag.";
    }

    /// <summary>
    /// Seats the racer: the 1-based grid position, 0 when the window is closed or already frozen without them, -1 when the grid is
    /// full. Slots are claimed with the atomic kv counter; a racer already seated (redelivered join) keeps their lane.
    /// </summary>
    private static int Seat(IWaddleHostNext host, KvStateStore store, RaceRecord race, string actor, ulong stake)
    {
        var laneKey = RacingStore.LaneKey(race.Id, actor);
        var existing = store.Get(laneKey);
        if (existing is not null)
        {
            return int.Parse(Encoding.UTF8.GetString(existing), CultureInfo.InvariantCulture);
        }

        var fresh = RacingStore.Load(store, race.Id);
        if (fresh is null || fresh.Status != RaceStatus.Open)
        {
            return 0;
        }

        var position = (int)host.Kv.Increment(RacingStore.CountKey(race.Id), 1, RacingStore.Ttl);
        if (position > race.MaxRacers)
        {
            return -1;
        }

        store.SetJson(RacingStore.SlotKey(race.Id, position), new RosterEntry(actor, stake), RacingJsonContext.Default.RosterEntry, RacingStore.Ttl);
        store.Set(laneKey, Encoding.UTF8.GetBytes(position.ToString(CultureInfo.InvariantCulture)), RacingStore.Ttl);

        // A settlement that froze its result between our status check and our slot write cannot include us: undo and refund.
        var frozen = store.GetJson(RacingStore.ResultKey(race.Id), RacingJsonContext.Default.RaceResultRecord);
        if (frozen is not null && frozen.Racers.All(r => r.Actor != actor))
        {
            store.Delete(laneKey);
            return 0;
        }

        return position;
    }

    /// <summary>Gives back the stake of a racer who could not be seated; once per racer per race (marker), deterministic idempotency key.</summary>
    private static void Refund(IWaddleHostNext host, KvStateStore store, RaceRecord race, string actor, ulong stake)
    {
        var marker = RacingStore.RefundedKey(race.Id, actor);
        if (store.Get(marker) is not null)
        {
            return;
        }

        new EconomyServiceClient(host.Tool, race.OpenMessageId, "race.late-refund").Credit(actor, stake);
        store.Set(marker, [1], RacingStore.Ttl);
    }

    /// <summary>The reply to a bare `!race`: the open window's state, or how to start one.</summary>
    private static string StatusText(IWaddleHostNext host, KvStateStore store)
    {
        var current = RacingStore.Current(store);
        if (current is null || current.Status != RaceStatus.Open)
        {
            return Usage;
        }

        var racers = host.Kv.Increment(RacingStore.CountKey(current.Id), 0, RacingStore.Ttl);
        var now = host.Clock.NowMillis();
        var seconds = current.ClosesAt > now ? (current.ClosesAt - now + 999) / 1000 : 0;
        return $"A race is open with {racers} racer(s), the green flag drops in {seconds}s. Join with !race Amount/All/Max.";
    }
}

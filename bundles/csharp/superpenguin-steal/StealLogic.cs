using System.Globalization;
using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinSteal;

/// <summary>
/// `!steal TARGET` (plus `!steal optout|optin`), ported from PenguinTwitchBot's `PastyGames/Steal.cs` and `StealGameOptions.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the original:
/// a steal moves a random amount between 100 and 10,000 with a 33.33% success chance; the thief must hold at least the maximum;
/// a target holding less than the maximum is "too poor" and the thief GIVES them the amount instead; a failed steal makes the
/// thief pay the target the same amount; a 300 s per-thief cooldown applies once an attempt is made; the chat wording is kept.
///
/// <para><b>All safe-wager logic lives HERE (user decision); the economy service is only I/O.</b> The app decides odds, the amount,
/// the direction, every cap and every refusal, then asks the economy's `transfer_from` tool (from, to, amount, reason, plus the
/// idempotency key) to move exactly that. Safety rules added to the original: the per-thief 300 s cooldown; a per-thief daily limit
/// (<see cref="DailyThiefLimit"/> at risk per UTC day) and a per-victim daily loss limit (<see cref="DailyVictimLimit"/>);
/// a 120 s immunity after a successful theft; targets who opted out (`!steal optout`, which also stops them stealing), targets
/// on the configured protected list (<c>protected_users</c> in the app config, e.g. moderators), and low-balance targets are never
/// robbed; self-targeting is refused. A thief never loses money without the victim receiving it in the same atomic transfer.</para>
///
/// <para><b>Dangerous grant, fails loud.</b> `transfer_from` needs the admin-approved dangerous tool grant. Until it exists the
/// host answers not-granted and EVERY money-moving path replies "steal not enabled" and moves nothing (cooldown released) -- there
/// is no private ledger, no pretend result.</para>
///
/// <para><b>Exactly-once money.</b> The roll, amount and direction are recorded under a replay key
/// (message id + platform + actor UUID + command, <see cref="IdempotentOutcome"/>) BEFORE the single transfer call; a redelivery
/// re-issues the same transfer with the same key and the host de-duplicates it. The record is bound to actor, command and raw
/// arguments. A refused transfer discards the record and releases the cooldown. Daily counters are committed once per message
/// after a successful transfer (a crash between the two under-counts, in the thief's favour, never over-moves money; the counters
/// use kv read-modify-write, so racing thefts against ONE victim can under-count by one attempt).</para>
///
/// <para><b>Deviations (documented):</b> the original's "stream must be online" check needs a capability no world offers and is
/// skipped; the original's `MovePoints` removed points from both users (an upstream bug) and is replaced by a true transfer; the host
/// does not forward per-community settings yet, so the defaults apply (the protected list is therefore empty until it does; mods
/// can opt out). Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).</para>
/// </summary>
public sealed class StealLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const ulong MinSteal = 100;
    internal const ulong MaxSteal = 10_000;
    internal const int SuccessBasisPoints = 3333;
    internal const uint CooldownSeconds = 300;
    internal const uint VictimImmunitySeconds = 120;
    internal const ulong DailyThiefLimit = 30_000;
    internal const ulong DailyVictimLimit = 10_000;
    internal const ulong DayMillis = 86_400_000;
    internal const uint CounterTtlSeconds = 2 * 24 * 60 * 60;
    internal const string FlagKey = "waddles.command-superpenguin-steal";
    internal const string NotEnabled = "steal not enabled";
    internal const string Usage = "to steal from someone the command is !steal TARGETNAME";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("steal"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("steal.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "Stealing needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("steal.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before stealing.");
        }

        var store = new KvStateStore(host.Kv);
        var text = Handle(command, @event, host, store, actor);
        return text.Length == 0 ? null : Reply(@event, command, text);
    }

    private string Handle(ChatCommand command, PlatformEventInfo @event, IWaddleHostNext host, KvStateStore store, string actor)
    {
        var first = command.Args.Count > 0 ? command.Args[0].ToLowerInvariant() : string.Empty;
        if (first == "optout")
        {
            store.Set(OptOutKey(actor), [1], 0);
            return "You opted out of stealing: nobody can steal from you and you cannot steal. Use !steal optin to rejoin.";
        }

        if (first == "optin")
        {
            store.Delete(OptOutKey(actor));
            return "You opted back in to stealing.";
        }

        var outcomes = new IdempotentOutcome(store, "steal", 3600);
        var messageId = host.Context.MessageId;
        var argsHash = StateKey.Hash(command.RawText);
        var recorded = outcomes.TryReplay(messageId, @event.Platform, actor, command.Name, StealJsonContext.Default.StealRecord,
            r => r.Actor == actor && r.Command == command.Name && r.ArgsHash == argsHash);
        if (recorded is not null)
        {
            var replay = Transfer(recorded, host, store, outcomes, messageId, @event.Platform, actor, command.Name);
            host.Log.Info("steal.replayed", new LogFields().Build());
            return replay;
        }

        if (command.Args.Count == 0)
        {
            return Usage;
        }

        string victim;
        try
        {
            victim = host.Identity.ResolveMention(command.Args[0]);
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotFound or IdentityErrorKind.Ambiguous or IdentityErrorKind.Invalid or IdentityErrorKind.NotAMember or IdentityErrorKind.NotLinked)
        {
            host.Log.Info("steal.refused", new LogFields().With("reason", "target_" + ex.Kind.ToString().ToLowerInvariant()).Build());
            return "Could not find that viewer";
        }

        if (victim == actor)
        {
            return Usage;
        }

        var thiefTag = UserPlaceholder.For(actor);
        var victimTag = UserPlaceholder.For(victim);
        if (store.Get(OptOutKey(actor)) is not null)
        {
            return $"{thiefTag}, you opted out of stealing; use !steal optin to play.";
        }

        if (store.Get(OptOutKey(victim)) is not null || IsProtected(host, victim))
        {
            return $"{victimTag} cannot be stolen from.";
        }

        var alias = Alias(host, actor);
        var economy = EconomyServiceClient.For(host);
        if (economy.Balance(actor) < (long)MaxSteal)
        {
            return $"{thiefTag}, you don't have enough {alias} to steal, you need a minimum of {MaxSteal}";
        }

        var now = host.Clock.NowMillis();
        var day = now / DayMillis;
        var thiefUsed = ReadCounter(store, ThiefDayKey(day, actor));
        var victimUsed = ReadCounter(store, VictimDayKey(day, victim));
        if (thiefUsed >= DailyThiefLimit)
        {
            return $"{thiefTag}, you reached your daily stealing limit; try again tomorrow.";
        }

        if (victimUsed >= DailyVictimLimit || store.Get(ImmunityKey(victim)) is not null)
        {
            return $"{victimTag} has been robbed enough for now.";
        }

        if (!StateCooldown.TryAcquire(store, "steal", actor, CooldownSeconds))
        {
            return string.Empty; // original: a steal inside the cooldown is dropped silently
        }

        var amount = (ulong)_nextInt((int)MinSteal, (int)MaxSteal);
        var poor = economy.Balance(victim) < (long)MaxSteal;
        var success = !poor && _nextInt(0, 9999) < SuccessBasisPoints;
        if (success)
        {
            amount = Math.Min(amount, DailyVictimLimit - victimUsed);
        }

        amount = Math.Min(amount, DailyThiefLimit - thiefUsed);
        var kind = poor ? "poor" : success ? "success" : "failed";
        var amt = amount.ToString("N0", CultureInfo.InvariantCulture);
        var (from, to, reply) = kind switch
        {
            "success" => (victim, actor, $"{thiefTag} successfully stole {amount} {alias} from {victimTag}"),
            "failed" => (actor, victim, $"{thiefTag} failed to steal {amount} {alias} from {victimTag}, {victimTag} gets {amount} {alias} from {thiefTag} instead"),
            _ => (actor, victim, $"{victimTag} is too poor for you to steal from them, instead you give them {amt} {alias}."),
        };

        var record = new StealRecord(actor, command.Name, argsHash, kind, from, to, amount, actor, victim, reply);
        outcomes.Record(messageId, @event.Platform, actor, command.Name, record, StealJsonContext.Default.StealRecord);
        return Transfer(record, host, store, outcomes, messageId, @event.Platform, actor, command.Name);
    }

    /// <summary>
    /// Issues the record's single `transfer_from` and commits the daily counters. Not granted -> "steal not enabled"; an
    /// insufficient-funds / over-cap / non-member refusal -> a refusal reply; both discard the record and release the cooldown.
    /// Anything else propagates so the redelivered event retries the identical transfer.
    /// </summary>
    private static string Transfer(StealRecord record, IWaddleHostNext host, KvStateStore store, IdempotentOutcome outcomes, string messageId, string platform, string actor, string command)
    {
        try
        {
            EconomyServiceClient.For(host, "steal").TransferFrom(record.From, record.To, record.Amount, "steal." + record.Kind);
        }
        catch (WaddleEconomyException ex) when (ex.Kind == EconomyErrorKind.Denied && ex.Message == "not_granted")
        {
            outcomes.Discard(messageId, platform, actor, command);
            StateCooldown.Release(store, "steal", actor);
            host.Log.Warn("steal.not_enabled", new LogFields().With("reason", "transfer_from_not_granted").Build());
            return NotEnabled;
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap or EconomyErrorKind.NotAMember)
        {
            outcomes.Discard(messageId, platform, actor, command);
            StateCooldown.Release(store, "steal", actor);
            host.Log.Info("steal.refused", new LogFields().With("reason", ex.Kind.ToString().ToLowerInvariant()).Build());
            return ex.Kind switch
            {
                EconomyErrorKind.InsufficientFunds => "The steal could not be paid: someone's balance changed. Try again.",
                EconomyErrorKind.OverCap => "The steal was over the economy's limits and was refused.",
                _ => "Both players must be members of this community.",
            };
        }

        Commit(record, host, store, messageId);
        host.Log.Info("steal.settled", new LogFields().With("kind", record.Kind).Build());
        return record.Reply;
    }

    /// <summary>Counts the attempt against the daily limits exactly once per message (a replay finds the marker and skips).</summary>
    private static void Commit(StealRecord record, IWaddleHostNext host, KvStateStore store, string messageId)
    {
        var marker = StateKey.Of("steal.counted", StateKey.Hash(messageId + "|" + record.Actor));
        if (!store.TryClaim(marker, CounterTtlSeconds))
        {
            return;
        }

        var day = host.Clock.NowMillis() / DayMillis;
        AddCounter(store, ThiefDayKey(day, record.Thief), record.Amount);
        if (record.Kind == "success")
        {
            AddCounter(store, VictimDayKey(day, record.Victim), record.Amount);
            store.Set(ImmunityKey(record.Victim), [1], VictimImmunitySeconds);
        }
    }

    private static bool IsProtected(IWaddleHostNext host, string victim)
    {
        try
        {
            var config = System.Text.Json.JsonSerializer.Deserialize(host.Context.ConfigJson, StealJsonContext.Default.StealConfig);
            return config?.ProtectedUsers?.Contains(victim) == true;
        }
        catch (System.Text.Json.JsonException)
        {
            host.Log.Warn("steal.config_invalid", new LogFields().Build());
            return false;
        }
    }

    private static ulong ReadCounter(KvStateStore store, string key)
    {
        var bytes = store.Get(key);
        return bytes is not null && ulong.TryParse(Encoding.UTF8.GetString(bytes), NumberStyles.None, CultureInfo.InvariantCulture, out var v) ? v : 0;
    }

    private static void AddCounter(KvStateStore store, string key, ulong amount) =>
        store.Mutate(key, current =>
        {
            var held = current is not null && ulong.TryParse(Encoding.UTF8.GetString(current), NumberStyles.None, CultureInfo.InvariantCulture, out var v) ? v : 0;
            return Encoding.UTF8.GetBytes((held + amount).ToString(CultureInfo.InvariantCulture));
        }, CounterTtlSeconds);

    private static string OptOutKey(string user) => StateKey.Of("steal.optout", StateKey.Hash(user));

    internal static string ImmunityKey(string user) => StateKey.Of("steal.imm", StateKey.Hash(user));

    internal static string ThiefDayKey(ulong day, string user) => StateKey.Of("steal.day.t", day.ToString(CultureInfo.InvariantCulture), StateKey.Hash(user));

    internal static string VictimDayKey(ulong day, string user) => StateKey.Of("steal.day.v", day.ToString(CultureInfo.InvariantCulture), StateKey.Hash(user));

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
            host.Log.Warn("steal.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
}

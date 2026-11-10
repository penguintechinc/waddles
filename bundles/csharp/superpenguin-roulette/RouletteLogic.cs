using System.Globalization;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRoulette;

/// <summary>
/// `!roulette amount|all|max`, ported from PenguinTwitchBot's `TicketGames/Roulette.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the original:
/// a 1..100 roll that wins an even-money bet when it beats <see cref="MustBeat"/> (52) and loses the stake otherwise, a
/// per-bet limit of <see cref="MaxPerBet"/>, a per-user total limit of <see cref="MaxPerDay"/>, an over-limit bet clamped (not
/// refused) to what is left, and a 1200 s per-user cooldown that bad input does not consume. The original never times the loser
/// out, so no moderation capability is needed or requested.
///
/// <para><b>Deviations.</b> (1) The original resets its per-user total and its online-only gate on "stream started"; no
/// stream-online capability exists in any WIT world yet, so the total resets per UTC day and the online-only gate is dropped.
/// (2) One per-community currency replaces the per-game "point type"; the alias comes from the economy service's `points_view`.
/// (3) The player is named with the host `{user:uuid}` placeholder.</para>
///
/// <para><b>Money.</b> Exactly one atomic host `economy.wager(stake, payout)` per spin (payout = 2x stake on a win, else 0).
/// The roll is made once and recorded under the replay key BEFORE the wager and reused on redelivery. The record is bound to
/// the message id, platform, actor UUID, command and the raw arguments, and is only treated as a replay on an exact match
/// (<see cref="ReplayGuard"/>); a blank message id is refused with an exception.</para>
///
/// <para><b>Failure policy.</b> Refusals the user can act on get a chat reply and release the cooldown; economy
/// `denied`/`unavailable`/`backend` and kv failures propagate -- never an unpaid reply.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF), checked after the command match and before the cooldown.
/// </summary>
public sealed class RouletteLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const uint CooldownSeconds = 1200;
    internal const int MustBeat = 52;
    internal const ulong MaxPerBet = 500;
    internal const ulong MaxPerDay = 1000;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const uint DayTotalTtlSeconds = 2 * 24 * 60 * 60;
    internal const string FlagKey = "waddles.command-superpenguin-roulette";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("roulette"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("roulette.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "Roulette needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("roulette.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before playing roulette.");
        }

        var store = new KvStateStore(host.Kv);
        var outcomeKey = ReplayGuard.KeyFor("rl.out", @event, host, actor, command.Name);
        var argsHash = StateKey.Hash(command.RawText);
        var name = UserPlaceholder.For(actor);

        var recorded = store.GetJson(outcomeKey, RouletteJsonContext.Default.RouletteOutcome);
        var replay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name && recorded.ArgsHash == argsHash;
        RouletteOutcome outcome;
        if (replay)
        {
            outcome = recorded!;
        }
        else
        {
            var fresh = Fresh(command, host, store, actor, name, argsHash, out var refusal);
            if (fresh is null)
            {
                return refusal is null ? null : Reply(@event, command, refusal);
            }

            outcome = fresh;
            store.SetJson(outcomeKey, outcome, RouletteJsonContext.Default.RouletteOutcome, OutcomeTtlSeconds);
        }

        var payout = Payout(outcome);
        long balance;
        try
        {
            balance = host.Economy.Wager(actor, outcome.Stake, payout);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            store.Delete(outcomeKey);
            StateCooldown.Release(store, command.Name, actor);
            host.Log.Info("roulette.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            var refusedAlias = Alias(host, actor);
            return Reply(@event, command, ex.Kind == EconomyErrorKind.OverCap
                ? $"{name}, that bet is over the limit for {refusedAlias}."
                : $"{name}, you don't have {outcome.Stake} {refusedAlias} to bet.");
        }

        if (!replay)
        {
            store.Set(DayTotalKey(host, actor), System.Text.Encoding.UTF8.GetBytes(outcome.DayTotal.ToString(CultureInfo.InvariantCulture)), DayTotalTtlSeconds);
        }

        var alias = Alias(host, actor);
        host.Log.Info("roulette.settled", new LogFields()
            .With("won", payout > 0)
            .With("stake", (long)outcome.Stake)
            .With("replay", replay)
            .Build());
        return Reply(@event, command, BuildResultText(outcome, name, alias, balance));
    }

    /// <summary>Validates and clamps the bet and rolls once; returns null (with a refusal, or none for a silent cooldown drop).</summary>
    private RouletteOutcome? Fresh(ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string name, string argsHash, out string? refusal)
    {
        refusal = null;
        if (command.Args.Count != 1)
        {
            refusal = "To roulette please do !roulette Amount/All/Max replacing amount with how many you would like to risk.";
            return null;
        }

        var held = EconomyServiceClient.For(host).Balance(actor);
        ulong stake;
        var arg = command.Args[0];
        if (string.Equals(arg, "all", StringComparison.OrdinalIgnoreCase) || string.Equals(arg, "max", StringComparison.OrdinalIgnoreCase))
        {
            stake = held > 0 ? (ulong)held : 0;
        }
        else if (!long.TryParse(arg, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var parsed))
        {
            refusal = "The amount must be a number, max, or all";
            return null;
        }
        else if (parsed <= 0)
        {
            refusal = "The amount needs to be greater then 0";
            return null;
        }
        else
        {
            stake = (ulong)parsed;
        }

        if (stake == 0 || (long)Math.Min(stake, long.MaxValue) > held)
        {
            refusal = "You don't have that many.";
            return null;
        }

        var total = ReadDayTotal(store, host, actor);
        if (total >= MaxPerDay)
        {
            refusal = $"You have reached your max per day limit for !roulette ({MaxPerDay} {Alias(host, actor)}).";
            return null;
        }

        stake = Math.Min(stake, MaxPerBet);
        stake = Math.Min(stake, MaxPerDay - total);

        if (!StateCooldown.TryAcquire(store, command.Name, actor, CooldownSeconds))
        {
            // The original silently drops a command received during its own cooldown window. Match that.
            return null;
        }

        return new RouletteOutcome(actor, command.Name, argsHash, stake, _nextInt(1, 100), total + stake);
    }

    /// <summary>Payout: even money (twice the stake back) when the roll beats <see cref="MustBeat"/>, else 0.</summary>
    internal static ulong Payout(RouletteOutcome outcome) => outcome.Value > MustBeat ? outcome.Stake * 2 : 0;

    private static string DayKey(IWaddleHostNext host) =>
        DateTimeOffset.FromUnixTimeMilliseconds((long)host.Clock.NowMillis()).UtcDateTime.ToString("yyyyMMdd", CultureInfo.InvariantCulture);

    private static string DayTotalKey(IWaddleHostNext host, string actor) =>
        StateKey.Of("rl.tot", DayKey(host), StateKey.Hash(actor));

    private static ulong ReadDayTotal(KvStateStore store, IWaddleHostNext host, string actor)
    {
        var bytes = store.Get(DayTotalKey(host, actor));
        return bytes is not null && ulong.TryParse(System.Text.Encoding.UTF8.GetString(bytes), NumberStyles.None, CultureInfo.InvariantCulture, out var v) ? v : 0;
    }

    /// <summary>The community currency alias from the economy service; falls back to its documented default (logged) if the tool cannot answer.</summary>
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
            host.Log.Warn("roulette.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>Pure result rendering (the original's default win/lose templates), exposed for unit tests.</summary>
    internal static string BuildResultText(RouletteOutcome outcome, string displayName, string alias, long balance)
    {
        var verb = outcome.Value > MustBeat ? "won" : "lost";
        return $"{displayName} rolled a {outcome.Value} and {verb} {outcome.Stake} {alias} in the roulette and now has {balance} {alias}! "
            + $"Rouletted {outcome.DayTotal} of {MaxPerDay} limit per day";
    }
}

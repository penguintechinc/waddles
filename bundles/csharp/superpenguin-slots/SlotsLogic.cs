using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinSlots;

/// <summary>
/// `!slot [bet]` / `!slots [bet]` three-reel slot machine, ported from PenguinTwitchBot's `PastyGames/Slots.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the
/// original's two aliases (each with its own 600 s per-user cooldown), five default emotes, three independent uniformly random
/// reels, the 3-of-a-kind 3.5x / first-two 2.5x / last-two 1.5x stake multipliers, the 25 minimum bet, the bet grammar
/// (`amount`, `max`/`all`, `N%`) and the verbatim win/lose message pools. The original's `Tax`, `MaxBet` and `Percentage`
/// settings fold into <see cref="SlotsConfig"/> (minimum bet, multipliers, emotes, cooldown) and the host `economy.max-bet`.
///
/// <para><b>Deviations forced by the economy.</b> (1) The original paid a free fixed prize for a bare `!slots` (no bet); a
/// free prize needs a MINT (`economy.credit`) this bundle does not request, so a bare `!slots` stakes the minimum bet
/// instead. (2) Only the actor can be debited, through one atomic host `economy.wager(stake, payout)`; the payout (gross,
/// stake included, exactly the original's `amount * multiplier` credit after the stake was removed) must be at most 100x the
/// stake, which is why <see cref="SlotsConfig"/> rejects multipliers above 100. (3) With the default five emotes the
/// expected return is 127.5/125 = 102% of the stake (the original's own table is player-favourable; tune the multipliers or
/// emotes in config). (4) The original picked a LOSE message with an index drawn over the WIN pool's length (so only the
/// first 20 of its 62 lose lines ever appeared); this port draws over the full lose pool. (5) A loss line also states the
/// stake lost. (6) The display name is the host `{user:uuid}` placeholder (no name lookup exists), the currency name the
/// economy service's `points_view` alias; the original's default-command-trigger event hooks are not ported.</para>
///
/// <para><b>Idempotency.</b> The reels and message picks are recorded in `kv` under a key hashed from message id, platform,
/// actor uuid AND command BEFORE the single `wager` call, with actor, command and raw text stored in the record. A redelivery
/// reprints the recorded outcome only on an exact match of all of them (otherwise it is validated and cooled down like any new
/// command); a replay skips the cooldown the first delivery already took. An invocation without a message id is refused
/// loudly.</para>
///
/// <para><b>Failure policy.</b> A refusal the user can act on (not linked, bet over the max / under the minimum, insufficient
/// funds) gets a chat reply and releases the cooldown; anything else propagates so the host retries or surfaces it.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF), checked AFTER the command match and before the
/// cooldown, so a disabled flag never consumes a cooldown slot.
/// </summary>
public sealed class SlotsLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const uint OutcomeTtlSeconds = 3600;
    internal const string FlagKey = "waddles.command-superpenguin-slots";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("slot", "slots"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        var config = SlotsConfig.Load(host.Context.ConfigJson);
        if (host.Context.Community is null)
        {
            host.Log.Warn("slots.refused", new LogFields().With("reason", "no_community").Build());
            return GameSupport.Reply(@event, command, "Slots needs a community; this channel is not linked to one.");
        }

        GameSupport.RequireMessageId(host.Context);
        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("slots.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return GameSupport.Reply(@event, command, "Link your account to this community before playing slots.");
        }

        var store = new KvStateStore(host.Kv);
        var outcomeKey = GameSupport.ReplayKey("slots.out", host.Context, @event.Platform, actor, command.Name);
        var name = UserPlaceholder.For(actor);

        var recorded = store.GetJson(outcomeKey, SlotsJsonContext.Default.SlotsOutcome);
        var isReplay = recorded is not null && IsSameDelivery(recorded, actor, command);
        SlotsOutcome outcome;
        if (isReplay)
        {
            outcome = recorded!;
        }
        else
        {
            var fresh = Fresh(command, host, config, store, actor, name, out var refusal);
            if (fresh is null)
            {
                return refusal is null ? null : GameSupport.Reply(@event, command, refusal);
            }

            outcome = fresh;
            store.SetJson(outcomeKey, outcome, SlotsJsonContext.Default.SlotsOutcome, OutcomeTtlSeconds);
        }

        var payout = Payout(outcome, config);
        long balance;
        try
        {
            balance = host.Economy.Wager(actor, outcome.Stake, payout);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            store.Delete(outcomeKey);
            StateCooldown.Release(store, command.Name, actor);
            host.Log.Info("slots.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            return GameSupport.Reply(@event, command, ex.Kind == EconomyErrorKind.OverCap
                ? $"{name}, that bet is over the limit for {GameSupport.Alias(host, actor, "slots")}."
                : $"{name}, you don't have that much to play slots with.");
        }

        var alias = GameSupport.Alias(host, actor, "slots");
        host.Log.Info("slots.settled", new LogFields()
            .With("command", command.Name)
            .With("won", payout > 0)
            .With("stake", (long)outcome.Stake)
            .With("replay", isReplay)
            .Build());

        return GameSupport.Reply(@event, command, BuildResultText(outcome, config, name, alias, balance));
    }

    /// <summary>A recorded outcome is this delivery only if actor, command name and the raw command text all match.</summary>
    private static bool IsSameDelivery(SlotsOutcome recorded, string actor, ChatCommand command) =>
        recorded.Actor == actor && recorded.Command == command.Name && recorded.Text == command.RawText;

    /// <summary>Validates the request, takes the cooldown and spins; returns null (with a refusal text, or none for a silent cooldown drop).</summary>
    private SlotsOutcome? Fresh(ChatCommand command, IWaddleHostNext host, SlotsConfig config, KvStateStore store, string actor, string name, out string? refusal)
    {
        refusal = null;
        var maxBet = host.Economy.MaxBet(actor);
        if (maxBet < 1)
        {
            refusal = $"{name}, you have no {GameSupport.Alias(host, actor, "slots")} to play slots with.";
            return null;
        }

        switch (BetParser.Parse(command.Args, maxBet, config.MinBet, config.MinBet, out var amount))
        {
            case BetParse.Invalid:
                refusal = $"To use !{command.Name}, either just run !{command.Name} or do !{command.Name} max or all or amount or % (ie. 50%)";
                return null;
            case BetParse.TooMuch:
            case BetParse.TooLow:
                refusal = $"The max bet is {GameSupport.N0(maxBet)} and must be greater then {GameSupport.N0(config.MinBet)}";
                return null;
        }

        if (!StateCooldown.TryAcquire(store, command.Name, actor, config.CooldownSeconds))
        {
            // The original silently drops a command received during its own cooldown window. Match that.
            return null;
        }

        var last = config.Emotes.Length - 1;
        return new SlotsOutcome(actor, command.Name, command.RawText, (ulong)amount,
            _nextInt(0, last), _nextInt(0, last), _nextInt(0, last),
            _nextInt(0, SlotsMessages.Win.Length - 1), _nextInt(0, SlotsMessages.Lose.Length - 1));
    }

    /// <summary>Stake times the configured multiplier (3 of a kind, first two equal, other pair), rounded half-to-even like the original's `Convert.ToInt64`; 0 for no match.</summary>
    internal static ulong Payout(SlotsOutcome o, SlotsConfig config)
    {
        double multiplier;
        if (o.Reel1 == o.Reel2 && o.Reel2 == o.Reel3)
        {
            multiplier = config.ThreeOfAKind;
        }
        else if (o.Reel1 == o.Reel2)
        {
            multiplier = config.FirstTwo;
        }
        else if (o.Reel2 == o.Reel3 || o.Reel3 == o.Reel1)
        {
            multiplier = config.LastTwo;
        }
        else
        {
            return 0;
        }

        return (ulong)Math.Round(o.Stake * multiplier);
    }

    /// <summary>Pure result rendering, exposed internally for unit testing without a host round trip.</summary>
    internal static string BuildResultText(SlotsOutcome o, SlotsConfig config, string displayName, string alias, long balance)
    {
        var text = $"{displayName}: [ {config.Emotes[o.Reel1]} {config.Emotes[o.Reel2]} {config.Emotes[o.Reel3]} ] ";
        var payout = Payout(o, config);
        if (payout == 0)
        {
            return text + $"You lose {GameSupport.N0((long)o.Stake)} {alias}. " + SlotsMessages.Lose[o.LoseMessage].Replace("{NAME_HERE}", displayName)
                + $" Balance: {GameSupport.N0(balance)} {alias}.";
        }

        return text + $"{GameSupport.N0((long)payout)} {alias}. " + SlotsMessages.Win[o.WinMessage].Replace("{NAME_HERE}", displayName)
            + $" Balance: {GameSupport.N0(balance)} {alias}.";
    }
}

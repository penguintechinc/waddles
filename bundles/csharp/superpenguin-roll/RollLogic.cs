using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRoll;

/// <summary>
/// `!roll [stake|all]` / `!dice [stake|all]` dice game, ported from PenguinTwitchBot's `PastyGames/Roll.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the
/// original's two aliases, dice, prize names, win/lose message pools and per-command 180 s user cooldown (`!roll` and
/// `!dice` track independently, exactly as the original's per-command `userCooldown`).
///
/// <para><b>Economy (stage-next-tools).</b> The original paid fixed prizes of a per-game "point type". Waddles has one
/// per-community currency (waddlebucks, alias configured on the economy service), moved only by the atomic host
/// `economy.wager(stake, payout)`: this port therefore takes a stake (default <see cref="DefaultStake"/>, `all` = the user's
/// current max bet) and pays doubles at <see cref="Multipliers"/> times the stake (Snake eyes 2x ... Boxcars 10x; house edge
/// about 8%: expected return 33/36 of the stake). The fixed-prize original would need a free MINT per roll
/// (`economy.credit`), which this bundle deliberately does not request. The display name is the host
/// `{user:uuid}` placeholder (the egress pass renders the real name); the currency alias comes from the economy service's
/// `points_view` tool.</para>
///
/// <para><b>Idempotency per event.</b> The host de-duplicates `wager` per (event, call ordinal), but a redelivered event
/// would re-roll the dice and print a different result than the money that actually moved. The dice and stake are therefore
/// recorded in `kv` under the event's message id BEFORE the single `wager` call and reused on redelivery, so a replay
/// reprints the original outcome and the replayed wager moves nothing. A replay also skips the cooldown (the first delivery
/// already took it).</para>
///
/// <para><b>Failure policy.</b> A refusal the user can act on (not linked, stake over max bet, insufficient funds) gets a chat
/// reply and releases the cooldown; anything else (economy `denied`/`unavailable`/`backend`, `kv` failure) propagates so the
/// host retries or surfaces it -- never a silent fallback to unpaid "points" text.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF), checked AFTER the command match and before the
/// cooldown, so a disabled flag never consumes a user's cooldown slot.
/// </summary>
public sealed class RollLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const uint CooldownSeconds = 180;
    internal const ulong DefaultStake = 10;
    internal const uint OutcomeTtlSeconds = 3600;

    /// <summary>
    /// Distinct from the first-party Python `bundles/python/roll` bundle's `waddles.command-roll` flag: that is an unrelated
    /// NdM dice command that also answers to `!roll`; a separate key keeps the two independently toggleable.
    /// </summary>
    internal const string FlagKey = "waddles.command-superpenguin-roll";

    /// <summary>Payout multiplier (of the stake) for a double of value 1..6; expected return is 33/36 of the stake.</summary>
    internal static readonly ulong[] Multipliers = [2, 3, 4, 6, 8, 10];

    private static readonly string[] PrizeNames = ["Snake eyes", "Hard four", "Hard six", "Hard eight", "Hard ten", "Boxcars"];

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("roll", "dice"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("roll.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "Rolling needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("roll.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before rolling.");
        }

        var store = new KvStateStore(host.Kv);
        var name = UserPlaceholder.For(actor);

        // Refuses (throws) on a blank message id; the key binds message id + platform + actor + command, and the stored
        // record must carry the same actor + command, else it is not a replay (full validation + cooldown apply).
        var outcomes = new IdempotentOutcome(store, "roll", OutcomeTtlSeconds);
        var messageId = host.Context.MessageId;
        var replay = outcomes.TryReplay(messageId, @event.Platform, actor, command.Name, RollJsonContext.Default.RollOutcome,
            r => r.Actor == actor && r.Command == command.Name);
        RollOutcome outcome;
        if (replay is not null)
        {
            outcome = replay;
        }
        else
        {
            var fresh = Fresh(command, host, store, actor, name, out var refusal);
            if (fresh is null)
            {
                return refusal is null ? null : Reply(@event, command, refusal);
            }

            outcome = fresh;
            outcomes.Record(messageId, @event.Platform, actor, command.Name, outcome, RollJsonContext.Default.RollOutcome);
        }

        var payout = Payout(outcome);
        long balance;
        try
        {
            balance = host.Economy.Wager(actor, outcome.Stake, payout);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            outcomes.Discard(messageId, @event.Platform, actor, command.Name);
            StateCooldown.Release(store, command.Name, actor);
            host.Log.Info("roll.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            var alias = Alias(host, actor);
            return Reply(@event, command, ex.Kind == EconomyErrorKind.OverCap
                ? $"{name}, that stake is over the limit for {alias}."
                : $"{name}, you don't have {outcome.Stake} {alias} to roll.");
        }

        var currency = Alias(host, actor);
        host.Log.Info("roll.settled", new LogFields()
            .With("command", command.Name)
            .With("won", payout > 0)
            .With("stake", (long)outcome.Stake)
            .With("replay", replay is not null)
            .Build());

        return Reply(@event, command, BuildResultText(outcome, name, currency, balance));
    }

    /// <summary>Validates the request and rolls fresh dice; returns null (with a refusal text, or none for a silent cooldown drop).</summary>
    private RollOutcome? Fresh(ChatCommand command, IWaddleHostNext host, KvStateStore store, string actor, string name, out string? refusal)
    {
        refusal = null;
        var maxBet = host.Economy.MaxBet(actor);
        if (maxBet < 1)
        {
            refusal = $"{name}, you have no {Alias(host, actor)} to roll with.";
            return null;
        }

        if (!TryStake(command.Args, (ulong)maxBet, out var stake, out var overCap))
        {
            refusal = overCap
                ? $"{name}, your max roll right now is {maxBet} {Alias(host, actor)}."
                : "Usage: !roll [stake|all]";
            return null;
        }

        if (!StateCooldown.TryAcquire(store, command.Name, actor, CooldownSeconds))
        {
            // The original silently drops a command received during its own cooldown window. Match that.
            return null;
        }

        return new RollOutcome(_nextInt(1, 6), _nextInt(1, 6), stake, _nextInt(0, WinMessages.Length - 1), _nextInt(0, LostMessages.Length - 1), actor, command.Name);
    }

    /// <summary>Parses `[stake|all]`; no argument = <see cref="DefaultStake"/> (clamped to the max bet only for `all`).</summary>
    internal static bool TryStake(IReadOnlyList<string> args, ulong maxBet, out ulong stake, out bool overCap)
    {
        stake = DefaultStake;
        overCap = false;
        if (args.Count > 1)
        {
            return false;
        }

        if (args.Count == 1)
        {
            if (string.Equals(args[0], "all", StringComparison.OrdinalIgnoreCase))
            {
                stake = maxBet;
                return true;
            }

            if (!ulong.TryParse(args[0], System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out stake) || stake == 0)
            {
                return false;
            }
        }

        if (stake > maxBet)
        {
            overCap = true;
            return false;
        }

        return true;
    }

    /// <summary>The payout for an outcome: stake times the double's multiplier, 0 for a miss.</summary>
    internal static ulong Payout(RollOutcome outcome) =>
        outcome.Dice1 == outcome.Dice2 ? outcome.Stake * Multipliers[outcome.Dice1 - 1] : 0;

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
            host.Log.Warn("roll.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>Pure result rendering, exposed internally for unit testing without a host round trip.</summary>
    internal static string BuildResultText(RollOutcome outcome, string displayName, string alias, long balance)
    {
        var text = $"{displayName} rolls a [{outcome.Dice1}] and [{outcome.Dice2}]. ";
        if (outcome.Dice1 != outcome.Dice2)
        {
            return text + $"You lose {outcome.Stake} {alias}. " + string.Format(LostMessages[outcome.LoseMessage], displayName) + $" Balance: {balance} {alias}.";
        }

        var payout = Payout(outcome);
        text += outcome.Dice1 == 6
            ? $"Boxcars to the max!!! {payout} {alias}! "
            : $"{PrizeNames[outcome.Dice1 - 1]} for {payout} {alias}! ";
        return text + string.Format(WinMessages[outcome.WinMessage], displayName) + $" Balance: {balance} {alias}.";
    }

    // Verbatim from PenguinTwitchBot's `Roll.cs` `LoadWinMessages()`/
    // `LoadLostMessages()` (MIT, used with permission -- see bundle.yaml
    // `notice`). Kept unmodified for behavioral fidelity per the porting
    // brief ("faithful ... output format").
    internal static readonly string[] WinMessages =
    [
        "Congratulations!",
        "On a scale of 1 to 10, this was 2 easy",
        "Aw, yeah!",
        "You got lucky.",
        "GOOOOOOOAAAAAL!!",
        "Keep it up!",
        "Baby, now you're number one, shining bright for everyone!",
        "I only let you win out of pity.",
        "If there were more clumsy and perverted people like {0}, the world would be a better place.",
        "Dreams do come true!",
        "The way to success is always difficult, but you still manage to get yourself on top and be honored.",
        "You rarely win, but sometimes you do.",
        "Sometimes in life you don't always feel like a winner, but that doesn't mean you're not a winner.",
        "It's easy to win. Anybody can win.",
        "Winning is great, sure, but if you are really going to do something in life, the secret is learning how to lose.",
        "A winner is just a loser who tried one more time.",
        "Sugoi~!",
        "This thing must have been rigged!",
        "The Goddess Fortuna smiles upon you.",
        "?!......... (Seriously?!)",
    ];

    internal static readonly string[] LostMessages =
    [
        "Better luck next time!",
        "Gambling can be hard, but don't stray.",
        "Dreamin', don't give it up {0}",
        "You have ignited a nuclear war! And no, there is no animated display of a mushroom cloud. Why? Because we do not reward failure.",
        "Can you like.. win? please?",
        "Game Over.",
        "Don't looooose your waaaaaaay!",
        "You just weren't good enough.",
        "Will {0} finally win? Find out next time on Dragon Ball Z!",
        "{0} has lost something great today!",
        "Perhaps if you trained in the mountains in solitude, you could learn how to win.",
        "Believe in the heart of the cards!",
        "Believe in me who believes in you!",
        "404 Win Not Found.",
        "If the human body is 65% water, how can you be 100% salt?",
        "To win you must gain sight beyond sight!",
        "You're great at losing! Don't let anyone tell you otherwise.",
        "So tell me, what's it like living in a constant haze of losses?",
        "Did you know that games of chance is the same way how Quantum Mechanics work?",
        "L-O-S-E-R...",
        "Dreams shattered :(",
        "Looks like you've activated my trap card Kappa",
        "You're not obligated to win. You're obligated to keep trying.",
        "This is not the end, this is not even the beginning of the end, this is just perhaps the end of the beginning.",
        "Sometimes not getting what you want is a brilliant stroke of luck.",
        "Winning takes talent, to repeat takes character.",
        "Snake? Snake?! Snaaaaaaaaaake!!.",
        "You're like forrest gump without the running thing",
        "You should go set the world record for holding your breath.",
        "Shut down!",
        "You don't deserve to play this game. Go back to playing with crayons.",
        "Too bad. Game over. Insert new quarter.",
        "The Goddess Fortuna frowns upon you.",
        "In my ideal nation, there would exist no one as weak as you!",
        "What a joke!",
        "Learn from your defeat, child.",
        "You do not have enough experience! Are you listening to me?!",
        "Hope you're listening. Level up homie!",
        "Your technique need work.",
        "Hey, did you hurt yourself?",
        "That's your best?",
        "Hah ha ha ha ha ha ha!",
        "Don't make excuses for your loss! Go train and try again!",
        "Hey, you're not that bad. You're not very good, either.",
        "I've had meetings that were more grueling than this.",
        "Even by the lowest standards, that was really bad.",
        "Ha-ha-ha! What's the matter? You don't like losing? Well, that's not my problem. Ha, ha, ha, ha, ha!",
        "Ancient words of wisdom... \"you suck\"",
        "I won't say you're bad. I'll just think it, OK?",
        "I think I've learned something from this. You're nothing...",
        "Hey! Don't worry about it! You know... being bad and all!",
        "The tragedy of all losers is that they think they were on the verge of victory.",
        "Hm! You should go back to playing puzzle games!",
        "The past is the past man. If you are a loser now, then you're a loser period.",
        "The reason you lost is quite simple: you're bad!",
        "Remember that one time during the game when it looked like you might actually win? No? Me neither.",
        "Is that all you can do? You wouldn't have gone very far with that anyway.",
        "Don't blame bad luck or fate. You lost because you suck.",
        "You weren't that bad. You were pathetic! Go home!",
        "You made an effort at least, pathetic as it was!",
        "My dad could win, and he's dead!",
        "You with the keyboard! I won DESPITE you. You suck. And smell -- REALLY smell.",
        "A loser doesn't know what he'll do if he loses, but talks about what he'll do if he wins, and a winner doesn't talk about what he'll do if he wins, but knows what he'll do if he loses.",
        "If you can't win, lose like a champion!",
        "Welcome to Loserville! Population: You!",
        "Whoever said, \"It's not whether you win or lose that counts,\" probably lost.",
        "What went wrong? What didn't? - it was just one of those days. Not your day really",
    ];
}

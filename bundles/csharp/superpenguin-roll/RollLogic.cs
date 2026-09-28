using WaddleSdk.Chat;
using WaddleSdk.Cooldown;
using WaddleSdk.Json;
using WaddleSdk.Random;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRoll;

/// <summary>
/// `!roll`/`!dice` dice game, ported from PenguinTwitchBot's
/// `PastyGames/Roll.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used
/// with permission -- see `bundle.yaml`'s `notice`). Faithful to the
/// original's argument handling (both `!roll` and `!dice` trigger the same
/// game, no arguments consumed), prize table, and win/lose message pools;
/// two simplifications, both because the surrounding platform capability
/// does not exist yet in Waddles:
///
/// <list type="bullet">
/// <item>The original substitutes the caller's Twitch/Discord display name
/// into message templates (`e.DisplayName`); this world's `PlatformEvent`
/// carries only `actor` (an opaque id string, `wit/waddle-bundle/stage.wit`),
/// no display-name lookup capability -- <see cref="Transform"/> substitutes
/// `actor` directly.</item>
/// <item>The original awards `prizes[dice1-1]` of a per-streamer configurable
/// "point type" via a shared points-system service; no cross-bundle points
/// ledger exists in this executor yet, so the prize amount/name are still
/// computed and announced in the reply text (matching the original's visible
/// output), generically as "points" rather than a configured type name, but
/// are not persisted anywhere.</item>
/// </list>
///
/// The original's per-command 180s user cooldown (`RegisterDefaultCommand(...,
/// userCooldown: 180, ...)`, independent per command name) is reproduced via
/// <see cref="CooldownGuard"/> over `kv`, keyed by command name so `!roll` and
/// `!dice` cooldowns track independently exactly as they did in the original.
/// `kv` is currently hardcoded to deny every call host-side
/// (svc-process/svc-action) while the real backend lands --
/// <see cref="CooldownGuard.TryAcquire"/> degrades gracefully in that case
/// (cooldown simply is not enforced yet), so this bundle still functions.
/// </summary>
public sealed class RollLogic : WaddleProcessStage
{
    internal const string CommandPrefix = "!";
    private const uint CooldownSeconds = 180;
    private const string PointTypeName = "points";

    /// <summary>Index 0..5 == dice value 1..6 on a double.</summary>
    private static readonly int[] Prizes = [40, 160, 360, 640, 1000, 1440];
    private static readonly string[] PrizeNames = ["Snake eyes", "Hard four", "Hard six", "Hard eight", "Hard ten", "Boxcars"];

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHost host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("roll", "dice"))
        {
            return null;
        }

        var userId = @event.Actor ?? "anonymous";
        if (!CooldownGuard.TryAcquire(host.Kv, command.Name, userId, CooldownSeconds))
        {
            // The original (BaseCommandService's cooldown check) silently
            // drops a command received during its own cooldown window -- no
            // chat reply, no error. Match that exactly.
            return null;
        }

        var dice1 = WaddleRandom.NextInt(1, 6);
        var dice2 = WaddleRandom.NextInt(1, 6);
        var text = BuildResultText(dice1, dice2, userId);

        return PlatformEventInfo.WithPayload(
            @event,
            new ChatReplyPayload(text, command.ChannelId),
            WaddleSdkJsonContext.Default.ChatReplyPayload);
    }

    /// <summary>Pure game logic, exposed internally for unit testing without a WIT/host round trip.</summary>
    internal static string BuildResultText(int dice1, int dice2, string displayName)
    {
        var resultMessage = $"{displayName} rolls a [{dice1}] and [{dice2}]. ";

        if (dice1 != dice2)
        {
            return resultMessage + string.Format(WaddleRandom.Pick(LostMessages), displayName);
        }

        var prizeAmount = Prizes[dice1 - 1];
        var prizeName = PrizeNames[dice1 - 1];
        resultMessage += dice1 == 6
            ? $"Boxcars to the max!!! {prizeAmount} {PointTypeName}! "
            : $"{prizeName} for {prizeAmount} {PointTypeName}! ";

        return resultMessage + string.Format(WaddleRandom.Pick(WinMessages), displayName);
    }

    // Verbatim from PenguinTwitchBot's `Roll.cs` `LoadWinMessages()`/
    // `LoadLostMessages()` (MIT, used with permission -- see bundle.yaml
    // `notice`). Kept unmodified for behavioral fidelity per the porting
    // brief ("faithful ... output format").
    private static readonly string[] WinMessages =
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

    private static readonly string[] LostMessages =
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

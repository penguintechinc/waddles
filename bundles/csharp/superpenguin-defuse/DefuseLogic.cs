using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinDefuse;

/// <summary>
/// `!defuse &lt;wire&gt;` bomb-defusal game, ported from PenguinTwitchBot's `PastyGames/defuse.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the
/// original: the user pays a fixed cost (500) and cuts one of the configured wires (red, blue, yellow); one wire chosen
/// uniformly at random defuses the bomb, which pays a reward drawn uniformly from cost x 3 - cost / 3 to cost x 3 + cost / 3
/// (integer division, bounds inclusive); any other wire explodes and the cost is lost. 10 s per-user cooldown; an invalid or
/// missing wire is answered with the wire list without charging or cooling down. Wires, cost, the multiplier and every message
/// template are configurable (<see cref="DefuseConfig"/>, original defaults); expected return is exactly the cost (fair game).
///
/// <para><b>Deviations forced by the economy.</b> (1) The cost and the reward settle through ONE atomic host
/// `economy.wager(cost, reward)` (reward 0 on an explosion) instead of the original's separate remove/add; the reward is the
/// gross payout and stays within the host's 100x cap because <see cref="DefuseConfig"/> bounds the multiplier. The `cost` must
/// also fit the host `economy.max-bet`. (2) The display name is the host `{user:uuid}` placeholder (the original's viewer
/// "name with title" lookup does not exist), the currency name the economy service's `points_view` alias, replies append the new
/// balance. (3) The original also queued a `defuse.gif` / `detonated.gif` overlay alert and fired default-command trigger events;
/// no overlay or trigger surface exists in this world, so neither is ported.</para>
///
/// <para><b>Idempotency.</b> The cut wire, the winning wire and the reward are recorded in `kv` under a key hashed from message
/// id, platform, actor uuid AND command BEFORE the single `wager` call, with actor, command and raw text stored in the record.
/// A redelivery reprints and re-pays the recorded outcome only on an exact match of all of them (otherwise it is validated and
/// cooled down like any new command); a replay skips the cooldown the first delivery already took. An invocation without a
/// message id is refused loudly.</para>
///
/// <para><b>Failure policy.</b> A refusal the user can act on (not linked, bad wire, cannot afford the cost) gets a chat reply
/// and releases the cooldown; anything else propagates so the host retries or surfaces it.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF), checked AFTER the command match and before the cooldown.
/// </summary>
public sealed class DefuseLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const uint OutcomeTtlSeconds = 3600;
    internal const string FlagKey = "waddles.command-superpenguin-defuse";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("defuse"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        var config = DefuseConfig.Load(host.Context.ConfigJson);
        if (host.Context.Community is null)
        {
            host.Log.Warn("defuse.refused", new LogFields().With("reason", "no_community").Build());
            return GameSupport.Reply(@event, command, "Defuse needs a community; this channel is not linked to one.");
        }

        GameSupport.RequireMessageId(host.Context);
        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("defuse.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return GameSupport.Reply(@event, command, "Link your account to this community before defusing bombs.");
        }

        var store = new KvStateStore(host.Kv);
        var outcomeKey = GameSupport.ReplayKey("defuse.out", host.Context, @event.Platform, actor, command.Name);
        var name = UserPlaceholder.For(actor);

        var recorded = store.GetJson(outcomeKey, DefuseJsonContext.Default.DefuseOutcome);
        var isReplay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name && recorded.Text == command.RawText;
        DefuseOutcome outcome;
        if (isReplay)
        {
            outcome = recorded!;
        }
        else
        {
            var fresh = Fresh(command, host, config, store, actor, out var refusal);
            if (fresh is null)
            {
                return refusal is null ? null : GameSupport.Reply(@event, command, refusal);
            }

            outcome = fresh;
            store.SetJson(outcomeKey, outcome, DefuseJsonContext.Default.DefuseOutcome, OutcomeTtlSeconds);
        }

        long balance;
        try
        {
            balance = host.Economy.Wager(actor, outcome.Cost, outcome.Reward);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            store.Delete(outcomeKey);
            StateCooldown.Release(store, command.Name, actor);
            host.Log.Info("defuse.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            return GameSupport.Reply(@event, command, NotEnough(host, config, actor));
        }

        var alias = GameSupport.Alias(host, actor, "defuse");
        host.Log.Info("defuse.settled", new LogFields()
            .With("won", outcome.Reward > 0)
            .With("cost", (long)outcome.Cost)
            .With("replay", isReplay)
            .Build());

        return GameSupport.Reply(@event, command, BuildResultText(outcome, config, name, alias, balance));
    }

    /// <summary>Validates the wire and the funds, takes the cooldown and draws the winning wire and reward; null (+ refusal text, or none for a silent cooldown drop) when it cannot be played.</summary>
    private DefuseOutcome? Fresh(ChatCommand command, IWaddleHostNext host, DefuseConfig config, KvStateStore store, string actor, out string? refusal)
    {
        refusal = null;
        var wire = string.Join(' ', command.Args).Trim();
        if (wire.Length == 0 || !config.Wires.Contains(wire))
        {
            refusal = GameplayRender(config.NoArgs, config, "", "", 0, "");
            return null;
        }

        if (host.Economy.MaxBet(actor) < config.Cost)
        {
            refusal = NotEnough(host, config, actor);
            return null;
        }

        if (!StateCooldown.TryAcquire(store, command.Name, actor, config.CooldownSeconds))
        {
            return null;
        }

        var cost = (ulong)config.Cost;
        var winning = config.Wires[_nextInt(0, config.Wires.Length - 1)];
        ulong reward = 0;
        if (string.Equals(winning, wire, StringComparison.OrdinalIgnoreCase))
        {
            var spread = config.Cost / config.WinMultiplier;
            var middle = config.Cost * config.WinMultiplier;
            reward = (ulong)_nextInt(checked((int)(middle - spread)), checked((int)(middle + spread)));
        }

        return new DefuseOutcome(actor, command.Name, command.RawText, wire, cost, reward);
    }

    private static string NotEnough(IWaddleHostNext host, DefuseConfig config, string actor) =>
        GameplayRender(config.NotEnough, config, "", "", 0, GameSupport.Alias(host, actor, "defuse"));

    /// <summary>Pure result rendering from the configured templates, exposed internally for unit testing without a host round trip.</summary>
    internal static string BuildResultText(DefuseOutcome outcome, DefuseConfig config, string displayName, string alias, long balance)
    {
        var start = GameplayRender(config.Starting, config, displayName, outcome.Wire, 0, alias);
        var end = outcome.Reward > 0
            ? GameplayRender(config.Success, config, displayName, outcome.Wire, (long)outcome.Reward, alias)
            : GameplayRender(config.Failure, config, displayName, outcome.Wire, (long)outcome.Cost, alias);
        return start + end + $" Balance: {GameSupport.N0(balance)} {alias}.";
    }

    /// <summary>The original's `ReplaceVariables`: `{Wires}`, `{Wire}`, `{Cost}`, `{PointType}`, `{Name}` and `{Points}`, case-insensitive.</summary>
    private static string GameplayRender(string template, DefuseConfig config, string name, string wire, long points, string alias) =>
        template
            .Replace("{Wires}", string.Join(", ", config.Wires), StringComparison.OrdinalIgnoreCase)
            .Replace("{Wire}", wire, StringComparison.OrdinalIgnoreCase)
            .Replace("{Cost}", GameSupport.N0(config.Cost), StringComparison.OrdinalIgnoreCase)
            .Replace("{PointType}", alias, StringComparison.OrdinalIgnoreCase)
            .Replace("{Name}", name, StringComparison.OrdinalIgnoreCase)
            .Replace("{Points}", GameSupport.N0(points), StringComparison.OrdinalIgnoreCase);
}

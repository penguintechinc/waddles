using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinGamble;

/// <summary>
/// `!gamble amount|max|all|N%` d100 gamble with a progressive jackpot, and `!jackpot`, ported from PenguinTwitchBot's
/// `PastyGames/Gamble.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`).
/// Faithful to the original's odds and payouts: a d100 roll, 49..100 wins (`minimum_for_win` 48) paying stake x
/// `winning_multiplier` (2), 69 is the jackpot number, a losing bet adds `jackpot_contribution` (10%) of the stake to the pot,
/// the jackpot starts at 1000 and never drops below it, a jackpot win pays a share of the pot proportional to the bet, the
/// 180 s `!gamble` cooldown, the 5 minimum bet, the bet grammar and the message templates (all configurable, original
/// defaults). The original's `MaxBet`/`Percentage` settings fold into <see cref="GambleConfig"/> and the host
/// `economy.max-bet`.
///
/// <para><b>Jackpot pool.</b> The pot lives in host `kv` as an atomic counter of what has accrued ABOVE the starting jackpot
/// (`kv.increment` is atomic; `kv` has no compare-and-swap, so a read-modify-write would lose updates). A loss adds its
/// contribution; a jackpot win subtracts what it paid, clamped so the pot never falls below the starting jackpot. Each
/// pot change is applied at most once per event through a `TryClaim` marker, so a redelivered event never double-counts. The
/// counter's TTL is refreshed by every increment; if nobody gambles for the host's maximum `kv` TTL the pot falls back to the
/// starting jackpot.</para>
///
/// <para><b>Deviations forced by the economy.</b> (1) Only the actor can be debited and the payout of the one atomic host
/// `economy.wager(stake, payout)` is capped at 100x the stake, so the jackpot share is truncated to fit that cap (the
/// remainder stays in the pot; with the defaults the cap is never reached). (2) The original scaled the share by
/// bet / (the original's configured maximum bet). Here the host `economy.max-bet` is bounded by the user's holdings, which
/// would let a 5-point bet claim the whole pot, so the share is scaled by bet / `max_bet` (config, default 500, also the
/// manifest's `max_bet` grant) instead. (3) The jackpot is paid only while the stream is online in the original; no
/// stream-state capability exists in this world, so the jackpot number always pays the jackpot. (4) The display name is the
/// host `{user:uuid}` placeholder, the currency name the economy service's `points_view` alias, replies append the new
/// balance, and the original's default-command-trigger event hooks are not ported.</para>
///
/// <para><b>Idempotency.</b> The roll and the jackpot share are recorded in `kv` under a key hashed from message id, platform,
/// actor uuid AND command BEFORE the single `wager` call, with actor, command and raw text stored in the record. A
/// redelivery reprints and re-pays the recorded outcome only on an exact match of all of them (otherwise it is validated and
/// cooled down like any new command); a replay skips the cooldown the first delivery already took. An invocation without a
/// message id is refused loudly.</para>
///
/// <para><b>Failure policy.</b> A refusal the user can act on (not linked, bad bet, insufficient funds) gets a chat reply and
/// releases the cooldown; anything else propagates so the host retries or surfaces it.</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF), checked AFTER the command match and before the cooldown.
/// </summary>
public sealed class GambleLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const uint OutcomeTtlSeconds = 3600;
    internal const string FlagKey = "waddles.command-superpenguin-gamble";
    internal const string PotKey = "gamble.pot";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("gamble", "jackpot"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        var config = GambleConfig.Load(host.Context.ConfigJson);
        if (host.Context.Community is null)
        {
            host.Log.Warn("gamble.refused", new LogFields().With("reason", "no_community").Build());
            return GameSupport.Reply(@event, command, "Gambling needs a community; this channel is not linked to one.");
        }

        GameSupport.RequireMessageId(host.Context);
        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("gamble.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return GameSupport.Reply(@event, command, "Link your account to this community before gambling.");
        }

        var store = new KvStateStore(host.Kv);
        if (command.Is("jackpot"))
        {
            var alias = GameSupport.Alias(host, actor, "gamble");
            var text = Render(config.CurrentJackpotMessage, ("{jackpot}", GameSupport.N0(Jackpot(host, config))), ("{PointType}", alias));
            return GameSupport.Reply(@event, command, text);
        }

        var outcomeKey = GameSupport.ReplayKey("gamble.out", host.Context, @event.Platform, actor, command.Name);
        var name = UserPlaceholder.For(actor);

        var recorded = store.GetJson(outcomeKey, GambleJsonContext.Default.GambleOutcome);
        var isReplay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name && recorded.Text == command.RawText;
        GambleOutcome outcome;
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
            store.SetJson(outcomeKey, outcome, GambleJsonContext.Default.GambleOutcome, OutcomeTtlSeconds);
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
            host.Log.Info("gamble.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            return GameSupport.Reply(@event, command, config.NotEnough);
        }

        ApplyPot(host, config, store, outcomeKey, outcome, payout);
        var currency = GameSupport.Alias(host, actor, "gamble");
        host.Log.Info("gamble.settled", new LogFields()
            .With("won", payout > 0)
            .With("jackpot", outcome.Share > 0)
            .With("stake", (long)outcome.Stake)
            .With("replay", isReplay)
            .Build());

        return GameSupport.Reply(@event, command, BuildResultText(outcome, config, name, currency, balance));
    }

    /// <summary>Validates the bet, takes the cooldown, rolls the d100 and fixes the jackpot share; null (+ refusal text, or none for a silent cooldown drop) when the bet cannot be played.</summary>
    private GambleOutcome? Fresh(ChatCommand command, IWaddleHostNext host, GambleConfig config, KvStateStore store, string actor, out string? refusal)
    {
        refusal = null;
        var maxBet = host.Economy.MaxBet(actor);
        if (maxBet < 1)
        {
            refusal = config.NotEnough;
            return null;
        }

        switch (BetParser.Parse(command.Args, maxBet, config.MinBet, null, out var amount))
        {
            case BetParse.Invalid:
                refusal = Render(config.IncorrectArgs, ("{Command}", command.Name));
                return null;
            case BetParse.TooMuch:
            case BetParse.TooLow:
                refusal = Render(config.IncorrectBet, ("{MaxBet}", GameSupport.N0(maxBet)), ("{MinBet}", GameSupport.N0(config.MinBet)), ("{PointType}", GameSupport.Alias(host, actor, "gamble")));
                return null;
        }

        if (!StateCooldown.TryAcquire(store, command.Name, actor, config.CooldownSeconds))
        {
            return null;
        }

        var stake = (ulong)amount;
        var rolled = _nextInt(1, 100);
        var share = rolled == config.JackpotNumber ? JackpotShare(Jackpot(host, config), stake, config) : 0UL;
        return new GambleOutcome(actor, command.Name, command.RawText, stake, rolled, share);
    }

    /// <summary>The pot to display or pay from: the starting jackpot plus everything accrued above it (never below the start).</summary>
    private static long Jackpot(IWaddleHostNext host, GambleConfig config) =>
        config.StartingJackpot + Math.Max(0, host.Kv.Increment(PotKey, 0, 0));

    /// <summary>The share of <paramref name="pot"/> a bet of <paramref name="stake"/> wins: proportional to the bet up to `max_bet`, truncated so stake x multiplier + share stays within the host's 100x payout cap.</summary>
    internal static ulong JackpotShare(long pot, ulong stake, GambleConfig config)
    {
        if (pot <= 0 || stake == 0)
        {
            return 0;
        }

        var bounded = Math.Min((decimal)stake, config.MaxBet);
        var share = (ulong)Math.Floor(pot * (bounded / config.MaxBet));
        var room = stake * GambleConfig.MaxMultiplier - stake * (ulong)config.WinningMultiplier;
        return Math.Min(share, room);
    }

    /// <summary>The gross payout for an outcome: stake x multiplier (plus the jackpot share) when the roll wins, 0 when it loses.</summary>
    internal static ulong Payout(GambleOutcome outcome, GambleConfig config) =>
        IsWin(outcome.Rolled, config) ? outcome.Stake * (ulong)config.WinningMultiplier + outcome.Share : 0;

    private static bool IsWin(int rolled, GambleConfig config) => rolled > config.MinimumForWin || rolled == config.JackpotNumber;

    /// <summary>
    /// Moves the pot for a settled event: a loss adds its contribution, a jackpot win removes the share it paid. Applied at most
    /// once per event (`TryClaim` marker), and only AFTER the money moved, so a refused wager never touches the pot.
    /// </summary>
    private static void ApplyPot(IWaddleHostNext host, GambleConfig config, KvStateStore store, string outcomeKey, GambleOutcome outcome, ulong payout)
    {
        var delta = 0L;
        if (payout == 0)
        {
            delta = (long)Math.Round(outcome.Stake * config.JackpotContribution);
        }
        else if (outcome.Share > 0)
        {
            delta = -Math.Min((long)outcome.Share, Math.Max(0, host.Kv.Increment(PotKey, 0, 0)));
        }

        if (delta == 0 || !store.TryClaim(StateKey.Of("gamble.pot", StateKey.Hash(outcomeKey)), OutcomeTtlSeconds))
        {
            return;
        }

        var accrued = host.Kv.Increment(PotKey, delta, 0);
        if (accrued < 0)
        {
            // Two jackpot wins raced on the same accrued amount: put back what neither could really take.
            host.Kv.Increment(PotKey, -accrued, 0);
        }
    }

    /// <summary>Pure result rendering from the configured template, exposed internally for unit testing without a host round trip.</summary>
    internal static string BuildResultText(GambleOutcome outcome, GambleConfig config, string displayName, string alias, long balance)
    {
        var payout = Payout(outcome, config);
        string template;
        long points;
        if (payout == 0)
        {
            template = config.LoseMessage;
            points = (long)outcome.Stake;
        }
        else
        {
            template = outcome.Share > 0 ? config.JackpotMessage : config.WinMessage;
            points = (long)payout;
        }

        return Render(template, ("{Name}", displayName), ("{Rolled}", outcome.Rolled.ToString(System.Globalization.CultureInfo.InvariantCulture)),
            ("{Points}", GameSupport.N0(points)), ("{PointType}", alias)) + $" Balance: {GameSupport.N0(balance)} {alias}.";
    }

    /// <summary>Replaces each token case-insensitively, like the original's `Replace(..., StringComparison.OrdinalIgnoreCase)` chain.</summary>
    internal static string Render(string template, params (string Token, string Value)[] tokens)
    {
        foreach (var (token, value) in tokens)
        {
            template = template.Replace(token, value, StringComparison.OrdinalIgnoreCase);
        }

        return template;
    }
}

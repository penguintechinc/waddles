using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Overlay;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace SuperPenguin.DropGameCore;

/// <summary>
/// Static description of one single-shot wagered game: its identity (<see cref="Game"/>, used in logs, kv keys and the widget id),
/// the chat <see cref="Commands"/> it answers to (without the `!`), the PostHog <see cref="FlagKey"/> (default OFF), the per-bet
/// <see cref="MaxBet"/> clamp, the per-user <see cref="CooldownSeconds"/>, the <see cref="Usage"/> text for a malformed command, the
/// <see cref="ConfigId"/> recorded with every play (the game-config revision a seed must be replayed under) and the overlay
/// <see cref="Widget"/> it registers.
/// </summary>
public sealed record GameSpec(
    string Game,
    IReadOnlyList<string> Commands,
    string FlagKey,
    ulong MaxBet,
    uint CooldownSeconds,
    string Usage,
    string ConfigId,
    OverlayWidget Widget);

/// <summary>
/// What a game's pure simulation decided: the <see cref="Payout"/> (0 = loss, at most 100x the stake: the host ledger refuses more),
/// a short human <see cref="Outcome"/> for the chat reply, and <see cref="DataJson"/>, the game-specific part of the overlay
/// envelope (keyframes, board, result) as one JSON value.
/// </summary>
public sealed record GameRound(ulong Payout, string Outcome, string DataJson);

/// <summary>
/// The reusable single-shot wagered drop-game host. A game subclass supplies a <see cref="GameSpec"/> and ONE pure function,
/// <see cref="Play"/> (seed + stake + extra args -> <see cref="GameRound"/>); everything else is here and identical for every game:
/// flag (default OFF, checked after the command match), community, identity, stake parse/clamp, cooldown, seed derivation from the
/// invocation plus one recorded host-entropy value, the replay record written BEFORE the single atomic `Economy.Wager`, refusal
/// handling, the chat reply, the overlay envelope push + one-time `RegisterWidget` after settlement, and structured logs
/// (ids/op/outcome/counts only, no names/PII).
///
/// <para><b>Money.</b> Exactly one `Economy.Wager(actor, stake, payout)` per settlement. A redelivery (same message id, platform,
/// actor, command and raw arguments) re-runs <see cref="Play"/> from the recorded seed, moves nothing on the ledger (the host
/// de-duplicates) and reprints the same reply. InsufficientFunds/OverCap discard the record, release the cooldown and answer in chat;
/// Denied/Unavailable/Backend and kv failures propagate (never an unpaid reply). A blank message id is refused with an exception.</para>
///
/// <para><b>Overlay.</b> Pushed after settlement, also on a replay (the envelope's `seed` is its idempotency id, so a client plays a
/// seed once): a crash between wager and push must not lose the animation. A failed push is warn-logged and never alters the result.</para>
/// </summary>
public abstract class WageredGameBase : WaddleProcessStageNext
{
    private const uint ReplayRecordTtlSeconds = 3600;
    private readonly GameSpec _spec;
    private readonly Func<ulong> _entropy;

    /// <summary>Creates the host; <paramref name="entropy"/> (default: the host RNG) supplies the ONE entropy value of a fresh play.</summary>
    protected WageredGameBase(GameSpec spec, Func<ulong>? entropy = null)
    {
        _spec = spec;
        _entropy = entropy ?? (() => SeedDerivation.NewEntropy());
    }

    /// <summary>
    /// The game's pure, deterministic simulation: the same <paramref name="seed"/>, <paramref name="stake"/> and config must always
    /// give the same <see cref="GameRound"/>. It must not touch the host, the clock or any other randomness.
    /// </summary>
    protected abstract GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs);

    /// <summary>Validates the arguments after the stake; returns the refusal text, or null to accept. The default accepts none.</summary>
    protected virtual string? CheckExtraArgs(IReadOnlyList<string> extraArgs) => extraArgs.Count == 0 ? null : _spec.Usage;

    /// <summary>Renders the chat reply; override to match a game's own phrasing.</summary>
    protected virtual string FormatReply(string name, string alias, ulong stake, GameRound round, long balance)
    {
        var verb = round.Payout > stake ? "won" : round.Payout == stake ? "got back" : "lost";
        var amount = round.Payout > stake ? round.Payout - stake : round.Payout == stake ? stake : stake - round.Payout;
        return $"{name}: {round.Outcome} -- {verb} {amount} {alias} on a {stake} bet and now has {balance} {alias}!";
    }

    /// <inheritdoc />
    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, DropSupport.CommandPrefix);
        if (command is null || !_spec.Commands.Contains(command.Name))
        {
            return null;
        }

        if (!host.Flags.Enabled(_spec.FlagKey, false))
        {
            return null;
        }

        var game = _spec.Game;
        if (host.Context.Community is null)
        {
            host.Log.Warn($"{game}.refused", new LogFields().With("reason", "no_community").Build());
            return DropSupport.Reply(@event, command, $"{Title} needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info($"{game}.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return DropSupport.Reply(@event, command, $"Link your account to this community before playing {game}.");
        }

        var store = new KvStateStore(host.Kv);
        var key = DropReplayGuard.KeyFor(game, @event, host, actor, _spec.Commands[0]);
        var argsHash = StateKey.Hash(command.RawText);
        var name = UserPlaceholder.For(actor);

        var recorded = DropReplayGuard.TryReplay(store, key, actor, _spec.Commands[0], argsHash);
        var replay = recorded is not null;
        DropReplayRecord record;
        if (recorded is not null)
        {
            if (recorded.Config != _spec.ConfigId)
            {
                throw new InvalidOperationException($"replay record was made under game config '{recorded.Config}' but this build plays '{_spec.ConfigId}'; refusing to settle a different result");
            }

            record = recorded;
        }
        else
        {
            var fresh = Fresh(command, host, store, actor, argsHash, @event.Platform, out var refusal);
            if (fresh is null)
            {
                return refusal is null ? null : DropSupport.Reply(@event, command, refusal);
            }

            record = fresh;
        }

        if (!SeedDerivation.TryParseHex(record.Seed, out var seed))
        {
            throw new InvalidOperationException("replay record carries a malformed seed");
        }

        GameRound round;
        try
        {
            round = Play(seed, record.Stake, ExtraArgs(command));
            if (round.Payout > record.Stake * 100)
            {
                throw new InvalidOperationException($"{game} produced payout {round.Payout} over the host cap of 100x stake {record.Stake}");
            }
        }
        catch (Exception) when (!replay)
        {
            // A broken simulation must not cost the user their cooldown slot; nothing was recorded or wagered yet.
            StateCooldown.Release(store, _spec.Commands[0], actor);
            throw;
        }

        if (!replay)
        {
            // Recorded BEFORE the single wager call: a redelivery then replays this exact seed.
            store.SetJson(key, record, DropCoreJsonContext.Default.DropReplayRecord, ReplayRecordTtlSeconds);
        }

        long balance;
        try
        {
            balance = host.Economy.Wager(actor, record.Stake, round.Payout);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.InsufficientFunds or EconomyErrorKind.OverCap)
        {
            store.Delete(key);
            StateCooldown.Release(store, _spec.Commands[0], actor);
            host.Log.Info($"{game}.refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "insufficient_funds").Build());
            var refusedAlias = DropSupport.Alias(host, actor, game);
            return DropSupport.Reply(@event, command, ex.Kind == EconomyErrorKind.OverCap
                ? $"{name}, that bet is over the limit for {refusedAlias}."
                : $"{name}, you don't have {record.Stake} {refusedAlias} to bet.");
        }

        host.Log.Info($"{game}.settled", new LogFields()
            .With("won", round.Payout > 0)
            .With("stake", (long)record.Stake)
            .With("payout", (long)round.Payout)
            .With("seed", record.Seed)
            .With("replay", replay)
            .Build());

        var envelope = OverlayEnvelope.Build(game, "settled", record.Seed, actor, record.Stake, round.Payout, round.DataJson);
        OverlayPublisher.Publish(host, store, game, _spec.Widget, envelope);

        return DropSupport.Reply(@event, command, FormatReply(name, DropSupport.Alias(host, actor, game), record.Stake, round, balance));
    }

    private string Title => char.ToUpperInvariant(_spec.Game[0]) + _spec.Game[1..];

    private static List<string> ExtraArgs(ChatCommand command) => [.. command.Args.Skip(1)];

    /// <summary>Validates and clamps the stake, takes the cooldown and builds the replay record; null (with a refusal, or none for a silent cooldown drop) otherwise.</summary>
    private DropReplayRecord? Fresh(ChatCommand command, IWaddleHostNext host, IStateStore store, string actor, string argsHash, string platform, out string? refusal)
    {
        refusal = null;
        if (command.Args.Count < 1)
        {
            refusal = _spec.Usage;
            return null;
        }

        refusal = CheckExtraArgs(ExtraArgs(command));
        if (refusal is not null)
        {
            return null;
        }

        var held = EconomyServiceClient.For(host).Balance(actor);
        var parsed = StakeParser.Parse(command.Args[0], held, _spec.MaxBet);
        if (!parsed.Ok)
        {
            refusal = parsed.Refusal;
            return null;
        }

        if (!StateCooldown.TryAcquire(store, _spec.Commands[0], actor, _spec.CooldownSeconds))
        {
            // The originals silently drop a command received during their own cooldown window. Match that.
            return null;
        }

        var seed = SeedDerivation.Derive(host.Context.MessageId, platform, actor, _spec.Commands[0], _entropy());
        return new DropReplayRecord(actor, _spec.Commands[0], argsHash, parsed.Stake, SeedDerivation.ToHex(seed), _spec.ConfigId);
    }
}

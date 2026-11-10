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

namespace WaddleBundleSuperpenguinFirst;

/// <summary>
/// `!first` / `!resetfirst`, ported from PenguinTwitchBot's `TicketGames/First.cs` + `FirstGameOptions.cs`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`). Faithful to the original:
/// the first <see cref="MaximumClaims"/> (200) distinct users to type `!first` each win an award that starts at
/// <see cref="StartingAward"/> (100) and drops by 0.5 per earlier claim (floored); a repeat claim is silently ignored; a claim
/// that would pay nothing gets the "too slow" message.
///
/// <para><b>Deviations (documented, none silent).</b> (1) The original resets per STREAM and refuses while offline. No
/// stream-online capability exists in any WIT world yet, so the claim list resets per UTC day and the offline gate is dropped.
/// (2) The original awards points from nothing; here that is a host `economy.credit` MINT of at most 100 per claim (the manifest
/// declares `max_amount: 100`), bounded host-side by the daily mint caps and the community supply ceiling. (3) `!resetfirst` is
/// Streamer-only in the original; no role/rank information reaches a bundle, and an unauthenticated reset would let anyone
/// re-open the mint, so this bundle REFUSES it loudly (see <see cref="ResetRefusal"/>) instead of pretending: the list resets
/// automatically at 00:00 UTC. (4) The player is named with the host `{user:uuid}` placeholder.</para>
///
/// <para><b>Money and replay.</b> The claim position comes from an atomic `kv.increment`, the per-user-per-day claim from an
/// atomic claim. Position and award are recorded under a replay key (message id + platform + actor UUID + command, see
/// <see cref="ReplayGuard"/>) BEFORE the single `economy.credit`, and a redelivered event re-sends that identical credit, which
/// the host de-duplicates per (event, call): it moves money once. A record only counts as a replay on an exact actor+command
/// match. A blank message id is refused with an exception.</para>
///
/// <para><b>Known window.</b> kv has no compare-and-swap: a crash between the per-user claim and the replay record leaves that
/// user's claim consumed without an award for the rest of the UTC day (a lost mint, never a double one).</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class FirstLogic : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const long MaximumClaims = 200;
    internal const long StartingAward = 100;
    internal const uint DayTtlSeconds = 2 * 24 * 60 * 60;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const string FlagKey = "waddles.command-superpenguin-first";

    internal const string TooSlow = "Sorry, You were to slow today. FeelsBadMan";

    /// <summary>The loud refusal for `!resetfirst`: the original gates it on the Streamer rank, which no bundle can see yet.</summary>
    internal const string ResetRefusal =
        "!resetfirst is unavailable: this bundle cannot verify the streamer role yet. The first-claim list resets automatically at 00:00 UTC.";

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("first", "resetfirst"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (command.Is("resetfirst"))
        {
            host.Log.Warn("first.reset_refused", new LogFields().With("reason", "role_not_verifiable").Build());
            return Reply(@event, command, ResetRefusal);
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("first.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "!first needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("first.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before claiming first.");
        }

        var store = new KvStateStore(host.Kv);
        var outcomeKey = ReplayGuard.KeyFor("first.out", @event, host, actor, command.Name);
        var recorded = store.GetJson(outcomeKey, FirstJsonContext.Default.FirstOutcome);
        var replay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name;

        FirstOutcome outcome;
        if (replay)
        {
            outcome = recorded!;
        }
        else
        {
            var fresh = Claim(host, store, actor, command.Name);
            if (fresh is null)
            {
                return null; // already claimed today: the original ignores it silently.
            }

            if (fresh.Award == 0)
            {
                return Reply(@event, command, TooSlow);
            }

            outcome = fresh;
            store.SetJson(outcomeKey, outcome, FirstJsonContext.Default.FirstOutcome, OutcomeTtlSeconds);
        }

        var balance = EconomyServiceClient.For(host).Credit(actor, (ulong)outcome.Award);
        var alias = Alias(host, actor);
        host.Log.Info("first.awarded", new LogFields()
            .With("position", outcome.Position)
            .With("award", outcome.Award)
            .With("replay", replay)
            .Build());
        return Reply(@event, command, BuildAwardText(outcome, alias, balance));
    }

    /// <summary>
    /// Takes this user's once-per-day claim and the next position. Returns null when they already claimed today; an outcome with
    /// award 0 ("too slow", the claim handed back so a later day is unaffected) when the list is full or the award has run out.
    /// </summary>
    private static FirstOutcome? Claim(IWaddleHostNext host, KvStateStore store, string actor, string command)
    {
        var day = DateTimeOffset.FromUnixTimeMilliseconds((long)host.Clock.NowMillis()).UtcDateTime.ToString("yyyyMMdd", CultureInfo.InvariantCulture);
        var userKey = StateKey.Of("first.u", day, StateKey.Hash(actor));
        if (!store.TryClaim(userKey, DayTtlSeconds))
        {
            return null;
        }

        var position = host.Kv.Increment(StateKey.Of("first.n", day), 1, DayTtlSeconds);
        var award = position > MaximumClaims ? 0 : AwardFor(position);
        if (award == 0)
        {
            store.Delete(userKey);
        }

        return new FirstOutcome(actor, command, position, award);
    }

    /// <summary>The award for the <paramref name="position"/>-th claim (1-based): floor(100 - 0.5 * earlier claims), never negative.</summary>
    internal static long AwardFor(long position) => Math.Max(0, (2 * StartingAward - (position - 1)) / 2);

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
            host.Log.Warn("first.alias_unavailable", new LogFields().With("code", ex.Code).Build());
        }

        return EconomyServiceTools.DefaultAlias;
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);

    /// <summary>The original's default award message, with the player named by the host placeholder.</summary>
    internal static string BuildAwardText(FirstOutcome outcome, string alias, long balance) =>
        $"{UserPlaceholder.For(outcome.Actor)}, Whooohooo! You came in position {outcome.Position} and get {outcome.Award} {alias}!! PogChamp Balance: {balance} {alias}.";
}

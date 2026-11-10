using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinLastseen;

/// <summary>
/// `!lastseen [@mention]`, ported from PenguinTwitchBot's `Misc/LastSeen.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used
/// with permission -- see `bundle.yaml`'s `notice`). Every chat message from a linked community member stamps their last-seen time
/// (the original does this inside `ViewerFeature.OnChatMessage`); `!lastseen` reports it as a compound duration (`1w, 2d, 3h, 4m`).
///
/// <para><b>Deviations (documented).</b> (1) The original looks a viewer up by Twitch username; a bundle only ever sees UUIDs, so the
/// target is an @mention (resolved host-side by `identity.ResolveMention`) and a bare `!lastseen` reports the caller (the original
/// stays silent). (2) The original's duration formatter drops seconds (a sub-minute age renders empty); this port renders `N sec`
/// instead. (3) Stamps expire after 90 days (bounded storage); an expired stamp reads as never seen. (4) The caller's own stamp
/// is written AFTER the read, so `!lastseen` never reports "0 sec" for the caller.</para>
///
/// <para>Passive stamping needs every chat message delivered to this bundle: the manifest therefore declares no command-prefix
/// filter. Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).</para>
/// </summary>
public sealed class LastseenLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-lastseen";
    internal const uint StampTtlSeconds = 90 * 24 * 60 * 60;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (!host.Flags.Enabled(FlagKey, false) || host.Context.Community is null)
        {
            return null;
        }

        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        var isQuery = command is not null && command.Is("lastseen");

        var actor = Kit.TryActor(host, "lastseen.actor_unlinked");
        if (actor is null)
        {
            return isQuery ? Kit.Reply(@event, command!, "Link your account to this community before using !lastseen.") : null;
        }

        var store = new KvStateStore(host.Kv);
        PlatformEventInfo? reply = null;
        if (isQuery)
        {
            reply = Query(@event, host, store, command!, actor);
        }

        Stamp(host, store, actor);
        return reply;
    }

    private static PlatformEventInfo Query(PlatformEventInfo @event, IWaddleHostNext host, IStateStore store, ChatCommand command, string actor)
    {
        var target = actor;
        if (command.Args.Count > 0)
        {
            try
            {
                target = host.Identity.ResolveMention(command.Args[0]);
            }
            catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotFound or IdentityErrorKind.Ambiguous or IdentityErrorKind.Invalid)
            {
                host.Log.Info("lastseen.mention_unresolved", new LogFields().With("kind", ex.Kind.ToString()).Build());
                return Kit.Reply(@event, command, "Mention the person you want to look up, e.g. !lastseen @name");
            }
        }

        var stamp = Kit.ReadLong(store, KeyFor(target));
        var who = UserPlaceholder.For(target);
        if (stamp <= 0)
        {
            return Kit.Reply(@event, command, $"Have never seen {who}");
        }

        var seconds = Math.Max(0, ((long)host.Clock.NowMillis() - stamp) / 1000);
        host.Log.Info("lastseen.answered", new LogFields().With("self", target == actor).Build());
        return Kit.Reply(@event, command, $"{who} was last seen {CompoundDuration(seconds)} ago");
    }

    private static void Stamp(IWaddleHostNext host, IStateStore store, string actor) =>
        Kit.WriteLong(store, KeyFor(actor), (long)host.Clock.NowMillis(), StampTtlSeconds);

    internal static string KeyFor(string userUuid) => StateKey.Of("ls.u", StateKey.Hash(userUuid));

    /// <summary>The original's `ConvertToCompoundDuration` (weeks, days, hours, minutes), with a seconds fallback under one minute.</summary>
    internal static string CompoundDuration(long seconds)
    {
        if (seconds <= 0)
        {
            return "0 sec";
        }

        var span = TimeSpan.FromSeconds(seconds);
        var parts = new List<string>();
        var weeks = span.Days / 7;
        var days = span.Days % 7;
        if (weeks > 0)
        {
            parts.Add($"{weeks}w");
        }

        if (days > 0)
        {
            parts.Add($"{days}d");
        }

        if (span.Hours > 0)
        {
            parts.Add($"{span.Hours}h");
        }

        if (span.Minutes > 0)
        {
            parts.Add($"{span.Minutes}m");
        }

        return parts.Count == 0 ? $"{span.Seconds} sec" : string.Join(", ", parts);
    }
}

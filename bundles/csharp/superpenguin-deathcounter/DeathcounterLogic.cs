using System.Globalization;
using WaddleSdk.Chat;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinDeathcounter;

/// <summary>
/// `!death [+|-|reset|set N|game NAME]`, ported from PenguinTwitchBot's `Misc/DeathCounters.cs` (github.com/Psychoboy/PenguinTwitchBot,
/// MIT, used with permission -- see `bundle.yaml`'s `notice`). Anyone reads the per-game counter; moderators and the broadcaster change
/// it (`+`, `-` floored at 0, `reset`, `set N`).
///
/// <para><b>Deviations (documented).</b> (1) The original keys the counter on the channel's current Twitch game (a live Twitch API
/// lookup). No bundle can read that, so the counter is keyed on an ACTIVE GAME LABEL a moderator sets with `!death game NAME` (a bare
/// `!death game` clears it back to "this stream"); each label keeps its own count. (2) The original names the broadcaster; no
/// broadcaster identity reaches a bundle, so the reply says "The streamer". (3) The original writes a text file and broadcasts a
/// SignalR event for overlays; neither exists here (the overlay path is the platform's overlay service). (4) Role flags come from the
/// host-stamped `is_mod` / `is_broadcaster` booleans and fail CLOSED when absent (every Discord message today).</para>
///
/// <para><b>Replay.</b> A mutation is claimed once per (message id, platform, actor, command), so a redelivered event cannot change the
/// counter twice. kv has no compare-and-swap: two DIFFERENT messages racing on one counter can lose an update.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class DeathcounterLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-deathcounter";
    internal const string DefaultLabel = "this stream";
    internal const long MaxCount = 1_000_000_000;
    internal const string GameKey = "dc.game";

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.Is("death"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("death.refused", new LogFields().With("reason", "no_community").Build());
            return Kit.Reply(@event, command, "!death needs a community; this channel is not linked to one.");
        }

        var store = new KvStateStore(host.Kv);
        var label = ActiveLabel(store);
        var verb = command.Args.Count > 0 ? command.Args[0] : null;
        if (verb is "+" or "-" or "reset" or "set" or "game" && Kit.IsModerator(@event.PayloadJson))
        {
            var actor = Kit.TryActor(host, "death.actor_unlinked");
            if (actor is null)
            {
                return Kit.Reply(@event, command, "Link your account to this community before changing the death counter.");
            }

            if (Kit.ClaimOnce(host, store, "dc.m", @event, actor, command.Name))
            {
                var changed = Mutate(store, verb, command, label);
                if (verb == "game")
                {
                    label = ActiveLabel(store);
                }

                host.Log.Info("death.mutated", new LogFields().With("verb", verb).With("changed", changed).Build());
            }
            else
            {
                host.Log.Info("death.replay", new LogFields().With("verb", verb).Build());
            }
        }

        return Kit.Reply(@event, command, Describe(store, label));
    }

    private static bool Mutate(IStateStore store, string verb, ChatCommand command, string label)
    {
        if (verb == "game")
        {
            var name = Kit.Clean(string.Join(' ', command.Args.Skip(1)), 64);
            if (name.Length == 0)
            {
                store.Delete(GameKey);
            }
            else
            {
                store.Set(GameKey, System.Text.Encoding.UTF8.GetBytes(name), 0);
            }

            return true;
        }

        var key = CounterKey(label);
        var current = Kit.ReadLong(store, key);
        long next;
        switch (verb)
        {
            case "+":
                next = Math.Min(MaxCount, current + 1);
                break;
            case "-":
                next = Math.Max(0, current - 1);
                break;
            case "reset":
                next = 0;
                break;
            default:
                if (command.Args.Count < 2 || !long.TryParse(command.Args[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out var amount))
                {
                    return false;
                }

                next = Math.Clamp(amount, 0, MaxCount);
                break;
        }

        Kit.WriteLong(store, key, next);
        return true;
    }

    private static string Describe(IStateStore store, string label)
    {
        var count = Kit.ReadLong(store, CounterKey(label));
        return count == 0
            ? $"The streamer has not died in {label} YET"
            : $"The streamer has died {count} times in {label}";
    }

    private static string ActiveLabel(IStateStore store)
    {
        var bytes = store.Get(GameKey);
        var name = bytes is null ? string.Empty : System.Text.Encoding.UTF8.GetString(bytes);
        return name.Length == 0 ? DefaultLabel : name;
    }

    internal static string CounterKey(string label) => StateKey.Of("dc.c", StateKey.Hash(label.ToLowerInvariant()));
}

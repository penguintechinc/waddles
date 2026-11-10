using System.Globalization;
using System.Text;
using System.Text.Json;
using SuperPenguinShared;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinAudiocommand;

/// <summary>
/// `!sfx`, the command/sound policy ported from PenguinTwitchBot's `AudioCommand/AudioCommands.cs` and
/// `RunAudioCommandHandler.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT -- see `bundle.yaml`'s `notice`). The app owns the
/// policy: which sounds exist, who may play them and the per-user and global cooldowns. Playback is a browser-source page
/// (`overlay/index.html`) driven over the host overlay import; no audio is synthesized, stored or streamed by the bundle.
///
/// <para><b>Commands.</b> Anyone: `!sfx &lt;name&gt;` plays a registered, enabled sound; `!sfx list`. Moderator/broadcaster:
/// `add &lt;name&gt; &lt;file&gt; [userCooldown] [globalCooldown] [volume]`, `remove`, `enable`, `disable` (the upstream
/// add/refresh/disable/enable commands; the registry lives in kv so no refresh step exists). Upstream registered every sound as
/// its own chat command; consumes filters are static, so sounds are played through the single root `!sfx &lt;name&gt;`.</para>
///
/// <para><b>Policy kept from upstream.</b> A disabled sound and a command inside its cooldown are dropped silently; the user
/// and global cooldowns are independent and 0 disables either. A sound that is on cooldown for the user does not spend the
/// global slot. Deviation: upstream's global semaphore that serialised overlapping sounds is not ported (the page queues them).</para>
///
/// <para><b>Safety.</b> File names must match `[A-Za-z0-9_.-]{1,64}` with an mp3/ogg/wav extension and no `..`; the overlay
/// receives only that name, never a path or URL, and the page builds the URL from its own `sounds` base parameter. The
/// audio files are the community's own uploads; the upstream sample clips are not shipped.</para>
///
/// <para><b>Replay.</b> The play is recorded under the event's replay key after the cooldowns are claimed and before the push;
/// a redelivered event re-pushes the same `play_id` (the page ignores a repeated id) and claims nothing again. Overlay
/// refusals propagate (see the README host needs). Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).</para>
/// </summary>
public sealed class AudiocommandLogic : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-audiocommand";
    internal const string WidgetId = "superpenguin.audiocommand";
    internal const int MaxSounds = 200;
    internal const int MaxName = 24;
    internal const int MaxFile = 64;
    internal const uint MaxCooldownSeconds = 86_400;
    internal const int DefaultVolume = 100;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const int MaxListChars = 400;
    internal const string ModOnly = "only moderators and the broadcaster can do that.";
    internal const string Usage = "Usage: !sfx <name> | !sfx list | !sfx add <name> <file> [userCooldown] [globalCooldown] [volume] | remove|enable|disable <name>";

    private static readonly string[] Reserved = ["add", "remove", "enable", "disable", "list"];
    private static readonly string[] Extensions = [".mp3", ".ogg", ".wav"];

    private static string RegistryKey => StateKey.Of("sfx.reg");

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("sfx"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("sfx.refused", new LogFields().With("reason", "no_community").Build());
            return Say(@event, command, "!sfx needs a community; this channel is not linked to one.");
        }

        var store = new KvStateStore(host.Kv);
        var sub = command.Args.Count == 0 ? "" : command.Args[0].ToLowerInvariant();
        switch (sub)
        {
            case "":
                return Say(@event, command, Usage);
            case "list":
                return Say(@event, command, ListText(store));
            case "add" or "remove" or "enable" or "disable":
                if (!ChatPrivileges.IsModerator(@event.PayloadJson))
                {
                    host.Log.Info("sfx.refused", new LogFields().With("reason", "not_moderator").With("sub", sub).Build());
                    return Say(@event, command, ModOnly);
                }

                return sub == "add" ? Add(@event, command, store) : Toggle(@event, command, store, sub);
            default:
                return Play(@event, command, host, store, sub);
        }
    }

    private static PlatformEventInfo Add(PlatformEventInfo @event, ChatCommand command, KvStateStore store)
    {
        var args = command.Args;
        if (args.Count is < 3 or > 6 || !TryName(args[1], out var name) || Reserved.Contains(name))
        {
            return Say(@event, command, "Usage: !sfx add <name> <file> [userCooldown] [globalCooldown] [volume]; the name is 1 to 24 of a-z 0-9 _ - and not a subcommand.");
        }

        if (!IsValidFile(args[2]))
        {
            return Say(@event, command, "The file must be 1 to 64 characters of letters, digits, _ . - ending in .mp3, .ogg or .wav.");
        }

        if (!TryUint(args, 3, 0, MaxCooldownSeconds, out var userCooldown) || !TryUint(args, 4, 0, MaxCooldownSeconds, out var globalCooldown)
            || !TryUint(args, 5, DefaultVolume, 100, out var volume))
        {
            return Say(@event, command, $"Cooldowns are 0 to {MaxCooldownSeconds} seconds and the volume is 0 to 100.");
        }

        var registry = Load(store);
        var exists = registry.Sounds.Any(s => s.Name == name);
        if (!exists && registry.Sounds.Count >= MaxSounds)
        {
            return Say(@event, command, $"At most {MaxSounds} sounds can be registered; remove one first.");
        }

        registry.Sounds.RemoveAll(s => s.Name == name);
        registry.Sounds.Add(new SoundDef(name, args[2], userCooldown, globalCooldown, (int)volume, false));
        store.SetJson(RegistryKey, registry, AudiocommandJsonContext.Default.SoundRegistry);
        return Say(@event, command, $"Sound {name} {(exists ? "updated" : "added")}.");
    }

    private static PlatformEventInfo Toggle(PlatformEventInfo @event, ChatCommand command, KvStateStore store, string sub)
    {
        if (command.Args.Count != 2 || !TryName(command.Args[1], out var name))
        {
            return Say(@event, command, $"Usage: !sfx {sub} <name>");
        }

        var registry = Load(store);
        var sound = registry.Sounds.FirstOrDefault(s => s.Name == name);
        if (sound is null)
        {
            return Say(@event, command, $"No sound named {name}.");
        }

        if (sub == "remove")
        {
            registry.Sounds.Remove(sound);
        }
        else
        {
            registry.Sounds[registry.Sounds.IndexOf(sound)] = sound with { Disabled = sub == "disable" };
        }

        store.SetJson(RegistryKey, registry, AudiocommandJsonContext.Default.SoundRegistry);
        return Say(@event, command, sub switch { "remove" => $"Sound {name} removed.", "enable" => $"Sound {name} enabled.", _ => $"Sound {name} disabled." });
    }

    private static string ListText(KvStateStore store)
    {
        var names = Load(store).Sounds.Where(s => !s.Disabled).Select(s => s.Name).ToList();
        if (names.Count == 0)
        {
            return "No sounds are registered.";
        }

        var text = "Sounds: " + string.Join(", ", names);
        return text.Length <= MaxListChars ? text : text[..(MaxListChars - 3)] + "...";
    }

    private static PlatformEventInfo? Play(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, string name)
    {
        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("sfx.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Say(@event, command, "Link your account to this community before playing sounds.");
        }

        var outcomes = new IdempotentOutcome(store, "sfx", OutcomeTtlSeconds);
        var messageId = host.Context.MessageId;
        var replay = outcomes.TryReplay(messageId, @event.Platform, actor, command.Name, AudiocommandJsonContext.Default.PlayOutcome,
            r => r.Actor == actor && r.Command == command.Name);
        var lookup = replay?.Name ?? name;
        var sound = Load(store).Sounds.FirstOrDefault(s => s.Name == lookup);
        if (sound is null)
        {
            return Say(@event, command, replay is null ? $"No sound named {name}. Try !sfx list." : "That sound was removed.");
        }

        if (sound.Disabled)
        {
            return null; // upstream drops a disabled sound silently.
        }

        string playId;
        if (replay is not null)
        {
            playId = replay.PlayId;
        }
        else
        {
            if (!TakeCooldowns(store, sound, actor))
            {
                return null; // inside a cooldown window: dropped silently, as upstream.
            }

            playId = StateKey.Hash($"{messageId}|{@event.Platform}|{actor}|{command.Name}")[..16];
            outcomes.Record(messageId, @event.Platform, actor, command.Name, new PlayOutcome(actor, command.Name, sound.Name, playId), AudiocommandJsonContext.Default.PlayOutcome);
        }

        OverlayAlert.Push(host.Overlay, WidgetId, "audiocommand.play", PlayJson(sound, playId), null);
        host.Log.Info("sfx.played", new LogFields().With("replay", replay is not null).Build());
        return null;
    }

    /// <summary>Claims the user then the global cooldown; a failed global claim hands the user's back. 0 disables a cooldown.</summary>
    private static bool TakeCooldowns(KvStateStore store, SoundDef sound, string actor)
    {
        var userScope = "sfxu." + sound.Name;
        var globalScope = "sfxg." + sound.Name;
        if (sound.UserCooldownSeconds > 0 && !StateCooldown.TryAcquire(store, userScope, actor, sound.UserCooldownSeconds))
        {
            return false;
        }

        if (sound.GlobalCooldownSeconds > 0 && !StateCooldown.TryAcquire(store, globalScope, "global", sound.GlobalCooldownSeconds))
        {
            if (sound.UserCooldownSeconds > 0)
            {
                StateCooldown.Release(store, userScope, actor);
            }

            return false;
        }

        return true;
    }

    /// <summary>True for a bare file name `[A-Za-z0-9_.-]{1,64}` with an mp3/ogg/wav extension, no leading dot and no `..`.</summary>
    internal static bool IsValidFile(string file) =>
        file.Length is >= 1 and <= MaxFile
        && !file.StartsWith('.')
        && !file.Contains("..", StringComparison.Ordinal)
        && file.All(c => c is >= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or '_' or '.' or '-')
        && Extensions.Any(e => file.EndsWith(e, StringComparison.OrdinalIgnoreCase));

    private static bool TryName(string raw, out string name)
    {
        name = raw.ToLowerInvariant();
        return name.Length is >= 1 and <= MaxName && name.All(c => c is >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '-');
    }

    /// <summary>Parses optional argument <paramref name="index"/> as a whole number in [min, max]; absent = <paramref name="min"/> (or the default passed as min for the volume).</summary>
    private static bool TryUint(IReadOnlyList<string> args, int index, uint absentValue, uint max, out uint value)
    {
        value = absentValue;
        return index >= args.Count || uint.TryParse(args[index], NumberStyles.None, CultureInfo.InvariantCulture, out value) && value <= max;
    }

    private static SoundRegistry Load(KvStateStore store) =>
        store.GetJson(RegistryKey, AudiocommandJsonContext.Default.SoundRegistry) ?? new SoundRegistry([]);

    private static PlatformEventInfo Say(PlatformEventInfo @event, ChatCommand command, string text) => Replies.Reply(@event, command, text);

    private static string PlayJson(SoundDef sound, string playId)
    {
        using var stream = new MemoryStream();
        using (var w = new Utf8JsonWriter(stream))
        {
            w.WriteStartObject();
            w.WriteString("name", sound.Name);
            w.WriteString("file", sound.File);
            w.WriteNumber("volume", sound.Volume);
            w.WriteString("play_id", playId);
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(stream.ToArray());
    }
}

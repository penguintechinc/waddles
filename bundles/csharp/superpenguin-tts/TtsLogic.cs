using System.Globalization;
using System.Text;
using System.Text.Json;
using SuperPenguinShared;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinTts;

/// <summary>
/// `!tts` and `!ttsvoice`, the command policy ported from PenguinTwitchBot's `TTS/TTSService.cs`
/// (`OnCommand`, `SayMessage`, per-user voices; github.com/Psychoboy/PenguinTwitchBot, MIT -- see `bundle.yaml`'s `notice`).
/// The app owns the policy (who may speak, voice choice, text hygiene, cooldown); synthesis belongs to the media SERVICE and
/// playback to the browser-source page (`overlay/index.html`). Upstream's Piper/Kokoro/Google synthesis, TTS player, queue and
/// file-cleanup infrastructure are deliberately NOT ported (duplicate synthesis/player infrastructure).
///
/// <para><b>Commands.</b> `!tts &lt;text&gt;` speaks "{user} says &lt;text&gt;" in the user's chosen voice, else a random registered
/// voice. `!ttsvoice list|set &lt;id&gt;|clear` for everyone; `!ttsvoice add|remove &lt;id&gt;` for moderators/broadcaster
/// (fail-closed badge read).</para>
///
/// <para><b>Text hygiene.</b> Control characters and braces are stripped (a braced token could smuggle a host `{user:uuid}`
/// placeholder), whitespace is collapsed, empty text is refused and text over <see cref="MaxTextChars"/> is refused. The
/// speaker is the host `{user:uuid}` placeholder; the media service must render placeholders before synthesis (README host needs).</para>
///
/// <para><b>Fail loud.</b> Synthesis is the service tool `waddles.core.service.media/tts_synthesize` (args `{text, voice}`, result
/// `{url}`). That tool does not exist yet. When the call is refused as unavailable, not granted or forbidden, the bundle logs an
/// Error, tells chat exactly why, releases the user's cooldown and does NOT fall back to any other synthesis. A result
/// without an https `url` (including a bare handle, which this app has no permission to resolve) throws.
/// No voices registered is also a refusal, never a default voice.</para>
///
/// <para><b>Replay.</b> Voice and text are recorded under the event's replay key after validation and the cooldown, before the
/// tool call; a redelivered event speaks the same thing, with the tool call carrying a stable idempotency key. The overlay
/// receives `play_id` so the page ignores a repeat. Overlay refusals propagate. Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).</para>
/// </summary>
public sealed class TtsLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-tts";
    internal const string WidgetId = "superpenguin.tts";
    internal const string MediaTarget = "waddles.core.service.media";
    internal const string SynthesizeTool = "tts_synthesize";
    internal const int MaxTextChars = 200;
    internal const int MaxVoices = 50;
    internal const int MaxVoiceId = 48;
    internal const uint CooldownSeconds = 30;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const string ModOnly = "only moderators and the broadcaster can do that.";
    internal const string NoVoices = "TTS is unavailable: no TTS voices are registered.";
    internal const string Unavailable = "TTS is unavailable: the media service does not provide tts_synthesize.";
    internal const string TtsUsage = "Usage: !tts <message> (up to 200 characters)";
    internal const string VoiceUsage = "Usage: !ttsvoice list | set <id> | clear | add <id> | remove <id> (add/remove are moderator only)";

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    private static string VoicesKey => StateKey.Of("tts.voices");

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.IsAny("tts", "ttsvoice"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("tts.refused", new LogFields().With("reason", "no_community").Build());
            return Say(@event, command, "!tts needs a community; this channel is not linked to one.");
        }

        var store = new KvStateStore(host.Kv);
        return command.Is("tts") ? Speak(@event, command, host, store) : Voice(@event, command, host, store);
    }

    private PlatformEventInfo? Speak(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        var text = Clean(string.Join(' ', command.Args));
        if (text.Length == 0 || text.Length > MaxTextChars)
        {
            return Say(@event, command, TtsUsage);
        }

        var actor = TryActor(host, "tts");
        if (actor is null)
        {
            return Say(@event, command, "Link your account to this community before using TTS.");
        }

        var outcomes = new IdempotentOutcome(store, "tts", OutcomeTtlSeconds);
        var messageId = host.Context.MessageId;
        var replay = outcomes.TryReplay(messageId, @event.Platform, actor, command.Name, TtsJsonContext.Default.TtsOutcome,
            r => r.Actor == actor && r.Command == command.Name);
        TtsOutcome outcome;
        if (replay is not null)
        {
            outcome = replay;
        }
        else
        {
            var voices = LoadVoices(store).Voices;
            if (voices.Count == 0)
            {
                host.Log.Error("tts.refused", new LogFields().With("reason", "no_voices").Build());
                return Say(@event, command, NoVoices);
            }

            if (!StateCooldown.TryAcquire(store, "tts", actor, CooldownSeconds))
            {
                return null; // inside the per-user cooldown: dropped silently, like upstream's command cooldown.
            }

            var chosen = store.GetJson(UserVoiceKey(actor), TtsJsonContext.Default.UserVoice)?.Voice;
            var voice = chosen is not null && voices.Contains(chosen) ? chosen : voices[_nextInt(0, voices.Count - 1)];
            var playId = StateKey.Hash($"{messageId}|{@event.Platform}|{actor}|{command.Name}")[..16];
            outcome = new TtsOutcome(actor, command.Name, voice, $"{UserPlaceholder.For(actor)} says {text}", playId);
            outcomes.Record(messageId, @event.Platform, actor, command.Name, outcome, TtsJsonContext.Default.TtsOutcome);
        }

        string url;
        try
        {
            url = Synthesize(host, outcome, messageId);
        }
        catch (WaddleToolException ex) when (ex.Kind is ToolErrorKind.TargetUnavailable or ToolErrorKind.NotGranted or ToolErrorKind.Forbidden)
        {
            host.Log.Error("tts.synthesis_unavailable", new LogFields().With("code", ex.Code).Build());
            StateCooldown.Release(store, "tts", actor);
            outcomes.Discard(messageId, @event.Platform, actor, command.Name);
            return Say(@event, command, Unavailable);
        }

        OverlayAlert.Push(host.Overlay, WidgetId, "tts.play", PlayJson(url, outcome), null);
        host.Log.Info("tts.spoken", new LogFields().With("replay", replay is not null).With("chars", outcome.Text.Length).Build());
        return null;
    }

    /// <summary>Calls the media service tool and returns the audio URL; anything but an https URL is a contract violation and throws.</summary>
    private static string Synthesize(IWaddleHostNext host, TtsOutcome outcome, string messageId)
    {
        var result = host.Tool.Invoke(MediaTarget, SynthesizeTool, new TtsSynthesizeArgs(outcome.Text, outcome.Voice),
            TtsJsonContext.Default.TtsSynthesizeArgs, TtsJsonContext.Default.TtsSynthesizeResult,
            StateKey.Hash(messageId + "\u001f" + outcome.PlayId));
        if (result.Url is null || !result.Url.StartsWith("https://", StringComparison.Ordinal))
        {
            throw new WaddleToolException(ToolErrorKind.InvalidArgs, $"{MediaTarget}/{SynthesizeTool} returned no https url");
        }

        return result.Url;
    }

    private PlatformEventInfo? Voice(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        var sub = command.Args.Count == 0 ? "" : command.Args[0].ToLowerInvariant();
        switch (sub)
        {
            case "list":
                var voices = LoadVoices(store).Voices;
                return Say(@event, command, voices.Count == 0 ? "No TTS voices are registered." : "Voices: " + string.Join(", ", voices));
            case "add" or "remove":
                if (!ChatPrivileges.IsModerator(@event.PayloadJson))
                {
                    host.Log.Info("tts.refused", new LogFields().With("reason", "not_moderator").Build());
                    return Say(@event, command, ModOnly);
                }

                return sub == "add" ? AddVoice(@event, command, store) : RemoveVoice(@event, command, store);
            case "set" or "clear":
                return ChooseVoice(@event, command, host, store, sub);
            default:
                return Say(@event, command, VoiceUsage);
        }
    }

    private static PlatformEventInfo AddVoice(PlatformEventInfo @event, ChatCommand command, KvStateStore store)
    {
        if (command.Args.Count != 2 || !IsVoiceId(command.Args[1]))
        {
            return Say(@event, command, $"Usage: !ttsvoice add <id> (1 to {MaxVoiceId} of letters, digits, _ . -)");
        }

        var list = LoadVoices(store);
        var id = command.Args[1];
        if (list.Voices.Contains(id))
        {
            return Say(@event, command, $"Voice {id} is already registered.");
        }

        if (list.Voices.Count >= MaxVoices)
        {
            return Say(@event, command, $"At most {MaxVoices} voices can be registered.");
        }

        list.Voices.Add(id);
        store.SetJson(VoicesKey, list, TtsJsonContext.Default.VoiceList);
        return Say(@event, command, $"Voice {id} registered.");
    }

    private static PlatformEventInfo RemoveVoice(PlatformEventInfo @event, ChatCommand command, KvStateStore store)
    {
        var list = LoadVoices(store);
        if (command.Args.Count != 2 || !list.Voices.Remove(command.Args[1]))
        {
            return Say(@event, command, "Usage: !ttsvoice remove <id> (a registered voice)");
        }

        store.SetJson(VoicesKey, list, TtsJsonContext.Default.VoiceList);
        return Say(@event, command, $"Voice {command.Args[1]} removed.");
    }

    private static PlatformEventInfo? ChooseVoice(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, string sub)
    {
        var actor = TryActor(host, "ttsvoice");
        if (actor is null)
        {
            return Say(@event, command, "Link your account to this community before choosing a voice.");
        }

        if (sub == "clear")
        {
            store.Delete(UserVoiceKey(actor));
            return Say(@event, command, "Your voice choice was cleared; a random voice will be used.");
        }

        if (command.Args.Count != 2 || !LoadVoices(store).Voices.Contains(command.Args[1]))
        {
            return Say(@event, command, "Usage: !ttsvoice set <id> (see !ttsvoice list)");
        }

        store.SetJson(UserVoiceKey(actor), new UserVoice(command.Args[1]), TtsJsonContext.Default.UserVoice);
        return Say(@event, command, $"Your voice is now {command.Args[1]}.");
    }

    private static string UserVoiceKey(string actor) => StateKey.Of("tts.uv", StateKey.Hash(actor));

    private static VoiceList LoadVoices(KvStateStore store) =>
        store.GetJson(VoicesKey, TtsJsonContext.Default.VoiceList) ?? new VoiceList([]);

    /// <summary>True for a voice id of 1..48 characters from `[A-Za-z0-9_.-]`.</summary>
    internal static bool IsVoiceId(string id) =>
        id.Length is >= 1 and <= MaxVoiceId && id.All(c => c is >= 'a' and <= 'z' or >= 'A' and <= 'Z' or >= '0' and <= '9' or '_' or '.' or '-');

    /// <summary>Drops control characters and braces (no host-placeholder smuggling), collapses whitespace and trims.</summary>
    internal static string Clean(string raw)
    {
        var sb = new StringBuilder(raw.Length);
        foreach (var c in raw)
        {
            if (char.IsWhiteSpace(c))
            {
                sb.Append(' ');
            }
            else if (!char.IsControl(c) && c != '{' && c != '}')
            {
                sb.Append(c);
            }
        }

        return string.Join(' ', sb.ToString().Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
    }

    private static string? TryActor(IWaddleHostNext host, string op)
    {
        try
        {
            return host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info(op + ".refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return null;
        }
    }

    private static PlatformEventInfo Say(PlatformEventInfo @event, ChatCommand command, string text) => Replies.Reply(@event, command, text);

    private static string PlayJson(string url, TtsOutcome outcome)
    {
        using var stream = new MemoryStream();
        using (var w = new Utf8JsonWriter(stream))
        {
            w.WriteStartObject();
            w.WriteString("url", url);
            w.WriteString("voice", outcome.Voice);
            w.WriteString("play_id", outcome.PlayId);
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(stream.ToArray());
    }
}

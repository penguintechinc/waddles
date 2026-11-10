using System.Text;
using System.Text.Json;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Overlay;
using WaddleSdk.Tests;
using WaddleSdk.Tool;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinTts.Tests.World;

namespace WaddleBundleSuperpenguinTts.Tests;

/// <summary>An overlay whose every call is refused like the host does today (`not-implemented`).</summary>
public sealed class RefusingOverlay : IOverlayClient
{
    public TransportResultInfo RegisterWidget(OverlayWidget widget) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("not_implemented", "overlay host capability has no handler"));

    public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("not_implemented", "overlay host capability has no handler"));
}

public class TtsLogicTests
{
    private const string AudioUrl = "https://media.invalid/tts/1.mp3";

    private static World NewWorld() => new(TtsLogic.FlagKey);

    private static void Synth(World w, string resultJson = "{\"url\":\"" + AudioUrl + "\"}") =>
        w.Tools.Handlers[(TtsLogic.MediaTarget, TtsLogic.SynthesizeTool)] = _ => Encoding.UTF8.GetBytes(resultJson);

    private static PlatformEventInfo Ev(string text, bool mod)
    {
        var json = "{\"text\":\"" + text + "\",\"channel_id\":\"chan-1\"" + (mod ? ",\"is_mod\":true" : "") + "}";
        return new PlatformEventInfo("twitch", "chat.message", "platform-actor", json, "2026-11-07T00:00:00.000Z");
    }

    private static string? Run(World w, TtsLogic l, string actor, string text, string id, bool mod = false)
    {
        var reply = l.Run(Ev(text, mod), w.HostFor(actor, id));
        return reply is null ? null : TextOf(reply);
    }

    private static JsonElement LastAmount(World w)
    {
        var push = w.Pushed.Pushes[^1];
        Assert.Equal(("superpenguin.tts", OverlaySurface.AlertBox), (push.WidgetId, push.Surface));
        var alert = JsonDocument.Parse(push.Payload).RootElement.GetProperty("alert");
        Assert.Equal("superpenguin.tts.play", alert.GetProperty("alert_type").GetString());
        return alert.GetProperty("amount").Clone();
    }

    private static void Register(World w, TtsLogic l, params string[] voices)
    {
        foreach (var v in voices)
        {
            Assert.Contains("registered", Run(w, l, Dave, "!ttsvoice add " + v, "reg" + v, mod: true));
        }
    }

    [Fact]
    public void other_commands_and_a_disabled_flag_are_ignored_and_a_missing_community_is_refused()
    {
        var w = NewWorld();
        var l = new TtsLogic();

        Assert.Null(Run(w, l, Alice, "hello", "m1"));
        Assert.Null(Run(w, l, Alice, "!other", "m2"));
        w.Flags.Overrides[TtsLogic.FlagKey] = false;
        Assert.Null(Run(w, l, Alice, "!tts hi", "m3"));
        w.Flags.Overrides[TtsLogic.FlagKey] = true;

        var noCommunity = l.Run(Ev("!tts hi", false), w.HostFor(Alice, "m4", community: null));
        Assert.Contains("needs a community", TextOf(noCommunity!));
    }

    [Theory]
    [InlineData("!tts")]
    [InlineData("!tts    ")]
    [InlineData("!tts {} {}")]
    public void empty_text_is_refused_with_the_usage(string text)
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v1");

        Assert.Equal(TtsLogic.TtsUsage, Run(w, l, Alice, text, "m1"));
        Assert.Empty(w.Tools.Calls);
    }

    [Fact]
    public void text_over_two_hundred_characters_is_refused()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v1");

        Assert.Equal(TtsLogic.TtsUsage, Run(w, l, Alice, "!tts " + new string('a', 201), "m1"));
        Synth(w);
        Assert.Null(Run(w, l, Alice, "!tts " + new string('a', 200), "m2"));
    }

    [Fact]
    public void with_no_voices_registered_it_refuses_loudly_and_never_calls_synthesis()
    {
        var w = NewWorld();
        var l = new TtsLogic();

        Assert.Equal(TtsLogic.NoVoices, Run(w, l, Alice, "!tts hello", "m1"));

        Assert.Empty(w.Tools.Calls);
        Assert.Contains(w.Log.Writes, x => x.Level == WaddleLogLevel.Error && x.FieldsJson.Contains("no_voices"));
    }

    [Fact]
    public void a_message_is_synthesized_through_the_media_service_and_played_on_the_overlay()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "af_heart");
        Synth(w);

        Assert.Null(Run(w, l, Alice, "!tts  hello   {user:evil} there", "m1"));

        var call = Assert.Single(w.Tools.Calls);
        Assert.Equal((TtsLogic.MediaTarget, TtsLogic.SynthesizeTool), (call.Target, call.Tool));
        Assert.Equal($"{{\"text\":\"{{user:{Alice}}} says hello user:evil there\",\"voice\":\"af_heart\"}}", call.Args);
        Assert.False(string.IsNullOrEmpty(call.Key));
        var amount = LastAmount(w);
        Assert.Equal(AudioUrl, amount.GetProperty("url").GetString());
        Assert.Equal("af_heart", amount.GetProperty("voice").GetString());
        Assert.Equal(16, amount.GetProperty("play_id").GetString()!.Length);
    }

    [Fact]
    public void the_synthesis_contract_is_pinned()
    {
        var args = JsonSerializer.Serialize(new TtsSynthesizeArgs("hi", "v"), TtsJsonContext.Default.TtsSynthesizeArgs);
        Assert.Equal("{\"text\":\"hi\",\"voice\":\"v\"}", args);

        var ok = JsonSerializer.Deserialize("{\"url\":\"https://a/b.mp3\"}", TtsJsonContext.Default.TtsSynthesizeResult)!;
        Assert.Equal(("https://a/b.mp3", null), (ok.Url, ok.Handle));
        var handleOnly = JsonSerializer.Deserialize("{\"handle\":\"h1\"}", TtsJsonContext.Default.TtsSynthesizeResult)!;
        Assert.Equal((null, "h1"), (handleOnly.Url, handleOnly.Handle));
    }

    [Fact]
    public void a_chosen_voice_beats_the_random_pick()
    {
        var w = NewWorld();
        var l = new TtsLogic(new ScriptedRandom(0).Next);
        Register(w, l, "v0", "v1");
        Synth(w);
        Assert.Contains("Your voice is now v1", Run(w, l, Alice, "!ttsvoice set v1", "s1"));

        Run(w, l, Alice, "!tts one", "m1");

        Assert.Equal("v1", LastAmount(w).GetProperty("voice").GetString());
    }

    [Fact]
    public void a_choice_that_was_unregistered_since_falls_back_to_a_random_registered_voice()
    {
        var w = NewWorld();
        var l = new TtsLogic(new ScriptedRandom(0).Next);
        Register(w, l, "v0", "v1");
        Synth(w);
        Run(w, l, Alice, "!ttsvoice set v0", "s1");
        Run(w, l, Dave, "!ttsvoice remove v0", "rm", mod: true);

        Run(w, l, Alice, "!tts one", "m1");

        Assert.Equal("v1", LastAmount(w).GetProperty("voice").GetString());
    }

    [Fact]
    public void without_a_choice_a_random_registered_voice_is_picked()
    {
        var w = NewWorld();
        var l = new TtsLogic(new ScriptedRandom(1).Next);
        Register(w, l, "v0", "v1");
        Synth(w);

        Run(w, l, Alice, "!tts hello", "m1");

        Assert.Equal("v1", LastAmount(w).GetProperty("voice").GetString());
    }

    [Fact]
    public void the_default_random_source_picks_a_registered_voice()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0", "v1");
        Synth(w);

        Run(w, l, Alice, "!tts hello", "m1");

        Assert.Contains(LastAmount(w).GetProperty("voice").GetString(), new[] { "v0", "v1" });
    }

    [Fact]
    public void the_per_user_cooldown_drops_a_repeat_silently_but_not_other_users()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");
        Synth(w);

        Run(w, l, Alice, "!tts one", "m1");
        Assert.Null(Run(w, l, Alice, "!tts two", "m2"));
        Run(w, l, Bob, "!tts three", "m3");

        Assert.Equal(2, w.Tools.Calls.Count);
    }

    [Theory]
    [InlineData(ToolErrorKind.NotGranted)]
    [InlineData(ToolErrorKind.TargetUnavailable)]
    [InlineData(ToolErrorKind.Forbidden)]
    public void when_the_media_service_cannot_synthesize_it_says_so_releases_the_cooldown_and_never_falls_back(ToolErrorKind kind)
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");
        w.Tools.Handlers[(TtsLogic.MediaTarget, TtsLogic.SynthesizeTool)] = _ => throw new WaddleToolException(kind, "no");

        Assert.Equal(TtsLogic.Unavailable, Run(w, l, Alice, "!tts hello", "m1"));

        Assert.Contains(w.Log.Writes, x => x.Level == WaddleLogLevel.Error && x.Message == "tts.synthesis_unavailable");
        Assert.Empty(w.Pushed.Pushes);
        Synth(w);
        Run(w, l, Alice, "!tts again", "m2");
        Assert.Single(w.Pushed.Pushes);
    }

    [Fact]
    public void the_tool_is_absent_by_default_so_the_bundle_refuses_until_the_service_exists()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");

        Assert.Equal(TtsLogic.Unavailable, Run(w, l, Alice, "!tts hello", "m1"));
        Assert.Single(w.Tools.Calls);
    }

    [Fact]
    public void other_tool_failures_propagate()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");
        w.Tools.Handlers[(TtsLogic.MediaTarget, TtsLogic.SynthesizeTool)] = _ => throw new WaddleToolException(ToolErrorKind.Timeout, "slow");

        Assert.Throws<WaddleToolException>(() => Run(w, l, Alice, "!tts hello", "m1"));
    }

    [Theory]
    [InlineData("{\"url\":\"http://insecure.invalid/a.mp3\"}")]
    [InlineData("{\"handle\":\"h1\"}")]
    [InlineData("{}")]
    public void a_result_without_an_https_url_is_a_contract_violation(string result)
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");
        Synth(w, result);

        var ex = Assert.Throws<WaddleToolException>(() => Run(w, l, Alice, "!tts hello", "m1"));
        Assert.Equal(ToolErrorKind.InvalidArgs, ex.Kind);
        Assert.Empty(w.Pushed.Pushes);
    }

    [Fact]
    public void a_redelivered_event_speaks_the_same_voice_and_text_with_the_same_play_id()
    {
        var w = NewWorld();
        var l = new TtsLogic(new ScriptedRandom(0, 1).Next);
        Register(w, l, "v0", "v1");
        Synth(w);

        Run(w, l, Alice, "!tts hello", "m1");
        Run(w, l, Alice, "!tts hello", "m1");

        Assert.Equal(2, w.Tools.Calls.Count);
        Assert.Equal(w.Tools.Calls[0].Args, w.Tools.Calls[1].Args);
        Assert.Equal(w.Tools.Calls[0].Key, w.Tools.Calls[1].Key);
        Assert.Equal(2, w.Pushed.Pushes.Count);
        Assert.Equal(JsonDocument.Parse(w.Pushed.Pushes[0].Payload).RootElement.ToString(), JsonDocument.Parse(w.Pushed.Pushes[1].Payload).RootElement.ToString());
    }

    [Fact]
    public void an_overlay_refusal_fails_loud_and_a_redelivery_recovers()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");
        Synth(w);
        w.Overlay = new RefusingOverlay();

        var ex = Assert.Throws<WaddleTransportException>(() => Run(w, l, Alice, "!tts hello", "m1"));
        Assert.Equal("not_implemented", ex.Error.Code);

        w.Overlay = new FakeOverlayClient();
        Assert.Null(Run(w, l, Alice, "!tts hello", "m1"));
        Assert.Single(w.Pushed.Pushes);
    }

    [Fact]
    public void speaking_needs_a_linked_account_and_a_message_id()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v0");
        Synth(w);

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, l, Alice, "!tts hi", "m1"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Run(w, l, Alice, "!tts hi", "m2"));
        w.ActorFailure = null;
        Assert.Throws<ArgumentException>(() => Run(w, l, Alice, "!tts hi", " "));
    }

    [Fact]
    public void voice_registration_is_moderator_only_validated_and_capped()
    {
        var w = NewWorld();
        var l = new TtsLogic();

        Assert.Equal("No TTS voices are registered.", Run(w, l, Alice, "!ttsvoice list", "m0"));
        Assert.Equal(TtsLogic.ModOnly, Run(w, l, Alice, "!ttsvoice add v1", "m1"));
        Assert.Equal(TtsLogic.ModOnly, Run(w, l, Alice, "!ttsvoice remove v1", "m2"));
        Assert.StartsWith("Usage: !ttsvoice add", Run(w, l, Dave, "!ttsvoice add", "m3", mod: true));
        Assert.StartsWith("Usage: !ttsvoice add", Run(w, l, Dave, "!ttsvoice add bad/id", "m4", mod: true));
        Assert.Equal("Voice v1 registered.", Run(w, l, Dave, "!ttsvoice add v1", "m5", mod: true));
        Assert.Equal("Voice v1 is already registered.", Run(w, l, Dave, "!ttsvoice add v1", "m6", mod: true));
        Assert.Equal("Voices: v1", Run(w, l, Alice, "!ttsvoice list", "m7"));

        for (var i = 0; i < TtsLogic.MaxVoices - 1; i++)
        {
            Run(w, l, Dave, $"!ttsvoice add x{i}", "x" + i, mod: true);
        }

        Assert.Contains("At most 50", Run(w, l, Dave, "!ttsvoice add overflow", "m8", mod: true));
    }

    [Fact]
    public void voice_removal_needs_a_registered_voice()
    {
        var w = NewWorld();
        var l = new TtsLogic();
        Register(w, l, "v1");

        Assert.StartsWith("Usage: !ttsvoice remove", Run(w, l, Dave, "!ttsvoice remove", "m1", mod: true));
        Assert.StartsWith("Usage: !ttsvoice remove", Run(w, l, Dave, "!ttsvoice remove nope", "m2", mod: true));
        Assert.Equal("Voice v1 removed.", Run(w, l, Dave, "!ttsvoice remove v1", "m3", mod: true));
        Assert.Equal("No TTS voices are registered.", Run(w, l, Dave, "!ttsvoice list", "m4"));
    }

    [Fact]
    public void users_set_and_clear_their_own_voice_from_the_registered_list()
    {
        var w = NewWorld();
        var l = new TtsLogic(new ScriptedRandom(1).Next);
        Register(w, l, "v0", "v1");
        Synth(w);

        Assert.StartsWith("Usage: !ttsvoice set", Run(w, l, Alice, "!ttsvoice set", "m1"));
        Assert.StartsWith("Usage: !ttsvoice set", Run(w, l, Alice, "!ttsvoice set unknown", "m2"));
        Assert.Equal("Your voice is now v0.", Run(w, l, Alice, "!ttsvoice set v0", "m3"));
        Assert.Contains("cleared", Run(w, l, Alice, "!ttsvoice clear", "m4"));

        Run(w, l, Alice, "!tts hello", "m5");
        Assert.Equal("v1", LastAmount(w).GetProperty("voice").GetString());
    }

    [Fact]
    public void choosing_a_voice_needs_a_linked_account_and_unknown_subcommands_get_the_usage()
    {
        var w = NewWorld();
        var l = new TtsLogic();

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, l, Alice, "!ttsvoice set v1", "m1"));
        w.ActorFailure = null;
        Assert.Equal(TtsLogic.VoiceUsage, Run(w, l, Alice, "!ttsvoice", "m2"));
        Assert.Equal(TtsLogic.VoiceUsage, Run(w, l, Alice, "!ttsvoice nonsense", "m3"));
    }

    [Theory]
    [InlineData("af_heart", true)]
    [InlineData("a.b-c_1", true)]
    [InlineData("", false)]
    [InlineData("bad id", false)]
    [InlineData("bad/id", false)]
    public void voice_ids_are_bare_identifiers(string id, bool valid) => Assert.Equal(valid, TtsLogic.IsVoiceId(id));

    [Fact]
    public void voice_ids_over_the_limit_are_refused() => Assert.False(TtsLogic.IsVoiceId(new string('a', TtsLogic.MaxVoiceId + 1)));

    [Theory]
    [InlineData("  a\tb   {x}  ", "a b x")]
    [InlineData("", "")]
    public void clean_strips_braces_controls_and_extra_spaces(string raw, string expected) => Assert.Equal(expected, TtsLogic.Clean(raw));

    [Fact]
    public void a_corrupt_voice_list_is_treated_as_empty()
    {
        var w = NewWorld();
        w.Kv.Set("tts.voices", [7, 7], 0);

        Assert.Equal("No TTS voices are registered.", Run(w, new TtsLogic(), Alice, "!ttsvoice list", "m1"));
    }
}

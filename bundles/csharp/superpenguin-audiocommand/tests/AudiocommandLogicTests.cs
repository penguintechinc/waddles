using System.Text.Json;
using WaddleSdk.Identity;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinAudiocommand.Tests.World;

namespace WaddleBundleSuperpenguinAudiocommand.Tests;

/// <summary>An overlay whose every call is refused like the host does today (`not-implemented`).</summary>
public sealed class RefusingOverlay : IOverlayClient
{
    public TransportResultInfo RegisterWidget(OverlayWidget widget) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("not_implemented", "overlay host capability has no handler"));

    public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("not_implemented", "overlay host capability has no handler"));
}

public class AudiocommandLogicTests
{
    private static World NewWorld() => new(AudiocommandLogic.FlagKey);

    private static PlatformEventInfo Ev(string text, bool mod)
    {
        var json = "{\"text\":\"" + text + "\",\"channel_id\":\"chan-1\"" + (mod ? ",\"is_mod\":true" : "") + "}";
        return new PlatformEventInfo("twitch", "chat.message", "platform-actor", json, "2026-11-07T00:00:00.000Z");
    }

    private static string? Run(World w, string actor, string text, string id, bool mod = true)
    {
        var reply = new AudiocommandLogic().Run(Ev(text, mod), w.HostFor(actor, id));
        return reply is null ? null : TextOf(reply);
    }

    private static JsonElement LastAmount(World w)
    {
        var push = w.Pushed.Pushes[^1];
        Assert.Equal(("superpenguin.audiocommand", OverlaySurface.AlertBox), (push.WidgetId, push.Surface));
        var alert = JsonDocument.Parse(push.Payload).RootElement.GetProperty("alert");
        Assert.Equal("superpenguin.audiocommand.play", alert.GetProperty("alert_type").GetString());
        return alert.GetProperty("amount").Clone();
    }

    private static void Add(World w, string spec, string id = "add") =>
        Assert.Contains("added", Run(w, Alice, "!sfx add " + spec, id));

    [Fact]
    public void other_commands_and_a_disabled_flag_are_ignored_and_a_missing_community_is_refused()
    {
        var w = NewWorld();

        Assert.Null(Run(w, Alice, "hello", "m1"));
        Assert.Null(Run(w, Alice, "!other", "m2"));
        w.Flags.Overrides[AudiocommandLogic.FlagKey] = false;
        Assert.Null(Run(w, Alice, "!sfx list", "m3"));
        w.Flags.Overrides[AudiocommandLogic.FlagKey] = true;

        var noCommunity = new AudiocommandLogic().Run(Ev("!sfx list", true), w.HostFor(Alice, "m4", community: null));
        Assert.Contains("needs a community", TextOf(noCommunity!));
    }

    [Fact]
    public void a_bare_sfx_command_gets_the_usage() =>
        Assert.Equal(AudiocommandLogic.Usage, Run(NewWorld(), Alice, "!sfx", "m1", mod: false));

    [Theory]
    [InlineData("add horn horn.mp3")]
    [InlineData("remove horn")]
    [InlineData("enable horn")]
    [InlineData("disable horn")]
    public void management_is_moderator_only(string sub)
    {
        var w = NewWorld();

        Assert.Equal(AudiocommandLogic.ModOnly, Run(w, Alice, "!sfx " + sub, "m1", mod: false));
    }

    [Fact]
    public void add_registers_updates_and_lists_sounds_hiding_disabled_ones()
    {
        var w = NewWorld();

        Assert.Equal("No sounds are registered.", Run(w, Bob, "!sfx list", "m0", mod: false));
        Assert.Equal("Sound Horn added.".Replace("Horn", "horn"), Run(w, Alice, "!sfx add Horn horn.mp3 5 10 80", "m1"));
        Assert.Equal("Sound horn updated.", Run(w, Alice, "!sfx add horn horn2.ogg", "m2"));
        Run(w, Alice, "!sfx add clap clap.WAV", "m3");
        Assert.Equal("Sounds: horn, clap", Run(w, Bob, "!sfx list", "m4", mod: false));
        Run(w, Alice, "!sfx disable clap", "m5");
        Assert.Equal("Sounds: horn", Run(w, Bob, "!sfx list", "m6", mod: false));
    }

    [Theory]
    [InlineData("!sfx add", "Usage: !sfx add")]
    [InlineData("!sfx add onlyname", "Usage: !sfx add")]
    [InlineData("!sfx add bad!name a.mp3", "Usage: !sfx add")]
    [InlineData("!sfx add list a.mp3", "Usage: !sfx add")]
    [InlineData("!sfx add a a.mp3 1 2 3 4", "Usage: !sfx add")]
    [InlineData("!sfx add a ../a.mp3", "The file must be")]
    [InlineData("!sfx add a http://x/a.mp3", "The file must be")]
    [InlineData("!sfx add a a.exe", "The file must be")]
    [InlineData("!sfx add a ..mp3", "The file must be")]
    [InlineData("!sfx add a .hidden.mp3", "The file must be")]
    [InlineData("!sfx add a a..b.mp3", "The file must be")]
    [InlineData("!sfx add a sub/dir.mp3", "The file must be")]
    [InlineData("!sfx add a a.mp3 x", "Cooldowns are 0 to 86400")]
    [InlineData("!sfx add a a.mp3 86401", "Cooldowns are 0 to 86400")]
    [InlineData("!sfx add a a.mp3 1 -2", "Cooldowns are 0 to 86400")]
    [InlineData("!sfx add a a.mp3 1 2 101", "Cooldowns are 0 to 86400")]
    public void add_validates_names_files_cooldowns_and_volume(string text, string expectedStart) =>
        Assert.StartsWith(expectedStart, Run(NewWorld(), Alice, text, "m1"));

    [Fact]
    public void add_refuses_an_over_long_file_name_and_a_full_registry()
    {
        var w = NewWorld();

        Assert.StartsWith("The file must be", Run(w, Alice, "!sfx add a " + new string('x', 61) + ".mp3", "m1"));
        for (var i = 0; i < AudiocommandLogic.MaxSounds; i++)
        {
            Add(w, $"s{i} s{i}.mp3", "a" + i);
        }

        Assert.Contains("At most 200 sounds", Run(w, Alice, "!sfx add extra e.mp3", "m2"));
        Assert.Contains("updated", Run(w, Alice, "!sfx add s0 other.mp3", "m3"));
    }

    [Fact]
    public void the_list_is_truncated_to_fit_a_chat_message()
    {
        var w = NewWorld();
        for (var i = 0; i < 40; i++)
        {
            Add(w, $"sound-number-{i:00} s{i}.mp3", "a" + i);
        }

        var text = Run(w, Bob, "!sfx list", "m1", mod: false)!;

        Assert.Equal(AudiocommandLogic.MaxListChars, text.Length);
        Assert.EndsWith("...", text);
    }

    [Fact]
    public void enable_disable_and_remove_change_the_registry_and_report_unknown_names()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3");

        Assert.StartsWith("Usage: !sfx remove", Run(w, Alice, "!sfx remove", "m1"));
        Assert.Equal("No sound named nope.", Run(w, Alice, "!sfx enable nope", "m2"));
        Assert.Equal("Sound horn disabled.", Run(w, Alice, "!sfx disable horn", "m3"));
        Assert.Equal("Sound horn enabled.", Run(w, Alice, "!sfx enable horn", "m4"));
        Assert.Equal("Sound horn removed.", Run(w, Alice, "!sfx remove horn", "m5"));
        Assert.Equal("No sounds are registered.", Run(w, Alice, "!sfx list", "m6"));
    }

    [Fact]
    public void playing_a_sound_pushes_its_validated_file_name_and_volume_and_stays_silent_in_chat()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3 0 0 80");

        Assert.Null(Run(w, Bob, "!sfx HORN", "p1", mod: false));

        var amount = LastAmount(w);
        Assert.Equal("horn", amount.GetProperty("name").GetString());
        Assert.Equal("horn.mp3", amount.GetProperty("file").GetString());
        Assert.Equal(80, amount.GetProperty("volume").GetInt32());
        Assert.Equal(16, amount.GetProperty("play_id").GetString()!.Length);
    }

    [Fact]
    public void the_default_volume_is_full()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3");

        Run(w, Bob, "!sfx horn", "p1", mod: false);

        Assert.Equal(100, LastAmount(w).GetProperty("volume").GetInt32());
    }

    [Fact]
    public void an_unknown_sound_gets_a_hint_and_a_disabled_one_is_dropped_silently()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3");
        Run(w, Alice, "!sfx disable horn", "d1");

        Assert.Equal("No sound named zzz. Try !sfx list.", Run(w, Bob, "!sfx zzz", "p1", mod: false));
        Assert.Null(Run(w, Bob, "!sfx horn", "p2", mod: false));
        Assert.Empty(w.Pushed.Pushes);
    }

    [Fact]
    public void the_user_cooldown_drops_a_repeat_by_the_same_user_only()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3 60 0");

        Run(w, Bob, "!sfx horn", "p1", mod: false);
        Run(w, Bob, "!sfx horn", "p2", mod: false);
        Run(w, Carol, "!sfx horn", "p3", mod: false);

        Assert.Equal(2, w.Pushed.Pushes.Count);
    }

    [Fact]
    public void the_global_cooldown_blocks_everyone_and_hands_back_the_blocked_users_slot()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3 100 100");

        Run(w, Bob, "!sfx horn", "p1", mod: false);
        Run(w, Carol, "!sfx horn", "p2", mod: false);

        Assert.Single(w.Pushed.Pushes);
        Assert.True(StateCooldown.TryAcquire(new KvStateStore(w.Kv), "sfxu.horn", Carol, 100));
    }

    [Fact]
    public void a_zero_cooldown_never_blocks()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3 0 0");

        for (var i = 0; i < 3; i++)
        {
            Run(w, Bob, "!sfx horn", "p" + i, mod: false);
        }

        Assert.Equal(3, w.Pushed.Pushes.Count);
    }

    [Fact]
    public void a_redelivered_play_repushes_the_same_id_and_claims_no_new_cooldown()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3 100 100");

        Run(w, Bob, "!sfx horn", "p1", mod: false);
        var firstId = LastAmount(w).GetProperty("play_id").GetString();
        Run(w, Bob, "!sfx horn", "p1", mod: false);

        Assert.Equal(2, w.Pushed.Pushes.Count);
        Assert.Equal(firstId, LastAmount(w).GetProperty("play_id").GetString());
    }

    [Fact]
    public void a_play_survives_an_overlay_refusal_through_redelivery()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3 100 100");
        w.Overlay = new RefusingOverlay();

        var ex = Assert.Throws<WaddleTransportException>(() => Run(w, Bob, "!sfx horn", "p1", mod: false));
        Assert.Equal("not_implemented", ex.Error.Code);

        w.Overlay = new WaddleSdk.Tests.FakeOverlayClient();
        Assert.Null(Run(w, Bob, "!sfx horn", "p1", mod: false));
        Assert.Single(w.Pushed.Pushes);
    }

    [Fact]
    public void a_redelivered_play_of_a_removed_sound_says_so()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3");
        Run(w, Bob, "!sfx horn", "p1", mod: false);
        Run(w, Alice, "!sfx remove horn", "rm");

        Assert.Equal("That sound was removed.", Run(w, Bob, "!sfx horn", "p1", mod: false));
    }

    [Fact]
    public void playing_needs_a_linked_account_and_a_message_id()
    {
        var w = NewWorld();
        Add(w, "horn horn.mp3");

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, Bob, "!sfx horn", "m1", mod: false));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Run(w, Bob, "!sfx horn", "m2", mod: false));
        w.ActorFailure = null;
        Assert.Throws<ArgumentException>(() => Run(w, Bob, "!sfx horn", " ", mod: false));
    }

    [Theory]
    [InlineData("a.mp3", true)]
    [InlineData("A_b-1.OGG", true)]
    [InlineData("a.wav", true)]
    [InlineData("a.flac", false)]
    [InlineData("", false)]
    [InlineData("a b.mp3", false)]
    [InlineData("a\\b.mp3", false)]
    [InlineData("a%2fb.mp3", false)]
    public void file_names_are_bare_audio_names(string file, bool valid) =>
        Assert.Equal(valid, AudiocommandLogic.IsValidFile(file));

    [Fact]
    public void a_corrupt_registry_is_treated_as_empty()
    {
        var w = NewWorld();
        w.Kv.Set("sfx.reg", [9, 9], 0);

        Assert.Equal("No sounds are registered.", Run(w, Bob, "!sfx list", "m1", mod: false));
    }
}

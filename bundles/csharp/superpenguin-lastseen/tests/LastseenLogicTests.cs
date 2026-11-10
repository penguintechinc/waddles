using WaddleSdk.Identity;
using Xunit;
using static WaddleBundleSuperpenguinLastseen.Tests.World;

namespace WaddleBundleSuperpenguinLastseen.Tests;

public class LastseenLogicTests
{
    private static World NewWorld() => new(LastseenLogic.FlagKey);

    [Fact]
    public void a_first_lookup_of_yourself_reports_never_seen_then_the_stamp_is_written()
    {
        var w = NewWorld();
        var l = new LastseenLogic();

        Assert.Equal($"Have never seen {{user:{Alice}}}", w.Say(l, Alice, "!lastseen", "m1"));

        w.Clock.Now += 90 * 60 * 1000;
        Assert.Equal($"{{user:{Alice}}} was last seen 1h, 30m ago", w.Say(l, Alice, "!lastseen", "m2"));
    }

    [Fact]
    public void ordinary_chat_stamps_the_speaker_silently_and_a_mention_reads_it()
    {
        var w = NewWorld();
        var l = new LastseenLogic();
        Assert.Null(w.Say(l, Bob, "hello everyone", "m1"));

        w.Clock.Now += 8 * 24 * 3600 * 1000L + 3600 * 1000L;
        var reply = w.Say(l, Alice, $"!lastseen {Bob}", "m2");

        Assert.Equal($"{{user:{Bob}}} was last seen 1w, 1d, 1h ago", reply);
    }

    [Fact]
    public void someone_never_seen_is_reported_as_such()
    {
        var w = NewWorld();
        Assert.Equal($"Have never seen {{user:{Carol}}}", w.Say(new LastseenLogic(), Alice, $"!lastseen {Carol}", "m1"));
    }

    [Theory]
    [InlineData(IdentityErrorKind.NotFound)]
    [InlineData(IdentityErrorKind.Ambiguous)]
    [InlineData(IdentityErrorKind.Invalid)]
    public void an_unresolvable_mention_asks_for_a_real_mention(IdentityErrorKind kind)
    {
        var w = NewWorld();
        w.MentionFailure = new WaddleIdentityException(kind, "nope");

        Assert.Contains("Mention the person", w.Say(new LastseenLogic(), Alice, "!lastseen nobody", "m1"));
    }

    [Fact]
    public void other_identity_failures_propagate_loudly()
    {
        var w = NewWorld();
        w.MentionFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");

        Assert.Throws<WaddleIdentityException>(() => w.Say(new LastseenLogic(), Alice, "!lastseen x", "m1"));
    }

    [Fact]
    public void an_unlinked_account_is_told_to_link_for_the_command_and_ignored_otherwise()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "no");
        var l = new LastseenLogic();

        Assert.Contains("Link your account", w.Say(l, Alice, "!lastseen", "m1"));
        Assert.Null(w.Say(l, Alice, "just chatting", "m2"));
    }

    [Fact]
    public void the_flag_defaults_off_and_a_community_is_required()
    {
        var w = NewWorld();
        w.Flags.Overrides[LastseenLogic.FlagKey] = false;
        Assert.Null(w.Say(new LastseenLogic(), Alice, "!lastseen", "m1"));

        w.Flags.Overrides[LastseenLogic.FlagKey] = true;
        Assert.Null(new LastseenLogic().Run(TestKit.Chat("!lastseen"), w.HostFor(Alice, "m2", community: null)));
    }

    [Theory]
    [InlineData(0, "0 sec")]
    [InlineData(-5, "0 sec")]
    [InlineData(42, "42 sec")]
    [InlineData(60, "1m")]
    [InlineData(3661, "1h, 1m")]
    [InlineData(86400 + 60, "1d, 1m")]
    [InlineData(14 * 86400, "2w")]
    public void the_compound_duration_matches_the_original_format(long seconds, string expected) =>
        Assert.Equal(expected, LastseenLogic.CompoundDuration(seconds));

    [Fact]
    public void a_malformed_payload_is_stamped_but_never_a_command()
    {
        var w = NewWorld();
        var raw = new WaddleSdk.Types.PlatformEventInfo("twitch", "chat.message", "x", "not json", "2026-11-07T00:00:00.000Z");

        Assert.Null(new LastseenLogic().Run(raw, w.HostFor(Alice, "m1")));
        Assert.Equal($"{{user:{Alice}}} was last seen 0 sec ago", w.Say(new LastseenLogic(), Alice, "!lastseen", "m2"));
    }
}

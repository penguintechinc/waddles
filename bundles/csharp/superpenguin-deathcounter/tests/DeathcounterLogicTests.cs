using WaddleSdk.Identity;
using Xunit;
using static WaddleBundleSuperpenguinDeathcounter.Tests.World;

namespace WaddleBundleSuperpenguinDeathcounter.Tests;

public class DeathcounterLogicTests
{
    private static World NewWorld() => new(DeathcounterLogic.FlagKey);

    [Fact]
    public void an_untouched_counter_reads_not_died_yet()
    {
        var w = NewWorld();
        Assert.Equal("The streamer has not died in this stream YET", w.Say(new DeathcounterLogic(), Alice, "!death", "m1"));
    }

    [Fact]
    public void a_moderator_increments_decrements_sets_and_resets()
    {
        var w = NewWorld();
        var l = new DeathcounterLogic();

        Assert.Equal("The streamer has died 1 times in this stream", w.Say(l, Bob, "!death +", "m1", mod: true));
        Assert.Equal("The streamer has died 2 times in this stream", w.Say(l, Bob, "!death +", "m2", broadcaster: true));
        Assert.Equal("The streamer has died 1 times in this stream", w.Say(l, Bob, "!death -", "m3", mod: true));
        Assert.Equal("The streamer has not died in this stream YET", w.Say(l, Bob, "!death -", "m4", mod: true));
        Assert.Equal("The streamer has not died in this stream YET", w.Say(l, Bob, "!death -", "m5", mod: true));
        Assert.Equal("The streamer has died 42 times in this stream", w.Say(l, Bob, "!death set 42", "m6", mod: true));
        Assert.Equal("The streamer has not died in this stream YET", w.Say(l, Bob, "!death reset", "m7", mod: true));
    }

    [Fact]
    public void viewers_cannot_change_the_counter_and_unknown_roles_fail_closed()
    {
        var w = NewWorld();
        var l = new DeathcounterLogic();
        w.Say(l, Bob, "!death set 5", "m1", mod: true);

        Assert.Equal("The streamer has died 5 times in this stream", w.Say(l, Alice, "!death +", "m2"));
        var noRoles = new WaddleSdk.Types.PlatformEventInfo("discord", "chat.message", "x", "{\"text\":\"!death reset\",\"channel_id\":\"c\"}", "t");
        Assert.Equal("The streamer has died 5 times in this stream", World.TextOf(l.Run(noRoles, w.HostFor(Alice, "m3"))!));
    }

    [Theory]
    [InlineData("!death set")]
    [InlineData("!death set abc")]
    public void a_set_without_a_number_changes_nothing(string text)
    {
        var w = NewWorld();
        var l = new DeathcounterLogic();
        w.Say(l, Bob, "!death set 3", "m1", mod: true);

        Assert.Equal("The streamer has died 3 times in this stream", w.Say(l, Bob, text, "m2", mod: true));
    }

    [Theory]
    [InlineData("-9", "not died")]
    [InlineData("99999999999", "died 1000000000 times")]
    public void set_is_clamped_to_the_valid_range(string amount, string expected) =>
        Assert.Contains(expected, NewWorld().Say(new DeathcounterLogic(), Bob, $"!death set {amount}", "m1", mod: true));

    [Fact]
    public void each_active_game_label_keeps_its_own_count()
    {
        var w = NewWorld();
        var l = new DeathcounterLogic();
        w.Say(l, Bob, "!death +", "m1", mod: true);

        Assert.Equal("The streamer has not died in Elden Ring YET", w.Say(l, Bob, "!death game Elden {Ring}", "m2", mod: true));
        Assert.Equal("The streamer has died 1 times in Elden Ring", w.Say(l, Bob, "!death +", "m3", mod: true));
        Assert.Equal("The streamer has died 1 times in Elden Ring", w.Say(l, Alice, "!death", "m4"));

        Assert.Equal("The streamer has died 1 times in this stream", w.Say(l, Bob, "!death game", "m5", mod: true));
    }

    [Fact]
    public void a_redelivered_mutation_is_applied_once()
    {
        var w = NewWorld();
        var l = new DeathcounterLogic();

        w.Say(l, Bob, "!death +", "same", mod: true);
        var again = w.Say(l, Bob, "!death +", "same", mod: true);

        Assert.Equal("The streamer has died 1 times in this stream", again);
    }

    [Fact]
    public void a_moderator_without_a_linked_account_is_told_to_link()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");

        Assert.Contains("Link your account", w.Say(new DeathcounterLogic(), Bob, "!death +", "m1", mod: true));
        Assert.Contains("not died", w.Say(new DeathcounterLogic(), Bob, "!death", "m2", mod: true));
    }

    [Fact]
    public void other_commands_flag_off_and_no_community_are_handled()
    {
        var w = NewWorld();
        var l = new DeathcounterLogic();
        Assert.Null(w.Say(l, Alice, "!other", "m1"));
        Assert.Null(w.Say(l, Alice, "plain chat", "m2"));
        Assert.Contains("needs a community", World.TextOf(l.Run(TestKit.Chat("!death"), w.HostFor(Alice, "m3", community: null))!));

        w.Flags.Overrides[DeathcounterLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!death", "m4"));
    }
}

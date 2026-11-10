using WaddleSdk.Identity;
using Xunit;
using static WaddleBundleSuperpenguinDailycounter.Tests.World;

namespace WaddleBundleSuperpenguinDailycounter.Tests;

public class DailycounterLogicTests
{
    private static World NewWorld() => new(DailycounterLogic.FlagKey);

    [Theory]
    [InlineData(0, 10)]
    [InlineData(8, 10)]
    [InlineData(9, 10)]
    [InlineData(10, 20)]
    [InlineData(19, 20)]
    [InlineData(-5, 10)]
    public void the_next_goal_is_the_next_multiple_of_ten(long current, long expected) =>
        Assert.Equal(expected, DailycounterLogic.NextValue(current));

    [Fact]
    public void the_default_text_renders_value_and_next_goal()
    {
        var w = NewWorld();
        Assert.Equal("Daily count: 0 (next goal: 10)", w.Say(new DailycounterLogic(), Alice, "!dailycounter", "m1"));
    }

    [Fact]
    public void the_broadcaster_sets_the_text_and_value()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();

        Assert.Equal("Subs today: 0/10", w.Say(l, Bob, "!setdailycountertext Subs today: {VALUE}/{NextGoal}", "m1", broadcaster: true));
        Assert.Equal("Subs today: 25/30", w.Say(l, Bob, "!setdailyvalue 25", "m2", broadcaster: true));
        Assert.Equal("Subs today: 25/30", w.Say(l, Alice, "!dailycounter", "m3"));
    }

    [Fact]
    public void a_blank_format_or_bad_value_changes_nothing()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();
        w.Say(l, Bob, "!setdailyvalue 7", "m1", broadcaster: true);

        Assert.Equal("Daily count: 7 (next goal: 10)", w.Say(l, Bob, "!setdailycountertext", "m2", broadcaster: true));
        Assert.Equal("Daily count: 7 (next goal: 10)", w.Say(l, Bob, "!setdailyvalue abc", "m3", broadcaster: true));
        Assert.Equal("Daily count: 7 (next goal: 10)", w.Say(l, Bob, "!setdailyvalue", "m4", broadcaster: true));
    }

    [Theory]
    [InlineData("-3", "Daily count: 0")]
    [InlineData("99999999999", "Daily count: 1000000000")]
    public void the_value_is_clamped(string amount, string expected) =>
        Assert.StartsWith(expected, NewWorld().Say(new DailycounterLogic(), Bob, $"!setdailyvalue {amount}", "m1", broadcaster: true));

    [Fact]
    public void setters_are_broadcaster_only_and_dailyadd_is_moderator_or_broadcaster()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();

        Assert.Null(w.Say(l, Bob, "!setdailyvalue 5", "m1", mod: true));
        Assert.Null(w.Say(l, Alice, "!setdailycountertext hi", "m2"));
        Assert.Null(w.Say(l, Alice, "!dailyadd", "m3"));
        Assert.Equal("Daily count: 1 (next goal: 10)", w.Say(l, Bob, "!dailyadd", "m4", mod: true));
        Assert.Equal("Daily count: 6 (next goal: 10)", w.Say(l, Bob, "!dailyadd 5", "m5", broadcaster: true));
    }

    [Theory]
    [InlineData("0")]
    [InlineData("1001")]
    [InlineData("x")]
    public void a_bad_dailyadd_amount_changes_nothing(string amount) =>
        Assert.Equal("Daily count: 0 (next goal: 10)", NewWorld().Say(new DailycounterLogic(), Bob, $"!dailyadd {amount}", "m1", mod: true));

    [Fact]
    public void dailyadd_saturates_at_the_maximum()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();
        w.Say(l, Bob, "!setdailyvalue 1000000000", "m1", broadcaster: true);

        Assert.StartsWith("Daily count: 1000000000", w.Say(l, Bob, "!dailyadd 10", "m2", mod: true));
    }

    [Fact]
    public void a_redelivered_dailyadd_counts_once()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();
        w.Say(l, Bob, "!dailyadd", "same", mod: true);

        Assert.Equal("Daily count: 1 (next goal: 10)", w.Say(l, Bob, "!dailyadd", "same", mod: true));
    }

    [Fact]
    public void an_unlinked_mutator_is_told_to_link()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");

        Assert.Contains("Link your account", w.Say(new DailycounterLogic(), Bob, "!dailyadd", "m1", mod: true));
    }

    [Fact]
    public void a_long_format_is_capped_and_controls_stripped()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();
        var reply = w.Say(l, Bob, "!setdailycountertext " + new string('x', 500), "m1", broadcaster: true);

        Assert.Equal(DailycounterLogic.MaxFormatLength, reply!.Length);
    }

    [Fact]
    public void other_commands_flag_off_and_no_community_are_handled()
    {
        var w = NewWorld();
        var l = new DailycounterLogic();
        Assert.Null(w.Say(l, Alice, "!other", "m1"));
        Assert.Null(w.Say(l, Alice, "chat", "m2"));
        Assert.Contains("needs a community", World.TextOf(l.Run(TestKit.Chat("!dailycounter"), w.HostFor(Alice, "m3", community: null))!));

        w.Flags.Overrides[DailycounterLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!dailycounter", "m4"));
    }
}

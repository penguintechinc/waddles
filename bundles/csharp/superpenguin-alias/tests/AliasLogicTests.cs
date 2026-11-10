using Xunit;
using static WaddleBundleSuperpenguinAlias.Tests.World;

namespace WaddleBundleSuperpenguinAlias.Tests;

public class AliasLogicTests
{
    private static World NewWorld() => new(AliasLogic.FlagKey);

    private static string? Mod(World w, AliasLogic l, string text, string id = "m") => w.Say(l, Bob, text, id, mod: true);

    [Fact]
    public void a_moderator_sets_an_alias_and_invoking_it_relays_the_resolved_command_with_args()
    {
        var w = NewWorld();
        var l = new AliasLogic();

        Assert.Equal("Alias !hi now runs !hello there.", Mod(w, l, "!alias set hi !hello there"));
        Assert.Equal("!hello there", w.Say(l, Alice, "!hi", "i1"));
        Assert.Equal("!hello there world", w.Say(l, Alice, "!HI world", "i2"));
    }

    [Fact]
    public void non_aliases_and_plain_chat_stay_silent()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        Mod(w, l, "!alias set hi !hello");

        Assert.Null(w.Say(l, Alice, "!unknown", "i1"));
        Assert.Null(w.Say(l, Alice, "hello", "i2"));
        Assert.Null(w.Say(l, Alice, "!", "i3"));
    }

    [Fact]
    public void list_shows_all_aliases_sorted_and_bare_alias_lists_too()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        Assert.Equal("No aliases are set.", w.Say(l, Alice, "!alias", "m0"));
        Mod(w, l, "!alias set zed !z");
        Mod(w, l, "!alias add !abe !a 1");

        Assert.Equal("Aliases: !abe -> !a 1, !zed -> !z", w.Say(l, Alice, "!alias list", "m1"));
        Assert.Equal("Aliases: !abe -> !a 1, !zed -> !z", w.Say(l, Alice, "!alias", "m2"));
    }

    [Fact]
    public void a_long_list_is_truncated()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        for (var i = 0; i < 60; i++)
        {
            Mod(w, l, $"!alias set name{i:D2}aaaaaaaaaaaaaa !target{i}", $"m{i}");
        }

        Assert.Equal(AliasLogic.MaxResolvedLength, w.Say(l, Alice, "!alias list", "x")!.Length);
    }

    [Fact]
    public void remove_deletes_and_unknown_names_are_reported()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        Mod(w, l, "!alias set hi !hello");

        Assert.Equal("Alias !hi removed.", Mod(w, l, "!alias remove hi"));
        Assert.Equal("No such alias.", Mod(w, l, "!alias del hi"));
        Assert.Equal("No such alias.", Mod(w, l, "!alias delete BAD NAME!"));
        Assert.Null(w.Say(l, Alice, "!hi", "i1"));
    }

    [Fact]
    public void viewers_cannot_change_aliases_and_unknown_roles_fail_closed()
    {
        var w = NewWorld();
        var l = new AliasLogic();

        Assert.Equal("Only moderators can change aliases.", w.Say(l, Alice, "!alias set hi !hello", "m1"));
        Assert.Equal("Only moderators can change aliases.", w.Say(l, Alice, "!alias remove hi", "m2"));
        Assert.Equal("No aliases are set.", w.Say(l, Alice, "!alias list", "m3"));
    }

    [Theory]
    [InlineData("!alias set", "Usage")]
    [InlineData("!alias set hi", "Usage")]
    [InlineData("!alias remove", "Usage")]
    [InlineData("!alias frobnicate", "Usage")]
    [InlineData("!alias set bad!name !hello", "Alias names are")]
    [InlineData("!alias set hi hello", "single !command")]
    [InlineData("!alias set hi !", "single !command")]
    [InlineData("!alias set hi !bad!token", "single !command")]
    [InlineData("!alias set hi !alias", "cannot point at !alias")]
    [InlineData("!alias set hi !hi", "point at itself")]
    [InlineData("!alias set alias !foo", "reserved")]
    public void invalid_management_input_is_rejected_with_a_reason(string text, string expected) =>
        Assert.Contains(expected, Mod(NewWorld(), new AliasLogic(), text));

    [Fact]
    public void an_alias_cannot_point_at_another_alias_so_there_are_no_chains()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        Mod(w, l, "!alias set a !real");

        Assert.Equal("An alias cannot point at !alias or at another alias.", Mod(w, l, "!alias set b !a"));
    }

    [Fact]
    public void an_oversized_or_control_character_target_is_rejected()
    {
        var w = NewWorld();
        var l = new AliasLogic();

        Assert.Contains("single !command", Mod(w, l, "!alias set hi !hello " + new string('x', 300)));
        Assert.Contains("single !command", Mod(w, l, "!alias set hi !hel\u0007lo"));
    }

    [Fact]
    public void the_alias_count_is_capped_but_existing_aliases_can_still_be_updated()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        for (var i = 0; i < AliasLogic.MaxAliases; i++)
        {
            Assert.StartsWith("Alias", Mod(w, l, $"!alias set n{i} !t{i}", $"m{i}"));
        }

        Assert.Contains("maximum", Mod(w, l, "!alias set extra !t", "x1"));
        Assert.Equal("Alias !n3 now runs !other.", Mod(w, l, "!alias set n3 !other", "x2"));
    }

    [Fact]
    public void a_very_long_resolution_is_capped()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        Mod(w, l, "!alias set hi !hello");

        Assert.Equal(AliasLogic.MaxResolvedLength, w.Say(l, Alice, "!hi " + new string('y', 800), "i1")!.Length);
    }

    [Theory]
    [InlineData("!Name", "name")]
    [InlineData("a-b_c9", "a-b_c9")]
    [InlineData("", null)]
    [InlineData("has space", null)]
    [InlineData("UPPER!", null)]
    public void names_are_validated_and_lowercased(string raw, string? expected) =>
        Assert.Equal(expected, AliasLogic.ParseName(raw));

    [Fact]
    public void flag_off_and_no_community_are_handled()
    {
        var w = NewWorld();
        var l = new AliasLogic();
        Assert.Contains("need a community", World.TextOf(l.Run(TestKit.Chat("!alias list"), w.HostFor(Alice, "m1", community: null))!));
        Assert.Null(l.Run(TestKit.Chat("!hi"), w.HostFor(Alice, "m2", community: null)));

        w.Flags.Overrides[AliasLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!alias list", "m3"));
    }
}

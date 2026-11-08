using WaddleSdk.Identity;
using Xunit;
using static WaddleBundleSuperpenguinQuotes.Tests.World;

namespace WaddleBundleSuperpenguinQuotes.Tests;

public class QuotesLogicTests
{
    private static World NewWorld() => new(QuotesLogic.FlagKey);

    private static string Add(World w, QuotesLogic l, string text, string id) => w.Say(l, Bob, "!addquote " + text, id, mod: true)!;

    [Fact]
    public void an_empty_book_says_so_for_every_lookup_shape()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Assert.Equal("There are no quotes yet.", w.Say(l, Alice, "!quote", "m1"));
        Assert.Equal("There are no quotes yet.", w.Say(l, Alice, "!quote 3", "m2"));
        Assert.Equal("There are no quotes yet.", w.Say(l, Alice, "!quote banana", "m3"));
    }

    [Fact]
    public void moderators_add_numbered_quotes_and_anyone_reads_them_by_number()
    {
        var w = NewWorld();
        var l = new QuotesLogic();

        Assert.Equal("Quote #1 added.", Add(w, l, "first words", "a1"));
        Assert.Equal("Quote #2 added.", Add(w, l, "second words", "a2"));

        Assert.Equal($"Quote #2: second words (2023-11-14) Created By {{user:{Bob}}}", w.Say(l, Alice, "!quote 2", "r1"));
        Assert.Equal($"Quote #1: first words (2023-11-14) Created By {{user:{Bob}}}", w.Say(l, Alice, "!quote #1", "r2"));
        Assert.Equal("Quote #9 not found.", w.Say(l, Alice, "!quote 9", "r3"));
    }

    [Fact]
    public void a_viewer_cannot_add_or_delete()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Assert.Null(w.Say(l, Alice, "!addquote hi", "m1"));
        Assert.Null(w.Say(l, Alice, "!delquote 1", "m2"));
    }

    [Fact]
    public void a_random_quote_is_always_a_live_one()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Add(w, l, "keep me", "a1");
        Add(w, l, "delete me", "a2");
        Add(w, l, "keep me too", "a3");
        Assert.Equal("Quote #2 removed.", w.Say(l, Bob, "!delquote 2", "d1", mod: true));

        for (var i = 0; i < 40; i++)
        {
            var reply = w.Say(l, Alice, "!quote", $"r{i}")!;
            Assert.Contains("keep me", reply);
            Assert.DoesNotContain("delete me", reply);
        }
    }

    [Fact]
    public void when_every_quote_was_deleted_random_says_none_and_numbers_are_never_reused()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Add(w, l, "only", "a1");
        w.Say(l, Bob, "!delquote 1", "d1", mod: true);

        Assert.Equal("There are no quotes yet.", w.Say(l, Alice, "!quote", "r1"));
        Assert.Equal("Quote #1 not found.", w.Say(l, Alice, "!quote 1", "r2"));
        Assert.Equal("Quote #2 added.", Add(w, l, "next", "a2"));
    }

    [Fact]
    public void text_search_finds_a_match_among_recent_quotes()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Add(w, l, "I love Bananas", "a1");
        Add(w, l, "apples are fine", "a2");
        Add(w, l, "bananas again", "a3");
        w.Say(l, Bob, "!delquote 3", "d1", mod: true);

        Assert.Contains("I love Bananas", w.Say(l, Alice, "!quote banana", "r1"));
        Assert.Equal("No quote matches that.", w.Say(l, Alice, "!quote cherry", "r2"));
    }

    [Fact]
    public void text_search_only_looks_at_the_most_recent_window()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Add(w, l, "ancient unicorn", "a0");
        for (var i = 1; i <= QuotesLogic.SearchWindow; i++)
        {
            Add(w, l, $"filler {i}", $"a{i}");
        }

        Assert.Equal("No quote matches that.", w.Say(l, Alice, "!quote unicorn", "r1"));
        Assert.Contains("ancient unicorn", w.Say(l, Alice, "!quote 1", "r2"));
    }

    [Fact]
    public void deleting_validates_its_argument()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Add(w, l, "x", "a1");

        Assert.Equal("you forgot to include the quote number you want to delete.", w.Say(l, Bob, "!delquote", "d1", mod: true));
        Assert.Equal("couldn't figure out which quote number you meant.", w.Say(l, Bob, "!delquote abc", "d2", mod: true));
        Assert.Equal("couldn't find that quote number.", w.Say(l, Bob, "!delquote 7", "d3", mod: true));
        Assert.Equal("Quote #1 removed.", w.Say(l, Bob, "!quotedel #1", "d4", mod: true));
        Assert.Equal("couldn't find that quote number.", w.Say(l, Bob, "!delquote 1", "d5", mod: true));
    }

    [Fact]
    public void adding_validates_cleans_and_replays_safely()
    {
        var w = NewWorld();
        var l = new QuotesLogic();

        Assert.Equal("you forgot to include the quote.", w.Say(l, Bob, "!addquote", "a0", mod: true));
        Assert.Equal("Quote #1 added.", w.Say(l, Bob, "!quoteadd hi {user:" + Carol + "} there", "a1", mod: true));
        Assert.Equal("Quote #1 added.", w.Say(l, Bob, "!quoteadd hi {user:" + Carol + "} there", "a1", mod: true));
        Assert.Equal("Quote #2 added.", Add(w, l, "different", "a2"));

        var stored = w.Say(l, Alice, "!quote 1", "r1")!;
        Assert.Contains("hi user:" + Carol + " there", stored);
        Assert.DoesNotContain("{user:" + Carol, stored);
    }

    [Fact]
    public void very_long_quotes_are_capped()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Add(w, l, new string('z', 1000), "a1");

        Assert.Contains(new string('z', QuotesLogic.MaxTextLength) + " (", w.Say(l, Alice, "!quote 1", "r1"));
    }

    [Fact]
    public void an_add_without_a_message_id_is_refused_loudly()
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => w.Say(new QuotesLogic(), Bob, "!addquote hi", " ", mod: true));
    }

    [Fact]
    public void an_unlinked_moderator_is_told_to_link()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", w.Say(new QuotesLogic(), Bob, "!addquote hi", "a1", mod: true));
    }

    [Fact]
    public void other_commands_flag_off_and_no_community_are_handled()
    {
        var w = NewWorld();
        var l = new QuotesLogic();
        Assert.Null(w.Say(l, Alice, "!other", "m1"));
        Assert.Null(w.Say(l, Alice, "chat", "m2"));
        Assert.Contains("need a community", World.TextOf(l.Run(TestKit.Chat("!quote"), w.HostFor(Alice, "m3", community: null))!));

        w.Flags.Overrides[QuotesLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!quote", "m4"));
    }
}

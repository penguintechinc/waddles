using WaddleSdk.Identity;
using WaddleSdk.State;
using Xunit;
using static WaddleBundleSuperpenguinTimers.Tests.World;

namespace WaddleBundleSuperpenguinTimers.Tests;

public class TimersLogicTests
{
    private static World NewWorld() => new(TimersLogic.FlagKey);

    private static string? Mod(World w, TimersLogic l, string text, string id) => w.Say(l, Bob, text, id, mod: true);

    private static int _seq;

    private static string Id() => "t" + Interlocked.Increment(ref _seq);

    /// <summary>Creates a timer with the given lines (10s interval, no minimum messages) and returns the world.</summary>
    private static (World W, TimersLogic L) WithTimer(string name = "news", bool shuffle = true, params string[] lines)
    {
        var w = NewWorld();
        var l = new TimersLogic();
        Assert.StartsWith($"Timer {name} created", Mod(w, l, $"!timer create {name} 10 10 0", Id()));
        foreach (var line in lines)
        {
            Assert.StartsWith("Line", Mod(w, l, $"!timer addline {name} {line}", Id()));
        }

        if (!shuffle)
        {
            Assert.Contains("shuffle off", Mod(w, l, $"!timer shuffle {name} off", Id()));
        }

        return (w, l);
    }

    private static string? Chat(World w, TimersLogic l) => w.Say(l, Alice, "just chatting", Id());

    [Fact]
    public void a_due_timer_is_announced_on_the_next_chat_message_in_that_channel_and_rescheduled()
    {
        var (w, l) = WithTimer("news", true, "Follow the stream");

        Assert.Null(Chat(w, l));
        w.Clock.Now += 11_000;
        Assert.Equal("Follow the stream", Chat(w, l));
        Assert.Null(Chat(w, l));
        w.Clock.Now += 11_000;
        Assert.Equal("Follow the stream", Chat(w, l));
    }

    [Fact]
    public void the_announcement_waits_for_the_minimum_message_count()
    {
        var w = NewWorld();
        var l = new TimersLogic();
        Mod(w, l, "!timer create news 10 10 5", Id());
        Mod(w, l, "!timer addline news hello", Id());
        w.Clock.Now += 11_000;

        Assert.Null(Chat(w, l));
        Assert.Null(Chat(w, l));
        Assert.Null(Chat(w, l));
        Assert.Equal("hello", Chat(w, l));
    }

    [Fact]
    public void a_shuffle_bag_plays_every_line_once_before_repeating()
    {
        var (w, l) = WithTimer("news", true, "a", "b", "c");
        var seen = new List<string>();
        for (var i = 0; i < 3; i++)
        {
            w.Clock.Now += 11_000;
            seen.Add(Chat(w, l)!);
        }

        Assert.Equal(["a", "b", "c"], seen.OrderBy(s => s));
        w.Clock.Now += 11_000;
        Assert.Contains(Chat(w, l), new[] { "a", "b", "c" });
    }

    [Fact]
    public void without_shuffle_the_lines_play_in_order_and_wrap()
    {
        var (w, l) = WithTimer("news", false, "one", "two");
        var seen = new List<string>();
        for (var i = 0; i < 3; i++)
        {
            w.Clock.Now += 11_000;
            seen.Add(Chat(w, l)!);
        }

        Assert.Equal(["one", "two", "one"], seen);
    }

    [Fact]
    public void a_once_timer_announces_a_single_time_then_goes_inactive()
    {
        var w = NewWorld();
        var l = new TimersLogic();
        Assert.Contains("once", Mod(w, l, "!timer create news 10 10 0 once", Id()));
        Mod(w, l, "!timer addline news only once", Id());

        w.Clock.Now += 11_000;
        Assert.Equal("only once", Chat(w, l));
        w.Clock.Now += 11_000;
        Assert.Null(Chat(w, l));
        Assert.Contains("(off,", w.Say(l, Alice, "!timer list", Id()));
    }

    [Fact]
    public void only_one_group_rides_an_event_and_a_timer_command_answers_the_command_instead()
    {
        var (w, l) = WithTimer("a", true, "from a");
        Mod(w, l, "!timer create b 10 10 0", Id());
        Mod(w, l, "!timer addline b from b", Id());
        w.Clock.Now += 11_000;

        Assert.Contains("a (on", w.Say(l, Alice, "!timer list", Id()));
        var first = Chat(w, l);
        var second = Chat(w, l);
        Assert.Equal(["from a", "from b"], new[] { first, second }.OrderBy(s => s));
        Assert.Null(Chat(w, l));
    }

    [Fact]
    public void a_racing_event_cannot_announce_the_same_due_window_twice()
    {
        var (w, l) = WithTimer("news", true, "hello");
        w.Clock.Now += 11_000;
        var before = w.Kv.Get(TimersLogic.DocKey)!;

        Assert.Equal("hello", Chat(w, l));
        w.Kv.Set(TimersLogic.DocKey, before, 0);

        Assert.Null(Chat(w, l));
    }

    [Fact]
    public void groups_without_lines_or_that_are_disabled_never_announce_and_enable_restarts_the_clock()
    {
        var (w, l) = WithTimer("news", true, "hello");
        Assert.Equal("Timer news disabled.", Mod(w, l, "!timer disable news", Id()));
        w.Clock.Now += 60_000;
        Assert.Null(Chat(w, l));

        Assert.Equal("Timer news enabled.", Mod(w, l, "!timer enable news", Id()));
        Assert.Null(Chat(w, l));
        w.Clock.Now += 11_000;
        Assert.Equal("hello", Chat(w, l));

        var empty = NewWorld();
        var el = new TimersLogic();
        Mod(empty, el, "!timer create quiet 10 10 0", Id());
        empty.Clock.Now += 60_000;
        Assert.Null(Chat(empty, el));
    }

    [Fact]
    public void run_forces_a_line_now_without_changing_whether_the_timer_is_active()
    {
        var (w, l) = WithTimer("news", true, "forced");
        Assert.Equal("forced", Mod(w, l, "!timer run news", Id()));

        var empty = NewWorld();
        var el = new TimersLogic();
        Mod(empty, el, "!timer create quiet", Id());
        Assert.Equal("That timer has no lines yet.", Mod(empty, el, "!timer run quiet", Id()));
    }

    [Fact]
    public void lines_can_be_removed_and_deleting_a_timer_removes_it()
    {
        var (w, l) = WithTimer("news", true, "a", "b");

        Assert.Equal("Line 1 removed from news.", Mod(w, l, "!timer delline news 1", Id()));
        Assert.Contains("1 lines", w.Say(l, Alice, "!timer", Id()));
        Assert.Equal("Give the line number to delete (1-1).", Mod(w, l, "!timer delline news 2", Id()));
        Assert.Equal("Give the line number to delete (1-1).", Mod(w, l, "!timer delline news", Id()));
        Assert.Equal("Timer news deleted.", Mod(w, l, "!timer delete news", Id()));
        Assert.Equal("No such timer.", Mod(w, l, "!timer delete news", Id()));
        Assert.Equal("No timers are set.", w.Say(l, Alice, "!timer list", Id()));
    }

    [Theory]
    [InlineData("!timer create", "Usage")]
    [InlineData("!timer frobnicate x", "Usage")]
    [InlineData("!timer create Bad!Name", "Timer names are")]
    [InlineData("!timer create x 5 10 1", "Intervals are")]
    [InlineData("!timer create x 20 10 1", "Intervals are")]
    [InlineData("!timer create x 10 20", "Intervals are")]
    [InlineData("!timer create x 10 20 5000", "Intervals are")]
    [InlineData("!timer addline nope hi", "No such timer")]
    [InlineData("!timer shuffle nope on", "No such timer")]
    public void invalid_management_input_is_rejected_with_a_reason(string text, string expected) =>
        Assert.Contains(expected, Mod(NewWorld(), new TimersLogic(), text, Id()));

    [Fact]
    public void creating_validates_duplicates_and_the_group_cap()
    {
        var w = NewWorld();
        var l = new TimersLogic();
        for (var i = 0; i < TimersLogic.MaxGroups; i++)
        {
            Assert.StartsWith("Timer", Mod(w, l, $"!timer create g{i}", Id()));
        }

        Assert.Contains("maximum", Mod(w, l, "!timer create extra", Id()));
        Assert.Equal("That timer already exists.", Mod(w, l, "!timer create g1", Id()));
    }

    [Fact]
    public void lines_are_validated_capped_and_storage_is_bounded()
    {
        var (w, l) = WithTimer("news");
        Assert.Equal("You forgot the announcement text.", Mod(w, l, "!timer addline news", Id()));
        Assert.Equal("You forgot the announcement text.", Mod(w, l, "!timer addline news {}", Id()));
        Assert.Equal("Use !timer shuffle <name> on|off.", Mod(w, l, "!timer shuffle news maybe", Id()));
        Assert.Equal("Use !timer shuffle <name> on|off.", Mod(w, l, "!timer shuffle news", Id()));

        for (var i = 0; i < TimersLogic.MaxLines; i++)
        {
            Assert.StartsWith("Line", Mod(w, l, $"!timer addline news line {i}", Id()));
        }

        Assert.Contains("at most", Mod(w, l, "!timer addline news more", Id()));
    }

    [Fact]
    public void multibyte_lines_eventually_fill_the_document_and_are_refused_loudly()
    {
        var w = NewWorld();
        var l = new TimersLogic();
        var wide = new string('€', TimersLogic.MaxLineLength);
        string? last = null;
        for (var g = 0; g < TimersLogic.MaxGroups && last != "Timer storage is full."; g++)
        {
            Mod(w, l, $"!timer create g{g}", Id());
            for (var i = 0; i < TimersLogic.MaxLines && last != "Timer storage is full."; i++)
            {
                last = Mod(w, l, $"!timer addline g{g} {wide}", Id());
            }
        }

        Assert.Equal("Timer storage is full.", last);
    }

    [Fact]
    public void viewers_cannot_manage_unlinked_moderators_are_told_and_replays_apply_once()
    {
        var w = NewWorld();
        var l = new TimersLogic();
        Assert.Equal("Only moderators can manage timers.", w.Say(l, Alice, "!timer create x", Id()));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", Mod(w, l, "!timer create x", Id()));
        w.ActorFailure = null;

        Assert.StartsWith("Timer x created", Mod(w, l, "!timer create x", "same"));
        Assert.Equal("Already applied.", Mod(w, l, "!timer create x", "same"));
        Assert.Throws<InvalidOperationException>(() => Mod(w, l, "!timer create y", " "));
    }

    [Fact]
    public void fire_ignores_stale_bag_entries()
    {
        var group = new TimerGroupState("g", true, true, 10, 10, 0, true, ["a", "b"], 0, 0, [5, -1, 1], 0);

        var (line, next) = TimersLogic.Fire(group, 1000, 7);

        Assert.Equal("b", line);
        Assert.Empty(next.Bag);
        Assert.Equal(11_000, next.NextRun);
        Assert.Equal(7, next.LastCount);
    }

    [Theory]
    [InlineData("News", "news")]
    [InlineData("a_b-9", "a_b-9")]
    [InlineData("", null)]
    [InlineData("bad name", null)]
    public void timer_names_are_validated(string raw, string? expected) => Assert.Equal(expected, TimersLogic.ParseName(raw));

    [Fact]
    public void flag_off_and_no_community_do_nothing_and_leave_the_counter_alone()
    {
        var w = NewWorld();
        var l = new TimersLogic();
        Assert.Null(l.Run(TestKit.Chat("hello"), w.HostFor(Alice, Id(), community: null)));
        w.Flags.Overrides[TimersLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!timer list", Id()));
        Assert.Null(w.Kv.Get(TimersLogic.CounterKey));
    }
}

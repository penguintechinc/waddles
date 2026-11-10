using WaddleSdk.Identity;
using Xunit;
using static WaddleBundleSuperpenguinViewers.Tests.World;

namespace WaddleBundleSuperpenguinViewers.Tests;

public class ViewersLogicTests
{
    private static World NewWorld() => new(ViewersLogic.FlagKey);

    [Fact]
    public void chatters_count_as_active_for_fifteen_minutes()
    {
        var w = NewWorld();
        var l = new ViewersLogic();
        Assert.Null(w.Say(l, Alice, "hello", "m1"));
        Assert.Null(w.Say(l, Bob, "hi", "m2"));

        Assert.Equal("Active viewers: 3 (0 lurking)", w.Say(l, Carol, "!viewers", "m3"));

        w.Clock.Now += ViewersLogic.ActiveWindowMillis + 1;
        Assert.Equal("Active viewers: 1 (0 lurking)", w.Say(l, Dave, "!viewers", "m4"));
    }

    [Fact]
    public void a_lurker_stays_counted_for_an_hour_silently()
    {
        var w = NewWorld();
        var l = new ViewersLogic();
        Assert.Null(w.Say(l, Alice, "!lurk", "m1"));

        w.Clock.Now += ViewersLogic.ActiveWindowMillis + 1;
        Assert.Equal("Active viewers: 2 (1 lurking)", w.Say(l, Bob, "!viewers", "m2"));

        w.Clock.Now += ViewersLogic.LurkWindowMillis;
        Assert.Equal("Active viewers: 1 (0 lurking)", w.Say(l, Carol, "!viewers", "m3"));
    }

    [Fact]
    public void chatting_again_keeps_the_lurk_mark_and_refreshes_activity()
    {
        var w = NewWorld();
        var l = new ViewersLogic();
        w.Say(l, Alice, "!lurk", "m1");
        w.Clock.Now += 10 * 60 * 1000;
        w.Say(l, Alice, "back for a sec", "m2");

        w.Clock.Now += 10 * 60 * 1000;
        Assert.Equal("Active viewers: 2 (1 lurking)", w.Say(l, Bob, "!viewers", "m3"));
    }

    [Fact]
    public void refreshes_inside_the_throttle_window_do_not_write_but_a_lurk_always_does()
    {
        var w = NewWorld();
        var l = new ViewersLogic();
        w.Say(l, Alice, "one", "m1");
        var writes = w.Kv.SetCalls.Count;

        w.Say(l, Alice, "two", "m2");
        Assert.Equal(writes, w.Kv.SetCalls.Count);

        w.Say(l, Alice, "!lurk", "m3");
        Assert.Equal(writes + 1, w.Kv.SetCalls.Count);

        w.Clock.Now += ViewersLogic.RefreshThrottleMillis;
        w.Say(l, Alice, "three", "m4");
        Assert.Equal(writes + 2, w.Kv.SetCalls.Count);
    }

    [Fact]
    public void a_clock_that_moved_backwards_still_refreshes()
    {
        var doc = new ViewerDoc([new ViewerEntry(Alice, 10_000, 0)]);

        Assert.True(ViewersLogic.Refresh(doc, Alice, 5_000, lurk: false));
        Assert.Equal(5_000, doc.Viewers[0].LastActiveMillis);
    }

    [Fact]
    public void stale_entries_are_dropped_and_the_table_is_bounded_evicting_the_stalest()
    {
        var doc = new ViewerDoc([new ViewerEntry("old", 0, 0)]);
        ViewersLogic.Refresh(doc, "new", ViewersLogic.StaleMillis + 1, lurk: false);
        Assert.Equal(["new"], doc.Viewers.Select(v => v.User));

        var big = new ViewerDoc([]);
        for (var i = 0; i < ViewersLogic.MaxTracked + 5; i++)
        {
            ViewersLogic.Refresh(big, $"u{i}", 1000 + i, lurk: false);
        }

        Assert.Equal(ViewersLogic.MaxTracked, big.Viewers.Count);
        Assert.DoesNotContain(big.Viewers, v => v.User == "u0");
        Assert.Contains(big.Viewers, v => v.User == "u204");
    }

    [Fact]
    public void an_unlinked_speaker_is_not_tracked_but_can_still_ask()
    {
        var w = NewWorld();
        var l = new ViewersLogic();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");

        Assert.Equal("Active viewers: 0 (0 lurking)", w.Say(l, Alice, "!viewers", "m1"));
        Assert.Empty(w.Kv.SetCalls);
    }

    [Fact]
    public void other_identity_failures_propagate_loudly()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");

        Assert.Throws<WaddleIdentityException>(() => w.Say(new ViewersLogic(), Alice, "hello", "m1"));
    }

    [Fact]
    public void flag_off_and_no_community_do_nothing()
    {
        var w = NewWorld();
        var l = new ViewersLogic();
        Assert.Null(l.Run(TestKit.Chat("!viewers"), w.HostFor(Alice, "m1", community: null)));
        w.Flags.Overrides[ViewersLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!viewers", "m2"));
        Assert.Empty(w.Kv.SetCalls);
    }
}

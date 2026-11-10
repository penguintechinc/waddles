using System.Globalization;
using System.Text;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.State;
using Xunit;
using static WaddleBundleSuperpenguinFirst.Tests.World;

namespace WaddleBundleSuperpenguinFirst.Tests;

public class FirstLogicTests
{
    private static World NewWorld()
    {
        var w = new World(FirstLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol })
        {
            w.Ledger.Seed(u, 0);
        }

        return w;
    }

    private static string CounterKey(World w) =>
        StateKey.Of("first.n", DateTimeOffset.FromUnixTimeMilliseconds((long)w.Clock.Now).UtcDateTime.ToString("yyyyMMdd", CultureInfo.InvariantCulture));

    [Theory]
    [InlineData(1, 100)]
    [InlineData(2, 99)]
    [InlineData(3, 99)]
    [InlineData(4, 98)]
    [InlineData(101, 50)]
    [InlineData(199, 1)]
    [InlineData(200, 0)]
    [InlineData(500, 0)]
    public void the_award_drops_half_a_point_per_earlier_claim_floored(long position, long award) =>
        Assert.Equal(award, FirstLogic.AwardFor(position));

    [Fact]
    public void claims_pay_a_decreasing_award_by_position_through_one_credit_each()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        var a = w.Send(l, Alice, "!first", "m1");
        var b = w.Send(l, Bob, "!first", "m2");

        Assert.Contains($"{{user:{Alice}}}, Whooohooo! You came in position 1 and get 100 stars!! PogChamp Balance: 100 stars.", a);
        Assert.Contains("position 2 and get 99 stars", b);
        Assert.Equal([("credit", Alice, 100UL), ("credit", Bob, 99UL)], w.Ledger.Moved);
    }

    [Fact]
    public void a_repeat_claim_the_same_day_is_silently_ignored()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        w.Send(l, Alice, "!first", "m1");

        Assert.Null(w.Send(l, Alice, "!first", "m2"));
        Assert.Single(w.Ledger.Moved);
    }

    [Fact]
    public void the_list_resets_on_the_next_utc_day()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        w.Send(l, Alice, "!first", "m1");
        w.Clock.Now += 24UL * 60 * 60 * 1000;

        var text = w.Send(l, Alice, "!first", "m2");
        Assert.Contains("position 1 and get 100", text);
        Assert.Equal(200, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_claim_past_the_award_is_told_it_was_too_slow_and_is_not_paid_or_consumed()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        w.Kv.Increment(CounterKey(w), 199, 60);                     // the 200th claim would pay 0

        Assert.Equal(FirstLogic.TooSlow, w.Send(l, Alice, "!first", "m1"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Equal(FirstLogic.TooSlow, w.Send(l, Alice, "!first", "m2"));   // her claim was handed back, still too slow, never silent
    }

    [Fact]
    public void the_last_paying_position_still_pays()
    {
        var w = NewWorld();
        w.Kv.Increment(CounterKey(w), 198, 60);
        Assert.Contains("position 199 and get 1 stars", w.Send(new FirstLogic(), Alice, "!first", "m1"));
    }

    [Fact]
    public void resetfirst_is_refused_loudly_and_changes_nothing()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        w.Send(l, Alice, "!first", "m1");
        var sets = w.Kv.SetCalls.Count;

        Assert.Equal(FirstLogic.ResetRefusal, w.Send(l, Bob, "!resetfirst", "m2"));
        Assert.Equal(sets, w.Kv.SetCalls.Count);
        Assert.Contains(w.Log.Writes, x => x.Message == "first.reset_refused");
        Assert.Null(w.Send(l, Alice, "!first", "m3"));              // still claimed: nothing was reset
        Assert.Contains("position 2", w.Send(l, Bob, "!first", "m4"));
    }

    // ---- replay-key rules --------------------------------------------------------------------------------------------

    [Fact]
    public void true_redelivery_pays_once_and_reprints_the_same_position()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        var first = w.Send(l, Alice, "!first", "m1");
        w.Send(l, Bob, "!first", "m2");
        var again = w.Send(l, Alice, "!first", "m1");
        var third = w.Send(l, Alice, "!first", "m1");

        Assert.Equal(first, again);
        Assert.Equal(first, third);
        Assert.Equal(2, w.Ledger.Moved.Count);
        Assert.Equal(100, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_crash_between_the_record_and_the_credit_is_completed_once_by_the_redelivery()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        w.Ledger.BeforeCall = (_, _) => throw new InvalidOperationException("crash");
        Assert.Throws<InvalidOperationException>(() => w.Send(l, Alice, "!first", "m1"));
        w.Ledger.BeforeCall = null;
        Assert.Equal(0, w.Ledger.Balance(Alice));

        Assert.Contains("position 1 and get 100", w.Send(l, Alice, "!first", "m1"));
        Assert.Equal(100, w.Ledger.Balance(Alice));
        Assert.Single(w.Ledger.Moved);
    }

    [Theory]
    [InlineData("")]
    [InlineData("\t")]
    public void a_blank_message_id_is_refused_loudly(string id)
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => w.Send(new FirstLogic(), Alice, "!first", id));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_same_message_id_from_a_different_actor_is_not_a_replay()
    {
        var w = NewWorld();
        var l = new FirstLogic();
        var a = w.Send(l, Alice, "!first", "same");
        var b = w.Send(l, Bob, "!first", "same");

        Assert.NotEqual(a, b);
        Assert.Contains("position 2", b);
        Assert.Equal(99, w.Ledger.Balance(Bob));
    }

    [Fact]
    public void a_record_for_another_actor_or_command_is_never_replayed()
    {
        var w = NewWorld();
        var key = ReplayGuard.KeyFor("first.out", Msg("!first"), w.HostFor(Alice, "m1"), Alice, "first");
        var store = new KvStateStore(w.Kv);
        store.SetJson(key, new FirstOutcome(Bob, "first", 1, 5000), FirstJsonContext.Default.FirstOutcome, 60);
        Assert.Contains("position 1 and get 100", w.Send(new FirstLogic(), Alice, "!first", "m1"));

        var w2 = NewWorld();
        var key2 = ReplayGuard.KeyFor("first.out", Msg("!first"), w2.HostFor(Alice, "m1"), Alice, "first");
        new KvStateStore(w2.Kv).SetJson(key2, new FirstOutcome(Alice, "resetfirst", 1, 5000), FirstJsonContext.Default.FirstOutcome, 60);
        Assert.Contains("get 100 stars", w2.Send(new FirstLogic(), Alice, "!first", "m1"));
    }

    [Fact]
    public void an_over_cap_mint_propagates_loudly()
    {
        var w = NewWorld();
        w.Ledger.MintCap = 10;
        var ex = Assert.Throws<WaddleEconomyException>(() => w.Send(new FirstLogic(), Alice, "!first", "m1"));
        Assert.Equal(EconomyErrorKind.OverCap, ex.Kind);
        Assert.Empty(w.Ledger.Moved);
    }

    // ---- gating ------------------------------------------------------------------------------------------------------

    [Fact]
    public void the_flag_gates_the_bundle_including_resetfirst()
    {
        var w = NewWorld();
        w.Flags.Overrides[FirstLogic.FlagKey] = false;
        Assert.Null(w.Send(new FirstLogic(), Alice, "!first", "m1"));
        Assert.Null(w.Send(new FirstLogic(), Alice, "!resetfirst", "m2"));
        Assert.Empty(w.Kv.SetCalls);
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!firstx")]
    [InlineData("!")]
    public void unrelated_chat_is_ignored(string text) => Assert.Null(NewWorld().Send(new FirstLogic(), Alice, text, "m1"));

    [Fact]
    public void no_community_and_unlinked_actors_get_a_reply_and_other_identity_failures_propagate()
    {
        var w = NewWorld();
        var noCommunity = new FirstLogic().Run(Msg("!first"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("not linked to one", TextOf(noCommunity!));

        foreach (var kind in new[] { IdentityErrorKind.NotLinked, IdentityErrorKind.NotAMember })
        {
            w.ActorFailure = new WaddleIdentityException(kind, "no");
            Assert.Contains("Link your account", w.Send(new FirstLogic(), Alice, "!first", "m2"));
        }

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");
        Assert.Throws<WaddleIdentityException>(() => w.Send(new FirstLogic(), Alice, "!first", "m3"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_alias_falls_back_to_the_default_when_the_tool_cannot_answer_or_answers_blank()
    {
        var w = NewWorld();
        w.Tools.Handlers.Remove((EconomyServiceTools.Target, "points_view"));
        Assert.Contains("get 100 waddlebucks", w.Send(new FirstLogic(), Alice, "!first", "m1"));
        Assert.Contains(w.Log.Writes, x => x.Message == "first.alias_unavailable");

        var w2 = NewWorld();
        w2.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\"\"}");
        Assert.Contains("get 100 waddlebucks", w2.Send(new FirstLogic(), Alice, "!first", "m1"));
    }
}

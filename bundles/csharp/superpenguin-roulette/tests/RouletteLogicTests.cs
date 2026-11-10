using System.Globalization;
using System.Text;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.State;
using WaddleSdk.Tests;
using Xunit;
using static WaddleBundleSuperpenguinRoulette.Tests.World;

namespace WaddleBundleSuperpenguinRoulette.Tests;

public class RouletteLogicTests
{
    private const long Start = 1000;

    private static World NewWorld()
    {
        var w = new World(RouletteLogic.FlagKey);
        w.Ledger.Seed(Alice, Start);
        w.Ledger.Seed(Bob, Start);
        return w;
    }

    private static RouletteLogic Spin(int value) => new(new ScriptedRandom(value).Next);

    private static string DayTotalKey(World w, string actor) =>
        StateKey.Of("rl.tot", DateTimeOffset.FromUnixTimeMilliseconds((long)w.Clock.Now).UtcDateTime.ToString("yyyyMMdd", CultureInfo.InvariantCulture), StateKey.Hash(actor));

    [Fact]
    public void a_roll_over_the_threshold_wins_even_money_through_one_atomic_wager()
    {
        var w = NewWorld();
        var text = w.Send(Spin(80), Alice, "!roulette 10", "m1");

        Assert.Equal([("wager", Alice, 10UL)], w.Ledger.Moved);
        Assert.Equal(Start + 10, w.Ledger.Balance(Alice));
        Assert.Contains($"{{user:{Alice}}} rolled a 80 and won 10 stars in the roulette and now has 1010 stars!", text);
        Assert.Contains("Rouletted 10 of 1000 limit per day", text);
    }

    [Theory]
    [InlineData(52)]
    [InlineData(1)]
    public void a_roll_at_or_under_the_threshold_loses_the_stake(int value)
    {
        var w = NewWorld();
        var text = w.Send(Spin(value), Alice, "!roulette 10", "m1");

        Assert.Equal(Start - 10, w.Ledger.Balance(Alice));
        Assert.Contains($"rolled a {value} and lost 10 stars in the roulette and now has 990 stars!", text);
    }

    [Theory]
    [InlineData("all")]
    [InlineData("MAX")]
    public void all_and_max_bet_everything_clamped_to_the_per_bet_limit(string arg)
    {
        var w = NewWorld();
        w.Send(Spin(80), Alice, $"!roulette {arg}", "m1");

        Assert.Equal(RouletteLogic.MaxPerBet, w.Ledger.Moved.Single().Amount);
    }

    [Fact]
    public void a_small_balance_bets_everything_it_has()
    {
        var w = NewWorld();
        w.Ledger.Seed(Alice, 37);
        w.Send(Spin(80), Alice, "!roulette all", "m1");

        Assert.Equal(37UL, w.Ledger.Moved.Single().Amount);
    }

    [Fact]
    public void the_daily_total_clamps_the_bet_to_what_is_left_and_then_refuses()
    {
        var w = NewWorld();
        var store = new KvStateStore(w.Kv);
        store.Set(DayTotalKey(w, Alice), Encoding.UTF8.GetBytes("800"), 60);

        var text = w.Send(Spin(80), Alice, "!roulette 500", "m1");
        Assert.Equal(200UL, w.Ledger.Moved.Single().Amount);
        Assert.Contains("Rouletted 1000 of 1000 limit per day", text);

        new KvStateStore(w.Kv).Set(DayTotalKey(w, Bob), Encoding.UTF8.GetBytes("1000"), 60);
        Assert.Contains("reached your max per day limit for !roulette (1000 stars)", w.Send(Spin(80), Bob, "!roulette 5", "m2"));
    }

    [Fact]
    public void the_daily_total_resets_on_the_next_utc_day()
    {
        var w = NewWorld();
        new KvStateStore(w.Kv).Set(DayTotalKey(w, Alice), Encoding.UTF8.GetBytes("1000"), 60);
        Assert.Contains("reached your max per day limit", w.Send(Spin(80), Alice, "!roulette 5", "m1"));

        w.Clock.Now += 24UL * 60 * 60 * 1000;
        Assert.Contains("rolled a 80", w.Send(Spin(80), Alice, "!roulette 5", "m2"));
    }

    [Theory]
    [InlineData("!roulette", "To roulette please do !roulette Amount/All/Max")]
    [InlineData("!roulette 5 6", "To roulette please do !roulette Amount/All/Max")]
    [InlineData("!roulette abc", "The amount must be a number, max, or all")]
    [InlineData("!roulette 0", "The amount needs to be greater then 0")]
    [InlineData("!roulette -3", "The amount needs to be greater then 0")]
    [InlineData("!roulette 5000", "You don't have that many.")]
    public void bad_input_gets_the_originals_reply_and_moves_nothing_and_keeps_the_cooldown_free(string text, string expected)
    {
        var w = NewWorld();
        Assert.StartsWith(expected, w.Send(Spin(80), Alice, text, "m1"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Contains("rolled a 80", w.Send(Spin(80), Alice, "!roulette 5", "m2"));
    }

    [Fact]
    public void all_with_nothing_to_bet_is_refused()
    {
        var w = NewWorld();
        w.Ledger.Seed(Alice, 0);
        Assert.Equal("You don't have that many.", w.Send(Spin(80), Alice, "!roulette all", "m1"));
    }

    [Fact]
    public void the_cooldown_silently_drops_a_second_spin()
    {
        var w = NewWorld();
        w.Send(Spin(80), Alice, "!roulette 5", "m1");
        Assert.Null(w.Send(Spin(80), Alice, "!roulette 5", "m2"));
        Assert.Single(w.Ledger.Moved);
    }

    // ---- replay-key rules --------------------------------------------------------------------------------------------

    [Fact]
    public void true_redelivery_moves_money_once_and_reprints_the_same_outcome()
    {
        var w = NewWorld();
        var first = w.Send(Spin(80), Alice, "!roulette 10", "m1");
        var again = w.Send(new RouletteLogic(), Alice, "!roulette 10", "m1");
        var third = w.Send(new RouletteLogic(), Alice, "!roulette 10", "m1");

        Assert.Equal(first, again);
        Assert.Equal(first, third);
        Assert.Single(w.Ledger.Moved);
        Assert.Equal(Start + 10, w.Ledger.Balance(Alice));
        Assert.Equal("10", Encoding.UTF8.GetString(w.Kv.Get(DayTotalKey(w, Alice))!));   // the daily total is not double counted
    }

    [Theory]
    [InlineData("")]
    [InlineData("  ")]
    public void a_blank_message_id_is_refused_loudly(string id)
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => w.Send(Spin(80), Alice, "!roulette 10", id));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_same_message_id_from_a_different_actor_is_not_a_replay()
    {
        var w = NewWorld();
        var a = w.Send(Spin(80), Alice, "!roulette 10", "same");
        var b = w.Send(Spin(1), Bob, "!roulette 20", "same");

        Assert.NotEqual(a, b);
        Assert.Contains("lost 20 stars", b);
        Assert.Equal(Start - 20, w.Ledger.Balance(Bob));
        Assert.Equal(2, w.Ledger.Moved.Count);
    }

    [Theory]
    [InlineData("other-actor", "roulette", "!roulette 10")]
    [InlineData("self", "roulette", "!roulette 99")]
    public void a_record_that_does_not_match_actor_and_arguments_is_never_replayed(string who, string cmd, string recordedArgsText)
    {
        var w = NewWorld();
        var ev = Msg("!roulette 10");
        var key = ReplayGuard.KeyFor("rl.out", ev, w.HostFor(Alice, "m1"), Alice, "roulette");
        var recordedActor = who == "self" ? Alice : Bob;
        var planted = new RouletteOutcome(recordedActor, cmd, StateKey.Hash(recordedArgsText), 400, 99, 400);
        new KvStateStore(w.Kv).SetJson(key, planted, RouletteJsonContext.Default.RouletteOutcome, 60);

        var text = w.Send(Spin(1), Alice, "!roulette 10", "m1");

        Assert.Contains("lost 10 stars", text);                 // fresh validation and a fresh roll, not the planted 400-stake win
        Assert.Equal(Start - 10, w.Ledger.Balance(Alice));
    }

    // ---- refusals from the host --------------------------------------------------------------------------------------

    [Fact]
    public void insufficient_funds_at_the_wager_replies_and_hands_back_the_cooldown_and_record()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "raced", 0);
        Assert.Contains("you don't have 10 stars to bet", w.Send(Spin(80), Alice, "!roulette 10", "m1"));

        w.Ledger.BeforeCall = null;
        Assert.Contains("rolled a 80", w.Send(Spin(80), Alice, "!roulette 10", "m2"));
    }

    [Fact]
    public void an_over_cap_stake_replies_with_the_limit()
    {
        var w = NewWorld();
        w.Ledger.MaxBetCap = 100;
        Assert.Contains("that bet is over the limit for stars", w.Send(Spin(80), Alice, "!roulette 200", "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_denied_wager_propagates_and_never_becomes_a_reply()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Denied, "not_granted");
        Assert.Throws<WaddleEconomyException>(() => w.Send(Spin(80), Alice, "!roulette 10", "m1"));
    }

    // ---- gating ------------------------------------------------------------------------------------------------------

    [Fact]
    public void the_flag_gates_the_bundle()
    {
        var w = NewWorld();
        w.Flags.Overrides[RouletteLogic.FlagKey] = false;
        Assert.Null(w.Send(Spin(80), Alice, "!roulette 10", "m1"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Empty(w.Kv.SetCalls);
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!roulettex 5")]
    [InlineData("!")]
    public void unrelated_chat_is_ignored(string text)
    {
        Assert.Null(NewWorld().Send(Spin(80), Alice, text, "m1"));
    }

    [Fact]
    public void no_community_and_unlinked_actors_get_a_reply_and_other_identity_failures_propagate()
    {
        var w = NewWorld();
        var noCommunity = Spin(80).Run(Msg("!roulette 5"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("not linked to one", TextOf(noCommunity!));

        foreach (var kind in new[] { IdentityErrorKind.NotLinked, IdentityErrorKind.NotAMember })
        {
            w.ActorFailure = new WaddleIdentityException(kind, "no");
            Assert.Contains("Link your account", w.Send(Spin(80), Alice, "!roulette 5", "m2"));
        }

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");
        Assert.Throws<WaddleIdentityException>(() => w.Send(Spin(80), Alice, "!roulette 5", "m3"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_alias_falls_back_to_the_documented_default_when_the_tool_cannot_answer_or_answers_blank()
    {
        var w = NewWorld();
        w.Tools.Handlers.Remove((EconomyServiceTools.Target, "points_view"));
        Assert.Contains("in the roulette and now has 1010 waddlebucks", w.Send(Spin(80), Alice, "!roulette 10", "m1"));
        Assert.Contains(w.Log.Writes, x => x.Message == "roulette.alias_unavailable");

        var w2 = NewWorld();
        w2.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\"\"}");
        Assert.Contains("1010 waddlebucks", w2.Send(Spin(80), Alice, "!roulette 10", "m1"));
    }

    [Fact]
    public void the_default_random_source_produces_a_roll_in_range()
    {
        var w = NewWorld();
        var text = w.Send(new RouletteLogic(), Alice, "!roulette 1", "m1");
        Assert.Matches(@"rolled a (\d{1,3}) and (won|lost) 1 stars", text!);
    }
}

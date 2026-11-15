using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.State;
using WaddleSdk.Tool;
using Xunit;
using static WaddleBundleSuperpenguinHeist.Tests.World;

namespace WaddleBundleSuperpenguinHeist.Tests;

public class HeistLogicTests
{
    private const long Start = 1000;
    private const string Eve = "55555555-5555-4555-8555-555555555555";

    private static World NewWorld()
    {
        var w = new World(HeistLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave, Eve })
        {
            w.Ledger.Seed(u, Start);
        }

        return w;
    }

    private static HeistLogic Rolls(params int[] values) => new(new ScriptedRandom(values).Next);

    private static string U(string uuid) => $"{{user:{uuid}}}";

    private static string? Heist(World w, HeistLogic l, string who, string id, string text = "!heist 100") => w.Send(l, who, text, id);

    private static void EndWindow(World w) => w.Clock.Now += HeistLogic.JoinMillis + 1_000;

    // ---- happy paths -------------------------------------------------------------------------------------------------

    [Fact]
    public void the_first_heist_opens_a_crew_and_debits_only_the_opener()
    {
        var w = NewWorld();
        var text = Heist(w, Rolls(), Alice, "m1");

        Assert.Equal($"{U(Alice)} is trying to get a team together for some serious heist business! use \"!heist AMOUNT/ALL/MAX\" to join!", text);
        Assert.Equal(Start - 100, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
        Assert.Equal([("debit", Alice, 100UL)], w.Ledger.Moved);
    }

    [Fact]
    public void later_joiners_are_silent_like_upstream_and_pay_their_own_bet()
    {
        var w = NewWorld();
        var l = Rolls();
        Heist(w, l, Alice, "m1");

        Assert.Null(Heist(w, l, Bob, "m2", "!heist 50"));
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));
        Assert.Equal(2, w.Ledger.Moved.Count);
    }

    [Fact]
    public void settlement_pays_survivors_bet_plus_one_and_a_half_times_and_caught_players_lose_the_bet()
    {
        var w = NewWorld();
        var l = Rolls(90, 10, 41); // Alice survives, Bob (roll 10) caught, Carol (roll 41 > 40) survives
        Heist(w, l, Alice, "m1", "!heist 100");
        Heist(w, l, Bob, "m2", "!heist 50");
        Heist(w, l, Carol, "m3", "!heist 20");
        EndWindow(w);

        var text = Heist(w, l, Dave, "m4");

        Assert.Contains($"Look out! The troll captured {U(Bob)}", text);
        Assert.Contains($"Survivors are: {U(Alice)} (250), {U(Carol)} (50).", text);
        Assert.EndsWith($"{U(Dave)}, you can not join the heist now.", text);
        Assert.Equal(Start + 150, w.Ledger.Balance(Alice));
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));
        Assert.Equal(Start + 30, w.Ledger.Balance(Carol));
        Assert.Equal(Start, w.Ledger.Balance(Dave));
    }

    [Fact]
    public void a_roll_of_exactly_forty_is_caught()
    {
        var w = NewWorld();
        var l = Rolls(40);
        Heist(w, l, Alice, "m1");
        EndWindow(w);

        var text = Heist(w, l, Bob, "m2");

        Assert.Contains("The heist ended! There are no survivors.", text);
        Assert.Equal(Start - 100, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_large_surviving_crew_is_reported_by_counts_only()
    {
        var w = NewWorld();
        var users = Enumerable.Range(0, 10).Select(i => $"00000000-0000-4000-8000-{i:D12}").ToList();
        foreach (var u in users)
        {
            w.Ledger.Seed(u, Start);
        }

        var l = Rolls(users.Select(_ => 99).Concat([1]).ToArray()[..9].Concat([1]).ToArray());
        for (var i = 0; i < users.Count; i++)
        {
            Heist(w, l, users[i], "j" + i, "!heist 10");
        }

        EndWindow(w);
        var text = Heist(w, l, Alice, "settle");

        Assert.Contains("The heist ended with 9 survivor(s) and 1 death(s).", text);
        Assert.DoesNotContain("Survivors are", text);
    }

    [Fact]
    public void the_cooldown_after_a_heist_drops_new_heists_silently_until_it_expires()
    {
        var w = NewWorld();
        var l = Rolls(99);
        Heist(w, l, Alice, "m1");
        EndWindow(w);
        Heist(w, l, Bob, "m2");
        var moved = w.Ledger.Moved.Count;

        Assert.Null(Heist(w, l, Carol, "m3"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        w.Kv.Delete(HeistLogic.CooldownKey); // the fake kv has no TTL clock: simulate the expiry
        Assert.Contains("is trying to get a team together", Heist(w, l, Carol, "m4"));
    }

    // ---- bet parsing and bounds --------------------------------------------------------------------------------------

    [Theory]
    [InlineData("!heist all", 500)]            // capped at the economy's max bet
    [InlineData("!heist MAX", 500)]
    [InlineData("!heist 50%", 250)]
    [InlineData("!heist 100%", 500)]
    [InlineData("!heist 12.5%", 62)]
    public void all_max_and_percentages_resolve_against_the_max_bet(string text, long bet)
    {
        var w = NewWorld();
        Heist(w, Rolls(), Alice, "m1", text);

        Assert.Equal(Start - bet, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void all_uses_the_whole_balance_when_it_is_below_the_cap()
    {
        var w = NewWorld();
        w.Ledger.Seed(Alice, 80);
        Heist(w, Rolls(), Alice, "m1", "!heist all");

        Assert.Equal(0, w.Ledger.Balance(Alice));
    }

    [Theory]
    [InlineData("!heist")]
    [InlineData("!heist abc")]
    [InlineData("!heist 0%")]
    [InlineData("!heist 101%")]
    [InlineData("!heist %")]
    [InlineData("!heist 5%%")]
    [InlineData("!heist x%")]
    public void malformed_arguments_get_the_usage_line_and_never_touch_money(string text)
    {
        var w = NewWorld();
        Assert.Equal("To Enter/Start a heist do !heist AMOUNT/ALL/MAX/%", Heist(w, Rolls(), Alice, "m1", text));
        Assert.Empty(w.Ledger.Moved);
        Assert.Null(w.Kv.Get(HeistLogic.RecKey));
    }

    [Theory]
    [InlineData("!heist 9")]
    [InlineData("!heist -5")]
    [InlineData("!heist 501")]
    public void bets_outside_the_bounds_are_refused_with_the_limits(string text)
    {
        var w = NewWorld();
        Assert.Equal("The max amount to join the heist is 500 stars and must be greater then 10 stars", Heist(w, Rolls(), Alice, "m1", text));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_bet_above_the_balance_is_not_enough_points_and_leaves_no_state()
    {
        var w = NewWorld();
        w.Ledger.Seed(Alice, 50);

        Assert.Equal("sorry you don't have that amount of stars to enter the heist.", Heist(w, Rolls(), Alice, "m1", "!heist 100"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Contains("is trying to get a team", Heist(w, Rolls(), Bob, "m2")); // no stale opener claim
    }

    // ---- refusals and races ------------------------------------------------------------------------------------------

    [Fact]
    public void joining_twice_is_refused_and_does_not_charge_again()
    {
        var w = NewWorld();
        var l = Rolls();
        Heist(w, l, Alice, "m1");

        Assert.Equal("you have already joined the heist.", Heist(w, l, Alice, "m2"));
        Assert.Single(w.Ledger.Moved);
    }

    [Fact]
    public void a_debit_refused_after_the_precheck_aborts_cleanly_and_releases_every_claim()
    {
        var w = NewWorld();
        var l = Rolls();
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
            {
                throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "raced", 0);
            }
        };

        var text = Heist(w, l, Alice, "m1");
        Assert.Contains("don't have that amount", text);
        Assert.Empty(w.Ledger.Moved);
        Assert.Equal(text, Heist(w, l, Alice, "m1"));

        w.Ledger.BeforeCall = null;
        Assert.Contains("is trying to get a team", Heist(w, l, Alice, "m2"));
    }

    [Fact]
    public void an_over_cap_debit_also_aborts_cleanly()
    {
        var w = NewWorld();
        var l = Rolls();
        Heist(w, l, Alice, "m1");
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 40);
            }
        };

        Assert.Contains("don't have that amount", Heist(w, l, Bob, "m2"));
        w.Ledger.BeforeCall = null;
        Assert.Null(Heist(w, l, Bob, "m3"));
        Assert.Equal(Start - 100, w.Ledger.Balance(Bob));
    }

    [Fact]
    public void the_crew_is_full_after_the_entrant_cap()
    {
        var w = NewWorld();
        var l = Rolls(Enumerable.Repeat(99, HeistLogic.MaxEntrants).ToArray());
        for (var i = 0; i < HeistLogic.MaxEntrants; i++)
        {
            var user = $"00000000-0000-4000-8000-{i:D12}";
            w.Ledger.Seed(user, Start);
            Heist(w, l, user, "j" + i, "!heist 10");
        }

        Assert.Equal("The heist crew is full.", Heist(w, l, Alice, "late"));
        Assert.Equal(Start, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_player_racing_an_unregistered_opener_is_asked_to_retry()
    {
        var w = NewWorld();
        w.Kv.Set(HeistLogic.LiveKey, [1], 60);
        w.Kv.Set("heist.liveown", System.Text.Encoding.UTF8.GetBytes("someone-else"), 60);

        Assert.Equal("The heist is starting, try again in a moment.", Heist(w, Rolls(), Alice, "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    // ---- settlement safety -------------------------------------------------------------------------------------------

    [Fact]
    public void a_crash_mid_settlement_pays_each_survivor_exactly_once_on_redelivery()
    {
        var w = NewWorld();
        var l = Rolls(99, 99);
        Heist(w, l, Alice, "m1");
        Heist(w, l, Bob, "m2");
        EndWindow(w);

        // The settling event pays Alice, then crashes before Bob's credit.
        w.Ledger.AfterCall = (kind, ordinal) => { if (kind == "credit" && ordinal == 0) { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => Heist(w, l, Carol, "m3"));
        w.Ledger.AfterCall = null;
        Assert.Equal(Start + 150, w.Ledger.Balance(Alice));
        Assert.Equal(Start - 100, w.Ledger.Balance(Bob));

        // A different command meets the owned settle claim and moves nothing.
        var moved = w.Ledger.Moved.Count;
        Assert.Equal("you can not join the heist now.", Heist(w, l, Dave, "m4"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        // The redelivered settling event resumes the SAME plan: Alice is not paid twice, Bob is paid once.
        Assert.Contains("Survivors are", Heist(w, l, Carol, "m3"));
        Assert.Equal(Start + 150, w.Ledger.Balance(Alice));
        Assert.Equal(Start + 150, w.Ledger.Balance(Bob));
    }

    [Fact]
    public void replayed_events_move_money_once_and_reprint_the_same_reply()
    {
        var w = NewWorld();
        var l = Rolls(99, 10);
        var open = Heist(w, l, Alice, "m1");
        var join = Heist(w, l, Bob, "m2");
        EndWindow(w);
        var settle = Heist(w, l, Carol, "m3");
        var moved = w.Ledger.Moved.ToList();
        var supply = w.Ledger.Supply;

        Assert.Equal(open, Heist(w, l, Alice, "m1"));
        Assert.Equal(join, Heist(w, l, Bob, "m2"));
        Assert.Equal(settle, Heist(w, l, Carol, "m3"));
        Assert.Equal(moved, w.Ledger.Moved);
        Assert.Equal(supply, w.Ledger.Supply);
    }

    [Fact]
    public void a_replayed_opener_never_reopens_a_settled_heist()
    {
        var w = NewWorld();
        var l = Rolls(99);
        Heist(w, l, Alice, "m1");
        EndWindow(w);
        Heist(w, l, Bob, "m2");
        Heist(w, l, Alice, "m1");

        Assert.Null(w.Kv.Get(HeistLogic.RecKey));
    }

    [Fact]
    public void a_join_redelivered_after_its_heist_was_settled_is_refunded_not_lost()
    {
        var w = NewWorld();
        var l = Rolls(99);
        Heist(w, l, Alice, "m1");

        // Bob's join plans and debits, then crashes before its entrant is written.
        w.Ledger.AfterCall = (kind, _) => { if (kind == "debit") { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => Heist(w, l, Bob, "m2", "!heist 50"));
        w.Ledger.AfterCall = null;
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));

        // Inside the grace period the claimed-but-unwritten slot defers settlement.
        EndWindow(w);
        var moved = w.Ledger.Moved.Count;
        Assert.Equal("you can not join the heist now.", Heist(w, l, Dave, "m3"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        w.Clock.Now += HeistLogic.InflightGraceMillis;
        Assert.Contains("Survivors are", Heist(w, l, Dave, "m4"));

        // Bob's join is redelivered: the debit is de-duplicated and the bet is refunded.
        Assert.Contains("your 50 stars was refunded", Heist(w, l, Bob, "m2", "!heist 50"));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
    }

    [Fact]
    public void an_empty_settlement_with_only_an_unwritten_slot_past_the_grace_closes_without_money()
    {
        var w = NewWorld();
        var l = Rolls();
        w.Ledger.AfterCall = (kind, _) => { if (kind == "debit") { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => Heist(w, l, Alice, "m1"));
        w.Ledger.AfterCall = null;
        // The opener's record was never written, so there is nothing to settle and a new opener is still blocked by its claim.
        Assert.Equal("The heist is starting, try again in a moment.", Heist(w, l, Bob, "m2"));
    }

    // ---- gating and identity -----------------------------------------------------------------------------------------

    [Fact]
    public void the_bundle_is_silent_when_the_flag_is_off_and_ignores_other_commands()
    {
        var w = NewWorld();
        w.Flags.Overrides[HeistLogic.FlagKey] = false;
        Assert.Null(Heist(w, Rolls(), Alice, "m1"));
        w.Flags.Overrides[HeistLogic.FlagKey] = true;
        Assert.Null(Heist(w, Rolls(), Alice, "m2", "!ffa"));
        Assert.Null(Heist(w, Rolls(), Alice, "m3", "hello"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_channel_without_a_community_and_an_unlinked_actor_are_refused()
    {
        var w = NewWorld();
        var l = Rolls();
        var noCommunity = l.Run(Msg("!heist 100"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("need a community", TextOf(noCommunity!));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Heist(w, l, Alice, "m2"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Heist(w, l, Alice, "m3"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Unavailable, "down");
        Assert.Throws<WaddleIdentityException>(() => Heist(w, l, Alice, "m4"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_blank_message_id_is_refused_loudly_before_any_money_moves()
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => Heist(w, Rolls(), Alice, " "));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_alias_falls_back_to_the_default_with_a_warning_when_the_service_cannot_answer()
    {
        var w = NewWorld();
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => throw new WaddleToolException(ToolErrorKind.TargetUnavailable, "down");
        w.Ledger.Seed(Alice, 5);

        Assert.Contains("amount of waddlebucks", Heist(w, Rolls(), Alice, "m1"));
        Assert.Contains(w.Log.Writes, x => x.Message == "heist.alias_unavailable");

        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => System.Text.Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\"\"}");
        Assert.Contains("amount of waddlebucks", Heist(w, Rolls(), Alice, "m2"));
    }

    [Fact]
    public void tampered_stored_plans_fail_loud_instead_of_being_skipped()
    {
        var w = NewWorld();
        var l = Rolls();
        Heist(w, l, Alice, "m1");
        var planKey = StateKey.Of("heist.ev", StateKey.Hash("m1|twitch|" + Alice + "|heist"));
        var plan = System.Text.Json.JsonSerializer.Deserialize(w.Kv.Get(planKey)!, HeistJsonContext.Default.EventPlan)!;

        void Plant(EventPlan p) => w.Kv.Set(planKey, System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(p, HeistJsonContext.Default.EventPlan), 60);

        Plant(plan with { Own = new PlanSection([new MoneyOp("steal", Alice, 1)], []) });
        Assert.Throws<InvalidOperationException>(() => Heist(w, l, Alice, "m1"));
        Plant(plan with { Own = new PlanSection([], [new Effect("explode", "x", 0, string.Empty, 0, null)]) });
        Assert.Throws<InvalidOperationException>(() => Heist(w, l, Alice, "m1"));
        Plant(plan with { Own = new PlanSection([], [new Effect("open", "x", 0, string.Empty, 0, null)]) });
        Assert.Throws<InvalidOperationException>(() => Heist(w, l, Alice, "m1"));
    }
}

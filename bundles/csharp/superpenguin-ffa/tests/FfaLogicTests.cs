using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.State;
using WaddleSdk.Tool;
using Xunit;
using static WaddleBundleSuperpenguinFfa.Tests.World;

namespace WaddleBundleSuperpenguinFfa.Tests;

public class FfaLogicTests
{
    private const long Start = 1000;
    private const string Eve = "55555555-5555-4555-8555-555555555555";

    private static World NewWorld()
    {
        var w = new World(FfaLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave, Eve })
        {
            w.Ledger.Seed(u, Start);
        }

        return w;
    }

    private static FfaLogic Picks(params int[] values) => new(new ScriptedRandom(values).Next);

    private static string U(string uuid) => $"{{user:{uuid}}}";

    private static string? Ffa(World w, FfaLogic l, string who, string id, string text = "!ffa") => w.Send(l, who, text, id);

    private static void EndWindow(World w) => w.Clock.Now += FfaLogic.JoinMillis + 1_000;

    // ---- happy paths -------------------------------------------------------------------------------------------------

    [Fact]
    public void the_first_ffa_opens_a_game_and_debits_only_the_opener()
    {
        var w = NewWorld();
        var text = Ffa(w, Picks(), Alice, "m1");

        Assert.Equal($"{U(Alice)} is starting a FFA battle! Type !ffa to join now!", text);
        Assert.Equal(Start - 100, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
        Assert.Equal([("debit", Alice, 100UL)], w.Ledger.Moved);
    }

    [Fact]
    public void later_entrants_are_announced_and_each_pay_their_own_fee()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        Assert.Equal($"{U(Bob)} joined the FFA!", Ffa(w, l, Bob, "m2"));
        Ffa(w, l, Carol, "m3");

        Assert.Equal(Start - 100, w.Ledger.Balance(Bob));
        Assert.Equal(Start - 100, w.Ledger.Balance(Carol));
        Assert.Equal(3, w.Ledger.Moved.Count);
    }

    [Fact]
    public void the_first_command_after_the_window_settles_it_pays_the_pot_to_one_winner_and_answers_too_late()
    {
        var w = NewWorld();
        var l = Picks(1); // index 1 of [Alice, Bob, Carol] -> Bob
        Ffa(w, l, Alice, "m1");
        Ffa(w, l, Bob, "m2");
        Ffa(w, l, Carol, "m3");
        EndWindow(w);

        var text = Ffa(w, l, Dave, "m4");

        Assert.Contains($"the last one standing is {U(Bob)} and gets 300 stars!", text);
        Assert.EndsWith("The FFA has already started, you are to late to join this one.", text);
        Assert.Equal(Start + 200, w.Ledger.Balance(Bob));
        Assert.Equal(Start - 100, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(Dave));          // the late command never joins or pays
        Assert.Equal(5 * Start, w.Ledger.Supply);             // burn + mint of the same amount
    }

    [Fact]
    public void a_lone_entrant_gets_the_fee_back()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        EndWindow(w);

        var text = Ffa(w, l, Bob, "m2");

        Assert.StartsWith("Not enough viewers joined the FFA, returning the fees.", text);
        Assert.Equal(Start, w.Ledger.Balance(Alice));
        Assert.Equal(5 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void the_cooldown_after_a_game_drops_new_games_silently_until_it_expires()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        EndWindow(w);
        Ffa(w, l, Bob, "m2");
        var moved = w.Ledger.Moved.Count;

        Assert.Null(Ffa(w, l, Carol, "m3"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        w.Kv.Delete(FfaLogic.CooldownKey); // the fake kv has no TTL clock: simulate the 300 s expiry
        Assert.Contains("is starting a FFA battle", Ffa(w, l, Carol, "m4"));
        Assert.Equal(Start - 100, w.Ledger.Balance(Carol));
    }

    // ---- refusals ----------------------------------------------------------------------------------------------------

    [Fact]
    public void joining_twice_is_refused_and_does_not_charge_again()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        Ffa(w, l, Bob, "m2");

        Assert.Equal("You have already joined the FFA!", Ffa(w, l, Bob, "m3"));
        Assert.Equal("You have already joined the FFA!", Ffa(w, l, Alice, "m4"));
        Assert.Equal(Start - 100, w.Ledger.Balance(Bob));
        Assert.Equal(2, w.Ledger.Moved.Count);
    }

    [Fact]
    public void a_player_without_the_fee_is_refused_without_state_or_money_and_cannot_open_a_game()
    {
        var w = NewWorld();
        w.Ledger.Seed(Alice, 50);
        var text = Ffa(w, Picks(), Alice, "m1");

        Assert.Equal("Sorry it costs 100 stars to join the FFA game which you do not have.", text);
        Assert.Empty(w.Ledger.Moved);
        Assert.Contains("is starting a FFA battle", Ffa(w, Picks(), Bob, "m2")); // no stale live claim
    }

    [Fact]
    public void a_debit_refused_after_the_precheck_aborts_cleanly_and_releases_every_claim()
    {
        var w = NewWorld();
        var l = Picks();
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
            {
                throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "raced", 0);
            }
        };

        var text = Ffa(w, l, Alice, "m1");
        Assert.Contains("costs 100 stars", text);
        Assert.Empty(w.Ledger.Moved);
        Assert.Equal(text, Ffa(w, l, Alice, "m1")); // the aborted plan replays as the same refusal

        w.Ledger.BeforeCall = null;
        Assert.Contains("is starting a FFA battle", Ffa(w, l, Alice, "m2")); // opener claim, slot and join marker were released
    }

    [Fact]
    public void an_over_cap_debit_also_aborts_cleanly()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 40);
            }
        };

        Assert.Contains("costs 100 stars", Ffa(w, l, Bob, "m2"));
        w.Ledger.BeforeCall = null;
        Assert.Equal($"{U(Bob)} joined the FFA!", Ffa(w, l, Bob, "m3"));
    }

    [Fact]
    public void the_game_is_full_after_the_entrant_cap()
    {
        var w = NewWorld();
        var l = Picks(0);
        for (var i = 0; i < FfaLogic.MaxEntrants; i++)
        {
            w.Ledger.Seed($"00000000-0000-4000-8000-{i:D12}", Start);
        }

        var supply = w.Ledger.Supply;
        for (var i = 0; i < FfaLogic.MaxEntrants; i++)
        {
            Assert.NotNull(Ffa(w, l, $"00000000-0000-4000-8000-{i:D12}", "j" + i));
        }

        Assert.Equal("The FFA is full.", Ffa(w, l, Alice, "late"));
        Assert.Equal(Start, w.Ledger.Balance(Alice));
        EndWindow(w);
        Assert.Contains($"gets {FfaLogic.MaxEntrants * 100:N0} stars", Ffa(w, l, Bob, "settle"));
        Assert.Equal(supply, w.Ledger.Supply);
        Assert.Equal(Start + (FfaLogic.MaxEntrants - 1) * 100, w.Ledger.Balance("00000000-0000-4000-8000-000000000000"));
    }

    [Fact]
    public void a_player_inside_the_window_race_with_an_unregistered_opener_is_asked_to_retry()
    {
        var w = NewWorld();
        // Another event already holds the opener claim but has not written the game record yet.
        w.Kv.Set(FfaLogic.LiveKey, [1], 60);
        w.Kv.Set("ffa.liveown", System.Text.Encoding.UTF8.GetBytes("someone-else"), 60);

        Assert.Equal("The FFA is starting, try again in a moment.", Ffa(w, Picks(), Alice, "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    // ---- settlement safety -------------------------------------------------------------------------------------------

    [Fact]
    public void a_crash_mid_settlement_pays_the_winner_exactly_once_on_redelivery()
    {
        var w = NewWorld();
        var l = Picks(0);
        Ffa(w, l, Alice, "m1");
        Ffa(w, l, Bob, "m2");
        EndWindow(w);

        w.Ledger.BeforeCall = (kind, _) => { if (kind == "credit") { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => Ffa(w, l, Carol, "m3"));
        w.Ledger.BeforeCall = null;
        Assert.Equal(Start - 100, w.Ledger.Balance(Alice)); // pot not yet paid

        // A different command meets the owned settle claim and moves nothing.
        var moved = w.Ledger.Moved.Count;
        Assert.Equal("The FFA has already started, you are to late to join this one.", Ffa(w, l, Dave, "m4"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        // The platform redelivers the settling event: the winner is paid once.
        Assert.Contains($"{U(Alice)} and gets 200 stars", Ffa(w, l, Carol, "m3"));
        Assert.Equal(Start + 100, w.Ledger.Balance(Alice));
        Assert.Equal(5 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void replayed_events_move_money_once_and_reprint_the_same_reply()
    {
        var w = NewWorld();
        var l = Picks(0);
        var open = Ffa(w, l, Alice, "m1");
        var join = Ffa(w, l, Bob, "m2");
        EndWindow(w);
        var settle = Ffa(w, l, Carol, "m3");
        var moved = w.Ledger.Moved.ToList();

        Assert.Equal(open, Ffa(w, l, Alice, "m1"));
        Assert.Equal(join, Ffa(w, l, Bob, "m2"));
        Assert.Equal(settle, Ffa(w, l, Carol, "m3"));
        Assert.Equal(moved, w.Ledger.Moved);
        Assert.Equal(5 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void a_replayed_opener_never_reopens_a_settled_game()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        EndWindow(w);
        Ffa(w, l, Bob, "m2");
        Ffa(w, l, Alice, "m1");

        Assert.Null(w.Kv.Get(FfaLogic.RecKey));
    }

    [Fact]
    public void a_join_redelivered_after_its_game_was_settled_is_refunded_not_lost()
    {
        var w = NewWorld();
        var l = Picks(0);
        Ffa(w, l, Alice, "m1");
        Ffa(w, l, Bob, "m2");

        // Carol's join plans and debits, then crashes before its entrant is written.
        w.Ledger.AfterCall = (kind, _) => { if (kind == "debit") { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => Ffa(w, l, Carol, "m3"));
        w.Ledger.AfterCall = null;
        Assert.Equal(Start - 100, w.Ledger.Balance(Carol));

        // Inside the grace period a claimed-but-unwritten slot defers settlement: nothing moves.
        EndWindow(w);
        var moved = w.Ledger.Moved.Count;
        Assert.Equal("The FFA has already started, you are to late to join this one.", Ffa(w, l, Dave, "m4"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        // After the grace the game settles without Carol's slot (the pot is the two registered fees).
        w.Clock.Now += FfaLogic.InflightGraceMillis;
        Assert.Contains("gets 200 stars", Ffa(w, l, Dave, "m5"));

        // Carol's join is redelivered: the debit is de-duplicated and the fee is refunded, so she is whole.
        Assert.Contains("your 100 stars was refunded", Ffa(w, l, Carol, "m3"));
        Assert.Equal(Start, w.Ledger.Balance(Carol));
        Assert.Equal(5 * Start, w.Ledger.Supply);
    }


    // ---- gating and identity -----------------------------------------------------------------------------------------

    [Fact]
    public void the_bundle_is_silent_when_the_flag_is_off_and_ignores_other_commands()
    {
        var w = NewWorld();
        w.Flags.Overrides[FfaLogic.FlagKey] = false;
        Assert.Null(Ffa(w, Picks(), Alice, "m1"));
        w.Flags.Overrides[FfaLogic.FlagKey] = true;
        Assert.Null(Ffa(w, Picks(), Alice, "m2", "!heist 10"));
        Assert.Null(Ffa(w, Picks(), Alice, "m3", "hello"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_channel_without_a_community_and_an_unlinked_actor_are_refused()
    {
        var w = NewWorld();
        var l = Picks();
        var noCommunity = l.Run(Msg("!ffa"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("needs a community", TextOf(noCommunity!));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Ffa(w, l, Alice, "m2"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Ffa(w, l, Alice, "m3"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Unavailable, "down");
        Assert.Throws<WaddleIdentityException>(() => Ffa(w, l, Alice, "m4"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_blank_message_id_is_refused_loudly_before_any_money_moves()
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => Ffa(w, Picks(), Alice, " "));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_alias_falls_back_to_the_default_with_a_warning_when_the_service_cannot_answer()
    {
        var w = NewWorld();
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => throw new WaddleToolException(ToolErrorKind.TargetUnavailable, "down");
        w.Ledger.Seed(Alice, 5);

        Assert.Contains("costs 100 waddlebucks", Ffa(w, Picks(), Alice, "m1"));
        Assert.Contains(w.Log.Writes, x => x.Message == "ffa.alias_unavailable");

        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => System.Text.Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\"\"}");
        Assert.Contains("costs 100 waddlebucks", Ffa(w, Picks(), Alice, "m2"));
    }

    [Fact]
    public void a_single_entrant_dispatch_shape_and_unknown_plan_parts_fail_loud()
    {
        var w = NewWorld();
        var l = Picks();
        Ffa(w, l, Alice, "m1");
        // Tamper: a stored plan with an unknown money op kind must throw, never be skipped.
        var planKey = ReplayKey(w, Alice, "m1");
        var plan = System.Text.Json.JsonSerializer.Deserialize(w.Kv.Get(planKey)!, FfaJsonContext.Default.EventPlan)!;
        var bad = plan with { Own = new PlanSection([new MoneyOp("steal", Alice, 1)], []) };
        w.Kv.Set(planKey, System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(bad, FfaJsonContext.Default.EventPlan), 60);
        Assert.Throws<InvalidOperationException>(() => Ffa(w, l, Alice, "m1"));

        var badFx = plan with { Own = new PlanSection([], [new Effect("explode", "x", 0, string.Empty, null)]) };
        w.Kv.Set(planKey, System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(badFx, FfaJsonContext.Default.EventPlan), 60);
        Assert.Throws<InvalidOperationException>(() => Ffa(w, l, Alice, "m1"));

        var noGame = plan with { Own = new PlanSection([], [new Effect("open", "x", 0, string.Empty, null)]) };
        w.Kv.Set(planKey, System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(noGame, FfaJsonContext.Default.EventPlan), 60);
        Assert.Throws<InvalidOperationException>(() => Ffa(w, l, Alice, "m1"));
    }

    private static string ReplayKey(World w, string actor, string messageId) =>
        StateKey.Of("ffa.ev", StateKey.Hash($"{messageId}|twitch|{actor}|ffa"));
}

using System.Text;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Scheduling;
using WaddleSdk.State;
using WaddleSdk.Tests;
using Xunit;
using static WaddleBundleSuperpenguinDuel.Tests.World;

namespace WaddleBundleSuperpenguinDuel.Tests;

public class DuelLogicTests
{
    private const long Start = 1000;

    private static World NewWorld()
    {
        var w = new World(DuelLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave })
        {
            w.Ledger.Seed(u, Start);
        }

        return w;
    }

    /// <summary>Win rolls: 10 -> challenger wins (under 50), 90 -> defender wins.</summary>
    private static DuelLogic Rolls(params int[] values) => new(new ScriptedRandom(values).Next);

    private static string U(string uuid) => $"{{user:{uuid}}}";

    private static string? Challenge(World w, DuelLogic l, string from, string to, int stake, string id) => w.Send(l, from, $"!duel {to} {stake}", id);

    // ---- happy paths -------------------------------------------------------------------------------------------------

    [Fact]
    public void a_challenge_debits_only_the_challenger_and_announces_the_stake()
    {
        var w = NewWorld();
        var text = Challenge(w, Rolls(), Alice, Bob, 50, "m1");

        Assert.Equal(Start - 50, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
        Assert.Contains($"{U(Bob)}, {U(Alice)} has challenged you to a duel for 50 stars", text);
        Assert.Contains("2 minutes to !accept or !deny", text);
        Assert.Equal([("debit", Alice, 50UL)], w.Ledger.Moved);
    }

    [Fact]
    public void accepting_debits_the_opponent_on_their_own_command_and_pays_the_winner_twice_the_stake()
    {
        var w = NewWorld();
        var l = Rolls(10);
        Challenge(w, l, Alice, Bob, 50, "m1");
        var text = w.Send(l, Bob, "!accept", "m2");

        Assert.Contains($"{U(Alice)} won the Duel vs {U(Bob)}", text);
        Assert.Equal(Start + 50, w.Ledger.Balance(Alice));
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));
        Assert.Equal(4 * Start, w.Ledger.Supply);     // burn + mint of the same amount: supply neutral
        Assert.Equal(
            [("debit", Alice, 50UL), ("debit", Bob, 50UL), ("credit", Alice, 100UL)],
            w.Ledger.Moved);
    }

    [Fact]
    public void the_defender_wins_when_the_roll_is_at_or_over_the_win_percent()
    {
        var w = NewWorld();
        var l = Rolls(50);
        Challenge(w, l, Alice, Bob, 30, "m1");
        var text = w.Send(l, Bob, "!accept", "m2");

        Assert.Contains($"{U(Bob)} won the Duel vs {U(Alice)}", text);
        Assert.Equal(Start - 30, w.Ledger.Balance(Alice));
        Assert.Equal(Start + 30, w.Ledger.Balance(Bob));
    }

    [Fact]
    public void denying_refunds_the_challengers_stake_and_closes_the_duel()
    {
        var w = NewWorld();
        var l = Rolls();
        Challenge(w, l, Alice, Bob, 50, "m1");
        var text = w.Send(l, Bob, "!deny", "m2");

        Assert.Contains("has denied your duel", text);
        Assert.Equal(Start, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
        Assert.Equal("You don't have any pending duels targeting you.", w.Send(l, Bob, "!accept", "m3"));
        Assert.Equal(Start, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void only_the_target_can_accept_or_deny()
    {
        var w = NewWorld();
        var l = Rolls();
        Challenge(w, l, Alice, Bob, 50, "m1");

        Assert.Equal("You don't have any pending duels targeting you.", w.Send(l, Alice, "!deny", "m2"));
        Assert.Equal("You don't have any pending duels targeting you.", w.Send(l, Carol, "!accept", "m3"));
        Assert.Single(w.Ledger.Moved);
    }

    // ---- money safety ------------------------------------------------------------------------------------------------

    [Fact]
    public void an_expired_duel_refunds_the_challenger_on_the_next_command_and_cannot_be_accepted()
    {
        var w = NewWorld();
        var l = Rolls();
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Clock.Now += DuelLogic.TimeoutMillis + 1_000;

        var text = w.Send(l, Bob, "!accept", "m2");

        Assert.Contains("That duel challenge has expired.", text);
        Assert.Contains("expired and the stake was refunded", text);
        Assert.Equal(Start, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
        Assert.Equal(4 * Start, w.Ledger.Supply);
        Assert.Equal("You don't have any pending duels targeting you.", w.Send(l, Bob, "!accept", "m3"));
    }

    [Fact]
    public void an_expired_duel_is_also_refunded_by_an_unrelated_players_command()
    {
        var w = NewWorld();
        var l = Rolls();
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Clock.Now += DuelLogic.TimeoutMillis + 1_000;

        var text = Challenge(w, l, Carol, Dave, 20, "m2");

        Assert.Contains("expired and the stake was refunded", text);
        Assert.Contains("has challenged you to a duel for 20", text);
        Assert.Equal(Start, w.Ledger.Balance(Alice));       // refunded
        Assert.Equal(Start - 20, w.Ledger.Balance(Carol));  // new stake
        Assert.Equal(Start, w.Ledger.Balance(Bob));
    }

    [Fact]
    public void denying_after_the_deadline_is_refused_but_still_refunds_once()
    {
        var w = NewWorld();
        var l = Rolls();
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Clock.Now += DuelLogic.TimeoutMillis + 1_000;

        var text = w.Send(l, Bob, "!deny", "m2");

        Assert.Contains("That duel challenge has expired.", text);
        Assert.Equal(Start, w.Ledger.Balance(Alice));
        Assert.Equal(2, w.Ledger.Moved.Count);              // the one debit and the one refund
    }

    [Fact]
    public void a_second_accept_after_the_duel_closed_moves_nothing()
    {
        var w = NewWorld();
        var l = Rolls(10, 10);
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Send(l, Bob, "!accept", "m2");
        var moved = w.Ledger.Moved.Count;

        Assert.Equal("You don't have any pending duels targeting you.", w.Send(l, Bob, "!accept", "m3"));
        Assert.Equal(moved, w.Ledger.Moved.Count);
    }

    [Fact]
    public void a_crash_mid_settlement_cannot_be_resolved_twice_and_the_redelivery_completes_it_once()
    {
        var w = NewWorld();
        var l = Rolls(10);
        Challenge(w, l, Alice, Bob, 50, "m1");

        // The accept event debits Bob, then crashes before the winner's credit.
        w.Ledger.BeforeCall = (kind, _) => { if (kind == "credit") { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => w.Send(l, Bob, "!accept", "m2"));
        w.Ledger.BeforeCall = null;
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));    // debited, winner not yet paid

        // A second, different accept event meets the owned claim and moves nothing.
        var moved = w.Ledger.Moved.Count;
        Assert.Equal("That duel was already resolved.", w.Send(l, Bob, "!accept", "m3"));
        Assert.Equal("That duel was already resolved.", w.Send(l, Bob, "!deny", "m4"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        // The platform redelivers the first accept: the debit is de-duplicated by the host, the credit lands once.
        var text = w.Send(l, Bob, "!accept", "m2");
        Assert.Contains($"{U(Alice)} won the Duel vs {U(Bob)}", text);
        Assert.Equal(Start + 50, w.Ledger.Balance(Alice));
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));
        Assert.Equal(4 * Start, w.Ledger.Supply);
        Assert.Equal(
            [("debit", Alice, 50UL), ("debit", Bob, 50UL), ("credit", Alice, 100UL)],
            w.Ledger.Moved);
    }

    [Fact]
    public void replayed_events_move_money_once_and_reprint_the_same_reply()
    {
        var w = NewWorld();
        var l = Rolls(10, 10);
        var first = Challenge(w, l, Alice, Bob, 50, "m1");
        var accept = w.Send(l, Bob, "!accept", "m2");
        var moved = w.Ledger.Moved.ToList();
        var supply = w.Ledger.Supply;

        Assert.Equal(first, Challenge(w, l, Alice, Bob, 50, "m1"));
        Assert.Equal(accept, w.Send(l, Bob, "!accept", "m2"));
        Assert.Equal(first, Challenge(w, l, Alice, Bob, 50, "m1"));
        // A replayed challenge must NOT re-open the duel that was already settled.
        Assert.Equal("You don't have any pending duels targeting you.", w.Send(l, Bob, "!accept", "m9"));

        Assert.Equal(moved, w.Ledger.Moved);
        Assert.Equal(supply, w.Ledger.Supply);
    }

    [Fact]
    public void a_replayed_deny_and_a_replayed_expiry_refund_once()
    {
        var w = NewWorld();
        var l = Rolls();
        Challenge(w, l, Alice, Bob, 50, "m1");
        var deny = w.Send(l, Bob, "!deny", "m2");
        Assert.Equal(deny, w.Send(l, Bob, "!deny", "m2"));
        Assert.Equal(Start, w.Ledger.Balance(Alice));

        Challenge(w, l, Carol, Dave, 40, "m3");
        w.Clock.Now += DuelLogic.TimeoutMillis + 1_000;
        var expiry = w.Send(l, Bob, "!accept", "m4");
        Assert.Equal(expiry, w.Send(l, Bob, "!accept", "m4"));
        Assert.Equal(Start, w.Ledger.Balance(Carol));
        Assert.Equal(4 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void concurrent_duels_between_disjoint_players_are_independent_and_overlapping_ones_are_refused()
    {
        var w = NewWorld();
        var l = Rolls(10, 90);
        const string eve = "55555555-5555-4555-8555-555555555555";
        w.Ledger.Seed(eve, Start);
        Challenge(w, l, Alice, Bob, 50, "m1");
        Challenge(w, l, Carol, Dave, 20, "m2");

        // Alice is inside her own cooldown; a player already in a duel cannot start another; nobody can challenge a busy player.
        Assert.Null(Challenge(w, l, Alice, eve, 10, "m3"));
        Assert.Contains("already in a duel", Challenge(w, l, Dave, eve, 10, "m4"));
        Assert.Contains("already in a duel", Challenge(w, l, Bob, eve, 10, "m5"));
        Assert.Contains("has a pending duel already", Challenge(w, l, eve, Bob, 10, "m6"));
        Assert.Equal(Start - 50, w.Ledger.Balance(Alice));
        Assert.Equal(Start, w.Ledger.Balance(eve));

        Assert.Contains($"{U(Alice)} won", w.Send(l, Bob, "!accept", "m7"));
        Assert.Contains($"{U(Dave)} won", w.Send(l, Dave, "!accept", "m8"));
        Assert.Equal(Start + 50, w.Ledger.Balance(Alice));
        Assert.Equal(Start - 50, w.Ledger.Balance(Bob));
        Assert.Equal(Start - 20, w.Ledger.Balance(Carol));
        Assert.Equal(Start + 20, w.Ledger.Balance(Dave));
        Assert.Equal(5 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void a_debit_refused_after_the_precheck_aborts_cleanly_and_releases_the_locks_and_cooldown()
    {
        var w = NewWorld();
        var l = Rolls(10);
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
            {
                throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "raced", 0);
            }
        };

        var text = Challenge(w, l, Alice, Bob, 50, "m1");
        Assert.Contains("you don't have 50 stars to duel with", text);
        Assert.Empty(w.Ledger.Moved);
        Assert.Equal(text, Challenge(w, l, Alice, Bob, 50, "m1")); // the aborted plan replays as the same refusal

        w.Ledger.BeforeCall = null;
        Assert.Contains("has challenged you", Challenge(w, l, Alice, Bob, 50, "m2")); // locks + cooldown were released
    }

    [Fact]
    public void an_accept_whose_debit_is_refused_releases_the_claim_so_the_duel_stays_open()
    {
        var w = NewWorld();
        // Both accept events select an outcome; refusal consumes the first draw.
        var l = Rolls(10, 10);
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 40);
            }
        };

        Assert.Contains("you don't have 50 stars to accept", w.Send(l, Bob, "!accept", "m2"));
        Assert.Equal(Start, w.Ledger.Balance(Bob));

        w.Ledger.BeforeCall = null;
        Assert.Contains($"{U(Alice)} won", w.Send(l, Bob, "!accept", "m3"));
    }

    [Fact]
    public void cleanup_failure_never_resurrects_an_aborted_money_plan()
    {
        var w = NewWorld();
        var l = Rolls(10, 10);
        Challenge(w, l, Alice, Bob, 50, "abort-challenge");
        w.Ledger.BeforeCall = (kind, _) =>
        {
            if (kind == "debit")
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 40);
        };
        w.Kv.BeforeDelete = _ => throw new InvalidOperationException("simulated cleanup crash");
        Assert.Throws<InvalidOperationException>(() => w.Send(l, Bob, "!accept", "abort-accept"));
        w.Kv.BeforeDelete = null;
        w.Ledger.BeforeCall = null;
        var movedBefore = w.Ledger.Moved.Count;
        Assert.Contains("you don't have 50 stars to accept", w.Send(l, Bob, "!accept", "abort-accept"));
        Assert.Equal(Start, w.Ledger.Balance(Bob));
        Assert.Equal(movedBefore, w.Ledger.Moved.Count);
    }

    [Fact]
    public void an_over_cap_winner_credit_fails_loudly_and_the_redelivery_pays_once_after_the_cap_is_raised()
    {
        var w = NewWorld();
        var l = Rolls(10);
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Ledger.MintCap = 50;

        var ex = Assert.Throws<WaddleEconomyException>(() => w.Send(l, Bob, "!accept", "m2"));
        Assert.Equal(EconomyErrorKind.OverCap, ex.Kind);

        w.Ledger.MintCap = ulong.MaxValue;
        Assert.Contains($"{U(Alice)} won", w.Send(l, Bob, "!accept", "m2"));
        Assert.Equal(Start + 50, w.Ledger.Balance(Alice));
        Assert.Equal(4 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void a_denied_economy_call_propagates_and_never_becomes_a_reply()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Denied, "not_granted");

        Assert.Throws<WaddleEconomyException>(() => Challenge(w, Rolls(), Alice, Bob, 50, "m1"));
    }

    // ---- replay-key rules (coordinator security review) --------------------------------------------------------------

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void a_blank_message_id_is_refused_loudly(string id)
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => Challenge(w, Rolls(), Alice, Bob, 50, id));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_same_message_id_from_a_different_actor_is_not_a_replay()
    {
        var w = NewWorld();
        var l = Rolls();
        var alice = Challenge(w, l, Alice, Bob, 50, "same-id");
        var carol = Challenge(w, l, Carol, Dave, 20, "same-id");

        Assert.NotEqual(alice, carol);
        Assert.Contains($"{U(Dave)}, {U(Carol)} has challenged you to a duel for 20", carol);
        Assert.Equal(Start - 20, w.Ledger.Balance(Carol));
    }

    [Fact]
    public void a_plan_recorded_for_another_actor_or_command_is_never_replayed()
    {
        var w = NewWorld();
        var l = Rolls();
        var ev = Msg($"!duel {Bob} 50");
        var key = ReplayGuard.KeyFor("duel.ev", ev, w.HostFor(Alice, "m1"), Alice, "duel");
        var stolen = new EventPlan(Carol, "duel", StateKey.Hash($"!duel {Bob} 50"), new PlanSection([], []), null, [], "STOLEN", "STOLEN");
        new KvStateStore(w.Kv).SetJson(key, stolen, DuelJsonContext.Default.EventPlan, 60);

        var text = Challenge(w, l, Alice, Bob, 50, "m1");
        Assert.DoesNotContain("STOLEN", text);
        Assert.Equal(Start - 50, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_plan_with_an_unknown_op_or_effect_fails_loudly_on_replay()
    {
        var w = NewWorld();
        var store = new KvStateStore(w.Kv);
        var ev = Msg($"!deny");
        var key = ReplayGuard.KeyFor("duel.ev", ev, w.HostFor(Bob, "p1"), Bob, "deny");
        var args = StateKey.Hash("!deny");

        var badOp = new EventPlan(Bob, "deny", args, new PlanSection([new MoneyOp("mint-everything", Bob, 1)], []), null, [], "x", "x");
        store.SetJson(key, badOp, DuelJsonContext.Default.EventPlan, 60);
        Assert.Throws<InvalidOperationException>(() => w.Send(Rolls(), Bob, "!deny", "p1"));

        var duel = new DuelRecord("d", Alice, Bob, 1, 50, 1);
        foreach (var effect in new[] { new DuelEffect("explode", "d", duel), new DuelEffect("open", "d", null) })
        {
            var badFx = new EventPlan(Bob, "deny", args, new PlanSection([], [effect]), null, [], "x", "x");
            store.SetJson(key, badFx, DuelJsonContext.Default.EventPlan, 60);
            Assert.Throws<InvalidOperationException>(() => w.Send(Rolls(), Bob, "!deny", "p1"));
        }
    }

    // ---- validation and gating ---------------------------------------------------------------------------------------

    [Fact]
    public void bad_input_gets_a_usage_reply_and_moves_nothing_and_keeps_the_cooldown_free()
    {
        var w = NewWorld();
        var l = Rolls();

        Assert.Equal("To use duel, !duel target amount", w.Send(l, Alice, "!duel", "m1"));
        Assert.Equal("To use duel, !duel target amount", w.Send(l, Alice, $"!duel {Bob}", "m2"));
        Assert.Equal("Please enter a proper amount. !duel target amount", w.Send(l, Alice, $"!duel {Bob} lots", "m3"));
        Assert.Equal("Please enter a proper amount. !duel target amount", w.Send(l, Alice, $"!duel {Bob} -5", "m4"));
        Assert.Equal("To duel you must choose an amount between 1 and 100", w.Send(l, Alice, $"!duel {Bob} 0", "m5"));
        Assert.Equal("To duel you must choose an amount between 1 and 100", w.Send(l, Alice, $"!duel {Bob} 101", "m6"));
        Assert.Equal("You can't duel yourself.", w.Send(l, Alice, $"!duel {Alice} 5", "m7"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Contains("has challenged you", w.Send(l, Alice, $"!duel {Bob} 5", "m9"));
    }

    [Fact]
    public void a_challenger_or_target_who_cannot_cover_the_stake_is_refused_without_a_debit()
    {
        var w = NewWorld();
        var l = Rolls();
        w.Ledger.Seed(Alice, 10);
        Assert.Equal("You don't have that much.", w.Send(l, Alice, $"!duel {Bob} 50", "m1"));

        w.Ledger.Seed(Alice, 500);
        w.Ledger.Seed(Bob, 10);
        Assert.Equal("They don't have that many stars.", w.Send(l, Alice, $"!duel {Bob} 50", "m2"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void an_accept_the_target_cannot_cover_is_refused_and_the_duel_stays_open()
    {
        var w = NewWorld();
        var l = Rolls(10);
        Challenge(w, l, Alice, Bob, 50, "m1");
        w.Ledger.Seed(Bob, 10);

        Assert.Contains("you don't have 50 stars to accept this duel", w.Send(l, Bob, "!accept", "m2"));
        w.Ledger.Seed(Bob, 100);
        Assert.Contains($"{U(Alice)} won", w.Send(l, Bob, "!accept", "m3"));
    }

    [Theory]
    [InlineData(IdentityErrorKind.NotFound)]
    [InlineData(IdentityErrorKind.Ambiguous)]
    [InlineData(IdentityErrorKind.NotAMember)]
    public void an_unresolvable_target_is_refused(IdentityErrorKind kind)
    {
        var w = NewWorld();
        w.MentionFailure = new WaddleIdentityException(kind, "nope");
        Assert.Equal("Could not find that viewer", Challenge(w, Rolls(), Alice, "ghost", 5, "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_challenger_cooldown_silently_drops_a_second_challenge()
    {
        var w = NewWorld();
        var l = Rolls(10);
        Challenge(w, l, Alice, Bob, 5, "m1");
        w.Send(l, Bob, "!accept", "m2");

        Assert.Null(Challenge(w, l, Alice, Carol, 5, "m3"));
        Assert.Equal(3, w.Ledger.Moved.Count);
        Assert.Null(Challenge(w, l, Alice, Carol, 5, "m3"));       // a silent drop stays silent on redelivery too
    }

    [Fact]
    public void the_flag_gates_the_bundle_and_nothing_else_runs_when_it_is_off()
    {
        var w = NewWorld();
        w.Flags.Overrides[DuelLogic.FlagKey] = false;
        Assert.Null(Challenge(w, Rolls(), Alice, Bob, 5, "m1"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Empty(w.Kv.SetCalls);
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!duelx")]
    [InlineData("!")]
    public void unrelated_chat_is_ignored(string text)
    {
        var w = NewWorld();
        Assert.Null(w.Send(Rolls(), Alice, text, "m1"));
    }

    [Fact]
    public void a_channel_without_a_community_and_an_unlinked_actor_get_a_reply()
    {
        var w = NewWorld();
        var noCommunity = Rolls().Run(Msg("!duel x 5"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("not linked to one", TextOf(noCommunity!));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "no");
        Assert.Contains("Link your account", Challenge(w, Rolls(), Alice, Bob, 5, "m2"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "no");
        Assert.Contains("Link your account", Challenge(w, Rolls(), Alice, Bob, 5, "m3"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void an_actor_resolution_failure_other_than_not_linked_propagates()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");
        Assert.Throws<WaddleIdentityException>(() => Challenge(w, Rolls(), Alice, Bob, 5, "m1"));
    }

    [Fact]
    public void the_currency_alias_falls_back_to_the_documented_default_when_the_tool_cannot_answer()
    {
        var w = NewWorld();
        w.Tools.Handlers.Remove((EconomyServiceTools.Target, "points_view"));
        Assert.Contains("for 5 waddlebucks", Challenge(w, Rolls(), Alice, Bob, 5, "m1"));
        Assert.Contains(w.Log.Writes, x => x.Message == "duel.alias_unavailable");
    }

    [Fact]
    public void a_blank_alias_also_falls_back_to_the_default()
    {
        var w = NewWorld();
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\" \"}");
        Assert.Contains("for 5 waddlebucks", Challenge(w, Rolls(), Alice, Bob, 5, "m1"));
    }

    // ---- scheduler hygiene -------------------------------------------------------------------------------------------

    [Fact]
    public void a_due_task_without_a_duel_record_is_dropped_by_the_sweep()
    {
        var w = NewWorld();
        var sched = new TickScheduler(new KvStateStore(w.Kv), w.Clock, "duel");
        sched.Schedule(new ScheduledTask("ghost", w.Clock.Now - 1, "expire", string.Empty));

        Challenge(w, Rolls(), Alice, Bob, 5, "m1");

        Assert.DoesNotContain(sched.Pending(), t => t.Id == "ghost");
    }

    [Fact]
    public void a_full_scheduler_board_refuses_new_challenges_before_any_money_moves()
    {
        var w = NewWorld();
        var sched = new TickScheduler(new KvStateStore(w.Kv), w.Clock, "duel");
        for (var i = 0; i < TickScheduler.MaxTasks - DuelLogic.MaxSweepPerEvent; i++)
        {
            sched.Schedule(new ScheduledTask($"t{i}", w.Clock.Now + 10_000_000, "expire", string.Empty));
        }

        Assert.Contains("Too many duels", Challenge(w, Rolls(), Alice, Bob, 5, "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_sweep_is_bounded_per_event_and_finishes_over_the_following_events()
    {
        var w = NewWorld();
        var l = Rolls();
        var users = Enumerable.Range(0, 12).Select(i => $"{i:x8}-0000-4000-8000-000000000000").ToList();
        foreach (var u in users)
        {
            w.Ledger.Seed(u, 100);
        }

        for (var i = 0; i < 12; i += 2)
        {
            Challenge(w, l, users[i], users[i + 1], 10, $"c{i}");
        }

        w.Clock.Now += DuelLogic.TimeoutMillis + 1_000;
        var first = w.Send(l, Alice, "!accept", "s1");
        Assert.Equal(DuelLogic.MaxSweepPerEvent, first!.Split("expired and the stake was refunded").Length - 1);
        w.Send(l, Alice, "!accept", "s2");
        for (var i = 0; i < 12; i += 2)
        {
            Assert.Equal(100, w.Ledger.Balance(users[i]));
        }
    }
}

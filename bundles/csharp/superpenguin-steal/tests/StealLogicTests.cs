using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.State;
using WaddleSdk.Tool;
using Xunit;
using static WaddleBundleSuperpenguinSteal.Tests.World;

namespace WaddleBundleSuperpenguinSteal.Tests;

public class StealLogicTests
{
    private const long Start = 50_000;
    private const string Eve = "55555555-5555-4555-8555-555555555555";

    private static World NewWorld(bool transferGranted = true)
    {
        var w = new World(StealLogic.FlagKey, transferGranted: transferGranted);
        foreach (var u in new[] { Alice, Bob, Carol, Dave, Eve })
        {
            w.Ledger.Seed(u, Start);
        }

        return w;
    }

    /// <summary>Scripted rolls: first the amount, then (for a non-poor victim) the success roll in 0..9999 (under 3333 succeeds).</summary>
    private static StealLogic Rolls(params int[] values) => new(new ScriptedRandom(values).Next);

    private static string U(string uuid) => $"{{user:{uuid}}}";

    private static string? Steal(World w, StealLogic l, string thief, string victim, string id) => w.Send(l, thief, $"!steal {victim}", id);

    private static void ClearCooldown(World w, string actor) => w.Kv.Delete(StateKey.Of("cd", "steal", StateKey.Hash(actor)));

    private static void ClearImmunity(World w, string victim) => w.Kv.Delete(StealLogic.ImmunityKey(victim));

    // ---- outcomes ----------------------------------------------------------------------------------------------------

    [Fact]
    public void a_successful_steal_moves_the_amount_from_the_victim_to_the_thief_in_one_transfer()
    {
        var w = NewWorld();
        var text = Steal(w, Rolls(500, 10), Alice, Bob, "m1");

        Assert.Equal($"{U(Alice)} successfully stole 500 stars from {U(Bob)}", text);
        Assert.Equal(Start + 500, w.Ledger.Balance(Alice));
        Assert.Equal(Start - 500, w.Ledger.Balance(Bob));
        Assert.Equal([("transfer", Bob + ">" + Alice, 500UL)], w.Ledger.Moved);
        Assert.Equal(5 * Start, w.Ledger.Supply);
    }

    [Fact]
    public void the_transfer_follows_the_economy_contract_fixture_exactly()
    {
        var w = NewWorld();
        Steal(w, Rolls(500, 10), Alice, Bob, "m1");

        var call = Assert.Single(w.Tools.Calls, c => c.Tool == "transfer_from");
        Assert.Equal(EconomyServiceTools.Target, call.Target);
        Assert.Equal($"{{\"from\":\"{Bob}\",\"to\":\"{Alice}\",\"amount\":500,\"reason\":\"steal.success\"}}", call.Args);
        Assert.False(string.IsNullOrEmpty(call.Key)); // the idempotency key travels in the invoke key slot
    }

    [Fact]
    public void a_failed_steal_makes_the_thief_pay_the_victim()
    {
        var w = NewWorld();
        var text = Steal(w, Rolls(700, 5000), Alice, Bob, "m1");

        Assert.Equal($"{U(Alice)} failed to steal 700 stars from {U(Bob)}, {U(Bob)} gets 700 stars from {U(Alice)} instead", text);
        Assert.Equal(Start - 700, w.Ledger.Balance(Alice));
        Assert.Equal(Start + 700, w.Ledger.Balance(Bob));
        Assert.Equal("steal.failed", System.Text.Json.JsonDocument.Parse(w.Tools.Calls.Single(c => c.Tool == "transfer_from").Args).RootElement.GetProperty("reason").GetString());
    }

    [Theory]
    [InlineData(3332, true)]
    [InlineData(3333, false)]
    public void the_success_chance_is_33_33_percent(int roll, bool success)
    {
        var w = NewWorld();
        var text = Steal(w, Rolls(100, roll), Alice, Bob, "m1");

        Assert.Equal(success, text!.Contains("successfully stole"));
    }

    [Fact]
    public void the_amount_is_clamped_to_the_100_to_10000_range()
    {
        var w = NewWorld();
        Steal(w, Rolls(1, 10), Alice, Bob, "m1");
        Assert.Equal(Start + 100, w.Ledger.Balance(Alice));
        ClearCooldown(w, Alice);
        ClearImmunity(w, Carol);
        Steal(w, Rolls(99_999, 10), Alice, Carol, "m2");
        Assert.Equal(Start + 100 + 10_000, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_poor_target_is_protected_and_the_thief_gives_them_the_amount_instead()
    {
        var w = NewWorld();
        w.Ledger.Seed(Bob, 9_999);
        var text = Steal(w, Rolls(1_234), Alice, Bob, "m1"); // only the amount is rolled: there is no chance to win

        Assert.Equal($"{U(Bob)} is too poor for you to steal from them, instead you give them 1,234 stars.", text);
        Assert.Equal(Start - 1_234, w.Ledger.Balance(Alice));
        Assert.Equal(9_999 + 1_234, w.Ledger.Balance(Bob));
    }

    // ---- refusals ----------------------------------------------------------------------------------------------------

    [Fact]
    public void a_thief_below_the_maximum_cannot_steal_and_keeps_their_cooldown_free()
    {
        var w = NewWorld();
        w.Ledger.Seed(Alice, 9_999);

        Assert.Equal($"{U(Alice)}, you don't have enough stars to steal, you need a minimum of 10000", Steal(w, Rolls(), Alice, Bob, "m1"));
        w.Ledger.Seed(Alice, Start);
        Assert.Contains("successfully", Steal(w, Rolls(100, 1), Alice, Bob, "m2"));
        Assert.Single(w.Ledger.Moved);
    }

    [Fact]
    public void usage_is_shown_without_a_target_or_when_targeting_yourself()
    {
        var w = NewWorld();
        Assert.Equal(StealLogic.Usage, w.Send(Rolls(), Alice, "!steal", "m1"));
        Assert.Equal(StealLogic.Usage, Steal(w, Rolls(), Alice, Alice, "m2"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void an_unknown_target_is_refused_and_other_identity_failures_propagate()
    {
        var w = NewWorld();
        w.MentionFailure = new WaddleIdentityException(IdentityErrorKind.NotFound, "nf");
        Assert.Equal("Could not find that viewer", Steal(w, Rolls(), Alice, "nobody", "m1"));
        w.MentionFailure = new WaddleIdentityException(IdentityErrorKind.Unavailable, "down");
        Assert.Throws<WaddleIdentityException>(() => Steal(w, Rolls(), Alice, "nobody", "m2"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void opted_out_players_cannot_be_stolen_from_and_cannot_steal_until_they_opt_back_in()
    {
        var w = NewWorld();
        var l = Rolls(100, 1, 100, 1);
        Assert.Contains("opted out of stealing", w.Send(l, Bob, "!steal optout", "m1"));

        Assert.Equal($"{U(Bob)} cannot be stolen from.", Steal(w, l, Alice, Bob, "m2"));
        Assert.Contains("you opted out of stealing", Steal(w, l, Bob, Alice, "m3"));
        Assert.Empty(w.Ledger.Moved);

        Assert.Contains("opted back in", w.Send(l, Bob, "!steal OPTIN", "m4"));
        Assert.Contains("successfully", Steal(w, l, Alice, Bob, "m5"));
    }

    [Fact]
    public void protected_users_from_the_app_config_are_never_robbed_and_a_bad_config_is_ignored_with_a_warning()
    {
        var w = NewWorld();
        w.ConfigJson = $"{{\"protected_users\":[\"{Bob}\"]}}";
        Assert.Equal($"{U(Bob)} cannot be stolen from.", Steal(w, Rolls(100, 1), Alice, Bob, "m1"));
        Assert.Empty(w.Ledger.Moved);

        w.ConfigJson = "not json";
        Assert.Contains("successfully", Steal(w, Rolls(100, 1), Alice, Bob, "m2"));
        Assert.Contains(w.Log.Writes, x => x.Message == "steal.config_invalid");
    }

    [Fact]
    public void the_per_thief_cooldown_drops_a_second_steal_silently()
    {
        var w = NewWorld();
        var l = Rolls(100, 1, 100, 1);
        Steal(w, l, Alice, Bob, "m1");
        var moved = w.Ledger.Moved.Count;

        Assert.Null(Steal(w, l, Alice, Carol, "m2"));
        Assert.Equal(moved, w.Ledger.Moved.Count);

        ClearCooldown(w, Alice);
        Assert.Contains("successfully", Steal(w, l, Alice, Carol, "m3"));
    }

    [Fact]
    public void a_victim_is_immune_for_a_while_after_being_robbed()
    {
        var w = NewWorld();
        var l = Rolls(100, 1, 100, 1);
        Steal(w, l, Alice, Bob, "m1");

        Assert.Equal($"{U(Bob)} has been robbed enough for now.", Steal(w, l, Carol, Bob, "m2"));
        ClearImmunity(w, Bob);
        Assert.Contains("successfully", Steal(w, l, Carol, Bob, "m3"));
    }

    // ---- daily caps --------------------------------------------------------------------------------------------------

    [Fact]
    public void a_victim_cannot_lose_more_than_the_daily_limit_and_the_last_theft_is_capped()
    {
        var w = NewWorld();
        var l = Rolls(9_950, 1, 500, 1);
        Steal(w, l, Alice, Bob, "m1");
        ClearCooldown(w, Carol);
        ClearImmunity(w, Bob);

        var text = Steal(w, l, Carol, Bob, "m2"); // only 50 of the 10,000 daily allowance is left

        Assert.Contains("stole 50 stars", text);
        Assert.Equal(Start - 10_000, w.Ledger.Balance(Bob));
        ClearCooldown(w, Dave);
        ClearImmunity(w, Bob);
        Assert.Equal($"{U(Bob)} has been robbed enough for now.", Steal(w, Rolls(100, 1), Dave, Bob, "m3"));
    }

    [Fact]
    public void a_thief_cannot_put_more_than_the_daily_limit_at_risk_and_the_last_attempt_is_capped()
    {
        var w = NewWorld();
        var l = Rolls(10_000, 1, 10_000, 1, 9_900, 1, 5_000, 5_000);
        foreach (var (victim, id) in new[] { (Bob, "m1"), (Carol, "m2"), (Dave, "m3") })
        {
            ClearCooldown(w, Alice);
            Assert.Contains("successfully", Steal(w, l, Alice, victim, id));
        }

        ClearCooldown(w, Alice);
        var text = Steal(w, l, Alice, Eve, "m4"); // 29,900 used: a failed 5,000 attempt is capped to the 100 left
        Assert.Contains("failed to steal 100 stars", text);
        Assert.Equal(Start + 29_900 - 100, w.Ledger.Balance(Alice));

        ClearCooldown(w, Alice);
        Assert.Equal($"{U(Alice)}, you reached your daily stealing limit; try again tomorrow.", Steal(w, Rolls(), Alice, Eve, "m5"));
    }

    [Fact]
    public void the_daily_counters_reset_on_the_next_utc_day()
    {
        var w = NewWorld();
        var l = Rolls(10_000, 1, 100, 1);
        Steal(w, l, Alice, Bob, "m1");
        ClearImmunity(w, Bob);
        ClearCooldown(w, Carol);
        Assert.Contains("robbed enough", Steal(w, l, Carol, Bob, "m2"));

        w.Clock.Now += StealLogic.DayMillis;
        Assert.Contains("successfully", Steal(w, l, Carol, Bob, "m3"));
    }

    // ---- fail loud ---------------------------------------------------------------------------------------------------

    [Fact]
    public void without_the_dangerous_transfer_grant_every_steal_path_says_steal_not_enabled_and_moves_nothing()
    {
        var w = NewWorld(transferGranted: false);
        var l = Rolls(100, 1, 100, 5000, 100);
        w.Ledger.Seed(Eve, 100); // a poor target would take the gift path

        Assert.Equal("steal not enabled", Steal(w, l, Alice, Bob, "m1"));    // success path
        Assert.Equal("steal not enabled", Steal(w, l, Alice, Carol, "m2"));  // failure path: the cooldown was released, so it is not silent
        Assert.Equal("steal not enabled", Steal(w, l, Alice, Eve, "m3"));    // too-poor path
        Assert.Empty(w.Ledger.Moved);
        Assert.Equal(4 * Start + 100, w.Ledger.Supply);
        Assert.Contains(w.Log.Writes, x => x.Message == "steal.not_enabled");
    }

    [Fact]
    public void a_transfer_refused_after_the_prechecks_replies_with_a_refusal_and_releases_the_cooldown()
    {
        var w = NewWorld();
        var l = Rolls(100, 1, 100, 1);
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "raced", 0);

        Assert.Contains("could not be paid", Steal(w, l, Alice, Bob, "m1"));
        Assert.Empty(w.Ledger.Moved);

        w.Ledger.BeforeCall = null;
        Assert.Contains("successfully", Steal(w, l, Alice, Bob, "m2")); // cooldown was released, the aborted record does not replay
    }

    [Theory]
    [InlineData(EconomyErrorKind.OverCap, "over the economy's limits")]
    [InlineData(EconomyErrorKind.NotAMember, "must be members")]
    public void over_cap_and_non_member_refusals_are_typed_replies(EconomyErrorKind kind, string expected)
    {
        var w = NewWorld();
        w.Tools.Handlers[(EconomyServiceTools.Target, "transfer_from")] = _ =>
            throw new WaddleToolException(ToolErrorKind.InvalidArgs, kind == EconomyErrorKind.OverCap ? "over_cap" : "not_a_member");

        Assert.Contains(expected, Steal(w, Rolls(100, 1), Alice, Bob, "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void an_unknown_backend_failure_propagates_so_the_redelivery_retries()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Backend, "db down");

        Assert.Throws<WaddleToolException>(() => Steal(w, Rolls(100, 1), Alice, Bob, "m1"));
        Assert.Empty(w.Ledger.Moved);
    }

    // ---- exactly-once ------------------------------------------------------------------------------------------------

    [Fact]
    public void a_replayed_steal_moves_money_once_reprints_the_same_reply_and_counts_the_day_once()
    {
        var w = NewWorld();
        var l = Rolls(2_000, 1);
        var first = Steal(w, l, Alice, Bob, "m1");
        var again = Steal(w, l, Alice, Bob, "m1"); // the scripted rolls are exhausted: only the recorded outcome can answer

        Assert.Equal(first, again);
        Assert.Single(w.Ledger.Moved);
        Assert.Equal(5 * Start, w.Ledger.Supply);
        var counter = System.Text.Encoding.UTF8.GetString(w.Kv.Get(StealLogic.ThiefDayKey(w.Clock.Now / StealLogic.DayMillis, Alice))!);
        Assert.Equal("2000", counter);
    }

    [Fact]
    public void a_crash_after_the_transfer_is_completed_once_by_the_redelivery()
    {
        var w = NewWorld();
        var l = Rolls(2_000, 1);
        w.Ledger.AfterCall = (kind, _) => { if (kind == "transfer") { throw new InvalidOperationException("crash"); } };
        Assert.Throws<InvalidOperationException>(() => Steal(w, l, Alice, Bob, "m1"));
        w.Ledger.AfterCall = null;
        Assert.Equal(Start + 2_000, w.Ledger.Balance(Alice)); // money moved, reply and counters not yet committed

        var text = Steal(w, l, Alice, Bob, "m1");
        Assert.Contains("successfully stole 2000", text);
        Assert.Single(w.Ledger.Moved);
        Assert.Equal(Start + 2_000, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_stored_outcome_for_different_arguments_is_not_a_replay()
    {
        var w = NewWorld();
        var l = Rolls(100, 1);
        Steal(w, l, Alice, Bob, "m1");
        var key = new IdempotentOutcome(new KvStateStore(w.Kv), "steal").KeyFor("m1", "twitch", Alice, "steal");
        var stored = System.Text.Json.JsonSerializer.Deserialize(w.Kv.Get(key)!, StealJsonContext.Default.StealRecord)!;
        w.Kv.Set(key, System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(stored with { ArgsHash = "other" }, StealJsonContext.Default.StealRecord), 60);
        var moved = w.Ledger.Moved.Count;

        // Not a replay: it is treated as a fresh request, which meets the victim's post-theft immunity, and moves nothing.
        Assert.Contains("robbed enough", Steal(w, l, Alice, Bob, "m1"));
        Assert.Equal(moved, w.Ledger.Moved.Count);
    }

    [Fact]
    public void a_blank_message_id_is_refused_loudly_before_any_money_moves()
    {
        var w = NewWorld();
        Assert.Throws<ArgumentException>(() => Steal(w, Rolls(100, 1), Alice, Bob, " "));
        Assert.Empty(w.Ledger.Moved);
    }

    // ---- gating and identity -----------------------------------------------------------------------------------------

    [Fact]
    public void the_bundle_is_silent_when_the_flag_is_off_and_ignores_other_commands()
    {
        var w = NewWorld();
        w.Flags.Overrides[StealLogic.FlagKey] = false;
        Assert.Null(Steal(w, Rolls(100, 1), Alice, Bob, "m1"));
        w.Flags.Overrides[StealLogic.FlagKey] = true;
        Assert.Null(w.Send(Rolls(), Alice, "!heist 10", "m2"));
        Assert.Null(w.Send(Rolls(), Alice, "hello", "m3"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_channel_without_a_community_and_an_unlinked_actor_are_refused()
    {
        var w = NewWorld();
        var l = Rolls();
        var noCommunity = l.Run(Msg($"!steal {Bob}"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("needs a community", TextOf(noCommunity!));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Steal(w, l, Alice, Bob, "m2"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Steal(w, l, Alice, Bob, "m3"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Unavailable, "down");
        Assert.Throws<WaddleIdentityException>(() => Steal(w, l, Alice, Bob, "m4"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void the_alias_falls_back_to_the_default_with_a_warning_when_the_service_cannot_answer()
    {
        var w = NewWorld();
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => throw new WaddleToolException(ToolErrorKind.TargetUnavailable, "down");
        Assert.Contains("stole 100 waddlebucks", Steal(w, Rolls(100, 1), Alice, Bob, "m1"));
        Assert.Contains(w.Log.Writes, x => x.Message == "steal.alias_unavailable");

        ClearCooldown(w, Alice);
        ClearImmunity(w, Carol);
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => System.Text.Encoding.UTF8.GetBytes("{\"user\":\"u\",\"balance\":1,\"alias\":\"\"}");
        Assert.Contains("stole 100 waddlebucks", Steal(w, Rolls(100, 1), Alice, Carol, "m2"));
    }
}

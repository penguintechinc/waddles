using System.Text;
using System.Text.Json;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using WaddleBundleSuperpenguinGamble;
using Xunit;

namespace WaddleBundleSuperpenguinGamble.Tests;

/// <summary>Scripted draws: yields the queued values in order (the d100 roll is the only draw).</summary>
file sealed class Scripted(params int[] values)
{
    private readonly Queue<int> _values = new(values);
    public int Next(int min, int max) => Math.Clamp(_values.Dequeue(), min, max);
}

public class GambleLogicTests
{
    private const string Actor = "11111111-1111-4111-8111-111111111111";
    private const string Other = "22222222-2222-4222-8222-222222222222";

    private static PlatformEventInfo Msg(string text, string? actor = "twitch-actor", string platform = "twitch") =>
        new(platform, "chat.message", actor, $"{{\"text\":\"{text}\",\"channel_id\":\"chan-1\"}}", "2026-11-07T00:00:00.000Z");

    private static FakeWaddleHostNext Host(string messageId = "evt-1", long balance = 1000, string? alias = "stars", bool flag = true, string config = "{}")
    {
        var host = new FakeWaddleHostNext().WithScope("community-1", messageId);
        host.Base.Context = host.Base.Context with { ConfigJson = config };
        host.EconomyFake.Seed(Actor, balance);
        host.EconomyFake.BeginEvent(messageId);
        host.IdentityFake.Actor = Actor;
        host.Base.FlagsFake.Overrides[GambleLogic.FlagKey] = flag;
        if (alias is not null)
        {
            host.ToolFake.Handlers[(EconomyServiceTools.Target, "points_view")] = _ =>
                Encoding.UTF8.GetBytes($"{{\"user\":\"{Actor}\",\"balance\":1,\"alias\":\"{alias}\"}}");
        }

        return host;
    }

    private static void NextEvent(FakeWaddleHostNext host, string id)
    {
        host.Base.Context = host.Base.Context with { MessageId = id };
        host.EconomyFake.BeginEvent(id);
    }

    private static string Text(PlatformEventInfo? reply) =>
        Assert.IsType<ChatReplyPayload>(reply!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)).Text;

    private static GambleLogic Rolls(params int[] rolled) => new(new Scripted(rolled).Next);

    private static long Accrued(FakeWaddleHostNext host) => host.Base.KvFake.Increment(GambleLogic.PotKey, 0, 0);

    [Fact]
    public void a_winning_roll_pays_the_stake_times_the_multiplier_through_one_atomic_wager()
    {
        var host = Host();
        var reply = Rolls(80).Run(Msg("!gamble 100"), host);

        Assert.Equal((Actor, 100UL, 200UL), Assert.Single(host.EconomyFake.Wagers));
        Assert.Equal(1100, host.EconomyFake.Balance(Actor));
        Assert.Equal($"{{user:{Actor}}} rolled 80 and won 200 stars! Balance: 1,100 stars.", Text(reply));
        Assert.Equal("chan-1", reply!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)!.ChannelId);
    }

    [Theory]
    [InlineData(1, 0UL)]
    [InlineData(48, 0UL)]
    [InlineData(49, 20UL)]
    [InlineData(100, 20UL)]
    public void rolls_above_the_minimum_for_win_win_and_the_rest_lose(int rolled, ulong payout)
    {
        var host = Host();
        Rolls(rolled).Run(Msg("!gamble 10"), host);
        Assert.Equal((Actor, 10UL, payout), Assert.Single(host.EconomyFake.Wagers));
    }

    [Fact]
    public void a_loss_states_the_stake_and_feeds_the_jackpot_pot_with_its_contribution()
    {
        var host = Host();
        var text = Text(Rolls(10).Run(Msg("!gamble 100"), host));

        Assert.Equal($"{{user:{Actor}}} rolled 10 and lost 100 stars Balance: 900 stars.", text);
        Assert.Equal(10, Accrued(host));                     // 10% of 100

        NextEvent(host, "evt-2");
        host.IdentityFake.Actor = Actor;
        Assert.Equal("The current jackpot is 1,010 stars", Text(new GambleLogic().Run(Msg("!jackpot"), host)));
    }

    [Fact]
    public void the_jackpot_number_pays_the_whole_pot_for_a_max_bet_and_the_pot_falls_back_to_the_start()
    {
        var host = Host();
        host.Base.KvFake.Increment(GambleLogic.PotKey, 200, 0);                 // pot = 1000 start + 200 accrued
        var text = Text(Rolls(69).Run(Msg("!gamble 500"), host));

        Assert.Equal((Actor, 500UL, 500UL * 2 + 1200), Assert.Single(host.EconomyFake.Wagers));   // win + whole pot
        Assert.Contains("won the jackpot of 2,200 stars!", text);
        Assert.Equal(0, Accrued(host));                                          // pot never below the starting 1000
    }

    [Fact]
    public void a_smaller_bet_wins_a_proportional_share_of_the_jackpot()
    {
        var host = Host();
        Rolls(69).Run(Msg("!gamble 250"), host);                                 // 250/500 of the 1000 start = 500
        Assert.Equal((Actor, 250UL, 250UL * 2 + 500), Assert.Single(host.EconomyFake.Wagers));
        Assert.Equal(0, Accrued(host));
    }

    [Fact]
    public void a_jackpot_win_takes_only_what_has_accrued_from_the_pot_above_the_start()
    {
        var host = Host();
        host.Base.KvFake.Increment(GambleLogic.PotKey, 1000, 0);                 // pot 2000
        Rolls(69).Run(Msg("!gamble 100"), host);                                 // share = 2000 * 100/500 = 400
        Assert.Equal(400UL + 200, Assert.Single(host.EconomyFake.Wagers).Payout);
        Assert.Equal(600, Accrued(host));                                        // 1000 accrued - 400 paid
    }

    [Fact]
    public void the_jackpot_share_is_truncated_to_the_host_payout_cap_and_only_what_was_paid_leaves_the_pot()
    {
        var host = Host(config: "{\"max_bet\":10}");
        host.Base.KvFake.Increment(GambleLogic.PotKey, 1_000_000, 0);
        Rolls(69).Run(Msg("!gamble 10"), host);

        var wager = Assert.Single(host.EconomyFake.Wagers);
        Assert.Equal(1000UL, wager.Payout);                                      // exactly 100x the stake
        Assert.Equal(1_000_000 - 980, Accrued(host));                            // share 980 = 1000 - 10 * 2
    }

    [Fact]
    public void a_jackpot_roll_with_a_pot_race_never_drives_the_pot_below_its_start()
    {
        var host = Host();
        host.Base.KvFake.Increment(GambleLogic.PotKey, 100, 0);
        var racing = new KvHost(host, new StaleReadKv(host.Base.KvFake, staleAccrued: 300));
        Rolls(69).Run(Msg("!gamble 500"), racing);

        Assert.Equal(0, Accrued(host));                                          // overdrawn to -200, then restored to 0
    }

    [Fact]
    public void a_pot_change_is_applied_at_most_once_per_event_even_on_redelivery()
    {
        var host = Host(messageId: "evt-replay");
        Rolls(10).Run(Msg("!gamble 100"), host);
        Assert.Equal(10, Accrued(host));

        host.EconomyFake.BeginEvent("evt-replay");
        new GambleLogic().Run(Msg("!gamble 100"), host);
        Assert.Equal(10, Accrued(host));
    }

    [Fact]
    public void a_zero_contribution_leaves_the_pot_alone()
    {
        var host = Host(config: "{\"jackpot_contribution\":0}");
        Rolls(10).Run(Msg("!gamble 100"), host);
        Assert.Equal(0, Accrued(host));
    }

    [Theory]
    [InlineData("!gamble", "To gamble, do !gamble amount to specify amount or do !gamble max or all to do the max bet. You can also do it by percentage like !gamble 50%")]
    [InlineData("!gamble abc", "To gamble, do !gamble amount to specify amount or do !gamble max or all to do the max bet. You can also do it by percentage like !gamble 50%")]
    [InlineData("!gamble 1 2", "To gamble, do !gamble amount to specify amount or do !gamble max or all to do the max bet. You can also do it by percentage like !gamble 50%")]
    [InlineData("!gamble 4", "The max bet is 500 stars and must be greater then 5 stars")]
    [InlineData("!gamble 501", "The max bet is 500 stars and must be greater then 5 stars")]
    public void a_missing_malformed_or_out_of_range_bet_gets_the_original_message_and_no_wager(string command, string expected)
    {
        var host = Host(balance: 10_000);
        Assert.Equal(expected, Text(Rolls(80).Run(Msg(command), host)));
        Assert.Empty(host.EconomyFake.Wagers);

        NextEvent(host, "evt-2");
        Assert.NotNull(Rolls(80).Run(Msg("!gamble 10"), host));                  // the refusal did not consume the cooldown
    }

    [Theory]
    [InlineData("!gamble max", 300UL)]
    [InlineData("!gamble all", 300UL)]
    [InlineData("!gamble 50%", 150UL)]
    [InlineData("!gamble 7", 7UL)]
    public void the_bet_grammar_is_amount_max_all_or_a_percentage_of_the_max_bet(string command, ulong stake)
    {
        var host = Host(balance: 300);
        Rolls(10).Run(Msg(command), host);
        Assert.Equal(stake, Assert.Single(host.EconomyFake.Wagers).Stake);
    }

    [Fact]
    public void a_user_with_nothing_to_stake_gets_the_not_enough_message()
    {
        var host = Host(balance: 0);
        Assert.Equal("You don't have enough to gamble with.", Text(Rolls(80).Run(Msg("!gamble 10"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!gamblex")]
    [InlineData("!points")]
    [InlineData("gamble 10")]
    public void non_commands_are_ignored_with_zero_host_calls(string text)
    {
        var host = Host();
        Assert.Null(new GambleLogic().Run(Msg(text), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.ToolFake.Calls);
    }

    [Fact]
    public void a_disabled_flag_replies_nothing_for_both_commands_and_never_consumes_the_cooldown()
    {
        var host = Host(flag: false, config: "{not json");
        Assert.Null(new GambleLogic().Run(Msg("!gamble 10"), host));
        Assert.Null(new GambleLogic().Run(Msg("!jackpot"), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Contains((GambleLogic.FlagKey, false), host.Base.FlagsFake.Calls);
        Assert.Equal("waddles.command-superpenguin-gamble", GambleLogic.FlagKey);

        host.Base.Context = host.Base.Context with { ConfigJson = "{}" };
        host.Base.FlagsFake.Overrides[GambleLogic.FlagKey] = true;
        Assert.NotNull(Rolls(10).Run(Msg("!gamble 10"), host));
    }

    [Fact]
    public void a_second_gamble_within_the_cooldown_is_silently_dropped_but_jackpot_has_no_cooldown()
    {
        var host = Host();
        Assert.NotNull(Rolls(10).Run(Msg("!gamble 10"), host));
        NextEvent(host, "evt-2");
        Assert.Null(Rolls(10).Run(Msg("!gamble 10"), host));
        Assert.Single(host.EconomyFake.Wagers);

        for (var i = 0; i < 3; i++)
        {
            NextEvent(host, $"evt-j{i}");
            Assert.NotNull(new GambleLogic().Run(Msg("!jackpot"), host));
        }
    }

    [Fact]
    public void different_users_have_independent_cooldowns()
    {
        var host = Host();
        Assert.NotNull(Rolls(10).Run(Msg("!gamble 10"), host));
        NextEvent(host, "evt-2");
        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 100);
        Assert.NotNull(Rolls(10).Run(Msg("!gamble 10", "other"), host));
    }

    [Fact]
    public void redelivery_of_the_same_event_reprints_the_same_outcome_and_moves_money_once()
    {
        var host = Host(messageId: "evt-replay");
        var first = Text(Rolls(90).Run(Msg("!gamble 20"), host));
        Assert.Equal(1020, host.EconomyFake.Balance(Actor));

        host.EconomyFake.BeginEvent("evt-replay");
        var replay = Text(Rolls(1).Run(Msg("!gamble 20"), host));                // a re-roll WOULD lose

        Assert.Equal(first, replay);
        Assert.Equal(1020, host.EconomyFake.Balance(Actor));
        Assert.All(host.EconomyFake.Wagers, w => Assert.Equal((Actor, 20UL, 40UL), w));
    }

    [Fact]
    public void a_replayed_jackpot_win_repays_the_recorded_share_and_takes_it_from_the_pot_once()
    {
        var host = Host(messageId: "evt-replay");
        host.Base.KvFake.Increment(GambleLogic.PotKey, 300, 0);
        var first = Text(Rolls(69).Run(Msg("!gamble 500"), host));
        Assert.Equal(0, Accrued(host));

        host.EconomyFake.BeginEvent("evt-replay");
        var replay = Text(Rolls(1).Run(Msg("!gamble 500"), host));
        Assert.Equal(first, replay);
        Assert.Equal(0, Accrued(host));
        Assert.All(host.EconomyFake.Wagers, w => Assert.Equal((Actor, 500UL, 2300UL), w));
    }

    [Fact]
    public void the_same_message_id_from_a_different_actor_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        var first = Text(Rolls(90).Run(Msg("!gamble 40"), host));

        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 1000);
        var second = Text(Rolls(5).Run(Msg("!gamble 40", "other"), host));

        Assert.NotEqual(first, second);
        Assert.Contains($"{{user:{Other}}} rolled 5 and lost 40", second);
        Assert.Equal(960, host.EconomyFake.Balance(Other));
        Assert.Equal(Other, host.EconomyFake.Wagers[1].User);
    }

    [Fact]
    public void the_same_message_id_from_a_different_platform_command_or_bet_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        Assert.NotNull(Rolls(90).Run(Msg("!gamble 40"), host));

        // A different platform / bet is validated like any new command: the first delivery's cooldown still holds.
        Assert.Null(Rolls(5).Run(Msg("!gamble 40", platform: "discord"), host));
        Assert.Null(Rolls(5).Run(Msg("!gamble 99"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData("")]
    [InlineData("  ")]
    public void an_invocation_without_a_message_id_is_refused_loudly_for_both_commands(string messageId)
    {
        var host = Host(messageId: messageId);
        Assert.Throws<InvalidOperationException>(() => Rolls(80).Run(Msg("!gamble 10"), host));
        Assert.Throws<InvalidOperationException>(() => new GambleLogic().Run(Msg("!jackpot"), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.Base.KvFake.SetCalls);
        Assert.Equal(0, Accrued(host));
    }

    [Fact]
    public void insufficient_funds_replies_hands_the_cooldown_back_and_leaves_the_pot_untouched()
    {
        var host = Host(balance: 500);
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "poor", 3);
        Assert.Equal("You don't have enough to gamble with.", Text(Rolls(10).Run(Msg("!gamble 100"), host)));
        Assert.Equal(0, Accrued(host));

        host.EconomyFake.ThrowOnWager = null;
        NextEvent(host, "evt-2");
        Assert.NotNull(Rolls(10).Run(Msg("!gamble 100"), host));
    }

    [Fact]
    public void an_over_cap_refusal_gets_a_reply_too()
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 5);
        Assert.Equal("You don't have enough to gamble with.", Text(Rolls(10).Run(Msg("!gamble 100"), host)));
    }

    [Theory]
    [InlineData(EconomyErrorKind.Denied)]
    [InlineData(EconomyErrorKind.Unavailable)]
    [InlineData(EconomyErrorKind.Backend)]
    [InlineData(EconomyErrorKind.NotAMember)]
    public void host_side_economy_failures_are_loud_and_never_move_the_pot(EconomyErrorKind kind)
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(kind, "host said no");
        var ex = Assert.Throws<WaddleEconomyException>(() => Rolls(10).Run(Msg("!gamble 100"), host));
        Assert.Equal(kind, ex.Kind);
        Assert.Equal(0, Accrued(host));
    }

    [Fact]
    public void a_retry_after_a_host_failure_reuses_the_recorded_roll()
    {
        var host = Host(messageId: "evt-retry");
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.Backend, "blip");
        Assert.Throws<WaddleEconomyException>(() => Rolls(95).Run(Msg("!gamble 10"), host));

        host.EconomyFake.ThrowOnWager = null;
        host.EconomyFake.BeginEvent("evt-retry");
        var text = Text(Rolls(1).Run(Msg("!gamble 10"), host));
        Assert.Contains("rolled 95 and won 20", text);
        Assert.Equal((Actor, 10UL, 20UL), host.EconomyFake.Wagers.Last());
    }

    [Theory]
    [InlineData("!gamble 10")]
    [InlineData("!jackpot")]
    public void an_unlinked_user_is_told_to_link_and_nothing_is_staked(string command)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", Text(new GambleLogic().Run(Msg(command), host)));
        Assert.Empty(host.EconomyFake.Wagers);

        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(IdentityErrorKind.NotAMember, "x");
        Assert.Contains("Link your account", Text(new GambleLogic().Run(Msg(command), host)));
    }

    [Theory]
    [InlineData(IdentityErrorKind.Denied)]
    [InlineData(IdentityErrorKind.Unavailable)]
    public void other_identity_failures_are_loud(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Throws<WaddleIdentityException>(() => new GambleLogic().Run(Msg("!gamble 10"), host));
    }

    [Fact]
    public void a_tenant_wide_activation_without_a_community_refuses_loudly()
    {
        var host = Host();
        host.WithScope(null, "evt-1");
        Assert.Contains("needs a community", Text(new GambleLogic().Run(Msg("!gamble 10"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Contains(host.Base.LogFake.Writes, w => w.Message == "gamble.refused");
    }

    [Fact]
    public void the_currency_alias_comes_from_the_economy_service_tool_and_falls_back_loudly()
    {
        var host = Host(alias: "gems");
        Assert.Contains("won 20 gems!", Text(Rolls(90).Run(Msg("!gamble 10"), host)));
        Assert.Equal("points_view", host.ToolFake.Calls.First(c => c.Tool == "points_view").Tool);

        var missing = Host(alias: null);
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Rolls(90).Run(Msg("!gamble 10"), missing)));
        Assert.Contains(missing.Base.LogFake.Writes, w => w.Message == "gamble.alias_unavailable");

        var blank = Host(alias: " ");
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Rolls(90).Run(Msg("!gamble 10"), blank)));
    }

    [Fact]
    public void a_denying_kv_fails_loud_instead_of_running_an_unguarded_money_game()
    {
        var host = Host();
        Assert.Throws<WaddleKvException>(() => new GambleLogic().Run(Msg("!gamble 10"), new KvHost(host, new DenyingKvClient())));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void logs_and_state_keys_never_carry_the_actor_the_user_uuid_or_the_event_id()
    {
        var host = Host();
        Rolls(10).Run(Msg("!gamble 100", "secret-platform-id"), host);

        foreach (var write in host.Base.LogFake.Writes)
        {
            Assert.DoesNotContain(Actor, write.FieldsJson + write.Message);
            Assert.DoesNotContain("secret-platform-id", write.FieldsJson + write.Message);
        }

        Assert.NotEmpty(host.Base.KvFake.SetCalls);
        foreach (var (key, _, _) in host.Base.KvFake.SetCalls)
        {
            Assert.DoesNotContain(':', key);
            Assert.DoesNotContain(Actor, key);
            Assert.DoesNotContain("evt-1", key);
        }
    }

    [Fact]
    public void the_default_random_source_drives_a_full_gamble_without_a_script()
    {
        var host = Host();
        Assert.NotNull(new GambleLogic().Run(Msg("!gamble 10"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void custom_config_changes_the_odds_multiplier_messages_and_the_jackpot_number()
    {
        var host = Host(config: "{\"minimum_for_win\":90,\"winning_multiplier\":3,\"jackpot_number\":7,\"win_message\":\"{Name} wins {Points} {PointType} on {Rolled}\"}");
        var text = Text(Rolls(95).Run(Msg("!gamble 10"), host));
        Assert.Equal($"{{user:{Actor}}} wins 30 stars on 95 Balance: 1,020 stars.", text);

        NextEvent(host, "evt-2");
        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 100);
        Rolls(7).Run(Msg("!gamble 10", "o"), host);                              // jackpot number 7 wins even below the win range
        Assert.Equal(10UL * 3 + 20, host.EconomyFake.Wagers.Last().Payout);       // win x3 + 10/500 of the 1000 pot
    }

    [Theory]
    [InlineData("{\"min_bet\":0}")]
    [InlineData("{\"min_bet\":10,\"max_bet\":5}")]
    [InlineData("{\"jackpot_number\":0}")]
    [InlineData("{\"jackpot_number\":101}")]
    [InlineData("{\"minimum_for_win\":101}")]
    [InlineData("{\"winning_multiplier\":0}")]
    [InlineData("{\"winning_multiplier\":101}")]
    [InlineData("{\"jackpot_contribution\":1.5}")]
    [InlineData("{\"starting_jackpot\":-1}")]
    [InlineData("{\"cooldown_seconds\":0}")]
    [InlineData("{\"win_message\":\" \"}")]
    [InlineData("{\"lose_message\":null}")]
    [InlineData("null")]
    public void an_invalid_config_fails_loud_instead_of_running_with_surprising_odds(string json)
    {
        var host = Host(config: json);
        Assert.Throws<InvalidOperationException>(() => new GambleLogic().Run(Msg("!gamble 10"), host));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void a_config_that_is_not_json_fails_loud_and_a_blank_one_means_defaults()
    {
        Assert.ThrowsAny<JsonException>(() => GambleConfig.Load("{not json"));
        var defaults = GambleConfig.Load("");
        Assert.Equal(5, defaults.MinBet);
        Assert.Equal(500, defaults.MaxBet);
        Assert.Equal(69, defaults.JackpotNumber);
        Assert.Equal(48, defaults.MinimumForWin);
        Assert.Equal(2, defaults.WinningMultiplier);
        Assert.Equal(1000, GambleConfig.Load(" ").StartingJackpot);
        Assert.Equal(0.10, GambleConfig.Load("{}").JackpotContribution);
        Assert.Equal(180u, GambleConfig.Load("{}").CooldownSeconds);
    }

    [Theory]
    [InlineData(1000, 500UL, 500, 2, 1000UL)]
    [InlineData(1000, 250UL, 500, 2, 500UL)]
    [InlineData(1000, 900UL, 500, 2, 1000UL)]      // a bet above max_bet earns no more than the whole pot
    [InlineData(0, 100UL, 500, 2, 0UL)]
    [InlineData(1000, 0UL, 500, 2, 0UL)]
    [InlineData(1_000_000, 10UL, 10, 2, 980UL)]    // truncated to the 100x payout cap
    public void the_jackpot_share_is_proportional_to_the_bet_and_capped(long pot, ulong stake, long maxBet, int multiplier, ulong share)
    {
        var config = new GambleConfig { MaxBet = maxBet, WinningMultiplier = multiplier };
        Assert.Equal(share, GambleLogic.JackpotShare(pot, stake, config));
    }

    [Fact]
    public void the_render_helper_replaces_tokens_case_insensitively_like_the_original()
    {
        Assert.Equal("a 5 b 5", GambleLogic.Render("a {POINTS} b {points}", ("{Points}", "5")));
    }
}

/// <summary>A host identical to <c>inner</c> except for its `kv`.</summary>
file sealed class KvHost(FakeWaddleHostNext inner, IKvClient kv) : WaddleSdk.Stage.IWaddleHostNext
{
    public WaddleSdk.Context.BundleContextInfo Context => inner.Context;
    public IKvClient Kv { get; } = kv;
    public WaddleSdk.Db.IDbClient Db => inner.Db;
    public WaddleSdk.Relay.IRelayClient Relay => inner.Relay;
    public WaddleSdk.Http.IHttpClient Http => inner.Http;
    public WaddleSdk.Flags.IFlagsClient Flags => inner.Flags;
    public WaddleSdk.Log.ILogClient Log => inner.Log;
    public WaddleSdk.Clock.IClockClient Clock => inner.Clock;
    public IEconomyClient Economy => inner.Economy;
    public IIdentityClient Identity => inner.Identity;
    public WaddleSdk.Tool.IToolClient Tool => inner.Tool;
    public WaddleSdk.Overlay.IOverlayClient Overlay => inner.Overlay;
    public WaddleSdk.Ai.IAiClient Ai => inner.Ai;
    public WaddleSdk.Media.IMediaClient Media => inner.Media;
}

/// <summary>A kv whose zero-delta reads of the pot return a stale (larger) value while writes hit the real counter: a lost race.</summary>
file sealed class StaleReadKv(IKvClient real, long staleAccrued) : IKvClient
{
    public byte[]? Get(string key) => real.Get(key);

    public void Set(string key, byte[] value, uint ttlSeconds) => real.Set(key, value, ttlSeconds);

    public void Delete(string key) => real.Delete(key);

    public long Increment(string key, long delta, uint ttlSeconds) =>
        key == "gamble.pot" && delta == 0 ? staleAccrued : real.Increment(key, delta, ttlSeconds);
}

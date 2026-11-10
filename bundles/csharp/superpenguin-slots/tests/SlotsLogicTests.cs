using System.Text;
using System.Text.Json;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using WaddleBundleSuperpenguinSlots;
using Xunit;

namespace WaddleBundleSuperpenguinSlots.Tests;

/// <summary>Scripted draws: yields the queued values in order for reel 1, 2, 3, win-message, lose-message.</summary>
file sealed class Scripted(params int[] values)
{
    private readonly Queue<int> _values = new(values);
    public int Next(int min, int max) => Math.Clamp(_values.Dequeue(), min, max);
}

public class SlotsLogicTests
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
        host.Base.FlagsFake.Overrides[SlotsLogic.FlagKey] = flag;
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

    /// <summary>Scripted spin: reel1, reel2, reel3, win message index, lose message index.</summary>
    private static SlotsLogic Spins(params int[] draws) => new(new Scripted(draws).Next);

    [Theory]
    [InlineData("!slot")]
    [InlineData("!slots")]
    public void both_aliases_spin_and_pay_through_one_atomic_wager(string command)
    {
        var host = Host();
        var reply = Spins(2, 2, 2, 0, 0).Run(Msg(command), host);

        var wager = Assert.Single(host.EconomyFake.Wagers);
        Assert.Equal((Actor, 25UL, 88UL), wager);                // bare command = minimum bet 25; 3 of a kind pays 3.5x = 87.5 -> 88 (half-to-even)
        Assert.Equal(1063, host.EconomyFake.Balance(Actor));
        var text = Text(reply);
        Assert.StartsWith($"{{user:{Actor}}}: [ LUL LUL LUL ] 88 stars. Congratulations!", text);
        Assert.Contains("Balance: 1,063 stars", text);
        Assert.Equal("chan-1", reply!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)!.ChannelId);
    }

    [Theory]
    [InlineData(0, 0, 0, 87UL + 1)]    // 3 of a kind: 3.5x of 25 = 87.5 -> 88
    [InlineData(0, 0, 1, 62UL)]        // first two equal: 2.5x of 25 = 62.5 -> 62 (half-to-even)
    [InlineData(0, 1, 1, 38UL)]        // last two equal: 1.5x of 25 = 37.5 -> 38
    [InlineData(1, 0, 1, 38UL)]        // first and last equal: still the "last two" multiplier
    [InlineData(0, 1, 2, 0UL)]         // no match: loss
    public void each_reel_pattern_pays_its_configured_multiple_of_the_stake(int r1, int r2, int r3, ulong payout)
    {
        var host = Host();
        Spins(r1, r2, r3, 0, 0).Run(Msg("!slots"), host);
        Assert.Equal((Actor, 25UL, payout), Assert.Single(host.EconomyFake.Wagers));
    }

    [Fact]
    public void a_loss_states_the_stake_and_uses_a_lose_line_with_the_name_substituted()
    {
        var host = Host();
        var text = Text(Spins(0, 1, 2, 0, 8).Run(Msg("!slots"), host));   // lose[8] = "Will {NAME_HERE} finally win? ..."

        Assert.Equal(975, host.EconomyFake.Balance(Actor));
        Assert.Contains("You lose 25 stars.", text);
        Assert.Contains($"Will {{user:{Actor}}} finally win?", text);
        Assert.DoesNotContain("{NAME_HERE}", text);
    }

    [Fact]
    public void the_lose_message_index_ranges_over_the_whole_lose_pool()
    {
        var host = Host();
        var last = SlotsMessages.Lose.Length - 1;
        var text = Text(Spins(0, 1, 2, 0, last).Run(Msg("!slots"), host));
        Assert.Contains(SlotsMessages.Lose[last], text);
    }

    [Theory]
    [InlineData("!slots 100", 100UL)]
    [InlineData("!slots max", 300UL)]
    [InlineData("!slots all", 300UL)]
    [InlineData("!slots 50%", 150UL)]
    [InlineData("!slots 100%", 300UL)]
    [InlineData("!slots 33.4%", 100UL)]
    public void the_bet_grammar_is_amount_max_all_or_a_percentage_of_the_max_bet(string command, ulong stake)
    {
        var host = Host(balance: 300);
        Spins(0, 1, 2, 0, 0).Run(Msg(command), host);
        Assert.Equal(stake, Assert.Single(host.EconomyFake.Wagers).Stake);
    }

    [Theory]
    [InlineData("!slots abc")]
    [InlineData("!slots -5")]
    [InlineData("!slots 0%")]
    [InlineData("!slots 101%")]
    [InlineData("!slots x%")]
    [InlineData("!slots 5 6")]
    public void a_malformed_bet_gets_the_usage_text_and_no_wager(string command)
    {
        var host = Host();
        var text = Text(Spins(0, 1, 2, 0, 0).Run(Msg(command), host));
        Assert.Equal("To use !slots, either just run !slots or do !slots max or all or amount or % (ie. 50%)", text);
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData("!slots 24")]
    [InlineData("!slots 501")]
    public void a_bet_outside_min_and_max_is_refused_with_the_limits_and_keeps_the_cooldown_free(string command)
    {
        var host = Host(balance: 10_000);
        var text = Text(Spins(0, 1, 2, 0, 0).Run(Msg(command), host));
        Assert.Equal("The max bet is 500 and must be greater then 25", text);
        Assert.Empty(host.EconomyFake.Wagers);

        NextEvent(host, "evt-2");
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slots 25"), host));   // the refusal did not consume the cooldown
    }

    [Fact]
    public void a_user_with_nothing_to_stake_is_told_so()
    {
        var host = Host(balance: 0);
        Assert.Contains("no stars to play slots with", Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void a_bare_command_with_less_than_the_minimum_bet_available_is_refused()
    {
        var host = Host(balance: 10);
        Assert.Contains("must be greater then 25", Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!slotsx")]
    [InlineData("!points")]
    [InlineData("slots")]
    public void non_commands_are_ignored_with_zero_host_calls(string text)
    {
        var host = Host();
        Assert.Null(new SlotsLogic().Run(Msg(text), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.ToolFake.Calls);
    }

    [Fact]
    public void a_disabled_flag_replies_nothing_and_never_consumes_the_cooldown()
    {
        var host = Host(flag: false);
        Assert.Null(new SlotsLogic().Run(Msg("!slots"), host));
        Assert.Empty(host.EconomyFake.Wagers);

        host.Base.FlagsFake.Overrides[SlotsLogic.FlagKey] = true;
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
    }

    [Fact]
    public void the_flag_is_checked_under_its_documented_key_defaulting_off()
    {
        var host = Host();
        new SlotsLogic().Run(Msg("!slots"), host);
        Assert.Contains((SlotsLogic.FlagKey, false), host.Base.FlagsFake.Calls);
        Assert.Equal("waddles.command-superpenguin-slots", SlotsLogic.FlagKey);
    }

    [Fact]
    public void a_second_spin_within_the_cooldown_is_silently_dropped_like_the_original()
    {
        var host = Host();
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
        NextEvent(host, "evt-2");
        Assert.Null(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void slot_and_slots_cooldowns_track_independently_and_users_do_not_share_one()
    {
        var host = Host();
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slot"), host));
        NextEvent(host, "evt-2");
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));

        NextEvent(host, "evt-3");
        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 100);
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slot", "other"), host));
    }

    [Fact]
    public void redelivery_of_the_same_event_reprints_the_same_outcome_and_moves_money_once()
    {
        var host = Host(messageId: "evt-replay");
        var first = Text(Spins(1, 1, 1, 2, 0).Run(Msg("!slots 40"), host));
        var expected = 1000 - 40 + 140;
        Assert.Equal(expected, host.EconomyFake.Balance(Actor));

        host.EconomyFake.BeginEvent("evt-replay");
        var replay = Text(Spins(0, 1, 2, 5, 5).Run(Msg("!slots 40"), host));   // a re-spin WOULD differ

        Assert.Equal(first, replay);
        Assert.Equal(expected, host.EconomyFake.Balance(Actor));
        Assert.All(host.EconomyFake.Wagers, w => Assert.Equal((Actor, 40UL, 140UL), w));
    }

    [Fact]
    public void a_replay_is_not_dropped_by_the_cooldown_the_first_delivery_took()
    {
        var host = Host(messageId: "evt-replay");
        Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host);
        host.EconomyFake.BeginEvent("evt-replay");
        Assert.NotNull(new SlotsLogic().Run(Msg("!slots"), host));
    }

    [Fact]
    public void the_same_message_id_from_a_different_actor_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        var first = Text(Spins(1, 1, 1, 0, 0).Run(Msg("!slots 40"), host));

        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 1000);
        var second = Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots 40", "other"), host));

        Assert.NotEqual(first, second);
        Assert.Contains($"{{user:{Other}}}", second);
        Assert.Contains("You lose 40 stars.", second);
        Assert.Equal(960, host.EconomyFake.Balance(Other));          // validated and settled as a brand-new command
        Assert.Equal(2, host.EconomyFake.Wagers.Count);
        Assert.Equal(Other, host.EconomyFake.Wagers[1].User);
    }

    [Fact]
    public void the_same_message_id_from_a_different_platform_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        Assert.NotNull(Spins(1, 1, 1, 0, 0).Run(Msg("!slots 40"), host));

        // Same actor, same cooldown: a different platform's event is validated like any new command (so it hits the
        // cooldown the first took) instead of reprinting the recorded outcome.
        Assert.Null(Spins(0, 1, 2, 0, 0).Run(Msg("!slots 40", platform: "discord"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void the_same_message_id_with_a_different_command_or_bet_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        Spins(1, 1, 1, 0, 0).Run(Msg("!slots 40"), host);

        // `!slot` is a different command (own cooldown): fresh spin, own outcome, not the recorded `!slots` one.
        var other = Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slot 40"), host));
        Assert.Contains("You lose 40 stars.", other);

        // Same command, same id, DIFFERENT bet: not a replay; full validation hits the cooldown the first took.
        Assert.Null(Spins(0, 1, 2, 0, 0).Run(Msg("!slots 99"), host));
        Assert.Equal(2, host.EconomyFake.Wagers.Count);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void an_invocation_without_a_message_id_is_refused_loudly_and_moves_no_money(string messageId)
    {
        var host = Host(messageId: messageId);
        var ex = Assert.Throws<InvalidOperationException>(() => Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
        Assert.Contains("message id", ex.Message);
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.Base.KvFake.SetCalls);
    }

    [Fact]
    public void insufficient_funds_replies_and_hands_the_cooldown_back()
    {
        var host = Host(balance: 500);
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "poor", 3);
        Assert.Contains("you don't have that much to play slots with", Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host)));

        host.EconomyFake.ThrowOnWager = null;
        NextEvent(host, "evt-2");
        Assert.NotNull(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
    }

    [Fact]
    public void an_over_cap_refusal_gets_a_reply_too()
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 5);
        Assert.Contains("over the limit for stars", Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host)));
    }

    [Theory]
    [InlineData(EconomyErrorKind.Denied)]
    [InlineData(EconomyErrorKind.Unavailable)]
    [InlineData(EconomyErrorKind.Backend)]
    [InlineData(EconomyErrorKind.NotAMember)]
    public void host_side_economy_failures_are_loud_never_a_silent_unpaid_spin(EconomyErrorKind kind)
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(kind, "host said no");
        var ex = Assert.Throws<WaddleEconomyException>(() => Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
        Assert.Equal(kind, ex.Kind);
    }

    [Fact]
    public void a_retry_after_a_host_failure_reuses_the_recorded_reels()
    {
        var host = Host(messageId: "evt-retry");
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.Backend, "blip");
        Assert.Throws<WaddleEconomyException>(() => Spins(3, 3, 3, 1, 1).Run(Msg("!slots"), host));

        host.EconomyFake.ThrowOnWager = null;
        host.EconomyFake.BeginEvent("evt-retry");
        var text = Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
        Assert.Contains("[ 4Head 4Head 4Head ]", text);
        Assert.Equal((Actor, 25UL, 88UL), host.EconomyFake.Wagers.Last());
    }

    [Theory]
    [InlineData(IdentityErrorKind.NotLinked)]
    [InlineData(IdentityErrorKind.NotAMember)]
    public void an_unlinked_user_is_told_to_link_and_nothing_is_staked(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Contains("Link your account", Text(new SlotsLogic().Run(Msg("!slots"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData(IdentityErrorKind.Denied)]
    [InlineData(IdentityErrorKind.Unavailable)]
    public void other_identity_failures_are_loud(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Throws<WaddleIdentityException>(() => new SlotsLogic().Run(Msg("!slots"), host));
    }

    [Fact]
    public void a_tenant_wide_activation_without_a_community_refuses_loudly()
    {
        var host = Host();
        host.WithScope(null, "evt-1");
        Assert.Contains("needs a community", Text(new SlotsLogic().Run(Msg("!slots"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Contains(host.Base.LogFake.Writes, w => w.Message == "slots.refused");
    }

    [Fact]
    public void the_currency_alias_comes_from_the_economy_service_tool()
    {
        var host = Host(alias: "gems");
        var text = Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), host));
        Assert.Contains("gems", text);
        Assert.Equal("points_view", host.ToolFake.Calls.First(c => c.Tool == "points_view").Tool);
        Assert.Contains(Actor, host.ToolFake.Calls.First(c => c.Tool == "points_view").Args);
    }

    [Fact]
    public void an_unanswerable_or_blank_alias_falls_back_to_the_documented_default_and_is_logged()
    {
        var missing = Host(alias: null);
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), missing)));
        Assert.Contains(missing.Base.LogFake.Writes, w => w.Message == "slots.alias_unavailable");

        var blank = Host(alias: "  ");
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Spins(0, 1, 2, 0, 0).Run(Msg("!slots"), blank)));
    }

    [Fact]
    public void a_denying_kv_fails_loud_instead_of_running_an_unguarded_money_game()
    {
        var host = Host();
        Assert.Throws<WaddleSdk.Kv.WaddleKvException>(() => new SlotsLogic().Run(Msg("!slots"), new DenyingKvHost(host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void logs_and_state_keys_never_carry_the_actor_the_user_uuid_or_the_event_id()
    {
        var host = Host();
        Spins(2, 2, 2, 0, 0).Run(Msg("!slots", "secret-platform-id"), host);

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
    public void the_default_random_source_drives_a_full_spin_without_a_script()
    {
        var host = Host();
        Assert.NotNull(new SlotsLogic().Run(Msg("!slots"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void the_default_payout_table_stays_within_the_host_cap_and_matches_the_originals_odds()
    {
        var config = new SlotsConfig();
        Assert.All(new[] { config.ThreeOfAKind, config.FirstTwo, config.LastTwo }, m => Assert.InRange(m, 0.01, 100));
        // 5 emotes: 5/125 three of a kind, 20/125 first two, 40/125 last two (the upstream defaults, 102% expected return).
        var expected = (5 * config.ThreeOfAKind + 20 * config.FirstTwo + 40 * config.LastTwo) / 125.0;
        Assert.Equal(1.02, expected, 3);
    }

    [Fact]
    public void the_message_pools_are_the_originals_verbatim_and_every_line_renders()
    {
        Assert.Equal(20, SlotsMessages.Win.Length);
        Assert.Equal(62, SlotsMessages.Lose.Length);
        Assert.Contains("The Goddess Fortuna smiles upon you.", SlotsMessages.Win);
        Assert.Contains("If there were more clumsy and perverted people like {NAME_HERE}, the world would be a better place.", SlotsMessages.Win);
        Assert.All(SlotsMessages.Win.Concat(SlotsMessages.Lose), m => Assert.False(string.IsNullOrWhiteSpace(m)));
    }

    [Fact]
    public void result_text_for_a_win_and_a_loss_keeps_the_original_shape()
    {
        var config = new SlotsConfig();
        var win = SlotsLogic.BuildResultText(new SlotsOutcome(Actor, "slots", "!slots", 100, 0, 0, 0, 0, 0), config, "{user:x}", "stars", 5);
        Assert.Equal("{user:x}: [ Kappa Kappa Kappa ] 350 stars. Congratulations! Balance: 5 stars.", win);

        var loss = SlotsLogic.BuildResultText(new SlotsOutcome(Actor, "slots", "!slots", 100, 0, 1, 2, 0, 0), config, "{user:x}", "stars", 5);
        Assert.Equal("{user:x}: [ Kappa PogChamp LUL ] You lose 100 stars. Better luck next time! Balance: 5 stars.", loss);
    }

    [Fact]
    public void custom_config_changes_emotes_multipliers_and_the_minimum_bet()
    {
        var host = Host(config: "{\"emotes\":[\"A\",\"B\"],\"three_of_a_kind\":10,\"first_two\":4,\"last_two\":2,\"min_bet\":10}");
        var text = Text(Spins(1, 1, 1, 0, 0).Run(Msg("!slots"), host));

        Assert.Equal((Actor, 10UL, 100UL), Assert.Single(host.EconomyFake.Wagers));   // bare command stakes the configured minimum
        Assert.Contains("[ B B B ] 100 stars.", text);
    }

    [Theory]
    [InlineData("{\"min_bet\":0}")]
    [InlineData("{\"three_of_a_kind\":0}")]
    [InlineData("{\"first_two\":101}")]
    [InlineData("{\"last_two\":-1}")]
    [InlineData("{\"emotes\":[\"one\"]}")]
    [InlineData("{\"emotes\":[\"a\",\" \"]}")]
    [InlineData("{\"emotes\":null}")]
    [InlineData("{\"cooldown_seconds\":0}")]
    [InlineData("null")]
    public void an_invalid_config_fails_loud_instead_of_running_with_surprising_odds(string json)
    {
        var host = Host(config: json);
        Assert.Throws<InvalidOperationException>(() => new SlotsLogic().Run(Msg("!slots"), host));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void a_config_that_is_not_json_fails_loud_and_a_blank_one_means_defaults()
    {
        Assert.ThrowsAny<JsonException>(() => SlotsConfig.Load("{not json"));
        Assert.Equal(25, SlotsConfig.Load("").MinBet);
        Assert.Equal(3.5, SlotsConfig.Load("  ").ThreeOfAKind);
    }

    [Fact]
    public void a_disabled_flag_does_not_even_parse_the_config()
    {
        var host = Host(flag: false, config: "{not json");
        Assert.Null(new SlotsLogic().Run(Msg("!slots"), host));
    }

    [Theory]
    [InlineData(new[] { "10" }, 100L, 5L, null, (int)BetParse.Ok, 10L)]
    [InlineData(new string[0], 100L, 5L, 7L, (int)BetParse.Ok, 7L)]
    [InlineData(new string[0], 100L, 5L, null, (int)BetParse.Invalid, 0L)]
    [InlineData(new[] { "max" }, 100L, 5L, null, (int)BetParse.Ok, 100L)]
    [InlineData(new[] { "ALL" }, 100L, 5L, null, (int)BetParse.Ok, 100L)]
    [InlineData(new[] { "50%" }, 99L, 5L, null, (int)BetParse.Ok, 49L)]
    [InlineData(new[] { "1%" }, 100L, 5L, null, (int)BetParse.TooLow, 1L)]
    [InlineData(new[] { "101" }, 100L, 5L, null, (int)BetParse.TooMuch, 101L)]
    [InlineData(new[] { "4" }, 100L, 5L, null, (int)BetParse.TooLow, 4L)]
    [InlineData(new[] { "1", "2" }, 100L, 5L, null, (int)BetParse.Invalid, 0L)]
    [InlineData(new[] { "NaN%" }, 100L, 5L, null, (int)BetParse.Invalid, 0L)]
    [InlineData(new[] { "1e400%" }, 100L, 5L, null, (int)BetParse.Invalid, 0L)]
    public void the_bet_parser_follows_the_original_grammar(string[] args, long max, long min, long? def, int expected, long amount)
    {
        Assert.Equal((BetParse)expected, BetParser.Parse(args, max, min, def, out var parsed));
        Assert.Equal(amount, parsed);
    }

    private sealed class DenyingKvHost(FakeWaddleHostNext inner) : WaddleSdk.Stage.IWaddleHostNext
    {
        public WaddleSdk.Context.BundleContextInfo Context => inner.Context;
        public WaddleSdk.Kv.IKvClient Kv { get; } = new DenyingKvClient();
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
}

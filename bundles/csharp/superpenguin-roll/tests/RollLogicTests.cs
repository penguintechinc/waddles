using System.Text;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using WaddleSdk.Chat;
using WaddleSdk.Json;
using WaddleBundleSuperpenguinRoll;
using Xunit;

namespace WaddleBundleSuperpenguinRoll.Tests;

/// <summary>Scripted dice: yields the queued values in order for the d1, d2, win-message, lose-message draws.</summary>
file sealed class ScriptedDice(params int[] values)
{
    private readonly Queue<int> _values = new(values);
    public int Next(int min, int max) => Math.Clamp(_values.Dequeue(), min, max);
}

public class RollLogicTests
{
    private const string Actor = "11111111-1111-4111-8111-111111111111";
    private const string Other = "22222222-2222-4222-8222-222222222222";

    private static PlatformEventInfo Msg(string text, string? actor = "twitch-actor") =>
        new("twitch", "chat.message", actor, $"{{\"text\":\"{text}\",\"channel_id\":\"chan-1\"}}", "2026-11-07T00:00:00.000Z");

    private static FakeWaddleHostNext Host(string messageId = "evt-1", long balance = 1000, string? alias = "stars", bool flag = true)
    {
        var host = new FakeWaddleHostNext().WithScope("community-1", messageId);
        host.EconomyFake.Seed(Actor, balance);
        host.EconomyFake.BeginEvent(messageId);
        host.IdentityFake.Actor = Actor;
        host.Base.FlagsFake.Overrides[RollLogic.FlagKey] = flag;
        if (alias is not null)
        {
            host.ToolFake.Handlers[(EconomyServiceTools.Target, "points_view")] = _ =>
                Encoding.UTF8.GetBytes($"{{\"user\":\"{Actor}\",\"balance\":1,\"alias\":\"{alias}\"}}");
        }

        return host;
    }

    private static string Text(PlatformEventInfo? reply) =>
        Assert.IsType<ChatReplyPayload>(reply!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)).Text;

    /// <summary>A roll whose dice and message indices are scripted: d1, d2, win msg, lose msg.</summary>
    private static RollLogic Rolls(params int[] dice) => new(new ScriptedDice(dice).Next);

    [Theory]
    [InlineData("!roll")]
    [InlineData("!dice")]
    public void both_aliases_roll_and_pay_through_one_atomic_wager(string command)
    {
        var host = Host();
        var reply = Rolls(3, 3, 0, 0).Run(Msg(command), host);

        Assert.NotNull(reply);
        var wager = Assert.Single(host.EconomyFake.Wagers);
        Assert.Equal((Actor, 10UL, 40UL), wager);              // stake 10, Hard four pays 4x
        Assert.Equal(1030, host.EconomyFake.Balance(Actor));   // 1000 - 10 + 40
        var text = Text(reply);
        Assert.Contains($"{{user:{Actor}}} rolls a [3] and [3]", text);
        Assert.Contains("Hard six for 40 stars", text);
        Assert.Contains("Balance: 1030 stars", text);
        Assert.Equal("chan-1", reply!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)!.ChannelId);
    }

    [Fact]
    public void a_miss_loses_the_stake_and_says_so()
    {
        var host = Host();
        var text = Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));

        Assert.Equal((Actor, 10UL, 0UL), Assert.Single(host.EconomyFake.Wagers));
        Assert.Equal(990, host.EconomyFake.Balance(Actor));
        Assert.Contains("You lose 10 stars.", text);
        Assert.Contains("Balance: 990 stars", text);
    }

    [Theory]
    [InlineData(1, 2, "Snake eyes")]
    [InlineData(2, 3, "Hard four")]
    [InlineData(3, 4, "Hard six")]
    [InlineData(4, 6, "Hard eight")]
    [InlineData(5, 8, "Hard ten")]
    [InlineData(6, 10, "Boxcars")]
    public void every_double_pays_its_multiplier_of_the_stake(int die, int multiplier, string prize)
    {
        var host = Host();
        var text = Text(Rolls(die, die, 0, 0).Run(Msg("!roll 25"), host));

        Assert.Equal((Actor, 25UL, (ulong)(25 * multiplier)), Assert.Single(host.EconomyFake.Wagers));
        Assert.Contains(prize, text);
    }

    [Fact]
    public void all_stakes_the_max_bet_and_a_stake_over_it_is_refused_without_a_wager()
    {
        var host = Host(balance: 300);
        host.EconomyFake.MaxBetCap = 200;
        Rolls(1, 2, 0, 0).Run(Msg("!roll all"), host);
        Assert.Equal(200UL, Assert.Single(host.EconomyFake.Wagers).Stake);

        var host2 = Host(messageId: "evt-2", balance: 300);
        host2.EconomyFake.MaxBetCap = 200;
        var text = Text(Rolls(1, 2, 0, 0).Run(Msg("!roll 201"), host2));
        Assert.Empty(host2.EconomyFake.Wagers);
        Assert.Contains("max roll right now is 200 stars", text);
    }

    [Theory]
    [InlineData("!roll 0")]
    [InlineData("!roll -5")]
    [InlineData("!roll abc")]
    [InlineData("!roll 5 6")]
    public void a_malformed_stake_gets_usage_and_no_wager(string command)
    {
        var host = Host();
        Assert.Equal("Usage: !roll [stake|all]", Text(Rolls(1, 2, 0, 0).Run(Msg(command), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void a_user_with_nothing_to_stake_is_told_so()
    {
        var host = Host(balance: 0);
        Assert.Contains("no stars to roll with", Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!rollx")]
    [InlineData("!points")]
    [InlineData("roll")]
    public void non_commands_are_ignored_with_zero_host_calls(string text)
    {
        var host = Host();
        Assert.Null(new RollLogic().Run(Msg(text), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.ToolFake.Calls);
    }

    [Fact]
    public void a_disabled_flag_replies_nothing_and_never_consumes_the_cooldown()
    {
        var off = Host(flag: false);
        Assert.Null(new RollLogic().Run(Msg("!roll"), off));
        Assert.Empty(off.EconomyFake.Wagers);

        // Same community kv, flag now on: the first roll is allowed (no cooldown was taken while off).
        off.Base.FlagsFake.Overrides[RollLogic.FlagKey] = true;
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!roll"), off));
    }

    [Fact]
    public void the_flag_is_checked_under_its_documented_key_defaulting_off()
    {
        var host = Host();
        new RollLogic().Run(Msg("!roll"), host);
        Assert.Contains((RollLogic.FlagKey, false), host.Base.FlagsFake.Calls);
        Assert.Equal("waddles.command-superpenguin-roll", RollLogic.FlagKey);
    }

    [Fact]
    public void a_second_roll_within_the_cooldown_is_silently_dropped_like_the_original()
    {
        var host = Host();
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        host.Base.Context = host.Base.Context with { MessageId = "evt-2" };
        host.EconomyFake.BeginEvent("evt-2");

        Assert.Null(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void roll_and_dice_cooldowns_track_independently()
    {
        var host = Host();
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        host.Base.Context = host.Base.Context with { MessageId = "evt-2" };
        host.EconomyFake.BeginEvent("evt-2");
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!dice"), host));
    }

    [Fact]
    public void different_users_have_independent_cooldowns()
    {
        var host = Host();
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));

        host.Base.Context = host.Base.Context with { MessageId = "evt-2" };
        host.EconomyFake.BeginEvent("evt-2");
        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 100);
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!roll", "other-actor"), host));
    }

    [Fact]
    public void redelivery_of_the_same_event_reprints_the_same_outcome_and_moves_money_once()
    {
        var host = Host(messageId: "evt-replay");
        var first = Text(Rolls(4, 4, 2, 2).Run(Msg("!roll 20"), host));
        Assert.Equal(1000 - 20 + 20 * 6, host.EconomyFake.Balance(Actor));

        // At-least-once redelivery: same message id, and the (random) dice WOULD differ if re-rolled.
        host.EconomyFake.BeginEvent("evt-replay");
        var replay = Text(Rolls(1, 2, 5, 5).Run(Msg("!roll 20"), host));

        Assert.Equal(first, replay);
        Assert.Equal(1000 - 20 + 20 * 6, host.EconomyFake.Balance(Actor));
        Assert.Equal(2, host.EconomyFake.Wagers.Count);                 // the call is repeated, the host de-duplicates it
        Assert.All(host.EconomyFake.Wagers, w => Assert.Equal((Actor, 20UL, 120UL), w));
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    public void a_blank_message_id_is_refused_loudly_before_any_money_moves(string id)
    {
        var host = Host(messageId: id);
        Assert.Throws<ArgumentException>(() => Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void the_same_message_id_for_a_different_actor_is_not_a_replay()
    {
        var host = Host(messageId: "shared");
        Rolls(1, 2, 0, 0).Run(Msg("!roll"), host);
        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 100);
        host.EconomyFake.BeginEvent("shared");
        var text = Text(Rolls(6, 6, 0, 0).Run(Msg("!roll 20", "other"), host));

        Assert.Contains("[6] and [6]", text);
        Assert.Equal((Other, 20UL, 200UL), host.EconomyFake.Wagers.Last());
    }

    [Fact]
    public void the_same_message_id_for_a_different_command_is_not_a_replay_and_validates_in_full()
    {
        var host = Host(messageId: "shared");
        Rolls(1, 2, 0, 0).Run(Msg("!roll"), host);
        host.EconomyFake.BeginEvent("shared");
        host.EconomyFake.MaxBetCap = 5;
        var text = Text(Rolls(6, 6, 0, 0).Run(Msg("!dice 10"), host));
        Assert.Contains("max roll right now is 5", text);
    }

    [Fact]
    public void a_stored_record_whose_binding_does_not_match_is_not_a_replay()
    {
        var host = Host(messageId: "evt-x");
        Rolls(1, 2, 0, 0).Run(Msg("!roll"), host);
        // Corrupt the stored binding (simulated key collision): same key, wrong actor inside.
        var key = host.Base.KvFake.SetCalls.Last(c => c.Key.StartsWith("out.roll.")).Key;
        host.Base.KvFake.Set(key, Encoding.UTF8.GetBytes("{\"d1\":6,\"d2\":6,\"stake\":10,\"win_msg\":0,\"lose_msg\":0,\"actor\":\"someone-else\",\"cmd\":\"roll\"}"), 60);
        host.EconomyFake.BeginEvent("evt-x");
        host.EconomyFake.MaxBetCap = 5;
        Assert.Contains("max roll right now is 5", Text(Rolls(1, 2, 0, 0).Run(Msg("!roll 10"), host)));
    }

    [Fact]
    public void a_replay_is_not_dropped_by_the_cooldown_the_first_delivery_took()
    {
        var host = Host(messageId: "evt-replay");
        Rolls(1, 2, 0, 0).Run(Msg("!roll"), host);
        host.EconomyFake.BeginEvent("evt-replay");
        Assert.NotNull(new RollLogic().Run(Msg("!roll"), host));
    }

    [Fact]
    public void insufficient_funds_replies_and_hands_the_cooldown_back()
    {
        var host = Host(balance: 500);
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "poor", 3);
        var refused = Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Contains("you don't have 10 stars to roll", refused);

        host.EconomyFake.ThrowOnWager = null;
        host.Base.Context = host.Base.Context with { MessageId = "evt-2" };
        host.EconomyFake.BeginEvent("evt-2");
        Assert.NotNull(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));   // cooldown was released, outcome was discarded
    }

    [Fact]
    public void an_over_cap_refusal_gets_a_reply_too()
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.OverCap, "cap", 5);
        Assert.Contains("over the limit for stars", Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host)));
    }

    [Theory]
    [InlineData(EconomyErrorKind.Denied)]
    [InlineData(EconomyErrorKind.Unavailable)]
    [InlineData(EconomyErrorKind.Backend)]
    [InlineData(EconomyErrorKind.NotAMember)]
    public void host_side_economy_failures_are_loud_never_a_silent_unpaid_roll(EconomyErrorKind kind)
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(kind, "host said no");
        var ex = Assert.Throws<WaddleEconomyException>(() => Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Equal(kind, ex.Kind);
    }

    [Fact]
    public void a_retry_after_a_host_failure_reuses_the_recorded_dice()
    {
        var host = Host(messageId: "evt-retry");
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.Backend, "blip");
        Assert.Throws<WaddleEconomyException>(() => Rolls(6, 6, 1, 1).Run(Msg("!roll"), host));

        host.EconomyFake.ThrowOnWager = null;
        host.EconomyFake.BeginEvent("evt-retry");
        var text = Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Contains("[6] and [6]", text);            // not the second script: the first delivery's dice stand
        Assert.Equal((Actor, 10UL, 100UL), host.EconomyFake.Wagers.Last());
    }

    [Theory]
    [InlineData(IdentityErrorKind.NotLinked)]
    [InlineData(IdentityErrorKind.NotAMember)]
    public void an_unlinked_user_is_told_to_link_and_nothing_is_staked(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Contains("Link your account", Text(new RollLogic().Run(Msg("!roll"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData(IdentityErrorKind.Denied)]
    [InlineData(IdentityErrorKind.Unavailable)]
    public void other_identity_failures_are_loud(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Throws<WaddleIdentityException>(() => new RollLogic().Run(Msg("!roll"), host));
    }

    [Fact]
    public void a_tenant_wide_activation_without_a_community_refuses_loudly()
    {
        var host = Host();
        host.WithScope(null, "evt-1");
        Assert.Contains("needs a community", Text(new RollLogic().Run(Msg("!roll"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Contains(host.Base.LogFake.Writes, w => w.Message == "roll.refused");
    }

    [Fact]
    public void the_currency_alias_comes_from_the_economy_service_tool()
    {
        var host = Host(alias: "gems");
        var text = Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Contains("gems", text);
        Assert.DoesNotContain("points", text);
        Assert.Equal("points_view", host.ToolFake.Calls.First(c => c.Tool == "points_view").Tool);
        Assert.Contains(Actor, host.ToolFake.Calls.First(c => c.Tool == "points_view").Args);
    }

    [Fact]
    public void an_unanswerable_alias_lookup_falls_back_to_the_documented_default_and_is_logged()
    {
        var host = Host(alias: null);
        var text = Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host));
        Assert.Contains(EconomyServiceTools.DefaultAlias, text);
        Assert.Contains(host.Base.LogFake.Writes, w => w.Message == "roll.alias_unavailable" && w.FieldsJson.Contains("not_granted"));
    }

    [Fact]
    public void an_empty_alias_from_the_service_is_not_displayed()
    {
        var host = Host(alias: "  ");
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Rolls(1, 2, 0, 0).Run(Msg("!roll"), host)));
    }

    [Fact]
    public void a_denying_kv_fails_loud_instead_of_running_an_unguarded_money_game()
    {
        var host = Host();
        var denying = new DenyingKvHost(host);
        Assert.Throws<WaddleSdk.Kv.WaddleKvException>(() => new RollLogic().Run(Msg("!roll"), denying));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void logs_and_state_keys_never_carry_the_actor_or_user_uuid()
    {
        var host = Host();
        Rolls(2, 2, 0, 0).Run(Msg("!roll", "secret-platform-id"), host);

        foreach (var write in host.Base.LogFake.Writes)
        {
            Assert.DoesNotContain(Actor, write.FieldsJson + write.Message);
            Assert.DoesNotContain("secret-platform-id", write.FieldsJson + write.Message);
        }

        foreach (var (key, _, _) in host.Base.KvFake.SetCalls)
        {
            Assert.DoesNotContain(':', key);
            Assert.DoesNotContain(Actor, key);
            Assert.DoesNotContain("evt-1", key);
        }
    }

    [Fact]
    public void the_default_random_source_drives_a_full_roll_without_script()
    {
        var host = Host();
        var reply = new RollLogic().Run(Msg("!roll"), host);
        Assert.NotNull(reply);
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void the_payout_table_never_exceeds_the_host_cap_and_keeps_a_house_edge()
    {
        Assert.All(RollLogic.Multipliers, m => Assert.InRange(m, 1UL, 100UL));
        Assert.True(RollLogic.Multipliers.Sum(m => (double)m) / 36.0 < 1.0, "expected return must stay below the stake");
        Assert.Equal(6, RollLogic.Multipliers.Length);
    }

    [Fact]
    public void every_message_template_formats_with_the_placeholder_name()
    {
        foreach (var template in RollLogic.WinMessages.Concat(RollLogic.LostMessages))
        {
            Assert.False(string.IsNullOrWhiteSpace(string.Format(template, "{user:abc}")));
        }

        Assert.Equal(20, RollLogic.WinMessages.Length);
        Assert.Equal(67, RollLogic.LostMessages.Length);
    }

    [Fact]
    public void result_text_for_boxcars_keeps_the_original_phrasing()
    {
        var text = RollLogic.BuildResultText(new RollOutcome(6, 6, 10, 0, 0, "a", "roll"), "{user:x}", "stars", 5);
        Assert.Contains("Boxcars to the max!!! 100 stars!", text);
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

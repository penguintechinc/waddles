using System.Text;
using System.Text.Json;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using WaddleBundleSuperpenguinDefuse;
using Xunit;

namespace WaddleBundleSuperpenguinDefuse.Tests;

/// <summary>Scripted draws: yields the queued values in order (winning wire index first, then the reward on a win).</summary>
file sealed class Scripted(params int[] values)
{
    private readonly Queue<int> _values = new(values);
    public int Next(int min, int max) => Math.Clamp(_values.Dequeue(), min, max);
}

public class DefuseLogicTests
{
    private const string Actor = "11111111-1111-4111-8111-111111111111";
    private const string Other = "22222222-2222-4222-8222-222222222222";

    private static PlatformEventInfo Msg(string text, string? actor = "twitch-actor", string platform = "twitch") =>
        new(platform, "chat.message", actor, $"{{\"text\":\"{text}\",\"channel_id\":\"chan-1\"}}", "2026-11-07T00:00:00.000Z");

    private static FakeWaddleHostNext Host(string messageId = "evt-1", long balance = 5000, string? alias = "stars", bool flag = true, string config = "{}")
    {
        var host = new FakeWaddleHostNext().WithScope("community-1", messageId);
        host.Base.Context = host.Base.Context with { ConfigJson = config };
        host.EconomyFake.Seed(Actor, balance);
        host.EconomyFake.BeginEvent(messageId);
        host.IdentityFake.Actor = Actor;
        host.Base.FlagsFake.Overrides[DefuseLogic.FlagKey] = flag;
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

    /// <summary>Scripted bomb: winning wire index (0 red, 1 blue, 2 yellow), then the reward draw when the wire is right.</summary>
    private static DefuseLogic Bomb(params int[] draws) => new(new Scripted(draws).Next);

    [Fact]
    public void cutting_the_right_wire_pays_the_drawn_reward_through_one_atomic_wager()
    {
        var host = Host();
        var reply = Bomb(0, 1500).Run(Msg("!defuse red"), host);

        Assert.Equal((Actor, 500UL, 1500UL), Assert.Single(host.EconomyFake.Wagers));
        Assert.Equal(6000, host.EconomyFake.Balance(Actor));
        Assert.Equal($"The bomb is beeping and {{user:{Actor}}} cuts the red wire... The bomb goes silent. As a thank for saving the day you got awarded 1,500 stars Balance: 6,000 stars.",
            Text(reply));
        Assert.Equal("chan-1", reply!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)!.ChannelId);
    }

    [Fact]
    public void cutting_the_wrong_wire_loses_the_cost_and_says_so()
    {
        var host = Host();
        var text = Text(Bomb(2).Run(Msg("!defuse blue"), host));

        Assert.Equal((Actor, 500UL, 0UL), Assert.Single(host.EconomyFake.Wagers));
        Assert.Equal(4500, host.EconomyFake.Balance(Actor));
        Assert.Equal($"The bomb is beeping and {{user:{Actor}}} cuts the blue wire... BOOM!!! The bomb explodes, you lose 500 stars. Balance: 4,500 stars.", text);
    }

    [Theory]
    [InlineData(0, 1334UL)]       // cost x 3 - cost / 3 = 1500 - 166
    [InlineData(1_000_000, 1666UL)] // cost x 3 + cost / 3 = 1500 + 166
    public void the_reward_is_drawn_inside_the_originals_window(int draw, ulong expected)
    {
        var host = Host();
        Bomb(1, draw).Run(Msg("!defuse blue"), host);
        Assert.Equal(expected, Assert.Single(host.EconomyFake.Wagers).Payout);
    }

    [Fact]
    public void the_reward_window_is_asked_for_with_the_originals_inclusive_bounds()
    {
        (int Min, int Max)? seen = null;
        var logic = new DefuseLogic((min, max) =>
        {
            if (max > 2)
            {
                seen = (min, max);
            }

            return min;
        });
        logic.Run(Msg("!defuse red"), Host());
        Assert.Equal((1334, 1666), seen);
    }

    [Theory]
    [InlineData("!defuse")]
    [InlineData("!defuse green")]
    [InlineData("!defuse Red")]
    [InlineData("!defuse   ")]
    public void a_missing_or_unknown_wire_lists_the_wires_without_charging_or_cooling_down(string command)
    {
        var host = Host();
        Assert.Equal("you need to choose one of these wires to cut: red, blue, yellow", Text(Bomb(0).Run(Msg(command), host)));
        Assert.Empty(host.EconomyFake.Wagers);

        NextEvent(host, "evt-2");
        Assert.NotNull(Bomb(1).Run(Msg("!defuse red"), host));
    }

    [Fact]
    public void a_user_who_cannot_afford_the_cost_is_told_so_and_keeps_the_cooldown_free()
    {
        var host = Host(balance: 100);
        Assert.Equal("Sorry it costs 500 stars to defuse the bomb which you do not have.", Text(Bomb(0).Run(Msg("!defuse red"), host)));
        Assert.Empty(host.EconomyFake.Wagers);

        host.EconomyFake.Seed(Actor, 5000);
        NextEvent(host, "evt-2");
        Assert.NotNull(Bomb(1).Run(Msg("!defuse red"), host));
    }

    [Theory]
    [InlineData("hello")]
    [InlineData("!defusex")]
    [InlineData("!points")]
    [InlineData("defuse red")]
    public void non_commands_are_ignored_with_zero_host_calls(string text)
    {
        var host = Host();
        Assert.Null(new DefuseLogic().Run(Msg(text), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.ToolFake.Calls);
    }

    [Fact]
    public void a_disabled_flag_replies_nothing_never_parses_the_config_and_never_consumes_the_cooldown()
    {
        var host = Host(flag: false, config: "{not json");
        Assert.Null(new DefuseLogic().Run(Msg("!defuse red"), host));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Contains((DefuseLogic.FlagKey, false), host.Base.FlagsFake.Calls);
        Assert.Equal("waddles.command-superpenguin-defuse", DefuseLogic.FlagKey);

        host.Base.Context = host.Base.Context with { ConfigJson = "{}" };
        host.Base.FlagsFake.Overrides[DefuseLogic.FlagKey] = true;
        Assert.NotNull(Bomb(0, 1500).Run(Msg("!defuse red"), host));
    }

    [Fact]
    public void a_second_defuse_within_the_cooldown_is_silently_dropped_and_users_do_not_share_one()
    {
        var host = Host();
        Assert.NotNull(Bomb(0, 1500).Run(Msg("!defuse red"), host));
        NextEvent(host, "evt-2");
        Assert.Null(Bomb(0, 1500).Run(Msg("!defuse red"), host));
        Assert.Single(host.EconomyFake.Wagers);

        NextEvent(host, "evt-3");
        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 1000);
        Assert.NotNull(Bomb(2).Run(Msg("!defuse red", "other"), host));
    }

    [Fact]
    public void redelivery_of_the_same_event_reprints_the_same_outcome_and_moves_money_once()
    {
        var host = Host(messageId: "evt-replay");
        var first = Text(Bomb(0, 1600).Run(Msg("!defuse red"), host));
        Assert.Equal(6100, host.EconomyFake.Balance(Actor));

        host.EconomyFake.BeginEvent("evt-replay");
        var replay = Text(Bomb(2).Run(Msg("!defuse red"), host));               // a re-draw WOULD explode

        Assert.Equal(first, replay);
        Assert.Equal(6100, host.EconomyFake.Balance(Actor));
        Assert.All(host.EconomyFake.Wagers, w => Assert.Equal((Actor, 500UL, 1600UL), w));
    }

    [Fact]
    public void a_replay_is_not_dropped_by_the_cooldown_the_first_delivery_took()
    {
        var host = Host(messageId: "evt-replay");
        Bomb(2).Run(Msg("!defuse red"), host);
        host.EconomyFake.BeginEvent("evt-replay");
        Assert.NotNull(new DefuseLogic().Run(Msg("!defuse red"), host));
    }

    [Fact]
    public void the_same_message_id_from_a_different_actor_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        var first = Text(Bomb(0, 1500).Run(Msg("!defuse red"), host));

        host.IdentityFake.Actor = Other;
        host.EconomyFake.Seed(Other, 1000);
        var second = Text(Bomb(2).Run(Msg("!defuse red", "other"), host));

        Assert.NotEqual(first, second);
        Assert.Contains($"{{user:{Other}}} cuts the red wire... BOOM!!!", second);
        Assert.Equal(500, host.EconomyFake.Balance(Other));
        Assert.Equal(Other, host.EconomyFake.Wagers[1].User);
    }

    [Fact]
    public void the_same_message_id_from_a_different_platform_or_wire_is_not_a_replay()
    {
        var host = Host(messageId: "evt-shared");
        Assert.NotNull(Bomb(0, 1500).Run(Msg("!defuse red"), host));

        // Validated like any new command: the first delivery's cooldown still holds, nothing is reprinted.
        Assert.Null(Bomb(2).Run(Msg("!defuse red", platform: "discord"), host));
        Assert.Null(Bomb(2).Run(Msg("!defuse blue"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData("")]
    [InlineData("  ")]
    public void an_invocation_without_a_message_id_is_refused_loudly_and_moves_no_money(string messageId)
    {
        var host = Host(messageId: messageId);
        var ex = Assert.Throws<InvalidOperationException>(() => Bomb(0, 1500).Run(Msg("!defuse red"), host));
        Assert.Contains("message id", ex.Message);
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Empty(host.Base.KvFake.SetCalls);
    }

    [Theory]
    [InlineData(EconomyErrorKind.InsufficientFunds)]
    [InlineData(EconomyErrorKind.OverCap)]
    public void a_refused_wager_replies_not_enough_and_hands_the_cooldown_back(EconomyErrorKind kind)
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(kind, "refused", 3);
        Assert.Equal("Sorry it costs 500 stars to defuse the bomb which you do not have.", Text(Bomb(0, 1500).Run(Msg("!defuse red"), host)));

        host.EconomyFake.ThrowOnWager = null;
        NextEvent(host, "evt-2");
        Assert.NotNull(Bomb(0, 1500).Run(Msg("!defuse red"), host));
    }

    [Theory]
    [InlineData(EconomyErrorKind.Denied)]
    [InlineData(EconomyErrorKind.Unavailable)]
    [InlineData(EconomyErrorKind.Backend)]
    [InlineData(EconomyErrorKind.NotAMember)]
    public void host_side_economy_failures_are_loud_never_a_silent_unpaid_bomb(EconomyErrorKind kind)
    {
        var host = Host();
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(kind, "host said no");
        var ex = Assert.Throws<WaddleEconomyException>(() => Bomb(0, 1500).Run(Msg("!defuse red"), host));
        Assert.Equal(kind, ex.Kind);
    }

    [Fact]
    public void a_retry_after_a_host_failure_reuses_the_recorded_draw()
    {
        var host = Host(messageId: "evt-retry");
        host.EconomyFake.ThrowOnWager = new WaddleEconomyException(EconomyErrorKind.Backend, "blip");
        Assert.Throws<WaddleEconomyException>(() => Bomb(0, 1400).Run(Msg("!defuse red"), host));

        host.EconomyFake.ThrowOnWager = null;
        host.EconomyFake.BeginEvent("evt-retry");
        Assert.Contains("awarded 1,400 stars", Text(Bomb(2).Run(Msg("!defuse red"), host)));
        Assert.Equal((Actor, 500UL, 1400UL), host.EconomyFake.Wagers.Last());
    }

    [Theory]
    [InlineData(IdentityErrorKind.NotLinked)]
    [InlineData(IdentityErrorKind.NotAMember)]
    public void an_unlinked_user_is_told_to_link_and_nothing_is_staked(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Contains("Link your account", Text(new DefuseLogic().Run(Msg("!defuse red"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Theory]
    [InlineData(IdentityErrorKind.Denied)]
    [InlineData(IdentityErrorKind.Unavailable)]
    public void other_identity_failures_are_loud(IdentityErrorKind kind)
    {
        var host = Host();
        host.IdentityFake.ThrowOnResolve = new WaddleIdentityException(kind, "x");
        Assert.Throws<WaddleIdentityException>(() => new DefuseLogic().Run(Msg("!defuse red"), host));
    }

    [Fact]
    public void a_tenant_wide_activation_without_a_community_refuses_loudly()
    {
        var host = Host();
        host.WithScope(null, "evt-1");
        Assert.Contains("needs a community", Text(new DefuseLogic().Run(Msg("!defuse red"), host)));
        Assert.Empty(host.EconomyFake.Wagers);
        Assert.Contains(host.Base.LogFake.Writes, w => w.Message == "defuse.refused");
    }

    [Fact]
    public void the_currency_alias_comes_from_the_economy_service_tool_and_falls_back_loudly()
    {
        var host = Host(alias: "gems");
        Assert.Contains("you lose 500 gems.", Text(Bomb(2).Run(Msg("!defuse red"), host)));
        Assert.Equal("points_view", host.ToolFake.Calls.First(c => c.Tool == "points_view").Tool);

        var missing = Host(alias: null);
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Bomb(2).Run(Msg("!defuse red"), missing)));
        Assert.Contains(missing.Base.LogFake.Writes, w => w.Message == "defuse.alias_unavailable");

        var blank = Host(alias: " ");
        Assert.Contains(EconomyServiceTools.DefaultAlias, Text(Bomb(2).Run(Msg("!defuse red"), blank)));
    }

    [Fact]
    public void a_denying_kv_fails_loud_instead_of_running_an_unguarded_money_game()
    {
        var host = Host();
        Assert.Throws<WaddleSdk.Kv.WaddleKvException>(() => new DefuseLogic().Run(Msg("!defuse red"), new DenyingKvHost(host)));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void logs_and_state_keys_never_carry_the_actor_the_user_uuid_or_the_event_id()
    {
        var host = Host();
        Bomb(0, 1500).Run(Msg("!defuse red", "secret-platform-id"), host);

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
    public void the_default_random_source_drives_a_full_defuse_without_a_script()
    {
        var host = Host();
        Assert.NotNull(new DefuseLogic().Run(Msg("!defuse red"), host));
        Assert.Single(host.EconomyFake.Wagers);
    }

    [Fact]
    public void custom_config_changes_wires_cost_multiplier_and_messages_and_accepts_multi_word_wires()
    {
        var host = Host(config: "{\"wires\":[\"dark red\",\"blue\"],\"cost\":100,\"win_multiplier\":5,\"success\":\"{Name} wins {Points} {PointType} with {Wire}\"}");
        var text = Text(Bomb(0, 520).Run(Msg("!defuse dark red"), host));

        Assert.Equal((Actor, 100UL, 520UL), Assert.Single(host.EconomyFake.Wagers));   // window 100*5 -/+ 100/5 = 480..520
        Assert.EndsWith($"{{user:{Actor}}} wins 520 stars with dark red Balance: 5,420 stars.", text);
    }

    [Fact]
    public void the_widest_allowed_multiplier_keeps_the_reward_within_the_host_payout_cap()
    {
        var host = Host(config: "{\"win_multiplier\":50,\"cost\":100}");
        Bomb(0, int.MaxValue).Run(Msg("!defuse red"), host);
        var wager = Assert.Single(host.EconomyFake.Wagers);
        Assert.Equal(100UL * 50 + 100 / 50, wager.Payout);
        Assert.True(wager.Payout <= wager.Stake * 100);
    }

    [Theory]
    [InlineData("{\"wires\":[\"only\"]}")]
    [InlineData("{\"wires\":[\"Red\",\"red\"]}")]
    [InlineData("{\"wires\":[\"a\",\" \"]}")]
    [InlineData("{\"wires\":null}")]
    [InlineData("{\"cost\":0}")]
    [InlineData("{\"cost\":10000001}")]
    [InlineData("{\"win_multiplier\":1}")]
    [InlineData("{\"win_multiplier\":51}")]
    [InlineData("{\"cooldown_seconds\":0}")]
    [InlineData("{\"starting\":\" \"}")]
    [InlineData("{\"failure\":null}")]
    [InlineData("null")]
    public void an_invalid_config_fails_loud_instead_of_running_with_surprising_odds(string json)
    {
        var host = Host(config: json);
        Assert.Throws<InvalidOperationException>(() => new DefuseLogic().Run(Msg("!defuse red"), host));
        Assert.Empty(host.EconomyFake.Wagers);
    }

    [Fact]
    public void a_config_that_is_not_json_fails_loud_and_a_blank_one_means_defaults()
    {
        Assert.ThrowsAny<JsonException>(() => DefuseConfig.Load("{not json"));
        var defaults = DefuseConfig.Load("");
        Assert.Equal(500, defaults.Cost);
        Assert.Equal(3, defaults.WinMultiplier);
        Assert.Equal(10u, defaults.CooldownSeconds);
        Assert.Equal(new[] { "red", "blue", "yellow" }, DefuseConfig.Load(" ").Wires);
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

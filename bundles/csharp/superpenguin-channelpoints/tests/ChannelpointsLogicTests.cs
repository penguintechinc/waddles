using System.Text;
using System.Text.Json;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinChannelpoints.Tests.World;

namespace WaddleBundleSuperpenguinChannelpoints.Tests;

public class ChannelpointsLogicTests
{
    private const string RewardId = "aaaaaaaa-1111-4111-8111-111111111111";
    private const string OtherRewardId = "bbbbbbbb-2222-4222-8222-222222222222";

    private static int _counter;

    private static string NextId() => "m" + Interlocked.Increment(ref _counter);

    private static World NewWorld()
    {
        var w = new World(ChannelpointsLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol })
        {
            w.Ledger.Seed(u, 0);
        }

        return w;
    }

    private static string Esc(string s) => s.Replace("\\", "\\\\").Replace("\"", "\\\"");

    private static PlatformEventInfo Chat(string text, bool mod) =>
        new("twitch", "chat.message", "platform-actor",
            "{\"text\":\"" + Esc(text) + "\",\"channel_id\":\"chan-1\"" + (mod ? ",\"is_mod\":true" : "") + "}", "2026-11-07T00:00:00.000Z");

    private static string? Say(World w, ChannelpointsLogic l, string actor, string text, bool mod = true)
    {
        var reply = l.Run(Chat(text, mod), w.HostFor(actor, NextId()));
        return reply is null ? null : TextOf(reply);
    }

    private static PlatformEventInfo Redemption(
        string id = "red-1", string status = "unfulfilled", string? rewardId = RewardId, string? title = "Hydrate", long cost = 300,
        string? input = null, string? login = "streamer", string? broadcasterId = "999", bool reward = true)
    {
        var sb = new StringBuilder("{\"id\":\"" + id + "\",\"status\":\"" + status + "\"");
        if (input is not null)
        {
            sb.Append(",\"user_input\":\"" + Esc(input) + "\"");
        }

        if (login is not null)
        {
            sb.Append(",\"broadcaster_user_login\":\"" + login + "\"");
        }

        if (broadcasterId is not null)
        {
            sb.Append(",\"broadcaster_user_id\":\"" + broadcasterId + "\"");
        }

        if (reward)
        {
            sb.Append(",\"reward\":{" + (rewardId is null ? "" : "\"id\":\"" + rewardId + "\",") + (title is null ? "" : "\"title\":\"" + Esc(title) + "\",") + "\"cost\":" + cost + "}");
        }

        sb.Append('}');
        return new PlatformEventInfo("twitch", ChannelpointsLogic.RedemptionEventType, "platform-actor", sb.ToString(), "2026-11-07T00:00:00.000Z");
    }

    private static PlatformEventInfo? Redeem(World w, ChannelpointsLogic l, PlatformEventInfo ev, string actor = Alice, string? messageId = null) =>
        l.Run(ev, w.HostFor(actor, messageId ?? NextId()));

    private static string Reply(PlatformEventInfo? reply) => TextOf(Assert.IsType<PlatformEventInfo>(reply));

    private static string ReplyChannel(PlatformEventInfo reply) =>
        reply.Payload(WaddleSdk.Json.WaddleSdkJsonContext.Default.ChatReplyPayload)!.ChannelId!;

    private static void MapTitle(World w, ChannelpointsLogic l, string steps, string reference = "Hydrate") =>
        Assert.StartsWith("Mapped", Say(w, l, Bob, $"!redeem map {reference} {steps}"));

    [Fact]
    public void a_moderator_maps_a_reward_and_it_appears_in_the_list()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();

        var reply = Say(w, l, Bob, "!redeem map \"Hydrate now\" award 25 && say Thanks {user}!");
        Assert.Equal("Mapped \"Hydrate now\": award 25 + say \"Thanks {user}!\".", reply);

        Assert.Equal("1 mapped: Hydrate now [on]: award 25 + say \"Thanks {user}!\"", Say(w, l, Bob, "!redeem list"));
    }

    [Fact]
    public void remapping_a_reward_replaces_the_row_and_the_list_caps_its_output()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 5");
        MapTitle(w, l, "award 6");
        Assert.Equal("1 mapped: Hydrate [on]: award 6", Say(w, l, Bob, "!redeem list"));

        for (var i = 0; i < 11; i++)
        {
            MapTitle(w, l, "say " + new string('x', 150), $"Reward{i}");
        }

        var list = Say(w, l, Bob, "!redeem list")!;
        Assert.StartsWith("12 mapped:", list);
        Assert.True(list.Length <= ChannelpointsLogic.MaxReply);
    }

    [Fact]
    public void the_list_shows_eight_rows_and_counts_the_rest()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        for (var i = 0; i < 10; i++)
        {
            MapTitle(w, l, "award 1", $"S{i}");
        }

        var list = Say(w, l, Bob, "!redeem list")!;

        Assert.StartsWith("10 mapped: S0 [on]: award 1; S1", list);
        Assert.EndsWith("(+2 more)", list);
    }

    [Fact]
    public void management_is_moderator_only_and_needs_a_community()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();

        Assert.Equal(ChannelpointsLogic.ModOnly, Say(w, l, Alice, "!redeem map Hydrate award 5", mod: false));
        Assert.Contains("needs a community", TextOf(l.Run(Chat("!redeem list", true), w.HostFor(Bob, NextId(), community: null))!));
        Assert.Equal("No rewards are mapped. Use !redeem map.", Say(w, l, Bob, "!redeem list"));
    }

    [Theory]
    [InlineData("!redeem")]
    [InlineData("!redeem frobnicate")]
    [InlineData("!redeem map")]
    [InlineData("!redeem map \"unterminated award 5")]
    [InlineData("!redeem unmap")]
    [InlineData("!redeem enable")]
    [InlineData("!redeem disable")]
    public void malformed_management_commands_get_the_usage_line(string text)
    {
        var w = NewWorld();

        Assert.Equal(ChannelpointsLogic.Usage, Say(w, new ChannelpointsLogic(), Bob, text));
    }

    [Fact]
    public void bad_actions_are_refused_without_writing()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();

        Assert.Contains("award needs a whole amount", Say(w, l, Bob, "!redeem map Hydrate award 5000"));
        Assert.Equal("No rewards are mapped. Use !redeem map.", Say(w, l, Bob, "!redeem list"));
    }

    [Fact]
    public void enable_disable_and_unmap_edit_the_row_or_say_it_is_missing()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();

        Assert.Equal("\"Hydrate\" is not mapped.", Say(w, l, Bob, "!redeem enable Hydrate"));
        MapTitle(w, l, "award 5");
        Assert.Equal("\"Hydrate\" is now disabled.", Say(w, l, Bob, "!redeem disable hydrate"));
        Assert.Contains("[off]", Say(w, l, Bob, "!redeem list"));
        Assert.Equal("\"Hydrate\" is now enabled.", Say(w, l, Bob, "!redeem enable Hydrate"));
        Assert.Contains("[on]", Say(w, l, Bob, "!redeem list"));
        Assert.Equal("Unmapped \"Hydrate\".", Say(w, l, Bob, "!redeem unmap Hydrate"));
        Assert.Equal("\"Hydrate\" is not mapped.", Say(w, l, Bob, "!redeem unmap Hydrate"));
    }

    [Fact]
    public void the_table_holds_at_most_one_hundred_rows()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        for (var i = 0; i < ChannelpointsLogic.MaxMappings; i++)
        {
            MapTitle(w, l, "award 1", $"R{i}");
        }

        Assert.Contains("table is full", Say(w, l, Bob, "!redeem map OneMore award 1"));
        Assert.StartsWith("Mapped", Say(w, l, Bob, "!redeem map R5 award 2"));
    }

    [Fact]
    public void a_table_over_the_document_budget_is_refused_and_not_written()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        var big = string.Join(" && ", Enumerable.Repeat("say " + new string('q', 195), 3));
        string? refusal = null;
        for (var i = 0; i < ChannelpointsLogic.MaxMappings && refusal is null; i++)
        {
            var reply = Say(w, l, Bob, $"!redeem map Big{i} {big}")!;
            refusal = reply.Contains("too large", StringComparison.Ordinal) ? reply : null;
        }

        Assert.NotNull(refusal);
        Assert.True(w.Kv.Get("cp.map")!.Length <= ChannelpointsLogic.MaxDocBytes);
    }

    [Fact]
    public void a_corrupt_or_empty_table_fails_loudly_instead_of_being_replaced()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        w.Kv.Set("cp.map", Encoding.UTF8.GetBytes("garbage"), 0);
        Assert.Throws<InvalidOperationException>(() => Say(w, l, Bob, "!redeem list"));
        Assert.Throws<InvalidOperationException>(() => Redeem(w, l, Redemption()));

        w.Kv.Set("cp.map", Encoding.UTF8.GetBytes("null"), 0);
        Assert.Throws<InvalidOperationException>(() => Say(w, l, Bob, "!redeem list"));
    }

    [Fact]
    public void a_redemption_runs_award_say_and_alert_in_one_pass()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 25 && say {user} redeemed {reward} for {cost} ({amount} {currency}): {input} && alert Hydrate time!");

        var reply = Redeem(w, l, Redemption(input: "water"));

        Assert.Equal($"{{user:{Alice}}} redeemed Hydrate for 300 (25 stars): water", Reply(reply));
        Assert.Equal("streamer", ReplyChannel(reply!));
        Assert.Equal([("credit", Alice, 25UL)], w.Ledger.Moved);
        var push = Assert.Single(w.Pushed.Pushes);
        Assert.Equal(("superpenguin.channelpoints", OverlaySurface.AlertBox), (push.WidgetId, push.Surface));
        using var doc = JsonDocument.Parse(push.Payload);
        var alert = doc.RootElement.GetProperty("alert");
        Assert.Equal("superpenguin.channelpoints.redeem", alert.GetProperty("alert_type").GetString());
        Assert.Equal(Alice, alert.GetProperty("user").GetString());
        Assert.Equal("Hydrate time!", alert.GetProperty("message").GetString());
        var amount = alert.GetProperty("amount");
        Assert.Equal(("Hydrate", 300L, "water"), (amount.GetProperty("reward").GetString(), amount.GetProperty("cost").GetInt64(), amount.GetProperty("input").GetString()));
    }

    [Fact]
    public void the_alert_omits_empty_input_and_message_and_a_say_free_mapping_stays_silent_in_chat()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "alert {user}");

        Assert.Null(Redeem(w, l, Redemption()));

        using var doc = JsonDocument.Parse(Assert.Single(w.Pushed.Pushes).Payload);
        var alert = doc.RootElement.GetProperty("alert");
        Assert.False(alert.GetProperty("amount").TryGetProperty("input", out _));
        Assert.False(alert.TryGetProperty("message", out _));
    }

    [Fact]
    public void the_reward_id_outranks_the_title_and_a_title_match_works_without_an_id_mapping()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "say by title");
        Assert.Equal("by title", Reply(Redeem(w, l, Redemption(id: "r1", rewardId: OtherRewardId))));
        Assert.Equal("by title", Reply(Redeem(w, l, Redemption(id: "r2", rewardId: "not-a-guid"))));
        Assert.Equal("by title", Reply(Redeem(w, l, Redemption(id: "r3", rewardId: null, title: "HYDRATE"))));

        MapTitle(w, l, "say by id", RewardId);
        Assert.Equal("by id", Reply(Redeem(w, l, Redemption(id: "r4"))));
    }

    [Fact]
    public void non_actionable_redemptions_are_ignored_with_a_counted_log_line()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 5");

        Assert.Null(Redeem(w, l, Redemption(id: "a", status: "fulfilled")));
        Assert.Null(Redeem(w, l, Redemption(id: "b", title: "Unmapped", rewardId: OtherRewardId)));
        Assert.Null(Redeem(w, l, Redemption(id: "c", title: "Unmapped", rewardId: OtherRewardId)));
        Assert.Null(Redeem(w, l, Redemption(id: "d", reward: false)));
        Assert.Null(Redeem(w, l, Redemption(id: "e", rewardId: null, title: null)));
        Say(w, l, Bob, "!redeem disable Hydrate");
        Assert.Null(Redeem(w, l, Redemption(id: "f")));

        Assert.Empty(w.Ledger.Moved);
        var reasons = w.Log.Writes.Where(x => x.Message == "cp.ignored").Select(x => x.FieldsJson).ToList();
        Assert.Contains(reasons, f => f.Contains("status"));
        Assert.Contains(reasons, f => f.Contains("disabled"));
        Assert.Contains(reasons, f => f.Contains("unmapped") && f.Contains("\"count\":4"));
    }

    [Fact]
    public void an_unlinked_redeemer_is_skipped_and_nothing_is_recorded()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 5");
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Null(Redeem(w, l, Redemption(id: "x1")));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Null(Redeem(w, l, Redemption(id: "x1")));
        Assert.Empty(w.Ledger.Moved);

        w.ActorFailure = null;
        Redeem(w, l, Redemption(id: "x1"));
        Assert.Single(w.Ledger.Moved);
    }

    [Fact]
    public void other_identity_failures_propagate()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 5");
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");

        Assert.Throws<WaddleIdentityException>(() => Redeem(w, l, Redemption()));
    }

    [Fact]
    public void malformed_events_are_ignored_loudly()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        var bad = new PlatformEventInfo("twitch", ChannelpointsLogic.RedemptionEventType, "p", "not json", "t");
        var blank = new PlatformEventInfo("twitch", ChannelpointsLogic.RedemptionEventType, "p", "{\"status\":\"unfulfilled\"}", "t");
        var nullBody = new PlatformEventInfo("twitch", ChannelpointsLogic.RedemptionEventType, "p", "null", "t");

        Assert.Null(Redeem(w, l, bad));
        Assert.Null(Redeem(w, l, blank));
        Assert.Null(Redeem(w, l, nullBody));
        Assert.Equal(3, w.Log.Writes.Count(x => x.Level == WaddleLogLevel.Warn && x.FieldsJson.Contains("bad_event")));
    }

    [Fact]
    public void a_redemption_without_a_community_is_ignored()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();

        Assert.Null(l.Run(Redemption(), w.HostFor(Alice, NextId(), community: null)));
        Assert.Contains(w.Log.Writes, x => x.FieldsJson.Contains("no_community"));
    }

    [Fact]
    public void everything_is_silent_while_the_flag_is_off()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 5");
        w.Flags.Overrides[ChannelpointsLogic.FlagKey] = false;

        Assert.Null(Redeem(w, l, Redemption()));
        Assert.Null(Say(w, l, Bob, "!redeem list"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void other_chat_commands_and_plain_chat_are_ignored()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();

        Assert.Null(Say(w, l, Bob, "!roll"));
        Assert.Null(Say(w, l, Bob, "hello there"));
    }

    [Fact]
    public void a_redelivered_redemption_repeats_its_reply_and_credits_once_even_if_the_mapping_changed()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 10 && say {user} +{amount}");
        var first = Reply(Redeem(w, l, Redemption(id: "dup"), messageId: "evt-1"));
        Say(w, l, Bob, "!redeem unmap Hydrate");

        var again = Reply(Redeem(w, l, Redemption(id: "dup"), messageId: "evt-1"));

        Assert.Equal(first, again);
        Assert.Single(w.Ledger.Moved);
        Assert.Equal(10, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void a_mint_cap_or_departed_member_skips_the_award_but_the_reply_still_goes_out()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 10 && say thanks");
        w.Ledger.MintCap = 5;
        Assert.Equal("thanks", Reply(Redeem(w, l, Redemption(id: "cap"))));

        w.Ledger.MintCap = ulong.MaxValue;
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.NotAMember, "gone");
        Assert.Equal("thanks", Reply(Redeem(w, l, Redemption(id: "gone"))));

        Assert.Empty(w.Ledger.Moved);
        Assert.Contains(w.Log.Writes, x => x.FieldsJson.Contains("over_cap"));
        Assert.Contains(w.Log.Writes, x => x.FieldsJson.Contains("not_a_member"));
    }

    [Fact]
    public void an_unavailable_economy_fails_the_redemption_loudly()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 10");
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "down");

        Assert.Throws<WaddleEconomyException>(() => Redeem(w, l, Redemption()));
    }

    [Fact]
    public void a_refused_overlay_push_is_not_swallowed()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "alert hi");
        w.Overlay = new RefusingOverlay();

        var ex = Assert.Throws<WaddleTransportException>(() => Redeem(w, l, Redemption()));

        Assert.Equal("NOT_IMPLEMENTED", ex.Error.Code);
    }

    [Fact]
    public void viewer_input_cannot_forge_a_placeholder_or_a_command()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "say {input}");

        var text = Reply(Redeem(w, l, Redemption(input: "!ban {user:" + Bob + "} @everyone")));

        Assert.DoesNotContain("{", text);
        Assert.DoesNotContain("@", text);
        Assert.False(text.StartsWith('!'));
        Assert.Contains("ban", text);
    }

    [Fact]
    public void the_chat_channel_falls_back_to_the_broadcaster_id_and_a_missing_channel_skips_the_reply_loudly()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "say hi");

        Assert.Equal("999", ReplyChannel(Redeem(w, l, Redemption(id: "c1", login: null))!));
        Assert.Equal("999", ReplyChannel(Redeem(w, l, Redemption(id: "c2", login: " "))!));
        Assert.Null(Redeem(w, l, Redemption(id: "c3", login: null, broadcasterId: null)));
        Assert.Contains(w.Log.Writes, x => x.Level == WaddleLogLevel.Warn && x.FieldsJson.Contains("no_channel"));
    }

    [Fact]
    public void a_long_reply_is_cut_to_the_chat_limit()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        var say = "say " + new string('a', 190);
        MapTitle(w, l, $"{say} && {say} && {say}");

        Assert.Equal(ChannelpointsLogic.MaxReply, Reply(Redeem(w, l, Redemption())).Length);
    }

    [Fact]
    public void the_currency_alias_is_fetched_only_when_a_template_uses_it()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "say plain");
        Redeem(w, l, Redemption(id: "p1"));
        Assert.DoesNotContain(w.Tools.Calls, c => c.Tool == "points_view");

        MapTitle(w, l, "say {currency}");
        Assert.Equal("stars", Reply(Redeem(w, l, Redemption(id: "p2"))));
        Assert.Contains(w.Tools.Calls, c => c.Tool == "points_view");
    }

    [Fact]
    public void the_award_key_is_stable_per_redemption_not_per_delivery()
    {
        var w = NewWorld();
        var l = new ChannelpointsLogic();
        MapTitle(w, l, "award 3");
        Redeem(w, l, Redemption(id: "k1"), messageId: "evt-a");
        Redeem(w, l, Redemption(id: "k1"), messageId: "evt-b");

        var keys = w.Tools.Calls.Where(c => c.Tool == "adjust_balance").Select(c => c.Key).Distinct().ToList();
        Assert.Single(keys);
        // The SDK's documented retry key: hash of redemption id, operation, verb and user.
        Assert.Equal(StateKey.Hash("k1\u001fcp\u001fcredit\u001f" + Alice), keys[0]);
    }

    /// <summary>An overlay whose host capability answers not-implemented, as the stage does today.</summary>
    private sealed class RefusingOverlay : IOverlayClient
    {
        public TransportResultInfo RegisterWidget(OverlayWidget widget) => throw Refusal();

        public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson) => throw Refusal();

        private static WaddleTransportException Refusal() => new(TransportErrorInfo.Fatal("NOT_IMPLEMENTED", "overlay host capability has no stage-side handler yet"));
    }
}

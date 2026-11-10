using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinBonustickets.Tests.World;

namespace WaddleBundleSuperpenguinBonustickets.Tests;

public class BonusticketsLogicTests
{
    private static World NewWorld()
    {
        var w = new World(BonusticketsLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave })
        {
            w.Ledger.Seed(u, 0);
        }

        return w;
    }

    private static PlatformEventInfo Ev(string text, bool mod = false, bool bot = false, bool channel = true)
    {
        var json = "{\"text\":\"" + text + "\"" + (channel ? ",\"channel_id\":\"chan-1\"" : "") + (mod ? ",\"is_mod\":true" : "") + (bot ? ",\"is_bot\":true" : "") + "}";
        return new PlatformEventInfo("twitch", "chat.message", "platform-actor", json, "2026-11-07T00:00:00.000Z");
    }

    private static string? Run(World w, BonusticketsLogic l, string actor, string text, string id, bool mod = false, bool bot = false, bool channel = true)
    {
        var reply = l.Run(Ev(text, mod, bot, channel), w.HostFor(actor, id));
        return reply is null ? null : TextOf(reply);
    }

    [Fact]
    public void a_bonus_claim_credits_the_scripted_award_once_through_the_economy_service()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic(new ScriptedRandom(37).Next);

        var text = Run(w, l, Alice, "!bonus", "m1");

        Assert.Equal($"{{user:{Alice}}} just got 37 bonus stars and now has 37 stars.", text);
        Assert.Equal([("credit", Alice, 37UL)], w.Ledger.Moved);
    }

    [Fact]
    public void the_default_roll_stays_inside_the_documented_range()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();

        Run(w, l, Alice, "!bonus", "m1");

        var award = (long)w.Ledger.Moved[0].Amount;
        Assert.InRange(award, BonusticketsLogic.MinAward, BonusticketsLogic.MaxAward);
    }

    [Fact]
    public void a_second_claim_the_same_day_is_ignored_and_the_next_day_pays_again()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic(new ScriptedRandom(30, 40).Next);
        Run(w, l, Alice, "!bonus", "m1");

        Assert.Null(Run(w, l, Alice, "!bonus", "m2"));
        Assert.Single(w.Ledger.Moved);

        w.Clock.Now += 24UL * 60 * 60 * 1000;
        Assert.Contains("40 bonus", Run(w, l, Alice, "!bonus", "m3"));
        Assert.Equal(2, w.Ledger.Moved.Count);
    }

    [Fact]
    public void a_redelivered_claim_reprints_the_same_award_and_moves_nothing_more()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic(new ScriptedRandom(33, 44).Next);
        var first = Run(w, l, Alice, "!bonus", "m1");

        var again = Run(w, l, Alice, "!bonus", "m1");

        Assert.Equal(first, again);
        Assert.Single(w.Ledger.Moved);
        Assert.Equal(33, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void bonus_refuses_without_a_community_or_a_linked_account()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();

        var noCommunity = l.Run(Ev("!bonus"), w.HostFor(Alice, "m1", community: null));
        Assert.Contains("needs a community", TextOf(noCommunity!));

        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, l, Alice, "!bonus", "m2"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Run(w, l, Alice, "!bonus", "m3"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void other_identity_failures_propagate()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");

        Assert.Throws<WaddleIdentityException>(() => Run(w, new BonusticketsLogic(), Alice, "!bonus", "m1"));
    }

    [Fact]
    public void a_mint_cap_refusal_replies_and_releases_the_claim()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic(new ScriptedRandom(30, 31).Next);
        w.Ledger.MintCap = 10;

        Assert.Contains("bonus limit was reached", Run(w, l, Alice, "!bonus", "m1"));
        Assert.Empty(w.Ledger.Moved);

        w.Ledger.MintCap = ulong.MaxValue;
        Assert.Contains("31 bonus", Run(w, l, Alice, "!bonus", "m2"));
    }

    [Fact]
    public void other_economy_failures_propagate_and_a_blank_message_id_is_refused()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "down");
        Assert.Throws<WaddleEconomyException>(() => Run(w, new BonusticketsLogic(), Alice, "!bonus", "m1"));

        var w2 = NewWorld();
        Assert.Throws<InvalidOperationException>(() => Run(w2, new BonusticketsLogic(), Alice, "!bonus", " "));
    }

    [Fact]
    public void everything_is_silent_while_the_flag_is_off()
    {
        var w = NewWorld();
        w.Flags.Overrides[BonusticketsLogic.FlagKey] = false;
        var l = new BonusticketsLogic();

        Assert.Null(Run(w, l, Alice, "!bonus", "m1"));
        Assert.Null(Run(w, l, Alice, "hello", "m2"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void addactive_is_moderator_only_and_validates_its_argument()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();

        Assert.Equal(BonusticketsLogic.ModOnly, Run(w, l, Alice, "!addactive 10", "m1"));
        Assert.Equal("Usage: !addactive <amount>", Run(w, l, Alice, "!addactive", "m2", mod: true));
        Assert.Equal("Usage: !addactive <amount>", Run(w, l, Alice, "!addactive abc", "m3", mod: true));
        Assert.Equal("Usage: !addactive <amount>", Run(w, l, Alice, "!addactive 0", "m4", mod: true));
        Assert.Equal("Usage: !addactive <amount>", Run(w, l, Alice, "!addactive 5 6", "m5", mod: true));
        Assert.Contains("Queued 10 stars", Run(w, l, Alice, "!addactive 10", "m6", mod: true));
    }

    [Fact]
    public void addactive_needs_a_linked_moderator()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");

        Assert.Contains("Link your account", Run(w, new BonusticketsLogic(), Alice, "!addactive 10", "m1", mod: true));
    }

    [Fact]
    public void the_pool_pays_every_active_chatter_after_the_quiet_period_and_clamps_the_amount()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        Run(w, l, Bob, "hi", "a2");
        Run(w, l, Carol, "hi", "a3", bot: true);
        Run(w, l, Dave, "!addactive 500", "a4", mod: true);

        Assert.Null(Run(w, l, Bob, "still here", "a5"));
        Assert.Empty(w.Ledger.Moved);

        w.Clock.Now += 6_000;
        var text = Run(w, l, Alice, "later", "a6");

        Assert.Equal("Sending 100 stars to 2 active users.", text);
        Assert.Equal(2, w.Ledger.Moved.Count);
        Assert.All(w.Ledger.Moved, m => Assert.Equal(("credit", 100UL), (m.Kind, m.Amount)));
        Assert.DoesNotContain(w.Ledger.Moved, m => m.User == Carol);
        Assert.Null(Run(w, l, Alice, "again", "a7"));
        Assert.Equal(2, w.Ledger.Moved.Count);
    }

    [Fact]
    public void adds_accumulate_and_a_replayed_add_counts_once()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        Run(w, l, Dave, "!addactive 10", "a2", mod: true);
        Run(w, l, Dave, "!addactive 10", "a2", mod: true);
        Run(w, l, Dave, "!addactive 15", "a3", mod: true);

        w.Clock.Now += 6_000;
        Assert.Equal("Sending 25 stars to 1 active users.", Run(w, l, Alice, "x", "a4"));
    }

    [Fact]
    public void a_large_payout_is_drained_in_chunks_across_events()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        for (var i = 0; i < 25; i++)
        {
            Run(w, l, $"user-{i}", "hi", $"c{i}");
        }

        Run(w, l, Dave, "!addactive 5", "add", mod: true);
        w.Clock.Now += 6_000;

        Assert.Equal("Sending 5 stars to 25 active users.", Run(w, l, "user-0", "go", "p1"));
        Assert.Equal(BonusticketsLogic.ChunkSize, w.Ledger.Moved.Count);
        Assert.Null(Run(w, l, "user-1", "go", "p2"));
        Assert.Equal(25, w.Ledger.Moved.Count);
        Assert.Null(Run(w, l, "user-2", "go", "p3"));
        Assert.Equal(25, w.Ledger.Moved.Count);
        Assert.Equal(25, w.Ledger.Moved.Select(m => m.User).Distinct().Count());
    }

    [Fact]
    public void a_redelivered_settlement_event_reprints_the_announcement_and_pays_nobody_twice()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        Run(w, l, Dave, "!addactive 20", "a2", mod: true);
        w.Clock.Now += 6_000;
        var first = Run(w, l, Alice, "go", "settle");
        var paid = w.Ledger.Moved.Count;

        var again = Run(w, l, Alice, "go", "settle");

        Assert.Equal(first, again);
        Assert.Equal(paid, w.Ledger.Moved.Count);
    }

    [Fact]
    public void with_nobody_active_the_pool_is_cleared_and_says_so()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Dave, "!addactive 10", "a1", mod: true);
        w.Clock.Now += 6_000;

        var text = Run(w, l, Alice, "beep", "a2", bot: true);

        Assert.Equal("No active users to send 10 stars to.", text);
        Assert.Empty(w.Ledger.Moved);
        Assert.Null(Run(w, l, Alice, "beep", "a3", bot: true));
    }

    [Fact]
    public void a_recipient_who_left_or_hit_a_cap_is_skipped_loudly_while_others_are_paid()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        Run(w, l, Bob, "hi", "a2");
        Run(w, l, Dave, "!addactive 10", "a3", mod: true);
        w.Clock.Now += 6_000;
        w.Ledger.BeforeCall = (_, ordinal) =>
        {
            if (ordinal == 0)
            {
                throw new WaddleEconomyException(EconomyErrorKind.NotAMember, "gone");
            }

            if (ordinal == 1)
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "cap");
            }
        };

        Run(w, l, Alice, "go", "settle");

        Assert.Empty(w.Ledger.Moved);
        Assert.Contains(w.Log.Writes, x => x.Level == WaddleLogLevel.Warn && x.FieldsJson.Contains("not_a_member"));
        Assert.Contains(w.Log.Writes, x => x.Level == WaddleLogLevel.Warn && x.FieldsJson.Contains("over_cap"));
    }

    [Fact]
    public void an_unavailable_economy_fails_the_settlement_loudly()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        Run(w, l, Dave, "!addactive 10", "a2", mod: true);
        w.Clock.Now += 6_000;
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "down");

        Assert.Throws<WaddleEconomyException>(() => Run(w, l, Alice, "go", "settle"));
    }

    [Fact]
    public void a_lost_start_race_pays_nothing_and_a_missing_channel_defers_the_payout()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        Run(w, l, Dave, "!addactive 10", "a2", mod: true);
        w.Clock.Now += 6_000;

        Assert.Null(Run(w, l, Alice, "no channel", "x1", channel: false));
        Assert.Empty(w.Ledger.Moved);

        new WaddleSdk.State.KvStateStore(w.Kv).TryClaim("bonus.start.0", 60);
        Assert.Null(Run(w, l, Alice, "raced", "x2"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void passive_chat_without_a_community_a_link_or_valid_json_is_ignored()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();

        Assert.Null(l.Run(Ev("hi"), w.HostFor(Alice, "m1", community: null)));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Null(Run(w, l, Alice, "hi", "m2"));
        w.ActorFailure = null;
        Assert.Null(l.Run(new PlatformEventInfo("twitch", "chat.message", "p", "not json", "t"), w.HostFor(Alice, "m3")));
    }

    [Fact]
    public void a_blank_message_id_is_refused_when_a_settlement_could_run()
    {
        var w = NewWorld();

        Assert.Throws<InvalidOperationException>(() => Run(w, new BonusticketsLogic(), Alice, "hi", " "));
    }

    [Fact]
    public void the_roster_is_rewritten_only_after_the_refresh_interval()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        var writes = w.Kv.SetCalls.Count;

        Run(w, l, Alice, "hi", "a2");
        Assert.Equal(writes, w.Kv.SetCalls.Count);

        w.Clock.Now += (BonusticketsLogic.RosterRefreshSeconds + 1) * 1000;
        Run(w, l, Alice, "hi", "a3");
        Assert.True(w.Kv.SetCalls.Count > writes);
    }

    [Fact]
    public void chatters_older_than_the_window_are_not_paid()
    {
        var w = NewWorld();
        var l = new BonusticketsLogic();
        Run(w, l, Alice, "hi", "a1");
        w.Clock.Now += (BonusticketsLogic.ActiveWindowSeconds + 1) * 1000;
        Run(w, l, Bob, "hi", "a2");
        Run(w, l, Dave, "!addactive 10", "a3", mod: true);
        w.Clock.Now += 6_000;

        Assert.Equal("Sending 10 stars to 1 active users.", Run(w, l, Bob, "go", "a4"));
        Assert.Equal([("credit", Bob, 10UL)], w.Ledger.Moved);
    }
}

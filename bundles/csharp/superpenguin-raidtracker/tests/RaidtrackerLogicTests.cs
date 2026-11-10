using WaddleSdk.Identity;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinRaidtracker.Tests.World;

namespace WaddleBundleSuperpenguinRaidtracker.Tests;

public class RaidtrackerLogicTests
{
    private static World NewWorld() => new(RaidtrackerLogic.FlagKey);

    private static PlatformEventInfo Raid(string? actor, string payload = "{\"metadata\":{\"viewers\":25}}") =>
        new("twitch", "channel.raid", actor, payload, "2026-11-07T00:00:00.000Z");

    private static void RaidFrom(World w, string actorToken, string messageId, string payload = "{\"metadata\":{\"viewers\":25}}") =>
        Assert.Null(new RaidtrackerLogic().Run(Raid(actorToken, payload), w.HostFor(Alice, messageId)));

    [Fact]
    public void incoming_raids_accumulate_per_raider_and_are_listed_by_count()
    {
        var w = NewWorld();
        var l = new RaidtrackerLogic();
        RaidFrom(w, "{user:raider-a}", "e1");
        RaidFrom(w, "{user:raider-b}", "e2", "{\"metadata\":{\"viewers\":100}}");
        RaidFrom(w, "{user:raider-a}", "e3", "{\"metadata\":{\"viewers\":5}}");

        Assert.Equal("Top raiders: 1. {user:raider-a} 2 raids (30 viewers), 2. {user:raider-b} 1 raids (100 viewers)", w.Say(l, Carol, "!raids", "r1"));
    }

    [Fact]
    public void a_redelivered_raid_counts_once()
    {
        var w = NewWorld();
        RaidFrom(w, "{user:raider-a}", "same");
        RaidFrom(w, "{user:raider-a}", "same");

        Assert.Contains("1 raids (25 viewers)", w.Say(new RaidtrackerLogic(), Carol, "!raids", "r1"));
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("not json")]
    [InlineData("{\"metadata\":{}}")]
    [InlineData("{\"metadata\":{\"viewers\":-4}}")]
    public void a_missing_or_negative_viewer_count_is_zero(string payload)
    {
        var w = NewWorld();
        RaidFrom(w, "{user:raider-a}", "e1", payload);

        Assert.Contains("1 raids (0 viewers)", w.Say(new RaidtrackerLogic(), Carol, "!raids", "r1"));
    }

    [Fact]
    public void a_huge_viewer_count_is_clamped()
    {
        var w = NewWorld();
        RaidFrom(w, "{user:raider-a}", "e1", "{\"metadata\":{\"viewers\":999999999999}}");

        Assert.Contains($"({RaidtrackerLogic.MaxViewers} viewers)", w.Say(new RaidtrackerLogic(), Carol, "!raids", "r1"));
    }

    [Fact]
    public void a_raider_without_a_token_falls_back_to_the_host_identity()
    {
        var w = NewWorld();
        Assert.Null(new RaidtrackerLogic().Run(Raid("plain-login"), w.HostFor(Dave, "e1")));

        Assert.Contains($"{{user:{Dave}}} 1 raids", w.Say(new RaidtrackerLogic(), Carol, "!raids", "r1"));
    }

    [Fact]
    public void an_unattributable_raid_is_tallied_not_dropped_silently()
    {
        var w = NewWorld();
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Null(new RaidtrackerLogic().Run(Raid(null), w.HostFor(Dave, "e1")));
        w.ActorFailure = null;

        Assert.Equal("No attributed raids yet (1 from unlinked raiders).", w.Say(new RaidtrackerLogic(), Carol, "!raids", "r1"));
    }

    [Fact]
    public void an_empty_history_says_so()
    {
        Assert.Equal("No incoming raids recorded yet.", NewWorld().Say(new RaidtrackerLogic(), Carol, "!raids", "r1"));
    }

    [Fact]
    public void only_the_fifty_most_recent_raiders_are_kept_and_only_five_are_listed()
    {
        var w = NewWorld();
        for (var i = 0; i < RaidtrackerLogic.MaxTracked + 3; i++)
        {
            RaidFrom(w, $"{{user:r{i}}}", $"e{i}");
        }

        var reply = w.Say(new RaidtrackerLogic(), Carol, "!raids", "r1")!;
        Assert.Equal(RaidtrackerLogic.TopCount, reply.Split(", ").Length);
        Assert.Null(w.Kv.Get(RaidtrackerLogic.RecordKey("r0")));
        Assert.NotNull(w.Kv.Get(RaidtrackerLogic.RecordKey("r52")));
    }

    [Fact]
    public void a_raid_without_a_message_id_is_refused_loudly()
    {
        var w = NewWorld();
        Assert.Throws<InvalidOperationException>(() => new RaidtrackerLogic().Run(Raid("{user:a}"), w.HostFor(Alice, " ")));
    }

    [Fact]
    public void outgoing_raid_is_refused_loudly_for_the_broadcaster_and_ignored_for_others()
    {
        var w = NewWorld();
        var l = new RaidtrackerLogic();

        Assert.Equal(RaidtrackerLogic.RaidRefusal, w.Say(l, Bob, "!raid somestreamer", "m1", broadcaster: true));
        Assert.Null(w.Say(l, Bob, "!raid somestreamer", "m2", mod: true));
        Assert.Null(w.Say(l, Alice, "!raid somestreamer", "m3"));
        Assert.Null(w.Say(l, Alice, "!other", "m4"));
        Assert.Null(w.Say(l, Alice, "chat", "m5"));
    }

    [Theory]
    [InlineData("{user:abc-1.2:x}", true, "abc-1.2:x")]
    [InlineData("  {user:abc}  ", true, "abc")]
    [InlineData("{user:}", false, "")]
    [InlineData("{user:a b}", false, "")]
    [InlineData("user:abc", false, "")]
    [InlineData("{user:abc", false, "")]
    [InlineData(null, false, "")]
    public void the_user_token_parser_is_strict(string? actor, bool ok, string id)
    {
        Assert.Equal(ok, RaidtrackerLogic.TryUserToken(actor, out var parsed));
        Assert.Equal(id, parsed);
    }

    [Fact]
    public void flag_off_and_no_community_are_ignored()
    {
        var w = NewWorld();
        var l = new RaidtrackerLogic();
        Assert.Null(l.Run(Raid("{user:a}"), w.HostFor(Alice, "e1", community: null)));
        w.Flags.Overrides[RaidtrackerLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!raids", "m1"));
        Assert.Null(w.Kv.Get(RaidtrackerLogic.IndexKey));
    }
}

using System.Text;
using WaddleSdk.Economy;
using WaddleSdk.Tool;
using Xunit;
using static WaddleBundleSuperpenguinTop.Tests.World;

namespace WaddleBundleSuperpenguinTop.Tests;

public class TopLogicTests
{
    private static World NewWorld() => new(TopLogic.FlagKey);

    private static void Leaders(World w, string json) =>
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_leaders")] = _ => Encoding.UTF8.GetBytes(json);

    private static void Board(World w, string json) =>
        w.Tools.Handlers[(TopLogic.ReputationTarget, "leaderboard")] = _ => Encoding.UTF8.GetBytes(json);

    [Fact]
    public void top10_lists_the_economy_leaders_with_grouped_numbers_and_requests_ten()
    {
        var w = NewWorld();
        Leaders(w, $"{{\"entries\":[{{\"user\":\"{Alice}\",\"balance\":12345}},{{\"user\":\"{Bob}\",\"balance\":7}}],\"alias\":\"stars\"}}");

        var reply = w.Say(new TopLogic(), Carol, "!top10", "m1");

        Assert.Equal($"Top 10 in stars: 1. {{user:{Alice}}} 12,345, 2. {{user:{Bob}}} 7", reply);
        Assert.Equal(("waddles.core.service.economy", "points_leaders", "{\"limit\":10}", (string?)null), Assert.Single(w.Tools.Calls.Where(c => c.Tool == "points_leaders")));
    }

    [Fact]
    public void top5_requests_five()
    {
        var w = NewWorld();
        Leaders(w, "{\"entries\":[],\"alias\":\"stars\"}");

        Assert.Equal("Nobody is on the stars leaderboard yet.", w.Say(new TopLogic(), Carol, "!top5", "m1"));
        Assert.Contains("{\"limit\":5}", w.Tools.Calls.Single(c => c.Tool == "points_leaders").Args);
    }

    [Fact]
    public void the_point_type_argument_must_name_the_community_currency()
    {
        var w = NewWorld();
        Leaders(w, $"{{\"entries\":[{{\"user\":\"{Alice}\",\"balance\":1}}],\"alias\":\"Stars\"}}");
        var l = new TopLogic();

        Assert.StartsWith("Top 5 in Stars", w.Say(l, Carol, "!top5 stars", "m1"));
        Assert.Equal("Point type 'gold' was not found. Use the exact point type name or omit it for the default.", w.Say(l, Carol, "!top5 gold", "m2"));
    }

    [Fact]
    public void a_blank_alias_falls_back_to_the_documented_default()
    {
        var w = NewWorld();
        Leaders(w, "{\"entries\":[],\"alias\":\" \"}");

        Assert.Equal("Nobody is on the waddlebucks leaderboard yet.", w.Say(new TopLogic(), Carol, "!top10", "m1"));
    }

    [Fact]
    public void toprep_lists_the_reputation_leaderboard()
    {
        var w = NewWorld();
        Board(w, $"{{\"entries\":[{{\"rank\":1,\"user\":\"{Bob}\",\"score\":1500,\"tier\":\"Gold\"}},{{\"rank\":2,\"user\":\"{Alice}\",\"score\":3,\"tier\":\"New\"}}]}}");

        Assert.Equal($"Top 10 in reputation: 1. {{user:{Bob}}} 1,500 (Gold), 2. {{user:{Alice}}} 3 (New)", w.Say(new TopLogic(), Carol, "!toprep", "m1"));
    }

    [Fact]
    public void an_empty_reputation_board_says_so()
    {
        var w = NewWorld();
        Board(w, "{\"entries\":[]}");

        Assert.Equal("Nobody is on the reputation leaderboard yet.", w.Say(new TopLogic(), Carol, "!toprep", "m1"));
    }

    [Fact]
    public void a_missing_grant_or_a_contract_mismatch_fails_loudly()
    {
        var w = NewWorld();
        Assert.Throws<WaddleToolException>(() => w.Say(new TopLogic(), Carol, "!top10", "m1"));

        Leaders(w, "{\"unexpected\":true}");
        Assert.Throws<WaddleToolException>(() => w.Say(new TopLogic(), Carol, "!top10", "m2"));
        Board(w, "{}");
        Assert.Throws<WaddleToolException>(() => w.Say(new TopLogic(), Carol, "!toprep", "m4"));
        Leaders(w, "not json");
        Assert.Throws<WaddleToolException>(() => w.Say(new TopLogic(), Carol, "!top10", "m3"));
    }

    [Fact]
    public void unported_commands_other_text_flag_off_and_no_community_are_handled()
    {
        var w = NewWorld();
        var l = new TopLogic();
        foreach (var text in new[] { "!toptime", "!loudest", "!toptournaments", "!toptickets", "chat" })
        {
            Assert.Null(w.Say(l, Alice, text, "m-" + text));
        }

        Assert.Contains("need a community", World.TextOf(l.Run(TestKit.Chat("!top10"), w.HostFor(Alice, "m3", community: null))!));
        w.Flags.Overrides[TopLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!top10", "m4"));
    }
}

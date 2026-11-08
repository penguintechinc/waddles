using System.Text.Json;
using System.Text.RegularExpressions;
using WaddleSdk.Economy;
using WaddleSdk.State;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinGiveaway.Tests.World;

namespace WaddleBundleSuperpenguinGiveaway.Tests;

public class RaffleTests
{
    private const string Mod = "mod-1";

    private static World NewWorld()
    {
        var w = new World(GiveawayLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave, Mod, "user-a", "user-b", "user-c", "user-d", "user-e" })
        {
            w.Ledger.Seed(u, 0);
        }

        return w;
    }

    private static GiveawayLogic NewLogic() => new(buffer =>
    {
        for (var i = 0; i < buffer.Length; i++)
        {
            buffer[i] = (byte)(i * 5 + 3);
        }
    });

    private static string? Run(World w, GiveawayLogic l, string actor, string text, string id, bool mod = false, bool bot = false)
    {
        var json = "{\"text\":\"" + text + "\",\"channel_id\":\"chan-1\"" + (mod ? ",\"is_mod\":true" : "") + (bot ? ",\"is_bot\":true" : "") + "}";
        var ev = new PlatformEventInfo("twitch", "chat.message", "platform-actor", json, "2026-11-07T00:00:00.000Z");
        var reply = l.Run(ev, w.HostFor(actor, id));
        return reply is null ? null : TextOf(reply);
    }

    private static string? ModRun(World w, GiveawayLogic l, string text, string id) => Run(w, l, Mod, text, id, mod: true);

    private static KvStateStore Store(World w) => new(w.Kv);

    private static RaffleDoc? Doc(World w, string key) =>
        Store(w).GetJson($"tgw.raffle.{key}", GiveawayJsonContext.Default.RaffleDoc);

    private static (World World, GiveawayLogic Logic) Started(string key = "k1", int winners = 2, long total = 101, params string[] entrants)
    {
        var w = NewWorld();
        var l = NewLogic();
        Assert.Contains("started", ModRun(w, l, $"!tgw raffle start {key} {winners} {total}", "s1"));
        foreach (var e in entrants)
        {
            Assert.Contains("entered raffle", Run(w, l, e, $"!tgw raffle enter {key}", "en-" + e));
        }

        return (w, l);
    }

    [Fact]
    public void usage_is_shown_for_missing_or_invalid_keys_and_unknown_actions()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Equal(GiveawayLogic.RaffleUsage, Run(w, l, Alice, "!tgw raffle", "m1"));
        Assert.Equal(GiveawayLogic.RaffleUsage, Run(w, l, Alice, "!tgw raffle enter", "m2"));
        Assert.Equal(GiveawayLogic.RaffleUsage, Run(w, l, Alice, "!tgw raffle enter bad_key", "m3"));
        Assert.Equal(GiveawayLogic.RaffleUsage, Run(w, l, Alice, "!tgw raffle enter " + new string('a', 25), "m4"));
        Assert.Equal(GiveawayLogic.RaffleUsage, Run(w, l, Alice, "!tgw raffle dance k", "m5"));
    }

    [Theory]
    [InlineData("!tgw raffle start k1")]
    [InlineData("!tgw raffle start k1 0 10")]
    [InlineData("!tgw raffle start k1 51 10")]
    [InlineData("!tgw raffle start k1 2 1000001")]
    [InlineData("!tgw raffle start k1 x 10")]
    [InlineData("!tgw raffle start k1 2 -5")]
    public void starting_validates_winners_and_award(string text)
    {
        var w = NewWorld();

        Assert.Contains("invalid_request", ModRun(w, NewLogic(), text, "m1"));
        Assert.Null(Doc(w, "k1"));
    }

    [Fact]
    public void starting_publishes_a_commitment_and_a_redelivery_reprints_it_while_another_start_is_refused()
    {
        var w = NewWorld();
        var l = NewLogic();

        var first = ModRun(w, l, "!tgw raffle start K1 2 100", "s1")!;
        var doc = Doc(w, "k1")!;

        Assert.Contains("Raffle 'k1' started: 2 winner(s) share 100 stars", first);
        Assert.Contains(doc.Commitment, first);
        Assert.DoesNotContain(doc.Seed, first);
        Assert.Equal(1, doc.Round);
        Assert.Equal(first, ModRun(w, l, "!tgw raffle start K1 2 100", "s1"));
        Assert.Contains("already_running", ModRun(w, l, "!tgw raffle start k1 3 5", "s2"));
        Assert.Equal(2, Doc(w, "k1")!.Winners);
    }

    [Fact]
    public void entering_is_free_once_per_user_and_refused_for_bots_and_excluded_users()
    {
        var (w, l) = Started("k1", 1, 10);

        Assert.Contains("entered raffle 'k1' (1 entries)", Run(w, l, Alice, "!tgw raffle enter k1", "m1"));
        Assert.Contains("already_entered", Run(w, l, Alice, "!tgw raffle enter k1", "m2"));
        Assert.Contains("cannot enter this raffle", Run(w, l, Bob, "!tgw raffle enter k1", "m3", bot: true));
        ModRun(w, l, $"!tgw exclude {Carol}", "x1");
        Assert.Contains("cannot enter this raffle", Run(w, l, Carol, "!tgw raffle enter k1", "m4"));
        Assert.Contains("1 entries, 1 winner(s), 10 total award", Run(w, l, Alice, "!tgw raffle count k1", "m5"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void entering_or_counting_a_missing_raffle_says_not_running()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("not_running", Run(w, l, Alice, "!tgw raffle enter k1", "m1"));
        Assert.Contains("not_running", Run(w, l, Alice, "!tgw raffle count k1", "m2"));
        Assert.Contains("not_running", ModRun(w, l, "!tgw raffle end k1", "m3"));
        Assert.Contains("not_running", ModRun(w, l, "!tgw raffle winners k1 2", "m4"));
        Assert.Contains("not_running", ModRun(w, l, "!tgw raffle award k1 2", "m5"));
    }

    [Fact]
    public void a_full_raffle_refuses_more_entries()
    {
        var (w, l) = Started("k1", 1, 10);
        var doc = Doc(w, "k1")!;
        Store(w).SetJson("tgw.raffle.k1", doc with { Entries = Enumerable.Range(0, GiveawayLogic.MaxRaffleEntries).Select(i => $"e{i}").ToList() }, GiveawayJsonContext.Default.RaffleDoc);

        Assert.Contains("is full", Run(w, l, Alice, "!tgw raffle enter k1", "m1"));
    }

    [Fact]
    public void winners_and_award_can_be_changed_before_the_end_and_are_validated()
    {
        var (w, l) = Started("k1", 1, 10);

        Assert.Contains("will have 3 winner(s)", ModRun(w, l, "!tgw raffle winners k1 3", "m1"));
        Assert.Contains("total award is now 50", ModRun(w, l, "!tgw raffle award k1 50", "m2"));
        Assert.Equal((3, 50L), (Doc(w, "k1")!.Winners, Doc(w, "k1")!.Total));
        Assert.Contains("invalid_request", ModRun(w, l, "!tgw raffle winners k1 0", "m3"));
        Assert.Contains("invalid_request", ModRun(w, l, "!tgw raffle winners k1", "m4"));
        Assert.Contains("invalid_request", ModRun(w, l, "!tgw raffle award k1 -1", "m5"));
        Assert.Contains("invalid_request", ModRun(w, l, "!tgw raffle award k1 1000001", "m6"));
    }

    [Fact]
    public void a_raffle_with_pending_winners_refuses_entries_and_changes()
    {
        var (w, l) = Started("k1", 1, 10, Alice);
        var doc = Doc(w, "k1")!;
        Store(w).SetJson("tgw.raffle.k1", doc with { Pending = [Alice], Awarded = [], Failed = [] }, GiveawayJsonContext.Default.RaffleDoc);

        Assert.Contains("ending_in_progress", Run(w, l, Bob, "!tgw raffle enter k1", "m1"));
        Assert.Contains("ending_in_progress", ModRun(w, l, "!tgw raffle winners k1 2", "m2"));
        Assert.Contains("ending_in_progress", ModRun(w, l, "!tgw raffle award k1 2", "m3"));
    }

    [Fact]
    public void ending_without_entries_removes_the_raffle()
    {
        var (w, l) = Started("k1", 1, 10);

        Assert.Contains("no_entries", ModRun(w, l, "!tgw raffle end k1", "e1"));
        Assert.Null(Doc(w, "k1"));
    }

    [Fact]
    public void ending_pays_each_winner_the_rounded_up_share_once_and_is_verifiable()
    {
        var (w, l) = Started("k1", 2, 101, "user-a", "user-b", "user-c", "user-d", "user-e");
        var doc = Doc(w, "k1")!;

        var text = ModRun(w, l, "!tgw raffle end k1", "e1")!;

        Assert.Contains("Raffle 'k1' ended:", text);
        Assert.Contains("win 51 stars each", text);
        Assert.Contains($"seed={doc.Seed}", text);
        Assert.Equal(2, w.Ledger.Moved.Count);
        Assert.All(w.Ledger.Moved, m => Assert.Equal(("credit", 51UL), (m.Kind, m.Amount)));
        Assert.Null(Doc(w, "k1"));

        List<string> sorted = ["user-a", "user-b", "user-c", "user-d", "user-e"];
        Assert.True(Fairness.VerifyCommitment(doc.Seed, GiveawayLogic.RaffleLabel("k1", 1), doc.Commitment));
        var picks = Fairness.PickWithoutReplacement(Sha256.FromHex(doc.Seed)!, GiveawayLogic.RaffleDrawFingerprint("k1", 1, sorted), sorted, 2);
        Assert.Equal(picks.OrderBy(p => p), w.Ledger.Moved.Select(m => m.User).OrderBy(p => p));
        Assert.All(picks, p => Assert.Contains($"{{user:{p}}}", text));
    }

    [Fact]
    public void a_redelivered_end_reprints_the_result_without_paying_again_and_a_second_end_finds_nothing()
    {
        var (w, l) = Started("k1", 2, 100, "user-a", "user-b", "user-c");
        var first = ModRun(w, l, "!tgw raffle end k1", "e1");
        var paid = w.Ledger.Moved.Count;

        Assert.Equal(first, ModRun(w, l, "!tgw raffle end k1", "e1"));
        Assert.Equal(paid, w.Ledger.Moved.Count);
        Assert.Contains("not_running", ModRun(w, l, "!tgw raffle end k1", "e2"));
    }

    [Fact]
    public void asking_for_more_winners_than_entrants_and_a_zero_award_are_handled()
    {
        var (w, l) = Started("k1", 3, 5, "user-a", "user-b");
        Assert.Contains("win 3 stars each", ModRun(w, l, "!tgw raffle end k1", "e1"));
        Assert.Equal(2, w.Ledger.Moved.Count);

        var (w2, l2) = Started("k2", 1, 0, "user-a");
        Assert.Contains("win 0 stars each", ModRun(w2, l2, "!tgw raffle end k2", "e2"));
        Assert.Empty(w2.Ledger.Moved);
    }

    [Fact]
    public void a_failed_payout_resumes_without_paying_anyone_twice()
    {
        var (w, l) = Started("k1", 3, 90, "user-a", "user-b", "user-c");
        w.Ledger.BeforeCall = (_, ordinal) =>
        {
            if (ordinal == 1)
            {
                throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "down");
            }
        };

        Assert.Throws<WaddleEconomyException>(() => ModRun(w, l, "!tgw raffle end k1", "e1"));
        var pending = Doc(w, "k1")!;
        Assert.Equal(3, pending.Pending!.Count);
        Assert.Single(pending.Awarded!);

        w.Ledger.BeforeCall = null;
        var text = ModRun(w, l, "!tgw raffle end k1", "e2")!;

        Assert.Contains("win 30 stars each", text);
        Assert.Equal(3, w.Ledger.Moved.Count);
        Assert.Equal(3, w.Ledger.Moved.Select(m => m.User).Distinct().Count());
        Assert.Null(Doc(w, "k1"));
    }

    [Fact]
    public void winners_who_left_or_hit_a_cap_are_reported_as_payout_failed_while_others_are_paid()
    {
        var (w, l) = Started("k1", 3, 90, "user-a", "user-b", "user-c");
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

        var text = ModRun(w, l, "!tgw raffle end k1", "e1")!;

        Assert.Contains("payout_failed for 2", text);
        Assert.Single(w.Ledger.Moved);
        Assert.Contains(w.Log.Writes, x => x.FieldsJson.Contains("not_a_member"));
        Assert.Contains(w.Log.Writes, x => x.FieldsJson.Contains("over_cap"));
    }

    [Fact]
    public void a_concurrent_end_is_told_it_is_in_progress()
    {
        var (w, l) = Started("k1", 1, 10, "user-a");
        Store(w).TryClaim("tgw.rlease.k1.1", 60);

        Assert.Contains("ending_in_progress", ModRun(w, l, "!tgw raffle end k1", "e1"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void a_corrupt_seed_fails_loudly_and_releases_the_lease()
    {
        var (w, l) = Started("k1", 1, 10, "user-a");
        Store(w).SetJson("tgw.raffle.k1", Doc(w, "k1")! with { Seed = "zz" }, GiveawayJsonContext.Default.RaffleDoc);

        Assert.Throws<InvalidOperationException>(() => ModRun(w, l, "!tgw raffle end k1", "e1"));
        Assert.Throws<InvalidOperationException>(() => ModRun(w, l, "!tgw raffle end k1", "e2"));
    }

    [Fact]
    public void raffle_winners_are_announced_on_the_overlay_when_enabled_and_again_on_a_redelivery()
    {
        var (w, l) = Started("k1", 1, 10, "user-a");
        ModRun(w, l, "!tgw overlay on", "o1");
        ModRun(w, l, "!tgw raffle end k1", "e1");
        ModRun(w, l, "!tgw raffle end k1", "e1");

        Assert.Equal(2, w.Pushed.Pushes.Count);
        var alert = JsonDocument.Parse(w.Pushed.Pushes[0].Payload).RootElement.GetProperty("alert");
        Assert.Equal("superpenguin.raffle.winner", alert.GetProperty("alert_type").GetString());
        Assert.Equal("user-a", alert.GetProperty("user").GetString());
        Assert.Equal("k1", alert.GetProperty("amount").GetProperty("key").GetString());

        var (w2, l2) = Started("k2", 2, 10, "user-a", "user-b");
        ModRun(w2, l2, "!tgw overlay on", "o2");
        ModRun(w2, l2, "!tgw raffle end k2", "e2");
        var multi = JsonDocument.Parse(w2.Pushed.Pushes.Single().Payload).RootElement.GetProperty("alert");
        Assert.False(multi.TryGetProperty("user", out _));
        Assert.Equal(2, multi.GetProperty("amount").GetProperty("winners").GetArrayLength());
    }

    [Fact]
    public void raffle_winner_derivation_is_deterministic_and_never_picks_an_entrant_twice()
    {
        List<string> sorted = Enumerable.Range(0, 20).Select(i => $"u{i:00}").ToList();
        var seed = Sha256.Hash("raffle seed");
        var fp = GiveawayLogic.RaffleDrawFingerprint("k", 1, sorted);

        var picks = Fairness.PickWithoutReplacement(seed, fp, sorted, 10);

        Assert.Equal(picks, Fairness.PickWithoutReplacement(seed, fp, sorted, 10));
        Assert.Equal(10, picks.Distinct().Count());
        Assert.Matches(new Regex("^tgw1-raffle\\|k\\|1\\|[0-9a-f]{64}$"), fp);
    }
}

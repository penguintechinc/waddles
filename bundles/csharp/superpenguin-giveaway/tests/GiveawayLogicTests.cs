using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using WaddleSdk.Tests;
using WaddleSdk.Tool;
using WaddleSdk.Types;
using Xunit;
using static WaddleBundleSuperpenguinGiveaway.Tests.World;

namespace WaddleBundleSuperpenguinGiveaway.Tests;

/// <summary>An overlay whose every call is refused like a host without an overlay handler.</summary>
public sealed class RefusingOverlay : IOverlayClient
{
    public TransportResultInfo RegisterWidget(OverlayWidget widget) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("NOT_IMPLEMENTED", "overlay host capability has no stage-side handler yet"));

    public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson) =>
        throw new WaddleTransportException(TransportErrorInfo.Fatal("NOT_IMPLEMENTED", "overlay host capability has no stage-side handler yet"));
}

/// <summary>A kv that drops the next writes to keys with a given prefix (a lost update from a concurrent writer).</summary>
public sealed class DroppingKv(IKvClient inner, string prefix, int drops) : IKvClient
{
    public int Remaining { get; set; } = drops;

    public byte[]? Get(string key) => inner.Get(key);

    public void Set(string key, byte[] value, uint ttlSeconds)
    {
        if (key.StartsWith(prefix, StringComparison.Ordinal) && Remaining > 0)
        {
            Remaining--;
            return;
        }

        inner.Set(key, value, ttlSeconds);
    }

    public void Delete(string key) => inner.Delete(key);

    public long Increment(string key, long delta, uint ttlSeconds) => inner.Increment(key, delta, ttlSeconds);
}

public class GiveawayLogicTests
{
    private const string Mod = "mod-1";

    private static World NewWorld()
    {
        var w = new World(GiveawayLogic.FlagKey);
        foreach (var u in new[] { Alice, Bob, Carol, Dave, Mod, "user-x" })
        {
            w.Ledger.Seed(u, 1000);
        }

        return w;
    }

    private static GiveawayLogic NewLogic() => new(buffer =>
    {
        for (var i = 0; i < buffer.Length; i++)
        {
            buffer[i] = (byte)(i * 7 + 1);
        }
    });

    private static PlatformEventInfo Ev(string text, bool mod = false, bool bot = false)
    {
        var json = "{\"text\":\"" + text + "\",\"channel_id\":\"chan-1\"" + (mod ? ",\"is_mod\":true" : "") + (bot ? ",\"is_bot\":true" : "") + "}";
        return new PlatformEventInfo("twitch", "chat.message", "platform-actor", json, "2026-11-07T00:00:00.000Z");
    }

    private static string? Run(World w, GiveawayLogic l, string actor, string text, string id, bool mod = false, bool bot = false)
    {
        var reply = l.Run(Ev(text, mod, bot), w.HostFor(actor, id));
        return reply is null ? null : TextOf(reply);
    }

    private static string? ModRun(World w, GiveawayLogic l, string text, string id) => Run(w, l, Mod, text, id, mod: true);

    private static KvStateStore Store(World w) => new(w.Kv);

    private static GiveawayState State(World w) =>
        Store(w).GetJson("tgw.state", GiveawayJsonContext.Default.GiveawayState)!;

    private static void Enter(World w, GiveawayLogic l, string actor, int n, string id) =>
        Assert.Contains("you have bought", Run(w, l, actor, $"!tgw enter {n}", id));

    private static (World World, GiveawayLogic Logic) ThreeEntrants()
    {
        var w = NewWorld();
        var l = NewLogic();
        Enter(w, l, Alice, 10, "e1");
        Enter(w, l, Bob, 30, "e2");
        Enter(w, l, Carol, 60, "e3");
        return (w, l);
    }

    [Fact]
    public void other_commands_and_a_disabled_flag_are_silent()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Null(Run(w, l, Alice, "!other", "m1"));
        Assert.Null(Run(w, l, Alice, "hello", "m2"));
        w.Flags.Overrides[GiveawayLogic.FlagKey] = false;
        Assert.Null(Run(w, l, Alice, "!tgw status", "m3"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void both_root_commands_work_and_unknown_subcommands_get_the_usage()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Equal(GiveawayLogic.Usage, Run(w, l, Alice, "!tgw", "m1"));
        Assert.Equal(GiveawayLogic.Usage, Run(w, l, Alice, "!ticketgiveaway nonsense", "m2"));
        Assert.Contains("Giveaway round 1 is open", Run(w, l, Alice, "!ticketgiveaway status", "m3"));
    }

    [Fact]
    public void it_refuses_without_a_community_or_a_linked_account()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("needs a community", TextOf(l.Run(Ev("!tgw status"), w.HostFor(Alice, "m1", community: null))!));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "nl");
        Assert.Contains("Link your account", Run(w, l, Alice, "!tgw status", "m2"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "nm");
        Assert.Contains("Link your account", Run(w, l, Alice, "!tgw status", "m3"));
        w.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Backend, "down");
        Assert.Throws<WaddleIdentityException>(() => Run(w, l, Alice, "!tgw status", "m4"));
    }

    [Theory]
    [InlineData("!tgw open")]
    [InlineData("!tgw close")]
    [InlineData("!tgw draw")]
    [InlineData("!tgw reset")]
    [InlineData("!tgw prize cake")]
    [InlineData("!tgw tier 2")]
    [InlineData("!tgw exclude x")]
    [InlineData("!tgw unexclude x")]
    [InlineData("!tgw fairness")]
    [InlineData("!tgw overlay on")]
    [InlineData("!tgw raffle start k 1 1")]
    [InlineData("!tgw raffle end k")]
    [InlineData("!tgw raffle winners k 2")]
    [InlineData("!tgw raffle award k 2")]
    public void moderator_commands_are_refused_for_everyone_else(string text)
    {
        var w = NewWorld();

        Assert.Equal(GiveawayLogic.ModOnly, Run(w, NewLogic(), Alice, text, "m1"));
        Assert.Empty(w.Kv.SetCalls);
    }

    [Fact]
    public void entering_charges_one_point_per_ticket_and_accumulates()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Equal($"{{user:{Alice}}}, you have bought 5 entries (5 in total).", Run(w, l, Alice, "!tgw enter 5", "m1"));
        Assert.Equal($"{{user:{Alice}}}, you have bought 3 entries (8 in total).", Run(w, l, Alice, "!tgw enter 3", "m2"));

        Assert.Equal(992, w.Ledger.Balance(Alice));
        Assert.Contains("has 8 entries", Run(w, l, Alice, "!tgw entries", "m3"));
        Assert.Contains("1 entrants with 8 tickets", Run(w, l, Alice, "!tgw status", "m4"));
    }

    [Fact]
    public void a_redelivered_entry_charges_and_credits_tickets_once()
    {
        var w = NewWorld();
        var l = NewLogic();
        var first = Run(w, l, Alice, "!tgw enter 5", "m1");

        var again = Run(w, l, Alice, "!tgw enter 5", "m1");

        Assert.Equal(first, again);
        Assert.Equal(995, w.Ledger.Balance(Alice));
        Assert.Single(w.Ledger.Moved);
    }

    [Fact]
    public void max_and_all_enter_the_whole_balance_reported_by_the_economy_service()
    {
        var w = NewWorld();
        var l = NewLogic();
        w.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ =>
            Encoding.UTF8.GetBytes($"{{\"user\":\"{Alice}\",\"balance\":7,\"alias\":\"stars\"}}");

        Assert.Contains("bought 7 entries", Run(w, l, Alice, "!tgw enter MAX", "m1"));
        Assert.Contains("bought 7 entries (14 in total)", Run(w, l, Alice, "!tgw enter all", "m2"));
    }

    [Fact]
    public void invalid_amounts_and_insufficient_funds_get_replies_and_move_nothing()
    {
        var w = NewWorld();
        var l = NewLogic();
        w.Ledger.Seed(Bob, 2);

        Assert.Contains("AMOUNT/MAX/ALL", Run(w, l, Alice, "!tgw enter", "m1"));
        Assert.Contains("number or max/all", Run(w, l, Alice, "!tgw enter abc", "m2"));
        Assert.Contains("number or max/all", Run(w, l, Alice, "!tgw enter -3", "m3"));
        Assert.Contains("not have enough", Run(w, l, Alice, "!tgw enter 0", "m4"));
        Assert.Contains("not have enough", Run(w, l, Alice, "!tgw enter 1000001", "m5"));
        Assert.Contains("not have enough", Run(w, l, Bob, "!tgw enter 5", "m6"));
        Assert.Empty(w.Ledger.Moved);
        Assert.Contains("0 entrants", Run(w, l, Alice, "!tgw status", "m7"));
    }

    [Fact]
    public void an_over_cap_debit_is_a_refusal_not_a_crash()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.OverCap, "cap");

        Assert.Contains("not have enough", Run(w, NewLogic(), Alice, "!tgw enter 5", "m1"));
    }

    [Fact]
    public void other_economy_failures_and_a_blank_message_id_fail_loudly()
    {
        var w = NewWorld();
        w.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "down");
        Assert.Throws<WaddleEconomyException>(() => Run(w, NewLogic(), Alice, "!tgw enter 5", "m1"));

        Assert.Throws<InvalidOperationException>(() => Run(NewWorld(), NewLogic(), Alice, "!tgw enter 5", " "));
    }

    [Fact]
    public void a_closed_giveaway_refuses_entries_until_it_is_opened_again()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("already open", ModRun(w, l, "!tgw open", "m1"));
        Assert.Contains("closed with no eligible entries", ModRun(w, l, "!tgw close", "m2"));
        Assert.Contains("closed and not accepting", Run(w, l, Alice, "!tgw enter 5", "m3"));
        Assert.Contains("The giveaway is open!", ModRun(w, l, "!tgw open", "m4"));
        Enter(w, l, Alice, 5, "m5");
    }

    [Fact]
    public void moderators_the_broadcaster_and_bots_cannot_enter()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("cannot enter", Run(w, l, Alice, "!tgw enter 5", "m1", mod: true));
        Assert.Contains("cannot enter", Run(w, l, Alice, "!tgw enter 5", "m2", bot: true));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void explicit_exclusions_block_entry_until_removed_or_expired()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains($"{{user:{Bob}}} is excluded from giveaways permanently", ModRun(w, l, $"!tgw exclude {Bob}", "m1"));
        Assert.Contains("excluded from giveaways", Run(w, l, Bob, "!tgw enter 5", "m2"));
        Assert.Contains("can enter giveaways again", ModRun(w, l, $"!tgw unexclude {Bob}", "m3"));
        Enter(w, l, Bob, 5, "m4");

        Assert.Contains("for 2 hour(s)", ModRun(w, l, $"!tgw exclude {Carol} 2", "m5"));
        Assert.Contains("excluded", Run(w, l, Carol, "!tgw enter 5", "m6"));
        w.Clock.Now += 3UL * 3_600_000;
        Enter(w, l, Carol, 5, "m7");
    }

    [Fact]
    public void exclusion_commands_validate_their_arguments_and_targets()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("Usage: !tgw exclude", ModRun(w, l, "!tgw exclude", "m1"));
        Assert.Contains("Usage: !tgw exclude", ModRun(w, l, $"!tgw exclude {Bob} 0", "m2"));
        Assert.Contains("Usage: !tgw exclude", ModRun(w, l, $"!tgw exclude {Bob} 9000", "m3"));
        Assert.Contains("Usage: !tgw exclude", ModRun(w, l, $"!tgw exclude {Bob} 1 2", "m4"));
        Assert.Contains("Usage: !tgw unexclude", ModRun(w, l, "!tgw unexclude", "m5"));
        Assert.Contains("was not excluded", ModRun(w, l, $"!tgw unexclude {Bob}", "m6"));
        w.MentionFailure = new WaddleIdentityException(IdentityErrorKind.NotFound, "nf");
        Assert.Equal("Could not find that viewer.", ModRun(w, l, $"!tgw exclude {Bob}", "m7"));
        Assert.Equal("Could not find that viewer.", ModRun(w, l, $"!tgw unexclude {Bob}", "m8"));
    }

    [Fact]
    public void the_exclusion_list_has_a_bounded_size()
    {
        var w = NewWorld();
        var items = Enumerable.Range(0, GiveawayLogic.MaxExclusions).Select(i => new ExclusionItem($"u{i}", 0)).ToList();
        Store(w).SetJson("tgw.excl", new ExclusionList(items), GiveawayJsonContext.Default.ExclusionList);

        Assert.Equal("The exclusion list is full.", ModRun(w, NewLogic(), $"!tgw exclude {Bob}", "m1"));
    }

    [Fact]
    public void closing_publishes_a_commitment_without_leaking_the_seed()
    {
        var (w, l) = ThreeEntrants();

        var text = ModRun(w, l, "!tgw close", "c1")!;

        var freeze = State(w).Freeze!;
        Assert.Contains("3 eligible entrants and 100 tickets", text);
        Assert.Contains(freeze.Commitment, text);
        Assert.Contains(freeze.Fingerprint, text);
        Assert.DoesNotContain(freeze.Seed, text);
        Assert.False(State(w).Open);
        Assert.Contains($"closed (commitment {freeze.Commitment})", Run(w, l, Alice, "!tgw status", "s1"));
        Assert.Equal(text, ModRun(w, l, "!tgw close", "c2"));
    }

    [Fact]
    public void a_draw_is_verifiable_from_the_published_audit_record()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, "!tgw prize Signed Poster", "p1");
        ModRun(w, l, "!tgw close", "c1");

        var text = ModRun(w, l, "!tgw draw", "d1")!;

        var audit = Store(w).GetJson("tgw.audit", GiveawayJsonContext.Default.AuditRecord)!;
        Assert.Equal(GiveawayLogic.AuditText(audit), Run(w, l, Alice, "!tgw audit", "a1"));
        Assert.Contains($"{{user:{audit.Winner}}} won the Signed Poster", text);
        Assert.Contains(audit.Seed, text);

        List<Ticket> pool = [new(Alice, 10), new(Bob, 30), new(Carol, 60)];
        var fingerprint = Fairness.Fingerprint(1, pool);
        Assert.Equal(audit.Fingerprint, fingerprint);
        Assert.True(Fairness.VerifyCommitment(audit.Seed, fingerprint, audit.Commitment));
        var index = Fairness.DeriveIndex(Sha256.FromHex(audit.Seed)!, fingerprint, 1, 100);
        Assert.Equal((ulong)audit.Index, index);
        var located = Fairness.Locate(pool, index);
        Assert.Equal((audit.Winner, audit.RangeStart, audit.RangeEnd), (located.Actor, located.RangeStart, located.RangeEnd));
        var chance = Regex.Match(text, @"with a (\d+\.\d\d)% chance").Groups[1].Value;
        Assert.Equal((audit.RangeEnd - audit.RangeStart + 1).ToString("0", CultureInfo.InvariantCulture) + ".00", chance);
        var tampered = audit.Seed[1..] + "0";
        Assert.False(Fairness.VerifyCommitment(tampered, fingerprint, audit.Commitment));
    }

    [Fact]
    public void drawing_before_closing_is_refused_and_an_empty_pool_has_no_winner()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("Close the giveaway first", ModRun(w, l, "!tgw draw", "d1"));
        Assert.Equal("No draw has been made yet.", Run(w, l, Alice, "!tgw audit", "a1"));
    }

    [Fact]
    public void a_pool_that_changed_after_closing_refuses_to_draw_until_closed_again()
    {
        var (w, l) = ThreeEntrants();
        var first = ModRun(w, l, "!tgw close", "c1")!;
        ModRun(w, l, $"!tgw exclude {Alice}", "x1");

        Assert.Contains("pool changed since it was closed", ModRun(w, l, "!tgw draw", "d1"));
        var second = ModRun(w, l, "!tgw close", "c2")!;
        Assert.NotEqual(first, second);
        Assert.Contains("2 eligible entrants and 90 tickets", second);
        Assert.Contains("won the No Prize", ModRun(w, l, "!tgw draw", "d2"));
    }

    [Fact]
    public void an_excluded_entrant_never_wins_even_with_tickets()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, $"!tgw exclude {Carol}", "x1");
        ModRun(w, l, "!tgw close", "c1");

        var text = ModRun(w, l, "!tgw draw", "d1")!;

        Assert.DoesNotContain(Carol, text);
        Assert.NotEqual(Carol, Store(w).GetJson("tgw.audit", GiveawayJsonContext.Default.AuditRecord)!.Winner);
    }

    [Fact]
    public void repeated_draws_pick_distinct_winners_until_the_pool_is_exhausted()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, "!tgw close", "c1");
        var winners = new List<string>();

        for (var i = 1; i <= 3; i++)
        {
            ModRun(w, l, "!tgw draw", $"d{i}");
            winners.Add(Store(w).GetJson("tgw.audit", GiveawayJsonContext.Default.AuditRecord)!.Winner);
        }

        Assert.Equal(3, winners.Distinct().Count());
        Assert.Equal("No eligible entries for the giveaway draw.", ModRun(w, l, "!tgw draw", "d4"));
        Assert.Equal(3, State(w).Freeze!.Draws);
    }

    [Fact]
    public void a_redelivered_draw_reprints_the_same_result_without_drawing_again()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, "!tgw close", "c1");
        var first = ModRun(w, l, "!tgw draw", "d1");

        var again = ModRun(w, l, "!tgw draw", "d1");

        Assert.Equal(first, again);
        Assert.Equal(1, State(w).Freeze!.Draws);
        Assert.Single(State(w).Freeze!.Won);
    }

    [Fact]
    public void a_winner_sits_out_the_tier_number_of_following_rounds()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, "!tgw tier 2", "t1");
        ModRun(w, l, "!tgw close", "c1");
        ModRun(w, l, "!tgw draw", "d1");
        var winner = Store(w).GetJson("tgw.audit", GiveawayJsonContext.Default.AuditRecord)!.Winner;

        Assert.Contains("Giveaway reset: round 2 is open", ModRun(w, l, "!tgw reset", "r1"));
        Assert.Contains("sitting out", Run(w, l, winner, "!tgw enter 5", "n1"));
        ModRun(w, l, "!tgw reset", "r2");
        Assert.Contains("sitting out", Run(w, l, winner, "!tgw enter 5", "n2"));
        ModRun(w, l, "!tgw reset", "r3");
        Enter(w, l, winner, 5, "n3");
    }

    [Fact]
    public void a_redelivered_reset_advances_the_round_once()
    {
        var w = NewWorld();
        var l = NewLogic();

        ModRun(w, l, "!tgw reset", "r1");
        ModRun(w, l, "!tgw reset", "r1");

        Assert.Equal(2, State(w).Round);
    }

    [Fact]
    public void prize_and_tier_commands_validate_their_arguments()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("Usage: !tgw prize", ModRun(w, l, "!tgw prize", "m1"));
        Assert.Contains("Usage: !tgw prize", ModRun(w, l, "!tgw prize " + new string('x', 201), "m2"));
        Assert.Equal("The giveaway prize is now: A Big Cake", ModRun(w, l, "!tgw prize A Big Cake", "m3"));
        Assert.Contains("Usage: !tgw tier", ModRun(w, l, "!tgw tier", "m4"));
        Assert.Contains("Usage: !tgw tier", ModRun(w, l, "!tgw tier 4", "m5"));
        Assert.Contains("Usage: !tgw tier", ModRun(w, l, "!tgw tier x", "m6"));
        Assert.Contains("sit out the next 3 round(s)", ModRun(w, l, "!tgw tier 3", "m7"));
        Assert.Equal("A Big Cake", State(w).Prize);
        Assert.Equal(3, State(w).Tier);
    }

    [Fact]
    public void the_fairness_report_clamps_iterations_and_reports_the_maximum_delta()
    {
        var (w, l) = ThreeEntrants();

        Assert.Contains("1000 simulated draws over 3 entrants / 100 tickets", ModRun(w, l, "!tgw fairness 999", "f1"));
        Assert.Contains("200000 simulated draws", ModRun(w, l, "!tgw fairness 999999", "f2"));
        var text = ModRun(w, l, "!tgw fairness", "f3")!;
        Assert.Contains("20000 simulated draws", text);
        Assert.Equal(text, ModRun(w, l, "!tgw fairness", "f4"));
        var delta = double.Parse(Regex.Match(text, @"= (\d+\.\d+)%").Groups[1].Value, CultureInfo.InvariantCulture);
        Assert.True(delta < 2.0, $"max delta {delta}");
        Assert.Contains("Usage: !tgw fairness", ModRun(w, l, "!tgw fairness 1 2", "f5"));
        Assert.Contains("Usage: !tgw fairness", ModRun(w, l, "!tgw fairness abc", "f6"));
        Assert.Equal("No eligible entries to report on.", ModRun(NewWorld(), NewLogic(), "!tgw fairness", "f7"));
    }

    [Fact]
    public void a_corrupt_stored_seed_fails_loudly()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, "!tgw close", "c1");
        var state = State(w);
        Store(w).SetJson("tgw.state", state with { Freeze = state.Freeze! with { Seed = "zz" } }, GiveawayJsonContext.Default.GiveawayState);

        Assert.Throws<InvalidOperationException>(() => ModRun(w, l, "!tgw draw", "d1"));
    }

    [Fact]
    public void overlay_messages_are_opt_in_and_carry_the_winner_uuid_not_a_name()
    {
        var (w, l) = ThreeEntrants();
        ModRun(w, l, "!tgw close", "c0");
        Assert.Empty(w.Pushed.Pushes);

        Assert.Equal("Giveaway overlay messages are on.", ModRun(w, l, "!tgw overlay on", "o1"));
        ModRun(w, l, $"!tgw exclude {Dave}", "x1");
        ModRun(w, l, "!tgw close", "c1");
        ModRun(w, l, "!tgw draw", "d1");
        ModRun(w, l, "!tgw open", "op1");
        ModRun(w, l, "!tgw reset", "r1");

        var alerts = w.Pushed.Pushes.Select(p => JsonDocument.Parse(p.Payload).RootElement.GetProperty("alert")).ToList();
        Assert.Equal(["superpenguin.giveaway.closed", "superpenguin.giveaway.winner", "superpenguin.giveaway.open"],
            alerts.Select(a => a.GetProperty("alert_type").GetString()).Distinct());
        var winner = alerts.Single(a => a.GetProperty("alert_type").GetString()!.EndsWith("winner", StringComparison.Ordinal));
        Assert.Equal(Store(w).GetJson("tgw.audit", GiveawayJsonContext.Default.AuditRecord)!.Winner, winner.GetProperty("user").GetString());
        Assert.Equal("No Prize", winner.GetProperty("amount").GetProperty("prize").GetString());
        Assert.All(w.Pushed.Pushes, p => Assert.Equal(("superpenguin.giveaway", OverlaySurface.AlertBox), (p.WidgetId, p.Surface)));

        Assert.Equal("Giveaway overlay messages are off.", ModRun(w, l, "!tgw overlay off", "o2"));
        var count = w.Pushed.Pushes.Count;
        ModRun(w, l, "!tgw reset", "r2");
        Assert.Equal(count, w.Pushed.Pushes.Count);
    }

    [Fact]
    public void overlay_registration_and_usage()
    {
        var w = NewWorld();
        var l = NewLogic();

        Assert.Contains("registered", ModRun(w, l, "!tgw overlay register", "m1"));
        Assert.Equal("superpenguin.giveaway", Assert.Single(w.Pushed.Registered).WidgetId);
        Assert.Contains("Usage: !tgw overlay", ModRun(w, l, "!tgw overlay", "m2"));
        Assert.Contains("Usage: !tgw overlay", ModRun(w, l, "!tgw overlay maybe", "m3"));
    }

    [Fact]
    public void a_refused_overlay_push_fails_the_command_loudly_when_overlays_are_enabled()
    {
        var w = NewWorld();
        var l = NewLogic();
        ModRun(w, l, "!tgw overlay on", "o1");
        w.Overlay = new RefusingOverlay();

        Assert.Throws<WaddleTransportException>(() => ModRun(w, l, "!tgw reset", "r1"));
        Assert.Throws<WaddleTransportException>(() => ModRun(w, l, "!tgw overlay register", "r2"));
    }

    [Fact]
    public void a_lost_shard_write_is_detected_by_read_back_and_retried()
    {
        var w = NewWorld();
        var l = NewLogic();
        w.KvOverride = new DroppingKv(w.Kv, "tgw.p.", 1);

        Assert.Contains("bought 5 entries (5 in total)", Run(w, l, Alice, "!tgw enter 5", "m1"));

        Assert.Contains(w.Log.Writes, x => x.Message == "tgw.shard_race");
        Assert.Equal(995, w.Ledger.Balance(Alice));
    }

    [Fact]
    public void persistent_write_loss_fails_loudly_and_the_redelivery_applies_the_tickets_exactly_once()
    {
        var w = NewWorld();
        var l = NewLogic();
        var dropping = new DroppingKv(w.Kv, "tgw.p.", 100);
        w.KvOverride = dropping;

        Assert.Throws<InvalidOperationException>(() => Run(w, l, Alice, "!tgw enter 5", "m1"));
        dropping.Remaining = 0;

        Assert.Contains("bought 5 entries (5 in total)", Run(w, l, Alice, "!tgw enter 5", "m1"));
        Assert.Equal(995, w.Ledger.Balance(Alice));
        Assert.Contains("has 5 entries", Run(w, l, Alice, "!tgw entries", "m2"));
    }

    [Fact]
    public void a_full_shard_and_the_per_person_cap_refuse_before_any_money_moves()
    {
        var w = NewWorld();
        var l = NewLogic();
        var shardKey = $"tgw.p.1.{StateKey.Hash(Alice)[..1]}";
        var others = Enumerable.Range(0, GiveawayLogic.ShardCap).Select(i => new PoolEntry($"other-{i}", 1, [])).ToList();
        Store(w).SetJson(shardKey, new PoolShard(others), GiveawayJsonContext.Default.PoolShard);

        Assert.Contains("giveaway is full", Run(w, l, Alice, "!tgw enter 5", "m1"));

        var capped = new PoolShard([new PoolEntry(Alice, GiveawayLogic.MaxTicketsPerUser, [])]);
        Store(w).SetJson(shardKey, capped, GiveawayJsonContext.Default.PoolShard);
        Assert.Contains("per-person ticket limit", Run(w, l, Alice, "!tgw enter 5", "m2"));
        Assert.Empty(w.Ledger.Moved);
    }

    [Fact]
    public void tickets_stay_inside_their_round_after_a_reset()
    {
        var w = NewWorld();
        var l = NewLogic();
        Enter(w, l, Alice, 5, "e1");

        ModRun(w, l, "!tgw reset", "r1");

        Assert.Contains("has 0 entries", Run(w, l, Alice, "!tgw entries", "m1"));
        Assert.Contains("0 entrants", Run(w, l, Alice, "!tgw status", "m2"));
    }
}

using System.Text.Json;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.State;
using WaddleSdk.Tests;
using WaddleSdk.Tool;
using WaddleSdk.Types;
using Xunit;

namespace SuperPenguin.DropGameCore.Tests;

/// <summary>A minimal game for exercising the host base: payout 0 / 1x / 3x by the first draw of the seed's stream.</summary>
public sealed class TestGame(ulong maxBet = 500, Func<ulong>? entropy = null, string configId = "test.v1") : WageredGameBase(MakeSpec(maxBet, configId), entropy)
{
    public const string Flag = "waddles.command-superpenguin-testdrop";
    public int Plays { get; private set; }
    public bool Explode { get; set; }
    public ulong? PayoutOverride { get; set; }
    public List<string> LastExtra { get; } = [];
    public string? ExtraRefusal { get; set; }
    public bool AllowExtra { get; set; }

    private static GameSpec MakeSpec(ulong maxBet, string configId) => new(
        "testdrop", ["testdrop", "td"], Flag, maxBet, 60, "Usage: !testdrop amount|all|max", configId,
        new OverlayWidget("testdrop", "Test Drop", OverlaySurface.FullScreen, OverlayEnvelope.Descriptor("testdrop", 1920, 1080)));

    protected override GameRound Play(ulong seed, ulong stake, IReadOnlyList<string> extraArgs)
    {
        Plays++;
        LastExtra.Clear();
        LastExtra.AddRange(extraArgs);
        if (Explode)
        {
            throw new InvalidOperationException("simulation bug");
        }

        var roll = new Xoshiro256(seed).NextInt(0, 2);
        var payout = PayoutOverride ?? roll switch { 0 => 0UL, 1 => stake, _ => stake * 3 };
        return new GameRound(payout, $"roll {roll}", $"{{\"roll\":{roll}}}");
    }

    protected override string? CheckExtraArgs(IReadOnlyList<string> extraArgs) =>
        ExtraRefusal ?? (AllowExtra || extraArgs.Count == 0 ? null : base.CheckExtraArgs(extraArgs));
}

/// <summary>Host-base behaviour: flag, community, identity, stake, cooldown, replay, money, refusals, overlay.</summary>
public class WageredGameBaseTests
{
    private const string Alice = World.Alice;

    private static (World World, TestGame Game) Setup(long balance = 1000, ulong maxBet = 500, Func<ulong>? entropy = null)
    {
        var world = new World(TestGame.Flag);
        world.Ledger.Seed(Alice, balance);
        return (world, new TestGame(maxBet, entropy ?? (() => 0x1111)));
    }

    /// <summary>The fake kv never expires TTLs, so tests that need a second play by the same user clear the cooldown key explicitly.</summary>
    private static void Cool(World world) => StateCooldown.Release(new KvStateStore(world.Kv), "testdrop", Alice);

    [Fact]
    public void Unrelated_chat_is_ignored()
    {
        var (world, game) = Setup();
        Assert.Null(world.Send(game, Alice, "hello there", "m1"));
        Assert.Null(world.Send(game, Alice, "!roll 5", "m2"));
        Assert.Equal(0, game.Plays);
    }

    [Fact]
    public void Flag_off_is_silent_and_touches_nothing()
    {
        var (world, game) = Setup();
        world.Flags.Overrides[TestGame.Flag] = false;
        Assert.Null(world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void Alias_command_is_accepted()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!td 10", "m1"));
        Assert.Equal(1, game.Plays);
    }

    [Fact]
    public void No_community_gets_a_reply_and_no_play()
    {
        var (world, game) = Setup();
        var reply = game.Run(World.Msg("!testdrop 10"), world.HostFor(Alice, "m1", community: null));
        Assert.Contains("needs a community", World.TextOf(reply!));
        Assert.Equal(0, game.Plays);
    }

    [Theory]
    [InlineData(IdentityErrorKind.NotLinked)]
    [InlineData(IdentityErrorKind.NotAMember)]
    public void Unlinked_actor_is_told_to_link(IdentityErrorKind kind)
    {
        var (world, game) = Setup();
        world.ActorFailure = new WaddleIdentityException(kind, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void Identity_denied_propagates()
    {
        var (world, game) = Setup();
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.Denied, "denied");
        Assert.Throws<WaddleIdentityException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
    }

    [Fact]
    public void Missing_stake_prints_usage()
    {
        var (world, game) = Setup();
        Assert.Equal("Usage: !testdrop amount|all|max", world.Send(game, Alice, "!testdrop", "m1"));
    }

    [Fact]
    public void Extra_arguments_are_refused_by_default_and_passed_through_when_allowed()
    {
        var (world, game) = Setup();
        Assert.Equal("Usage: !testdrop amount|all|max", world.Send(game, Alice, "!testdrop 10 extra", "m1"));
        Assert.Equal(0, game.Plays);
        game.AllowExtra = true;
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10 left right", "m2"));
        Assert.Equal(["left", "right"], game.LastExtra);
        game.ExtraRefusal = "nope";
        Assert.Equal("nope", world.Send(game, Alice, "!testdrop 10 left", "m3"));
    }

    [Theory]
    [InlineData("abc", "The amount must be a number, max, or all")]
    [InlineData("0", "The amount needs to be greater then 0")]
    [InlineData("-5", "The amount needs to be greater then 0")]
    [InlineData("5000", "You don't have that many.")]
    public void Bad_stakes_are_refused_without_taking_the_cooldown(string arg, string expected)
    {
        var (world, game) = Setup(balance: 100);
        Assert.Equal(expected, world.Send(game, Alice, $"!testdrop {arg}", "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10", "m2"));
    }

    [Fact]
    public void An_empty_wallet_is_refused_for_all_and_max()
    {
        var (world, game) = Setup(balance: 0);
        Assert.Equal("You don't have that many.", world.Send(game, Alice, "!testdrop all", "m1"));
        Assert.Equal("You don't have that many.", world.Send(game, Alice, "!testdrop max", "m2"));
    }

    [Fact]
    public void Over_the_per_bet_limit_is_clamped_not_refused()
    {
        var (world, game) = Setup(balance: 2000);
        world.Send(game, Alice, "!testdrop 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
    }

    [Fact]
    public void All_bets_the_whole_balance_up_to_the_limit()
    {
        var (world, game) = Setup(balance: 120);
        world.Send(game, Alice, "!testdrop all", "m1");
        Assert.Equal(120UL, world.Ledger.Moved.Single().Amount);
    }

    [Fact]
    public void Settlement_moves_exactly_one_wager_and_prints_the_balance()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!testdrop 100", "m1")!;
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(("wager", Alice, 100UL), world.Ledger.Moved[0]);
        Assert.Contains($"has {world.Ledger.Balance(Alice)} stars", text);
        Assert.Contains("{user:" + Alice + "}", text);
    }

    [Theory]
    [InlineData(0UL, "lost")]
    [InlineData(100UL, "got back")]
    [InlineData(300UL, "won")]
    public void Reply_wording_follows_the_payout(ulong payout, string verb)
    {
        var (world, game) = Setup();
        game.PayoutOverride = payout;
        var text = world.Send(game, Alice, "!testdrop 100", "m1")!;
        Assert.Contains($" {verb} ", text);
        Assert.Equal(1000 - 100 + (long)payout, world.Ledger.Balance(Alice));
    }

    [Fact]
    public void Redelivery_replays_the_same_seed_result_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!testdrop 100", "m1");
        var balance = world.Ledger.Balance(Alice);
        var second = world.Send(game, Alice, "!testdrop 100", "m1");
        Assert.Equal(first, second);
        Assert.Equal(balance, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Equal(2, game.Plays);
    }

    [Fact]
    public void Redelivery_does_not_draw_new_entropy()
    {
        var draws = 0;
        var (world, game) = Setup(entropy: () => (ulong)++draws);
        world.Send(game, Alice, "!testdrop 100", "m1");
        world.Send(game, Alice, "!testdrop 100", "m1");
        Assert.Equal(1, draws);
    }

    [Fact]
    public void A_replay_record_for_different_arguments_is_a_fresh_request()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!testdrop 100", "m1");
        // Same message id, different raw text: not a replay; the cooldown now blocks it silently.
        Assert.Null(world.Send(game, Alice, "!testdrop 200", "m1"));
        Assert.Single(world.Ledger.Moved);
    }

    /// <summary>An alias must share the primary command's cooldown, otherwise `!td` would bypass `!testdrop`'s cooldown.</summary>
    [Fact]
    public void Alias_shares_the_primary_commands_cooldown()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!td 10", "m2"));
        Assert.Equal(1, game.Plays);
    }

    [Fact]
    public void Cooldown_silently_drops_a_second_play_and_is_per_user()
    {
        var (world, game) = Setup();
        world.Ledger.Seed(World.Bob, 1000);
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!testdrop 10", "m2"));
        Assert.NotNull(world.Send(game, World.Bob, "!testdrop 10", "m3"));
    }

    [Fact]
    public void Different_messages_get_different_seeds()
    {
        var (world, game) = Setup(balance: 100_000);
        var seeds = new HashSet<string>();
        for (var i = 0; i < 6; i++)
        {
            world.Send(game, Alice, "!testdrop 1", $"m{i}");
            Cool(world);
            var push = world.Overlay.Pushes[^1].Payload;
            seeds.Add(JsonDocument.Parse(push).RootElement.GetProperty("extra").GetProperty("seed").GetString()!);
        }

        Assert.Equal(6, seeds.Count);
    }

    [Fact]
    public void Ledger_insufficient_funds_is_a_chat_refusal_and_releases_the_cooldown()
    {
        var (world, game) = Setup(balance: 1000);
        world.Ledger.BeforeCall = (_, _) => world.Ledger.Seed(Alice, 5);
        var text = world.Send(game, Alice, "!testdrop 100", "m1")!;
        Assert.Contains("you don't have 100 stars to bet", text);
        Assert.Empty(world.Ledger.Moved);
        world.Ledger.BeforeCall = null;
        world.Ledger.Seed(Alice, 1000);
        Assert.NotNull(world.Send(game, Alice, "!testdrop 100", "m2"));
    }

    [Fact]
    public void Ledger_over_cap_is_a_chat_refusal()
    {
        var (world, game) = Setup(balance: 5000, maxBet: 1000);
        var text = world.Send(game, Alice, "!testdrop 900", "m1")!;
        Assert.Contains("over the limit for stars", text);
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void Economy_denied_and_unavailable_propagate()
    {
        var (world, game) = Setup();
        world.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "down");
        Assert.Throws<WaddleEconomyException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Empty(world.Overlay.Pushes);
    }

    [Fact]
    public void Kv_failure_propagates()
    {
        var (world, game) = Setup();
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_blank_message_id_is_refused_loudly()
    {
        var (world, game) = Setup();
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!testdrop 10", " "));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_replay_record_from_another_config_is_refused()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!testdrop 10", "m1");
        var other = new TestGame(500, () => 1, configId: "test.v2");
        Assert.Throws<InvalidOperationException>(() => world.Send(other, Alice, "!testdrop 10", "m1"));
    }

    [Fact]
    public void A_malformed_recorded_seed_is_refused()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!testdrop 10", "m1");
        var key = world.Kv.SetCalls.Select(c => c.Key).Single(k => k.StartsWith("dg.testdrop.out.", StringComparison.Ordinal));
        var rec = JsonDocument.Parse(world.Kv.Get(key)!).RootElement;
        var tampered = new DropReplayRecord(rec.GetProperty("actor").GetString()!, rec.GetProperty("cmd").GetString()!, rec.GetProperty("args").GetString()!, 10, "nothex", "test.v1");
        world.Kv.Set(key, System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(tampered, DropCoreJsonContext.Default.DropReplayRecord)), 60);
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
    }

    [Fact]
    public void A_payout_over_the_host_cap_is_a_loud_bug_and_frees_the_cooldown()
    {
        var (world, game) = Setup();
        game.PayoutOverride = 100_000;
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Empty(world.Ledger.Moved);
        game.PayoutOverride = null;
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10", "m2"));
    }

    [Fact]
    public void A_throwing_simulation_frees_the_cooldown_and_records_nothing()
    {
        var (world, game) = Setup();
        game.Explode = true;
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
        game.Explode = false;
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10", "m2"));
        Assert.Single(world.Ledger.Moved);
    }

    [Fact]
    public void A_throwing_simulation_on_replay_propagates()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!testdrop 10", "m1");
        game.Explode = true;
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!testdrop 10", "m1"));
    }

    [Fact]
    public void The_overlay_gets_one_widget_registration_and_one_envelope_per_play()
    {
        var (world, game) = Setup(balance: 10_000);
        world.Send(game, Alice, "!testdrop 100", "m1");
        Cool(world);
        world.Send(game, Alice, "!testdrop 100", "m2");
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("testdrop", world.Overlay.Registered[0].WidgetId);
        Assert.Equal(2, world.Overlay.Pushes.Count);
        var (id, surface, payload) = world.Overlay.Pushes[0];
        Assert.Equal("testdrop", id);
        Assert.Equal(OverlaySurface.FullScreen, surface);
        var extra = JsonDocument.Parse(payload).RootElement.GetProperty("extra");
        Assert.Equal(1, extra.GetProperty("v").GetInt32());
        Assert.Equal("testdrop", extra.GetProperty("game").GetString());
        Assert.Equal("settled", extra.GetProperty("kind").GetString());
        Assert.Equal(Alice, extra.GetProperty("actor").GetString());
        Assert.Equal(100, extra.GetProperty("stake").GetInt32());
        Assert.True(SeedDerivation.TryParseHex(extra.GetProperty("seed").GetString(), out _));
        Assert.True(extra.GetProperty("data").TryGetProperty("roll", out _));
    }

    [Fact]
    public void A_replay_pushes_the_same_seed_again()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!testdrop 100", "m1");
        world.Send(game, Alice, "!testdrop 100", "m1");
        Assert.Equal(2, world.Overlay.Pushes.Count);
        Assert.Equal(world.Overlay.Pushes[0].Payload, world.Overlay.Pushes[1].Payload);
    }

    [Fact]
    public void A_failed_push_never_changes_the_settled_result()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "no overlay");
        var text = world.Send(game, Alice, "!testdrop 100", "m1");
        Assert.NotNull(text);
        Assert.Single(world.Ledger.Moved);
        Assert.Contains(world.Log.Writes, e => e.Message == "testdrop.overlay_push_failed" && e.FieldsJson.Contains("OVERLAY_DOWN", StringComparison.Ordinal));
        world.Overlay.PushFailure = null;
        Assert.Equal(text, world.Send(game, Alice, "!testdrop 100", "m1"));
    }

    [Fact]
    public void A_failed_widget_registration_is_logged_and_retried_next_play()
    {
        var (world, game) = Setup(balance: 10_000);
        world.Overlay.RegisterFailure = TransportErrorInfo.RetryableError("REG_DOWN", "no");
        Assert.NotNull(world.Send(game, Alice, "!testdrop 10", "m1"));
        Assert.Empty(world.Overlay.Registered);
        Assert.Single(world.Overlay.Pushes);
        Assert.Contains(world.Log.Writes, e => e.Message == "testdrop.overlay_register_failed");
        world.Overlay.RegisterFailure = null;
        Cool(world);
        world.Send(game, Alice, "!testdrop 10", "m2");
        Assert.Single(world.Overlay.Registered);
    }

    [Fact]
    public void Alias_falls_back_to_the_default_when_the_economy_tool_cannot_answer()
    {
        var (world, game) = Setup();
        world.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ => throw new WaddleToolException(ToolErrorKind.TargetUnavailable, "down");
        var text = world.Send(game, Alice, "!testdrop 100", "m1")!;
        Assert.Contains(EconomyServiceTools.DefaultAlias, text);
        Assert.Contains(world.Log.Writes, e => e.Message == "testdrop.alias_unavailable");
    }

    [Fact]
    public void Alias_falls_back_when_the_service_returns_a_blank_alias()
    {
        var (world, game) = Setup();
        world.Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ =>
            System.Text.Encoding.UTF8.GetBytes($"{{\"user\":\"{Alice}\",\"balance\":1,\"alias\":\" \"}}");
        Assert.Contains(EconomyServiceTools.DefaultAlias, world.Send(game, Alice, "!testdrop 100", "m1")!);
    }

    [Fact]
    public void Logs_carry_ids_and_counts_only()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!testdrop 100", "m1");
        var settled = world.Log.Writes.Single(e => e.Message == "testdrop.settled");
        Assert.Contains("stake", settled.FieldsJson);
        Assert.Contains("seed", settled.FieldsJson);
        Assert.DoesNotContain(Alice, settled.FieldsJson);
    }
}

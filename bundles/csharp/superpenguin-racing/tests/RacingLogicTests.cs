using System.Text;
using System.Text.Json;
using SuperPenguin.DropGameCore;
using SuperPenguin.DropGameCore.TestSupport;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Overlay;
using WaddleSdk.Scheduling;
using WaddleSdk.State;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinRacing.Tests;

/// <summary>An <see cref="IKvClient"/> that forwards to the world's kv and lets a test run code around specific calls (to model concurrent joiners).</summary>
internal sealed class HookKv(IKvClient inner) : IKvClient
{
    public Action<string>? AfterZeroIncrement { get; set; }

    public Action<string>? AfterSet { get; set; }

    public byte[]? Get(string key) => inner.Get(key);

    public void Set(string key, byte[] value, uint ttlSeconds)
    {
        inner.Set(key, value, ttlSeconds);
        AfterSet?.Invoke(key);
    }

    public void Delete(string key) => inner.Delete(key);

    public long Increment(string key, long delta, uint ttlSeconds)
    {
        var result = inner.Increment(key, delta, ttlSeconds);
        if (delta == 0)
        {
            AfterZeroIncrement?.Invoke(key);
        }

        return result;
    }
}

/// <summary>End-to-end `!race`: join window, stakes, deterministic settlement, void/refund, replay, cooldown, refusals and the overlay envelopes.</summary>
public class RacingLogicTests
{
    private const string Alice = World.Alice;
    private const string Bob = World.Bob;
    private const string Carol = World.Carol;
    private const string Dave = World.Dave;
    private const ulong Entropy = 7;
    private const ulong Start = 1_700_000_000_000UL;

    private static (World World, RacingLogic Game) Setup(RaceConfig? config = null, long balance = 1000)
    {
        var world = new World(RacingLogic.FlagKey);
        foreach (var user in new[] { Alice, Bob, Carol, Dave })
        {
            world.Ledger.Seed(user, balance);
        }

        return (world, new RacingLogic(() => Entropy, config));
    }

    private static string RaceId(string message, string actor, string platform = "twitch") => StateKey.Hash($"{message}|{platform}|{actor}|race");

    private static ulong SeedOf(string message, string actor) => SeedDerivation.Derive(RaceId(message, actor), "twitch", "race", "race", Entropy);

    private static KvStateStore Store(World world) => new(world.Kv);

    private static void Free(World world, string actor) => StateCooldown.Release(Store(world), "race", actor);

    private static void Advance(World world, ulong ms) => world.Clock.Now += ms;

    private static string Say(World world, RacingLogic game, string actor, string text, string message)
    {
        Free(world, actor);
        return world.Send(game, actor, text, message)!;
    }

    private static JsonElement Extra(World world, int index) => JsonDocument.Parse(world.Overlay.Pushes[index].Payload).RootElement.GetProperty("extra");

    [Fact]
    public void The_first_joiner_opens_the_window_takes_the_stake_and_records_the_seed()
    {
        var (world, game) = Setup();
        var text = world.Send(game, Alice, "!race 100", "m1")!;
        Assert.Contains($"{UserPlaceholder.For(Alice)} opened a race with 100 stars on the line", text);
        Assert.Contains("60s", text);
        Assert.Equal(900, world.Ledger.Balance(Alice));
        Assert.Equal(("wager", Alice, 100UL), Assert.Single(world.Ledger.Moved));
        var race = RacingStore.Current(Store(world))!;
        Assert.Equal(RaceId("m1", Alice), race.Id);
        Assert.Equal(SeedDerivation.ToHex(SeedOf("m1", Alice)), race.Seed);
        Assert.Equal(SeedDerivation.ToHex(Entropy), race.Entropy);
        Assert.Equal(RaceConfig.Id, race.Config);
        Assert.Equal(Start + 60_000, race.ClosesAt);
        Assert.Equal(RaceStatus.Open, race.Status);
        var task = Assert.Single(new TickScheduler(Store(world), world.Clock, "race").Pending());
        Assert.Equal(race.Id, task.Id);
        Assert.Equal(Start + 60_000, task.DueAtMillis);
    }

    [Fact]
    public void The_join_frame_carries_the_lobby_state_and_registers_the_widget_once()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!racing 50", "m2");
        Assert.Equal(2, world.Overlay.Pushes.Count);
        Assert.Single(world.Overlay.Registered);
        Assert.Equal("racing", world.Overlay.Registered[0].WidgetId);
        var first = Extra(world, 0);
        Assert.Equal("racing", first.GetProperty("game").GetString());
        Assert.Equal("join", first.GetProperty("kind").GetString());
        Assert.Equal(Alice, first.GetProperty("actor").GetString());
        Assert.Equal(100, first.GetProperty("stake").GetInt64());
        Assert.Equal(0, first.GetProperty("payout").GetInt64());
        var data = first.GetProperty("data");
        Assert.Equal(1, data.GetProperty("lane").GetInt32());
        Assert.Equal(1, data.GetProperty("racers").GetInt32());
        Assert.Equal(12, data.GetProperty("maxRacers").GetInt32());
        Assert.Equal("lobby", data.GetProperty("phase").GetString());
        Assert.Equal(2, Extra(world, 1).GetProperty("data").GetProperty("lane").GetInt32());
        Assert.Equal(2, Extra(world, 1).GetProperty("data").GetProperty("racers").GetInt32());
    }

    [Theory]
    [InlineData("race")]
    [InlineData("racing")]
    [InlineData("kart")]
    [InlineData("grandprix")]
    public void Every_alias_joins(string alias)
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, $"!{alias} 10", "m1"));
        Assert.Single(world.Ledger.Moved);
    }

    [Fact]
    public void A_second_joiner_lines_up_behind_without_a_new_window()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        var text = world.Send(game, Bob, "!race 50", "m2")!;
        Assert.Contains("lined up on the starting grid in position #2 with 50 stars", text);
        Assert.Equal(950, world.Ledger.Balance(Bob));
        Assert.Single(new TickScheduler(Store(world), world.Clock, "race").Pending());
        Assert.Equal(2, RacingStore.Roster(world.Kv, Store(world), RacingStore.Current(Store(world))!).Count);
    }

    [Fact]
    public void Joining_twice_is_refused_without_moving_money_and_releases_the_cooldown()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        Free(world, Alice);
        Assert.Contains("already registered", world.Send(game, Alice, "!race 100", "m2"));
        Assert.Single(world.Ledger.Moved);
        Assert.NotNull(world.Send(game, Alice, "!race", "m3"));
    }

    [Fact]
    public void A_join_inside_the_cooldown_is_dropped_silently()
    {
        var (world, game) = Setup();
        Assert.NotNull(world.Send(game, Alice, "!race 10", "m1"));
        Assert.Null(world.Send(game, Alice, "!race 10", "m2"));
        Assert.Equal(5U, RacingLogic.CooldownSeconds);
    }

    [Fact]
    public void A_full_grid_refuses_the_next_joiner_with_no_money_moved()
    {
        var (world, game) = Setup(RaceConfig.Default with { MaxRacers = 2 });
        world.Send(game, Alice, "!race 10", "m1");
        world.Send(game, Bob, "!race 10", "m2");
        Assert.Contains("starting grid is full for this race (2 max racers)", world.Send(game, Carol, "!race 10", "m3"));
        Assert.Equal(2, world.Ledger.Moved.Count);
    }

    [Fact]
    public void A_joiner_who_loses_the_last_seat_to_a_concurrent_join_is_refunded()
    {
        var (world, game) = Setup(RaceConfig.Default with { MaxRacers = 2 });
        world.Send(game, Alice, "!race 10", "m1");
        var hook = new HookKv(world.Kv);
        world.KvOverride = hook;
        hook.AfterZeroIncrement = key =>
        {
            hook.AfterZeroIncrement = null;
            world.Kv.Increment(key, 5, 60); // others took every remaining seat after our pre-check
        };
        var text = world.Send(game, Bob, "!race 10", "m2")!;
        Assert.Contains("grid is full for this race (2 max racers); your 10 stars were refunded", text);
        Assert.Equal(1000, world.Ledger.Balance(Bob));
        Assert.Contains(world.Ledger.Moved, m => m.Kind == "credit" && m.User == Bob && m.Amount == 10);
        Assert.Equal(1, world.Overlay.Pushes.Count);
    }

    [Fact]
    public void A_settlement_that_froze_its_result_without_the_joiner_refunds_them()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 10", "m1");
        var id = RaceId("m1", Alice);
        var hook = new HookKv(world.Kv);
        world.KvOverride = hook;
        hook.AfterSet = key =>
        {
            if (key.Contains(".p."))
            {
                hook.AfterSet = null;
                var frozen = new RaceResultRecord("too_few", "0000000000000000", 10, [new RacerResult(Alice, 10, 0, 0, 10, -1, false)], []);
                world.Kv.Set(RacingStore.ResultKey(id), JsonSerializer.SerializeToUtf8Bytes(frozen, RacingJsonContext.Default.RaceResultRecord), 60);
            }
        };
        var text = world.Send(game, Bob, "!race 10", "m2")!;
        Assert.Contains("closed before you could line up; your 10 stars were refunded", text);
        Assert.Equal(1000, world.Ledger.Balance(Bob));
        Assert.Null(world.Kv.Get(RacingStore.LaneKey(id, Bob)));
    }

    [Fact]
    public void A_redelivered_join_after_the_window_closed_unseated_refunds_exactly_once()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        var store = Store(world);
        var race = RacingStore.Current(store)!;
        world.Kv.Delete(RacingStore.LaneKey(race.Id, Alice));
        RacingStore.Save(store, race with { Status = RaceStatus.Done });
        var text = world.Send(game, Alice, "!race 100", "m1")!;
        Assert.Contains("closed before you could line up; your 100 stars were refunded", text);
        Assert.Equal(1000, world.Ledger.Balance(Alice));
        Assert.Equal(1, world.Ledger.Moved.Count(m => m.Kind == "credit"));
        world.Send(game, Alice, "!race 100", "m1");
        Assert.Equal(1, world.Ledger.Moved.Count(m => m.Kind == "credit"));
    }

    [Fact]
    public void The_window_closes_on_the_next_command_pays_the_pot_to_the_seed_derived_winner()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        Advance(world, 61_000);
        var text = world.Send(game, Carol, "!race 50", "m3")!;

        var outcome = RacingSimulation.Run(SeedOf("m1", Alice), 2, RaceConfig.Default);
        var winner = outcome.Order[0] == 0 ? Alice : Bob;
        var loser = winner == Alice ? Bob : Alice;
        Assert.Equal(1100, world.Ledger.Balance(winner));
        Assert.Equal(900, world.Ledger.Balance(loser));
        Assert.Contains($"Race finished! 1st {UserPlaceholder.For(winner)} (+200 stars), 2nd {UserPlaceholder.For(loser)}. 2 racers, pot 200 stars.", text);
        Assert.Contains("opened a race with 50 stars on the line", text);
        Assert.Equal(RaceStatus.Done, RacingStore.Load(Store(world), RaceId("m1", Alice))!.Status);
        Assert.Equal(RaceId("m3", Carol), RacingStore.Current(Store(world))!.Id);

        var race = Extra(world, 2);
        Assert.Equal("race", race.GetProperty("kind").GetString());
        Assert.Equal(SeedDerivation.ToHex(SeedOf("m1", Alice)), race.GetProperty("seed").GetString());
        Assert.Equal(winner, race.GetProperty("actor").GetString());
        Assert.Equal(200, race.GetProperty("stake").GetInt64());
        Assert.Equal(200, race.GetProperty("payout").GetInt64());
        var data = race.GetProperty("data");
        Assert.Equal(2, data.GetProperty("racers").GetArrayLength());
        Assert.Equal(outcome.TotalMs, data.GetProperty("track").GetProperty("totalMs").GetInt32());
        Assert.Equal(outcome.Events.Count, data.GetProperty("events").GetArrayLength());
        var lane0 = data.GetProperty("racers")[0];
        Assert.Equal(Alice, lane0.GetProperty("actor").GetString());
        Assert.Equal(outcome.Keyframes[0].Count, lane0.GetProperty("keyframes").GetArrayLength());
        Assert.Equal(outcome.FinishMs[0], lane0.GetProperty("finishMs").GetInt64());
        Assert.Equal(outcome.Order.ToArray(), data.GetProperty("order").EnumerateArray().Select(e => e.GetInt32()).ToArray());
    }

    [Fact]
    public void Four_racers_split_the_pot_sixty_thirty_ten_and_nothing_is_minted()
    {
        var (world, game) = Setup();
        string[] users = [Alice, Bob, Carol, Dave];
        for (var i = 0; i < 4; i++)
        {
            Say(world, game, users[i], "!race 100", $"j{i}");
        }

        Advance(world, 61_000);
        Free(world, Alice);
        world.Send(game, Alice, "!race", "tick");
        var outcome = RacingSimulation.Run(SeedOf("j0", Alice), 4, RaceConfig.Default);
        var expected = new Dictionary<string, long>();
        for (var lane = 0; lane < 4; lane++)
        {
            var place = outcome.Order.ToList().IndexOf(lane);
            expected[users[lane]] = 900 + new long[] { 240, 120, 40, 0 }[place];
        }

        foreach (var user in users)
        {
            Assert.Equal(expected[user], world.Ledger.Balance(user));
        }

        Assert.Equal(4000, world.Ledger.Supply);
        Assert.Equal(3, world.Ledger.Moved.Count(m => m.Kind == "credit"));
    }

    [Fact]
    public void Three_racers_split_seventy_thirty()
    {
        var (world, game) = Setup();
        string[] users = [Alice, Bob, Carol];
        for (var i = 0; i < 3; i++)
        {
            Say(world, game, users[i], "!race 100", $"j{i}");
        }

        Advance(world, 60_000);
        world.Send(game, Dave, "!race", "tick");
        Assert.Equal(4000, world.Ledger.Supply);
        Assert.Equal(new ulong[] { 210, 90 }, world.Ledger.Moved.Where(m => m.Kind == "credit").Select(m => m.Amount).OrderByDescending(a => a).ToArray());
    }

    [Fact]
    public void Payout_credits_use_keys_bound_to_the_opening_message_not_the_settling_event()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        Advance(world, 61_000);
        world.Send(game, Carol, "!race 10", "m9-settling-event");
        var credit = Assert.Single(world.Tools.Calls, c => c.Tool == "adjust_balance");
        var winner = RacingSimulation.Run(SeedOf("m1", Alice), 2, RaceConfig.Default).Order[0] == 0 ? Alice : Bob;
        Assert.Equal(StateKey.Hash("m1" + "\u001f" + "race.pay" + "\u001f" + "credit" + "\u001f" + winner), credit.Key);
    }

    [Fact]
    public void A_window_picked_up_after_the_stale_limit_is_voided_and_every_stake_refunded()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 60", "m2");
        Advance(world, 60_000 + (RaceConfig.Default.StaleVoidSeconds * 1000UL) + 1);
        var text = world.Send(game, Carol, "!race 10", "m3")!;
        Assert.Contains("The race was cancelled (the join window was settled too long after it closed); 2 stake(s) refunded.", text);
        Assert.Equal(1000, world.Ledger.Balance(Alice));
        Assert.Equal(1000, world.Ledger.Balance(Bob));
        Assert.Equal(RaceStatus.Void, RacingStore.Load(Store(world), RaceId("m1", Alice))!.Status);
        var voided = Extra(world, 2);
        Assert.Equal("void", voided.GetProperty("kind").GetString());
        Assert.Equal("late", voided.GetProperty("data").GetProperty("reason").GetString());
        Assert.Equal(2, voided.GetProperty("data").GetProperty("refunds").GetArrayLength());
    }

    [Fact]
    public void A_window_exactly_at_the_stale_limit_still_races()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        Advance(world, 60_000 + (RaceConfig.Default.StaleVoidSeconds * 1000UL));
        Assert.Contains("Race finished!", world.Send(game, Carol, "!race 10", "m3"));
    }

    [Fact]
    public void A_lone_racer_is_refunded_when_the_window_closes()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        Advance(world, 61_000);
        var text = world.Send(game, Bob, "!race 10", "m2")!;
        Assert.Contains("it needed at least two racers); 1 stake(s) refunded.", text);
        Assert.Equal(1000, world.Ledger.Balance(Alice));
        Assert.Equal("too_few", Extra(world, 1).GetProperty("data").GetProperty("reason").GetString());
    }

    [Fact]
    public void An_empty_orphan_window_closes_silently()
    {
        var (world, game) = Setup();
        world.Ledger.BeforeCall = (kind, _) => throw new WaddleEconomyException(EconomyErrorKind.Denied, "wager denied");
        Assert.Throws<WaddleEconomyException>(() => world.Send(game, Alice, "!race 100", "m1"));
        world.Ledger.BeforeCall = null;
        Advance(world, 61_000);
        var text = world.Send(game, Bob, "!race 10", "m2")!;
        Assert.DoesNotContain("cancelled", text);
        Assert.Contains("opened a race", text);
        Assert.Equal(RaceStatus.Void, RacingStore.Load(Store(world), RaceId("m1", Alice))!.Status);
        Assert.Equal(RaceId("m2", Bob), RacingStore.Current(Store(world))!.Id);
    }

    [Fact]
    public void A_dnf_field_still_pays_by_distance_rank()
    {
        var (world, game) = Setup(RaceConfig.Default with { MaxRaceMillis = 3000 });
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        Advance(world, 61_000);
        Assert.Contains("Race finished!", world.Send(game, Carol, "!race 10", "m3"));
        Assert.Equal(1100, new[] { world.Ledger.Balance(Alice), world.Ledger.Balance(Bob) }.Max());
        var raceExtra = Extra(world, 2).GetProperty("data").GetProperty("racers");
        Assert.True(raceExtra[0].GetProperty("dnf").GetBoolean());
    }

    [Fact]
    public void A_failed_settlement_is_retried_from_the_next_event_and_pays_each_racer_once()
    {
        var (world, game) = Setup();
        string[] users = [Alice, Bob, Carol, Dave];
        for (var i = 0; i < 4; i++)
        {
            Say(world, game, users[i], "!race 100", $"j{i}");
        }

        Advance(world, 61_000);
        world.Ledger.BeforeCall = (kind, ordinal) =>
        {
            if (kind == "credit" && ordinal == 1)
            {
                throw new WaddleEconomyException(EconomyErrorKind.Unavailable, "economy down");
            }
        };
        Free(world, Alice);
        Assert.Throws<WaddleEconomyException>(() => world.Send(game, Alice, "!race", "t1"));
        Assert.Equal(1, world.Ledger.Moved.Count(m => m.Kind == "credit"));
        Assert.Equal(RaceStatus.Settling, RacingStore.Load(Store(world), RaceId("j0", Alice))!.Status);

        world.Ledger.BeforeCall = null;
        Advance(world, 1000);
        Free(world, Alice);
        var text = world.Send(game, Alice, "!race", "t2")!;
        Assert.Contains("Race finished!", text);
        Assert.Equal(3, world.Ledger.Moved.Count(m => m.Kind == "credit"));
        Assert.Equal(4000, world.Ledger.Supply);
        Assert.Equal(RaceStatus.Done, RacingStore.Load(Store(world), RaceId("j0", Alice))!.Status);
    }

    [Fact]
    public void A_claimed_but_never_settled_window_is_picked_up_by_the_backstop()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        new TickScheduler(Store(world), world.Clock, "race").Cancel(RaceId("m1", Alice));
        Advance(world, 61_000);
        Assert.Contains("Race finished!", world.Send(game, Carol, "!race 10", "m3"));
        Assert.Equal(RaceStatus.Done, RacingStore.Load(Store(world), RaceId("m1", Alice))!.Status);
    }

    [Fact]
    public void A_task_whose_race_record_expired_is_dropped_with_a_warning_and_foreign_tasks_are_ignored()
    {
        var (world, game) = Setup();
        var sched = new TickScheduler(Store(world), world.Clock, "race");
        sched.Schedule(new ScheduledTask("gone", Start - 1, RacingSettlement.CloseKind, string.Empty));
        sched.Schedule(new ScheduledTask("other", Start - 1, "unrelated", string.Empty));
        Assert.NotNull(world.Send(game, Alice, "!race 10", "m1"));
        Assert.Contains(world.Log.Writes, w => w.Message == "racing.window_expired");
    }

    [Fact]
    public void A_current_pointer_to_a_missing_record_is_not_a_race()
    {
        var (world, game) = Setup();
        RacingStore.SetCurrent(Store(world), "deadbeef");
        Assert.Contains("opened a race", world.Send(game, Alice, "!race 10", "m1"));
    }

    [Fact]
    public void A_newer_race_keeps_its_pointer_when_an_older_one_settles()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 10", "m1");
        var store = Store(world);
        RacingStore.Save(store, new RaceRecord("newer", Start, Start + 200_000, "mx", "twitch", SeedDerivation.ToHex(1), SeedDerivation.ToHex(Entropy), RaceConfig.Id, 3, 12, RaceStatus.Open));
        RacingStore.SetCurrent(store, "newer");
        Advance(world, 61_000);
        world.Send(game, Bob, "!race 10", "m2");
        Assert.Equal("newer", RacingStore.Current(store)!.Id);
        Assert.Equal(RaceStatus.Void, RacingStore.Load(store, RaceId("m1", Alice))!.Status);
    }

    [Fact]
    public void A_race_recorded_under_another_game_config_is_refused()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        var store = Store(world);
        RacingStore.Save(store, RacingStore.Current(store)! with { Config = "racing.v0" });
        Advance(world, 61_000);
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Bob, "!race 10", "m2"));
    }

    [Fact]
    public void A_malformed_recorded_seed_is_refused()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        var store = Store(world);
        RacingStore.Save(store, RacingStore.Current(store)! with { Seed = "zz" });
        Advance(world, 61_000);
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Carol, "!race 10", "m3"));
    }

    [Fact]
    public void Redelivery_replays_the_same_join_and_moves_nothing()
    {
        var (world, game) = Setup();
        var first = world.Send(game, Alice, "!race 100", "m1");
        Assert.Equal(first, world.Send(game, Alice, "!race 100", "m1"));
        Assert.Equal(900, world.Ledger.Balance(Alice));
        Assert.Single(world.Ledger.Moved);
        Assert.Single(RacingStore.Roster(world.Kv, Store(world), RacingStore.Current(Store(world))!));
        Assert.Equal(1, world.Kv.Increment(RacingStore.CountKey(RaceId("m1", Alice)), 0, 60));
    }

    [Fact]
    public void A_replay_with_a_different_amount_is_a_fresh_request()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        Free(world, Alice);
        Assert.Contains("already registered", world.Send(game, Alice, "!race 200", "m1"));
        Assert.Equal(900, world.Ledger.Balance(Alice));
    }

    [Fact]
    public void A_replay_under_another_game_config_or_without_a_race_is_refused()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        var store = Store(world);
        var key = DropReplayGuard.KeyFor("racing", World.Msg("!race 100"), world.HostFor(Alice, "m1"), Alice, "race");
        var record = store.GetJson(key, DropCoreJsonContext.Default.DropReplayRecord)!;

        store.SetJson(key, record with { Config = "racing.v0" }, DropCoreJsonContext.Default.DropReplayRecord, 60);
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!race 100", "m1"));

        store.SetJson(key, record with { Extra = null }, DropCoreJsonContext.Default.DropReplayRecord, 60);
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!race 100", "m1"));

        store.SetJson(key, record with { Extra = "nosuchrace" }, DropCoreJsonContext.Default.DropReplayRecord, 60);
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!race 100", "m1"));
    }

    [Fact]
    public void Max_and_all_stakes_and_the_500_clamp()
    {
        var (world, game) = Setup(balance: 5000);
        world.Send(game, Alice, "!race 900", "m1");
        Assert.Equal(500UL, world.Ledger.Moved.Single().Amount);
        world.Send(game, Bob, "!race all", "m2");
        world.Send(game, Carol, "!race max", "m3");
        Assert.Equal(new ulong[] { 500, 500, 500 }, world.Ledger.Moved.Select(m => m.Amount).ToArray());
    }

    [Theory]
    [InlineData("!race 10 20", "To race do !race Amount/All/Max")]
    [InlineData("!race abc", "The amount must be a number, max, or all")]
    [InlineData("!race 0", "The amount needs to be greater then 0")]
    [InlineData("!race 99999", "You don't have that many.")]
    public void Refusals_use_the_shared_wording_and_move_no_money(string text, string contains)
    {
        var (world, game) = Setup();
        Assert.Contains(contains, world.Send(game, Alice, text, "m1"));
        Assert.Empty(world.Ledger.Moved);
        Assert.Empty(world.Overlay.Pushes);
        Assert.Null(RacingStore.Current(Store(world)));
    }

    [Fact]
    public void A_bare_race_shows_usage_or_the_open_window()
    {
        var (world, game) = Setup();
        Assert.Contains("To race do !race Amount/All/Max", world.Send(game, Alice, "!race", "m1"));
        world.Send(game, Alice, "!race 10", "m2");
        Advance(world, 12_000);
        var status = world.Send(game, Bob, "!race", "m3")!;
        Assert.Contains("A race is open with 1 racer(s), the green flag drops in 48s.", status);
    }

    [Fact]
    public void Flag_off_other_commands_no_community_and_unlinked_paths()
    {
        var (world, game) = Setup();
        world.Flags.Overrides[RacingLogic.FlagKey] = false;
        Assert.Null(world.Send(game, Alice, "!race 10", "m1"));
        world.Flags.Overrides[RacingLogic.FlagKey] = true;
        Assert.Null(world.Send(game, Alice, "!roll 10", "m2"));
        Assert.Null(world.Send(game, Alice, "hello", "m2b"));
        Assert.Contains("needs a community", World.TextOf(game.Run(World.Msg("!race 10"), world.HostFor(Alice, "m3", community: null))!));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotLinked, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!race 10", "m4"));
        world.ActorFailure = new WaddleIdentityException(IdentityErrorKind.NotAMember, "x");
        Assert.Contains("Link your account", world.Send(game, Alice, "!race 10", "m5"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_blank_message_id_is_refused_loudly()
    {
        var (world, game) = Setup();
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Alice, "!race 10", ""));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void An_over_cap_wager_answers_in_chat_undoes_the_opened_window_and_releases_the_cooldown()
    {
        var (world, game) = Setup();
        world.Ledger.MaxBetCap = 50;
        var text = world.Send(game, Alice, "!race 100", "m1")!;
        Assert.Contains("that bet is over the limit for stars", text);
        Assert.Empty(world.Ledger.Moved);
        Assert.Null(RacingStore.Current(Store(world)));
        Assert.Empty(new TickScheduler(Store(world), world.Clock, "race").Pending());
        Assert.Null(world.Kv.Get(RacingStore.MemberKey(RaceId("m1", Alice), Alice)));
        world.Ledger.MaxBetCap = 500;
        Assert.Contains("opened a race", world.Send(game, Alice, "!race 100", "m2"));
    }

    [Fact]
    public void An_insufficient_funds_wager_for_a_later_joiner_keeps_the_open_window()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 10", "m1");
        world.Ledger.Seed(Bob, 5);
        world.Tools.Handlers[(EconomyServiceTools.Target, "get_balance")] = _ =>
            Encoding.UTF8.GetBytes($"{{\"user\":\"{Bob}\",\"balance\":1000}}");
        var text = world.Send(game, Bob, "!race 100", "m2")!;
        Assert.Contains($"{UserPlaceholder.For(Bob)}, you don't have 100 stars to bet.", text);
        Assert.NotNull(RacingStore.Current(Store(world)));
        Assert.Single(new TickScheduler(Store(world), world.Clock, "race").Pending());
    }

    [Fact]
    public void An_insufficient_funds_wager_for_the_opener_with_a_concurrent_joiner_keeps_the_window()
    {
        var (world, game) = Setup();
        world.Ledger.Seed(Alice, 5);
        world.Tools.Handlers[(EconomyServiceTools.Target, "get_balance")] = _ =>
            Encoding.UTF8.GetBytes($"{{\"user\":\"{Alice}\",\"balance\":1000}}");
        var hook = new HookKv(world.Kv);
        world.KvOverride = hook;
        hook.AfterSet = key =>
        {
            if (key == RacingStore.CurrentKey)
            {
                hook.AfterSet = null;
                world.Kv.Increment(RacingStore.CountKey(RaceId("m1", Alice)), 1, 60); // somebody seated meanwhile
            }
        };
        Assert.Contains("you don't have 100 stars", world.Send(game, Alice, "!race 100", "m1"));
        Assert.NotNull(RacingStore.Current(Store(world)));
    }

    [Fact]
    public void Denied_economy_and_kv_failures_propagate()
    {
        var (world, game) = Setup();
        world.Ledger.BeforeCall = (_, _) => throw new WaddleEconomyException(EconomyErrorKind.Denied, "denied");
        Assert.Throws<WaddleEconomyException>(() => world.Send(game, Alice, "!race 10", "m1"));
        world.Ledger.BeforeCall = null;
        world.KvOverride = new FaultyKvClient(new InvalidOperationException("kv down"));
        Assert.Throws<InvalidOperationException>(() => world.Send(game, Bob, "!race 10", "m2"));
        Assert.Empty(world.Ledger.Moved);
    }

    [Fact]
    public void A_failed_overlay_push_never_reverses_the_stake_or_the_payout()
    {
        var (world, game) = Setup();
        world.Overlay.PushFailure = TransportErrorInfo.Fatal("OVERLAY_DOWN", "x");
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        Advance(world, 61_000);
        Assert.Contains("Race finished!", world.Send(game, Carol, "!race 10", "m3"));
        Assert.Contains(world.Log.Writes, w => w.Message == "racing.overlay_push_failed");
        Assert.Equal(1100, new[] { world.Ledger.Balance(Alice), world.Ledger.Balance(Bob) }.Max());
    }

    [Fact]
    public void Default_entropy_comes_from_the_host_rng_so_two_races_have_different_seeds()
    {
        var world = new World(RacingLogic.FlagKey);
        world.Ledger.Seed(Alice, 10_000);
        var game = new RacingLogic();
        world.Send(game, Alice, "!race 10", "m1");
        var first = RacingStore.Current(Store(world))!.Seed;
        Free(world, Alice);
        Advance(world, 1_000_000);
        world.Send(game, Alice, "!race 10", "m2");
        Assert.NotEqual(first, RacingStore.Current(Store(world))!.Seed);
    }

    [Fact]
    public void A_race_is_replayable_from_the_recorded_seed_config_and_roster()
    {
        var (world, game) = Setup();
        world.Send(game, Alice, "!race 100", "m1");
        world.Send(game, Bob, "!race 100", "m2");
        var store = Store(world);
        var race = RacingStore.Current(store)!;
        Advance(world, 61_000);
        world.Send(game, Carol, "!race 10", "m3");
        var recorded = store.GetJson(RacingStore.ResultKey(race.Id), RacingJsonContext.Default.RaceResultRecord)!;
        Assert.True(SeedDerivation.TryParseHex(race.Seed, out var seed));
        var again = RacingSimulation.Run(seed, 2, RaceConfig.Default with { Laps = race.Laps });
        Assert.Equal(again.Order[0], recorded.Racers.Single(r => r.Place == 1).Lane);
        Assert.Single(recorded.Paid);
    }
}

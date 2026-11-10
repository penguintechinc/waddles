using Xunit;

namespace WaddleBundleSuperpenguinFishing.Tests;

public class TournamentTests
{
    private static TournamentTemplate T(ScoreCategory score = ScoreCategory.Largest, int period = 60, int duration = 30, int offset = 10,
        string[]? fish = null, string[]? categories = null, string? target = null, TournamentReward[]? rewards = null) =>
        new("t", "T", "d", score, target, fish ?? [], categories ?? [], period, duration, offset, rewards ?? []);

    private static ulong Min(long minute) => (ulong)minute * 60_000UL;

    [Theory]
    [InlineData(609, false)] // minute 9 of the hour: before the window
    [InlineData(610, true)]  // 10 = start
    [InlineData(639, true)]
    [InlineData(640, false)] // 40 = end (exclusive)
    public void a_window_is_active_between_its_offset_and_offset_plus_duration(long minute, bool active)
    {
        // Minute 600 is an hour boundary (600 % 60 == 0).
        Assert.Equal(active, FishingTournament.ActiveWindow(T(), Min(minute)) is not null);
    }

    [Fact]
    public void the_window_id_is_stable_and_carries_the_start_minute()
    {
        var w = FishingTournament.ActiveWindow(T(), Min(615))!;
        Assert.Equal(610, w.StartMinute);
        Assert.Equal(640, w.EndMinute);
        Assert.Equal("t.610", w.Id);
        Assert.Equal(Min(640), w.EndMillis);
        Assert.Equal(w, FishingTournament.ActiveWindow(T(), Min(639)));
    }

    [Fact]
    public void a_window_may_spill_over_into_the_next_period()
    {
        // period 60, offset 50, duration 30: runs :50 to :20 of the next hour.
        var t = T(period: 60, duration: 30, offset: 50);
        var w = FishingTournament.ActiveWindow(t, Min(605))!; // 600 + 5 -> previous period's window (start 590)
        Assert.Equal(590, w.StartMinute);
        Assert.Equal(620, w.EndMinute);
        Assert.NotNull(FishingTournament.ActiveWindow(t, Min(655)));
        Assert.Null(FishingTournament.ActiveWindow(t, Min(625)));
    }

    [Theory]
    [InlineData(0, 30, 0)]
    [InlineData(60, 0, 0)]
    [InlineData(60, 61, 0)]
    [InlineData(60, 30, -1)]
    [InlineData(60, 30, 60)]
    public void invalid_geometry_never_activates_and_fails_validation(int period, int duration, int offset)
    {
        var t = T(period: period, duration: duration, offset: offset);
        Assert.Null(FishingTournament.ActiveWindow(t, Min(1000)));
        Assert.Contains("invalid window geometry", FishingTournament.Validate(t));
    }

    [Fact]
    public void validate_rejects_rewards_outside_the_board_or_negative()
    {
        Assert.Null(FishingTournament.Validate(T(rewards: [new TournamentReward(1, 5, 5)])));
        Assert.NotNull(FishingTournament.Validate(T(rewards: [new TournamentReward(0, 5, 5)])));
        Assert.NotNull(FishingTournament.Validate(T(rewards: [new TournamentReward(FishingTournament.BoardSize + 1, 5, 5)])));
        Assert.NotNull(FishingTournament.Validate(T(rewards: [new TournamentReward(1, -1, 5)])));
        Assert.NotNull(FishingTournament.Validate(T(rewards: [new TournamentReward(1, 5, -1)])));
    }

    [Fact]
    public void eligibility_follows_upstream_all_by_default_else_fish_or_category()
    {
        var pike = Cat.Fish("pike", categories: ["Predator", "game"]);
        var carp = Cat.Fish("carp", categories: ["bottom"]);
        Assert.True(FishingTournament.IsEligible(T(), carp));
        Assert.True(FishingTournament.IsEligible(T(fish: ["carp"]), carp));
        Assert.False(FishingTournament.IsEligible(T(fish: ["carp"]), pike));
        Assert.True(FishingTournament.IsEligible(T(categories: ["predator"]), pike)); // case-insensitive
        Assert.False(FishingTournament.IsEligible(T(categories: ["predator"]), carp));
        Assert.True(FishingTournament.IsEligible(T(fish: ["carp"], categories: ["predator"]), pike)); // either selection suffices
    }

    [Fact]
    public void specific_fish_tournaments_only_count_the_target()
    {
        var pike = Cat.Fish("pike");
        var carp = Cat.Fish("carp");
        var t = T(ScoreCategory.SpecificFish, target: "pike");
        Assert.True(FishingTournament.Counts(t, pike));
        Assert.False(FishingTournament.Counts(t, carp));
        Assert.True(FishingTournament.Counts(T(ScoreCategory.SpecificFish), carp)); // no target set: every eligible fish
        Assert.True(FishingTournament.Counts(T(ScoreCategory.Largest, target: "pike"), carp));
        Assert.False(FishingTournament.Counts(T(ScoreCategory.Largest, fish: ["pike"]), carp));
    }

    private static CastResult Catch(double weight, int gold, int stars = 1) =>
        new(CastKind.Caught, "f", stars, weight, gold, 0, [], []);

    [Fact]
    public void aggregates_fold_every_scoring_rule()
    {
        var a = new TournamentAggregate();
        FishingTournament.Record(a, Catch(4, 10, 1));
        FishingTournament.Record(a, Catch(10, 5, 3));
        FishingTournament.Record(a, Catch(1, 40, 2));

        Assert.Equal(10, FishingTournament.Score(a, ScoreCategory.Largest));
        Assert.Equal(10, FishingTournament.Score(a, ScoreCategory.SpecificFish));
        Assert.Equal(40, FishingTournament.Score(a, ScoreCategory.MostValuable));
        Assert.Equal(1, FishingTournament.Score(a, ScoreCategory.Smallest));
        Assert.Equal(5, FishingTournament.Score(a, ScoreCategory.Average));
        Assert.Equal(3, FishingTournament.Score(a, ScoreCategory.MostCatches));
        Assert.Equal(15, FishingTournament.Score(a, ScoreCategory.TotalWeight));
        Assert.Equal(6, a.TotalStars);
        Assert.Equal(0, FishingTournament.Score(new TournamentAggregate(), ScoreCategory.Average));
        Assert.Equal(0, FishingTournament.Score(a, (ScoreCategory)99));
    }

    private static TournamentAggregate Agg(int count, double max = 0, double min = 0, double sum = 0, int stars = 0) =>
        new() { Count = count, MaxWeight = max, MinWeight = min, TotalWeight = sum, TotalStars = stars };

    [Fact]
    public void the_board_ranks_descending_with_fewer_catches_then_fewer_stars_as_tiebreaks()
    {
        var board = new TournamentBoard();
        FishingTournament.Upsert(board, "b", Agg(5, max: 10, stars: 9), ScoreCategory.Largest);
        FishingTournament.Upsert(board, "a", Agg(3, max: 10, stars: 9), ScoreCategory.Largest);
        FishingTournament.Upsert(board, "c", Agg(3, max: 10, stars: 4), ScoreCategory.Largest);
        FishingTournament.Upsert(board, "d", Agg(1, max: 12), ScoreCategory.Largest);
        FishingTournament.Upsert(board, "e", Agg(1, max: 1), ScoreCategory.Largest);

        Assert.Equal(["d", "c", "a", "b", "e"], board.Rows.Select(r => r.User));

        // Upserting a user again replaces their row rather than duplicating it.
        FishingTournament.Upsert(board, "e", Agg(2, max: 99), ScoreCategory.Largest);
        Assert.Equal(5, board.Rows.Count);
        Assert.Equal("e", board.Rows[0].User);
    }

    [Fact]
    public void the_smallest_rule_ranks_ascending_and_ties_break_on_user_id_for_determinism()
    {
        var board = new TournamentBoard();
        FishingTournament.Upsert(board, "z", Agg(1, min: 2), ScoreCategory.Smallest);
        FishingTournament.Upsert(board, "y", Agg(1, min: 0.5), ScoreCategory.Smallest);
        FishingTournament.Upsert(board, "x", Agg(1, min: 2), ScoreCategory.Smallest);
        Assert.Equal(["y", "x", "z"], board.Rows.Select(r => r.User));
    }

    [Fact]
    public void the_board_is_bounded()
    {
        var board = new TournamentBoard();
        for (var i = 0; i < FishingTournament.BoardSize + 10; i++)
        {
            FishingTournament.Upsert(board, "u" + i.ToString("D3"), Agg(1, max: i), ScoreCategory.Largest);
        }

        Assert.Equal(FishingTournament.BoardSize, board.Rows.Count);
        Assert.Equal("u" + (FishingTournament.BoardSize + 9).ToString("D3"), board.Rows[0].User);
        Assert.DoesNotContain(board.Rows, r => r.User == "u000");
    }

    [Fact]
    public void winners_are_the_nth_ranked_row_per_reward_and_empty_or_zero_rewards_are_skipped()
    {
        var board = new TournamentBoard();
        FishingTournament.Upsert(board, "a", Agg(1, max: 9), ScoreCategory.Largest);
        FishingTournament.Upsert(board, "b", Agg(1, max: 5), ScoreCategory.Largest);
        var t = T(rewards: [new(2, 10, 0), new(1, 0, 50), new(3, 5, 5), new(2 + 0, 0, 0)]);

        var winners = FishingTournament.Winners(t, board);

        Assert.Equal([(1, "a"), (2, "b")], winners.Select(w => (w.Reward.Placement, w.Winner.User)));
        Assert.Empty(FishingTournament.Winners(T(rewards: [new TournamentReward(1, 1, 1)]), new TournamentBoard()));
    }
}

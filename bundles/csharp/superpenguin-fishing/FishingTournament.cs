namespace WaddleBundleSuperpenguinFishing;

/// <summary>One concrete run of a <see cref="TournamentTemplate"/>: a UTC time window with a stable id.</summary>
public sealed record TournamentWindow(string TemplateId, long StartMinute, long EndMinute)
{
    /// <summary>Stable id (state keys and settlement idempotency): the template id plus the window's start minute.</summary>
    public string Id => $"{TemplateId}.{StartMinute}";

    /// <summary>Window end as epoch milliseconds (the settlement due time).</summary>
    public ulong EndMillis => (ulong)EndMinute * 60_000UL;
}

/// <summary>
/// Tournament mathematics ported from upstream `FishingService` (window activity, `FishingCalculations.IsFishEligible`,
/// `CalculateStandings`, reward placement selection) and reshaped for per-invocation limits: scores are folded into one
/// <see cref="TournamentAggregate"/> per participant and a bounded <see cref="TournamentBoard"/> of the top
/// <see cref="BoardSize"/> rows is kept per window, so a catch costs O(1) host calls and settlement never scans participants.
/// </summary>
public static class FishingTournament
{
    /// <summary>Rows kept per window board (must exceed the deepest reward placement).</summary>
    public const int BoardSize = 25;

    /// <summary>The window of <paramref name="template"/> that is active at <paramref name="nowMillis"/>, or null when none is.</summary>
    public static TournamentWindow? ActiveWindow(TournamentTemplate template, ulong nowMillis)
    {
        if (template.PeriodMinutes < 1 || template.DurationMinutes < 1 || template.DurationMinutes > template.PeriodMinutes
            || template.OffsetMinutes < 0 || template.OffsetMinutes >= template.PeriodMinutes)
        {
            return null;
        }

        var minute = (long)(nowMillis / 60_000UL);
        var periodStart = minute - (minute % template.PeriodMinutes);
        // A window may spill past its period boundary, so the previous period's window can still be running.
        foreach (var baseMinute in new[] { periodStart, periodStart - template.PeriodMinutes })
        {
            var start = baseMinute + template.OffsetMinutes;
            var end = start + template.DurationMinutes;
            if (minute >= start && minute < end)
            {
                return new TournamentWindow(template.Id, start, end);
            }
        }

        return null;
    }

    /// <summary>Upstream `IsFishEligible`: no selections means every fish; otherwise the fish id or one of its categories must match.</summary>
    public static bool IsEligible(TournamentTemplate template, FishType fish)
    {
        if (template.EligibleFishIds.Length == 0 && template.EligibleCategories.Length == 0)
        {
            return true;
        }

        return template.EligibleFishIds.Contains(fish.Id)
            || fish.Categories.Any(c => template.EligibleCategories.Any(s => string.Equals(s, c, StringComparison.OrdinalIgnoreCase)));
    }

    /// <summary>True when a catch of <paramref name="fish"/> counts toward the template (eligible, and the target fish for `SpecificFish`).</summary>
    public static bool Counts(TournamentTemplate template, FishType fish) =>
        IsEligible(template, fish) && (template.Score != ScoreCategory.SpecificFish || template.TargetFishId is null || template.TargetFishId == fish.Id);

    /// <summary>Folds one catch into a participant's aggregate.</summary>
    public static void Record(TournamentAggregate aggregate, CastResult cast)
    {
        aggregate.Count++;
        aggregate.MaxWeight = Math.Max(aggregate.MaxWeight, cast.Weight);
        aggregate.MinWeight = Math.Min(aggregate.MinWeight, cast.Weight);
        aggregate.TotalWeight += cast.Weight;
        aggregate.MaxGold = Math.Max(aggregate.MaxGold, cast.Gold);
        aggregate.TotalStars += cast.Stars;
    }

    /// <summary>The participant's score under <paramref name="category"/> (upstream `CalculateStandings` score selection).</summary>
    public static double Score(TournamentAggregate a, ScoreCategory category) => category switch
    {
        ScoreCategory.Largest or ScoreCategory.SpecificFish => a.MaxWeight,
        ScoreCategory.MostValuable => a.MaxGold,
        ScoreCategory.Smallest => a.MinWeight,
        ScoreCategory.Average => a.Count == 0 ? 0 : a.TotalWeight / a.Count,
        ScoreCategory.MostCatches => a.Count,
        ScoreCategory.TotalWeight => a.TotalWeight,
        _ => 0,
    };

    /// <summary>Upserts a participant into the bounded board and re-ranks it (upstream ordering: score, then fewer catches, then fewer stars).</summary>
    public static void Upsert(TournamentBoard board, string user, TournamentAggregate aggregate, ScoreCategory category)
    {
        board.Rows.RemoveAll(r => r.User == user);
        board.Rows.Add(new BoardEntry(user, Score(aggregate, category), aggregate.Count, aggregate.TotalStars));
        var ordered = category == ScoreCategory.Smallest
            ? board.Rows.OrderBy(r => r.Score).ThenBy(r => r.Count).ThenBy(r => r.TotalStars).ThenBy(r => r.User, StringComparer.Ordinal)
            : board.Rows.OrderByDescending(r => r.Score).ThenBy(r => r.Count).ThenBy(r => r.TotalStars).ThenBy(r => r.User, StringComparer.Ordinal);
        board.Rows = ordered.Take(BoardSize).ToList();
    }

    /// <summary>Winner of each reward placement (the placement-th ranked row; upstream `Take(placement).LastOrDefault()`), skipping empty placements.</summary>
    public static List<(TournamentReward Reward, BoardEntry Winner)> Winners(TournamentTemplate template, TournamentBoard board)
    {
        var result = new List<(TournamentReward, BoardEntry)>();
        foreach (var reward in template.Rewards.OrderBy(r => r.Placement))
        {
            if (reward.Placement < 1 || reward.Placement > board.Rows.Count || (reward.Points <= 0 && reward.Gold <= 0))
            {
                continue;
            }

            result.Add((reward, board.Rows[reward.Placement - 1]));
        }

        return result;
    }

    /// <summary>Whether a template is well formed (window geometry, rewards inside the board).</summary>
    public static string? Validate(TournamentTemplate template)
    {
        if (template.PeriodMinutes < 1 || template.DurationMinutes < 1 || template.DurationMinutes > template.PeriodMinutes
            || template.OffsetMinutes < 0 || template.OffsetMinutes >= template.PeriodMinutes)
        {
            return $"tournament '{template.Id}' has invalid window geometry";
        }

        if (template.Rewards.Any(r => r.Placement < 1 || r.Placement > BoardSize || r.Points < 0 || r.Gold < 0))
        {
            return $"tournament '{template.Id}' has an invalid reward";
        }

        return null;
    }
}

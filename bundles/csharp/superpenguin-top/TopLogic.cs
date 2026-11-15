using System.Globalization;
using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Economy;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinTop;

/// <summary>
/// Leaderboard commands, ported from PenguinTwitchBot's `Misc/Top.cs` + `TopPointTypeSelection.cs` (github.com/Psychoboy/PenguinTwitchBot,
/// MIT, used with permission -- see `bundle.yaml`'s `notice`): `!top5` / `!top10 [point type]` list the richest community members.
/// The app holds only the command policy and formatting; the ranking itself is the economy SERVICE's `points_leaders` tool, and the new
/// `!toprep` (top 10) reads the reputation SERVICE's `leaderboard` tool. Names are `{user:uuid}` placeholders the host renders.
///
/// <para><b>Deviations (documented).</b> (1) The original has several point types; a community has ONE currency here, so the optional
/// argument is accepted only when it names that currency (case-insensitive), otherwise the original's "was not found" reply. (2) NOT
/// ported: `!toptime` and `!loudest` (viewer watch-time / message counts have no service yet), `!toptournaments` (needs the fishing
/// game's tournament standings) and `!toptickets`. They would need new service tools; nothing is faked. (3) `!toprep` is an addition
/// that completes "uses economy/reputation leaderboard tools".</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class TopLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-top";
    internal const string ReputationTarget = "waddles.core.service.reputation";
    internal const int RepLimit = 10;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.IsAny("top5", "top10", "toprep"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("top.refused", new LogFields().With("reason", "no_community").Build());
            return Kit.Reply(@event, command, "Leaderboards need a community; this channel is not linked to one.");
        }

        var text = command.Is("toprep") ? Reputation(host) : Points(host, command.Is("top5") ? 5 : 10, Kit.Clean(Kit.RestOf(command), 40));
        return Kit.Reply(@event, command, text);
    }

    private static string Points(IWaddleHostNext host, int count, string requestedType)
    {
        var leaders = host.Tool.Invoke(
            EconomyServiceTools.Target, "points_leaders", new LeadersArgs(count),
            TopJsonContext.Default.LeadersArgs, TopJsonContext.Default.EconomyLeaders);
        var entries = leaders.Entries
            ?? throw new WaddleToolException(ToolErrorKind.InvalidArgs, "economy points_leaders returned no entries array");
        var alias = string.IsNullOrWhiteSpace(leaders.Alias) ? EconomyServiceTools.DefaultAlias : leaders.Alias;
        if (requestedType.Length > 0 && !string.Equals(requestedType, alias, StringComparison.OrdinalIgnoreCase))
        {
            return $"Point type '{requestedType}' was not found. Use the exact point type name or omit it for the default.";
        }

        if (entries.Count == 0)
        {
            return $"Nobody is on the {alias} leaderboard yet.";
        }

        var sb = new StringBuilder();
        for (var i = 0; i < entries.Count; i++)
        {
            var e = entries[i];
            sb.Append(CultureInfo.InvariantCulture, $"{(i > 0 ? ", " : string.Empty)}{i + 1}. {UserPlaceholder.For(e.User)} {e.Balance:N0}");
        }

        host.Log.Info("top.points", new LogFields().With("rows", entries.Count).Build());
        return $"Top {count} in {alias}: {sb}";
    }

    private static string Reputation(IWaddleHostNext host)
    {
        var board = host.Tool.Invoke(
            ReputationTarget, "leaderboard", new LeadersArgs(RepLimit),
            TopJsonContext.Default.LeadersArgs, TopJsonContext.Default.ReputationLeaderboard);
        var rows = board.Entries
            ?? throw new WaddleToolException(ToolErrorKind.InvalidArgs, "reputation leaderboard returned no entries array");
        if (rows.Count == 0)
        {
            return "Nobody is on the reputation leaderboard yet.";
        }

        var sb = new StringBuilder();
        for (var i = 0; i < rows.Count; i++)
        {
            var e = rows[i];
            sb.Append(CultureInfo.InvariantCulture, $"{(i > 0 ? ", " : string.Empty)}{e.Rank}. {UserPlaceholder.For(e.User)} {e.Score:N0} ({e.Tier})");
        }

        host.Log.Info("top.reputation", new LogFields().With("rows", rows.Count).Build());
        return $"Top {RepLimit} in reputation: {sb}";
    }
}

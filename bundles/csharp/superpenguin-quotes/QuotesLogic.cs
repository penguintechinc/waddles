using System.Globalization;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Random;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinQuotes;

/// <summary>
/// The community quote book, ported from PenguinTwitchBot's `Misc/QuoteSystem.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used
/// with permission -- see `bundle.yaml`'s `notice`): `!quote [N|#N|text]` (random, by number, or a random match), `!addquote TEXT`
/// (alias `!quoteadd`) and `!delquote N` (alias `!quotedel`), the last two moderator-only.
///
/// <para><b>Deviations (documented).</b> (1) The original renumbers the book when a quote is deleted (numbers are positions in a
/// GUIDv7 ordering); here a number is permanent and a delete leaves a tombstone, so a number quoted in chat never changes meaning.
/// (2) The original stamps the current Twitch game and the author's display name. Bundles can read neither; the line carries the
/// author as a `{user:uuid}` placeholder (the host renders the display name) and omits the game. (3) Text search covers the 48 most
/// recent quotes (kv has no listing, and a host invoke is capped at 64 kv operations); the original searches the whole book and also
/// accepts a GUID. (4) The original hides quotes by banned users via the viewer ban list; no ban list reaches a bundle, so moderators
/// delete instead. (5) Quote text is stripped of control characters and `{`/`}` (so a quote can never smuggle a `{user:..}` placeholder)
/// and capped at 400 characters. (6) Role flags are the host-stamped `is_mod` / `is_broadcaster` booleans and fail CLOSED when absent.</para>
///
/// <para><b>Replay.</b> The number assigned to an add is recorded under a per-invocation key BEFORE the quote is written, so a
/// redelivered `!addquote` re-writes the same quote under the same number instead of adding a second one.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class QuotesLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-quotes";
    internal const string CounterKey = "q.n";
    internal const int MaxTextLength = 400;
    internal const int SearchWindow = 48;
    internal const int RandomScan = 24;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.IsAny("quote", "addquote", "quoteadd", "delquote", "quotedel"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("quotes.refused", new LogFields().With("reason", "no_community").Build());
            return Kit.Reply(@event, command, "Quotes need a community; this channel is not linked to one.");
        }

        var store = new KvStateStore(host.Kv);
        if (command.Is("quote"))
        {
            return Kit.Reply(@event, command, Say(host, store, Kit.RestOf(command)));
        }

        if (!Kit.IsModerator(@event.PayloadJson))
        {
            host.Log.Info("quotes.denied", new LogFields().With("command", command.Name).Build());
            return null;
        }

        var actor = Kit.TryActor(host, "quotes.actor_unlinked");
        if (actor is null)
        {
            return Kit.Reply(@event, command, "Link your account to this community before changing quotes.");
        }

        return Kit.Reply(@event, command, command.IsAny("addquote", "quoteadd") ? Add(@event, host, store, command, actor) : Delete(host, store, command));
    }

    private static string Add(PlatformEventInfo @event, IWaddleHostNext host, IStateStore store, ChatCommand command, string actor)
    {
        var text = Kit.Clean(Kit.RestOf(command), MaxTextLength);
        if (text.Length == 0)
        {
            return "you forgot to include the quote.";
        }

        var messageId = host.Context.MessageId;
        if (string.IsNullOrWhiteSpace(messageId))
        {
            throw new InvalidOperationException("the host supplied no message id; refusing to add a quote without a replay key");
        }

        var outcomeKey = StateKey.Of("q.m", StateKey.Hash($"{messageId}|{@event.Platform}|{actor}|{command.Name}"));
        var number = Kit.ReadLong(store, outcomeKey);
        if (number <= 0)
        {
            number = host.Kv.Increment(CounterKey, 1, 0);
            Kit.WriteLong(store, outcomeKey, number, 3600);
        }

        store.SetJson(RecordKey(number), new QuoteRecord(number, text, actor, (long)host.Clock.NowMillis(), false), QuotesJsonContext.Default.QuoteRecord);
        host.Log.Info("quotes.added", new LogFields().With("number", number).Build());
        return $"Quote #{number} added.";
    }

    private static string Delete(IWaddleHostNext host, IStateStore store, ChatCommand command)
    {
        if (command.Args.Count == 0)
        {
            return "you forgot to include the quote number you want to delete.";
        }

        if (!TryNumber(command.Args[0], out var number))
        {
            return "couldn't figure out which quote number you meant.";
        }

        var record = store.GetJson(RecordKey(number), QuotesJsonContext.Default.QuoteRecord);
        if (record is null || record.Deleted)
        {
            return "couldn't find that quote number.";
        }

        store.SetJson(RecordKey(number), record with { Text = string.Empty, Deleted = true }, QuotesJsonContext.Default.QuoteRecord);
        host.Log.Info("quotes.deleted", new LogFields().With("number", number).Build());
        return $"Quote #{number} removed.";
    }

    private static string Say(IWaddleHostNext host, IStateStore store, string search)
    {
        var total = host.Kv.Increment(CounterKey, 0, 0);
        if (total <= 0)
        {
            return "There are no quotes yet.";
        }

        if (search.Length == 0)
        {
            var random = RandomLive(store, total);
            return random is null ? "There are no quotes yet." : Format(random);
        }

        if (TryNumber(search, out var number))
        {
            var one = number <= total ? store.GetJson(RecordKey(number), QuotesJsonContext.Default.QuoteRecord) : null;
            return one is null || one.Deleted ? $"Quote #{number} not found." : Format(one);
        }

        var needle = search.ToLowerInvariant();
        var matches = new List<QuoteRecord>();
        for (var n = total; n > 0 && n > total - SearchWindow; n--)
        {
            var record = store.GetJson(RecordKey(n), QuotesJsonContext.Default.QuoteRecord);
            if (record is { Deleted: false } && record.Text.Contains(needle, StringComparison.OrdinalIgnoreCase))
            {
                matches.Add(record);
            }
        }

        return matches.Count == 0 ? "No quote matches that." : Format(WaddleRandom.Pick(matches));
    }

    private static QuoteRecord? RandomLive(IStateStore store, long total)
    {
        var start = WaddleRandom.NextInt(1, (int)Math.Min(total, int.MaxValue));
        for (var i = 0; i < Math.Min(total, RandomScan); i++)
        {
            var n = ((start - 1 + i) % total) + 1;
            var record = store.GetJson(RecordKey(n), QuotesJsonContext.Default.QuoteRecord);
            if (record is { Deleted: false })
            {
                return record;
            }
        }

        return null;
    }

    private static bool TryNumber(string raw, out long number) =>
        long.TryParse(raw.TrimStart('#'), NumberStyles.Integer, CultureInfo.InvariantCulture, out number) && number > 0;

    internal static string RecordKey(long number) => StateKey.Of("q.i", number.ToString(CultureInfo.InvariantCulture));

    internal static string Format(QuoteRecord q) =>
        $"Quote #{q.Number}: {q.Text} ({DateTimeOffset.FromUnixTimeMilliseconds(q.AtMillis).UtcDateTime:yyyy-MM-dd}) Created By {UserPlaceholder.For(q.By)}";
}

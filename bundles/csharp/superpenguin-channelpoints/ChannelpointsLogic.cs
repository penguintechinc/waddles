using System.Text;
using System.Text.Json;
using SuperPenguinShared;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Overlay;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinChannelpoints;

/// <summary>
/// Redemption-to-action policy, ported from the channel-point features of PenguinTwitchBot (github.com/Psychoboy/PenguinTwitchBot,
/// MIT -- see `bundle.yaml`'s `notice`): the `ChannelPointsCustomRewardRedemptionAdd` trigger with its per-reward title filter,
/// the "Channel point redeem" recipe (chat message plus optional alert) and the redemption context variables
/// (`{User}`, `{RewardTitle}`, `{RewardCost}`, `{UserInput}`...). This is the POLICY only: Helix reward create/update/delete,
/// the `ChannelPointSetEnabledState`/`SetPausedState` sub-actions and fulfilling or cancelling a redemption are Twitch platform
/// operations owned by the platform service/host and are NOT ported (see README "Deferred host needs").
///
/// <para><b>Redemptions.</b> A Twitch EventSub `channel.channel_points_custom_reward_redemption.add` event is matched to a mapping
/// by reward id first, then by case-insensitive title. Only `unfulfilled` redemptions act; an unmapped, disabled or non-actionable
/// redemption is ignored with a counted log line (never an error). The mapping's steps (`award`, `say`, `alert`, at most three,
/// at most one award) run in order. The redeemer is the host-resolved actor: an unlinked redeemer is skipped and logged, never
/// invented. The resolved steps and viewer text are recorded under the redemption id BEFORE any money moves and the award uses
/// the redemption id as its stable retry identity, so a redelivered event repeats the same steps and credits exactly once.</para>
///
/// <para><b>Management</b> (moderator/broadcaster only, fail-closed badges): `!redeem map &lt;reward-id|"title"&gt; &lt;steps&gt;`,
/// `unmap`, `enable`, `disable`, `list`. The table is one kv document (at most <see cref="MaxMappings"/> rows).</para>
///
/// <para><b>Safety.</b> Viewer input and reward titles are sanitised before echo (no braces, no `@`, no leading command characters,
/// length bounded) so a redemption can neither forge the `{user:uuid}` placeholder nor make the bot emit a command. Logs carry
/// ids, counts and reasons only, never viewer text. An overlay push that the host refuses is not swallowed (fail loud).</para>
///
/// Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class ChannelpointsLogic : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-channelpoints";
    internal const string RedemptionEventType = "channel.channel_points_custom_reward_redemption.add";
    internal const string WidgetId = "superpenguin.channelpoints";
    internal const string AlertSuffix = "channelpoints.redeem";
    internal const int MaxMappings = 100;
    internal const int MaxDocBytes = 60_000;
    internal const int MaxReply = 400;
    internal const int ListShown = 8;
    internal const uint OutcomeTtlSeconds = 24 * 60 * 60;
    internal const uint CounterTtlSeconds = 30 * 24 * 60 * 60;

    internal const string ModOnly = "only moderators and the broadcaster can do that.";
    internal const string Usage = "Usage: !redeem map <reward-id|\"title\"> <steps> | unmap <ref> | enable <ref> | disable <ref> | list";

    private static string TableKey => StateKey.Of("cp.map");

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        if (@event.EventType == RedemptionEventType)
        {
            return host.Flags.Enabled(FlagKey, false) ? Redemption(@event, host) : null;
        }

        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("redeem") || !host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        return Manage(@event, command, host);
    }

    private PlatformEventInfo? Redemption(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var red = ParseRedemption(@event.PayloadJson);
        if (red is null || string.IsNullOrWhiteSpace(red.Id))
        {
            host.Log.Warn("cp.ignored", new LogFields().With("reason", "bad_event").Build());
            return null;
        }

        if (!string.Equals(red.Status, "unfulfilled", StringComparison.OrdinalIgnoreCase))
        {
            return Ignore(host, "status");
        }

        if (host.Context.Community is null)
        {
            return Ignore(host, "no_community");
        }

        var store = new KvStateStore(host.Kv);
        var outcomeKey = StateKey.Of("cp.out", StateKey.Hash(red.Id));
        var outcome = store.GetJson(outcomeKey, ChannelpointsJsonContext.Default.RedeemOutcome);
        var replay = outcome is not null;
        if (outcome is null)
        {
            var mapping = Find(Load(store), red.Reward);
            if (mapping is null)
            {
                return Ignore(host, "unmapped");
            }

            if (!mapping.Enabled)
            {
                return Ignore(host, "disabled");
            }

            var actor = TryActor(host);
            if (actor is null)
            {
                return Ignore(host, "not_linked");
            }

            outcome = new RedeemOutcome(
                actor,
                mapping.Match,
                RedeemGrammar.Clean(red.Reward?.Title),
                red.Reward?.Cost ?? 0,
                RedeemGrammar.Clean(red.UserInput),
                mapping.Steps);
            store.SetJson(outcomeKey, outcome, ChannelpointsJsonContext.Default.RedeemOutcome, OutcomeTtlSeconds);
        }

        var say = Execute(host, red.Id, outcome);
        host.Log.Info("cp.redeemed", new LogFields().With("steps", outcome.Steps.Count).With("replay", replay).Build());
        if (say.Length == 0)
        {
            return null;
        }

        var channel = FirstNonBlank(red.BroadcasterLogin, red.BroadcasterId);
        if (channel is null)
        {
            host.Log.Warn("cp.say_skipped", new LogFields().With("reason", "no_channel").Build());
            return null;
        }

        return PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(RedeemGrammar.Truncate(say, MaxReply), channel), WaddleSdkJsonContext.Default.ChatReplyPayload);
    }

    /// <summary>Runs the recorded steps in order and returns the combined chat text ("" when no `say` step produced any).</summary>
    private static string Execute(IWaddleHostNext host, string redemptionId, RedeemOutcome outcome)
    {
        var client = new EconomyServiceClient(host.Tool, redemptionId, "cp");
        var amount = outcome.Steps.Where(s => s.Kind == "award").Select(s => s.Amount).FirstOrDefault();
        var currency = outcome.Steps.Any(s => s.Text.Contains("{currency}", StringComparison.OrdinalIgnoreCase))
            ? Replies.Alias(host, outcome.Actor, "cp")
            : string.Empty;
        var user = UserPlaceholder.For(outcome.Actor);
        List<string> say = [];
        foreach (var step in outcome.Steps)
        {
            switch (step.Kind)
            {
                case "award":
                    Award(host, client, outcome.Actor, step.Amount);
                    break;
                case "alert":
                    PushAlert(host, outcome, RedeemGrammar.Render(step.Text, string.Empty, outcome.Title, outcome.Cost, outcome.Input, amount, currency).Trim());
                    break;
                default:
                    say.Add(RedeemGrammar.Render(step.Text, user, outcome.Title, outcome.Cost, outcome.Input, amount, currency));
                    break;
            }
        }

        return string.Join(' ', say);
    }

    /// <summary>Credits the redeemer once; a cap or a departed member is logged and skipped, any other failure propagates.</summary>
    private static void Award(IWaddleHostNext host, EconomyServiceClient client, string actor, long amount)
    {
        try
        {
            client.Credit(actor, (ulong)amount);
        }
        catch (WaddleEconomyException ex) when (ex.Kind is EconomyErrorKind.OverCap or EconomyErrorKind.NotAMember)
        {
            host.Log.Warn("cp.award_refused", new LogFields().With("reason", ex.Kind == EconomyErrorKind.OverCap ? "over_cap" : "not_a_member").Build());
        }
    }

    /// <summary>
    /// Pushes one `superpenguin.channelpoints.redeem` alert-box message carrying the redeemer's user UUID (the presentation service
    /// resolves the display name), the reward and the sanitised input. A refused push throws and is never swallowed.
    /// </summary>
    private static void PushAlert(IWaddleHostNext host, RedeemOutcome outcome, string message)
    {
        using var stream = new MemoryStream();
        using (var w = new Utf8JsonWriter(stream))
        {
            w.WriteStartObject();
            w.WriteStartObject("alert");
            w.WriteString("alert_type", OverlayAlert.TypePrefix + AlertSuffix);
            w.WriteString("user", outcome.Actor);
            w.WriteStartObject("amount");
            w.WriteString("reward", outcome.Title);
            w.WriteNumber("cost", outcome.Cost);
            if (outcome.Input.Length > 0)
            {
                w.WriteString("input", outcome.Input);
            }

            w.WriteEndObject();
            if (message.Length > 0)
            {
                w.WriteString("message", message);
            }

            w.WriteEndObject();
            w.WriteEndObject();
        }

        host.Overlay.Push(WidgetId, OverlaySurface.AlertBox, Encoding.UTF8.GetString(stream.ToArray()));
    }

    private PlatformEventInfo? Manage(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host)
    {
        if (host.Context.Community is null)
        {
            host.Log.Warn("cp.refused", new LogFields().With("reason", "no_community").Build());
            return Replies.Reply(@event, command, "!redeem needs a community; this channel is not linked to one.");
        }

        if (!ChatPrivileges.IsModerator(@event.PayloadJson))
        {
            host.Log.Info("cp.refused", new LogFields().With("reason", "not_moderator").Build());
            return Replies.Reply(@event, command, ModOnly);
        }

        var tail = command.RawText[(command.Prefix.Length + command.Name.Length)..].Trim();
        var space = tail.IndexOf(' ');
        var sub = (space < 0 ? tail : tail[..space]).ToLowerInvariant();
        var args = space < 0 ? string.Empty : tail[(space + 1)..].Trim();
        var store = new KvStateStore(host.Kv);
        var text = sub switch
        {
            "map" => Map(store, args, host),
            "unmap" => Edit(store, args, host, "unmap"),
            "enable" => Edit(store, args, host, "enable"),
            "disable" => Edit(store, args, host, "disable"),
            "list" => List(store),
            _ => Usage,
        };
        return Replies.Reply(@event, command, text);
    }

    private static string Map(KvStateStore store, string args, IWaddleHostNext host)
    {
        if (!RedeemGrammar.TryParseReference(args, out var reference, out var rest))
        {
            return Usage;
        }

        if (!RedeemGrammar.TryParseSteps(rest, out var steps, out var error))
        {
            return error;
        }

        var table = Load(store);
        var key = RedeemGrammar.KeyFor(reference);
        var existing = table.Items.FindIndex(m => m.Key == key);
        var mapping = new RedeemMapping(key, reference, true, steps);
        if (existing >= 0)
        {
            table.Items[existing] = mapping;
        }
        else if (table.Items.Count >= MaxMappings)
        {
            return $"The mapping table is full ({MaxMappings}); unmap one first.";
        }
        else
        {
            table.Items.Add(mapping);
        }

        if (!Save(store, table))
        {
            return "The mapping table is too large; shorten some templates or unmap a reward.";
        }

        host.Log.Info("cp.mapped", new LogFields().With("steps", steps.Count).With("rows", table.Items.Count).Build());
        return $"Mapped \"{RedeemGrammar.Clean(reference)}\": {RedeemGrammar.Describe(steps)}.";
    }

    private static string Edit(KvStateStore store, string args, IWaddleHostNext host, string op)
    {
        if (!RedeemGrammar.TryParseReference(args, out var reference, out _))
        {
            return Usage;
        }

        var table = Load(store);
        var key = RedeemGrammar.KeyFor(reference);
        var index = table.Items.FindIndex(m => m.Key == key);
        if (index < 0)
        {
            return $"\"{RedeemGrammar.Clean(reference)}\" is not mapped.";
        }

        var label = RedeemGrammar.Clean(table.Items[index].Match);

        if (op == "unmap")
        {
            table.Items.RemoveAt(index);
        }
        else
        {
            table.Items[index] = table.Items[index] with { Enabled = op == "enable" };
        }

        Save(store, table);
        host.Log.Info("cp.edited", new LogFields().With("op", op).Build());
        return op == "unmap" ? $"Unmapped \"{label}\"." : $"\"{label}\" is now {(op == "enable" ? "enabled" : "disabled")}.";
    }

    private static string List(KvStateStore store)
    {
        var table = Load(store);
        if (table.Items.Count == 0)
        {
            return "No rewards are mapped. Use !redeem map.";
        }

        var rows = table.Items.Take(ListShown).Select(m => $"{RedeemGrammar.Clean(m.Match)} [{(m.Enabled ? "on" : "off")}]: {RedeemGrammar.Describe(m.Steps)}");
        var more = table.Items.Count > ListShown ? $" (+{table.Items.Count - ListShown} more)" : string.Empty;
        return RedeemGrammar.Truncate($"{table.Items.Count} mapped: " + string.Join("; ", rows) + more, MaxReply);
    }

    /// <summary>Loads the mapping table; a stored document that cannot be read is an error, never silently replaced by an empty one.</summary>
    private static RedeemMappings Load(KvStateStore store)
    {
        var bytes = store.Get(TableKey);
        if (bytes is null)
        {
            return new RedeemMappings([]);
        }

        try
        {
            return JsonSerializer.Deserialize(bytes, ChannelpointsJsonContext.Default.RedeemMappings)
                ?? throw new InvalidOperationException("the redemption mapping table is empty JSON; operator recovery required");
        }
        catch (JsonException ex)
        {
            throw new InvalidOperationException("the redemption mapping table is corrupt; operator recovery required", ex);
        }
    }

    /// <summary>Writes the table unless it would exceed the single-document budget (false then; nothing is written).</summary>
    private static bool Save(KvStateStore store, RedeemMappings table)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(table, ChannelpointsJsonContext.Default.RedeemMappings);
        if (bytes.Length > MaxDocBytes)
        {
            return false;
        }

        store.Set(TableKey, bytes, 0);
        return true;
    }

    /// <summary>The mapping for a redemption's reward: by reward id first, then by case-insensitive title.</summary>
    private static RedeemMapping? Find(RedeemMappings table, RewardInfo? reward)
    {
        if (reward is null)
        {
            return null;
        }

        var byId = reward.Id is null ? null : RedeemGrammar.KeyFor(reward.Id);
        var found = byId is null || !byId.StartsWith("id.", StringComparison.Ordinal) ? null : table.Items.FirstOrDefault(m => m.Key == byId);
        if (found is not null)
        {
            return found;
        }

        var byTitle = string.IsNullOrWhiteSpace(reward.Title) ? null : RedeemGrammar.KeyForTitle(reward.Title);
        return byTitle is null ? null : table.Items.FirstOrDefault(m => m.Key == byTitle);
    }

    private static RedemptionEvent? ParseRedemption(string payloadJson)
    {
        try
        {
            return JsonSerializer.Deserialize(payloadJson, ChannelpointsJsonContext.Default.RedemptionEvent);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    /// <summary>Counts and logs an ignored redemption (kv counter per reason) and yields no output.</summary>
    private static PlatformEventInfo? Ignore(IWaddleHostNext host, string reason)
    {
        var count = host.Kv.Increment(StateKey.Of("cp.cnt", reason), 1, CounterTtlSeconds);
        host.Log.Info("cp.ignored", new LogFields().With("reason", reason).With("count", count).Build());
        return null;
    }

    private static string? TryActor(IWaddleHostNext host)
    {
        try
        {
            return host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            return null;
        }
    }

    private static string? FirstNonBlank(string? a, string? b) =>
        !string.IsNullOrWhiteSpace(a) ? a : !string.IsNullOrWhiteSpace(b) ? b : null;
}

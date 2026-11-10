using System.Globalization;
using System.Text;
using System.Text.Json;
using SuperPenguinShared;
using WaddleSdk.Chat;
using WaddleSdk.Identity;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinWheel;

/// <summary>
/// `!wheel`, the picker/visual app ported from PenguinTwitchBot's `WheelSpin/*` (`WheelService.cs`, `WheelSpinComplete*`;
/// github.com/Psychoboy/PenguinTwitchBot, MIT -- see `bundle.yaml`'s `notice`). The app owns the policy (which wheels exist,
/// who may spin, weighted odds, the viewer name wheel, when the result may be announced); the visual is a browser-source page
/// (`overlay/index.html`) driven over the host overlay import on the `alert-box` surface.
///
/// <para><b>Commands.</b> Moderator/broadcaster: `define &lt;name&gt; &lt;label[*weight]|...&gt;`, `delete &lt;name&gt;`,
/// `show &lt;name&gt;`, `hide`, `spin`, `message &lt;name&gt; &lt;text with {label}&gt;`, `name open|show|spin|close`.
/// Anyone: `list`, `result`, `join` (also `name join`) while the viewer wheel is open.</para>
///
/// <para><b>Fair, recorded spin.</b> The winning slice is drawn here (weighted by prefix sums over the integer weights, unbiased
/// via <see cref="WaddleSdk.Random.WaddleRandom"/>) and recorded under the event's replay key BEFORE the overlay is told to spin,
/// so a redelivered event spins to the same slice. The browser only animates to the index it is given; it cannot choose or
/// report a result (the overlay channel is read-only), unlike upstream where the page POSTed the stop back over a websocket.</para>
///
/// <para><b>Result.</b> `!wheel result` announces the winner once the spin duration has elapsed on the host clock (otherwise it
/// says the wheel is still spinning). It is a pure read of persisted state, so it is naturally replay-safe. Upstream also fired
/// a `spinwheel` default-command trigger event with the result; there is no cross-app call in Waddles, so the chat line is
/// the output.</para>
///
/// <para><b>Privacy.</b> The viewer wheel is shown to the overlay as `Entrant 1..N`, never a name; the winner is announced in
/// chat as the host `{user:uuid}` placeholder. Labels and templates are stripped of braces and control characters so a label
/// cannot smuggle a placeholder into chat. Bots (the `is_bot` badge) and duplicates cannot join.</para>
///
/// <para><b>Documented limits.</b> The viewer wheel locks entries when it is shown (upstream let joins re-show a live wheel). kv
/// has no compare-and-swap, so two simultaneous joins can lose one entry. Overlay pushes fail loud: the host overlay capability
/// currently answers not-implemented (see the README host needs). Gated behind the PostHog flag <see cref="FlagKey"/> (default OFF).</para>
/// </summary>
public sealed class WheelLogic(Func<int, int, int>? nextInt = null) : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-wheel";
    internal const string WidgetId = "superpenguin.wheel";
    internal const int MaxWheels = 20;
    internal const int MaxItems = 24;
    internal const int MaxLabel = 24;
    internal const int MaxWeight = 100;
    internal const int MaxName = 24;
    internal const int MaxTemplate = 120;
    internal const int MaxEntrants = 200;
    internal const int SpinMillis = 10_000;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const uint StateTtlSeconds = 7 * 24 * 60 * 60;
    internal const string DefaultTemplate = "Congratulations {label}!";
    internal const string ModOnly = "only moderators and the broadcaster can do that.";
    internal const string Usage =
        "Usage: !wheel list | show <name> | hide | spin | result | join | define <name> <label[*weight]|...> | delete <name> | message <name> <text> | name open|show|spin|close";

    private const char BraceSentinel = '';

    private readonly Func<int, int, int> _nextInt = nextInt ?? WaddleSdk.Random.WaddleRandom.NextInt;

    private static string DefsKey => StateKey.Of("wheel.defs");

    private static string CurrentKey => StateKey.Of("wheel.cur");

    private static string NamesKey => StateKey.Of("wheel.names");

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !command.Is("wheel"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("wheel.refused", new LogFields().With("reason", "no_community").Build());
            return Say(@event, command, "!wheel needs a community; this channel is not linked to one.");
        }

        var store = new KvStateStore(host.Kv);
        var moderator = ChatPrivileges.IsModerator(@event.PayloadJson);
        var sub = command.Args.Count == 0 ? "" : command.Args[0].ToLowerInvariant();
        switch (sub)
        {
            case "list":
                return Say(@event, command, ListText(store));
            case "result":
                return Say(@event, command, ResultText(store, host));
            case "join":
                return Join(@event, command, host, store);
            case "name" when command.Args.Count > 1 && command.Args[1].Equals("join", StringComparison.OrdinalIgnoreCase):
                return Join(@event, command, host, store);
            case "define" or "delete" or "show" or "hide" or "spin" or "message" or "name":
                if (!moderator)
                {
                    host.Log.Info("wheel.refused", new LogFields().With("reason", "not_moderator").With("sub", sub).Build());
                    return Say(@event, command, ModOnly);
                }

                return Moderate(@event, command, host, store, sub);
            default:
                return Say(@event, command, Usage);
        }
    }

    private PlatformEventInfo? Moderate(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, string sub)
    {
        switch (sub)
        {
            case "define":
                return Define(@event, command, store);
            case "delete":
                return Delete(@event, command, store);
            case "show":
                return Show(@event, command, host, store);
            case "hide":
                return Hide(@event, command, host, store);
            case "spin":
                return Spin(@event, command, host, store, requireNames: false);
            case "message":
                return SetMessage(@event, command, store);
            default:
                return NameWheelCommand(@event, command, host, store);
        }
    }

    private static PlatformEventInfo Define(PlatformEventInfo @event, ChatCommand command, KvStateStore store)
    {
        if (command.Args.Count < 3 || !TryName(command.Args[1], out var name))
        {
            return Say(@event, command, "Usage: !wheel define <name> <label[*weight]|label[*weight]|...>");
        }

        if (!TryParseItems(string.Join(' ', command.Args.Skip(2)), out var items, out var error))
        {
            return Say(@event, command, error);
        }

        var defs = LoadDefs(store);
        var existing = defs.Wheels.FirstOrDefault(w => w.Name == name);
        if (existing is null && defs.Wheels.Count >= MaxWheels)
        {
            return Say(@event, command, $"At most {MaxWheels} wheels can be defined; delete one first.");
        }

        defs.Wheels.RemoveAll(w => w.Name == name);
        defs.Wheels.Add(new WheelDef(name, items, existing?.Message));
        store.SetJson(DefsKey, defs, WheelJsonContext.Default.WheelDefs);
        return Say(@event, command, $"Wheel {name} {(existing is null ? "defined" : "updated")} with {items.Count} items.");
    }

    private static PlatformEventInfo Delete(PlatformEventInfo @event, ChatCommand command, KvStateStore store)
    {
        if (command.Args.Count != 2 || !TryName(command.Args[1], out var name))
        {
            return Say(@event, command, "Usage: !wheel delete <name>");
        }

        var defs = LoadDefs(store);
        if (defs.Wheels.RemoveAll(w => w.Name == name) == 0)
        {
            return Say(@event, command, $"No wheel named {name}.");
        }

        store.SetJson(DefsKey, defs, WheelJsonContext.Default.WheelDefs);
        return Say(@event, command, $"Wheel {name} deleted.");
    }

    private static PlatformEventInfo SetMessage(PlatformEventInfo @event, ChatCommand command, KvStateStore store)
    {
        if (command.Args.Count < 3 || !TryName(command.Args[1], out var name))
        {
            return Say(@event, command, "Usage: !wheel message <name> <text with {label}>");
        }

        var template = CleanTemplate(string.Join(' ', command.Args.Skip(2)));
        if (template.Length == 0 || template.Length > MaxTemplate)
        {
            return Say(@event, command, $"The message must be 1 to {MaxTemplate} characters.");
        }

        var defs = LoadDefs(store);
        var wheel = defs.Wheels.FirstOrDefault(w => w.Name == name);
        if (wheel is null)
        {
            return Say(@event, command, $"No wheel named {name}.");
        }

        defs.Wheels[defs.Wheels.IndexOf(wheel)] = wheel with { Message = template };
        store.SetJson(DefsKey, defs, WheelJsonContext.Default.WheelDefs);
        return Say(@event, command, $"Wheel {name} will announce: {template}");
    }

    private static string ListText(KvStateStore store)
    {
        var defs = LoadDefs(store);
        return defs.Wheels.Count == 0
            ? "No wheels are defined."
            : "Wheels: " + string.Join(", ", defs.Wheels.Select(w => $"{w.Name} ({w.Items.Count})"));
    }

    private static PlatformEventInfo Show(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        if (command.Args.Count != 2 || !TryName(command.Args[1], out var name))
        {
            return Say(@event, command, "Usage: !wheel show <name>");
        }

        var wheel = LoadDefs(store).Wheels.FirstOrDefault(w => w.Name == name);
        if (wheel is null)
        {
            return Say(@event, command, $"No wheel named {name}.");
        }

        var current = new CurrentWheel(
            wheel.Name, wheel.Items.Select(i => i.Label).ToList(), wheel.Items.Select(i => i.Weight).ToList(), [],
            wheel.Message, false, -1, 0, SpinMillis, "");
        Present(host, store, current);
        return Say(@event, command, $"Showing the {name} wheel.");
    }

    private static PlatformEventInfo Hide(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        store.Delete(CurrentKey);
        OverlayAlert.Push(host.Overlay, WidgetId, "wheel.hide", "{}", null);
        host.Log.Info("wheel.hidden", new LogFields().Build());
        return Say(@event, command, "The wheel is hidden.");
    }

    /// <summary>
    /// Stores the shown wheel and tells the overlay to display it; a refusal propagates. The widget is not registered with the
    /// designer palette: the page is a standalone browser source, and the host's overlay quota (1 call / 10 s) is too small
    /// to spend a call on registration.
    /// </summary>
    private static void Present(IWaddleHostNext host, KvStateStore store, CurrentWheel current)
    {
        store.SetJson(CurrentKey, current, WheelJsonContext.Default.CurrentWheel, StateTtlSeconds);
        OverlayAlert.Push(host.Overlay, WidgetId, "wheel.show", ShowJson(current), null);
        host.Log.Info("wheel.shown", new LogFields().With("items", current.Labels.Count).With("names", current.IsNames).Build());
    }

    private PlatformEventInfo? Spin(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, bool requireNames)
    {
        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("wheel.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Say(@event, command, "Link your account to this community before spinning the wheel.");
        }

        var outcomes = new IdempotentOutcome(store, "wheel", OutcomeTtlSeconds);
        var messageId = host.Context.MessageId;
        var replay = outcomes.TryReplay(messageId, @event.Platform, actor, command.Name, WheelJsonContext.Default.SpinOutcome,
            r => r.Actor == actor && r.Command == command.Name);
        var current = store.GetJson(CurrentKey, WheelJsonContext.Default.CurrentWheel);
        if (current is null)
        {
            return Say(@event, command, "No wheel is showing; use !wheel show <name> first.");
        }

        if (requireNames && !current.IsNames)
        {
            return Say(@event, command, "The viewer wheel is not showing; use !wheel name show first.");
        }

        var now = (long)host.Clock.NowMillis();
        SpinOutcome outcome;
        if (replay is not null)
        {
            outcome = replay;
        }
        else
        {
            if (current.Winning >= 0 && now < current.SpunAtMillis + current.DurationMillis)
            {
                return Say(@event, command, "The wheel is already spinning.");
            }

            var index = Pick(current.Weights, _nextInt(0, current.Weights.Sum() - 1));
            var spinId = StateKey.Hash($"{messageId}|{@event.Platform}|{actor}")[..16];
            outcome = new SpinOutcome(actor, command.Name, index, now, spinId);
            outcomes.Record(messageId, @event.Platform, actor, command.Name, outcome, WheelJsonContext.Default.SpinOutcome);
        }

        current = current with { Winning = outcome.Index, SpunAtMillis = outcome.AtMillis, SpinId = outcome.SpinId };
        store.SetJson(CurrentKey, current, WheelJsonContext.Default.CurrentWheel, StateTtlSeconds);
        OverlayAlert.Push(host.Overlay, WidgetId, "wheel.spin", SpinJson(outcome.Index, current.DurationMillis, outcome.SpinId), null);
        host.Log.Info("wheel.spun", new LogFields().With("slices", current.Labels.Count).With("replay", replay is not null).Build());
        return Say(@event, command, $"The wheel is spinning! Ask for !wheel result in {current.DurationMillis / 1000} seconds.");
    }

    private static string ResultText(KvStateStore store, IWaddleHostNext host)
    {
        var current = store.GetJson(CurrentKey, WheelJsonContext.Default.CurrentWheel);
        if (current is null || current.Winning < 0)
        {
            return "There is no spin to report.";
        }

        if ((long)host.Clock.NowMillis() < current.SpunAtMillis + current.DurationMillis)
        {
            return "The wheel is still spinning...";
        }

        var label = current.IsNames ? UserPlaceholder.For(current.Who[current.Winning]) : current.Labels[current.Winning];
        return (current.Message ?? DefaultTemplate).Replace("{label}", label, StringComparison.Ordinal);
    }

    private PlatformEventInfo? NameWheelCommand(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        var action = command.Args.Count < 2 ? "" : command.Args[1].ToLowerInvariant();
        switch (action)
        {
            case "open":
                store.SetJson(NamesKey, new NameWheel(true, []), WheelJsonContext.Default.NameWheel, StateTtlSeconds);
                return Say(@event, command, "The viewer wheel is open! Type !wheel join to enter.");
            case "show":
                return ShowNames(@event, command, host, store);
            case "spin":
                return Spin(@event, command, host, store, requireNames: true);
            case "close":
                store.Delete(NamesKey);
                store.Delete(CurrentKey);
                OverlayAlert.Push(host.Overlay, WidgetId, "wheel.hide", "{}", null);
                return Say(@event, command, "The viewer wheel is closed.");
            default:
                return Say(@event, command, "Usage: !wheel name open|join|show|spin|close (join is open to everyone)");
        }
    }

    private static PlatformEventInfo ShowNames(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        var names = store.GetJson(NamesKey, WheelJsonContext.Default.NameWheel);
        if (names is null || names.Entrants.Count < 2)
        {
            return Say(@event, command, "The viewer wheel needs at least 2 entrants; open it with !wheel name open.");
        }

        store.SetJson(NamesKey, names with { Open = false }, WheelJsonContext.Default.NameWheel, StateTtlSeconds);
        var labels = Enumerable.Range(1, names.Entrants.Count).Select(i => "Entrant " + i.ToString(CultureInfo.InvariantCulture)).ToList();
        var current = new CurrentWheel("viewers", labels, labels.Select(_ => 1).ToList(), names.Entrants, "Congratulations {label}!", true, -1, 0, SpinMillis, "");
        Present(host, store, current);
        return Say(@event, command, $"Showing the viewer wheel with {labels.Count} entrants; joining is closed.");
    }

    private static PlatformEventInfo? Join(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store)
    {
        if (ChatPrivileges.IsBot(@event.PayloadJson))
        {
            return null;
        }

        var names = store.GetJson(NamesKey, WheelJsonContext.Default.NameWheel);
        if (names is null || !names.Open)
        {
            return Say(@event, command, "The viewer wheel is not open.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("wheel.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Say(@event, command, "Link your account to this community before joining the wheel.");
        }

        if (names.Entrants.Contains(actor))
        {
            return null;
        }

        if (names.Entrants.Count >= MaxEntrants)
        {
            return Say(@event, command, $"The viewer wheel is full ({MaxEntrants} entrants).");
        }

        names.Entrants.Add(actor);
        store.SetJson(NamesKey, names, WheelJsonContext.Default.NameWheel, StateTtlSeconds);
        host.Log.Info("wheel.joined", new LogFields().With("entrants", names.Entrants.Count).Build());
        return null;
    }

    /// <summary>The slice whose cumulative weight first exceeds <paramref name="roll"/> (0-based, in 0..total-1).</summary>
    internal static int Pick(IReadOnlyList<int> weights, int roll)
    {
        var running = 0;
        for (var i = 0; i < weights.Count; i++)
        {
            running += weights[i];
            if (roll < running)
            {
                return i;
            }
        }

        throw new ArgumentOutOfRangeException(nameof(roll), roll, "roll is outside the total weight");
    }

    /// <summary>Parses `label[*weight]|...` into 2..<see cref="MaxItems"/> items; <paramref name="error"/> says what is wrong.</summary>
    internal static bool TryParseItems(string spec, out List<WheelItem> items, out string error)
    {
        items = [];
        error = "";
        var parts = spec.Split('|', StringSplitOptions.TrimEntries);
        if (parts.Length > MaxItems)
        {
            error = $"A wheel holds at most {MaxItems} items.";
            return false;
        }

        foreach (var part in parts)
        {
            var label = part;
            var weight = 1;
            var star = part.LastIndexOf('*');
            if (star >= 0)
            {
                if (!int.TryParse(part[(star + 1)..], NumberStyles.None, CultureInfo.InvariantCulture, out weight) || weight < 1 || weight > MaxWeight)
                {
                    error = $"Weights must be whole numbers from 1 to {MaxWeight}.";
                    return false;
                }

                label = part[..star];
            }

            label = Clean(label);
            if (label.Length == 0 || label.Length > MaxLabel)
            {
                error = $"Each label must be 1 to {MaxLabel} characters.";
                return false;
            }

            items.Add(new WheelItem(label, weight));
        }

        if (items.Count < 2)
        {
            error = "A wheel needs at least 2 items.";
            return false;
        }

        return true;
    }

    /// <summary>Drops control characters and braces (no host-placeholder smuggling), collapses whitespace and trims.</summary>
    internal static string Clean(string raw)
    {
        var sb = new StringBuilder(raw.Length);
        foreach (var c in raw)
        {
            if (char.IsWhiteSpace(c))
            {
                sb.Append(' ');
            }
            else if (!char.IsControl(c) && c != '{' && c != '}')
            {
                sb.Append(c);
            }
        }

        return string.Join(' ', sb.ToString().Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
    }

    /// <summary>Like <see cref="Clean"/> but keeps the exact token `{label}`.</summary>
    internal static string CleanTemplate(string raw) =>
        Clean(raw.Replace("{label}", BraceSentinel.ToString(), StringComparison.Ordinal)).Replace(BraceSentinel.ToString(), "{label}", StringComparison.Ordinal);

    private static bool TryName(string raw, out string name)
    {
        name = raw.ToLowerInvariant();
        return name.Length is >= 1 and <= MaxName && name.All(c => c is >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '-');
    }

    private static WheelDefs LoadDefs(KvStateStore store) =>
        store.GetJson(DefsKey, WheelJsonContext.Default.WheelDefs) ?? new WheelDefs([]);

    private static PlatformEventInfo Say(PlatformEventInfo @event, ChatCommand command, string text) => Replies.Reply(@event, command, text);

    private static string ShowJson(CurrentWheel wheel)
    {
        using var stream = new MemoryStream();
        using (var w = new Utf8JsonWriter(stream))
        {
            w.WriteStartObject();
            w.WriteString("wheel", wheel.Name);
            w.WriteStartArray("items");
            for (var i = 0; i < wheel.Labels.Count; i++)
            {
                w.WriteStartObject();
                w.WriteString("label", wheel.Labels[i]);
                w.WriteNumber("weight", wheel.Weights[i]);
                w.WriteEndObject();
            }

            w.WriteEndArray();
            w.WriteStartArray("labels");
            foreach (var label in wheel.Labels)
            {
                w.WriteStringValue(label);
            }

            w.WriteEndArray();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(stream.ToArray());
    }

    private static string SpinJson(int index, int durationMillis, string spinId) =>
        $"{{\"spin_to\":{index.ToString(CultureInfo.InvariantCulture)},\"duration_ms\":{durationMillis.ToString(CultureInfo.InvariantCulture)},\"spin_id\":\"{spinId}\"}}";
}

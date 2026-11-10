using System.Text;
using WaddleSdk.Chat;
using WaddleSdk.Log;
using WaddleSdk.State;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinAlias;

/// <summary>
/// Command aliases, ported from PenguinTwitchBot's `Alias/Alias.cs` and its request handlers (github.com/Psychoboy/PenguinTwitchBot, MIT,
/// used with permission -- see `bundle.yaml`'s `notice`). The original keeps `AliasModel` rows (alias name to command name) and, on
/// an alias, rewrites the command and re-runs it through the service backbone with `FromAlias` set so an alias can never recurse.
///
/// <para>Management (moderator / broadcaster only): `!alias set NAME !target [fixed args]`, `!alias remove NAME`, `!alias list`
/// (bare `!alias` lists too). Invocation: `!NAME args` answers with the RESOLVED command text `!target fixed-args args`.</para>
///
/// <para><b>Documented limitation.</b> No WIT world lets a bundle re-inject an event into the pipeline or call another APP bundle (app to
/// app calls are forbidden), and no shared command registry exists yet, so an alias cannot silently EXECUTE its target the way the
/// original does; it relays the resolved text. Real re-dispatch needs a host capability that does not exist. Target existence cannot
/// be verified for the same reason.</para>
///
/// <para><b>Safety (kept from the original's no-recursion rule).</b> A target must be one `!command` token (plus optional fixed args), may
/// not be `!alias` or an existing alias, may not equal its own alias name, and may not contain control characters; resolution is a single
/// step (no chains, so no loops). Names are 1-32 chars of `a-z 0-9 _ -`; at most 100 aliases per community; the registry is one kv
/// document, so two moderators editing in the same instant can lose one edit (kv has no compare-and-swap).</para>
///
/// <para>Alias invocation needs every command message delivered to this bundle, so the manifest declares no command-prefix filter; a
/// message that does not start with `!` costs zero kv operations, any other `!word` costs one read.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class AliasLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-alias";
    internal const string RegistryKey = "alias.registry";
    internal const string Management = "alias";
    internal const int MaxAliases = 100;
    internal const int MaxNameLength = 32;
    internal const int MaxTargetLength = 200;
    internal const int MaxResolvedLength = 500;
    internal const string Usage = "Usage: !alias set <name> <!target [args]> | !alias remove <name> | !alias list";

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("alias.refused", new LogFields().With("reason", "no_community").Build());
            return command.Is(Management) ? Kit.Reply(@event, command, "Aliases need a community; this channel is not linked to one.") : null;
        }

        var store = new KvStateStore(host.Kv);
        var registry = store.GetJson(RegistryKey, AliasJsonContext.Default.AliasRegistry) ?? new AliasRegistry([]);
        if (command.Is(Management))
        {
            return Kit.Reply(@event, command, Manage(@event, host, store, registry, command));
        }

        var name = command.Name.ToLowerInvariant();
        if (!registry.Aliases.TryGetValue(name, out var target))
        {
            return null;
        }

        var resolved = command.Args.Count == 0 ? target : $"{target} {string.Join(' ', command.Args)}";
        if (resolved.Length > MaxResolvedLength)
        {
            resolved = resolved[..MaxResolvedLength];
        }

        host.Log.Info("alias.resolved", new LogFields().Build());
        return Kit.Reply(@event, command, resolved);
    }

    private static string Manage(PlatformEventInfo @event, IWaddleHostNext host, IStateStore store, AliasRegistry registry, ChatCommand command)
    {
        var verb = command.Args.Count > 0 ? command.Args[0].ToLowerInvariant() : "list";
        if (verb == "list")
        {
            return List(registry);
        }

        if (verb is not ("set" or "add" or "remove" or "del" or "delete"))
        {
            return Usage;
        }

        if (!Kit.IsModerator(@event.PayloadJson))
        {
            host.Log.Info("alias.denied", new LogFields().With("verb", verb).Build());
            return "Only moderators can change aliases.";
        }

        return verb is "set" or "add" ? Set(host, store, registry, command) : Remove(host, store, registry, command);
    }

    private static string Set(IWaddleHostNext host, IStateStore store, AliasRegistry registry, ChatCommand command)
    {
        if (command.Args.Count < 3)
        {
            return Usage;
        }

        var name = ParseName(command.Args[1]);
        if (name is null)
        {
            return $"Alias names are 1-{MaxNameLength} characters of a-z, 0-9, _ and -.";
        }

        var target = string.Join(' ', command.Args.Skip(2)).Trim();
        var targetCommand = TargetCommand(target);
        if (targetCommand is null || target.Length > MaxTargetLength || target.Any(char.IsControl))
        {
            return "The target must be a single !command (optionally with fixed arguments).";
        }

        if (targetCommand == Management || registry.Aliases.ContainsKey(targetCommand))
        {
            return "An alias cannot point at !alias or at another alias.";
        }

        if (targetCommand == name)
        {
            return "An alias cannot point at itself.";
        }

        if (name == Management)
        {
            return "!alias is reserved.";
        }

        if (!registry.Aliases.ContainsKey(name) && registry.Aliases.Count >= MaxAliases)
        {
            return $"This community already has the maximum of {MaxAliases} aliases.";
        }

        registry.Aliases[name] = target;
        store.SetJson(RegistryKey, registry, AliasJsonContext.Default.AliasRegistry);
        host.Log.Info("alias.set", new LogFields().With("count", registry.Aliases.Count).Build());
        return $"Alias !{name} now runs {target}.";
    }

    private static string Remove(IWaddleHostNext host, IStateStore store, AliasRegistry registry, ChatCommand command)
    {
        if (command.Args.Count < 2)
        {
            return Usage;
        }

        var name = ParseName(command.Args[1]);
        if (name is null || !registry.Aliases.Remove(name))
        {
            return "No such alias.";
        }

        store.SetJson(RegistryKey, registry, AliasJsonContext.Default.AliasRegistry);
        host.Log.Info("alias.removed", new LogFields().With("count", registry.Aliases.Count).Build());
        return $"Alias !{name} removed.";
    }

    private static string List(AliasRegistry registry)
    {
        if (registry.Aliases.Count == 0)
        {
            return "No aliases are set.";
        }

        var sb = new StringBuilder("Aliases: ");
        sb.AppendJoin(", ", registry.Aliases.OrderBy(a => a.Key, StringComparer.Ordinal).Select(a => $"!{a.Key} -> {a.Value}"));
        var text = sb.ToString();
        return text.Length > MaxResolvedLength ? text[..MaxResolvedLength] : text;
    }

    /// <summary>The validated, lowercased alias name (an optional leading `!` is accepted), or null when it is not valid.</summary>
    internal static string? ParseName(string raw)
    {
        var name = raw.TrimStart('!').ToLowerInvariant();
        return name.Length is >= 1 and <= MaxNameLength && name.All(IsNameChar) ? name : null;
    }

    /// <summary>The command token of <paramref name="target"/> (`!word ...`), or null when it is not a single `!command` start.</summary>
    internal static string? TargetCommand(string target)
    {
        if (!target.StartsWith('!'))
        {
            return null;
        }

        var token = target[1..].Split(' ', 2)[0].ToLowerInvariant();
        return token.Length is >= 1 and <= MaxNameLength && token.All(IsNameChar) ? token : null;
    }

    private static bool IsNameChar(char c) => c is >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '-';
}

using System.Globalization;
using System.Text.Json;
using WaddleSdk.Chat;
using WaddleSdk.Economy;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Overlay;
using WaddleSdk.Scheduling;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinFishing;

/// <summary>
/// The SuperPenguinTV fishing game, ported from PenguinTwitchBot's `Bot/Commands/Fishing/*`
/// (github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission -- see `bundle.yaml`'s `notice`): casting
/// (<c>!fish [1-10]</c>, <c>!cast</c>), catches and the fish catalog, inventory and equipment, the gold shop (buy, sell, equip,
/// repair), lifetime leaderboards and automatic recurring tournaments. Chat command names and reply wording are authored here:
/// upstream's chat triggers and messages live in admin-configured actions outside the ported source.
///
/// <para><b>App/service boundary.</b> The app owns every game rule. Gold is this game's own closed-loop resource (earned by
/// catches, spent in the shop, never convertible to community currency), held in the player's state exactly as upstream keeps it in
/// its own `FishingGold` table. Community currency is touched in exactly one place: tournament point rewards, minted through the
/// economy SERVICE tool `adjust_balance` with a stable idempotency key.</para>
///
/// <para><b>Replay safety.</b> Every mutating command computes its result, records it under the event's replay key BEFORE writing
/// state, and marks the key applied inside the same single-value player write; a redelivered event finds the mark and reprints the
/// record, or (record present, mark absent after a crash) re-applies with the recorded seed. Tournament aggregates, boards and
/// leaderboards are idempotent upserts. A per-player lock serializes mutations because kv has no compare-and-swap.</para>
///
/// <para><b>Flags.</b> The game is behind <see cref="FlagKey"/> and tournaments (including their minted rewards) additionally behind
/// <see cref="TournamentFlagKey"/>; both default OFF and are checked after the command match.</para>
/// </summary>
public sealed class FishingLogic : WaddleProcessStageNext
{
    internal const string CommandPrefix = "!";
    internal const string FlagKey = "waddles.command-superpenguin-fishing";
    internal const string TournamentFlagKey = "waddles.superpenguin-fishing-tournaments";
    internal const uint CastCooldownSeconds = 20;
    internal const uint OutcomeTtlSeconds = 3600;
    internal const int MaxCasts = 10;
    internal const int MaxReplyLength = 450;
    internal const string WidgetId = "waddles.integrations.superpenguin.fishing.catch";
    internal const int LifetimeBoardSize = 25;

    private static readonly string[] CommandNames =
    [
        "fish", "cast", "fishinv", "fishinventory", "fishgold", "fishshop", "fishbuy", "fishsell", "fishequip", "fishunequip",
        "fishrepair", "fishlog", "fishtop", "fishtourney", "fishhelp",
    ];

    private readonly FishingCatalog _catalog;
    private readonly FishingEngine _engine;
    private readonly Func<ulong> _seed;

    /// <summary>Creates the logic; tests inject a catalog and a deterministic seed source.</summary>
    public FishingLogic(FishingCatalog? catalog = null, Func<ulong>? seed = null)
    {
        _catalog = catalog ?? FishingCatalog.Default;
        _engine = new FishingEngine(_catalog);
        _seed = seed ?? (() => (ulong)System.Random.Shared.NextInt64() ^ ((ulong)System.Random.Shared.NextInt64() << 1));
        if (_catalog.Fish.Count == 0)
        {
            throw new ArgumentException("the fishing catalog needs at least one fish", nameof(catalog));
        }

        foreach (var item in _catalog.Shop)
        {
            if (FishingCalculations.ValidateShopItem(item) is { } problem)
            {
                throw new ArgumentException($"shop item '{item.Id}': {problem}", nameof(catalog));
            }
        }

        foreach (var template in _catalog.Tournaments)
        {
            if (FishingTournament.Validate(template) is { } problem)
            {
                throw new ArgumentException(problem, nameof(catalog));
            }
        }
    }

    /// <inheritdoc />
    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, CommandPrefix);
        if (command is null || !CommandNames.Contains(command.Name))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        if (host.Context.Community is null)
        {
            host.Log.Warn("fishing.refused", new LogFields().With("reason", "no_community").Build());
            return Reply(@event, command, "Fishing needs a community; this channel is not linked to one.");
        }

        string actor;
        try
        {
            actor = host.Identity.ResolveActor();
        }
        catch (WaddleIdentityException ex) when (ex.Kind is IdentityErrorKind.NotLinked or IdentityErrorKind.NotAMember)
        {
            host.Log.Info("fishing.refused", new LogFields().With("reason", ex.Kind == IdentityErrorKind.NotLinked ? "not_linked" : "not_a_member").Build());
            return Reply(@event, command, "Link your account to this community before fishing.");
        }

        var store = new KvStateStore(host.Kv);
        var fs = new KvFishingStore(store);
        var tournaments = host.Flags.Enabled(TournamentFlagKey, false);
        var announcement = tournaments ? SettleDue(host, store, fs) : null;

        var text = command.Name switch
        {
            "fish" or "cast" => Cast(@event, command, host, store, fs, actor, tournaments),
            "fishinv" or "fishinventory" => Inventory(fs, actor),
            "fishgold" => Gold(fs, actor),
            "fishshop" => Shop(command, fs, actor),
            "fishbuy" => Mutate(@event, command, host, store, fs, actor, state => Buy(command, state)),
            "fishsell" => Mutate(@event, command, host, store, fs, actor, state => ByItemId(command, id => _engine.Sell(state, id))),
            "fishequip" => Mutate(@event, command, host, store, fs, actor, state => ByItemId(command, id => _engine.Equip(state, id))),
            "fishunequip" => Mutate(@event, command, host, store, fs, actor, state => ByItemId(command, id => _engine.Unequip(state, id))),
            "fishrepair" => Mutate(@event, command, host, store, fs, actor, state => Repair(command, state)),
            "fishlog" => Log(fs, actor),
            "fishtop" => Top(fs),
            "fishtourney" => tournaments ? Tourney(host, fs) : "Fishing tournaments are not enabled in this community.",
            _ => Help(),
        };

        if (text is null)
        {
            return null;
        }

        if (announcement is not null)
        {
            text += " " + announcement;
        }

        return Reply(@event, command, Clip(text));
    }

    // ------------------------------------------------------------------ casting

    private string? Cast(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, KvFishingStore fs, string actor, bool tournaments)
    {
        var count = 1;
        if (command.Args.Count > 1
            || (command.Args.Count == 1 && (!int.TryParse(command.Args[0], NumberStyles.None, CultureInfo.InvariantCulture, out count) || count < 1 || count > MaxCasts)))
        {
            return $"Usage: !fish [1-{MaxCasts}]";
        }

        var key = ReplayGuard.KeyFor("fish.out", @event, host, actor, command.Name);
        var applied = Short(key);
        var recorded = store.GetJson(key, FishingJsonContext.Default.CastOutcome);
        var replay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name;

        CastOutcome outcome;
        var freshlyApplied = false;
        PlayerState state;
        if (replay && fs.LoadPlayer(actor).HasApplied(applied))
        {
            outcome = recorded!;
            state = fs.LoadPlayer(actor);
        }
        else
        {
            var takenCooldown = false;
            if (!replay)
            {
                // The original has no cooldown of its own (the bot's generic action cooldown does); a silent drop mirrors it.
                if (!StateCooldown.TryAcquire(store, "cast", actor, CastCooldownSeconds))
                {
                    return null;
                }

                takenCooldown = true;
            }

            if (!fs.TryLock(actor))
            {
                if (takenCooldown)
                {
                    StateCooldown.Release(store, "cast", actor);
                }

                host.Log.Info("fishing.busy", new LogFields().With("command", command.Name).Build());
                return "One cast at a time -- try again in a moment.";
            }

            try
            {
                state = fs.LoadPlayer(actor);
                if (replay && state.HasApplied(applied))
                {
                    outcome = recorded!;
                }
                else
                {
                    var seed = replay ? recorded!.Seed : _seed();
                    var rng = new SeededRng(seed);
                    var casts = replay ? recorded!.Count : count;
                    var results = new CastResult[casts];
                    for (var i = 0; i < casts; i++)
                    {
                        results[i] = _engine.Cast(state, rng);
                    }

                    outcome = new CastOutcome(actor, command.Name, seed, casts, Compose(actor, results, state), results);
                    // Record BEFORE the state write, so a crash in between is repaired from the recorded seed on redelivery.
                    store.SetJson(key, outcome, FishingJsonContext.Default.CastOutcome, OutcomeTtlSeconds);
                    state.MarkApplied(applied);
                    fs.SavePlayer(actor, state);
                    freshlyApplied = true;
                }
            }
            finally
            {
                fs.Unlock(actor);
            }
        }

        string? tournamentNote = null;
        if (tournaments)
        {
            tournamentNote = RecordTournaments(host, store, fs, actor, applied, outcome.Results);
        }

        UpdateLifetime(fs, actor, state);
        host.Log.Info("fishing.cast", new LogFields()
            .With("casts", outcome.Count)
            .With("caught", outcome.Results.Count(r => r.Kind == CastKind.Caught))
            .With("accidents", outcome.Results.Count(r => r.Kind != CastKind.Caught))
            .With("replay", replay)
            .Build());

        if (freshlyApplied)
        {
            PushOverlay(host, fs, actor, outcome.Results);
        }

        return tournamentNote is null ? outcome.Text : outcome.Text + " " + tournamentNote;
    }

    private string Compose(string actor, IReadOnlyList<CastResult> results, PlayerState state)
    {
        var name = UserPlaceholder.For(actor);
        var parts = results.Select(r => Describe(r)).ToList();
        var head = results.Count == 1 ? $"{name}: " : $"{name} casts {results.Count} times: ";
        var caught = results.Where(r => r.Kind == CastKind.Caught).Sum(r => (long)r.Gold);
        var tail = results.Count == 1 ? string.Empty : $" Total {caught}g.";
        return $"{head}{string.Join(" | ", parts)}{tail} Gold: {state.Gold}g.";
    }

    private string Describe(CastResult r)
    {
        switch (r.Kind)
        {
            case CastKind.Caught:
                var fish = _catalog.FishById(r.FishId);
                var line = $"{Stars(r.Stars)} {fish?.Name ?? r.FishId} ({fish?.Rarity}, {r.Weight.ToString("0.##", CultureInfo.InvariantCulture)} lb) +{r.Gold}g";
                if (r.Broken.Length > 0)
                {
                    line += " [" + string.Join(", ", r.Broken.Select(b => b.Replaced ? $"{b.Name} broke, spare equipped" : $"{b.Name} broke")) + "]";
                }

                return line;
            default:
                var what = r.Kind switch
                {
                    CastKind.RodSnapped => "ROD SNAPPED",
                    CastKind.LineSnapped => "LINE SNAPPED",
                    CastKind.ReelJammed => "REEL JAMMED",
                    CastKind.TackleBoxLost => "TACKLE BOX LOST",
                    _ => "NET BROKEN",
                };
                var lost = r.LostItems.Length == 0 ? "nothing equipped was lost" : "lost " + string.Join(", ", r.LostItems) + $" ({r.GoldLost}g)";
                return $"{what}! {lost}";
        }
    }

    private static string Stars(int stars) => new('*', Math.Clamp(stars, 1, 3));

    // ------------------------------------------------------------------ shop and inventory

    private string? Mutate(PlatformEventInfo @event, ChatCommand command, IWaddleHostNext host, KvStateStore store, KvFishingStore fs, string actor, Func<PlayerState, OpResult> op)
    {
        var key = ReplayGuard.KeyFor("fish.out", @event, host, actor, command.Name);
        var applied = Short(key);
        var recorded = store.GetJson(key, FishingJsonContext.Default.ShopOutcome);
        var replay = recorded is not null && recorded.Actor == actor && recorded.Command == command.Name;
        if (replay && fs.LoadPlayer(actor).HasApplied(applied))
        {
            return recorded!.Text;
        }

        if (!fs.TryLock(actor))
        {
            return "One action at a time -- try again in a moment.";
        }

        try
        {
            var state = fs.LoadPlayer(actor);
            if (replay && state.HasApplied(applied))
            {
                return recorded!.Text;
            }

            var result = op(state);
            if (!result.Ok)
            {
                return result.Message;
            }

            var text = replay ? recorded!.Text : result.Message;
            store.SetJson(key, new ShopOutcome(actor, command.Name, text), FishingJsonContext.Default.ShopOutcome, OutcomeTtlSeconds);
            state.MarkApplied(applied);
            fs.SavePlayer(actor, state);
            host.Log.Info("fishing.shop", new LogFields().With("command", command.Name).With("replay", replay).Build());
            return text;
        }
        finally
        {
            fs.Unlock(actor);
        }
    }

    private OpResult Buy(ChatCommand command, PlayerState state)
    {
        if (command.Args.Count == 0)
        {
            return new(false, "Usage: !fishbuy <item> [quantity]  (see !fishshop)");
        }

        var args = command.Args.ToList();
        var quantity = 1;
        if (args.Count > 1 && int.TryParse(args[^1], NumberStyles.None, CultureInfo.InvariantCulture, out var parsed))
        {
            quantity = parsed;
            args.RemoveAt(args.Count - 1);
        }

        var item = FindShopItem(string.Join(' ', args));
        return item is null ? new(false, "No such shop item. See !fishshop.") : _engine.Buy(state, item, quantity);
    }

    private ShopItem? FindShopItem(string query)
    {
        var q = query.Trim().ToLowerInvariant();
        var exact = _catalog.Shop.FirstOrDefault(s => s.Id == q || s.Name.ToLowerInvariant() == q);
        if (exact is not null)
        {
            return exact;
        }

        var contains = _catalog.Shop.Where(s => s.Name.ToLowerInvariant().Contains(q)).ToList();
        return contains.Count == 1 ? contains[0] : null;
    }

    private static OpResult ByItemId(ChatCommand command, Func<int, OpResult> op)
    {
        if (command.Args.Count != 1 || !int.TryParse(command.Args[0].TrimStart('#'), NumberStyles.None, CultureInfo.InvariantCulture, out var id))
        {
            return new(false, $"Usage: !{command.Name} <item number>  (see !fishinv)");
        }

        return op(id);
    }

    private OpResult Repair(ChatCommand command, PlayerState state)
    {
        if (command.Args.Count == 0 || (command.Args.Count == 1 && string.Equals(command.Args[0], "all", StringComparison.OrdinalIgnoreCase)))
        {
            return _engine.Repair(state, null);
        }

        return ByItemId(command, id => _engine.Repair(state, id));
    }

    private string Inventory(KvFishingStore fs, string actor)
    {
        var state = fs.LoadPlayer(actor);
        if (state.Items.Count == 0)
        {
            return $"{UserPlaceholder.For(actor)}: your tackle box is empty. Gold: {state.Gold}g. Browse !fishshop.";
        }

        var rows = state.Items.OrderBy(i => i.Id).Select(i =>
        {
            var shop = _catalog.ShopById(i.ShopId);
            var uses = i.RemainingUses >= 0 ? $" {i.RemainingUses} uses" : string.Empty;
            var dur = i.Durability is { } d && shop?.MaxDurability is { } m ? $" {d:0.#}/{m}" : string.Empty;
            return $"#{i.Id} {shop?.Name ?? i.ShopId}{(i.Equipped ? " [E]" : string.Empty)}{uses}{dur}";
        });
        return $"{UserPlaceholder.For(actor)}: {string.Join("; ", rows)}. Gold: {state.Gold}g.";
    }

    private static string Gold(KvFishingStore fs, string actor)
    {
        var state = fs.LoadPlayer(actor);
        return $"{UserPlaceholder.For(actor)}: {state.Gold}g in hand, {state.TotalGoldEarned}g earned over {state.Casts} casts, {state.TotalGoldLost}g lost to accidents.";
    }

    private string Shop(ChatCommand command, KvFishingStore fs, string actor)
    {
        var slotArg = command.Args.Count > 0 ? command.Args[0].ToLowerInvariant() : null;
        if (slotArg is null)
        {
            var slots = _catalog.Shop.Where(s => s.Enabled && !s.AdminOnly).GroupBy(s => s.Slot?.ToString() ?? "Other")
                .Select(g => $"{g.Key.ToLowerInvariant()} ({g.Count()})");
            return $"Fishing shop sections: {string.Join(", ", slots)}. Use !fishshop <section>, then !fishbuy <item>.";
        }

        var items = _catalog.Shop
            .Where(s => s.Enabled && !s.AdminOnly && string.Equals(s.Slot?.ToString() ?? "Other", slotArg, StringComparison.OrdinalIgnoreCase))
            .ToList();
        if (items.Count == 0)
        {
            return $"No shop section '{slotArg}'. Use !fishshop for the list.";
        }

        var state = fs.LoadPlayer(actor);
        var tiers = _engine.Tiers();
        var rows = items.Select(s =>
        {
            var cmp = _engine.Compare(s, state, tiers);
            return $"{s.Name} {s.Cost}g ({tiers[s.Id]}{(cmp.Length > 0 ? ", " + cmp : string.Empty)})";
        });
        return $"{slotArg}: {string.Join("; ", rows)}.";
    }

    private string Log(KvFishingStore fs, string actor)
    {
        var state = fs.LoadPlayer(actor);
        var best = state.Best.OrderByDescending(kv => kv.Value.Stars).ThenByDescending(kv => kv.Value.Weight).Take(3)
            .Select(kv => $"{_catalog.FishById(kv.Key)?.Name ?? kv.Key} {Stars(kv.Value.Stars)} {kv.Value.Weight.ToString("0.##", CultureInfo.InvariantCulture)} lb");
        var line = $"{UserPlaceholder.For(actor)}: {state.Counts.Count} of {_catalog.Fish.Count} species caught";
        return state.Best.Count == 0 ? line + "." : $"{line}. Best: {string.Join("; ", best)}.";
    }

    private static string Top(KvFishingStore fs)
    {
        var boards = fs.LoadLifetime();
        if (boards.Earned.Count == 0)
        {
            return "Nobody has caught anything yet.";
        }

        var earned = string.Join("; ", boards.Earned.Take(5).Select((r, i) => $"{i + 1}. {UserPlaceholder.For(r.User)} {r.Value}g"));
        var text = $"Top earners: {earned}.";
        if (boards.Lost.Count > 0)
        {
            text += " Biggest losses: " + string.Join("; ", boards.Lost.Take(3).Select((r, i) => $"{i + 1}. {UserPlaceholder.For(r.User)} {r.Value}g")) + ".";
        }

        return text;
    }

    private static string Help() =>
        "Fishing: !fish [1-10] cast | !fishinv items | !fishgold | !fishshop [section] | !fishbuy <item> [qty] | !fishsell <#> | "
        + "!fishequip <#> | !fishunequip <#> | !fishrepair [#|all] | !fishlog | !fishtop | !fishtourney";

    // ------------------------------------------------------------------ leaderboards

    private static void UpdateLifetime(KvFishingStore fs, string actor, PlayerState state)
    {
        var boards = fs.LoadLifetime();
        var changed = Upsert(boards.Earned, actor, state.TotalGoldEarned);
        changed |= Upsert(boards.Lost, actor, state.TotalGoldLost);
        if (changed)
        {
            fs.SaveLifetime(boards);
        }
    }

    /// <summary>Monotone top-N upsert: a user's value only rises, so a bounded board stays exact. Returns whether the board changed.</summary>
    internal static bool Upsert(List<LifetimeRow> rows, string user, long value)
    {
        if (value <= 0)
        {
            return false;
        }

        var existing = rows.FindIndex(r => r.User == user);
        if (existing >= 0)
        {
            if (rows[existing].Value == value)
            {
                return false;
            }

            rows[existing] = new LifetimeRow(user, value);
        }
        else if (rows.Count < LifetimeBoardSize)
        {
            rows.Add(new LifetimeRow(user, value));
        }
        else if (value > rows.Min(r => r.Value))
        {
            var lowest = rows.IndexOf(rows.OrderBy(r => r.Value).First());
            rows[lowest] = new LifetimeRow(user, value);
        }
        else
        {
            return false;
        }

        var ordered = rows.OrderByDescending(r => r.Value).ThenBy(r => r.User, StringComparer.Ordinal).ToList();
        rows.Clear();
        rows.AddRange(ordered);
        return true;
    }

    // ------------------------------------------------------------------ tournaments

    private string? RecordTournaments(IWaddleHostNext host, KvStateStore store, KvFishingStore fs, string actor, string applied, CastResult[] results)
    {
        var now = host.Clock.NowMillis();
        var notes = new List<string>();
        foreach (var template in _catalog.Tournaments)
        {
            var window = FishingTournament.ActiveWindow(template, now);
            if (window is null)
            {
                continue;
            }

            var counted = results
                .Where(r => r.Kind == CastKind.Caught && _catalog.FishById(r.FishId) is { } fish && FishingTournament.Counts(template, fish))
                .ToList();
            if (counted.Count == 0)
            {
                continue;
            }

            var aggregate = fs.LoadAggregate(window.Id, actor);
            if (!aggregate.Applied.Contains(applied))
            {
                foreach (var cast in counted)
                {
                    FishingTournament.Record(aggregate, cast);
                }

                aggregate.Applied.Add(applied);
                if (aggregate.Applied.Count > 8)
                {
                    aggregate.Applied.RemoveRange(0, aggregate.Applied.Count - 8);
                }

                fs.SaveAggregate(window.Id, actor, aggregate);
            }

            var board = fs.LoadBoard(window.Id) ?? new TournamentBoard();
            FishingTournament.Upsert(board, actor, aggregate, template.Score);
            if (!board.Scheduled)
            {
                new TickScheduler(store, host.Clock, "fishing").Schedule(new ScheduledTask(window.Id, window.EndMillis, "settle", template.Id));
                board.Scheduled = true;
            }

            fs.SaveBoard(window.Id, board);
            var rank = board.Rows.FindIndex(r => r.User == actor) + 1;
            notes.Add(rank > 0 ? $"[{template.Name}: you are #{rank}]" : $"[{template.Name}: not on the board yet]");
        }

        return notes.Count == 0 ? null : string.Join(" ", notes);
    }

    private string? SettleDue(IWaddleHostNext host, KvStateStore store, KvFishingStore fs)
    {
        var scheduler = new TickScheduler(store, host.Clock, "fishing");
        var now = host.Clock.NowMillis();
        var lines = new List<string>();
        foreach (var task in scheduler.Pending().Where(t => t.Kind == "settle" && t.DueAtMillis <= now))
        {
            var template = _catalog.Tournaments.FirstOrDefault(t => t.Id == task.PayloadJson);
            var board = fs.LoadBoard(task.Id);
            if (template is null || board is null)
            {
                host.Log.Warn("fishing.settle_orphan", new LogFields().With("known_template", template is not null).Build());
                scheduler.Cancel(task.Id);
                continue;
            }

            try
            {
                if (!board.Settled)
                {
                    lines.Add(Pay(host, fs, template, task.Id, board));
                    board.Settled = true;
                    fs.SaveBoard(task.Id, board);
                }

                scheduler.Cancel(task.Id);
            }
            catch (Exception ex) when (ex is WaddleEconomyException or WaddleToolException or InvalidOperationException)
            {
                // Settlement is idempotent (economy key + per-player applied marks), so it stays pending and retries on the next event.
                host.Log.Error("fishing.settle_failed", new LogFields().With("template", template.Id).With("error", ex.GetType().Name).Build());
            }
        }

        return lines.Count == 0 ? null : string.Join(" ", lines);
    }

    private string Pay(IWaddleHostNext host, KvFishingStore fs, TournamentTemplate template, string windowId, TournamentBoard board)
    {
        var winners = FishingTournament.Winners(template, board);
        var parts = new List<string>();
        foreach (var (reward, winner) in winners)
        {
            var paid = new List<string>();
            if (reward.Points > 0)
            {
                try
                {
                    new EconomyServiceClient(host.Tool, "fishing.tournament." + windowId, "placement" + reward.Placement).Credit(winner.User, (ulong)reward.Points);
                    paid.Add($"{reward.Points} pts");
                }
                catch (WaddleEconomyException ex) when (ex.Kind == EconomyErrorKind.NotAMember)
                {
                    host.Log.Warn("fishing.reward_skipped", new LogFields().With("reason", "not_a_member").With("placement", reward.Placement).Build());
                }
            }

            if (reward.Gold > 0)
            {
                GrantGold(fs, winner.User, "t" + StateKey.Hash(windowId + "." + reward.Placement)[..12], reward.Gold);
                paid.Add($"{reward.Gold}g");
            }

            parts.Add($"#{reward.Placement} {UserPlaceholder.For(winner.User)} ({string.Join(" + ", paid)})");
        }

        host.Log.Info("fishing.settled", new LogFields().With("template", template.Id).With("winners", winners.Count).Build());
        return winners.Count == 0
            ? $"[{template.Name} ended: no winners.]"
            : $"[{template.Name} ended! {string.Join(", ", parts)}]";
    }

    private static void GrantGold(KvFishingStore fs, string user, string mark, long gold)
    {
        if (!fs.TryLock(user))
        {
            throw new InvalidOperationException("winner state is busy; the settlement retries on the next event");
        }

        try
        {
            var state = fs.LoadPlayer(user);
            if (state.HasApplied(mark))
            {
                return;
            }

            state.Gold = Math.Min(int.MaxValue, state.Gold + gold);
            state.MarkApplied(mark);
            fs.SavePlayer(user, state);
        }
        finally
        {
            fs.Unlock(user);
        }
    }

    private string Tourney(IWaddleHostNext host, KvFishingStore fs)
    {
        var now = host.Clock.NowMillis();
        var rows = new List<string>();
        foreach (var template in _catalog.Tournaments)
        {
            var window = FishingTournament.ActiveWindow(template, now);
            if (window is null)
            {
                rows.Add($"{template.Name}: next run in {MinutesUntilNext(template, now)} min");
                continue;
            }

            var board = fs.LoadBoard(window.Id);
            var left = Math.Max(0, window.EndMinute - (long)(now / 60_000UL));
            var top = board is null || board.Rows.Count == 0
                ? "no catches yet"
                : string.Join(", ", board.Rows.Take(3).Select((r, i) => $"{i + 1}. {UserPlaceholder.For(r.User)} {FormatScore(template.Score, r.Score)}"));
            rows.Add($"{template.Name} ({template.Score}, {left} min left): {top}");
        }

        return rows.Count == 0 ? "No fishing tournaments are configured." : string.Join(" | ", rows);
    }

    private static long MinutesUntilNext(TournamentTemplate template, ulong nowMillis)
    {
        var minute = (long)(nowMillis / 60_000UL);
        var next = minute - (minute % template.PeriodMinutes) + template.OffsetMinutes;
        if (next <= minute)
        {
            next += template.PeriodMinutes;
        }

        return next - minute;
    }

    private static string FormatScore(ScoreCategory category, double score) => category switch
    {
        ScoreCategory.MostCatches => ((long)score).ToString(CultureInfo.InvariantCulture),
        ScoreCategory.MostValuable => $"{(long)score}g",
        _ => score.ToString("0.##", CultureInfo.InvariantCulture) + " lb",
    };

    // ------------------------------------------------------------------ overlay

    private void PushOverlay(IWaddleHostNext host, KvFishingStore fs, string actor, CastResult[] results)
    {
        // Headline: the first accident, else the most valuable catch. Cosmetic only: a dead overlay never fails the cast.
        var headline = results.FirstOrDefault(r => r.Kind != CastKind.Caught) ?? results.OrderByDescending(r => r.Gold).First();
        string title;
        string body;
        if (headline.Kind == CastKind.Caught)
        {
            var fish = _catalog.FishById(headline.FishId);
            title = fish?.Name ?? headline.FishId;
            body = $"{Stars(headline.Stars)} {headline.Weight.ToString("0.##", CultureInfo.InvariantCulture)} lb, {headline.Gold}g ({fish?.Rarity})";
        }
        else
        {
            title = headline.Kind.ToString();
            body = headline.LostItems.Length == 0 ? "nothing equipped was lost" : "lost " + string.Join(", ", headline.LostItems);
        }

        var alert = new OverlayAlert(new OverlayAlertBody(headline.Kind == CastKind.Caught ? "fishing_catch" : "fishing_accident", actor, headline.Gold, body), title, body);
        try
        {
            if (!fs.WidgetRegistered())
            {
                var descriptor = new OverlayWidgetDescriptor(640, 220, "bottom-center", 5000, ["alert.user", "alert.amount", "title", "body"]);
                host.Overlay.RegisterWidget(new OverlayWidget(WidgetId, "Fishing catch", OverlaySurface.AlertBox,
                    JsonSerializer.Serialize(descriptor, FishingJsonContext.Default.OverlayWidgetDescriptor)));
                fs.MarkWidgetRegistered();
            }

            host.Overlay.Push(WidgetId, OverlaySurface.AlertBox, JsonSerializer.Serialize(alert, FishingJsonContext.Default.OverlayAlert));
        }
        catch (WaddleTransportException ex)
        {
            host.Log.Warn("fishing.overlay_failed", new LogFields().With("code", ex.Error.Code).Build());
        }
    }

    // ------------------------------------------------------------------ helpers

    private static string Short(string key) => StateKey.Hash(key)[..16];

    /// <summary>Clips a reply to chat length without cutting through a `{user:uuid}` placeholder (a torn one would leak raw braces).</summary>
    internal static string Clip(string text)
    {
        if (text.Length <= MaxReplyLength)
        {
            return text;
        }

        var cut = text[..(MaxReplyLength - 3)];
        var open = cut.LastIndexOf("{user:", StringComparison.Ordinal);
        if (open >= 0 && cut.IndexOf('}', open) < 0)
        {
            cut = cut[..open];
        }

        return cut.TrimEnd() + "...";
    }

    private static PlatformEventInfo Reply(PlatformEventInfo @event, ChatCommand command, string text) =>
        PlatformEventInfo.WithPayload(@event, new ChatReplyPayload(text, command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
}

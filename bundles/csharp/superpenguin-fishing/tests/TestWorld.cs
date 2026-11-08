using System.Text;
using System.Text.Json;
using WaddleSdk.Ai;
using WaddleSdk.Chat;
using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Economy;
using WaddleSdk.Flags;
using WaddleSdk.Http;
using WaddleSdk.Identity;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Media;
using WaddleSdk.Overlay;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Tests;
using WaddleSdk.Tool;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinFishing.Tests;

/// <summary>Scripted integer source for the rules: yields queued values in order (clamped into the requested range), then zeros.</summary>
public sealed class ScriptedRng(params int[] values) : IFishRng
{
    private readonly Queue<int> _values = new(values);

    public int Next(int minInclusive, int maxExclusive) =>
        Math.Clamp(_values.Count > 0 ? _values.Dequeue() : 0, minInclusive, maxExclusive - 1);
}

/// <summary>
/// The economy SERVICE as the host exposes it to a tool call: `adjust_balance` keyed by idempotency key. A replayed key with the same
/// arguments returns the original result and moves nothing; the same key with different arguments is refused; <see cref="Failure"/>
/// scripts a tool outage. <see cref="Credits"/> lists only calls that actually moved money.
/// </summary>
public sealed class KeyedEconomyTool : IToolClient
{
    private readonly Dictionary<string, (string Args, long Result)> _done = [];
    private readonly Dictionary<string, long> _balances = [];

    public List<(string User, long Delta, string? Key)> Credits { get; } = [];
    public List<string?> Keys { get; } = [];
    public Exception? Failure { get; set; }
    public string Alias { get; set; } = "stars";
    public HashSet<string> NonMembers { get; } = [];

    public long Balance(string user) => _balances.GetValueOrDefault(user);

    public byte[] Invoke(string target, string tool, byte[] argsJson, string? idempotencyKey)
    {
        if (target != EconomyServiceTools.Target)
        {
            throw new WaddleToolException(ToolErrorKind.NotGranted, "no grant for " + target);
        }

        using var doc = JsonDocument.Parse(argsJson);
        var user = doc.RootElement.GetProperty("user").GetString()!;
        if (tool == "points_view")
        {
            return Encoding.UTF8.GetBytes($"{{\"user\":\"{user}\",\"balance\":{Balance(user)},\"alias\":\"{Alias}\"}}");
        }

        if (tool != "adjust_balance")
        {
            throw new WaddleToolException(ToolErrorKind.NotGranted, "no grant for " + tool);
        }

        if (Failure is not null)
        {
            throw Failure;
        }

        var args = Encoding.UTF8.GetString(argsJson);
        Keys.Add(idempotencyKey);
        if (idempotencyKey is not null && _done.TryGetValue(idempotencyKey, out var prior))
        {
            if (prior.Args != args)
            {
                throw new WaddleToolException(ToolErrorKind.Forbidden, "idempotency_conflict");
            }

            return Encoding.UTF8.GetBytes($"{{\"user\":\"{user}\",\"balance\":{prior.Result}}}");
        }

        if (NonMembers.Contains(user))
        {
            throw new WaddleToolException(ToolErrorKind.InvalidArgs, "not_a_member");
        }

        var delta = doc.RootElement.GetProperty("delta").GetInt64();
        _balances[user] = Balance(user) + delta;
        Credits.Add((user, delta, idempotencyKey));
        if (idempotencyKey is not null)
        {
            _done[idempotencyKey] = (args, _balances[user]);
        }

        return Encoding.UTF8.GetBytes($"{{\"user\":\"{user}\",\"balance\":{_balances[user]}}}");
    }
}

/// <summary>kv that can fail a chosen write, to simulate a crash between two host calls.</summary>
public sealed class FlakyKv : IKvClient
{
    public FakeKvClient Inner { get; } = new();

    /// <summary>Called before every set; throw to crash at that write.</summary>
    public Action<string>? BeforeSet { get; set; }

    public int Reads { get; private set; }
    public int Writes { get; private set; }
    public int Ops => Reads + Writes;

    public void ResetCounters()
    {
        Reads = 0;
        Writes = 0;
    }

    public byte[]? Get(string key)
    {
        Reads++;
        return Inner.Get(key);
    }

    public void Set(string key, byte[] value, uint ttlSeconds)
    {
        BeforeSet?.Invoke(key);
        Writes++;
        Inner.Set(key, value, ttlSeconds);
    }

    public void Delete(string key)
    {
        Writes++;
        Inner.Delete(key);
    }

    public long Increment(string key, long delta, uint ttlSeconds)
    {
        Writes++;
        return Inner.Increment(key, delta, ttlSeconds);
    }
}

/// <summary>Overlay fake that records widgets/pushes and can refuse like a host without the grant.</summary>
public sealed class TestOverlay : IOverlayClient
{
    public List<OverlayWidget> Registered { get; } = [];
    public List<(string WidgetId, OverlaySurface Surface, string Payload)> Pushes { get; } = [];
    public Exception? Failure { get; set; }

    public TransportResultInfo RegisterWidget(OverlayWidget widget)
    {
        if (Failure is not null)
        {
            throw Failure;
        }

        Registered.Add(widget);
        return TransportResultInfo.Success();
    }

    public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson)
    {
        if (Failure is not null)
        {
            throw Failure;
        }

        Pushes.Add((widgetId, surface, payloadJson));
        return TransportResultInfo.Success();
    }
}

/// <summary>One invocation's host over the shared <see cref="World"/> state.</summary>
public sealed class EventHost(BundleContextInfo context, World world, string actor) : IWaddleHostNext
{
    private readonly FakeWaddleHostNext _inner = new();

    public BundleContextInfo Context => context;
    public IKvClient Kv => world.Kv;
    public IDbClient Db => _inner.Db;
    public IRelayClient Relay => _inner.Relay;
    public IHttpClient Http => _inner.Http;
    public IFlagsClient Flags => world.Flags;
    public ILogClient Log => world.Log;
    public IClockClient Clock => world.Clock;
    public IEconomyClient Economy => _inner.Economy;
    public IIdentityClient Identity { get; } = new WorldIdentity(actor, world);
    public IToolClient Tool => world.Tools;
    public IOverlayClient Overlay => world.Overlay;
    public IAiClient Ai => _inner.Ai;
    public IMediaClient Media => _inner.Media;
}

/// <summary>Identity for one event: the actor, or a scripted failure.</summary>
public sealed class WorldIdentity(string actor, World world) : IIdentityClient
{
    public string ResolveActor() => world.ActorFailure is null ? actor : throw world.ActorFailure;

    public string ResolveMention(string token) => token;
}

/// <summary>A tiny community: shared kv, clock, flags, log, economy service and overlay, plus a `Send` that runs one chat event.</summary>
public sealed class World
{
    public const string Alice = "11111111-1111-4111-8111-111111111111";
    public const string Bob = "22222222-2222-4222-8222-222222222222";
    public const string Carol = "33333333-3333-4333-8333-333333333333";

    /// <summary>2026-11-07 17:30 UTC: 30 minutes before the default Heavyweight Hour (18:00 UTC).</summary>
    public const ulong BeforeHeavyweight = 1_794_072_600_000UL;

    public FlakyKv Kv { get; } = new();
    public MutableClock Clock { get; } = new(1_700_000_000_000UL);
    public FakeFlagsClient Flags { get; } = new();
    public FakeLogClient Log { get; } = new();
    public KeyedEconomyTool Tools { get; } = new();
    public TestOverlay Overlay { get; } = new();
    public Exception? ActorFailure { get; set; }
    public int Seq { get; private set; }

    public World(bool tournaments = false)
    {
        Flags.Overrides[FishingLogic.FlagKey] = true;
        Flags.Overrides[FishingLogic.TournamentFlagKey] = tournaments;
    }

    public EventHost HostFor(string actor, string messageId, string? community = "community-1") =>
        new(new BundleContextInfo("tenant-1", community, "waddles.integrations.superpenguin.fishing", "waddles.integrations.superpenguin", "1.0.0", messageId, "{}"), this, actor);

    public static PlatformEventInfo Msg(string text, string platform = "twitch") =>
        new(platform, "chat.message", "platform-actor", $"{{\"text\":\"{text}\",\"channel_id\":\"chan-1\"}}", "2026-11-07T00:00:00.000Z");

    /// <summary>Runs one chat event with an explicit message id; returns the reply text, or null when the bundle stayed silent.</summary>
    public string? Send(WaddleProcessStageNext logic, string actor, string text, string messageId, string platform = "twitch")
    {
        var reply = logic.Run(Msg(text, platform), HostFor(actor, messageId));
        return reply is null ? null : TextOf(reply);
    }

    /// <summary>Runs one chat event with a fresh message id.</summary>
    public string? Say(WaddleProcessStageNext logic, string actor, string text) => Send(logic, actor, text, "m-" + (++Seq));

    public static string TextOf(PlatformEventInfo reply) =>
        Assert.IsType<ChatReplyPayload>(reply.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)).Text;

    /// <summary>The persisted player state of <paramref name="user"/> (through the real store).</summary>
    public PlayerState Player(string user) => new KvFishingStore(new WaddleSdk.State.KvStateStore(Kv)).LoadPlayer(user);

    /// <summary>Writes a player state (test setup).</summary>
    public void SetPlayer(string user, PlayerState state) => new KvFishingStore(new WaddleSdk.State.KvStateStore(Kv)).SavePlayer(user, state);

    public KvFishingStore Store => new(new WaddleSdk.State.KvStateStore(Kv));
}

/// <summary>Catalog builders for tests.</summary>
public static class Cat
{
    public static FishType Fish(string id, FishRarity rarity = FishRarity.Common, double weight = 5, int gold = 10, params string[] categories) =>
        new(id, id, rarity, weight, gold, categories);

    public static ShopItem Gear(string id, EquipmentSlot slot, int cost = 100, BoostType type = BoostType.GeneralRarityBoost, double amount = 0.1,
        int? maxUses = null, bool disableBreaking = false, int? maxDurability = null, double? loss = null, bool adminOnly = false, bool enabled = true,
        BoostType? type2 = null, double? amount2 = null, string? targetFish = null, string? targetCategory = null) =>
        new(id, id, id, cost, type, amount, slot, MaxUses: maxUses, BoostType2: type2, BoostAmount2: amount2, TargetFishId: targetFish,
            TargetCategory: targetCategory, Enabled: enabled, AdminOnly: adminOnly, DisableBreaking: disableBreaking,
            MaxDurability: maxDurability, DurabilityLossPerUse: loss);

    public static FishingCatalog Of(FishingSettings? settings = null, IReadOnlyList<ShopItem>? shop = null, IReadOnlyList<FishType>? fish = null,
        IReadOnlyList<TournamentTemplate>? tournaments = null) => new()
    {
        Fish = fish ?? [Fish("minnow")],
        Shop = shop ?? [],
        Settings = settings ?? new FishingSettings(LineSnapChance: 0, RodSnapChance: 0),
        Tournaments = tournaments ?? [],
    };

    /// <summary>A catalog where nothing ever snaps unless a setting says so, with one fish.</summary>
    public static FishingSettings Calm(double rod = 0, double line = 0, double reel = 0, double box = 0, double net = 0, double repair = 0, bool boost = false) =>
        new(BoostMode: boost, LineSnapChance: line, RodSnapChance: rod, ReelJamChance: reel, TackleBoxLostChance: box, NetBreakChance: net, RepairCostMultiplier: repair);
}

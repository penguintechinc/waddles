using System.Text;
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

namespace WaddleBundleSuperpenguinAudiocommand.Tests;

/// <summary>
/// In-memory economy that reproduces the HOST's money-safety rules for the compound ops these bundles use: every call is keyed by
/// (event id, kind, ordinal) and a replay returns the original result and moves nothing; a replay with different parameters is
/// `denied(idempotency_conflict)`; debit/wager/transfer name the invocation's actor only (`actor_mismatch`); credit is a mint
/// bounded by <see cref="MintCap"/>. <see cref="Moved"/> lists only calls that actually changed a balance.
/// </summary>
public sealed class HostLedger : IEconomyClient
{
    private readonly Dictionary<string, long> _balances = [];
    private readonly Dictionary<(string Event, string Kind, int Ordinal), (string Signature, long Result)> _done = [];
    private readonly Dictionary<string, int> _ordinals = [];

    public string EventId { get; private set; } = string.Empty;
    public string Actor { get; private set; } = string.Empty;
    public long MaxBetCap { get; set; } = 500;
    public ulong MintCap { get; set; } = ulong.MaxValue;
    public List<(string Kind, string User, ulong Amount)> Moved { get; } = [];

    /// <summary>Test hook: called before a call is applied; throw to simulate a crash between host calls.</summary>
    public Action<string, int>? BeforeCall { get; set; }

    public void Seed(string user, long balance) => _balances[user] = balance;

    public void BeginEvent(string eventId, string actor)
    {
        EventId = eventId;
        Actor = actor;
        _ordinals.Clear();
    }

    public long Supply => _balances.Values.Sum();

    private long Apply(string kind, string signature, Func<long> apply)
    {
        var ordinal = _ordinals.GetValueOrDefault(kind);
        _ordinals[kind] = ordinal + 1;
        if (_done.TryGetValue((EventId, kind, ordinal), out var prior))
        {
            if (prior.Signature != signature)
            {
                throw new WaddleEconomyException(EconomyErrorKind.Denied, "idempotency_conflict");
            }

            return prior.Result;
        }

        BeforeCall?.Invoke(kind, ordinal);
        var result = apply();
        _done[(EventId, kind, ordinal)] = (signature, result);
        return result;
    }

    private void RequireActor(string user)
    {
        if (user != Actor)
        {
            throw new WaddleEconomyException(EconomyErrorKind.Denied, "actor_mismatch");
        }
    }

    public long Balance(string user) => _balances.GetValueOrDefault(user);

    public long Wager(string user, ulong stake, ulong payout)
    {
        RequireActor(user);
        return Apply("wager", $"{user}|{stake}|{payout}", () =>
        {
            if (stake == 0)
            {
                throw new WaddleEconomyException(EconomyErrorKind.Invalid, "stake must be >= 1");
            }

            if ((long)stake > MaxBetCap)
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "stake over cap", (ulong)MaxBetCap);
            }

            if (payout > stake * 100)
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "payout over cap", stake * 100);
            }

            var held = Balance(user);
            if (held < (long)stake)
            {
                throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "insufficient funds", (ulong)held);
            }

            _balances[user] = held - (long)stake + (long)payout;
            Moved.Add(("wager", user, stake));
            return _balances[user];
        });
    }

    public long Debit(string user, ulong amount)
    {
        RequireActor(user);
        return Apply("debit", $"{user}|{amount}", () =>
        {
            var held = Balance(user);
            if (held < (long)amount)
            {
                throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "insufficient funds", (ulong)held);
            }

            _balances[user] = held - (long)amount;
            Moved.Add(("debit", user, amount));
            return _balances[user];
        });
    }

    public long Credit(string user, ulong amount) =>
        Apply("credit", $"{user}|{amount}", () =>
        {
            if (amount > MintCap)
            {
                throw new WaddleEconomyException(EconomyErrorKind.OverCap, "mint over cap", MintCap);
            }

            _balances[user] = Balance(user) + (long)amount;
            Moved.Add(("credit", user, amount));
            return _balances[user];
        });

    public long MaxBet(string user) => Math.Min(MaxBetCap, Balance(user));

    public void Transfer(string from, string to, ulong amount) => throw new NotSupportedException("not used by these bundles");

    public IReadOnlyList<EconomyEntry> Leaderboard(uint limit) => throw new NotSupportedException("not used by these bundles");

    public void BatchCredit(IReadOnlyList<EconomyCredit> entries) => throw new NotSupportedException("not used by these bundles");

    public long ConditionalDebit(string user, ulong amount, ulong minBalance) => throw new NotSupportedException("not used by these bundles");

    public void PayPot(IReadOnlyList<EconomyCredit> winners) => throw new NotSupportedException("not used by these bundles");
}

/// <summary>Scripted random source: yields the queued values in order (clamped into the requested range).</summary>
public sealed class ScriptedRandom(params int[] values)
{
    private readonly Queue<int> _values = new(values);
    public int Next(int min, int max) => Math.Clamp(_values.Dequeue(), min, max);
}

/// <summary>Identity for one event: the actor (or a scripted failure) and mention targets passed through as UUID tokens (or a scripted failure).</summary>
public sealed class WorldIdentity(string actor, World world) : IIdentityClient
{
    public string ResolveActor() => world.ActorFailure is null ? actor : throw world.ActorFailure;

    public string ResolveMention(string token) => world.MentionFailure is null ? token : throw world.MentionFailure;
}

/// <summary>One invocation's host: shared kv, ledger and clock across events, a fresh actor/message id per event.</summary>
public sealed class EventHost(BundleContextInfo context, World world, string actor) : IWaddleHostNext
{
    private readonly FakeWaddleHostNext _inner = new();
    private readonly WorldIdentity _identity = new(actor, world);

    public BundleContextInfo Context => context;
    public IKvClient Kv => world.Kv;
    public IDbClient Db => _inner.Db;
    public IRelayClient Relay => _inner.Relay;
    public IHttpClient Http => _inner.Http;
    public IFlagsClient Flags => world.Flags;
    public ILogClient Log => world.Log;
    public IClockClient Clock => world.Clock;
    public IEconomyClient Economy => world.Ledger;
    public IIdentityClient Identity => _identity;
    public IToolClient Tool => world.Tools;
    public IOverlayClient Overlay => world.Overlay;
    public IAiClient Ai => _inner.Ai;
    public IMediaClient Media => _inner.Media;
}

/// <summary>A tiny community: shared kv, ledger, clock, flags, log and economy-service tool, plus a `Send` that runs one chat event.</summary>
public sealed class World
{
    public const string Alice = "11111111-1111-4111-8111-111111111111";
    public const string Bob = "22222222-2222-4222-8222-222222222222";
    public const string Carol = "33333333-3333-4333-8333-333333333333";
    public const string Dave = "44444444-4444-4444-8444-444444444444";

    public FakeKvClient Kv { get; } = new();
    public IOverlayClient Overlay { get; set; } = new FakeOverlayClient();
    public FakeOverlayClient Pushed => (FakeOverlayClient)Overlay;
    public HostLedger Ledger { get; } = new();
    public MutableClock Clock { get; } = new(1_700_000_000_000UL);
    public FakeFlagsClient Flags { get; } = new();
    public FakeLogClient Log { get; } = new();
    public FakeToolClient Tools { get; } = new();
    public Exception? ActorFailure { get; set; }
    public Exception? MentionFailure { get; set; }

    public World(string flagKey, string alias = "stars")
    {
        EconomyToolFixture.Bind(Tools, Ledger);
        Flags.Overrides[flagKey] = true;
        Tools.Handlers[(EconomyServiceTools.Target, "points_view")] = _ =>
            Encoding.UTF8.GetBytes($"{{\"user\":\"{Alice}\",\"balance\":1,\"alias\":\"{alias}\"}}");
    }

    public EventHost HostFor(string actor, string messageId, string? community = "community-1")
    {
        Ledger.BeginEvent(messageId + "/" + actor, actor);
        return new EventHost(new BundleContextInfo("tenant-1", community, "waddles.integrations.superpenguin.test", "waddles.integrations.superpenguin", "1.0.0", messageId, "{}"), this, actor);
    }

    public static PlatformEventInfo Msg(string text, string platform = "twitch") =>
        new(platform, "chat.message", "platform-actor", $"{{\"text\":\"{text}\",\"channel_id\":\"chan-1\"}}", "2026-11-07T00:00:00.000Z");

    /// <summary>Runs one chat event as <paramref name="actor"/>; returns the reply text, or null when the bundle stayed silent.</summary>
    public string? Send(WaddleProcessStageNext logic, string actor, string text, string messageId, string platform = "twitch")
    {
        var reply = logic.Run(Msg(text, platform), HostFor(actor, messageId));
        return reply is null ? null : TextOf(reply);
    }

    public static string TextOf(PlatformEventInfo reply) =>
        Assert.IsType<ChatReplyPayload>(reply.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)).Text;
}

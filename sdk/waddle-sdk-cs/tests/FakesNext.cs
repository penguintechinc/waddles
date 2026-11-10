using WaddleSdk.Ai;
using WaddleSdk.Clock;
using WaddleSdk.Economy;
using WaddleSdk.Flags;
using WaddleSdk.Identity;
using WaddleSdk.Media;
using WaddleSdk.Overlay;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

namespace WaddleSdk.Tests;

/// <summary>A clock tests can advance.</summary>
public sealed class MutableClock(ulong nowMillis = 1_000_000) : IClockClient
{
    public ulong Now { get; set; } = nowMillis;
    public ulong NowMillis() => Now;
    public string NowRfc3339() => "2026-11-07T00:00:00.000Z";
    public ulong MonotonicNanos() => Now * 1_000_000;
}

/// <summary>
/// In-memory economy ledger that reproduces the host's money-safety rules the SDK contract depends on:
/// wagers are idempotent per (event id, call ordinal), stakes are bounded by `MaxBetCap` and by holdings, and a payout is
/// capped at 100x the stake. `EventId` is whatever the test says the current event is.
/// </summary>
public sealed class FakeEconomyClient : IEconomyClient
{
    private readonly Dictionary<string, long> _balances = [];
    private readonly Dictionary<(string Event, int Ordinal), long> _replay = [];
    private int _ordinal;

    public string EventId { get; set; } = "evt-1";
    public long MaxBetCap { get; set; } = 500;
    public List<(string User, ulong Stake, ulong Payout)> Wagers { get; } = [];
    public Exception? ThrowOnWager { get; set; }

    public void Seed(string user, long balance) => _balances[user] = balance;

    /// <summary>Marks the start of a new delivery of the same or a different event (resets the per-event call ordinal).</summary>
    public void BeginEvent(string eventId)
    {
        EventId = eventId;
        _ordinal = 0;
    }

    public long Balance(string user) => _balances.GetValueOrDefault(user);

    public long Wager(string user, ulong stake, ulong payout)
    {
        if (ThrowOnWager is not null)
        {
            throw ThrowOnWager;
        }

        Wagers.Add((user, stake, payout));
        var key = (EventId, _ordinal++);
        if (_replay.TryGetValue(key, out var original))
        {
            return original;
        }

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

        var next = held - (long)stake + (long)payout;
        _balances[user] = next;
        _replay[key] = next;
        return next;
    }

    public void Transfer(string from, string to, ulong amount)
    {
        _balances[from] = Balance(from) - (long)amount;
        _balances[to] = Balance(to) + (long)amount;
    }

    public long MaxBet(string user) => Math.Min(MaxBetCap, Balance(user));

    public IReadOnlyList<EconomyEntry> Leaderboard(uint limit) =>
        _balances.OrderByDescending(kv => kv.Value).Take((int)limit).Select(kv => new EconomyEntry(kv.Key, kv.Value)).ToList();

    public long Credit(string user, ulong amount) => _balances[user] = Balance(user) + (long)amount;

    public void BatchCredit(IReadOnlyList<EconomyCredit> entries)
    {
        foreach (var e in entries)
        {
            Credit(e.User, e.Amount);
        }
    }

    public long Debit(string user, ulong amount) => ConditionalDebit(user, amount, 0);

    public long ConditionalDebit(string user, ulong amount, ulong minBalance)
    {
        var held = Balance(user);
        if (held - (long)amount < (long)minBalance)
        {
            throw new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "insufficient funds", (ulong)held);
        }

        return _balances[user] = held - (long)amount;
    }

    public void PayPot(IReadOnlyList<EconomyCredit> winners) => BatchCredit(winners);
}

public sealed class FakeIdentityClient : IIdentityClient
{
    public string Actor { get; set; } = "11111111-1111-4111-8111-111111111111";
    public Exception? ThrowOnResolve { get; set; }

    public string ResolveActor() => ThrowOnResolve is null ? Actor : throw ThrowOnResolve;
    public string ResolveMention(string token) => token;
}

/// <summary>Answers tool calls from a handler map and records every call (target, tool, args, idempotency key).</summary>
public sealed class FakeToolClient : IToolClient
{
    public Dictionary<(string Target, string Tool), Func<byte[], byte[]>> Handlers { get; } = [];
    public List<(string Target, string Tool, string Args, string? Key)> Calls { get; } = [];

    public byte[] Invoke(string target, string tool, byte[] argsJson, string? idempotencyKey)
    {
        Calls.Add((target, tool, System.Text.Encoding.UTF8.GetString(argsJson), idempotencyKey));
        return Handlers.TryGetValue((target, tool), out var handler)
            ? handler(argsJson)
            : throw new WaddleToolException(ToolErrorKind.NotGranted, $"no grant for {target}/{tool}");
    }
}

public sealed class FakeOverlayClient : IOverlayClient
{
    public List<OverlayWidget> Registered { get; } = [];
    public List<(string WidgetId, OverlaySurface Surface, string Payload)> Pushes { get; } = [];

    public TransportResultInfo RegisterWidget(OverlayWidget widget)
    {
        Registered.Add(widget);
        return TransportResultInfo.Success();
    }

    public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson)
    {
        Pushes.Add((widgetId, surface, payloadJson));
        return TransportResultInfo.Success();
    }
}

public sealed class FakeAiClient : IAiClient
{
    public string Completion { get; set; } = "ok";
    public string Generate(string prompt, AiOptions options) => Completion;
}

public sealed class FakeMediaClient : IMediaClient
{
    public string GenerateImage(string prompt) => "handle-img";
    public string SubmitMusic(string prompt) => "job-music";
    public string SubmitVideo(string prompt) => "job-video";
    public MediaJobState JobStatus(string jobId) => MediaJobState.Done("handle-" + jobId);
    public string ObjectUrl(string handle) => "https://objects.invalid/" + handle;
}

/// <summary>A full <see cref="IWaddleHostNext"/>: the base fakes plus the newer-world capabilities.</summary>
public sealed class FakeWaddleHostNext : IWaddleHostNext
{
    public FakeWaddleHostNext() => EconomyToolFixture.Bind(ToolFake, EconomyFake);

    public FakeWaddleHost Base { get; } = new();

    /// <summary>Sets the invocation scope (community and message id) the stages under test see.</summary>
    public FakeWaddleHostNext WithScope(string? community, string messageId)
    {
        Base.Context = Base.Context with { Community = community, MessageId = messageId };
        return this;
    }

    public FakeEconomyClient EconomyFake { get; } = new();
    public FakeIdentityClient IdentityFake { get; } = new();
    public FakeToolClient ToolFake { get; } = new();
    public FakeOverlayClient OverlayFake { get; } = new();
    public FakeAiClient AiFake { get; } = new();
    public FakeMediaClient MediaFake { get; } = new();

    public Context.BundleContextInfo Context => Base.Context;
    public Kv.IKvClient Kv => Base.Kv;
    public Db.IDbClient Db => Base.Db;
    public Relay.IRelayClient Relay => Base.Relay;
    public Http.IHttpClient Http => Base.Http;
    public IFlagsClient Flags => Base.Flags;
    public Log.ILogClient Log => Base.Log;
    public IClockClient Clock => Base.Clock;
    public IEconomyClient Economy => EconomyFake;
    public IIdentityClient Identity => IdentityFake;
    public IToolClient Tool => ToolFake;
    public IOverlayClient Overlay => OverlayFake;
    public IAiClient Ai => AiFake;
    public IMediaClient Media => MediaFake;
}


/// <summary>Test transport for the real economy service's documented JSON contracts.</summary>
public static class EconomyToolFixture
{
    public static void Bind(FakeToolClient tools, IEconomyClient ledger)
    {
        foreach (var name in new[] { "get_balance", "adjust_balance" })
        {
            var op = name;
            tools.Handlers[(EconomyServiceTools.Target, op)] = bytes =>
            {
                using var doc = System.Text.Json.JsonDocument.Parse(bytes);
                var args = doc.RootElement;
                var user = args.GetProperty("user").GetString()!;
                try
                {
                    long balance;
                    switch (op)
                    {
                        case "get_balance": balance = ledger.Balance(user); break;
                        default:
                            var delta = args.GetProperty("delta").GetInt64();
                            balance = delta > 0 ? ledger.Credit(user, (ulong)delta) : ledger.Debit(user, (ulong)-delta);
                            break;
                    }
                    return System.Text.Encoding.UTF8.GetBytes($"{{\"user\":\"{user}\",\"balance\":{balance}}}");
                }
                catch (WaddleEconomyException ex)
                {
                    throw new WaddleToolException(ex.Kind switch { EconomyErrorKind.Denied => ToolErrorKind.Forbidden, EconomyErrorKind.Unavailable or EconomyErrorKind.Backend => ToolErrorKind.TargetUnavailable, _ => ToolErrorKind.InvalidArgs },
                        ex.Kind switch { EconomyErrorKind.InsufficientFunds => "insufficient_funds", EconomyErrorKind.OverCap => "over_cap", EconomyErrorKind.NotAMember => "not_a_member", _ => ex.Message });
                }
            };
        }
    }
}

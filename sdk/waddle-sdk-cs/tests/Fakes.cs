using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Flags;
using WaddleSdk.Http;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Relay;
using WaddleSdk.Stage;

namespace WaddleSdk.Tests;

/// <summary>In-memory <see cref="IKvClient"/> fake -- TTLs are recorded but not
/// expired (tests assert cooldown *acquisition* logic, not real wall-clock expiry).</summary>
public sealed class FakeKvClient : IKvClient
{
    private readonly Dictionary<string, byte[]> _store = [];
    public List<(string Key, byte[] Value, uint TtlSeconds)> SetCalls { get; } = [];

    public byte[]? Get(string key) => _store.TryGetValue(key, out var v) ? v : null;

    public void Set(string key, byte[] value, uint ttlSeconds)
    {
        _store[key] = value;
        SetCalls.Add((key, value, ttlSeconds));
    }

    public void Delete(string key) => _store.Remove(key);

    public long Increment(string key, long delta, uint ttlSeconds)
    {
        var current = _store.TryGetValue(key, out var bytes) ? BitConverter.ToInt64(bytes) : 0L;
        var next = current + delta;
        _store[key] = BitConverter.GetBytes(next);
        return next;
    }
}

/// <summary>
/// Simulates the current (2026-09-28) host state: `kv`/`db` hardcoded to deny
/// every call in svc-process/svc-action, ahead of the real backend landing.
/// Every <see cref="IKvClient"/>/<see cref="IDbClient"/> method throws the
/// SDK's typed denial exception, exactly what a bundle's real per-project
/// adapter should surface for a `denied` host response.
/// </summary>
public sealed class DenyingKvClient : IKvClient
{
    private static WaddleKvException Denied() => new(KvErrorKind.Denied, "kv capability denied by host");

    public byte[]? Get(string key) => throw Denied();
    public void Set(string key, byte[] value, uint ttlSeconds) => throw Denied();
    public void Delete(string key) => throw Denied();
    public long Increment(string key, long delta, uint ttlSeconds) => throw Denied();
}

public sealed class DenyingDbClient : IDbClient
{
    public DbRows Execute(string statement, IReadOnlyList<DbValue> parameters) =>
        throw new WaddleDbException(DbErrorKind.Denied, "db capability denied by host");
}

/// <summary>An <see cref="IKvClient"/> that throws a caller-supplied exception from
/// every method -- used to prove a genuine (non-`denied`) `kv` failure is NOT
/// swallowed by <see cref="CooldownGuard"/>.</summary>
public sealed class FaultyKvClient(Exception toThrow) : IKvClient
{
    public byte[]? Get(string key) => throw toThrow;
    public void Set(string key, byte[] value, uint ttlSeconds) => throw toThrow;
    public void Delete(string key) => throw toThrow;
    public long Increment(string key, long delta, uint ttlSeconds) => throw toThrow;
}

/// <summary>Records every <see cref="IRelayClient.Push"/> call for assertion.</summary>
public sealed class FakeRelayClient : IRelayClient
{
    public List<(string Provider, string MessageJson)> Pushes { get; } = [];
    public Exception? ThrowOnPush { get; set; }

    public void Push(string provider, string messageJson)
    {
        if (ThrowOnPush is not null)
        {
            throw ThrowOnPush;
        }

        Pushes.Add((provider, messageJson));
    }
}

public sealed class FakeDbClient : IDbClient
{
    public DbRows? NextResult { get; set; }

    public DbRows Execute(string statement, IReadOnlyList<DbValue> parameters) =>
        NextResult ?? new DbRows([], [], 0);
}

public sealed class FakeHttpClient : IHttpClient
{
    public HttpResponseInfo Send(HttpRequestInfo request) => new(200, [], [], false);
}

public sealed class FakeFlagsClient : IFlagsClient
{
    public bool Enabled(string key, bool defaultValue) => defaultValue;
    public string Tier() => "free";
}

public sealed class FakeLogClient : ILogClient
{
    public List<(WaddleLogLevel Level, string Message, string FieldsJson)> Writes { get; } = [];
    public void Write(WaddleLogLevel level, string message, string fieldsJson) => Writes.Add((level, message, fieldsJson));
}

public sealed class FakeClockClient : IClockClient
{
    public ulong NowMillis() => 1_000_000;
    public string NowRfc3339() => "2026-09-28T00:00:00.000Z";
    public ulong MonotonicNanos() => 42;
}

/// <summary>A full <see cref="IWaddleHost"/> built from the fakes above, ready to
/// hand to a <see cref="WaddleProcessStage"/>/<see cref="WaddleActionStage"/> under test.</summary>
public sealed class FakeWaddleHost : IWaddleHost
{
    public BundleContextInfo Context { get; init; } = new("tenant-1", null, "waddles.test.app", "waddles.test", "1.0.0", "msg-1", "{}");
    public FakeKvClient KvFake { get; } = new();
    public FakeDbClient DbFake { get; } = new();
    public FakeRelayClient RelayFake { get; } = new();
    public FakeHttpClient HttpFake { get; } = new();
    public FakeFlagsClient FlagsFake { get; } = new();
    public FakeLogClient LogFake { get; } = new();
    public FakeClockClient ClockFake { get; } = new();

    public IKvClient Kv => KvFake;
    public IDbClient Db => DbFake;
    public IRelayClient Relay => RelayFake;
    public IHttpClient Http => HttpFake;
    public IFlagsClient Flags => FlagsFake;
    public ILogClient Log => LogFake;
    public IClockClient Clock => ClockFake;
}

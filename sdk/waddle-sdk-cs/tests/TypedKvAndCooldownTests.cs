using WaddleSdk.Chat;
using WaddleSdk.Cooldown;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using Xunit;

namespace WaddleSdk.Tests;

public class TypedKvTests
{
    [Fact]
    public void set_json_then_get_json_round_trips()
    {
        var kv = new FakeKvClient();
        var value = new ChatReplyPayload("pong", "12345");

        TypedKv.SetJson(kv, "reply:1", value, WaddleSdkJsonContext.Default.ChatReplyPayload, ttlSeconds: 60);
        var result = TypedKv.GetJson(kv, "reply:1", WaddleSdkJsonContext.Default.ChatReplyPayload);

        Assert.Equal(value, result);
        Assert.Equal(60u, kv.SetCalls[0].TtlSeconds);
    }

    [Fact]
    public void get_json_returns_default_for_unset_key()
    {
        var kv = new FakeKvClient();
        Assert.Null(TypedKv.GetJson(kv, "missing", WaddleSdkJsonContext.Default.ChatReplyPayload));
    }

    [Fact]
    public void get_json_returns_default_for_malformed_bytes()
    {
        var kv = new FakeKvClient();
        kv.Set("bad", "not json"u8.ToArray(), 0);
        Assert.Null(TypedKv.GetJson(kv, "bad", WaddleSdkJsonContext.Default.ChatReplyPayload));
    }
}

public class CooldownGuardTests
{
    [Fact]
    public void first_acquisition_succeeds_and_sets_a_key()
    {
        var kv = new FakeKvClient();
        Assert.True(CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
        Assert.Single(kv.SetCalls);
        Assert.Equal(180u, kv.SetCalls[0].TtlSeconds);
    }

    [Fact]
    public void second_acquisition_within_cooldown_fails()
    {
        var kv = new FakeKvClient();
        Assert.True(CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
        Assert.False(CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
    }

    [Fact]
    public void different_users_have_independent_cooldowns()
    {
        var kv = new FakeKvClient();
        Assert.True(CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
        Assert.True(CooldownGuard.TryAcquire(kv, "roll", "user-2", 180));
    }

    [Fact]
    public void different_scopes_have_independent_cooldowns()
    {
        var kv = new FakeKvClient();
        Assert.True(CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
        Assert.True(CooldownGuard.TryAcquire(kv, "dice", "user-1", 180));
    }

    [Fact]
    public void degrades_gracefully_and_allows_the_command_when_kv_is_denied()
    {
        // Regression: as of 2026-09-28 the host `kv` capability is hardcoded to
        // deny every call in svc-process/svc-action. A cooldown must never turn
        // that into a hard failure for the whole command.
        var kv = new DenyingKvClient();
        Assert.True(CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
    }

    [Fact]
    public void does_not_swallow_a_genuine_non_denied_kv_failure()
    {
        var kv = new FaultyKvClient(new WaddleKvException(KvErrorKind.Backend, "storage unreachable"));
        var ex = Assert.Throws<WaddleKvException>(() => CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
        Assert.Equal(KvErrorKind.Backend, ex.Kind);
    }

    [Fact]
    public void does_not_swallow_an_unrelated_exception_type()
    {
        var kv = new FaultyKvClient(new InvalidOperationException("unexpected"));
        Assert.Throws<InvalidOperationException>(() => CooldownGuard.TryAcquire(kv, "roll", "user-1", 180));
    }
}

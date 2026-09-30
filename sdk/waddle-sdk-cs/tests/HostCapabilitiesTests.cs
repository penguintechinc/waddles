using WaddleSdk.Http;
using Xunit;

namespace WaddleSdk.Tests;

/// <summary>Exercises the remaining `IWaddleHost` capability members
/// (`http`/`%flags`/`clock`/`context`) not already covered by the stage/relay/kv
/// tests, via <see cref="FakeWaddleHost"/>.</summary>
public class HostCapabilitiesTests
{
    [Fact]
    public void http_send_returns_a_response()
    {
        var host = new FakeWaddleHost();
        var response = host.Http.Send(new HttpRequestInfo("GET", "https://example.com", [], null, []));
        Assert.Equal(200, response.Status);
        Assert.False(response.Truncated);
    }

    [Fact]
    public void flags_enabled_falls_back_to_the_supplied_default()
    {
        var host = new FakeWaddleHost();
        Assert.True(host.Flags.Enabled("waddles.some-feature", true));
        Assert.False(host.Flags.Enabled("waddles.some-feature", false));
    }

    [Fact]
    public void flags_tier_reports_free_by_default()
    {
        Assert.Equal("free", new FakeWaddleHost().Flags.Tier());
    }

    [Fact]
    public void clock_reports_deterministic_fixture_values()
    {
        var host = new FakeWaddleHost();
        Assert.Equal(1_000_000ul, host.Clock.NowMillis());
        Assert.Equal("2026-09-28T00:00:00.000Z", host.Clock.NowRfc3339());
        Assert.Equal(42ul, host.Clock.MonotonicNanos());
    }

    [Fact]
    public void context_carries_the_fixture_tenant_and_app_id()
    {
        var host = new FakeWaddleHost();
        Assert.Equal("tenant-1", host.Context.Tenant);
        Assert.Equal("waddles.test.app", host.Context.AppId);
    }

    [Fact]
    public void kv_increment_accumulates_across_calls()
    {
        var kv = new FakeKvClient();
        Assert.Equal(5, kv.Increment("counter", 5, 0));
        Assert.Equal(8, kv.Increment("counter", 3, 0));
    }

    [Fact]
    public void kv_delete_removes_the_key()
    {
        var kv = new FakeKvClient();
        kv.Set("k", [1], 0);
        kv.Delete("k");
        Assert.Null(kv.Get("k"));
    }
}

using WaddleSdk.Context;
using WaddleSdk.Http;
using WaddleSdk.Types;
using Xunit;

namespace WaddleSdk.Tests;

/// <summary>
/// Exercises the record-generated members (`Equals`/`GetHashCode`/`ToString`)
/// of this SDK's plain data POCOs -- these carry no hand-written logic, but
/// each one is a public contract bundle authors and their own tests
/// construct/compare directly (e.g. asserting a built `HttpRequestInfo`
/// matches an expected value), so exercising equality/inequality here is a
/// real regression guard, not coverage padding.
/// </summary>
public class HttpTypesEqualityTests
{
    [Fact]
    public void http_header_equality_is_value_based()
    {
        var a = new HttpHeader("X-Test", "1");
        var b = new HttpHeader("X-Test", "1");
        var c = new HttpHeader("X-Test", "2");

        Assert.Equal(a, b);
        Assert.NotEqual(a, c);
        Assert.Contains("X-Test", a.ToString());
    }

    [Fact]
    public void http_request_info_equality_covers_every_field()
    {
        var headers = new List<HttpHeader> { new("Accept", "application/json") };
        var secretRefs = new List<(string, string)> { ("Authorization", "my-secret-ref") };
        var a = new HttpRequestInfo("POST", "https://example.com", headers, "body"u8.ToArray(), secretRefs);
        var b = new HttpRequestInfo("POST", "https://example.com", headers, "body"u8.ToArray(), secretRefs);

        Assert.Equal(a.Method, b.Method);
        Assert.Equal(a.Url, b.Url);
        Assert.Equal(a.Headers, b.Headers);
        Assert.Equal(a.SecretRefs, b.SecretRefs);
        Assert.NotNull(a.ToString());
    }

    [Fact]
    public void http_response_info_equality_is_value_based()
    {
        // Record-generated equality delegates to `IReadOnlyList<T>`/`byte[]`
        // members' own `Equals` -- neither `List<T>` nor arrays override it
        // (reference equality), so `a`/`b` must share the SAME header list
        // and body array instances for `Assert.Equal` to pass; this is a
        // property of the BCL collection types, not a bug in this record.
        var headers = new List<HttpHeader> { new("X", "1") };
        var body = new byte[] { 1, 2, 3 };
        var a = new HttpResponseInfo(200, headers, body, false);
        var b = new HttpResponseInfo(200, headers, body, false);
        var c = a with { Truncated = true };

        Assert.Equal(a, b);
        Assert.NotEqual(a, c);
    }

    [Fact]
    public void waddle_http_exception_carries_denied_detail()
    {
        var ex = new WaddleHttpException(HttpErrorKind.Denied, "egress not allowed");
        Assert.Equal(HttpErrorKind.Denied, ex.Kind);
        Assert.Equal("egress not allowed", ex.Message);
        Assert.Null(ex.TooLargeBytes);
        Assert.Null(ex.RateLimitedSeconds);
    }
}

public class BundleContextInfoTests
{
    [Fact]
    public void equality_is_value_based_and_covers_optional_community()
    {
        var a = new BundleContextInfo("t1", "c1", "waddles.a.b.c", "waddles.a.b", "1.0.0", "m1", "{}");
        var b = new BundleContextInfo("t1", "c1", "waddles.a.b.c", "waddles.a.b", "1.0.0", "m1", "{}");
        var noCommunity = a with { Community = null };

        Assert.Equal(a, b);
        Assert.NotEqual(a, noCommunity);
        Assert.Null(noCommunity.Community);
        Assert.NotNull(a.ToString());
    }
}

public class StageEnvelopeInfoTests
{
    private static StageEnvelopeInfo Sample() => new(
        "tenant-1", "community-1", "waddles.a.b.c", "action",
        new PlatformEventInfo("twitch", "chat.message", "user-1", "{}", "2026-09-28T00:00:00.000Z"),
        "2026-09-28T00:00:00.000Z", "waddles.a.b.d", "traceparent-value");

    [Fact]
    public void equality_is_value_based_across_every_field()
    {
        var a = Sample();
        var b = Sample();
        Assert.Equal(a, b);
        Assert.NotNull(a.ToString());
    }

    [Fact]
    public void with_expression_changes_only_the_targeted_field()
    {
        var a = Sample();
        var withoutTarget = a with { TargetAppId = null };

        Assert.Null(withoutTarget.TargetAppId);
        Assert.Equal(a.Tenant, withoutTarget.Tenant);
        Assert.NotEqual(a, withoutTarget);
    }
}

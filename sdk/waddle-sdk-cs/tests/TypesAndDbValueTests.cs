using WaddleSdk.Chat;
using WaddleSdk.Db;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using WaddleSdk.Relay;
using WaddleSdk.Types;
using Xunit;

namespace WaddleSdk.Tests;

public class PlatformEventInfoTests
{
    [Fact]
    public void payload_deserializes_matching_json()
    {
        var evt = new PlatformEventInfo("twitch", "chat.message", "user-1", """{"text":"!roll","channel_id":"1"}""", "2026-09-28T00:00:00.000Z");
        var payload = evt.Payload(WaddleSdkJsonContext.Default.ChatMessagePayload);
        Assert.NotNull(payload);
        Assert.Equal("!roll", payload!.Text);
    }

    [Fact]
    public void payload_returns_null_for_malformed_json()
    {
        var evt = new PlatformEventInfo("twitch", "chat.message", null, "not json", "2026-09-28T00:00:00.000Z");
        Assert.Null(evt.Payload(WaddleSdkJsonContext.Default.ChatMessagePayload));
    }

    [Fact]
    public void with_payload_copies_every_other_field_and_replaces_payload_json()
    {
        var source = new PlatformEventInfo("discord", "chat.message", "user-2", "{}", "2026-09-28T00:00:00.000Z");
        var reply = new ChatReplyPayload("pong", "42");

        var result = PlatformEventInfo.WithPayload(source, reply, WaddleSdkJsonContext.Default.ChatReplyPayload);

        Assert.Equal(source.Platform, result.Platform);
        Assert.Equal(source.EventType, result.EventType);
        Assert.Equal(source.Actor, result.Actor);
        Assert.Equal(source.OccurredAt, result.OccurredAt);
        var roundTripped = result.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload);
        Assert.Equal(reply, roundTripped);
    }
}

public class TransportResultAndErrorTests
{
    [Fact]
    public void success_defaults_ok_true_with_no_status()
    {
        var result = TransportResultInfo.Success(detail: "twitch");
        Assert.True(result.Ok);
        Assert.Null(result.Status);
        Assert.Equal("twitch", result.Detail);
    }

    [Fact]
    public void fatal_is_never_retryable()
    {
        var error = TransportErrorInfo.Fatal("BAD_PAYLOAD", "nope");
        Assert.False(error.Retryable);
        Assert.Equal("BAD_PAYLOAD", error.Code);
    }

    [Fact]
    public void retryable_is_always_retryable()
    {
        var error = TransportErrorInfo.RetryableError("RELAY_PUSH_FAILED", "transient", retryAfterMs: 500);
        Assert.True(error.Retryable);
        Assert.Equal(500u, error.RetryAfterMs);
    }

    [Fact]
    public void waddle_transport_exception_carries_its_error()
    {
        var error = TransportErrorInfo.Fatal("X", "y");
        var ex = new WaddleTransportException(error);
        Assert.Same(error, ex.Error);
        Assert.Equal("y", ex.Message);
    }
}

public class DbValueTests
{
    [Fact]
    public void of_bool_sets_kind_and_value()
    {
        var v = DbValue.Of(true);
        Assert.Equal(DbValueKind.Bool, v.Kind);
        Assert.True(v.BoolValue);
    }

    [Fact]
    public void of_int_sets_kind_and_value()
    {
        var v = DbValue.Of(42L);
        Assert.Equal(DbValueKind.Int, v.Kind);
        Assert.Equal(42L, v.IntValue);
    }

    [Fact]
    public void of_float_sets_kind_and_value()
    {
        var v = DbValue.Of(1.5);
        Assert.Equal(DbValueKind.Float, v.Kind);
        Assert.Equal(1.5, v.FloatValue);
    }

    [Fact]
    public void of_text_sets_kind_and_value()
    {
        var v = DbValue.Of("hi");
        Assert.Equal(DbValueKind.Text, v.Kind);
        Assert.Equal("hi", v.TextValue);
    }

    [Fact]
    public void of_bytes_sets_kind_and_value()
    {
        byte[] bytes = [1, 2, 3];
        var v = DbValue.Of(bytes);
        Assert.Equal(DbValueKind.Bytes, v.Kind);
        Assert.Equal(bytes, v.BytesValue);
    }

    [Fact]
    public void null_sets_kind_only()
    {
        var v = DbValue.Null();
        Assert.Equal(DbValueKind.Null, v.Kind);
    }

    [Fact]
    public void db_exception_carries_kind_and_message()
    {
        var ex = new WaddleDbException(DbErrorKind.Conflict, "dup key");
        Assert.Equal(DbErrorKind.Conflict, ex.Kind);
        Assert.Equal("dup key", ex.Message);
    }

    [Fact]
    public void db_denied_is_distinguishable_from_other_db_errors()
    {
        var db = new DenyingDbClient();
        var ex = Assert.Throws<WaddleDbException>(() => db.Execute("SELECT 1", []));
        Assert.Equal(DbErrorKind.Denied, ex.Kind);
    }
}

public class ExceptionKindTests
{
    [Fact]
    public void kv_exception_carries_too_large_bytes()
    {
        var ex = new WaddleKvException(KvErrorKind.TooLarge, "too big", 1024);
        Assert.Equal(KvErrorKind.TooLarge, ex.Kind);
        Assert.Equal(1024ul, ex.TooLargeBytes);
    }

    [Fact]
    public void kv_denied_is_distinguishable_from_other_kv_errors()
    {
        var kv = new DenyingKvClient();
        var ex = Assert.Throws<WaddleKvException>(() => kv.Get("any"));
        Assert.Equal(KvErrorKind.Denied, ex.Kind);
    }

    [Fact]
    public void relay_exception_carries_kind()
    {
        var ex = new WaddleRelayException(RelayErrorKind.Denied, "no");
        Assert.Equal(RelayErrorKind.Denied, ex.Kind);
    }

    [Fact]
    public void http_exception_carries_rate_limit_seconds()
    {
        var ex = new Http.WaddleHttpException(Http.HttpErrorKind.RateLimited, "slow down", rateLimitedSeconds: 30);
        Assert.Equal(Http.HttpErrorKind.RateLimited, ex.Kind);
        Assert.Equal(30u, ex.RateLimitedSeconds);
    }
}

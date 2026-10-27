using WaddleSdk.Chat;
using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Flags;
using WaddleSdk.Http;
using WaddleSdk.Json;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Types;
using WaddleBundleCsping;
using Xunit;

namespace WaddleBundleCsping.Tests;

// Minimal local fakes -- deliberately not shared with sdk/waddle-sdk-cs/tests
// (a separate xUnit test project; see this project's own csproj header
// comment for why it cannot reference the wasi-wasm bundle project either).
// Mirrors bundles/csharp/superpenguin-roll/tests/RollLogicTests.cs's fakes.
file sealed class FakeRelayClient : IRelayClient
{
    public List<(string Provider, string MessageJson)> Pushes { get; } = [];
    public void Push(string provider, string messageJson) => Pushes.Add((provider, messageJson));
}

file sealed class FailingRelayClient : IRelayClient
{
    public void Push(string provider, string messageJson) =>
        throw new WaddleRelayException(RelayErrorKind.Backend, "relay backend unavailable");
}

file sealed class RecordingLog : ILogClient
{
    public List<(WaddleLogLevel Level, string Message, string FieldsJson)> Writes { get; } = [];
    public void Write(WaddleLogLevel level, string message, string fieldsJson) => Writes.Add((level, message, fieldsJson));
}

file sealed class FakeHost(IRelayClient relay, ILogClient? log = null) : IWaddleHost
{
    public BundleContextInfo Context { get; } = new("tenant-1", null, "waddles.core.example.csping", "waddles.core.example", "1.0.0", "msg-1", "{}");
    public IKvClient Kv => throw new NotSupportedException("!csping needs no kv");
    public IDbClient Db => throw new NotSupportedException("!csping needs no db");
    public IRelayClient Relay { get; } = relay;
    public IHttpClient Http => throw new NotSupportedException("!csping needs no http");
    public IFlagsClient Flags { get; } = new NoopFlags();
    public ILogClient Log { get; } = log ?? new NoopLog();
    public IClockClient Clock { get; } = new FixedClock();

    private sealed class NoopFlags : IFlagsClient
    {
        public bool Enabled(string key, bool defaultValue) => defaultValue;
        public string Tier() => "free";
    }

    private sealed class NoopLog : ILogClient
    {
        public void Write(WaddleLogLevel level, string message, string fieldsJson) { }
    }

    private sealed class FixedClock : IClockClient
    {
        public ulong NowMillis() => 0;
        public string NowRfc3339() => "2026-10-03T00:00:00.000Z";
        public ulong MonotonicNanos() => 0;
    }
}

public class CspingLogicTransformTests
{
    private static PlatformEventInfo ChatEvent(string platform, string text, string? actor = "user-1", string? channelId = "42") =>
        new(platform, "chat.message", actor,
            System.Text.Json.JsonSerializer.Serialize(new ChatMessagePayload(text, channelId), WaddleSdkJsonContext.Default.ChatMessagePayload),
            "2026-10-03T00:00:00.000Z");

    [Theory]
    [InlineData("twitch")]
    [InlineData("discord")]
    public void matching_command_produces_a_pong_reply_on_the_same_platform_and_channel(string platform)
    {
        var host = new FakeHost(new FakeRelayClient());
        var result = new CspingLogic().Run(ChatEvent(platform, "!csping"), host);

        Assert.NotNull(result);
        Assert.Equal(platform, result!.Platform);
        var reply = result.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload);
        Assert.NotNull(reply);
        Assert.Equal("pong (c#)", reply!.Text);
        Assert.Equal("42", reply.ChannelId);
    }

    [Fact]
    public void non_matching_text_produces_no_reply()
    {
        var host = new FakeHost(new FakeRelayClient());
        Assert.Null(new CspingLogic().Run(ChatEvent("twitch", "hello"), host));
    }

    [Fact]
    public void non_json_payload_produces_no_reply()
    {
        var host = new FakeHost(new FakeRelayClient());
        var @event = new PlatformEventInfo("twitch", "chat.message", "user-1", "not json", "2026-10-03T00:00:00.000Z");
        Assert.Null(new CspingLogic().Run(@event, host));
    }

    [Fact]
    public void command_prefix_without_the_exact_name_produces_no_reply()
    {
        var host = new FakeHost(new FakeRelayClient());
        Assert.Null(new CspingLogic().Run(ChatEvent("twitch", "!csping-not-quite"), host));
    }

    [Fact]
    public void missing_channel_id_still_produces_a_reply_with_a_null_channel()
    {
        var host = new FakeHost(new FakeRelayClient());
        var result = new CspingLogic().Run(ChatEvent("twitch", "!csping", channelId: null), host);

        Assert.NotNull(result);
        var reply = result!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload);
        Assert.NotNull(reply);
        Assert.Null(reply!.ChannelId);
    }
}

public class CspingDispatchTests
{
    private static StageEnvelopeInfo EnvelopeWithReply(string platform, string text, string? channelId) =>
        new(
            "tenant-1", null, "waddles.core.example.csping", "action",
            new PlatformEventInfo(
                platform,
                "chat.message",
                "user-1",
                System.Text.Json.JsonSerializer.Serialize(new ChatReplyPayload(text, channelId), WaddleSdkJsonContext.Default.ChatReplyPayload),
                "2026-10-03T00:00:00.000Z"),
            "2026-10-03T00:00:00.000Z", null, null);

    [Theory]
    [InlineData("twitch")]
    [InlineData("discord")]
    public void dispatch_relays_to_the_events_own_origin_platform_not_a_hardcoded_one(string platform)
    {
        var relay = new FakeRelayClient();
        var host = new FakeHost(relay);

        var result = new CspingDispatch().Run(EnvelopeWithReply(platform, "pong (c#)", "42"), "{}", host);

        Assert.True(result.Ok);
        Assert.Equal(platform, result.Detail);
        Assert.Single(relay.Pushes);
        Assert.Equal(platform, relay.Pushes[0].Provider);
    }

    [Fact]
    public void throws_a_fatal_error_when_channel_id_is_missing()
    {
        var host = new FakeHost(new FakeRelayClient());
        var ex = Assert.Throws<WaddleTransportException>(
            () => new CspingDispatch().Run(EnvelopeWithReply("twitch", "pong (c#)", null), "{}", host));

        Assert.Equal("MISSING_CHANNEL", ex.Error.Code);
        Assert.False(ex.Error.Retryable);
    }

    [Fact]
    public void throws_a_retryable_error_when_relay_push_fails()
    {
        var host = new FakeHost(new FailingRelayClient());
        var ex = Assert.Throws<WaddleTransportException>(
            () => new CspingDispatch().Run(EnvelopeWithReply("twitch", "pong (c#)", "42"), "{}", host));

        Assert.Equal("RELAY_PUSH_FAILED", ex.Error.Code);
        Assert.True(ex.Error.Retryable);
    }

    [Fact]
    public void throws_a_fatal_error_for_a_non_reply_payload()
    {
        var host = new FakeHost(new FakeRelayClient());
        // A JSON array (not an object) fails to deserialize as `ChatReplyPayload`
        // -- `{}` would NOT reproduce this (see superpenguin-roll's equivalent
        // test for the full explanation: an empty object "successfully"
        // parses into a reply with null Text/ChannelId and hits the
        // MISSING_CHANNEL path instead).
        var envelope = new StageEnvelopeInfo(
            "tenant-1", null, "waddles.core.example.csping", "action",
            new PlatformEventInfo("twitch", "chat.message", "user-1", "[]", "2026-10-03T00:00:00.000Z"),
            "2026-10-03T00:00:00.000Z", null, null);

        var ex = Assert.Throws<WaddleTransportException>(() => new CspingDispatch().Run(envelope, "{}", host));
        Assert.Equal("BAD_PAYLOAD", ex.Error.Code);
    }
}

public class CspingMatchingTests
{
    private static PlatformEventInfo ChatEvent(string text, string? actor = "user-1", string platform = "twitch") =>
        new(platform, "chat.message", actor,
            System.Text.Json.JsonSerializer.Serialize(new ChatMessagePayload(text, "42"), WaddleSdkJsonContext.Default.ChatMessagePayload),
            "2026-10-09T00:00:00.000Z");

    [Theory]
    [InlineData("!csping")]
    [InlineData("  !csping  ")]
    [InlineData("!csping extra arguments are ignored")]
    public void command_name_matches_ignoring_surrounding_whitespace_and_trailing_arguments(string text)
    {
        var result = new CspingLogic().Run(ChatEvent(text), new FakeHost(new FakeRelayClient()));

        Assert.NotNull(result);
        Assert.Equal("pong (c#)", result!.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)!.Text);
    }

    [Theory]
    [InlineData("!CSPING")]
    [InlineData("!Csping")]
    [InlineData("csping")]
    [InlineData("!")]
    [InlineData("")]
    [InlineData("   ")]
    public void command_names_are_case_sensitive_and_a_bare_prefix_or_blank_never_matches(string text)
    {
        Assert.Null(new CspingLogic().Run(ChatEvent(text), new FakeHost(new FakeRelayClient())));
    }

    [Fact]
    public void non_object_json_payload_produces_no_reply()
    {
        var @event = new PlatformEventInfo("twitch", "chat.message", "user-1", "[]", "2026-10-09T00:00:00.000Z");
        Assert.Null(new CspingLogic().Run(@event, new FakeHost(new FakeRelayClient())));
    }

    [Fact]
    public void reply_keeps_platform_event_type_actor_and_timestamp_unchanged()
    {
        var result = new CspingLogic().Run(ChatEvent("!csping", actor: "user-9", platform: "discord"), new FakeHost(new FakeRelayClient()));

        Assert.NotNull(result);
        Assert.Equal("discord", result!.Platform);
        Assert.Equal("chat.message", result.EventType);
        Assert.Equal("user-9", result.Actor);
        Assert.Equal("2026-10-09T00:00:00.000Z", result.OccurredAt);
    }
}

public class CspingHygieneTests
{
    private const string PiiActor = "PIIACTOR_alice";
    private const string PiiText = "PIITEXT_secret_phrase";

    private static StageEnvelopeInfo EnvelopeFor(PlatformEventInfo reply) =>
        new("tenant-1", null, "waddles.core.example.csping", "action", reply, "2026-10-09T00:00:00.000Z", null, null);

    // regression: gh-674 -- a bundle runs outside the PII boundary: neither the actor nor typed
    // text may be copied into a reply payload, a relay message or any log line, on any path.
    [Fact]
    public void actor_and_typed_text_never_reach_the_reply_payload_the_relay_message_or_any_log()
    {
        var log = new RecordingLog();
        var relay = new FakeRelayClient();
        var host = new FakeHost(relay, log);
        var @event = new PlatformEventInfo(
            "twitch", "chat.message", PiiActor,
            System.Text.Json.JsonSerializer.Serialize(new ChatMessagePayload($"!csping {PiiText}", "42"), WaddleSdkJsonContext.Default.ChatMessagePayload),
            "2026-10-09T00:00:00.000Z");

        var reply = new CspingLogic().Run(@event, host);
        Assert.NotNull(reply);
        Assert.DoesNotContain(PiiActor, reply!.PayloadJson);
        Assert.DoesNotContain(PiiText, reply.PayloadJson);

        var result = new CspingDispatch().Run(EnvelopeFor(reply), "{}", host);
        Assert.True(result.Ok);
        Assert.Single(relay.Pushes);
        Assert.Contains("pong (c#)", relay.Pushes[0].MessageJson);
        Assert.DoesNotContain(PiiActor, relay.Pushes[0].MessageJson);
        Assert.DoesNotContain(PiiText, relay.Pushes[0].MessageJson);

        Assert.All(log.Writes, w =>
        {
            Assert.DoesNotContain(PiiActor, w.Message + w.FieldsJson);
            Assert.DoesNotContain(PiiText, w.Message + w.FieldsJson);
        });
    }

    [Fact]
    public void dispatch_error_messages_never_contain_the_actor_or_typed_text()
    {
        var host = new FakeHost(new FakeRelayClient());
        var noChannel = new PlatformEventInfo(
            "twitch", "chat.message", PiiActor,
            System.Text.Json.JsonSerializer.Serialize(new ChatReplyPayload(PiiText, null), WaddleSdkJsonContext.Default.ChatReplyPayload),
            "2026-10-09T00:00:00.000Z");
        var ex = Assert.Throws<WaddleTransportException>(() => new CspingDispatch().Run(EnvelopeFor(noChannel), "{}", host));

        Assert.Equal("MISSING_CHANNEL", ex.Error.Code);
        Assert.DoesNotContain(PiiActor, ex.Error.Message);
        Assert.DoesNotContain(PiiText, ex.Error.Message);
    }

    [Fact]
    public void relay_failure_is_a_retryable_error_not_a_swallowed_one()
    {
        var host = new FakeHost(new FailingRelayClient());
        var reply = new PlatformEventInfo(
            "discord", "chat.message", PiiActor,
            System.Text.Json.JsonSerializer.Serialize(new ChatReplyPayload("pong (c#)", "42"), WaddleSdkJsonContext.Default.ChatReplyPayload),
            "2026-10-09T00:00:00.000Z");

        var ex = Assert.Throws<WaddleTransportException>(() => new CspingDispatch().Run(EnvelopeFor(reply), "{}", host));
        Assert.True(ex.Error.Retryable);
        Assert.Equal("RELAY_PUSH_FAILED", ex.Error.Code);
        Assert.DoesNotContain(PiiActor, ex.Error.Message);
    }
}

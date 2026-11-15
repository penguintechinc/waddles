using WaddleSdk.Relay;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using WaddleBundleSuperpenguinSteal;
using Xunit;

namespace WaddleBundleSuperpenguinSteal.Tests;

public class StealDispatchTests
{
    private static StageEnvelopeInfo EnvelopeWithReply(string platform, string text, string? channelId)
    {
        var replyJson = channelId is null
            ? $"{{\"text\":\"{text}\"}}"
            : $"{{\"text\":\"{text}\",\"channel_id\":\"{channelId}\"}}";
        return new StageEnvelopeInfo(
            "tenant-1", null, "waddles.integrations.superpenguin.steal", "action",
            new PlatformEventInfo(platform, "chat.message", "user-1", replyJson, "2026-09-28T00:00:00.000Z"),
            "2026-09-28T00:00:00.000Z", null, null);
    }

    [Theory]
    [InlineData("twitch")]
    [InlineData("discord")]
    public void relays_to_the_events_own_origin_platform_never_a_hardcoded_one(string platform)
    {
        var host = new FakeWaddleHost();

        var result = new StealDispatch().Run(EnvelopeWithReply(platform, "alice rolls...", "42"), "{}", host);

        Assert.True(result.Ok);
        Assert.Equal(platform, result.Detail);
        Assert.Equal(platform, Assert.Single(host.RelayFake.Pushes).Provider);
    }

    [Fact]
    public void throws_a_fatal_error_when_channel_id_is_missing()
    {
        var ex = Assert.Throws<WaddleTransportException>(
            () => new StealDispatch().Run(EnvelopeWithReply("twitch", "text", null), "{}", new FakeWaddleHost()));

        Assert.Equal("MISSING_CHANNEL", ex.Error.Code);
        Assert.False(ex.Error.Retryable);
    }

    [Theory]
    [InlineData(RelayErrorKind.Backend)]
    [InlineData(RelayErrorKind.Denied)]
    public void a_relay_push_failure_is_a_retryable_transport_error(RelayErrorKind kind)
    {
        // Fail-loud: a relay host failure must surface as RELAY_PUSH_FAILED (retryable), never a "success".
        var host = new FakeWaddleHost();
        host.RelayFake.ThrowOnPush = new WaddleRelayException(kind, "relay backend unavailable");

        var ex = Assert.Throws<WaddleTransportException>(
            () => new StealDispatch().Run(EnvelopeWithReply("discord", "alice rolls...", "42"), "{}", host));

        Assert.Equal("RELAY_PUSH_FAILED", ex.Error.Code);
        Assert.True(ex.Error.Retryable);
    }

    [Fact]
    public void the_relayed_message_carries_only_the_channel_and_reply_text()
    {
        // PII-free wire contract: the actor id on the inbound event never rides the relay message.
        var host = new FakeWaddleHost();

        new StealDispatch().Run(EnvelopeWithReply("twitch", "alice rolls...", "42"), "{}", host);

        var json = host.RelayFake.Pushes[0].MessageJson;
        Assert.Contains("\"alice rolls...\"", json);
        Assert.Contains("42", json);
        Assert.DoesNotContain("user-1", json);
    }

    [Fact]
    public void throws_a_fatal_error_for_a_non_reply_payload()
    {
        // A JSON array fails to deserialize as `ChatReplyPayload` (an empty object would parse with null fields
        // and hit the MISSING_CHANNEL path instead).
        var envelope = new StageEnvelopeInfo(
            "tenant-1", null, "waddles.integrations.superpenguin.steal", "action",
            new PlatformEventInfo("twitch", "chat.message", "user-1", "[]", "2026-09-28T00:00:00.000Z"),
            "2026-09-28T00:00:00.000Z", null, null);

        var ex = Assert.Throws<WaddleTransportException>(() => new StealDispatch().Run(envelope, "{}", new FakeWaddleHost()));
        Assert.Equal("BAD_PAYLOAD", ex.Error.Code);
    }
}

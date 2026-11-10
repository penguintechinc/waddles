using WaddleSdk.Relay;
using WaddleSdk.Tests;
using WaddleSdk.Types;
using Xunit;

namespace WaddleBundleSuperpenguinFishing.Tests;

public class FishingDispatchTests
{
    private static StageEnvelopeInfo EnvelopeWithReply(string platform, string text, string? channelId, string payload = "")
    {
        var replyJson = payload.Length > 0 ? payload
            : channelId is null ? $"{{\"text\":\"{text}\"}}" : $"{{\"text\":\"{text}\",\"channel_id\":\"{channelId}\"}}";
        return new StageEnvelopeInfo(
            "tenant-1", null, "waddles.integrations.superpenguin.fishing", "action",
            new PlatformEventInfo(platform, "chat.message", "user-1", replyJson, "2026-11-07T00:00:00.000Z"),
            "2026-11-07T00:00:00.000Z", null, null);
    }

    [Theory]
    [InlineData("twitch")]
    [InlineData("discord")]
    public void relays_to_the_events_own_origin_platform_never_a_hardcoded_one(string platform)
    {
        var host = new FakeWaddleHost();

        var result = new FishingDispatch().Run(EnvelopeWithReply(platform, "caught a fish", "42"), "{}", host);

        Assert.True(result.Ok);
        Assert.Equal(platform, result.Detail);
        Assert.Equal(platform, Assert.Single(host.RelayFake.Pushes).Provider);
    }

    [Fact]
    public void throws_a_fatal_error_when_channel_id_is_missing()
    {
        var ex = Assert.Throws<WaddleTransportException>(
            () => new FishingDispatch().Run(EnvelopeWithReply("twitch", "text", null), "{}", new FakeWaddleHost()));

        Assert.Equal("MISSING_CHANNEL", ex.Error.Code);
        Assert.False(ex.Error.Retryable);
    }

    [Fact]
    public void throws_a_fatal_error_when_the_payload_is_not_a_reply()
    {
        var ex = Assert.Throws<WaddleTransportException>(
            () => new FishingDispatch().Run(EnvelopeWithReply("twitch", "", null, "null"), "{}", new FakeWaddleHost()));

        Assert.Equal("BAD_PAYLOAD", ex.Error.Code);
    }

    [Theory]
    [InlineData(RelayErrorKind.Backend)]
    [InlineData(RelayErrorKind.Denied)]
    public void a_relay_push_failure_is_a_retryable_transport_error(RelayErrorKind kind)
    {
        var host = new FakeWaddleHost();
        host.RelayFake.ThrowOnPush = new WaddleRelayException(kind, "relay backend unavailable");

        var ex = Assert.Throws<WaddleTransportException>(
            () => new FishingDispatch().Run(EnvelopeWithReply("discord", "caught", "42"), "{}", host));

        Assert.Equal("RELAY_PUSH_FAILED", ex.Error.Code);
        Assert.True(ex.Error.Retryable);
    }
}

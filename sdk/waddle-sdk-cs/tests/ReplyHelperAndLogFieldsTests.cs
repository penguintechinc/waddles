using System.Text.Json;
using WaddleSdk.Log;
using WaddleSdk.Relay;
using Xunit;

namespace WaddleSdk.Tests;

public class ReplyHelperTests
{
    [Fact]
    public void send_reply_pushes_the_channel_and_text_to_the_given_provider()
    {
        var relay = new FakeRelayClient();
        ReplyHelper.SendReply(relay, "twitch", "12345", "pong");

        Assert.Single(relay.Pushes);
        Assert.Equal("twitch", relay.Pushes[0].Provider);
        using var doc = JsonDocument.Parse(relay.Pushes[0].MessageJson);
        Assert.Equal("12345", doc.RootElement.GetProperty("channel").GetString());
        Assert.Equal("pong", doc.RootElement.GetProperty("text").GetString());
    }

    [Fact]
    public void send_reply_propagates_relay_failures()
    {
        var relay = new FakeRelayClient { ThrowOnPush = new WaddleRelayException(RelayErrorKind.Backend, "boom") };
        Assert.Throws<WaddleRelayException>(() => ReplyHelper.SendReply(relay, "discord", "1", "hi"));
    }
}

public class LogFieldsTests
{
    [Fact]
    public void build_produces_a_json_object_with_every_field_type()
    {
        var json = new LogFields()
            .With("str", "value")
            .With("num", 42L)
            .With("flt", 1.5)
            .With("flag", true)
            .Build();

        using var doc = JsonDocument.Parse(json);
        Assert.Equal("value", doc.RootElement.GetProperty("str").GetString());
        Assert.Equal(42, doc.RootElement.GetProperty("num").GetInt64());
        Assert.Equal(1.5, doc.RootElement.GetProperty("flt").GetDouble());
        Assert.True(doc.RootElement.GetProperty("flag").GetBoolean());
    }

    [Fact]
    public void build_is_idempotent()
    {
        var fields = new LogFields().With("a", "b");
        var first = fields.Build();
        var second = fields.Build();
        Assert.Equal(first, second);
    }
}

public class LogClientExtensionsTests
{
    [Fact]
    public void each_level_helper_writes_the_matching_level()
    {
        var log = new FakeLogClient();
        log.Error("e");
        log.Warn("w");
        log.Info("i");
        log.Debug("d");

        Assert.Equal(
            [WaddleLogLevel.Error, WaddleLogLevel.Warn, WaddleLogLevel.Info, WaddleLogLevel.Debug],
            log.Writes.Select(w => w.Level));
    }
}

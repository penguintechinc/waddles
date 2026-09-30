using WaddleSdk.Chat;
using WaddleSdk.Db;
using WaddleSdk.Json;
using WaddleSdk.Log;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Types;
using Xunit;

namespace WaddleSdk.Tests;

/// <summary>Minimal fixture bundle exercising <see cref="WaddleProcessStage"/> exactly
/// the way a real bundle's business-logic class would -- parses a `!ping` command via
/// <see cref="ChatCommand"/> and rewrites the payload via <see cref="PlatformEventInfo.WithPayload{T}"/>.</summary>
file sealed class FixtureProcessStage : WaddleProcessStage
{
    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHost host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.Is("ping"))
        {
            return null;
        }

        host.Log.Info("matched ping");
        return PlatformEventInfo.WithPayload(@event, new ChatReplyPayload("pong", command.ChannelId), WaddleSdkJsonContext.Default.ChatReplyPayload);
    }
}

file sealed class FixtureActionStage : WaddleActionStage
{
    protected override TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHost host)
    {
        var reply = envelope.Event.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)
            ?? throw new WaddleTransportException(TransportErrorInfo.Fatal("BAD_PAYLOAD", "missing reply payload"));
        if (reply.ChannelId is null)
        {
            throw new WaddleTransportException(TransportErrorInfo.Fatal("MISSING_CHANNEL", "no channel_id"));
        }

        ReplyHelper.SendReply(host.Relay, envelope.Event.Platform, reply.ChannelId, reply.Text);
        return TransportResultInfo.Success(detail: envelope.Event.Platform);
    }
}

public class WaddleProcessStageTests
{
    [Fact]
    public void run_delegates_to_transform_and_returns_a_reply()
    {
        var host = new FakeWaddleHost();
        var evt = new PlatformEventInfo("twitch", "chat.message", "user-1", """{"text":"!ping","channel_id":"1"}""", "2026-09-28T00:00:00.000Z");

        var result = new FixtureProcessStage().Run(evt, host);

        Assert.NotNull(result);
        Assert.Single(host.LogFake.Writes);
    }

    [Fact]
    public void run_returns_null_for_a_non_matching_command()
    {
        var host = new FakeWaddleHost();
        var evt = new PlatformEventInfo("twitch", "chat.message", "user-1", """{"text":"!other","channel_id":"1"}""", "2026-09-28T00:00:00.000Z");

        Assert.Null(new FixtureProcessStage().Run(evt, host));
    }
}

public class WaddleActionStageTests
{
    private static StageEnvelopeInfo EnvelopeFor(string platform, string? channelId) => new(
        "tenant-1", null, "waddles.test.app", "action",
        new PlatformEventInfo(platform, "chat.message", "user-1",
            System.Text.Json.JsonSerializer.Serialize(new ChatReplyPayload("pong", channelId), WaddleSdkJsonContext.Default.ChatReplyPayload),
            "2026-09-28T00:00:00.000Z"),
        "2026-09-28T00:00:00.000Z", null, null);

    [Fact]
    public void run_relays_to_the_events_own_platform()
    {
        var host = new FakeWaddleHost();
        var result = new FixtureActionStage().Run(EnvelopeFor("discord", "42"), "{}", host);

        Assert.True(result.Ok);
        Assert.Equal("discord", result.Detail);
        Assert.Single(host.RelayFake.Pushes);
        Assert.Equal("discord", host.RelayFake.Pushes[0].Provider);
    }

    [Fact]
    public void run_throws_a_fatal_transport_exception_when_channel_id_is_missing()
    {
        var host = new FakeWaddleHost();
        var ex = Assert.Throws<WaddleTransportException>(() => new FixtureActionStage().Run(EnvelopeFor("twitch", null), "{}", host));
        Assert.Equal("MISSING_CHANNEL", ex.Error.Code);
        Assert.False(ex.Error.Retryable);
    }
}

public class FakeDbClientTests
{
    [Fact]
    public void execute_returns_the_configured_result()
    {
        var db = new FakeDbClient { NextResult = new DbRows(["id"], [[DbValue.Of(1L)]], 1) };
        var rows = db.Execute("SELECT 1", []);
        Assert.Equal(1ul, rows.RowsAffected);
    }

    [Fact]
    public void execute_defaults_to_empty_result()
    {
        var db = new FakeDbClient();
        var rows = db.Execute("SELECT 1", []);
        Assert.Empty(rows.Columns);
        Assert.Empty(rows.Rows);
    }
}

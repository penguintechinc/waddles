using WaddleSdk.Json;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace SuperPenguinShared;

/// <summary>
/// `action-stage.dispatch` half shared by every SuperPenguin port: reads the reply the process stage produced off the
/// envelope's own event payload and relays it to the event's own origin platform (never a hardcoded provider).
/// </summary>
public sealed class ReplyDispatch : WaddleActionStage
{
    protected override TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHost host)
    {
        var reply = envelope.Event.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)
            ?? throw new WaddleTransportException(TransportErrorInfo.Fatal(
                "BAD_PAYLOAD", "envelope.event.payload_json is not a reply payload"));

        if (reply.ChannelId is null)
        {
            throw new WaddleTransportException(TransportErrorInfo.Fatal(
                "MISSING_CHANNEL", "reply requires a channel_id from the inbound chat.message"));
        }

        var provider = envelope.Event.Platform;
        try
        {
            ReplyHelper.SendReply(host.Relay, provider, reply.ChannelId, reply.Text);
        }
        catch (WaddleRelayException ex)
        {
            throw new WaddleTransportException(TransportErrorInfo.RetryableError("RELAY_PUSH_FAILED", ex.Message));
        }

        return TransportResultInfo.Success(detail: provider);
    }
}

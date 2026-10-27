using WaddleSdk.Json;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinRoll;

/// <summary>
/// `action-stage.dispatch` half of the roll bundle: reads the reply
/// <see cref="RollLogic"/> produced back off the envelope's own event payload
/// and relays it to the event's own origin platform -- never a hardcoded
/// provider, same non-negotiable contract every Tier-1 SDK sibling's ping/pong
/// bundle enforces (`bundles/rust/ping`'s own regression test).
/// </summary>
public sealed class RollDispatch : WaddleActionStage
{
    protected override TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHost host)
    {
        var reply = envelope.Event.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)
            ?? throw new WaddleTransportException(TransportErrorInfo.Fatal(
                "BAD_PAYLOAD", "envelope.event.payload_json is not a roll reply payload"));

        if (reply.ChannelId is null)
        {
            throw new WaddleTransportException(TransportErrorInfo.Fatal(
                "MISSING_CHANNEL", "roll reply requires a channel_id from the inbound chat.message"));
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

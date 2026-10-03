using WaddleSdk.Json;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleCsping;

/// <summary>
/// `action-stage.dispatch` half of `!csping`, built on `waddle-sdk-cs`
/// (<see cref="WaddleActionStage"/>) instead of this bundle's original
/// hand-rolled wit-bindgen `Relay.Push` call and manual `Relay.Error` tag
/// switch. Reads the `pong (c#)` reply <see cref="CspingLogic"/> produced back
/// off the envelope's own event payload and relays it, via
/// <see cref="ReplyHelper"/>, to the event's own origin platform -- never a
/// hardcoded provider, same non-negotiable contract
/// `bundles/rust/ping`'s own regression test enforces: a Discord-origin
/// `!csping` relays to Discord, a Twitch-origin one to Twitch.
/// </summary>
public sealed class CspingDispatch : WaddleActionStage
{
    protected override TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHost host)
    {
        var reply = envelope.Event.Payload(WaddleSdkJsonContext.Default.ChatReplyPayload)
            ?? throw new WaddleTransportException(TransportErrorInfo.Fatal(
                "BAD_PAYLOAD", "envelope.event.payload_json is not a csping reply payload"));

        if (reply.ChannelId is null)
        {
            throw new WaddleTransportException(TransportErrorInfo.Fatal(
                "MISSING_CHANNEL", "pong (c#) reply requires a channel_id from the inbound chat.message"));
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

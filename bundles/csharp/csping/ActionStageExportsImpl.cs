// `action-stage.dispatch` half of the C#-SDK-toolchain spike -- see
// `ProcessStageExportsImpl.cs` for the naming-contract explanation (same
// applies here: `ActionStageExportsImpl` implementing `IActionStageExports`
// is the exact name/namespace `wit-bindgen`'s generated
// `ActionStageExportsInterop` requires, discovered via the build's own
// `CS0103` error rather than guessed).
//
// Relays a `pong (c#)` reply back over the WIT `relay` host import
// (granted only to action-stage bundles, `wit/waddle-bundle/stage.wit`
// `interface relay`) to the event's own origin platform
// (`envelope.event.platform` -- never a fixed provider), mirroring
// `bundles/rust/ping`'s `build_relay`/`dispatch` and
// `bundles/python/pyping`'s `dispatch`, including that same file's
// regression contract: a Discord-origin `!csping`'s pong relays to
// Discord, a Twitch-origin one to Twitch, never a hardcoded platform.
namespace StageWorld.wit.Exports.waddle.bundle.v1_0_0;

using System.Text.Json;
using System.Text.Json.Nodes;
using Types = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.ITypesImports;
using Relay = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IRelayImports;

/// <summary>
/// Implements `action-stage.dispatch`: reads the `pong (c#)` reply
/// `process-stage.transform` produced back off `envelope.event.payload-json`
/// and relays it to `envelope.event.platform` via <see cref="Relay.Push"/>.
/// </summary>
public class ActionStageExportsImpl : IActionStageExports
{
    public static Types.TransportResult Dispatch(Types.StageEnvelope envelope, string config)
    {
        JsonObject payload;
        try
        {
            payload = JsonNode.Parse(envelope.@event.payloadJson) as JsonObject
                ?? throw new JsonException("payload-json is not a JSON object");
        }
        catch (JsonException ex)
        {
            // Mirrors `bundles/rust/ping::build_relay`'s `BAD_PAYLOAD`
            // fatal (non-retryable) error for a reply payload that isn't
            // the shape `transform` produces.
            throw new global::StageWorld.WitException<Types.TransportError>(
                new Types.TransportError(false, "BAD_PAYLOAD", ex.Message, null), 0);
        }

        if (payload["channel_id"] is not JsonValue channelValue || !channelValue.TryGetValue(out string? channel))
        {
            // Mirrors `bundles/rust/ping::build_relay`'s `MISSING_CHANNEL`
            // fatal (non-retryable) error.
            throw new global::StageWorld.WitException<Types.TransportError>(
                new Types.TransportError(
                    false,
                    "MISSING_CHANNEL",
                    "pong (c#) reply requires a channel_id from the inbound chat.message",
                    null),
                0);
        }

        var text = payload["text"] is JsonValue textValue && textValue.TryGetValue(out string? t) ? t : "pong (c#)";

        var message = new JsonObject
        {
            ["channel"] = channel,
            ["text"] = text,
        };

        // Always the inbound event's own platform -- never a hardcoded
        // provider constant (the exact regression `bundles/rust/ping`'s
        // own test suite guards:
        // `dispatch_relays_to_the_events_own_origin_platform_not_a_hardcoded_one`).
        var provider = envelope.@event.platform;
        try
        {
            Relay.Push(provider, message.ToJsonString());
        }
        catch (global::StageWorld.WitException<Relay.Error> ex)
        {
            var detail = ex.TypedValue.Tag switch
            {
                Relay.Error.Tags.Denied => ex.TypedValue.AsDenied,
                _ => ex.TypedValue.AsBackend,
            };
            throw new global::StageWorld.WitException<Types.TransportError>(
                new Types.TransportError(true, "RELAY_PUSH_FAILED", detail, null), 0);
        }

        return new Types.TransportResult(true, null, provider, null);
    }
}

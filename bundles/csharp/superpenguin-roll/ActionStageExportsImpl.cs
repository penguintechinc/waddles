// See `ProcessStageExportsImpl.cs` for the naming-contract explanation --
// `ActionStageExportsImpl` implementing `IActionStageExports` in this exact
// namespace is wit-bindgen's own requirement, not a choice made here. Thin:
// converts to/from the wit-bindgen-generated `Types.StageEnvelope`/
// `Types.TransportResult`/`Types.TransportError` and delegates to
// `RollDispatch` (`waddle-sdk-cs`'s `WaddleActionStage` base class), catching
// `WaddleTransportException` and re-throwing it as the generated
// `WitException<Types.TransportError>`.
namespace StageWorld.wit.Exports.waddle.bundle.v1_0_0;

using WaddleBundleSuperpenguinRoll;
using WaddleSdk.Types;
using Types = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.ITypesImports;

public class ActionStageExportsImpl : IActionStageExports
{
    private static readonly RollDispatch Logic = new();
    private static readonly WaddleHostAdapter Host = new();

    public static Types.TransportResult Dispatch(Types.StageEnvelope envelope, string config)
    {
        var sdkEnvelope = new StageEnvelopeInfo(
            envelope.tenant,
            envelope.community,
            envelope.appId,
            envelope.stage,
            new PlatformEventInfo(envelope.@event.platform, envelope.@event.eventType, envelope.@event.actor, envelope.@event.payloadJson, envelope.@event.occurredAt),
            envelope.ts,
            envelope.targetAppId,
            envelope.traceContext);

        TransportResultInfo result;
        try
        {
            result = Logic.Run(sdkEnvelope, config, Host);
        }
        catch (WaddleTransportException ex)
        {
            throw new global::StageWorld.WitException<Types.TransportError>(
                new Types.TransportError(ex.Error.Retryable, ex.Error.Code, ex.Error.Message, ex.Error.RetryAfterMs), 0);
        }

        return new Types.TransportResult(result.Ok, result.Status, result.Detail, result.ProviderMessageId);
    }
}

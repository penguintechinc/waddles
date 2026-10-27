// `wit-bindgen c-sharp` requires this EXACT type name
// (`ProcessStageExportsImpl`) implementing `IProcessStageExports` in this
// EXACT namespace -- see `bundles/csharp/csping/ProcessStageExportsImpl.cs`'s
// header comment for the full explanation (discovered empirically there, not
// guessed). Deliberately thin: converts the wit-bindgen-generated
// `Types.PlatformEvent` to/from `WaddleSdk.Types.PlatformEventInfo` and
// delegates all real logic to `RollLogic` (`waddle-sdk-cs`'s
// `WaddleProcessStage` base class) -- see `sdk/waddle-sdk-cs/README.md`
// "Writing a bundle".
namespace StageWorld.wit.Exports.waddle.bundle.v1_0_0;

using WaddleBundleSuperpenguinRoll;
using WaddleSdk.Types;
using Types = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.ITypesImports;

public class ProcessStageExportsImpl : IProcessStageExports
{
    private static readonly RollLogic Logic = new();
    private static readonly WaddleHostAdapter Host = new();

    public static Types.PlatformEvent? Transform(Types.PlatformEvent @event)
    {
        var sdkEvent = new PlatformEventInfo(@event.platform, @event.eventType, @event.actor, @event.payloadJson, @event.occurredAt);

        var result = Logic.Run(sdkEvent, Host);
        if (result is null)
        {
            return null;
        }

        return new Types.PlatformEvent(result.Platform, result.EventType, result.Actor, result.PayloadJson, result.OccurredAt);
    }
}

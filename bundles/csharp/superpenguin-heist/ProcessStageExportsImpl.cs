// `wit-bindgen c-sharp` requires this EXACT type name (`ProcessStageExportsImpl`) implementing `IProcessStageExports`
// in this EXACT namespace (`<WorldName>World.wit.Exports.waddle.bundle.v1_0_0`) -- discovered empirically in
// `bundles/csharp/csping`, not guessed. The world is `stage-next-tools`, so the root namespace is
// `StageNextToolsWorld`. Deliberately thin: converts the generated `Types.PlatformEvent` to/from
// `WaddleSdk.Types.PlatformEventInfo` and delegates all real logic to `HeistLogic` -- see `docs/sdk-cs.md`.
namespace StageNextToolsWorld.wit.Exports.waddle.bundle.v1_0_0;

using WaddleBundleSuperpenguinHeist;
using WaddleSdk.Types;
using Types = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.ITypesImports;

public class ProcessStageExportsImpl : IProcessStageExports
{
    private static readonly HeistLogic Logic = new();
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

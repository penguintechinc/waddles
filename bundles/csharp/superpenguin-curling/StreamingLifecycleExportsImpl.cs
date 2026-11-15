// The `stage-next`/`stage-next-tools` worlds export `streaming-lifecycle`, so wit-bindgen requires this EXACT type name
// (`StreamingLifecycleExportsImpl`) in this EXACT namespace even for a bundle that does not react to stream events.
// Per the WIT contract ("a bundle not implementing streaming behavior returns `unsupported-stage` from every hook")
// every hook throws the generated `WitException<UnsupportedStage>`, which the interop layer lowers to `err(unsupported-stage)`.
// Copy verbatim into any C# bundle on these worlds that has no stream hooks.
namespace StageNextToolsWorld.wit.Exports.waddle.bundle.v1_0_0;

using Types = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.ITypesImports;

public class StreamingLifecycleExportsImpl : IStreamingLifecycleExports
{
    public static Types.PlatformEvent? OnStart(IStreamingLifecycleExports.StreamInfo info) => Unsupported("on-start");

    public static Types.PlatformEvent? OnStop(IStreamingLifecycleExports.StreamInfo info) => Unsupported("on-stop");

    public static Types.PlatformEvent? OnSegment(IStreamingLifecycleExports.SegmentInfo segment) => Unsupported("on-segment");

    public static Types.PlatformEvent? OnRecordingReady(IStreamingLifecycleExports.RecordingInfo recording) => Unsupported("on-recording-ready");

    private static Types.PlatformEvent? Unsupported(string stage) =>
        throw new global::StageNextToolsWorld.WitException<Types.UnsupportedStage>(new Types.UnsupportedStage(stage), 0);
}

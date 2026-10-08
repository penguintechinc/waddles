using WaddleSdk.Types;

namespace WaddleSdk.Stage;

/// <summary>
/// Base class for a bundle's `process-stage.transform` logic
/// (`wit/waddle-bundle/stage.wit` `interface process-stage`). A bundle derives
/// from this, implements <see cref="Transform"/> against SDK POCOs and
/// capability interfaces only, and its own thin per-project
/// `ProcessStageExportsImpl` (required by wit-bindgen's C# naming contract --
/// see the SDK README) converts the wit-bindgen-generated `Types.PlatformEvent`
/// to/from <see cref="PlatformEventInfo"/> and delegates to <see cref="Run"/>.
/// </summary>
public abstract class WaddleProcessStage
{
    /// <summary>
    /// Returns the rewritten reply event, or null for "no reply" -- the event is
    /// dropped, exactly like every Tier-1 SDK sibling's `transform`. Must never throw
    /// for an event this bundle simply does not recognize; only a genuine host-capability
    /// failure (a <see cref="Kv.WaddleKvException"/>, etc., surfaced by <paramref name="host"/>)
    /// should propagate.
    /// </summary>
    protected abstract PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHost host);

    /// <summary>Entry point the bundle's thin `ProcessStageExportsImpl` calls.</summary>
    public PlatformEventInfo? Run(PlatformEventInfo @event, IWaddleHost host) => Transform(@event, host);
}

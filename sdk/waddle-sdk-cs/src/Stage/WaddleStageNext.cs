using WaddleSdk.Types;

namespace WaddleSdk.Stage;

/// <summary>
/// `process-stage.transform` base for bundles on the newer worlds: identical contract to <see cref="WaddleProcessStage"/> but the
/// host is an <see cref="IWaddleHostNext"/>. Economy, identity and tool calls belong here: the host wires them into the process
/// stage only (the action stage answers them `not_implemented`).
/// </summary>
public abstract class WaddleProcessStageNext
{
    /// <summary>Returns the reply event, or null for "no reply" (event dropped).</summary>
    protected abstract PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host);

    /// <summary>Entry point the bundle's thin `ProcessStageExportsImpl` calls.</summary>
    public PlatformEventInfo? Run(PlatformEventInfo @event, IWaddleHostNext host) => Transform(@event, host);
}

/// <summary>
/// `action-stage.dispatch` base for bundles on the newer worlds. Keep action-stage work to relay/overlay pushes: it cannot
/// call economy/identity/tool.
/// </summary>
public abstract class WaddleActionStageNext
{
    /// <summary>Performs the dispatch; throw <see cref="WaddleTransportException"/> for a fatal or retryable failure.</summary>
    protected abstract TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHostNext host);

    /// <summary>Entry point the bundle's thin `ActionStageExportsImpl` calls.</summary>
    public TransportResultInfo Run(StageEnvelopeInfo envelope, string configJson, IWaddleHostNext host) =>
        Dispatch(envelope, configJson, host);
}

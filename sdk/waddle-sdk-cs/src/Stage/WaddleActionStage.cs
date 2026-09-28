using WaddleSdk.Types;

namespace WaddleSdk.Stage;

/// <summary>
/// Base class for a bundle's `action-stage.dispatch` logic
/// (`wit/waddle-bundle/stage.wit` `interface action-stage`). A bundle derives
/// from this, implements <see cref="Dispatch"/> against SDK POCOs and
/// capability interfaces only, and its own thin per-project
/// `ActionStageExportsImpl` (required by wit-bindgen's C# naming contract --
/// see the SDK README) converts to/from the wit-bindgen-generated
/// `Types.StageEnvelope`/`Types.TransportResult`/`Types.TransportError` and
/// delegates to <see cref="Run"/>, catching <see cref="WaddleTransportException"/>
/// and re-throwing it as the generated `WitException&lt;Types.TransportError&gt;`.
/// </summary>
public abstract class WaddleActionStage
{
    /// <summary>
    /// Performs the dispatch (typically an <see cref="IWaddleHost.Relay"/> push) and
    /// returns the transport result. Throw <see cref="WaddleTransportException"/> for a
    /// fatal or retryable failure -- never let an unrelated exception escape uncaught.
    /// </summary>
    protected abstract TransportResultInfo Dispatch(StageEnvelopeInfo envelope, string configJson, IWaddleHost host);

    /// <summary>Entry point the bundle's thin `ActionStageExportsImpl` calls.</summary>
    public TransportResultInfo Run(StageEnvelopeInfo envelope, string configJson, IWaddleHost host) =>
        Dispatch(envelope, configJson, host);
}

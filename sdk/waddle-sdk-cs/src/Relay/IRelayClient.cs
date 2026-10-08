namespace WaddleSdk.Relay;

/// <summary>
/// Typed wrapper over `waddle:bundle/relay` (`wit/waddle-bundle/stage.wit`) --
/// pushes onto a provider-scoped outbound relay queue owned by svc-ingest.
/// Granted only to action-stage bundles. A bundle's per-project adapter
/// implements this by delegating to the generated `IRelayImports.Push`
/// static method -- see the SDK README.
/// </summary>
public interface IRelayClient
{
    void Push(string provider, string messageJson);
}

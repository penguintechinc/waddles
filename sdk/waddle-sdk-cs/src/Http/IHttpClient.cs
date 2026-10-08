namespace WaddleSdk.Http;

/// <summary>
/// Typed wrapper over `waddle:bundle/http` (`wit/waddle-bundle/stage.wit`) --
/// guarded outbound HTTP, granted only when the manifest's `egress` is
/// non-empty. A bundle's per-project adapter implements this by delegating
/// to the generated `IHttpImports.Send` static method -- see the SDK README.
/// </summary>
public interface IHttpClient
{
    HttpResponseInfo Send(HttpRequestInfo request);
}

using WaddleSdk.Types;

namespace WaddleSdk.Overlay;

/// <summary>Mirrors `waddle:bundle/overlay`'s `surface` enum (kept in lockstep with `core/overlay_schema::Surface`).</summary>
public enum OverlaySurface
{
    FullScreen,
    Media,
    Crawler,
    Music,
    AlertBox,
    Chat,
    Goals,
    Ticker,
    Image,
    Caption,
}

/// <summary>
/// One widget a bundle offers the overlay designer's palette. <see cref="DescriptorJson"/> is a canonical
/// JSON object (default size/anchor + declared data-binding fields), host-validated -- never raw HTML/CSS/JS.
/// </summary>
public sealed record OverlayWidget(string WidgetId, string DisplayName, OverlaySurface Surface, string DescriptorJson);

/// <summary>
/// Typed wrapper over `waddle:bundle/overlay` (`stage-next`/`stage-v2` worlds). Both calls return a
/// transport result; a refusal surfaces as <see cref="WaddleTransportException"/> carrying the host's
/// `transport-error` (retryable flag + code), so an action-stage dispatch can let it propagate unchanged.
/// Needs `overlay.media`.
/// </summary>
public interface IOverlayClient
{
    /// <summary>Declares (idempotently updates) one widget for the designer's palette; pushes no live content.</summary>
    TransportResultInfo RegisterWidget(OverlayWidget widget);

    /// <summary>Pushes one content update (<paramref name="payloadJson"/> is the surface's documented JSON object) to every live overlay client.</summary>
    TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson);
}

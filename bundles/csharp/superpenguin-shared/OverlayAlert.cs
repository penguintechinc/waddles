using System.Text;
using System.Text.Json;
using WaddleSdk.Overlay;
using WaddleSdk.Types;

namespace SuperPenguinShared;

/// <summary>
/// Emits one app-defined overlay message over the host's existing `overlay.push` import on the `alert-box` surface. The surface
/// renderer (core/svc_presentation, owned elsewhere) keeps `alert_type`, `message` and an open JSON `amount`, sanitises them and
/// fans them out on the community's live channel, where a browser-source component (each bundle's `overlay/` directory)
/// filters on the `superpenguin.` alert-type prefix. No core change is needed: the payload is the documented
/// `overlay_schema::AlertPayload`. A refused push throws <see cref="WaddleTransportException"/> and is never swallowed.
/// </summary>
public static class OverlayAlert
{
    /// <summary>Alert types are namespaced so unrelated alert-box consumers ignore them.</summary>
    public const string TypePrefix = "superpenguin.";

    /// <summary>Largest `amount` JSON the helper will send; the host enforces its own bound too.</summary>
    public const int MaxAmountBytes = 8 * 1024;

    /// <summary>Declares the bundle's widget in the overlay designer palette (idempotent).</summary>
    public static void Register(IOverlayClient overlay, string widgetId, string displayName) =>
        overlay.RegisterWidget(new OverlayWidget(widgetId, displayName, OverlaySurface.AlertBox, "{\"anchor\":\"center\",\"width\":1920,\"height\":1080}"));

    /// <summary>Pushes `{alert:{alert_type, amount, message}}`; <paramref name="amountJson"/> must already be a JSON value.</summary>
    public static TransportResultInfo Push(IOverlayClient overlay, string widgetId, string alertSuffix, string amountJson, string? message)
    {
        if (string.IsNullOrWhiteSpace(alertSuffix))
        {
            throw new ArgumentException("alert suffix is required", nameof(alertSuffix));
        }

        if (Encoding.UTF8.GetByteCount(amountJson) > MaxAmountBytes)
        {
            throw new InvalidOperationException($"overlay amount exceeds {MaxAmountBytes} bytes");
        }

        using var stream = new MemoryStream();
        using (var w = new Utf8JsonWriter(stream))
        {
            w.WriteStartObject();
            w.WriteStartObject("alert");
            w.WriteString("alert_type", TypePrefix + alertSuffix);
            w.WritePropertyName("amount");
            using (var amount = JsonDocument.Parse(amountJson))
            {
                amount.RootElement.WriteTo(w);
            }

            if (message is not null)
            {
                w.WriteString("message", message);
            }

            w.WriteEndObject();
            w.WriteEndObject();
        }

        return overlay.Push(widgetId, OverlaySurface.AlertBox, Encoding.UTF8.GetString(stream.ToArray()));
    }
}

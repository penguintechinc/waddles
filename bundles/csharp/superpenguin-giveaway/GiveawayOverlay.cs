using System.Text;
using System.Text.Json;
using WaddleSdk.Overlay;

namespace WaddleBundleSuperpenguinGiveaway;

/// <summary>
/// Pushes giveaway/raffle overlay messages on the `alert-box` surface (`alert_type` = `superpenguin.giveaway.*` or
/// `superpenguin.raffle.*`). Unlike the shared helper this also fills the alert's `user` UUID so the presentation service resolves
/// the winner's display name server-side; no raw name ever passes through the bundle. A refused push throws and is never swallowed.
/// </summary>
public static class GiveawayOverlay
{
    /// <summary>Alert-type prefix of giveaway messages.</summary>
    public const string GiveawayPrefix = "superpenguin.giveaway.";

    /// <summary>Alert-type prefix of raffle messages.</summary>
    public const string RafflePrefix = "superpenguin.raffle.";

    /// <summary>Declares the widget in the overlay designer palette (one host call; used by `!tgw overlay register`).</summary>
    public static void Register(IOverlayClient overlay, string widgetId) =>
        overlay.RegisterWidget(new OverlayWidget(widgetId, "Giveaway and raffle", OverlaySurface.AlertBox, "{\"anchor\":\"center\",\"width\":1920,\"height\":1080}"));

    /// <summary>Pushes one alert; <paramref name="amount"/> writes the open `amount` object, <paramref name="user"/> is an optional user uuid.</summary>
    public static void Push(IOverlayClient overlay, string widgetId, string alertType, Action<Utf8JsonWriter> amount, string message, string? user)
    {
        using var stream = new MemoryStream();
        using (var w = new Utf8JsonWriter(stream))
        {
            w.WriteStartObject();
            w.WriteStartObject("alert");
            w.WriteString("alert_type", alertType);
            if (user is not null)
            {
                w.WriteString("user", user);
            }

            w.WritePropertyName("amount");
            w.WriteStartObject();
            amount(w);
            w.WriteEndObject();
            w.WriteString("message", message);
            w.WriteEndObject();
            w.WriteEndObject();
        }

        overlay.Push(widgetId, OverlaySurface.AlertBox, Encoding.UTF8.GetString(stream.ToArray()));
    }
}

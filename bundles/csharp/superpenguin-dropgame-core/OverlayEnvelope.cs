using System.Buffers;
using System.Text;
using System.Text.Json;
using WaddleSdk.Log;
using WaddleSdk.Overlay;
using WaddleSdk.Stage;
using WaddleSdk.State;
using WaddleSdk.Types;

namespace SuperPenguin.DropGameCore;

/// <summary>
/// Builds the one structured payload every drop game pushes to the overlay: `{"extra": {...}}`, where `extra` is
/// core/overlay_schema's bundle-widget escape hatch. The envelope fields are fixed here (see `overlay/SCHEMA.md`); the game-specific
/// `data` object (keyframes, outcome, board) is supplied by the game as already-serialized JSON and embedded verbatim, so no
/// reflection JSON is involved.
/// </summary>
public static class OverlayEnvelope
{
    /// <summary>The envelope schema version published as `extra.v`; bump only on an incompatible change to the fixed fields.</summary>
    public const int Version = 1;

    /// <summary>
    /// Builds the push payload. <paramref name="seedHex"/> doubles as the envelope's idempotency id (an overlay client drops a
    /// frame whose `seed` it has already played); <paramref name="actor"/> is the community user uuid, never a name.
    /// <paramref name="dataJson"/> must be one valid JSON value (normally an object).
    /// </summary>
    public static string Build(string game, string kind, string seedHex, string actor, ulong stake, ulong payout, string dataJson)
    {
        var buffer = new ArrayBufferWriter<byte>(512);
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteStartObject("extra");
            w.WriteNumber("v", Version);
            w.WriteString("game", game);
            w.WriteString("kind", kind);
            w.WriteString("seed", seedHex);
            w.WriteString("actor", actor);
            w.WriteNumber("stake", stake);
            w.WriteNumber("payout", payout);
            w.WritePropertyName("data");
            w.WriteRawValue(dataJson);
            w.WriteEndObject();
            w.WriteEndObject();
        }

        return Encoding.UTF8.GetString(buffer.WrittenSpan);
    }

    /// <summary>
    /// The widget descriptor a bundle registers once: default size and anchor, the data-binding field (`extra`), the envelope
    /// version and the asset prefix under which the bundle's vendored overlay files live. Host-validated JSON, never HTML/JS.
    /// </summary>
    public static string Descriptor(string game, int width, int height, string anchor = "center") =>
        $"{{\"size\":{{\"w\":{width},\"h\":{height}}},\"anchor\":\"{anchor}\",\"bindings\":[\"extra\"],\"envelope_version\":{Version},\"asset_prefix\":\"drop-games/{game}/\"}}";
}

/// <summary>
/// Pushes the envelope AFTER settlement and registers the bundle's widget once. A failed push or registration is logged at warn
/// with its transport code and is swallowed on purpose: a broken overlay must never reverse or hide a settled payout. Everything
/// else (kv, economy, identity) still propagates.
/// </summary>
public static class OverlayPublisher
{
    private const uint WidgetMarkerTtlSeconds = 24 * 60 * 60;

    /// <summary>Registers <paramref name="widget"/> if this bundle has not in the last day, then pushes <paramref name="payloadJson"/>; true when the push was accepted.</summary>
    public static bool Publish(IWaddleHostNext host, IStateStore store, string game, OverlayWidget widget, string payloadJson)
    {
        EnsureWidget(host, store, game, widget);
        try
        {
            host.Overlay.Push(widget.WidgetId, widget.Surface, payloadJson);
            return true;
        }
        catch (WaddleTransportException ex)
        {
            host.Log.Warn($"{game}.overlay_push_failed", new LogFields().With("code", ex.Error.Code).With("retryable", ex.Error.Retryable).Build());
            return false;
        }
    }

    /// <summary>
    /// Declares the widget once per day per community (a kv marker records it; the declaration itself is idempotent on the host).
    /// A failed declaration is logged and retried on the next play, because the marker is only written on success.
    /// </summary>
    public static void EnsureWidget(IWaddleHostNext host, IStateStore store, string game, OverlayWidget widget)
    {
        var marker = StateKey.Of("dg", game, "widget");
        if (store.Get(marker) is not null)
        {
            return;
        }

        try
        {
            host.Overlay.RegisterWidget(widget);
        }
        catch (WaddleTransportException ex)
        {
            host.Log.Warn($"{game}.overlay_register_failed", new LogFields().With("code", ex.Error.Code).With("retryable", ex.Error.Retryable).Build());
            return;
        }

        store.Set(marker, [1], WidgetMarkerTtlSeconds);
    }
}

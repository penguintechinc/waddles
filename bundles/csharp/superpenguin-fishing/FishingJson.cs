using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinFishing;

/// <summary>
/// This bundle's own source-generated JSON context (no reflection JSON under NativeAOT): every persisted state record and the
/// overlay push body.
/// </summary>
[JsonSerializable(typeof(PlayerState))]
[JsonSerializable(typeof(CastOutcome))]
[JsonSerializable(typeof(CastResult[]))]
[JsonSerializable(typeof(ShopOutcome))]
[JsonSerializable(typeof(TournamentAggregate))]
[JsonSerializable(typeof(TournamentBoard))]
[JsonSerializable(typeof(LifetimeBoards))]
[JsonSerializable(typeof(OverlayAlert))]
[JsonSerializable(typeof(OverlayWidgetDescriptor))]
public sealed partial class FishingJsonContext : JsonSerializerContext;

/// <summary>The `alert_box` push body (`core/overlay_schema` `OverlayPush`/`AlertPayload` subset): a tokenized user reference, never a name.</summary>
public sealed record OverlayAlert(
    [property: JsonPropertyName("alert")] OverlayAlertBody Alert,
    [property: JsonPropertyName("title")] string Title,
    [property: JsonPropertyName("body")] string Body);

/// <summary>The alert part of <see cref="OverlayAlert"/>.</summary>
public sealed record OverlayAlertBody(
    [property: JsonPropertyName("alert_type")] string AlertType,
    [property: JsonPropertyName("user")] string User,
    [property: JsonPropertyName("amount")] int Amount,
    [property: JsonPropertyName("message")] string Message);

/// <summary>The designer-palette descriptor of the catch widget (host-validated declarative JSON, never HTML/JS).</summary>
public sealed record OverlayWidgetDescriptor(
    [property: JsonPropertyName("width")] int Width,
    [property: JsonPropertyName("height")] int Height,
    [property: JsonPropertyName("anchor")] string Anchor,
    [property: JsonPropertyName("duration_ms")] int DurationMs,
    [property: JsonPropertyName("bindings")] string[] Bindings);

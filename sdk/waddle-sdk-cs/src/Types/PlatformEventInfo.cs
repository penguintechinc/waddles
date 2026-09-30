using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

namespace WaddleSdk.Types;

/// <summary>
/// Idiomatic, WIT-independent mirror of `waddle:bundle/types.platform-event`
/// (`wit/waddle-bundle/stage.wit`). A bundle's thin `ProcessStageExportsImpl`
/// converts the wit-bindgen-generated `Types.PlatformEvent` to/from this
/// record via a plain field-by-field copy -- see the SDK README.
/// </summary>
public sealed record PlatformEventInfo(
    string Platform,
    string EventType,
    string? Actor,
    string PayloadJson,
    string OccurredAt)
{
    /// <summary>
    /// Deserializes <see cref="PayloadJson"/> as <typeparamref name="T"/> using a
    /// caller-supplied source-generated <see cref="JsonTypeInfo{T}"/> -- never
    /// reflection-based `JsonSerializer.Deserialize&lt;T&gt;()`, which NativeAOT-LLVM's
    /// trimmer either strips (silent data loss) or throws for at runtime.
    /// Returns null if the payload is not valid JSON or does not match the shape.
    /// </summary>
    public T? Payload<T>(JsonTypeInfo<T> typeInfo)
    {
        try
        {
            return JsonSerializer.Deserialize(PayloadJson, typeInfo);
        }
        catch (JsonException)
        {
            return default;
        }
    }

    /// <summary>
    /// Builds a new <see cref="PlatformEventInfo"/> carrying <paramref name="payload"/>
    /// as its <see cref="PayloadJson"/>, serialized via the caller-supplied source-generated
    /// <paramref name="typeInfo"/>. Every other field is copied from <paramref name="source"/>
    /// unchanged, mirroring the "same platform/channel, new payload" reply pattern every
    /// Tier-1 SDK sibling's `transform` uses.
    /// </summary>
    public static PlatformEventInfo WithPayload<T>(PlatformEventInfo source, T payload, JsonTypeInfo<T> typeInfo) =>
        source with { PayloadJson = JsonSerializer.Serialize(payload, typeInfo) };
}

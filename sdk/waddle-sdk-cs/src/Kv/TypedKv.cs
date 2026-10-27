using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

namespace WaddleSdk.Kv;

/// <summary>
/// JSON-typed convenience helpers over <see cref="IKvClient"/>'s raw byte-array
/// API, using a caller-supplied source-generated <see cref="JsonTypeInfo{T}"/>
/// (never reflection-based `JsonSerializer.Deserialize&lt;T&gt;()` -- see
/// `PlatformEventInfo.Payload` for why).
/// </summary>
public static class TypedKv
{
    /// <summary>Deserializes the value at <paramref name="key"/> as <typeparamref name="T"/>,
    /// or returns <c>default</c> if the key is unset or its bytes are not valid JSON for
    /// <typeparamref name="T"/>.</summary>
    public static T? GetJson<T>(IKvClient kv, string key, JsonTypeInfo<T> typeInfo)
    {
        var bytes = kv.Get(key);
        if (bytes is null)
        {
            return default;
        }

        try
        {
            return JsonSerializer.Deserialize(bytes, typeInfo);
        }
        catch (JsonException)
        {
            return default;
        }
    }

    /// <summary>Serializes <paramref name="value"/> as JSON and stores it at <paramref name="key"/>
    /// with the given TTL (0 = no expiry).</summary>
    public static void SetJson<T>(IKvClient kv, string key, T value, JsonTypeInfo<T> typeInfo, uint ttlSeconds = 0)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(value, typeInfo);
        kv.Set(key, bytes, ttlSeconds);
    }
}

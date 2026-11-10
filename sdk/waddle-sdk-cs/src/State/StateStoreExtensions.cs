using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

namespace WaddleSdk.State;

/// <summary>Reflection-free JSON helpers over <see cref="IStateStore"/> (source-generated type info only).</summary>
public static class StateStoreExtensions
{
    /// <summary>Deserializes the value at <paramref name="key"/>; null if absent, expired or not valid JSON for <typeparamref name="T"/>.</summary>
    public static T? GetJson<T>(this IStateStore store, string key, JsonTypeInfo<T> typeInfo)
    {
        var bytes = store.Get(key);
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

    /// <summary>Serializes <paramref name="value"/> and stores it with the given TTL (0 = none).</summary>
    public static void SetJson<T>(this IStateStore store, string key, T value, JsonTypeInfo<T> typeInfo, uint ttlSeconds = 0) =>
        store.Set(key, JsonSerializer.SerializeToUtf8Bytes(value, typeInfo), ttlSeconds);
}

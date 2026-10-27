namespace WaddleSdk.Kv;

/// <summary>
/// Typed wrapper over `waddle:bundle/kv` (`wit/waddle-bundle/stage.wit`) --
/// bundle-scoped key/value storage, stored under the bundle's own `...:state`
/// key, always granted. A bundle's per-project adapter implements this by
/// delegating to the generated `IKvImports` static methods and rethrowing
/// <see cref="WaddleKvException"/> in place of the generated `WitException&lt;
/// IKvImports.Error&gt;` -- see the SDK README.
/// </summary>
public interface IKvClient
{
    /// <summary>Returns the raw bytes stored at <paramref name="key"/>, or null if unset.</summary>
    byte[]? Get(string key);

    /// <summary><paramref name="ttlSeconds"/> = 0 means "no expiry"; the host clamps to KV_MAX_TTL_S.</summary>
    void Set(string key, byte[] value, uint ttlSeconds);

    void Delete(string key);

    /// <summary>Atomically adds <paramref name="delta"/> to the counter at <paramref name="key"/>
    /// (creating it at 0 first if unset) and returns the new value.</summary>
    long Increment(string key, long delta, uint ttlSeconds);
}

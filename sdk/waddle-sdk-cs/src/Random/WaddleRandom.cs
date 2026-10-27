namespace WaddleSdk.Random;

/// <summary>
/// Thin wrapper over <see cref="System.Random.Shared"/> for bundle authors who
/// want a named, discoverable entry point rather than reaching for the BCL type
/// directly. No dedicated WIT `random` import exists in `wit/waddle-bundle/stage.wit`
/// (the world imports only `context`/`http`/`kv`/`db`/`relay`/`%flags`/`log`/`clock`) --
/// on the `wasi-wasm` target, the .NET runtime's own RNG seeding transitively
/// pulls in `wasi:random/random@0.2.6` (confirmed present in
/// `bundles/csharp/csping`'s compiled import set even though that bundle never
/// calls `System.Random` itself), so `System.Random.Shared` already works
/// correctly, host-call-free, without this SDK needing its own WIT binding.
/// </summary>
public static class WaddleRandom
{
    /// <summary>A random integer in the inclusive range [<paramref name="minInclusive"/>, <paramref name="maxInclusive"/>].</summary>
    public static int NextInt(int minInclusive, int maxInclusive)
    {
        if (maxInclusive < minInclusive)
        {
            throw new ArgumentOutOfRangeException(nameof(maxInclusive), maxInclusive,
                $"must be >= minInclusive ({minInclusive})");
        }

        return System.Random.Shared.Next(minInclusive, maxInclusive + 1);
    }

    /// <summary>A random double in [0.0, 1.0).</summary>
    public static double NextDouble() => System.Random.Shared.NextDouble();

    /// <summary>Picks a uniformly random element from <paramref name="items"/>.</summary>
    public static T Pick<T>(IReadOnlyList<T> items)
    {
        if (items.Count == 0)
        {
            throw new ArgumentException("items must be non-empty", nameof(items));
        }

        return items[System.Random.Shared.Next(items.Count)];
    }
}

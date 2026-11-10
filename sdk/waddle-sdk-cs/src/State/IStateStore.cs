namespace WaddleSdk.State;

/// <summary>
/// Small expiring key/value state for a bundle (cooldowns, per-event outcome records, jackpot pots, join
/// windows). Two implementations ship: <see cref="KvStateStore"/> (host `kv`; needs the `storage.kv`
/// permission) and <see cref="DbStateStore"/> (the bundle's own `db` table; needs `data.tables` and the
/// `waddles.bundle-db-capability` flag). Game code is written against this interface so a port can pick
/// the store its host actually grants without changing its logic. Keys are built with
/// <see cref="StateKey"/> (no `:`, host `kv` rejects it).
/// </summary>
public interface IStateStore
{
    /// <summary>True when <see cref="Mutate"/> is a real compare-and-swap (db row versions); false when it is a best-effort read-modify-write (kv has no CAS).</summary>
    bool AtomicMutate { get; }

    /// <summary>The value at <paramref name="key"/>, or null if absent or expired (lazy expiry).</summary>
    byte[]? Get(string key);

    /// <summary>Stores <paramref name="value"/>; <paramref name="ttlSeconds"/> = 0 means no expiry.</summary>
    void Set(string key, byte[] value, uint ttlSeconds);

    void Delete(string key);

    /// <summary>First-writer-wins claim of <paramref name="key"/> for <paramref name="ttlSeconds"/>: true for exactly one caller until it expires.</summary>
    bool TryClaim(string key, uint ttlSeconds);

    /// <summary>
    /// Read-modify-write: <paramref name="mutate"/> receives the current value (null if absent/expired) and returns the new value
    /// (null deletes). Retries up to <paramref name="maxAttempts"/> times on an optimistic-concurrency conflict (db only);
    /// returns false if every attempt conflicted.
    /// </summary>
    bool Mutate(string key, Func<byte[]?, byte[]?> mutate, uint ttlSeconds, int maxAttempts = 5);
}

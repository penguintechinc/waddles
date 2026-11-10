using WaddleSdk.Kv;

namespace WaddleSdk.State;

/// <summary>
/// <see cref="IStateStore"/> over host `kv`. `TryClaim` is atomic (`kv.increment`); <see cref="Mutate"/> is NOT (kv has no
/// compare-and-swap), so two events racing on one key can lose an update -- keep money-moving shared state (pots, wagers
/// settled later) in <see cref="DbStateStore"/> or behind `TryClaim`.
/// </summary>
public sealed class KvStateStore(IKvClient kv) : IStateStore
{
    public bool AtomicMutate => false;

    public byte[]? Get(string key) => kv.Get(key);

    public void Set(string key, byte[] value, uint ttlSeconds) => kv.Set(key, value, ttlSeconds);

    public void Delete(string key) => kv.Delete(key);

    /// <summary>
    /// The host refreshes a key's TTL on every `increment`, so a plain increment-claim would let a spamming user extend their
    /// own cooldown forever. The cheap `get` rejects the common already-claimed case without touching the TTL; the
    /// `increment == 1` check then arbitrates two simultaneous first claims.
    /// </summary>
    public bool TryClaim(string key, uint ttlSeconds)
    {
        if (kv.Get(key) is not null)
        {
            return false;
        }

        return kv.Increment(key, 1, ttlSeconds) == 1;
    }

    public bool Mutate(string key, Func<byte[]?, byte[]?> mutate, uint ttlSeconds, int maxAttempts = 5)
    {
        var next = mutate(kv.Get(key));
        if (next is null)
        {
            kv.Delete(key);
        }
        else
        {
            kv.Set(key, next, ttlSeconds);
        }

        return true;
    }
}

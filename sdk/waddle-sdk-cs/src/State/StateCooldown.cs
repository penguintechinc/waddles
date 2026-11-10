namespace WaddleSdk.State;

/// <summary>
/// Per-user, per-command cooldowns over any <see cref="IStateStore"/>. Unlike <see cref="Cooldown.CooldownGuard"/> (kv-only,
/// kv-denial tolerant), this is store-agnostic and FAILS LOUD when the store is unavailable: a cooldown guarding a money game
/// that silently stops guarding is a defect. The user key is hashed, so no actor id or UUID reaches a key.
/// </summary>
public static class StateCooldown
{
    /// <summary>Starts the cooldown if <paramref name="userId"/> is not on cooldown for <paramref name="scope"/>; false if they are.</summary>
    public static bool TryAcquire(IStateStore store, string scope, string userId, uint cooldownSeconds) =>
        store.TryClaim(KeyFor(scope, userId), cooldownSeconds);

    /// <summary>Releases a cooldown taken by <see cref="TryAcquire"/> (e.g. the command then failed and must not cost the user their slot).</summary>
    public static void Release(IStateStore store, string scope, string userId) => store.Delete(KeyFor(scope, userId));

    private static string KeyFor(string scope, string userId) => StateKey.Of("cd", scope, StateKey.Hash(userId));
}

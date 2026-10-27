using WaddleSdk.Kv;

namespace WaddleSdk.Cooldown;

/// <summary>
/// Per-user command cooldowns built on <see cref="IKvClient"/>: the key's own
/// TTL is the cooldown timer, so no separate expiry bookkeeping is needed.
/// Mirrors the original PenguinTwitchBot per-command `userCooldown` semantics
/// (`BaseCommandService.RegisterDefaultCommand`'s `userCooldown` seconds
/// parameter) that ported bundles commonly need to reproduce.
/// </summary>
public static class CooldownGuard
{
    private const string KeyPrefix = "cooldown";

    /// <summary>
    /// Attempts to acquire the cooldown slot for <paramref name="userId"/> under
    /// <paramref name="scope"/> (typically the command name, e.g. `"roll"`).
    /// Returns true and starts the cooldown if the user was not already on
    /// cooldown; returns false (and leaves the existing cooldown untouched) if
    /// they were.
    ///
    /// <para><b>Degrades gracefully when `kv` is denied.</b> As of 2026-09-28 the
    /// host `kv` capability is hardcoded to deny every call in svc-process/
    /// svc-action (see <see cref="KvErrorKind.Denied"/>). A cooldown is a
    /// best-effort convenience, never a correctness requirement -- so a
    /// <see cref="WaddleKvException"/> with <see cref="KvErrorKind.Denied"/> is
    /// caught here and treated as "cooldown state unavailable, allow the
    /// command through" rather than failing the whole command over a feature
    /// that cannot currently be enforced. Any OTHER `kv` error
    /// (<see cref="KvErrorKind.TooLarge"/>/<see cref="KvErrorKind.Backend"/>) is a
    /// genuine, unexpected failure and is deliberately NOT swallowed here.</para>
    /// </summary>
    public static bool TryAcquire(IKvClient kv, string scope, string userId, uint cooldownSeconds)
    {
        var key = BuildKey(scope, userId);
        try
        {
            if (kv.Get(key) is not null)
            {
                return false;
            }

            // The value itself is unused -- the key's existence (and TTL) IS the
            // cooldown state. A single byte keeps the `kv` payload minimal.
            kv.Set(key, [1], cooldownSeconds);
            return true;
        }
        catch (WaddleKvException ex) when (ex.Kind == KvErrorKind.Denied)
        {
            return true;
        }
    }

    private static string BuildKey(string scope, string userId) => $"{KeyPrefix}:{scope}:{userId}";
}

namespace WaddleSdk.Kv;

/// <summary>
/// Mirrors `waddle:bundle/kv`'s `error` variant (`wit/waddle-bundle/stage.wit`) --
/// `TooLarge`/`Backend` -- plus <see cref="Denied"/>, an SDK-level addition with
/// no WIT-level tag of its own (the `kv` interface has no `denied` case,
/// unlike `db`/`http`/`relay`). As of 2026-09-28 the host `kv` capability is
/// hardcoded to deny every call in svc-process/svc-action while the real
/// backend is being implemented; a bundle's per-project adapter maps that
/// condition onto this value (see the SDK README "kv/db availability")
/// so callers can distinguish "not available yet" from a genuine backend
/// failure without string-sniffing an error message themselves.
/// </summary>
public enum KvErrorKind
{
    Denied,
    TooLarge,
    Backend,
}

/// <summary>
/// Thrown by <see cref="IKvClient"/> implementations for a `kv` host-call failure.
/// A bundle's per-project adapter catches the generated `WitException&lt;IKvImports.Error&gt;`
/// and rethrows this instead -- see the SDK README.
/// </summary>
public sealed class WaddleKvException(KvErrorKind kind, string message, ulong? tooLargeBytes = null)
    : Exception(message)
{
    public KvErrorKind Kind { get; } = kind;

    /// <summary>Populated only when <see cref="Kind"/> is <see cref="KvErrorKind.TooLarge"/>.</summary>
    public ulong? TooLargeBytes { get; } = tooLargeBytes;
}

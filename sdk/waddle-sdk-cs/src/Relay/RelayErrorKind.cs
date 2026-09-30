namespace WaddleSdk.Relay;

/// <summary>Mirrors `waddle:bundle/relay`'s `error` variant (`wit/waddle-bundle/stage.wit`).</summary>
public enum RelayErrorKind
{
    Denied,
    Backend,
}

/// <summary>
/// Thrown by <see cref="IRelayClient"/> implementations for a `relay` host-call failure.
/// A bundle's per-project adapter catches the generated `WitException&lt;IRelayImports.Error&gt;`
/// and rethrows this instead -- see the SDK README.
/// </summary>
public sealed class WaddleRelayException(RelayErrorKind kind, string message) : Exception(message)
{
    public RelayErrorKind Kind { get; } = kind;
}

namespace WaddleSdk.Types;

/// <summary>
/// Idiomatic mirror of `waddle:bundle/types.stage-envelope` -- the argument
/// `action-stage.dispatch` receives. See <see cref="PlatformEventInfo"/> for
/// why this SDK never references the wit-bindgen-generated type directly.
/// </summary>
public sealed record StageEnvelopeInfo(
    string Tenant,
    string? Community,
    string AppId,
    string Stage,
    PlatformEventInfo Event,
    string Ts,
    string? TargetAppId,
    string? TraceContext);

/// <summary>Idiomatic mirror of `waddle:bundle/types.transport-result`.</summary>
public sealed record TransportResultInfo(bool Ok, ushort? Status, string? Detail, string? ProviderMessageId)
{
    /// <summary>A successful result with no status/detail/message-id -- the common case
    /// for a bundle whose only action-stage work is a single `relay.push`.</summary>
    public static TransportResultInfo Success(string? detail = null, string? providerMessageId = null) =>
        new(true, null, detail, providerMessageId);
}

/// <summary>Idiomatic mirror of `waddle:bundle/types.transport-error`.</summary>
public sealed record TransportErrorInfo(bool Retryable, string Code, string Message, uint? RetryAfterMs)
{
    /// <summary>A non-retryable (fatal) error -- e.g. a malformed reply payload
    /// the bundle's own `transform` should never have produced.</summary>
    public static TransportErrorInfo Fatal(string code, string message) => new(false, code, message, null);

    /// <summary>A retryable error -- e.g. a transient `relay.push` failure. Named
    /// `RetryableError` (not `Retryable`) to avoid colliding with this record's own
    /// `Retryable` property.</summary>
    public static TransportErrorInfo RetryableError(string code, string message, uint? retryAfterMs = null) =>
        new(true, code, message, retryAfterMs);
}

/// <summary>
/// Thrown by a <see cref="Stage.WaddleActionStage"/> implementation to signal a
/// `transport-error` result. The bundle's thin `ActionStageExportsImpl` catches this
/// and converts <see cref="Error"/> into the wit-bindgen-generated
/// `WitException&lt;Types.TransportError&gt;` -- see the SDK README.
/// </summary>
public sealed class WaddleTransportException(TransportErrorInfo error) : Exception(error.Message)
{
    public TransportErrorInfo Error { get; } = error;
}

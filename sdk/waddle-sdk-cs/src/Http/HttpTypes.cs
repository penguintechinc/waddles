namespace WaddleSdk.Http;

/// <summary>Idiomatic mirror of `waddle:bundle/http.header`.</summary>
public sealed record HttpHeader(string Name, string Value);

/// <summary>
/// Idiomatic mirror of `waddle:bundle/http.request`. <see cref="SecretRefs"/> maps a
/// header name to a secret reference name -- the stage resolves the reference and
/// injects the header; the secret value never enters the component.
/// </summary>
public sealed record HttpRequestInfo(
    string Method,
    string Url,
    IReadOnlyList<HttpHeader> Headers,
    byte[]? Body,
    IReadOnlyList<(string HeaderName, string SecretRefName)> SecretRefs);

/// <summary>Idiomatic mirror of `waddle:bundle/http.response`.</summary>
public sealed record HttpResponseInfo(ushort Status, IReadOnlyList<HttpHeader> Headers, byte[] Body, bool Truncated);

/// <summary>Mirrors `waddle:bundle/http`'s `error` variant.</summary>
public enum HttpErrorKind
{
    Denied,
    Timeout,
    TooLarge,
    RateLimited,
    Transport,
}

/// <summary>
/// Thrown by <see cref="IHttpClient"/> implementations for an `http` host-call failure.
/// A bundle's per-project adapter catches the generated `WitException&lt;IHttpImports.Error&gt;`
/// and rethrows this instead -- see the SDK README.
/// </summary>
public sealed class WaddleHttpException(HttpErrorKind kind, string message, ulong? tooLargeBytes = null, uint? rateLimitedSeconds = null)
    : Exception(message)
{
    public HttpErrorKind Kind { get; } = kind;
    public ulong? TooLargeBytes { get; } = tooLargeBytes;
    public uint? RateLimitedSeconds { get; } = rateLimitedSeconds;
}

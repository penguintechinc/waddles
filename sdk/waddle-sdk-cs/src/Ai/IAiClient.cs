namespace WaddleSdk.Ai;

/// <summary>Mirrors `waddle:bundle/ai`'s `error` variant (`stage-next`/`stage-v2` worlds only).</summary>
public enum AiErrorKind
{
    /// <summary>Gate denial or the host refused the prompt's content (`contains_pii`: tokenize/strip it first).</summary>
    Denied,

    /// <summary>Prompt or <c>MaxTokens</c> over the host bound; <see cref="WaddleAiException.Bound"/> carries the bound.</summary>
    TooManyTokens,

    ContentFiltered,
    Invalid,

    /// <summary>Flag OFF / no model backend / backend unreachable. Fail loud -- never substitute a canned answer.</summary>
    Unavailable,

    Backend,
}

/// <summary>Thrown by <see cref="IAiClient"/> implementations.</summary>
public sealed class WaddleAiException(AiErrorKind kind, string message, uint? bound = null) : Exception(message)
{
    public AiErrorKind Kind { get; } = kind;

    /// <summary>Populated only for <see cref="AiErrorKind.TooManyTokens"/>.</summary>
    public uint? Bound { get; } = bound;
}

/// <summary>Per-call generation options; every field is a REQUEST the host clamps. <c>Json</c> selects the structured-output model.</summary>
public sealed record AiOptions(uint? MaxTokens = null, float? Temperature = null, bool Json = false);

/// <summary>
/// Typed wrapper over `waddle:bundle/ai.generate` -- host-mediated text generation against the
/// platform's self-hosted model. The completion is untrusted text, never a command or identity
/// claim. Needs `ai.generate` (dangerous, consent-ack, 10 calls/60 s) and the default-OFF flag
/// `waddles.bundle-ai-capability`.
/// </summary>
public interface IAiClient
{
    /// <summary>Generates a completion for <paramref name="prompt"/> (host-framed as untrusted data).</summary>
    string Generate(string prompt, AiOptions options);
}

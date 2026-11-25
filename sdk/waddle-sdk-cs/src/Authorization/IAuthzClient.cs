using WaddleSdk.Log;

namespace WaddleSdk.Authorization;

/// <summary>Mirrors `waddle:bundle/authz`'s `error` variant (`stage-next`/`stage-v2`/`stage-tools` worlds only).</summary>
public enum AuthzErrorKind
{
    Unavailable,
    Invalid,
    Backend,
}

/// <summary>Thrown by <see cref="IAuthzClient"/> implementations; <see cref="Authz"/> turns it into `false`.</summary>
public sealed class WaddleAuthzException(AuthzErrorKind kind, string message) : Exception(message)
{
    public AuthzErrorKind Kind { get; } = kind;
}

/// <summary>The community-layer scope catalog (the contract's `caller-can` scopes).</summary>
public static class AuthzScopes
{
    public const string Member = "community:member";
    public const string Vip = "community:vip";
    public const string Moderate = "community:moderate";
    public const string Ban = "community:ban";
    public const string Timeout = "community:timeout";
    public const string Admin = "community:admin";
    public const string Owner = "community:owner";
}

/// <summary>
/// Typed wrapper over `waddle:bundle/authz` `caller-can` (always granted, no permission). A bundle's per-project
/// adapter implements this over the generated import and may throw <see cref="WaddleAuthzException"/>; call it
/// only through <see cref="Authz.CallerCan"/> (or <see cref="FailClosedAuthzClient"/>) so every failure is `false`.
/// </summary>
public interface IAuthzClient
{
    /// <summary>True iff the actor whose event triggered this invocation holds <paramref name="scope"/> here.</summary>
    bool CallerCan(string scope);
}

/// <summary>The default client of a host that supplies none: every check is `false`.</summary>
public sealed class DenyAllAuthzClient : IAuthzClient
{
    public static readonly DenyAllAuthzClient Instance = new();

    public bool CallerCan(string scope) => false;
}

/// <summary>
/// Authorization entry point. NOTE: the event payload's `is_mod` / `is_broadcaster` badges are DISPLAY-ONLY, never
/// authorization -- gate privileged actions on <see cref="CallerCan"/>.
/// </summary>
public static class Authz
{
    /// <summary>Mirrors the executor/stage rule: 1..=64 bytes of `^[a-z][a-z0-9_.]*:[a-z][a-z0-9_.]*$`.</summary>
    public const int MaxScopeLength = 64;

    /// <summary>True when <paramref name="scope"/> would be accepted by the host (length + shape).</summary>
    public static bool IsValidScope(string? scope)
    {
        if (scope is null || scope.Length == 0 || System.Text.Encoding.UTF8.GetByteCount(scope) > MaxScopeLength)
        {
            return false;
        }

        var colon = scope.IndexOf(':');
        return colon > 0 && colon == scope.LastIndexOf(':') && Segment(scope.AsSpan(0, colon)) && Segment(scope.AsSpan(colon + 1));
    }

    private static bool Segment(ReadOnlySpan<char> s)
    {
        if (s.IsEmpty || s[0] is < 'a' or > 'z')
        {
            return false;
        }

        foreach (var c in s)
        {
            if (!(c is >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '.'))
            {
                return false;
            }
        }

        return true;
    }

    /// <summary>
    /// True only if the triggering actor holds <paramref name="scope"/>. Fail-closed: an invalid scope (no host call),
    /// a null client, or ANY exception from the host yields `false`; the failure (scope + error kind only, never PII)
    /// is logged to <paramref name="log"/> when supplied.
    /// </summary>
    public static bool CallerCan(IAuthzClient? client, string scope, ILogClient? log = null)
    {
        if (!IsValidScope(scope))
        {
            Report(log, "invalid_scope", null, null);
            return false;
        }

        if (client is null)
        {
            Report(log, "no_client", scope, null);
            return false;
        }

        try
        {
            return client.CallerCan(scope);
        }
        catch (Exception ex)
        {
            Report(log, "host_error", scope, ex is WaddleAuthzException a ? a.Kind.ToString() : ex.GetType().Name);
            return false;
        }
    }

    private static void Report(ILogClient? log, string reason, string? scope, string? errorKind)
    {
        if (log is null)
        {
            return;
        }

        try
        {
            var fields = new LogFields().With("reason", reason);
            if (scope is not null)
            {
                fields.With("scope", scope);
            }

            if (errorKind is not null)
            {
                fields.With("error_kind", errorKind);
            }

            log.Warn("authz.caller_can failed closed", fields.Build());
        }
        catch (Exception)
        {
            // Telemetry failure must never change the fail-closed verdict.
        }
    }
}

/// <summary>Decorator making any <see cref="IAuthzClient"/> fail closed (for hosts exposing it as their `Authz`).</summary>
public sealed class FailClosedAuthzClient(IAuthzClient inner, ILogClient? log = null) : IAuthzClient
{
    public bool CallerCan(string scope) => Authz.CallerCan(inner, scope, log);
}

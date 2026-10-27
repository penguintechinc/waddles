namespace WaddleSdk.Flags;

/// <summary>
/// Typed wrapper over `waddle:bundle/%flags` (`wit/waddle-bundle/stage.wit`) --
/// PostHog flag + license entitlement, two-gate, cached, fail-open to the
/// supplied default (`rules/critical-rules.md` Feature Flags &amp; License Tiers).
/// A bundle's per-project adapter implements this by delegating to the
/// generated `IFlagsImports` static methods -- see the SDK README.
/// </summary>
public interface IFlagsClient
{
    /// <summary>Resolves a PostHog flag by key, falling back to <paramref name="defaultValue"/>
    /// when the flag has never been seen or the flag/license server is unreachable.</summary>
    bool Enabled(string key, bool defaultValue);

    /// <summary>The caller's license tier: `"free"`, `"professional"`, or `"enterprise"`.</summary>
    string Tier();
}

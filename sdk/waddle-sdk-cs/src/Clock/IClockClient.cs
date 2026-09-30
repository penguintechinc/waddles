namespace WaddleSdk.Clock;

/// <summary>
/// Typed wrapper over `waddle:bundle/clock` (`wit/waddle-bundle/stage.wit`).
/// A bundle's per-project adapter implements this by delegating to the
/// generated `IClockImports` static methods -- see the SDK README.
/// </summary>
public interface IClockClient
{
    /// <summary>Milliseconds since the Unix epoch, as the stage sees it.</summary>
    ulong NowMillis();

    /// <summary>RFC 3339 UTC, millisecond precision.</summary>
    string NowRfc3339();

    /// <summary>Monotonic nanoseconds, for in-bundle duration measurement only.</summary>
    ulong MonotonicNanos();
}

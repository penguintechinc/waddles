namespace WaddleSdk.Log;

/// <summary>
/// Typed wrapper over `waddle:bundle/log` (`wit/waddle-bundle/stage.wit`) --
/// sanitized, levelled logging into the stage's OTel pipeline, always granted.
/// A bundle's per-project adapter implements this by delegating to the
/// generated `ILogImports.Write` static method -- see the SDK README.
/// </summary>
public interface ILogClient
{
    /// <summary><paramref name="fieldsJson"/> must be a canonical JSON object (use
    /// <see cref="LogFields"/> to build one without reflection); the host sanitizes it
    /// with the penguin logging SENSITIVE_KEYS rule before emission.</summary>
    void Write(WaddleLogLevel level, string message, string fieldsJson);
}

/// <summary>Convenience extension methods for <see cref="ILogClient"/> matching the
/// four `rules/critical-rules.md` Observability levels.</summary>
public static class LogClientExtensions
{
    public static void Error(this ILogClient log, string message, string fieldsJson = "{}") =>
        log.Write(WaddleLogLevel.Error, message, fieldsJson);

    public static void Warn(this ILogClient log, string message, string fieldsJson = "{}") =>
        log.Write(WaddleLogLevel.Warn, message, fieldsJson);

    public static void Info(this ILogClient log, string message, string fieldsJson = "{}") =>
        log.Write(WaddleLogLevel.Info, message, fieldsJson);

    public static void Debug(this ILogClient log, string message, string fieldsJson = "{}") =>
        log.Write(WaddleLogLevel.Debug, message, fieldsJson);
}

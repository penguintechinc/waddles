using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

namespace WaddleSdk.Tool;

/// <summary>Mirrors `waddle:bundle/tool`'s `tool-error` variant (`stage-next-tools`/`stage-tools` worlds only).</summary>
public enum ToolErrorKind
{
    /// <summary>No approved grant for this caller/target/tool (declare `tool.call:&lt;target&gt;:&lt;tool&gt;` in the manifest).</summary>
    NotGranted,

    /// <summary>Grant present but the kind matrix forbids it (app to app is forbidden).</summary>
    Forbidden,

    Timeout,
    DepthExceeded,
    PoolExhausted,
    TargetUnavailable,
    InvalidArgs,
}

/// <summary>Thrown by <see cref="IToolClient"/> implementations.</summary>
public sealed class WaddleToolException(ToolErrorKind kind, string message) : Exception(message)
{
    public ToolErrorKind Kind { get; } = kind;

    /// <summary>The stable snake_case code the Python SDK and the host logs use for this kind.</summary>
    public string Code => Kind switch
    {
        ToolErrorKind.NotGranted => "not_granted",
        ToolErrorKind.Forbidden => "forbidden",
        ToolErrorKind.Timeout => "timeout",
        ToolErrorKind.DepthExceeded => "depth_exceeded",
        ToolErrorKind.PoolExhausted => "pool_exhausted",
        ToolErrorKind.TargetUnavailable => "target_unavailable",
        _ => "invalid_args",
    };
}

/// <summary>
/// Typed wrapper over `waddle:bundle/tool.invoke`: calls another SERVICE bundle's exported tool. The
/// host is the sole authority (it gates, resolves and runs the target under the target's own grants);
/// the caller can never name the community/tenant/actor. Arguments and results are canonical JSON bytes.
/// Writes MUST carry an idempotency key (retry-safe) -- derive it from the event, never from a clock.
/// </summary>
public interface IToolClient
{
    /// <summary>Invokes <paramref name="tool"/> on <paramref name="target"/> (a 4-segment `waddles.a.b.c` app id).</summary>
    byte[] Invoke(string target, string tool, byte[] argsJson, string? idempotencyKey);
}

/// <summary>Reflection-free JSON conveniences over <see cref="IToolClient"/>.</summary>
public static class ToolClientExtensions
{
    /// <summary>
    /// Serializes <paramref name="args"/> via the source-generated <paramref name="argsInfo"/>, invokes the tool and
    /// deserializes the result via <paramref name="resultInfo"/>. A result that does not match the shape throws
    /// <see cref="WaddleToolException"/> (<see cref="ToolErrorKind.InvalidArgs"/>) -- a contract mismatch is loud, never null.
    /// </summary>
    public static TResult Invoke<TArgs, TResult>(
        this IToolClient client,
        string target,
        string tool,
        TArgs args,
        JsonTypeInfo<TArgs> argsInfo,
        JsonTypeInfo<TResult> resultInfo,
        string? idempotencyKey = null)
    {
        var bytes = client.Invoke(target, tool, JsonSerializer.SerializeToUtf8Bytes(args, argsInfo), idempotencyKey);
        try
        {
            return JsonSerializer.Deserialize(bytes, resultInfo)
                ?? throw new WaddleToolException(ToolErrorKind.InvalidArgs, $"{target}/{tool} returned null");
        }
        catch (JsonException ex)
        {
            throw new WaddleToolException(ToolErrorKind.InvalidArgs, $"{target}/{tool} result did not match the expected contract: {ex.Message}");
        }
    }
}

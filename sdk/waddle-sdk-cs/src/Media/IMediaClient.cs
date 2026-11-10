namespace WaddleSdk.Media;

/// <summary>Mirrors `waddle:bundle/media`'s `error` variant (`stage-next`/`stage-v2` worlds only).</summary>
public enum MediaErrorKind
{
    /// <summary>Effective tier below Professional (or license unknown, fail closed); the message carries the detail.</summary>
    TierRequired,

    ByokMissing,
    ProviderRefused,
    Quota,
    Timeout,
    Denied,
    Invalid,
    Unavailable,
    NotFound,
    Backend,
}

/// <summary>Thrown by <see cref="IMediaClient"/> implementations.</summary>
public sealed class WaddleMediaException(MediaErrorKind kind, string message) : Exception(message)
{
    public MediaErrorKind Kind { get; } = kind;
}

/// <summary>Discriminates <see cref="MediaJobState"/>.</summary>
public enum MediaJobStatus
{
    Pending,
    Done,
    Failed,
}

/// <summary>Result of polling a media job: pending, done (with an opaque object handle) or failed (with the error kind/message).</summary>
public sealed record MediaJobState(MediaJobStatus Status, string? Handle = null, MediaErrorKind? Error = null, string? ErrorMessage = null)
{
    public static MediaJobState Pending() => new(MediaJobStatus.Pending);
    public static MediaJobState Done(string handle) => new(MediaJobStatus.Done, Handle: handle);
    public static MediaJobState Failed(MediaErrorKind error, string message) => new(MediaJobStatus.Failed, Error: error, ErrorMessage: message);
}

/// <summary>
/// Typed wrapper over `waddle:bundle/media` -- host-mediated image/music/video generation. Outputs are
/// opaque object handles, never bytes or public URLs; only the requesting bundle can resolve a handle.
/// Needs `media.generate` (dangerous; Professional+ and BYOK enforced host-side) and the default-OFF flag
/// `waddles.harness-media-generation`.
/// </summary>
public interface IMediaClient
{
    /// <summary>Synchronous image generation (60 s bound); returns an object handle.</summary>
    string GenerateImage(string prompt);

    /// <summary>Starts music generation; returns a job id to poll with <see cref="JobStatus"/>.</summary>
    string SubmitMusic(string prompt);

    /// <summary>Starts video generation; returns a job id to poll with <see cref="JobStatus"/>.</summary>
    string SubmitVideo(string prompt);

    /// <summary>Polls a job started by THIS bundle in THIS community.</summary>
    MediaJobState JobStatus(string jobId);

    /// <summary>Resolves an object handle this bundle owns to a short-TTL presigned GET URL.</summary>
    string ObjectUrl(string handle);
}

namespace WaddleSdk.Identity;

/// <summary>Mirrors `waddle:bundle/identity`'s `error` variant (`stage-next`/`stage-v2` worlds only).</summary>
public enum IdentityErrorKind
{
    Denied,

    /// <summary>The identity has no resolved community `user-uuid` yet; tell the user their account is not linked.</summary>
    NotLinked,

    NotAMember,

    /// <summary>The mention token/handle is not one this message carried (or matches nothing in the tenant).</summary>
    NotFound,

    Ambiguous,
    Invalid,
    Unavailable,
    Backend,
}

/// <summary>Thrown by <see cref="IIdentityClient"/> implementations.</summary>
public sealed class WaddleIdentityException(IdentityErrorKind kind, string message) : Exception(message)
{
    public IdentityErrorKind Kind { get; } = kind;
}

/// <summary>
/// Typed wrapper over `waddle:bundle/identity`. UUID-only by construction: the host never returns a
/// platform id, handle or display name. To show a person's name in a reply, embed the literal
/// placeholder from <see cref="UserPlaceholder.For"/> -- the egress pass substitutes the display name
/// at the final send. Needs `identity.resolve`; process-stage only.
/// </summary>
public interface IIdentityClient
{
    /// <summary>The community `user-uuid` of the actor whose event triggered this invocation.</summary>
    string ResolveActor();

    /// <summary>The `user-uuid` a mention token present in the triggering message refers to.</summary>
    string ResolveMention(string token);
}

/// <summary>Builds the `{user:&lt;uuid&gt;}` placeholder the egress detokenizer renders as a display name.</summary>
public static class UserPlaceholder
{
    /// <summary>Returns `{user:uuid}` for <paramref name="userUuid"/>.</summary>
    public static string For(string userUuid) => "{user:" + userUuid + "}";
}

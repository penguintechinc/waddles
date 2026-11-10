using System.Text.Json;

namespace SuperPenguinShared;

/// <summary>
/// Reads the host-supplied privilege badges (`is_mod`, `is_broadcaster`, `is_bot`) off a chat event payload. Fail-closed:
/// only a JSON literal `true` counts; absent badges, strings, numbers and malformed JSON all read as "not privileged"
/// (the Python bundles use the same strict `is True` rule). The SDK's typed payload does not carry these fields, so the
/// payload is probed with the reflection-free <see cref="JsonDocument"/>.
/// </summary>
public static class ChatPrivileges
{
    /// <summary>True only when the payload carries a literal `is_mod: true` or `is_broadcaster: true`.</summary>
    public static bool IsModerator(string payloadJson) => Flag(payloadJson, "is_mod") || Flag(payloadJson, "is_broadcaster");

    /// <summary>True only when the payload carries a literal `is_bot: true`.</summary>
    public static bool IsBot(string payloadJson) => Flag(payloadJson, "is_bot");

    /// <summary>Strict-boolean probe of one top-level payload member.</summary>
    internal static bool Flag(string payloadJson, string name)
    {
        try
        {
            using var doc = JsonDocument.Parse(payloadJson);
            return doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty(name, out var value)
                && value.ValueKind == JsonValueKind.True;
        }
        catch (JsonException)
        {
            return false;
        }
    }
}

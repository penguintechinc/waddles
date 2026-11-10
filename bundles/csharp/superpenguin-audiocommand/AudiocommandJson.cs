using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinAudiocommand;

/// <summary>One registered sound command: chat-safe name, validated file name, cooldowns in seconds, volume 0..100.</summary>
public sealed record SoundDef(
    [property: JsonPropertyName("n")] string Name,
    [property: JsonPropertyName("f")] string File,
    [property: JsonPropertyName("uc")] uint UserCooldownSeconds,
    [property: JsonPropertyName("gc")] uint GlobalCooldownSeconds,
    [property: JsonPropertyName("v")] int Volume,
    [property: JsonPropertyName("d")] bool Disabled);

/// <summary>All registered sounds of the community (one kv document, capped).</summary>
public sealed record SoundRegistry([property: JsonPropertyName("s")] List<SoundDef> Sounds);

/// <summary>A `!sfx` play recorded under the event's replay key once its cooldowns were taken, so a redelivery re-pushes without a second cooldown claim.</summary>
public sealed record PlayOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("id")] string PlayId);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(SoundRegistry))]
[JsonSerializable(typeof(PlayOutcome))]
public sealed partial class AudiocommandJsonContext : JsonSerializerContext;

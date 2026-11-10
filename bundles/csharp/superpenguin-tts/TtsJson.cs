using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinTts;

/// <summary>Arguments of the media service's `tts_synthesize` tool (contract pinned by a fixture test).</summary>
public sealed record TtsSynthesizeArgs(
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("voice")] string Voice);

/// <summary>Result of `tts_synthesize`: the https URL of the synthesized audio; a bare object handle is not enough (see the README).</summary>
public sealed record TtsSynthesizeResult(
    [property: JsonPropertyName("url")] string? Url,
    [property: JsonPropertyName("handle")] string? Handle);

/// <summary>The community's registered voice ids.</summary>
public sealed record VoiceList([property: JsonPropertyName("v")] List<string> Voices);

/// <summary>One user's chosen voice.</summary>
public sealed record UserVoice([property: JsonPropertyName("v")] string Voice);

/// <summary>A `!tts` request recorded once validated (voice picked, cooldown taken) and before synthesis, so a redelivery speaks the same thing.</summary>
public sealed record TtsOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("cmd")] string Command,
    [property: JsonPropertyName("voice")] string Voice,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("id")] string PlayId);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(TtsSynthesizeArgs))]
[JsonSerializable(typeof(TtsSynthesizeResult))]
[JsonSerializable(typeof(VoiceList))]
[JsonSerializable(typeof(UserVoice))]
[JsonSerializable(typeof(TtsOutcome))]
public sealed partial class TtsJsonContext : JsonSerializerContext;

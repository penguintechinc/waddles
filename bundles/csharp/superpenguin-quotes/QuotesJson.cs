using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinQuotes;

/// <summary>
/// One stored quote. The number is stable for the life of the community (a delete leaves a tombstone so later numbers never shift).
/// `by` is the author's community user UUID, never a name.
/// </summary>
public sealed record QuoteRecord(
    [property: JsonPropertyName("n")] long Number,
    [property: JsonPropertyName("text")] string Text,
    [property: JsonPropertyName("by")] string By,
    [property: JsonPropertyName("at")] long AtMillis,
    [property: JsonPropertyName("del")] bool Deleted);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(QuoteRecord))]
public sealed partial class QuotesJsonContext : JsonSerializerContext;
